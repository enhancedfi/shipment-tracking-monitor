"""AT&T order-status link and the carrier tracking link for customer email.

The order-status URL is built only from the lookup email and ZIP recorded
by att-order-monitor. A missing or unusable value omits the link. Customer
notification email is never substituted.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlencode

from shipment_monitor.models import Shipment
from shipment_monitor.page_source import tracking_page_url

ORDER_DETAILS = "https://www.att.com/acctmgmt/order/details"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ZIP = re.compile(r"^\d{5}(?:-\d{4})?$")


def order_status_url(lookup_email: str, lookup_zip: str) -> str | None:
    """Deep link to AT&T order details, or None when email or ZIP is absent."""
    email = (lookup_email or "").strip()
    zip_code = re.sub(r"\s+", "", lookup_zip or "")
    if not email or not zip_code:
        return None
    if _EMAIL.fullmatch(email) is None or _ZIP.fullmatch(zip_code) is None:
        return None
    query = urlencode({"email": email, "zip": zip_code})
    return f"{ORDER_DETAILS}?{query}"


def html_anchor(url: str, label: str) -> str:
    href = html.escape(url, quote=True)
    text = html.escape(label)
    return f'<a href="{href}">{text}</a>'


def email_links(shipment: Shipment) -> tuple[str, str, str | None, str | None]:
    """Tracking URL, its anchor, and the order-status pair when one exists.

    The order-status values are None when lookup email or ZIP is missing.
    """
    tracking = tracking_page_url(shipment.carrier, shipment.tracking_number)
    tracking_html = html_anchor(tracking, "Track shipment")
    order = order_status_url(shipment.lookup_email, shipment.lookup_zip)
    if order is None:
        return tracking, tracking_html, None, None
    return tracking, tracking_html, order, html_anchor(order, "AT&T order status")
