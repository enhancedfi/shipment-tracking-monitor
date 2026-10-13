# Shipment Tracking Monitor

Tracks active phone shipments, detects status changes, and hands those changes to the existing customer email sender. It also builds the short shipment summary used at 6:00 AM, noon, and 4:00 PM Eastern.

This repository does not send customer email and does not install a schedule. The crons below are ready to copy in. Checks run only when you start them, or when those crons do. No carrier developer API credentials are required.

## What it watches

Each row is one phone line: customer, order, device, carrier, and tracking number. Several lines can share one box and one tracking number.

The live path reads public tracking pages. Carrier sites need JavaScript, so a browser step saves each page's visible text and this monitor parses the file. FedEx, UPS, and USPS official track APIs stay available as an optional path when page text is not configured. A webhook payload is still accepted.

Customer-facing statuses:

| Status | Meaning |
| --- | --- |
| `shipped` | Carrier has the package. In transit, including UPS "Preparing for Delivery" while the package is still at the facility. |
| `delayed` | Exception, hold, or delay |
| `out_for_delivery` | On the delivery vehicle, out for delivery, or available for pickup |
| `delivered` | Delivered |

Label-created and unchanged in-transit scans do not send another email.

## How a cycle runs

The AT&T order monitor (`~/workspace/att-order-monitor`) reads order status pages and emits shipment rows. This repo then checks public tracking pages and hands status changes to `~/workspace/customer-comms/send-shipment-updates.py`.

1. Import the new rows so tracking numbers are in the store. `run` does this when you pass `--input`. You can also import first, which is what the browser step needs.
2. Print the public URLs for shipments that are not delivered and not closed. Save each page's visible text as `data/page-text/<tracking_number>.txt`.
3. Run import, poll, change detection, and the sender in one command.

```bash
python track_shipments.py import-shipments \
  --store data/shipments.json \
  --input data/incoming-shipments.json

python list_tracking_pages.py data/shipments.json
# Browser opens each URL and writes data/page-text/<tracking_number>.txt

python track_shipments.py run \
  --store data/shipments.json \
  --input data/incoming-shipments.json \
  --pages data/page-text
```

`run` writes the store. Email stays a dry run: it prints the sender arguments and does not start the script. `--execute` calls `send-shipment-updates.py` and also requires `SHIPMENT_ALLOW_EMAIL=1`.

The cron entry point is `run_tracking_cycle.py`. It imports `data/incoming-shipments.json` when that file exists, prints the public URLs, runs `SHIPMENT_PAGE_FETCH` when that command is set (URLs on stdin, page text into `data/page-text/<tracking_number>.txt`), then polls with `--force` and `--execute`. `--force` is only on this scheduled command, so the 2-hour cadence is not skipped by the 3-hour cooldown used for a manual check. Customer email is still at most one per 6 hours per customer. `shipped` and `delivered` still go out immediately.

Each `import-shipments` and `run` command holds an exclusive flock on `data/shipments.json.lock` for the throttle check and the store write. The lock file is created at mode 0600 and opened with `O_NOFOLLOW`. A second run waits up to 20 minutes. If the lock is still held, that run skips and logs the skip. `run_tracking_cycle.py` starts those commands as separate processes, and each one takes the lock itself.

A tracking number imported in the same pass as the poll has no page file until the next fetch. The poll reports `missing_snapshot` and does not mark the line checked, so the next URL list includes it.

### Schedules

Times are Eastern (`America/New_York`). `CRON_TZ` keeps them on Eastern time when daylight saving changes. The lines live in `shipment_monitor/schedule.py`. `python -m shipment_monitor.schedule` prints the same block.

AT&T order status, the existing check in `~/workspace/att-order-monitor`. Weekdays only, expanded from 9:00 AM so a same-day shipment is caught the same day:

- 9:00 AM, 1:00 PM, 5:00 PM ET

Tracking page fetch, poll, and send, this repo, every day:

- 07:00, 09:00, 11:00, 13:00, 15:00, 17:00, 19:00, 21:00 ET

Delivery-day poll, this repo, every day, only shipments whose ETA is today, whose ETA has already passed and are not delivered, or that are out for delivery:

- 09:30 through 20:30 ET every 30 minutes, including 10:00, 12:00, 14:00, 16:00, 18:00, and 20:00
- not at 09:00, 11:00, 13:00, 15:00, 17:00, 19:00, or 21:00, when the 2-hour cycle already runs

```cron
CRON_TZ=America/New_York
# AT&T order status (~/workspace/att-order-monitor). Weekdays 9:00 AM, 1:00 PM, 5:00 PM Eastern.
0 9,13,17 * * 1-5 cd "$HOME/workspace/att-order-monitor" && python3 -m att_order_monitor
# Tracking page fetch + poll + send. Daily 07:00, 09:00, 11:00, 13:00, 15:00, 17:00, 19:00, 21:00 Eastern.
0 7,9,11,13,15,17,19,21 * * * cd "$HOME/workspace/shipment-tracking-monitor" && python3 run_tracking_cycle.py
# Delivery-day poll only. Every 30 minutes from 09:30 through 20:30 Eastern,
# skipping 09:00, 11:00, 13:00, 15:00, 17:00, 19:00, and 21:00 (the 2-hour cycle).
30 9-20 * * * cd "$HOME/workspace/shipment-tracking-monitor" && python3 run_tracking_cycle.py --delivery-day
0 10,12,14,16,18,20 * * * cd "$HOME/workspace/shipment-tracking-monitor" && python3 run_tracking_cycle.py --delivery-day
```

Delivery-day means the saved page ETA is today in Eastern time, that ETA has passed and the package is not delivered, or the carrier status is already `out_for_delivery`. A standard in-transit shipment whose ETA is a later day stays on the 2-hour job. `--delivery-day` polls only those lines. `30 9-20` is 09:30 through 20:30. `0 10,12,14,16,18,20` is the on-the-hour slots the 2-hour cycle does not use. 21:30 is outside the window. A page that only says "Today" is ignored when the saved file is from an earlier Eastern day. "Wednesday, October 14" and "Oct 14" are read as estimated delivery. The year is this year, the previous year when that day was at most about 60 days ago (Dec 31 checked on Jan 1 stays last year), or next year when the day is already more than about 60 days past. A date-only scan line is not the ETA.

Set `SHIPMENT_ALLOW_EMAIL=1` in the environment that starts the tracking job when that job should invoke the sender. The printed crontab leaves that unset. The 6-hour throttle still applies. The AT&T command is that repo's order-status check (`python3 -m att_order_monitor`); change it in `shipment_monitor/schedule.py` if the entry point differs.

Public page URLs:

| Carrier | URL |
| --- | --- |
| UPS | `https://www.ups.com/track?tracknum=<number>` |
| FedEx | `https://www.fedex.com/fedextrack/?trknbr=<number>` |
| USPS | `https://tools.usps.com/go/TrackConfirmAction?tLabels=<number>` |

Page text directory: `--pages`, or `SHIPMENT_PAGE_TEXT_DIR`. `run` defaults to `data/page-text`. `check` uses that directory when it already contains `.txt` files, so a poll with no API keys is not `carrier_not_configured`.

`examples/page-text/` holds the UPS pages captured on 2026-10-13 for `1ZH4K6592930123854` and `1ZH4K6592920755842`.

## Status mapping

The headline on the page is the customer status. A progress bar that lists a future "Delivered" step is not a delivery. A dated scan that says the package was delivered still beats an earlier pickup.

| Page text | Status |
| --- | --- |
| UPS "Preparing for Delivery" (latest scan still at the facility, for example "Processing at UPS Facility") | `shipped` |
| "On the Way", "In Transit", "Picked up", "Processing at UPS Facility", arrived or departed a facility | `shipped` |
| UPS "Loaded on Delivery Vehicle" | `out_for_delivery` |
| "Out for Delivery", "On vehicle for delivery", "On FedEx vehicle for delivery", "Available for Pickup" | `out_for_delivery` |
| "Delivered" as the headline or as a dated scan | `delivered` |
| "Delivery exception", "Delayed", "Weather delay", "Delivery Attempt", "Alert", hold | `delayed` |
| "Label Created", "Shipment information sent", "Pre-Shipment" | `pending` (no email) |

UPS "Preparing for Delivery" is in transit. The package is at the facility being prepared. It is not `out_for_delivery` until a scan says it is on the vehicle.

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
  --event-time ... \
  --tracking-url ... \
  --tracking-link <a href="...">Track shipment</a>
```

When the shipment has both a recorded lookup email and ZIP, the argv also includes `--order-status-url` and `--order-status-link`. The URL is `https://www.att.com/acctmgmt/order/details?email=...&zip=...`, and `--order-status-link` is that URL as an HTML anchor. If either lookup field is missing, those two arguments are omitted. The customer notification address is not used in their place.

`send-shipment-updates.py` takes no command-line arguments, so those four link flags are ignored if they are the only place the URLs exist. The monitor writes the same payload, including `tracking_url`, `tracking_link`, `order_status_url`, `order_status_link`, and the rendered HTML `body`, to `data/shipments.json.email.json` beside the store. `SHIPMENT_EMAIL_STATE` is set to that path for the sender process. The file is created at mode 0600. It is deleted after the sender exits 0. When the sender exits non-zero, the file stays at mode 0600 so a retry can read the same handoff. The body contains the carrier tracking link and, when lookup email and ZIP are both present, the AT&T order-status link.

`orders.json` from att-order-monitor imports through the same command. Each order's `lookup_email` and `lookup_zip` are copied onto its shipment lines:

```bash
python track_shipments.py import-shipments --store data/shipments.json --input data/orders.json
```

Override the path with `--sender` or `SHIPMENT_SENDER_SCRIPT`. The default command is a dry run: it prints the arguments and does not start the script.

Three rules are applied here, before the sender is invoked:

- A delivered line can never trigger a shipped email. If a payload contains both a pickup scan and a delivery scan, the line is delivered.
- A closed order does not get another email. Importing a refreshed row cannot clear `order_closed` or `ever_delivered`.
- Each customer gets at most one email per 6 hours. `shipped` and `delivered` still go out immediately.

`delayed` and `out_for_delivery` that fall inside the 6-hour window stay queued on the shipment as `pending_event` and are tried on a later check. They are dropped if the line has moved on, or if the order is closed.

## Carrier reads

Prefer a webhook or saved page text. `track_shipments.py webhook` reads one payload file and uses the same rules as a poll.

Page-text polling:

- Shipments already delivered or on a closed order are not polled and are not printed by `list_tracking_pages.py`.
- A tracking number is skipped for 3 hours after a successful read (`--cooldown-hours`, `SHIPMENT_COOLDOWN_HOURS`).
- A missing text file is `missing_snapshot`. The line is not marked checked.

Official APIs are an explicit opt-in. `check --api` or `SHIPMENT_USE_API=1` builds them, and only when no page-text directory is configured. A manual `check` with API keys in the environment and an empty page-text folder does not call the APIs. `run` and the crons stay on saved page text. Leave the variables unset if you are not using them.

- Calls to one carrier are spaced by `CARRIER_MIN_INTERVAL_SECONDS` (default 1).
- Each run allows `SHIPMENT_MAX_TRACK_REQUESTS` HTTP calls per carrier (default 60). FedEx numbers are sent in batches of 30.
- HTTP 429 stops that carrier for the rest of the run. There is no retry loop.

| Variable | Purpose |
| --- | --- |
| `FEDEX_CLIENT_ID`, `FEDEX_CLIENT_SECRET` | FedEx Track API |
| `FEDEX_API_BASE` | Default `https://apis.fedex.com` |
| `UPS_CLIENT_ID`, `UPS_CLIENT_SECRET` | UPS Track API |
| `UPS_API_BASE` | Default `https://onlinetools.ups.com` |
| `USPS_CLIENT_ID`, `USPS_CLIENT_SECRET` | USPS Tracking v3 |
| `USPS_API_BASE` | Default `https://apis.usps.com` |
| `SHIPMENT_PAGE_TEXT_DIR` | Saved `<tracking_number>.txt` files |

Sandbox hosts, when you have sandbox keys: `https://apis-sandbox.fedex.com`, `https://wwwcie.ups.com`, `https://apis-tem.usps.com`.

## Setup

Python 3.11 or newer. No third-party packages.

```bash
cp examples/shipments.sample.json data/shipments.json
mkdir -p data/page-text
```

`data/` is gitignored. The sample file uses fictional customers.

## Commands

Preview a check against saved carrier JSON. This does not write the store and does not email anyone:

```bash
python track_shipments.py check \
  --store examples/shipments.sample.json \
  --fixtures examples/fixtures \
  --now 2026-10-09T16:00:00+00:00
```

Poll saved public page text. With no API keys this does not stop at `carrier_not_configured`:

```bash
python track_shipments.py check \
  --store data/shipments.json \
  --pages data/page-text \
  --commit
```

Print the URLs a browser step should open:

```bash
python list_tracking_pages.py data/shipments.json
```

Import, poll page text, and hand changes to the sender (dry run):

```bash
python track_shipments.py run \
  --store data/shipments.json \
  --input data/incoming-shipments.json \
  --pages data/page-text
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

`data/shipments.json` holds `shipments` and `email_log` and is written at mode 0600. Times are timezone-aware ISO-8601, stored in UTC by the monitor. Page event times are the local clock time printed on the tracking page. The email log is how the 6-hour window is counted, and it is written only after the sender exits 0.

## Tests

No network and no customer email:

```bash
python -m unittest discover -s tests -v
```

The tests cover carrier payload mapping (including a delivery scan that must beat an earlier pickup), the 2026-10-13 UPS page text, FedEx and USPS page phrases, the three email guards, cooldown and request spacing, the sender argument list, and the three briefing slots.
