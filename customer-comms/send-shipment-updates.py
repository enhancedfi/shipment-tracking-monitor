#!/usr/bin/env python3
"""Shipment update emails.

The monitor stages one handoff at ``SHIPMENT_EMAIL_STATE``
(``data/shipments.json.email.json``) and starts this script with no
arguments it reads. Customer contacts are not stored in this file. Point
``SHIPMENT_CONTACTS`` at a mode-0600 JSON file outside git.

Resend is imported from the host directory in ``SHIPMENT_RESEND_BIN``
(default ``/home/hatch/workspace/skills/resend/bin``) with
``from resend import _request``. This script does not vendor that module
or any credential helper.
"""

import html
import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone

FROM = "Ava <ava@enhancedfi.pro>"
REPLY_TO = "dom@enhancedfi.com"
IMMEDIATE_TYPES = {"shipped", "final-delivered"}
THROTTLE_WINDOW = timedelta(hours=6)
STATUS_QUO_INTERVAL = timedelta(hours=24)

SIGNATURE = "\n".join([
    "",
    "Ava",
    "Digital Assistant",
    "www.enhancedfi.com",
])

LOGO_URL = (
    "https://enhancedfi.com/wp-content/uploads/2025/05/"
    "enhancedFi_Business_Solutions_transparent-300x71.png"
)
SITE_URL = "https://www.enhancedfi.com"
SIGNATURE_HTML = (
    "<br><br><br>"
    '<div style="font-size:13px;color:#444;">'
    "Ava<br>"
    "Digital Assistant"
    "</div>"
    '<div style="margin-top:8px;width:150px;">'
    f'<a href="{SITE_URL}"><img src="{LOGO_URL}" width="150" '
    'alt="EnhancedFI Business Solutions" style="display:block;border:0;"></a>'
    '<div style="margin-top:8px;text-align:center;">'
    f'<a href="{SITE_URL}" style="font-size:13px;color:#1a73e8;">www.enhancedfi.com</a>'
    "</div>"
    "</div>"
)

_MONTHS = {name: index for index, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_EXIT = {
    "sent": 0,
    "skipped": 0,
    "waiting": 4,
    "throttled": 3,
    "failed": 1,
    "missing": 2,
}


def comms_dir():
    override = os.environ.get("SHIPMENT_COMMS_DIR", "").strip()
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "data", "customer-comms")


def _ledger(name):
    return os.path.join(comms_dir(), name)


def text_to_html(text):
    return html.escape(text).replace("\n", "<br>")


def resend_request(method, path, payload=None):
    """Import ``_request`` from the configured host directory."""
    bin_dir = os.environ.get(
        "SHIPMENT_RESEND_BIN",
        "/home/hatch/workspace/skills/resend/bin",
    )
    if bin_dir and bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    from resend import _request
    if payload is None:
        return _request(method, path)
    return _request(method, path, payload)


def track_url(carrier, tracking):
    """Legacy watch-state link. Staged handoffs pass an explicit URL instead."""
    carrier = (carrier or "").lower()
    if "fedex" in carrier:
        return f"https://www.fedex.com/fedextrack/?trknbr={tracking}"
    if "ups" in carrier:
        return f"https://www.ups.com/track?tracknum={tracking}"
    return ""


def line_tracking_url(line):
    """Use a staged URL when the handoff has the key. Never fill a blank one in."""
    if "tracking_url" in line:
        return (line.get("tracking_url") or "").strip()
    return track_url(line.get("carrier"), line.get("tracking"))


def explicit_link_text(lines):
    rows = []
    seen = set()
    for line in lines:
        url = (line.get("tracking_url") or "").strip()
        if url and url not in seen:
            seen.add(url)
            rows.append(f"Track it here: {url}")
    order_url = ""
    for line in lines:
        candidate = (line.get("order_status_url") or "").strip()
        if candidate:
            order_url = candidate
            break
    if order_url:
        rows.append(f"AT&T order status: {order_url}")
    return "\n".join(rows)


def link_html(lines):
    parts = []
    seen = set()
    for line in lines:
        url = (line.get("tracking_url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        anchor = (line.get("tracking_link") or "").strip()
        if not anchor:
            anchor = f'<a href="{html.escape(url, quote=True)}">Track shipment</a>'
        parts.append(f"<p>{anchor}</p>")
    order_url = ""
    order_anchor = ""
    for line in lines:
        candidate = (line.get("order_status_url") or "").strip()
        if candidate:
            order_url = candidate
            order_anchor = (line.get("order_status_link") or "").strip()
            break
    if order_url:
        if not order_anchor:
            order_anchor = (
                f'<a href="{html.escape(order_url, quote=True)}">AT&T order status</a>'
            )
        parts.append(f"<p>{order_anchor}</p>")
    return "".join(parts)


def extract_eta(line):
    for key in ("eta", "expected_delivery", "estimated_delivery",
                "expected_ship", "delivery_estimate"):
        if line.get(key):
            return str(line[key])
    note = str(line.get("note", "") or "")
    match = re.search(r"[Ee]stimat\w*\s+deliver[^.]{0,80}", note)
    if match:
        return match.group(0).strip()
    return ""


def build_shipped_batch(contact, order_id, lines):
    """One consolidated on-its-way email for this order's new ship batch."""
    first = contact["first_name"]
    if len(lines) == 1:
        device = lines[0].get("device", "your phone")
        subject = f"Your {device} is on its way!"
        intro = f"Good news - your {device} just shipped and is headed your way."
    else:
        subject = "Your phones are on their way!"
        intro = (
            f"Good news - {len(lines)} phones from your AT&T order for "
            f"{contact['account']} just shipped and are headed your way."
        )
    groups = {}
    for line in lines:
        key = (line.get("carrier") or "", line.get("tracking") or "")
        groups.setdefault(key, []).append(line)
    parts = [f"Hi {first},", "", intro, f"Order: {order_id}", ""]
    for (carrier, tracking), group in groups.items():
        if len(groups) > 1:
            parts.append("Phones in this shipment:")
        for line in group:
            parts.append(
                f"- {line.get('device', 'phone')} (line ...{line.get('line_ending')})"
            )
        if carrier:
            parts.append(f"Carrier: {carrier}")
        if tracking:
            parts.append(f"Tracking: {tracking}")
        url = ""
        for line in group:
            url = line_tracking_url(line)
            if url:
                break
        if url:
            parts.append(f"Track it here: {url}")
        etas = [extract_eta(line) for line in group]
        etas = [item for item in etas if item]
        if etas:
            parts.append(f"Expected delivery: {etas[0]}")
        parts.append("")
    order_url = ""
    for line in lines:
        candidate = (line.get("order_status_url") or "").strip()
        if candidate:
            order_url = candidate
            break
    if order_url:
        parts.append(f"AT&T order status: {order_url}")
        parts.append("")
    parts.append(
        "I'll keep an eye on them and let you know the moment they land. "
        "Just reply to this email if you need anything."
    )
    return subject, "\n".join(parts)


def build_order_delivered(contact, order_id, lines):
    """One consolidated final email once every line on the order has arrived."""
    items = [
        f"- {line.get('device', 'phone')} (line ...{line.get('line_ending')})"
        for line in lines
    ]
    body = "\n".join([
        f"Hi {contact['first_name']},",
        "",
        f"Everything from your AT&T order {order_id} for {contact['account']} "
        "has arrived:",
        "",
        *items,
        "",
        "You are all set. Reply here if anything looks off with any of the phones.",
    ])
    extra = explicit_link_text(lines)
    if extra:
        body = body + "\n\n" + extra
    return "All your phones have arrived!", body


def build_progress(contact, line, event, description):
    device = line.get("device", "your phone")
    ending = line.get("line_ending") or ""
    if event == "out_for_delivery":
        subject = f"Your {device} is out for delivery"
        detail = f"Your {device} (line ...{ending}) is out for delivery."
    else:
        subject = f"Update on your {device}"
        detail = description or "The carrier reported a delay."
        detail = f"An update on your {device} (line ...{ending}): {detail}"
    body = "\n".join([
        f"Hi {contact['first_name']},",
        "",
        detail,
        "",
        "Just reply to this email if you need anything.",
    ])
    extra = explicit_link_text([line])
    if extra:
        body = body + "\n\n" + extra
    return subject, body


def expected_ship_label(line, windows, order_id):
    if line.get("expected_ship"):
        return str(line["expected_ship"])
    return (windows.get(order_id) or {}).get("label", "")


def parse_window_start(label, today):
    match = re.search(r"([A-Za-z]+)\s+(\d{1,2})\s*[-\u2013]", label or "")
    if not match:
        return None
    month = _MONTHS.get(match.group(1)[:3].lower())
    if not month:
        return None
    try:
        return datetime(today.year, month, int(match.group(2))).date()
    except ValueError:
        return None


def is_delivered(line):
    return (line.get("status") or "").lower() == "delivered" or bool(line.get("delivered"))


def domain_verified():
    status, body = resend_request("GET", "/domains")
    if status != 200:
        return False
    for domain in (body.get("data") or []):
        if domain.get("name") == "enhancedfi.pro":
            return domain.get("status") == "verified"
    return False


def send_email(to, subject, text, html_extra=""):
    full_text = text.rstrip("\n") + "\n" + SIGNATURE
    full_html = text_to_html(text.rstrip("\n")) + html_extra + SIGNATURE_HTML
    payload = {
        "from": FROM,
        "to": [to],
        "cc": [REPLY_TO],
        "reply_to": REPLY_TO,
        "subject": subject,
        "text": full_text,
        "html": full_html,
    }
    status, body = resend_request("POST", "/emails", payload)
    if status not in (200, 202):
        raise RuntimeError(f"Resend rejected send: {status} {body}")
    return body.get("id"), full_text, full_html


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _write_private(path, text):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        payload = text.encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(f"could not write {temporary}")
            view = view[written:]
    except Exception:
        os.close(descriptor)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    os.close(descriptor)
    os.replace(temporary, path)


def _append_private(path, text):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, text.encode("utf-8"))
    finally:
        os.close(descriptor)


def load_contact_book():
    """Load contacts from ``SHIPMENT_CONTACTS``. Refuse a loose or missing file."""
    path = os.environ.get("SHIPMENT_CONTACTS", "").strip()
    if not path:
        print("DOMINIC-NOTIFY: SHIPMENT_CONTACTS is not set; no customer emails can be sent.")
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        print(
            f"DOMINIC-NOTIFY: contacts file {path} cannot be read ({exc.strerror}); "
            "no customer emails can be sent."
        )
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o077):
            print(
                f"DOMINIC-NOTIFY: contacts file {path} must be mode 0600; "
                "refusing to read it."
            )
            return None
        chunks = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except Exception as exc:
        print(
            f"DOMINIC-NOTIFY: contacts file {path} is unreadable ({exc}); "
            "no customer emails can be sent."
        )
        return None
    if isinstance(data, dict) and isinstance(data.get("contacts"), dict):
        raw_contacts = data["contacts"]
        skip = {str(item) for item in (data.get("skip_status_quo") or [])}
    elif isinstance(data, dict):
        raw_contacts = data
        skip = set()
    else:
        raw_contacts = {}
        skip = set()
    contacts = {}
    for order_id, contact in raw_contacts.items():
        if not isinstance(contact, dict) or not str(contact.get("email") or "").strip():
            continue
        contacts[str(order_id)] = {
            "email": str(contact["email"]),
            "first_name": str(contact.get("first_name") or ""),
            "account": str(contact.get("account") or ""),
        }
    return contacts, skip


def throttle_ok(throttle_ledger, recipient, email_type):
    """Non-immediate mail waits 6 hours. Shipped and final-delivered do not."""
    if email_type in IMMEDIATE_TYPES:
        return True
    stamps = throttle_ledger.get(recipient, [])
    cutoff = datetime.now(timezone.utc) - THROTTLE_WINDOW
    recent = []
    for stamp in stamps:
        try:
            if datetime.fromisoformat(stamp) >= cutoff:
                recent.append(stamp)
        except Exception:
            continue
    throttle_ledger[recipient] = recent
    return not recent


def record_throttle(throttle_ledger, recipient, email_type):
    if email_type in IMMEDIATE_TYPES:
        return
    throttle_ledger.setdefault(recipient, []).append(datetime.now(timezone.utc).isoformat())


def log_send(contact, line, event, subject, msg_id, notify=True):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "to": contact["email"],
        "event": event,
        "device": line.get("device"),
        "line_ending": line.get("line_ending"),
        "subject": subject,
        "resend_id": msg_id,
    }
    try:
        _append_private(_ledger("sent-log.jsonl"), json.dumps(entry) + "\n")
    except Exception as exc:
        print(f"WARN: could not write sent log: {exc}")
    tracking = line.get("tracking") or ""
    if notify:
        print(
            f"DOMINIC-NOTIFY: {event} email sent to {contact['account']} "
            f"({contact['email']}), {line.get('device')} line ...{line.get('line_ending')}"
            + (f", tracking {tracking}" if tracking else "")
            + f" | subject: {subject}"
        )


def maybe_send_shipped_batch(contact, order_id, lines, ledger, throttle_ledger, now):
    new_lines = [
        line for line in lines
        if f"{order_id}:{line.get('line_ending')}:shipped" not in ledger
        and not is_delivered(line)
    ]
    if not new_lines:
        return "skipped"
    if not throttle_ok(throttle_ledger, contact["email"], "shipped"):
        return "throttled"
    subject, body = build_shipped_batch(contact, order_id, new_lines)
    try:
        msg_id, _text, _html = send_email(
            contact["email"], subject, body, link_html(new_lines)
        )
    except Exception as exc:
        print(f"FAILED shipped batch email for {contact['account']}: {exc}")
        return "failed"
    for line in new_lines:
        ledger[f"{order_id}:{line.get('line_ending')}:shipped"] = now
        log_send(contact, line, "shipped", subject, msg_id, notify=False)
    devices = ", ".join(
        f"{line.get('device')} ...{line.get('line_ending')}" for line in new_lines
    )
    trackings = sorted({line.get("tracking") for line in new_lines if line.get("tracking")})
    print(
        f"DOMINIC-NOTIFY: shipped email sent to {contact['account']} "
        f"({contact['email']}), {len(new_lines)} phone(s): {devices}"
        + (f", tracking {', '.join(trackings)}" if trackings else "")
        + f" | subject: {subject}"
    )
    print(f"Sent shipped update to {contact['email']} ({len(new_lines)} lines, 1 email)")
    return "sent"


def build_status_quo(contact, pending, window_label):
    lines = []
    for line in pending:
        status = (line.get("status") or "").lower()
        device = line.get("device", "phone")
        if status == "shipped" and line.get("tracking"):
            url = line_tracking_url(line)
            lines.append(
                f"- {device} (line ...{line.get('line_ending')}): on the way, "
                f"tracking {line.get('tracking')}"
                + (f" ({url})" if url else "")
            )
        else:
            eta = (
                f"still expected to ship {window_label}" if window_label
                else "we are watching it daily and will email the moment it ships"
            )
            lines.append(f"- {device} (line ...{line.get('line_ending')}): {eta}")
    body = "\n".join([
        f"Hi {contact['first_name']},",
        "",
        f"Quick update on your AT&T order for {contact['account']}: no change "
        "since our last update, everything is still on track.",
        "",
        *lines,
        "",
        "We check your order status multiple times a day and will email you "
        "the moment anything moves.",
    ])
    return f"No change: your {contact['account']} order is on track", body


def recent_order_activity(contact_email, since):
    try:
        with open(_ledger("sent-log.jsonl"), "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get("to") != contact_email:
                    continue
                try:
                    if datetime.fromisoformat(entry["ts"]) >= since:
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def maybe_send_status_quo(contact, order_id, pending, windows, quo_ledger, now):
    cutoff = datetime.now(timezone.utc) - STATUS_QUO_INTERVAL
    last = quo_ledger.get(order_id)
    if last:
        try:
            if datetime.fromisoformat(last) >= cutoff:
                return "skipped"
        except Exception:
            pass
    if recent_order_activity(contact["email"], cutoff):
        return "skipped"
    window_label = (windows.get(order_id) or {}).get("label", "")
    subject, body = build_status_quo(contact, pending, window_label)
    try:
        msg_id, _text, _html = send_email(contact["email"], subject, body, link_html(pending))
    except Exception as exc:
        print(f"FAILED status-quo email for {order_id}: {exc}")
        return "failed"
    quo_ledger[order_id] = now
    log_send(
        contact,
        {"device": f"{len(pending)} pending line(s)", "line_ending": ""},
        "status-quo",
        subject,
        msg_id,
    )
    print(
        f"Sent status-quo update to {contact['email']} "
        f"({contact['account']}, {len(pending)} pending)"
    )
    return "sent"


def send_final_delivered(contact, order_id, lines, ledger, now):
    subject, body = build_order_delivered(contact, order_id, lines)
    try:
        msg_id, _text, _html = send_email(
            contact["email"], subject, body, link_html(lines)
        )
    except Exception as exc:
        print(f"FAILED final-delivered email for {contact['account']}: {exc}")
        return "failed"
    ledger[f"{order_id}:final-delivered"] = now
    log_send(
        contact,
        {"device": f"{len(lines)} line(s), all delivered", "line_ending": ""},
        "final-delivered",
        subject,
        msg_id,
    )
    print(
        f"Sent final delivered email to {contact['email']} "
        f"({contact['account']}, {len(lines)} lines)"
    )
    return "sent"


def send_progress(contact, order_id, line, event, description, ledger, throttle_ledger, now):
    key = f"{order_id}:{line.get('line_ending')}:{event}"
    if key in ledger:
        return "skipped"
    if not throttle_ok(throttle_ledger, contact["email"], event):
        return "throttled"
    subject, body = build_progress(contact, line, event, description)
    try:
        msg_id, _text, _html = send_email(
            contact["email"], subject, body, link_html([line])
        )
    except Exception as exc:
        print(f"FAILED {event} email for {contact['account']}: {exc}")
        return "failed"
    ledger[key] = now
    record_throttle(throttle_ledger, contact["email"], event)
    log_send(contact, line, event, subject, msg_id)
    return "sent"


def _ending(value):
    text = str(value or "")
    return text[-4:] if len(text) >= 4 else text


def order_from_staged(state):
    """Turn the monitor handoff into the order shape these rules already use."""
    raw_lines = state.get("order_lines") or [state]
    lines = []
    for raw in raw_lines:
        status = (raw.get("status") or "").lower()
        line = {
            "device": raw.get("device") or state.get("device") or "phone",
            "line_ending": raw.get("line_ending") or _ending(raw.get("line") or state.get("line")),
            "carrier": raw.get("carrier") or "",
            "tracking": raw.get("tracking") or raw.get("tracking_number") or "",
            "status": status,
            "delivered": bool(raw.get("delivered") or status == "delivered"),
            "order_closed": bool(raw.get("order_closed") or state.get("order_closed")),
            "estimated_delivery": raw.get("estimated_delivery") or "",
            "note": raw.get("status_description") or "",
            "tracking_url": (raw.get("tracking_url") or "").strip(),
        }
        if raw.get("tracking_link"):
            line["tracking_link"] = raw.get("tracking_link")
        if raw.get("order_status_url"):
            line["order_status_url"] = raw.get("order_status_url")
        if raw.get("order_status_link"):
            line["order_status_link"] = raw.get("order_status_link")
        if raw.get("shipment_id") and raw.get("shipment_id") == state.get("shipment_id"):
            line["current"] = True
        lines.append(line)
    if lines and not any(line.get("current") for line in lines):
        lines[0]["current"] = True
    staged_tracking = (state.get("tracking_number") or "").strip()
    staged_url = (state.get("tracking_url") or "").strip()
    if staged_url:
        for line in lines:
            if line["tracking"] == staged_tracking and not line["tracking_url"]:
                line["tracking_url"] = staged_url
                if state.get("tracking_link"):
                    line["tracking_link"] = state.get("tracking_link")
    if state.get("order_status_url") and not any(line.get("order_status_url") for line in lines):
        if lines:
            lines[0]["order_status_url"] = state["order_status_url"]
            if state.get("order_status_link"):
                lines[0]["order_status_link"] = state["order_status_link"]
    return {
        "order_id": state.get("order_id", ""),
        "label": state.get("company") or state.get("customer_name") or "?",
        "lines": lines,
        "event": (state.get("event") or "").lower(),
        "description": state.get("status_description") or "",
    }


def _shipped_lines(order_id, lines, ledger):
    delivered_before = {
        f"{order_id}:{line.get('line_ending')}:delivered"
        for line in lines
        if f"{order_id}:{line.get('line_ending')}:delivered" in ledger
    }
    chosen = []
    for line in lines:
        status = (line.get("status") or "").lower()
        key = f"{order_id}:{line.get('line_ending')}:delivered"
        if status in ("shipped", "delivered") and line.get("tracking") and not is_delivered(line):
            if key not in delivered_before:
                chosen.append(line)
    return chosen


def _close_delivered(contact, order_id, lines, ledger, now):
    """One final email when every line is delivered, then the order stays closed."""
    final_key = f"{order_id}:final-delivered"
    if final_key in ledger:
        for line in lines:
            ledger.setdefault(f"{order_id}:{line.get('line_ending')}:delivered", now)
        print(f"Skipping {contact['account']}: order fully delivered")
        return "skipped"
    new_transition = any(
        f"{order_id}:{line.get('line_ending')}:delivered" not in ledger
        for line in lines
    )
    if not new_transition:
        ledger[final_key] = "retired-before-consolidation"
        for line in lines:
            ledger.setdefault(f"{order_id}:{line.get('line_ending')}:delivered", now)
        print(f"Skipping {contact['account']}: order fully delivered")
        return "skipped"
    result = send_final_delivered(contact, order_id, lines, ledger, now)
    if result != "sent":
        return result
    for line in lines:
        ledger.setdefault(f"{order_id}:{line.get('line_ending')}:delivered", now)
    return "sent"


def process_staged(order, contact, ledger, throttle_ledger, now):
    order_id = order["order_id"]
    lines = order.get("lines") or []
    event = order.get("event") or ""
    if any(line.get("order_closed") for line in lines):
        print(f"Skipping {contact['account']}: order closed")
        return "skipped"
    if f"{order_id}:final-delivered" in ledger:
        print(f"Skipping {contact['account']}: final-delivered already sent, order closed")
        return "skipped"
    if event == "shipped":
        # A delivered line never gets an on-its-way email.
        shipped = _shipped_lines(order_id, lines, ledger)
        if not shipped:
            print(f"Skipping {contact['account']}: delivered line does not send shipped")
            return "skipped"
        return maybe_send_shipped_batch(
            contact, order_id, shipped, ledger, throttle_ledger, now
        )
    if lines and all(is_delivered(line) for line in lines):
        return _close_delivered(contact, order_id, lines, ledger, now)
    if event == "delivered":
        for line in lines:
            if is_delivered(line):
                ledger.setdefault(f"{order_id}:{line.get('line_ending')}:delivered", now)
        return "waiting"
    if event in {"delayed", "out_for_delivery"}:
        current = next((line for line in lines if line.get("current")), None)
        if current is None:
            current = next(
                (line for line in lines if not is_delivered(line)),
                lines[0] if lines else {"line_ending": "", "device": "phone"},
            )
        return send_progress(
            contact,
            order_id,
            current,
            event,
            order.get("description") or "",
            ledger,
            throttle_ledger,
            now,
        )
    return "skipped"


def process_watch_order(order, contact, skip_status_quo, ledger, throttle_ledger,
                        quo_ledger, about_ledger, windows, now, today):
    order_id = order.get("order_id", "")
    lines = order.get("lines", [])
    if lines and all(is_delivered(line) for line in lines):
        result = _close_delivered(contact, order_id, lines, ledger, now)
        return "sent" if result == "sent" else result
    if f"{order_id}:final-delivered" in ledger:
        print(f"Skipping {contact['account']}: final-delivered already sent, order closed")
        return "skipped"
    pending = []
    about_groups = {}
    delivered_before = {
        f"{order_id}:{line.get('line_ending')}:delivered"
        for line in lines
        if f"{order_id}:{line.get('line_ending')}:delivered" in ledger
    }
    for line in lines:
        ending = line.get("line_ending", "")
        if not is_delivered(line):
            pending.append(line)
        else:
            ledger.setdefault(f"{order_id}:{ending}:delivered", now)
    outcome = "skipped"
    shipped_lines = []
    for line in lines:
        status = (line.get("status") or "").lower()
        key = f"{order_id}:{line.get('line_ending')}:delivered"
        if status in ("shipped", "delivered") and line.get("tracking") and not is_delivered(line):
            if key not in delivered_before:
                shipped_lines.append(line)
    if shipped_lines:
        batch = maybe_send_shipped_batch(
            contact, order_id, shipped_lines, ledger, throttle_ledger, now
        )
        if batch == "failed":
            return "failed"
        if batch == "sent":
            outcome = "sent"
    for line in lines:
        ending = line.get("line_ending", "")
        status = (line.get("status") or "").lower()
        if status == "not_shipped":
            label = expected_ship_label(line, windows, order_id)
            if label:
                about_groups.setdefault(label, []).append(ending)
    for label, endings in about_groups.items():
        key = f"{order_id}:about-ship:{label}"
        if key not in about_ledger:
            suffix = ""
            start = parse_window_start(label, today)
            if start is not None and 0 <= (start - today).days <= 7:
                suffix = f" (window opens {start.strftime('%b %d')}, about a week out)"
            print(
                f"DOMINIC-NOTIFY: about-to-ship: {contact['account']}, "
                f"{len(endings)} line(s) ...{', ...'.join(endings)}, "
                f"AT&T expected ship {label}{suffix}"
            )
            about_ledger[key] = now
    if pending and order_id not in skip_status_quo:
        quo = maybe_send_status_quo(contact, order_id, pending, windows, quo_ledger, now)
        if quo == "failed":
            return "failed"
        if quo == "sent":
            outcome = "sent"
    return outcome


def _load_ledgers():
    return {
        "ledger": load_json(_ledger("notified-shipments.json"), {}),
        "throttle": load_json(_ledger("rate-limit.json"), {}),
        "quo": load_json(_ledger("status-quo.json"), {}),
        "about": load_json(_ledger("about-to-ship.json"), {}),
        "windows": load_json(_ledger("expected-windows.json"), {}),
    }


def _save_ledgers(ledgers):
    _write_private(_ledger("notified-shipments.json"), json.dumps(ledgers["ledger"], indent=2) + "\n")
    _write_private(_ledger("rate-limit.json"), json.dumps(ledgers["throttle"], indent=2) + "\n")
    _write_private(_ledger("status-quo.json"), json.dumps(ledgers["quo"], indent=2) + "\n")
    _write_private(_ledger("about-to-ship.json"), json.dumps(ledgers["about"], indent=2) + "\n")


def send_staged(path):
    state = load_json(path, None)
    if not isinstance(state, dict):
        print("DOMINIC-NOTIFY: SHIPMENT_EMAIL_STATE is missing or unreadable; no customer email was sent.")
        return 2
    book = load_contact_book()
    if book is None:
        return 2
    contacts, _skip = book
    if not domain_verified():
        print("enhancedfi.pro not verified in Resend yet; skipping sends.")
        return 1
    order = order_from_staged(state)
    contact = contacts.get(order["order_id"])
    if not contact:
        print(
            f"DOMINIC-NOTIFY: order {order['order_id']} ({order.get('label', '?')}) "
            "is in the watch state but has no CONTACTS entry; no customer emails can be sent for it."
        )
        return 2
    ledgers = _load_ledgers()
    now = datetime.now(timezone.utc).isoformat()
    try:
        result = process_staged(order, contact, ledgers["ledger"], ledgers["throttle"], now)
    finally:
        _save_ledgers(ledgers)
    return _EXIT.get(result, 1)


def send_watch(path):
    if not domain_verified():
        print("enhancedfi.pro not verified in Resend yet; skipping sends.")
        return 0
    book = load_contact_book()
    if book is None:
        return 2
    contacts, skip = book
    state = load_json(path, {})
    ledgers = _load_ledgers()
    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now().date()
    sent = 0
    try:
        for order in state.get("orders", []):
            order_id = order.get("order_id", "")
            contact = contacts.get(order_id)
            if not contact:
                if order.get("lines"):
                    print(
                        f"DOMINIC-NOTIFY: order {order_id} ({order.get('label', '?')}) "
                        "is in the watch state but has no CONTACTS entry; "
                        "no customer emails can be sent for it."
                    )
                continue
            result = process_watch_order(
                order,
                contact,
                skip,
                ledgers["ledger"],
                ledgers["throttle"],
                ledgers["quo"],
                ledgers["about"],
                ledgers["windows"],
                now,
                today,
            )
            if result == "sent":
                sent += 1
    finally:
        _save_ledgers(ledgers)
    print(f"Done: {sent} email(s) sent.")
    return 0


def main(argv=None):
    """Ignore argv. The handoff in ``SHIPMENT_EMAIL_STATE`` is the staged send."""
    del argv
    staged = os.environ.get("SHIPMENT_EMAIL_STATE", "").strip()
    if staged:
        return send_staged(staged)
    watch = os.environ.get("SHIPMENT_WATCH_STATE", "").strip()
    if watch:
        return send_watch(watch)
    print("No SHIPMENT_EMAIL_STATE or SHIPMENT_WATCH_STATE; nothing to send.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
