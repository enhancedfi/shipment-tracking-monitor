"""Hand a status change to customer-comms/send-shipment-updates.py.

This module does not open an SMTP session. The sender takes no command-line
arguments, so the link flags on argv are ignored. Before the script is
started, the shipment, its order lines, and the rendered body are written
to a JSON file beside the store and ``SHIPMENT_EMAIL_STATE`` points at that
file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from shipment_monitor.email_body import render_email_body
from shipment_monitor.links import email_links
from shipment_monitor.models import DELIVERED, Shipment
from shipment_monitor.store import write_private_text

DEFAULT_SENDER = Path(__file__).resolve().parents[1] / "customer-comms" / "send-shipment-updates.py"
EMAIL_STATE_ENV = "SHIPMENT_EMAIL_STATE"


@dataclass(frozen=True)
class TriggerResult:
    ok: bool
    reason: str
    argv: tuple[str, ...]


def email_state_path(store_path: Path) -> Path:
    """JSON handoff beside the store. The sender reads this; it takes no argv."""
    return store_path.with_name(store_path.name + ".email.json")


def order_line_payload(shipment: Shipment) -> dict:
    """One order line inside the staged handoff, including only real links."""
    tracking_url, tracking_html, order_url, order_html = email_links(shipment)
    payload = {
        "shipment_id": shipment.shipment_id,
        "line": shipment.line,
        "line_ending": shipment.line[-4:] if len(shipment.line) >= 4 else shipment.line,
        "device": shipment.device,
        "carrier": shipment.carrier,
        "tracking": shipment.tracking_number,
        "status": shipment.status,
        "delivered": bool(shipment.ever_delivered or shipment.status == DELIVERED),
        "order_closed": bool(shipment.order_closed),
        "estimated_delivery": shipment.estimated_delivery or "",
        "tracking_url": tracking_url,
        "tracking_link": tracking_html,
    }
    if order_url and order_html:
        payload["order_status_url"] = order_url
        payload["order_status_link"] = order_html
    return payload


def email_handoff(
    shipment: Shipment,
    event: str,
    description: str,
    event_time: str,
    order_lines: list[Shipment] | None = None,
) -> dict:
    """Fields the sender used to take as flags, plus the rendered body.

    ``send-shipment-updates.py`` accepts no command-line arguments. The
    tracking and order-status links are here, in the state file, because
    ``--tracking-url``, ``--tracking-link``, ``--order-status-url``, and
    ``--order-status-link`` on argv are ignored.
    """
    tracking_url, tracking_html, order_url, order_html = email_links(shipment)
    payload = {
        "event": event,
        "shipment_id": shipment.shipment_id,
        "order_id": shipment.order_id,
        "customer_name": shipment.customer_name,
        "customer_email": shipment.customer_email,
        "company": shipment.company,
        "line": shipment.line,
        "device": shipment.device,
        "carrier": shipment.carrier,
        "tracking_number": shipment.tracking_number,
        "status_description": description,
        "event_time": event_time,
        "lookup_email": shipment.lookup_email,
        "lookup_zip": shipment.lookup_zip,
        "tracking_url": tracking_url,
        "tracking_link": tracking_html,
        "body": render_email_body(shipment, event, description),
    }
    if order_url and order_html:
        payload["order_status_url"] = order_url
        payload["order_status_link"] = order_html
    siblings = order_lines if order_lines else [shipment]
    payload["order_lines"] = [order_line_payload(item) for item in siblings]
    payload["order_closed"] = bool(shipment.order_closed or any(item.order_closed for item in siblings))
    return payload


def write_email_state(path: Path, payload: dict) -> None:
    """Write the handoff at mode 0600 from the moment it is created."""
    write_private_text(path, json.dumps(payload, indent=2) + "\n")


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
    def __init__(
        self,
        script: Path = DEFAULT_SENDER,
        mode: str = "dry-run",
        runner=None,
        state_path: Path | None = None,
    ) -> None:
        if mode not in {"dry-run", "execute"}:
            raise ValueError("email mode must be dry-run or execute")
        self.script = script
        self.mode = mode
        self.runner = runner or _subprocess_runner
        self.state_path = state_path
        self.planned: list[list[str]] = []

    def send(
        self,
        shipment: Shipment,
        event: str,
        description: str,
        event_time: str,
        order_lines: list[Shipment] | None = None,
    ) -> TriggerResult:
        argv = build_argv(self.script, shipment, event, description, event_time)
        if self.mode != "execute":
            self.planned.append(argv)
            return TriggerResult(True, "dry_run", tuple(argv))
        if not self.script.is_file():
            return TriggerResult(False, "sender_missing", tuple(argv))
        if self.state_path is not None:
            write_email_state(
                self.state_path,
                email_handoff(shipment, event, description, event_time, order_lines),
            )
        previous = os.environ.get(EMAIL_STATE_ENV)
        if self.state_path is not None:
            os.environ[EMAIL_STATE_ENV] = str(self.state_path)
        try:
            code = self.runner(argv)
        finally:
            if previous is None:
                os.environ.pop(EMAIL_STATE_ENV, None)
            else:
                os.environ[EMAIL_STATE_ENV] = previous
        if code == 4:
            # The order is not fully delivered yet. Keep the handoff for the retry.
            return TriggerResult(False, "waiting_for_order", tuple(argv))
        if code != 0:
            # Keep the 0600 state file so a retry can read the same handoff.
            return TriggerResult(False, f"sender_exit_{code}", tuple(argv))
        if self.state_path is not None:
            self.state_path.unlink(missing_ok=True)
        self.planned.append(argv)
        return TriggerResult(True, "sent", tuple(argv))


def _subprocess_runner(argv: list[str]) -> int:
    completed = subprocess.run(argv, check=False, capture_output=True, text=True)
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return completed.returncode
