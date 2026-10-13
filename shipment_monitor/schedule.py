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

# Every 30 minutes from 09:30 through 20:30 Eastern.
# Minute 0 of a tracking-cycle hour is left to the 2-hour job so the two
# commands do not start together on the same store.
DELIVERY_DAY_COMMAND = f"{TRACKING_CYCLE_COMMAND} --delivery-day"
# On-the-hour slots inside the delivery window that the 2-hour cycle does not use.
DELIVERY_DAY_OFFSET_HOURS = (10, 12, 14, 16, 18, 20)


def _hours(values: tuple[int, ...]) -> str:
    return ",".join(str(hour) for hour in values)


def att_order_cron() -> str:
    """Weekday AT&T order-status check."""
    return f"0 {_hours(ATT_ORDER_HOURS)} * * {ATT_ORDER_WEEKDAYS} {ATT_ORDER_COMMAND}"


def tracking_cycle_cron() -> str:
    """Daily tracking-page fetch, poll, and send."""
    return f"0 {_hours(TRACKING_CYCLE_HOURS)} * * * {TRACKING_CYCLE_COMMAND}"


def delivery_day_cron_lines() -> tuple[str, str]:
    """30-minute delivery-day poll, skipping minutes the 2-hour cycle uses.

    Half hours from 09:30 through 20:30, plus 10:00, 12:00, 14:00, 16:00,
    18:00, and 20:00. Not 09:00, 11:00, 13:00, 15:00, 17:00, 19:00, or 21:00.
    """
    return (
        f"30 9-20 * * * {DELIVERY_DAY_COMMAND}",
        f"0 {_hours(DELIVERY_DAY_OFFSET_HOURS)} * * * {DELIVERY_DAY_COMMAND}",
    )


def delivery_day_clock_times() -> tuple[str, ...]:
    """Delivery-day clock times that do not collide with the 2-hour cycle."""
    overlap = set(TRACKING_CYCLE_HOURS)
    times: list[str] = []
    hour, minute = 9, 0
    while (hour, minute) <= (21, 0):
        if not (minute == 0 and hour in overlap):
            times.append(f"{hour:02d}:{minute:02d}")
        minute += 30
        if minute >= 60:
            hour += 1
            minute = 0
    return tuple(times)


def crontab_text() -> str:
    """Installable crontab. Copy it; this repository does not install it."""
    first, last = delivery_day_cron_lines()
    return "\n".join(
        [
            f"CRON_TZ={EASTERN}",
            "SHIPMENT_ALLOW_EMAIL=1",
            "# AT&T order status (~/workspace/att-order-monitor). Weekdays 9:00 AM, 1:00 PM, 5:00 PM Eastern.",
            att_order_cron(),
            f"# Tracking page fetch + poll + send. Daily {', '.join(f'{hour:02d}:00' for hour in TRACKING_CYCLE_HOURS)} Eastern.",
            tracking_cycle_cron(),
            "# Delivery-day poll only. Every 30 minutes from 09:30 through 20:30 Eastern,",
            "# skipping 09:00, 11:00, 13:00, 15:00, 17:00, 19:00, and 21:00 (the 2-hour cycle).",
            first,
            last,
            "",
        ]
    )


def main() -> int:
    sys.stdout.write(crontab_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
