"""Command line for a manual check, a webhook delivery, or a briefing.

Nothing here installs a schedule. Email is dry-run unless --execute is set
and SHIPMENT_ALLOW_EMAIL=1. Even then, the only send path is the existing
send-shipment-updates.py script.
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
from shipment_monitor.email_trigger import DEFAULT_SENDER, EmailTrigger
from shipment_monitor.fixtures import load_fixture_clients
from shipment_monitor.models import Shipment, parse_time
from shipment_monitor.monitor import run_check
from shipment_monitor.monitor import apply_webhook
from shipment_monitor.parsing import snapshot_from_webhook
from shipment_monitor.rate_limit import RequestBudget, SpacingLimiter
from shipment_monitor.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Track phone shipments and queue customer status emails.")
    parser.add_argument("--store", default=os.environ.get("SHIPMENT_STORE", "data/shipments.json"))
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Read carrier status for due shipments")
    _add_email_flags(check)
    check.add_argument("--fixtures", help="Directory of carrier JSON fixtures. Skips live APIs.")
    check.add_argument("--force", action="store_true", help="Poll even inside the cooldown window")
    check.add_argument("--cooldown-hours", type=float, default=float(os.environ.get("SHIPMENT_COOLDOWN_HOURS", "3")))
    check.add_argument("--now", help="ISO timestamp used instead of the wall clock")
    check.add_argument("--commit", action="store_true", help="Write status changes back to the store")

    briefing = sub.add_parser("briefing", help="Print the 6:00 AM, noon, or 4:00 PM summary")
    briefing.add_argument("--slot", choices=("auto", "morning", "noon", "afternoon"), default="auto")
    briefing.add_argument("--now", help="ISO timestamp used instead of the wall clock")

    webhook = sub.add_parser("webhook", help="Apply one carrier push payload from a file")
    _add_email_flags(webhook)
    webhook.add_argument("--input", required=True, help="JSON payload file")
    webhook.add_argument("--now", help="ISO timestamp used instead of the wall clock")
    webhook.add_argument("--commit", action="store_true")

    incoming = sub.add_parser("import-shipments", help="Insert or refresh shipment rows")
    incoming.add_argument("--input", required=True)

    args = parser.parse_args(argv)
    store = Store(Path(args.store))
    store.load()

    if args.command == "import-shipments":
        store.upsert(_load_shipments(Path(args.input)))
        store.save()
        print(f"stored {len(store.shipments)} shipments")
        return 0

    clock = _clock(getattr(args, "now", None))
    if args.command == "briefing":
        slot = None if args.slot == "auto" else args.slot
        sys.stdout.write(shipment_summary(store.shipments, clock.now(), slot))
        return 0

    email = _email_trigger(args)
    if isinstance(email, int):
        return email

    if args.command == "check":
        carriers = _carriers(args, clock)
        results = run_check(
            store,
            carriers,
            clock,
            email,
            cooldown=timedelta(hours=args.cooldown_hours),
            force=args.force,
            persist=args.commit or args.execute,
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


def _email_trigger(args) -> EmailTrigger | int:
    if args.execute and os.environ.get("SHIPMENT_ALLOW_EMAIL") != "1":
        print(
            "Refusing to email customers. Leave --execute off. "
            "SHIPMENT_ALLOW_EMAIL=1 is required before the existing sender can be invoked.",
            file=sys.stderr,
        )
        return 2
    return EmailTrigger(Path(args.sender), mode="execute" if args.execute else "dry-run")


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
    if args.fixtures:
        return load_fixture_clients(Path(args.fixtures))
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


def _load_shipments(path: Path) -> list[Shipment]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw["shipments"] if isinstance(raw, dict) else raw
    return [Shipment.from_dict(item) for item in rows]


def _print_results(results) -> None:
    if not results:
        print("no shipment actions")
        return
    for result in results:
        print(json.dumps(result.to_dict(), sort_keys=True))
