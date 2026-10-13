#!/usr/bin/env python3
"""Cron entry point: list tracking pages, optionally fetch them, poll, and send.

The clock for this command is shipment_monitor.schedule. Cron decides when
to start it. This script does not apply its own time-of-day gate.

Customer email still follows the existing 6-hour throttle. Shipped and
delivered send immediately. `--execute` is used here because this is the
automatic cycle; it still requires SHIPMENT_ALLOW_EMAIL=1.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

from shipment_monitor.page_source import active_page_targets, tracking_page_url
from shipment_monitor.store import Store

ROOT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    os.chdir(ROOT)
    parser = argparse.ArgumentParser(description="Run the scheduled tracking-page cycle.")
    parser.add_argument("--store", default=os.environ.get("SHIPMENT_STORE", "data/shipments.json"))
    parser.add_argument("--input", default=os.environ.get("SHIPMENT_INCOMING", "data/incoming-shipments.json"))
    parser.add_argument("--pages", default=os.environ.get("SHIPMENT_PAGE_TEXT_DIR", "data/page-text"))
    args = parser.parse_args(argv)

    store_path = Path(args.store)
    incoming = Path(args.input)
    pages = Path(args.pages)
    pages.mkdir(parents=True, exist_ok=True)

    if incoming.is_file():
        imported = _python(
            "import-shipments",
            "--store",
            str(store_path),
            "--input",
            str(incoming),
        )
        if imported != 0:
            return imported

    url_text = _url_text(store_path)
    sys.stdout.write(url_text)

    fetch = os.environ.get("SHIPMENT_PAGE_FETCH", "").strip()
    if fetch:
        fetched = subprocess.run(shlex.split(fetch), input=url_text, text=True, check=False)
        if fetched.returncode != 0:
            print(f"page fetch exited {fetched.returncode}", file=sys.stderr)
            return fetched.returncode

    command = [
        "run",
        "--store",
        str(store_path),
        "--pages",
        str(pages),
        "--force",
        "--execute",
    ]
    sender = os.environ.get("SHIPMENT_SENDER_SCRIPT", "").strip()
    if sender:
        command.extend(["--sender", sender])
    return _python(*command)


def _url_text(store_path: Path) -> str:
    if not store_path.is_file():
        return ""
    store = Store(store_path)
    store.load()
    lines = [tracking_page_url(carrier, number) for carrier, number in active_page_targets(store.shipments)]
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _python(*args: str) -> int:
    completed = subprocess.run([sys.executable, str(ROOT / "track_shipments.py"), *args], check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
