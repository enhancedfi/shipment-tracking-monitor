"""Render the customer email body that includes the tracking and order links.

``send-shipment-updates.py`` takes no command-line arguments, so the four
link flags on argv are ignored. The monitor writes this body, and the URLs
it was built from, into the email state file the sender reads.
"""

from __future__ import annotations

import html

from shipment_monitor.links import email_links
from shipment_monitor.models import Shipment


def render_email_body(shipment: Shipment, event: str, description: str) -> str:
    """HTML body with the carrier tracking link and, when known, the AT&T order link."""
    _tracking_url, tracking_html, _order_url, order_html = email_links(shipment)
    title = event.replace("_", " ")
    lines = [
        "<div>",
        (
            f"<p>{html.escape(shipment.customer_name)}, your {html.escape(shipment.device)} "
            f"is {html.escape(title)}.</p>"
        ),
        f"<p>{html.escape(description)}</p>",
        f"<p>{tracking_html}</p>",
    ]
    if order_html:
        lines.append(f"<p>{order_html}</p>")
    lines.append("</div>")
    return "\n".join(lines)
