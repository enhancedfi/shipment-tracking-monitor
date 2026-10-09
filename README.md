# Shipment Tracking Monitor

Tracks active phone shipments, detects status changes, and hands those changes to the existing customer email sender. It also builds the short shipment summary used at 6:00 AM, noon, and 4:00 PM Eastern.

This repository does not send customer email and does not install a schedule. Checks run only when you start them.

## What it watches

Each row is one phone line: customer, order, device, carrier, and tracking number. Several lines can share one box and one tracking number. FedEx, UPS, and USPS are read from their official track APIs, or from a webhook payload you pass in. The monitor does not scrape carrier websites.

Customer-facing statuses:

| Status | Meaning |
| --- | --- |
| `shipped` | Carrier has the package |
| `delayed` | Exception, hold, or delay |
| `out_for_delivery` | Out for delivery, or available for pickup |
| `delivered` | Delivered |

Label-created and unchanged in-transit scans do not send another email.

## Existing email sender

Customer wording stays in `~/workspace/customer-comms/send-shipment-updates.py`. This monitor only decides whether that script may run, then calls it:

```text
python send-shipment-updates.py \
  --event shipped|delayed|out_for_delivery|delivered \
  --shipment-id ... \
  --order-id ... \
  --customer-name ... \
  --customer-email ... \
  --company ... \
  --line ... \
  --device ... \
  --carrier fedex|ups|usps \
  --tracking-number ... \
  --status-description ... \
  --event-time ...
```

Override the path with `--sender` or `SHIPMENT_SENDER_SCRIPT`. The default command is a dry run: it prints the arguments and does not start the script.

Three rules are applied here, before the sender is invoked:

- A delivered line can never trigger a shipped email. If a payload contains both a pickup scan and a delivery scan, the line is delivered.
- A closed order does not get another email. Importing a refreshed row cannot clear `order_closed` or `ever_delivered`.
- Each customer gets at most one email per 6 hours. `shipped` and `delivered` still go out immediately.

`delayed` and `out_for_delivery` that fall inside the 6-hour window stay queued on the shipment as `pending_event` and are tried on a later check. They are dropped if the line has moved on, or if the order is closed.

## Carrier calls

Prefer a webhook. `track_shipments.py webhook` reads one payload file and uses the same rules as a poll. Live polling is the fallback:

- Shipments already delivered or on a closed order are not polled.
- A tracking number is skipped for 3 hours after a successful read (`--cooldown-hours`, `SHIPMENT_COOLDOWN_HOURS`).
- Calls to one carrier are spaced by `CARRIER_MIN_INTERVAL_SECONDS` (default 1).
- Each run allows `SHIPMENT_MAX_TRACK_REQUESTS` HTTP calls per carrier (default 60). FedEx numbers are sent in batches of 30.
- HTTP 429 stops that carrier for the rest of the run. There is no retry loop.

Set these when you want live reads. Leave them unset to use `--fixtures`.

| Variable | Purpose |
| --- | --- |
| `FEDEX_CLIENT_ID`, `FEDEX_CLIENT_SECRET` | FedEx Track API |
| `FEDEX_API_BASE` | Default `https://apis.fedex.com` |
| `UPS_CLIENT_ID`, `UPS_CLIENT_SECRET` | UPS Track API |
| `UPS_API_BASE` | Default `https://onlinetools.ups.com` |
| `USPS_CLIENT_ID`, `USPS_CLIENT_SECRET` | USPS Tracking v3 |
| `USPS_API_BASE` | Default `https://apis.usps.com` |

Sandbox hosts, when you have sandbox keys: `https://apis-sandbox.fedex.com`, `https://wwwcie.ups.com`, `https://apis-tem.usps.com`.

## Setup

Python 3.11 or newer. No third-party packages.

```bash
cp examples/shipments.sample.json data/shipments.json
```

`data/` is gitignored. The sample file uses fictional customers.

## Commands

Preview a check. This does not write the store and does not email anyone:

```bash
python track_shipments.py check \
  --store examples/shipments.sample.json \
  --fixtures examples/fixtures \
  --now 2026-10-09T16:00:00+00:00
```

Save carrier status and keep unsent emails queued, still without calling the sender:

```bash
python track_shipments.py check --store data/shipments.json --fixtures examples/fixtures --commit
```

Print a briefing. `auto` selects morning before noon Eastern, noon from noon until 4:00 PM, and afternoon after that.

```bash
python track_shipments.py briefing --store data/shipments.json --slot noon
```

Apply a webhook payload:

```bash
python track_shipments.py webhook \
  --store data/shipments.json \
  --input examples/webhook-delivered.json
```

A raw carrier body is accepted as `{"carrier", "tracking_number", "carrier_payload"}`. A short envelope is also accepted: `carrier`, `tracking_number`, `status_code`, `status_description`, `event_time`.

Refresh descriptive fields from a JSON list. Existing delivery and closed-order flags stay set:

```bash
python track_shipments.py import-shipments --store data/shipments.json --input examples/shipments.sample.json
```

`--execute` calls `send-shipment-updates.py`. It also requires `SHIPMENT_ALLOW_EMAIL=1`. Do not set that when you are testing.

```python
from datetime import datetime, timezone
from shipment_monitor.briefing import shipment_summary

text = shipment_summary(store.shipments, datetime.now(timezone.utc), slot="noon")
```

`slot` is `morning`, `noon`, or `afternoon`. Omit it to use the clock.

## Store

`data/shipments.json` holds `shipments` and `email_log`. Times are timezone-aware ISO-8601, stored in UTC by the monitor. The email log is how the 6-hour window is counted, and it is written only after the sender exits 0.

## Tests

No network and no customer email:

```bash
python -m unittest discover -s tests -v
```

The tests cover carrier payload mapping (including a delivery scan that must beat an earlier pickup), the three email guards, cooldown and request spacing, the sender argument list, and the three briefing slots.
