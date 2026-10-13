"""Hand a status change to the existing send-shipment-updates.py script.

This module does not compose customer email and does not open an SMTP
session. The sender lives at ~/workspace/customer-comms/send-shipment-updates.py
and already knows the wording. We only decide whether it is allowed to run.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from shipment_monitor.links import email_links
from shipment_monitor.models import Shipment

DEFAULT_SENDER = Path.home() / "workspace" / "customer-comms" / "send-shipment-updates.py"


@dataclass(frozen=True)
class TriggerResult:
    ok: bool
    reason: str
    argv: tuple[str, ...]


def build_argv(
    script: Path,
    shipment: Shipment,
    event: str,
    description: str,
    event_time: str,
    python: str = sys.executable,
) -> list[str]:
    argv = [
        python,
        str(script),
        "--event",
        event,
        "--shipment-id",
        shipment.shipment_id,
        "--order-id",
        shipment.order_id,
        "--customer-name",
        shipment.customer_name,
        "--customer-email",
        shipment.customer_email,
        "--company",
        shipment.company,
        "--line",
        shipment.line,
        "--device",
        shipment.device,
        "--carrier",
        shipment.carrier,
        "--tracking-number",
        shipment.tracking_number,
        "--status-description",
        description,
        "--event-time",
        event_time,
    ]
    tracking_url, tracking_html, order_url, order_html = email_links(shipment)
    argv.extend(["--tracking-url", tracking_url, "--tracking-link", tracking_html])
    if order_url and order_html:
        argv.extend(["--order-status-url", order_url, "--order-status-link", order_html])
    return argv


class EmailTrigger:
    def __init__(self, script: Path = DEFAULT_SENDER, mode: str = "dry-run", runner=None) -> None:
        if mode not in {"dry-run", "execute"}:
            raise ValueError("email mode must be dry-run or execute")
        self.script = script
        self.mode = mode
        self.runner = runner or _subprocess_runner
        self.planned: list[list[str]] = []

    def send(self, shipment: Shipment, event: str, description: str, event_time: str) -> TriggerResult:
        argv = build_argv(self.script, shipment, event, description, event_time)
        if self.mode != "execute":
            self.planned.append(argv)
            return TriggerResult(True, "dry_run", tuple(argv))
        if not self.script.is_file():
            return TriggerResult(False, "sender_missing", tuple(argv))
        code = self.runner(argv)
        if code != 0:
            return TriggerResult(False, f"sender_exit_{code}", tuple(argv))
        self.planned.append(argv)
        return TriggerResult(True, "sent", tuple(argv))


def _subprocess_runner(argv: list[str]) -> int:
    completed = subprocess.run(argv, check=False, capture_output=True, text=True)
    return completed.returncode
