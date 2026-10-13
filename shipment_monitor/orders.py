"""Read att-order-monitor orders.json into shipment rows.

Lookup email and ZIP travel with the order. They are copied onto every
shipment line for that order and are not invented from the customer email.
"""

from __future__ import annotations

from shipment_monitor.models import PENDING, Shipment

_EMAIL_KEYS = ("lookup_email", "lookupEmail")
_ZIP_KEYS = ("lookup_zip", "lookupZip")


def load_shipments_document(raw: dict | list) -> list[Shipment]:
    """Accept a shipment list, a store document, or an orders.json document."""
    shipments: list[Shipment] = []
    lookups: dict[str, tuple[str, str]] = {}
    if isinstance(raw, dict):
        for order in raw.get("orders") or []:
            if isinstance(order, dict):
                _consume_order(order, shipments, lookups)
        for row in raw.get("shipments") or []:
            if isinstance(row, dict):
                shipments.append(Shipment.from_dict(row))
        if not shipments and not lookups and _is_order(raw):
            _consume_order(raw, shipments, lookups)
    elif isinstance(raw, list):
        for row in raw:
            if not isinstance(row, dict):
                raise ValueError("import rows must be objects")
            if _is_order(row) and not _is_shipment(row):
                _consume_order(row, shipments, lookups)
            else:
                shipments.append(Shipment.from_dict(row))
    else:
        raise ValueError("import document must be a list or object")
    for shipment in shipments:
        email, zip_code = lookups.get(shipment.order_id, ("", ""))
        if email and not shipment.lookup_email:
            shipment.lookup_email = email
        if zip_code and not shipment.lookup_zip:
            shipment.lookup_zip = zip_code
    return shipments


def _consume_order(order: dict, shipments: list[Shipment], lookups: dict[str, tuple[str, str]]) -> None:
    order_id = str(order.get("order_id") or order.get("orderId") or "").strip()
    email = _lookup_email(order)
    zip_code = _lookup_zip(order)
    if order_id and (email or zip_code):
        lookups[order_id] = (email, zip_code)
    children = order.get("shipments")
    if children is None:
        children = order.get("lines")
    if isinstance(children, list) and children:
        for child in children:
            if isinstance(child, dict):
                shipments.append(_shipment_from_line(order, child, email, zip_code))
        return
    if _is_shipment(order):
        shipments.append(Shipment.from_dict({**order, "lookup_email": email, "lookup_zip": zip_code}))


def _shipment_from_line(order: dict, line: dict, email: str, zip_code: str) -> Shipment:
    order_id = str(line.get("order_id") or order.get("order_id") or order.get("orderId") or "").strip()
    tracking = str(line.get("tracking_number") or "").strip()
    phone = str(line.get("line") or "").strip()
    shipment_id = str(line.get("shipment_id") or "").strip() or f"{order_id}-{phone or tracking}"
    payload = {
        "shipment_id": shipment_id,
        "order_id": order_id,
        "customer_name": line.get("customer_name") or order.get("customer_name") or "",
        "customer_email": line.get("customer_email") or order.get("customer_email") or "",
        "company": line.get("company") or order.get("company") or "",
        "line": phone,
        "device": line.get("device") or order.get("device") or "",
        "carrier": line.get("carrier") or order.get("carrier") or "",
        "tracking_number": tracking,
        "order_closed": line.get("order_closed", order.get("order_closed", False)),
        "status": line.get("status") or order.get("status") or PENDING,
        "lookup_email": _lookup_email(line) or email,
        "lookup_zip": _lookup_zip(line) or zip_code,
    }
    return Shipment.from_dict(payload)


def _lookup_email(record: dict) -> str:
    for key in _EMAIL_KEYS:
        value = str(record.get(key) or "").strip()
        if value:
            return value
    lookup = record.get("lookup")
    if isinstance(lookup, dict):
        value = str(lookup.get("email") or lookup.get("lookup_email") or "").strip()
        if value:
            return value
    return ""


def _lookup_zip(record: dict) -> str:
    for key in _ZIP_KEYS:
        value = str(record.get(key) or "").strip()
        if value:
            return value
    lookup = record.get("lookup")
    if isinstance(lookup, dict):
        for key in ("zip", "zip_code", "postal_code"):
            value = str(lookup.get(key) or "").strip()
            if value:
                return value
    return ""


def _is_shipment(row: dict) -> bool:
    return bool(str(row.get("shipment_id") or "").strip() and str(row.get("tracking_number") or "").strip())


def _is_order(row: dict) -> bool:
    if _lookup_email(row) or _lookup_zip(row):
        return True
    children = row.get("shipments")
    if children is None:
        children = row.get("lines")
    return isinstance(children, list)
