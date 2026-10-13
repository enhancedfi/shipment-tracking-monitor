#!/usr/bin/env python3
"""Print public tracking-page URLs for active shipments.

The carrier sites need JavaScript, so this does not download the pages.
A scheduled browser step should open each URL and save the visible text to
<page-dir>/<tracking_number>.txt. `track_shipments.py run` reads those files.

Active means not delivered and not on a closed order.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from shipment_monitor.page_source import active_page_targets, tracking_page_url
from shipment_monitor.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print public tracking-page URLs for active shipments.")
    parser.add_argument("store_path", nargs="?", help="Path to shipments.json")
    parser.add_argument("--store", help="Path to shipments.json")
    args = parser.parse_args(argv)
    chosen = args.store or args.store_path or os.environ.get("SHIPMENT_STORE", "data/shipments.json")
    path = Path(chosen)
    if not path.is_file():
        print(f"shipment store not found: {path}", file=sys.stderr)
        return 1
    store = Store(path)
    store.load()
    for carrier, number in active_page_targets(store.shipments):
        print(tracking_page_url(carrier, number))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
