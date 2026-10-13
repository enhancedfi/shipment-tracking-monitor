"""Map saved public tracking-page text onto TrackingSnapshot.

Carrier tracking pages need JavaScript, so this module does not fetch URLs.
A scheduled browser step writes one text file per tracking number. The
phrases below are the words those pages show, mapped onto the same customer
statuses the official API parsers use.

UPS shows "Preparing for Delivery" while the package is still at the
destination facility. The scan underneath is "Processing at UPS Facility".
That is in transit, so the customer status is shipped. It is not
out_for_delivery until the page says the package is on the vehicle
("Loaded on Delivery Vehicle", "Out for Delivery", "On vehicle for delivery").

A progress bar lists future steps, including "Delivered", before they
happen. Those labels are ignored when a real status line is also present.
A dated scan that says the package was delivered still wins over an earlier
pickup, same as a carrier API history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from shipment_monitor.models import (
    DELAYED,
    DELIVERED,
    OUT_FOR_DELIVERY,
    PENDING,
    SHIPPED,
    UNKNOWN,
    TrackingSnapshot,
    normalize_carrier,
)
from shipment_monitor.parsing import classify_text

# Longer phrases first. Prefix matching keeps the longest hit.
STATUS_PHRASES: tuple[tuple[str, str], ...] = (
    ("loaded on delivery vehicle", OUT_FOR_DELIVERY),
    ("on fedex vehicle for delivery", OUT_FOR_DELIVERY),
    ("on vehicle for delivery", OUT_FOR_DELIVERY),
    ("available for pickup", OUT_FOR_DELIVERY),
    ("out for delivery", OUT_FOR_DELIVERY),
    ("preparing for delivery", SHIPPED),
    ("processing at ups facility", SHIPPED),
    ("delivery exception", DELAYED),
    ("shipment exception", DELAYED),
    ("delivery attempt", DELAYED),
    ("weather delay", DELAYED),
    ("clearance delay", DELAYED),
    ("delivery complete", DELIVERED),
    ("we have your package", SHIPPED),
    ("shipment information sent", PENDING),
    ("shipper created a label", PENDING),
    ("label created", PENDING),
    ("pre-shipment", PENDING),
    ("pre shipment", PENDING),
    ("on the way", SHIPPED),
    ("in transit", SHIPPED),
    ("picked up", SHIPPED),
    ("pickup scan", SHIPPED),
    ("departed from facility", SHIPPED),
    ("arrived at facility", SHIPPED),
    ("delivered", DELIVERED),
    ("delayed", DELAYED),
    ("exception", DELAYED),
    ("accepted", SHIPPED),
    ("alert", DELAYED),
)

# Whole-line only. These words show up in unrelated sentences.
LINE_ONLY = frozenset({"alert"})

# Stepper labels the public pages render for steps that have not happened.
MILESTONE_LABELS = frozenset(
    {
        "label created",
        "we have your package",
        "on the way",
        "out for delivery",
        "delivered",
        "in transit",
        "shipped",
    }
)

PROGRESS_MARKERS = (
    "shipment progress",
    "travel history",
    "tracking history",
    "shipment activity",
    "scan history",
)

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

DATE_TIME = re.compile(
    r"(?P<date>"
    r"(?:\d{1,2}/\d{1,2}/\d{4})"
    r"|(?:\d{4}-\d{2}-\d{2})"
    r"|(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+\d{4})"
    r")"
    r"(?:[,\s]+(?:at\s+)?)?"
    r"(?P<time>\d{1,2}:\d{2}\s*[AP]\.?\s*M\.?)?",
    re.IGNORECASE,
)

# Weekday and month-day ETAs that do not include a year: "Wednesday, October 14", "Oct 14".
_ETA_NAMED = re.compile(
    r"(?<![A-Za-z])"
    r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:(?:,|\s)+(?P<year>\d{4}))?",
    re.IGNORECASE,
)
# A clock glued to a date is a scan, not an estimated-delivery day.
_CLOCK_AFTER = re.compile(
    r"^[\s,]+(?:at\s+)?\d{1,2}:\d{2}\s*[AP]\.?\s*M",
    re.IGNORECASE,
)
# A yearless date this many days before today stays in the past, including
# Dec 31 checked on Jan 1. Older than that rolls to the next occurrence.
YEARLESS_PAST_DAYS = 60

_STATE_ZIP = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")
_STATE_COMMA = re.compile(r"\b[A-Z]{2}\b")


@dataclass(frozen=True)
class _Hit:
    display: str
    canonical: str
    status: str


@dataclass(frozen=True)
class _Scan:
    when: datetime
    description: str
    index: int


def parse_estimated_delivery(
    text: str,
    now: datetime,
    saved_at: datetime | None = None,
) -> str | None:
    """Eastern calendar day promised by the tracking page, as YYYY-MM-DD.

    "Today" and "Tomorrow" use ``now`` in America/New_York. A scan date
    (a date with a clock time, including one under Shipment Progress) is
    not the ETA. A saved page that says Today is ignored when ``saved_at``
    falls on an earlier Eastern day. A month and day with no year, such as
    "Wednesday, October 14" or "Oct 14", uses this year unless that day is
    already more than about 60 days past, in which case it rolls forward.
    Dec 31 checked on Jan 1 stays in the past year. A numeric date such as
    10/14/2026 is the ETA. A date-only last-scan line is not.
    Returns None when the page has no estimated-delivery line.
    """
    if now.tzinfo is None:
        return None
    normalized = _normalize(text)
    label = re.search(
        r"(?:estimated|expected|scheduled)\s+delivery(?:\s+date)?",
        normalized,
        re.IGNORECASE,
    )
    if label is None:
        return None
    window = normalized[label.end() :]
    lowered = window.lower()
    cut = len(window)
    for marker in PROGRESS_MARKERS:
        index = lowered.find(marker)
        if index != -1:
            cut = min(cut, index)
    window = "\n".join(window[:cut].splitlines()[:12])
    eastern = now.astimezone(ZoneInfo("America/New_York")).date()
    found: list[tuple[int, datetime]] = []
    for match in DATE_TIME.finditer(window):
        if match.group("time") or _is_scan_date(window, match.start(), match.end(), match.group("date")):
            continue
        day = _parse_date(match.group("date"))
        if day is not None:
            found.append((match.start(), day))
    for match in _ETA_NAMED.finditer(window):
        if _is_scan_date(window, match.start(), match.end(), match.group(0)):
            continue
        day = _named_eta_date(match, eastern)
        if day is not None:
            found.append((match.start(), day))
    if found:
        found.sort(key=lambda item: item[0])
        return found[0][1].date().isoformat()
    if _saved_page_is_stale(saved_at, eastern):
        return None
    folded = window.lower()
    if re.search(r"\btoday\b", folded):
        return eastern.isoformat()
    if re.search(r"\btomorrow\b", folded):
        return (eastern + timedelta(days=1)).isoformat()
    return None


def parse_tracking_page(
    carrier: str,
    text: str,
    tracking_number: str,
    now: datetime | None = None,
    saved_at: datetime | None = None,
) -> TrackingSnapshot:
    """Read one saved tracking page and return the current customer status."""
    name = normalize_carrier(carrier)
    number = tracking_number.strip()
    normalized = _normalize(text)
    eta = parse_estimated_delivery(normalized, now, saved_at=saved_at) if now is not None else None
    if not normalized.strip():
        return _unknown(name, number, "empty tracking page", eta)

    banner = _banner(_header_region(normalized))
    readings = _scan_readings(_extract_scans(normalized))
    latest = max(readings, key=_scan_rank) if readings else None
    delivered = [item for item in readings if item[1] == DELIVERED]
    delivered_scan = max(delivered, key=_scan_rank) if delivered else None

    seen = [status for _, status, _ in readings]
    banner_status = banner.status if banner else None
    if banner_status:
        seen.append(banner_status)
    latest_status = latest[1] if latest else None
    status = _combine(banner_status, latest_status, seen)

    event = None
    if status == DELIVERED and delivered_scan is not None:
        description = delivered_scan[2].display or "Delivered"
        canonical = delivered_scan[2].canonical
        event = delivered_scan[0].when
    elif status == DELIVERED and banner_status == DELIVERED and banner is not None:
        description = banner.display
        canonical = banner.canonical
        event = latest[0].when if latest else None
    elif banner is not None:
        description = banner.display
        canonical = banner.canonical
        event = latest[0].when if latest else None
    elif latest is not None and latest[2] is not None:
        description = latest[2].display
        canonical = latest[2].canonical
        event = latest[0].when
    elif latest is not None and latest[0].description:
        description = latest[0].description
        canonical = status
        event = latest[0].when
    else:
        return _unknown(name, number, "unrecognized tracking page", eta)

    if status == UNKNOWN:
        return _unknown(name, number, description or "unrecognized tracking page", eta)

    return TrackingSnapshot(
        carrier=name,
        tracking_number=number,
        status=status,
        raw_code=canonical,
        description=description,
        event_time=_iso(event) if event else None,
        source="page",
        estimated_delivery=eta,
    )


def _unknown(carrier: str, number: str, description: str, estimated_delivery: str | None = None) -> TrackingSnapshot:
    return TrackingSnapshot(
        carrier=carrier,
        tracking_number=number,
        status=UNKNOWN,
        description=description,
        source="page",
        estimated_delivery=estimated_delivery,
    )


def _combine(banner: str | None, latest: str | None, seen: list[str]) -> str:
    """Headline first, except a delivery scan and a vehicle scan win."""
    if banner == DELIVERED or DELIVERED in seen:
        return DELIVERED
    if banner == OUT_FOR_DELIVERY or latest == OUT_FOR_DELIVERY:
        return OUT_FOR_DELIVERY
    if banner == DELAYED or latest == DELAYED:
        return DELAYED
    if banner:
        return banner
    if latest and latest != UNKNOWN:
        return latest
    for status in seen:
        if status != UNKNOWN:
            return status
    return UNKNOWN


def _banner(header: str) -> _Hit | None:
    lines = header.splitlines()
    legend = _legend_indexes(lines)
    outside: list[_Hit] = []
    all_hits: list[_Hit] = []
    for index, line in enumerate(lines):
        hit = _phrase_status(line)
        if hit is None:
            continue
        all_hits.append(hit)
        if index not in legend:
            outside.append(hit)
    if outside:
        return outside[0]
    if all_hits:
        return all_hits[0]
    return None


def _legend_indexes(lines: list[str]) -> set[int]:
    """Indexes of a progress-bar cluster, not the current status heading."""
    milestone_at = [
        index
        for index, line in enumerate(lines)
        if line.strip().lower().rstrip(".") in MILESTONE_LABELS
    ]
    if len(milestone_at) < 3:
        return set()
    clusters: list[list[int]] = [[milestone_at[0]]]
    for previous, nxt in zip(milestone_at, milestone_at[1:]):
        between = lines[previous + 1 : nxt]
        if all(not item.strip() for item in between):
            clusters[-1].append(nxt)
        else:
            clusters.append([nxt])
    drop: set[int] = set()
    for cluster in clusters:
        labels = {lines[index].strip().lower().rstrip(".") for index in cluster}
        if len(cluster) >= 3 and len(labels) >= 3:
            drop.update(cluster)
    return drop


def _header_region(text: str) -> str:
    lowered = text.lower()
    cuts = [lowered.find(marker) for marker in PROGRESS_MARKERS if lowered.find(marker) != -1]
    for match in DATE_TIME.finditer(text):
        if match.group("time"):
            cuts.append(match.start())
            break
    if not cuts:
        return text
    return text[: min(cuts)]


def _progress_region(text: str) -> str:
    lowered = text.lower()
    indexes = [lowered.find(marker) for marker in PROGRESS_MARKERS if lowered.find(marker) != -1]
    if not indexes:
        return text
    return text[min(indexes) :]


def _extract_scans(text: str) -> list[_Scan]:
    region = _progress_region(text)
    matches = [match for match in DATE_TIME.finditer(region) if match.group("time")]
    scans: list[_Scan] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(region)
        description = _scan_description(region[match.end() : end])
        when = _parse_when(match.group("date"), match.group("time"))
        if when is None:
            continue
        scans.append(_Scan(when=when, description=description, index=match.start()))
    return scans


def _scan_readings(scans: list[_Scan]) -> list[tuple[_Scan, str, _Hit | None]]:
    readings = []
    for scan in scans:
        hit = _phrase_status(scan.description) if scan.description else None
        status = hit.status if hit else UNKNOWN
        readings.append((scan, status, hit))
    return readings


def _scan_rank(item: tuple[_Scan, str, _Hit | None]) -> tuple[datetime, int]:
    scan = item[0]
    # Equal timestamps: the earlier line on the page is the newer scan.
    return (scan.when, -scan.index)


def _scan_description(body: str) -> str:
    for line in body.splitlines():
        cleaned = line.strip()
        if not cleaned or _looks_like_location(cleaned):
            continue
        return cleaned
    return ""


def _looks_like_location(line: str) -> bool:
    folded = line.strip().lower().rstrip(".")
    if folded in {"united states", "usa", "us"}:
        return True
    if "," in line and _STATE_COMMA.search(line):
        return True
    if _STATE_ZIP.search(line):
        return True
    return False


def _phrase_status(line: str) -> _Hit | None:
    raw = line.strip().rstrip(".")
    if not raw:
        return None
    folded = raw.lower()
    prefix: _Hit | None = None
    for phrase, status in STATUS_PHRASES:
        if folded == phrase:
            return _Hit(raw, phrase, status)
        if phrase in LINE_ONLY:
            continue
        if _starts_phrase(folded, phrase) and (prefix is None or len(phrase) > len(prefix.canonical)):
            prefix = _Hit(raw[: len(phrase)], phrase, status)
    if prefix is not None:
        return prefix
    embedded = _earliest_phrase(raw)
    if embedded is not None:
        return embedded
    status = classify_text("", raw)
    if status == UNKNOWN:
        return None
    return _Hit(raw, status, status)


def _starts_phrase(folded: str, phrase: str) -> bool:
    return any(folded.startswith(phrase + separator) for separator in (" ", ",", ":"))


def _earliest_phrase(text: str) -> _Hit | None:
    lowered = text.lower()
    best: tuple[tuple[int, int], _Hit] | None = None
    for phrase, status in STATUS_PHRASES:
        if phrase in LINE_ONLY:
            continue
        start = 0
        while True:
            index = lowered.find(phrase, start)
            if index < 0:
                break
            end = index + len(phrase)
            if _bounded(lowered, index, end):
                rank = (index, -len(phrase))
                hit = _Hit(text[index:end], phrase, status)
                if best is None or rank < best[0]:
                    best = (rank, hit)
            start = index + 1
    if best is None:
        return None
    return best[1]


def _bounded(text: str, start: int, end: int) -> bool:
    if start > 0 and text[start - 1].isalnum():
        return False
    if end < len(text) and text[end].isalnum():
        return False
    return True


def _parse_when(date_text: str, time_text: str | None) -> datetime | None:
    day = _parse_date(date_text)
    if day is None:
        return None
    if not time_text:
        return day
    clock = _parse_clock(time_text)
    if clock is None:
        return day
    hour, minute = clock
    return day.replace(hour=hour, minute=minute)


def _named_eta_date(match: re.Match[str], today) -> datetime | None:
    month = MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    day = int(match.group("day"))
    year_text = match.group("year")
    if year_text:
        return _safe_date(int(year_text), month, day)
    return _infer_yearless_date(month, day, today)


def _content_lines(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        cleaned = line.strip().strip(":").strip("-").strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def _previous_lines(window: str, start: int) -> list[str]:
    """Completed lines before this date. A weekday on the same line stays the ETA."""
    head = window[:start]
    newline = head.rfind("\n")
    if newline == -1:
        return []
    return _content_lines(head[:newline])


def _is_scan_date(window: str, start: int, end: int, date_text: str) -> bool:
    """True when this date is a tracking scan, not the estimated-delivery day.

    A clock glued to the date is a scan. So is a status phrase or a location
    on the neighboring line. A numeric ETA such as 10/14/2026 is kept. A
    date-only line that trails other text in the delivery window is a scan.
    """
    if _CLOCK_AFTER.match(window[end : end + 64]):
        return True
    after = _content_lines(window[end:])
    if after and re.match(r"by\b", after[0], re.IGNORECASE):
        return False
    before = _previous_lines(window, start)
    if before and _looks_like_location(before[-1]):
        return True
    if after and _phrase_status(after[0]) is not None:
        return True
    if "/" in date_text and before and not after:
        return True
    return False


def _infer_yearless_date(month: int, day: int, today) -> datetime | None:
    """This year, the previous year when that day was at most ~60 days ago, or next year.

    Dec 31 checked on Jan 1 is one day late, so it stays Dec 31 of the past year.
    A month-day more than about 60 days ago rolls forward.
    """
    this_year = _safe_date(today.year, month, day)
    if this_year is None:
        return _safe_date(today.year + 1, month, day)
    late = (today - this_year.date()).days
    if late > YEARLESS_PAST_DAYS:
        return _safe_date(today.year + 1, month, day) or this_year
    if late < 0:
        previous = _safe_date(today.year - 1, month, day)
        if previous is not None and 0 <= (today - previous.date()).days <= YEARLESS_PAST_DAYS:
            return previous
    return this_year


def _saved_page_is_stale(saved_at: datetime | None, today) -> bool:
    """True when the saved page file was last written on an earlier Eastern day."""
    if saved_at is None:
        return False
    moment = saved_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("America/New_York"))
    return moment.astimezone(ZoneInfo("America/New_York")).date() < today


def _parse_date(date_text: str) -> datetime | None:
    cleaned = re.sub(r"\s+", " ", date_text.strip()).replace(".", "")
    numeric = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", cleaned)
    if numeric:
        month, day, year = (int(part) for part in numeric.groups())
        return _safe_date(year, month, day)
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", cleaned)
    if iso:
        year, month, day = (int(part) for part in iso.groups())
        return _safe_date(year, month, day)
    named = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", cleaned)
    if named:
        month = MONTHS.get(named.group(1).lower())
        if month is None:
            return None
        return _safe_date(int(named.group(3)), month, int(named.group(2)))
    return None


def _safe_date(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _parse_clock(time_text: str) -> tuple[int, int] | None:
    cleaned = re.sub(r"[\s.]", "", time_text).upper()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})([AP]M)", cleaned)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    if match.group(3) == "PM" and hour != 12:
        hour += 12
    if match.group(3) == "AM" and hour == 12:
        hour = 0
    return hour, minute


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _normalize(text: str) -> str:
    return text.replace("\u00a0", " ").replace("\u202f", " ")
