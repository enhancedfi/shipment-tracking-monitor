"""The automatic cycle clock, and the cron entry point that follows it."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shipment_monitor.detect import EMAIL_WINDOW
from shipment_monitor.models import DELIVERED, IMMEDIATE_EVENTS, SHIPPED
from shipment_monitor.schedule import (
    ATT_ORDER_HOURS,
    ATT_ORDER_WEEKDAYS,
    EASTERN,
    TRACKING_CYCLE_HOURS,
    att_order_cron,
    crontab_text,
    delivery_day_clock_times,
    delivery_day_cron_lines,
    tracking_cycle_cron,
)

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "examples" / "page-text"
PREPARING = "1Z999AA10123456784"


class ScheduleTests(unittest.TestCase):
    def test_owner_clock_times(self):
        self.assertEqual(EASTERN, "America/New_York")
        self.assertEqual(ATT_ORDER_HOURS, (9, 13, 17))
        self.assertEqual(ATT_ORDER_WEEKDAYS, "1-5")
        self.assertEqual(TRACKING_CYCLE_HOURS, (7, 9, 11, 13, 15, 17, 19, 21))
        self.assertEqual(TRACKING_CYCLE_HOURS[0], 7)
        self.assertEqual(TRACKING_CYCLE_HOURS[-1], 21)
        self.assertEqual([hour - previous for previous, hour in zip(TRACKING_CYCLE_HOURS, TRACKING_CYCLE_HOURS[1:])], [2] * 7)

    def test_crontab_lines_match_those_hours(self):
        self.assertIn("0 9,13,17 * * 1-5 ", att_order_cron())
        self.assertIn('cd "$HOME/workspace/att-order-monitor"', att_order_cron())
        self.assertIn("0 7,9,11,13,15,17,19,21 * * * ", tracking_cycle_cron())
        self.assertIn("python3 run_tracking_cycle.py", tracking_cycle_cron())
        text = crontab_text()
        self.assertTrue(text.startswith("CRON_TZ=America/New_York\n"))
        self.assertNotIn("SHIPMENT_ALLOW_EMAIL", text)
        self.assertIn(att_order_cron() + "\n", text)
        self.assertIn(tracking_cycle_cron() + "\n", text)

    def test_readme_documents_the_same_crontab(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(att_order_cron(), readme)
        self.assertIn(tracking_cycle_cron(), readme)
        first, last = delivery_day_cron_lines()
        self.assertIn(first, readme)
        self.assertIn(last, readme)
        self.assertIn("CRON_TZ=America/New_York", readme)
        cron = readme.split("```cron\n", 1)[1].split("\n```", 1)[0]
        self.assertNotIn("SHIPMENT_ALLOW_EMAIL", cron)
        self.assertIn("9:00 AM, 1:00 PM, 5:00 PM", readme)
        self.assertIn("07:00, 09:00, 11:00, 13:00, 15:00, 17:00, 19:00, 21:00", readme)
        self.assertIn("--delivery-day", readme)

    def test_schedule_lists_the_30_minute_delivery_day_job(self):
        times = delivery_day_clock_times()
        self.assertEqual(times[0], "09:30")
        self.assertEqual(times[-1], "20:30")
        self.assertIn("10:00", times)
        self.assertNotIn("09:00", times)
        self.assertNotIn("21:00", times)
        self.assertNotIn("21:30", times)
        cycle_minutes = {f"{hour:02d}:00" for hour in TRACKING_CYCLE_HOURS}
        self.assertFalse(set(times) & cycle_minutes)
        self.assertEqual(len(times), 18)
        first, last = delivery_day_cron_lines()
        self.assertIn("30 9-20 * * *", first)
        self.assertIn("--delivery-day", first)
        self.assertIn("0 10,12,14,16,18,20 * * *", last)
        self.assertNotIn("0,30 9-20", first)
        self.assertFalse(last.startswith("0 21 "))
        self.assertIn(first, crontab_text())
        self.assertIn(last, crontab_text())

    def test_email_throttle_is_unchanged(self):
        self.assertEqual(EMAIL_WINDOW, timedelta(hours=6))
        self.assertEqual(IMMEDIATE_EVENTS, frozenset({SHIPPED, DELIVERED}))


class TrackingCycleTests(unittest.TestCase):
    def test_cycle_refuses_to_send_without_the_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, pages, sender, log = _layout(Path(tmp))
            env = _env(log, allow=False)
            completed = _cycle(store, pages, sender, env)
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertFalse(log.exists())
            self.assertIn("Refusing to email", completed.stderr)

    def test_cycle_polls_inside_the_cooldown_and_sends_through_the_stub(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, pages, sender, log = _layout(root)
            recent = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            store.write_text(
                json.dumps(
                    {
                        "shipments": [_row(status="pending", last_checked_at=recent)],
                        "email_log": [],
                    }
                ),
                encoding="utf-8",
            )
            captured = root / "urls.txt"
            env = _env(log, allow=True)
            env["SHIPMENT_PAGE_FETCH"] = f"{sys.executable} -c \"import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())\" {captured}"
            completed = _cycle(store, pages, sender, env)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(f"https://www.ups.com/track?tracknum={PREPARING}", completed.stdout)
            self.assertIn(PREPARING, captured.read_text(encoding="utf-8"))
            sent = log.read_text(encoding="utf-8")
            self.assertIn("\nshipped\n", sent)
            self.assertNotIn("\ndelivered\n", sent)
            saved = json.loads(store.read_text(encoding="utf-8"))
            self.assertEqual(saved["shipments"][0]["status"], "shipped")
            self.assertTrue(any(entry["event"] == "shipped" for entry in saved["email_log"]))

    def test_two_simultaneous_runs_send_one_email_and_keep_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, pages, sender, log = _layout(root)
            store.write_text(
                json.dumps(
                    {
                        "shipments": [_row(status="pending", last_checked_at=None, company="Old Co")],
                        "email_log": [
                            {
                                "customer_email": "other@example.com",
                                "shipment_id": "old",
                                "event": "delivered",
                                "sent_at": "2026-10-01T12:00:00+00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            incoming = root / "incoming.json"
            incoming.write_text(
                json.dumps([_row(status="pending", last_checked_at=None, company="Kept Co")]),
                encoding="utf-8",
            )
            env = _env(log, allow=True)
            full = _spawn_cycle(store, pages, incoming, env, delivery_day=False)
            delivery = _spawn_cycle(store, pages, incoming, env, delivery_day=True)
            full_out, full_err = full.communicate(timeout=30)
            delivery_out, delivery_err = delivery.communicate(timeout=30)
            self.assertEqual(full.returncode, 0, full_err)
            self.assertEqual(delivery.returncode, 0, delivery_err)
            self.assertIn(PREPARING, full_out + delivery_out)
            sent = log.read_text(encoding="utf-8")
            self.assertEqual(sent.count("\n---\n"), 1, sent)
            self.assertEqual(sent.count("\nshipped\n"), 1, sent)
            saved = json.loads(store.read_text(encoding="utf-8"))
            row = saved["shipments"][0]
            self.assertEqual(row["status"], "shipped")
            self.assertEqual(row["company"], "Kept Co")
            self.assertIn("shipped", row["notified_events"])
            shipped = [entry for entry in saved["email_log"] if entry["event"] == "shipped"]
            self.assertEqual(len(shipped), 1)
            self.assertTrue(any(entry["shipment_id"] == "old" for entry in saved["email_log"]))
            self.assertTrue((root / "shipments.json.lock").is_file())


def _spawn_cycle(
    store: Path,
    pages: Path,
    incoming: Path,
    env: dict[str, str],
    delivery_day: bool,
) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        str(ROOT / "run_tracking_cycle.py"),
        "--store",
        str(store),
        "--input",
        str(incoming),
        "--pages",
        str(pages),
    ]
    if delivery_day:
        command.append("--delivery-day")
    return subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _cycle(store: Path, pages: Path, sender: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "run_tracking_cycle.py"),
            "--store",
            str(store),
            "--input",
            str(store.parent / "missing-incoming.json"),
            "--pages",
            str(pages),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _layout(root: Path) -> tuple[Path, Path, Path, Path]:
    pages = root / "pages"
    pages.mkdir()
    (pages / f"{PREPARING}.txt").write_text((PAGES / f"{PREPARING}.txt").read_text(encoding="utf-8"), encoding="utf-8")
    store = root / "shipments.json"
    log = root / "sender.log"
    sender = root / "send-shipment-updates.py"
    sender.write_text(
        "import os, sys, time\nfrom pathlib import Path\n"
        "time.sleep(0.4)\n"
        "Path(os.environ['STUB_LOG']).open('a').write('\\n'.join(sys.argv) + '\\n---\\n')\n",
        encoding="utf-8",
    )
    return store, pages, sender, log


def _env(log: Path, allow: bool) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "SHIPMENT_ALLOW_EMAIL",
            "SHIPMENT_PAGE_TEXT_DIR",
            "SHIPMENT_SENDER_SCRIPT",
            "SHIPMENT_PAGE_FETCH",
            "SHIPMENT_STORE",
            "SHIPMENT_INCOMING",
            "SHIPMENT_STORE_LOCK_HELD",
            "SHIPMENT_EMAIL_STATE",
        }
        and not key.endswith("_CLIENT_ID")
        and not key.endswith("_CLIENT_SECRET")
    }
    env["STUB_LOG"] = str(log)
    env["SHIPMENT_SENDER_SCRIPT"] = str(log.parent / "send-shipment-updates.py")
    if allow:
        env["SHIPMENT_ALLOW_EMAIL"] = "1"
    return env


def _row(status: str, last_checked_at: str | None, company: str = "Example Dental") -> dict:
    return {
        "shipment_id": "prep",
        "order_id": "99-1",
        "customer_name": "Riley Chen",
        "customer_email": "a@example.com",
        "company": company,
        "line": "6165550101",
        "device": "iPhone 17",
        "carrier": "ups",
        "tracking_number": PREPARING,
        "status": status,
        "last_checked_at": last_checked_at,
    }


if __name__ == "__main__":
    unittest.main()
