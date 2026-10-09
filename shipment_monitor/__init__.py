"""Shipment tracking monitor for active phone orders.

The monitor reads carrier status, detects changes, and asks the existing
customer-comms sender to email the customer. It does not compose or send
mail itself.
"""

from shipment_monitor.briefing import shipment_summary
from shipment_monitor.monitor import run_check

__all__ = ["run_check", "shipment_summary"]
