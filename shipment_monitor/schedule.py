"""Clock times for the automatic cycle.

Cron is not installed from here. `python -m shipment_monitor.schedule`
prints the lines to install. Times are America/New_York so they stay on
Eastern time across daylight saving.

The customer email throttle is unchanged: one email per customer per 6
hours, and shipped and delivered still send immediately.
"""

from __future__ import annotations

import sys

EASTERN = "America/New_York"

# Weekdays 9:00 AM, 1:00 PM, 5:00 PM Eastern.
# Expanded from 9:00 AM only so a same-day shipment is seen the same day.
ATT_ORDER_HOURS = (9, 13, 17)
ATT_ORDER_WEEKDAYS = "1-5"
ATT_ORDER_COMMAND = 'cd "$HOME/workspace/att-order-monitor" && python3 -m att_order_monitor'

# Daily 7:00 AM through 9:00 PM Eastern, every 2 hours.
TRACKING_CYCLE_HOURS = (7, 9, 11, 13, 15, 17, 19, 21)
TRACKING_CYCLE_COMMAND = 'cd "$HOME/workspace/shipment-tracking-monitor" && python3 run_tracking_cycle.py'


def _hours(values: tuple[int, ...]) -> str:
    return ",".join(str(hour) for hour in values)


def att_order_cron() -> str:
    """Weekday AT&T order-status check."""
    return f"0 {_hours(ATT_ORDER_HOURS)} * * {ATT_ORDER_WEEKDAYS} {ATT_ORDER_COMMAND}"


def tracking_cycle_cron() -> str:
    """Daily tracking-page fetch, poll, and send."""
    return f"0 {_hours(TRACKING_CYCLE_HOURS)} * * * {TRACKING_CYCLE_COMMAND}"


def crontab_text() -> str:
    """Installable crontab. Copy it; this repository does not install it."""
    return "\n".join(
        [
            f"CRON_TZ={EASTERN}",
            "SHIPMENT_ALLOW_EMAIL=1",
            f"# AT&T order status (~/workspace/att-order-monitor). Weekdays 9:00 AM, 1:00 PM, 5:00 PM Eastern.",
            att_order_cron(),
            f"# Tracking page fetch + poll + send. Daily {', '.join(f'{hour:02d}:00' for hour in TRACKING_CYCLE_HOURS)} Eastern.",
            tracking_cycle_cron(),
            "",
        ]
    )


def main() -> int:
    sys.stdout.write(crontab_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
