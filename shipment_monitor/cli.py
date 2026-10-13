"""Command line for a manual check, a webhook delivery, or a briefing.

Nothing here installs a schedule. The crons are shipment_monitor.schedule.
Email is dry-run unless --execute is set and SHIPMENT_ALLOW_EMAIL=1.
Even then, the only send path is the existing send-shipment-updates.py script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from shipment_monitor.briefing import shipment_summary
from shipment_monitor.carriers import FEDEX_PRODUCTION, UPS_PRODUCTION, USPS_PRODUCTION, FedExClient, UPSClient, USPSClient
from shipment_monitor.clock import Clock, FakeClock, SystemClock
from shipment_monitor.delivery_day import delivery_day_shipments
from shipment_monitor.email_trigger import DEFAULT_SENDER, EmailTrigger, email_state_path
from shipment_monitor.store import Store, StoreLockTimeout, exclusive_store_lock
from shipment_monitor.fixtures import load_fixture_clients
from shipment_monitor.orders import load_shipments_document
from shipment_monitor.models import Shipment, parse_time
from shipment_monitor.monitor import run_check
from shipment_monitor.monitor import apply_webhook
from shipment_monitor.page_source import web_page_clients
from shipment_monitor.parsing import snapshot_from_webhook
from shipment_monitor.rate_limit import RequestBudget, SpacingLimiter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Track phone shipments and queue customer status emails.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", default=os.environ.get("SHIPMENT_STORE", "data/shipments.json"))
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", parents=[common], help="Read carrier status for due shipments")
    _add_email_flags(check)
    check.add_argument("--fixtures", help="Directory of carrier JSON fixtures. Skips live APIs and page text.")
    check.add_argument("--pages", help="Directory of <tracking_number>.txt public tracking-page text")
    check.add_argument("--api", action="store_true", help="Use official carrier APIs. Requires credentials. Ignored when page text is configured.")
    check.add_argument("--delivery-day", action="store_true", help="Poll only shipments whose ETA is today or that are out for delivery")
    check.add_argument("--force", action="store_true", help="Poll even inside the cooldown window")
    check.add_argument("--cooldown-hours", type=float, default=float(os.environ.get("SHIPMENT_COOLDOWN_HOURS", "3")))
    check.add_argument("--now", help="ISO timestamp used instead of the wall clock")
    check.add_argument("--commit", action="store_true", help="Write status changes back to the store")

    run = sub.add_parser(
        "run",
        parents=[common],
        help="Import shipments, poll saved tracking pages, and hand changes to the sender",
    )
    _add_email_flags(run)
    run.add_argument("--input", help="JSON shipment list to import before polling")
    run.add_argument(
        "--pages",
        help="Directory of <tracking_number>.txt public tracking-page text. Default: data/page-text",
    )
    run.add_argument("--delivery-day", action="store_true", help="Poll only shipments whose ETA is today or that are out for delivery")
    run.add_argument("--force", action="store_true", help="Poll even inside the cooldown window")
    run.add_argument("--cooldown-hours", type=float, default=float(os.environ.get("SHIPMENT_COOLDOWN_HOURS", "3")))
    run.add_argument("--now", help="ISO timestamp used instead of the wall clock")

    briefing = sub.add_parser("briefing", parents=[common], help="Print the 6:00 AM, noon, or 4:00 PM summary")
    briefing.add_argument("--slot", choices=("auto", "morning", "noon", "afternoon"), default="auto")
    briefing.add_argument("--now", help="ISO timestamp used instead of the wall clock")

    webhook = sub.add_parser("webhook", parents=[common], help="Apply one carrier push payload from a file")
    _add_email_flags(webhook)
    webhook.add_argument("--input", required=True, help="JSON payload file")
    webhook.add_argument("--now", help="ISO timestamp used instead of the wall clock")
    webhook.add_argument("--commit", action="store_true")

    incoming = sub.add_parser("import-shipments", parents=[common], help="Insert or refresh shipment rows")
    incoming.add_argument("--input", required=True)

    args = parser.parse_args(argv)
    if _refuses_execute(args):
        return 2
    if args.command == "briefing":
        store = Store(Path(args.store))
        store.load()
        clock = _clock(args.now)
        slot = None if args.slot == "auto" else args.slot
        sys.stdout.write(shipment_summary(store.shipments, clock.now(), slot))
        return 0
    try:
        with exclusive_store_lock(Path(args.store)):
            return _main_locked(args)
    except StoreLockTimeout as exc:
        message = str(exc)
        if "ALERT" not in message:
            print(f"ALERT: {message}", file=sys.stderr)
        return 1


def _main_locked(args) -> int:
    store = Store(Path(args.store))
    store.load()

    if args.command == "import-shipments":
        store.upsert(_load_shipments(Path(args.input)))
        store.save()
        print(f"stored {len(store.shipments)} shipments")
        return 0

    clock = _clock(getattr(args, "now", None))
    email = _email_trigger(args)
    if isinstance(email, int):
        return email

    if args.command == "run" and args.input:
        store.upsert(_load_shipments(Path(args.input)))
        store.save()
        print(f"stored {len(store.shipments)} shipments")

    if args.command in {"check", "run"}:
        carriers = _carriers(args, clock)
        only_ids = None
        if getattr(args, "delivery_day", False):
            only_ids = {
                item.shipment_id
                for item in delivery_day_shipments(store.shipments, clock.now(), _pages_for_selection(args))
            }
        results = run_check(
            store,
            carriers,
            clock,
            email,
            cooldown=timedelta(hours=args.cooldown_hours),
            force=args.force,
            persist=args.command == "run" or getattr(args, "commit", False) or args.execute,
            only_ids=only_ids,
        )
        _print_results(results)
        return 0

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    snapshot = snapshot_from_webhook(payload)
    results = apply_webhook(store, snapshot, clock, email, persist=args.commit or args.execute)
    _print_results(results)
    return 0


def _add_email_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Invoke send-shipment-updates.py. Requires SHIPMENT_ALLOW_EMAIL=1. Default is dry-run.",
    )
    parser.add_argument(
        "--sender",
        default=os.environ.get("SHIPMENT_SENDER_SCRIPT", str(DEFAULT_SENDER)),
        help="Path to send-shipment-updates.py",
    )


def _refuses_execute(args) -> bool:
    if not getattr(args, "execute", False) or os.environ.get("SHIPMENT_ALLOW_EMAIL") == "1":
        return False
    print(
        "Refusing to email customers. Leave --execute off. "
        "SHIPMENT_ALLOW_EMAIL=1 is required before the existing sender can be invoked.",
        file=sys.stderr,
    )
    return True


def _email_trigger(args) -> EmailTrigger | int:
    if _refuses_execute(args):
        return 2
    return EmailTrigger(
        Path(args.sender),
        mode="execute" if args.execute else "dry-run",
        state_path=email_state_path(Path(args.store)),
    )


def _clock(now: str | None) -> Clock:
    if not now:
        return SystemClock()
    moment = parse_time(now)
    if moment is None:
        raise SystemExit("Could not parse --now")
    if moment.tzinfo is None:
        raise SystemExit("--now must include a timezone offset")
    return FakeClock(moment)


def _carriers(args, clock: Clock) -> dict:
    if getattr(args, "fixtures", None):
        return load_fixture_clients(Path(args.fixtures))
    page_dir = _page_directory(args)
    if page_dir is not None:
        return web_page_clients(page_dir, clock)
    if not _api_opt_in(args):
        return {}
    interval = float(os.environ.get("CARRIER_MIN_INTERVAL_SECONDS", "1"))
    budget_limit = int(os.environ.get("SHIPMENT_MAX_TRACK_REQUESTS", "60"))
    limiter = SpacingLimiter(interval, clock)
    clients = {}
    fedex_id = os.environ.get("FEDEX_CLIENT_ID", "")
    fedex_secret = os.environ.get("FEDEX_CLIENT_SECRET", "")
    if fedex_id and fedex_secret:
        clients["fedex"] = FedExClient(
            fedex_id,
            fedex_secret,
            os.environ.get("FEDEX_API_BASE", FEDEX_PRODUCTION),
            clock,
            limiter,
            RequestBudget(budget_limit),
        )
    ups_id = os.environ.get("UPS_CLIENT_ID", "")
    ups_secret = os.environ.get("UPS_CLIENT_SECRET", "")
    if ups_id and ups_secret:
        clients["ups"] = UPSClient(
            ups_id,
            ups_secret,
            os.environ.get("UPS_API_BASE", UPS_PRODUCTION),
            clock,
            limiter,
            RequestBudget(budget_limit),
        )
    usps_id = os.environ.get("USPS_CLIENT_ID", "")
    usps_secret = os.environ.get("USPS_CLIENT_SECRET", "")
    if usps_id and usps_secret:
        clients["usps"] = USPSClient(
            usps_id,
            usps_secret,
            os.environ.get("USPS_API_BASE", USPS_PRODUCTION),
            clock,
            limiter,
            RequestBudget(budget_limit),
        )
    return clients


def _page_directory(args) -> Path | None:
    """Where saved public tracking-page text lives, if this poll should use it.

    `run` always reads page text. `check` uses it when --pages or
    SHIPMENT_PAGE_TEXT_DIR is set, or when data/page-text already holds
    files. Official API clients are used only with --api or
    SHIPMENT_USE_API=1, and only when no page text is configured.
    """
    explicit = getattr(args, "pages", None)
    if explicit:
        return _require_page_dir(Path(explicit))
    if args.command == "run":
        return _require_page_dir(Path(os.environ.get("SHIPMENT_PAGE_TEXT_DIR", "data/page-text")))
    env = os.environ.get("SHIPMENT_PAGE_TEXT_DIR")
    if env:
        return _require_page_dir(Path(env))
    default = Path("data/page-text")
    if default.is_dir() and any(default.glob("*.txt")):
        return default
    return None


def _api_opt_in(args) -> bool:
    return bool(getattr(args, "api", False)) or os.environ.get("SHIPMENT_USE_API") == "1"


def _pages_for_selection(args) -> Path | None:
    explicit = getattr(args, "pages", None)
    if explicit:
        path = Path(explicit)
        return path if path.is_dir() else None
    if getattr(args, "fixtures", None):
        return None
    env = os.environ.get("SHIPMENT_PAGE_TEXT_DIR")
    if env:
        path = Path(env)
        return path if path.is_dir() else None
    default = Path("data/page-text")
    if default.is_dir():
        return default
    return None


def _require_page_dir(path: Path) -> Path:
    if not path.is_dir():
        raise SystemExit(
            f"Page text directory not found: {path}. "
            "Save each public tracking page as <tracking_number>.txt before polling."
        )
    return path


def _load_shipments(path: Path) -> list[Shipment]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return load_shipments_document(raw)


_CUSTOMER_ARGV = {"--customer-name", "--customer-email", "--company", "--line"}


def _mask_printed_argv(argv: list) -> list:
    """Mask leftover customer flags in printed results."""
    masked = []
    hide_value = False
    for item in argv:
        if hide_value:
            masked.append("***")
            hide_value = False
            continue
        if item in _CUSTOMER_ARGV:
            masked.append(item)
            hide_value = True
            continue
        masked.append(item)
    return masked


def _print_results(results) -> None:
    if not results:
        print("no shipment actions")
        return
    for result in results:
        payload = result.to_dict()
        payload["argv"] = _mask_printed_argv(list(payload.get("argv") or []))
        print(json.dumps(payload, sort_keys=True))
