"""The in-repo sender: staged links, contact privacy, and the standing rules."""

import importlib.util
import json
import os
import re
import shutil
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from shipment_monitor.email_trigger import EmailTrigger
from shipment_monitor.models import Shipment

ROOT = Path(__file__).resolve().parents[1]
SENDER_PATH = ROOT / "customer-comms" / "send-shipment-updates.py"
EXAMPLE = ROOT / "customer-comms" / "contacts.example.json"
ORDER_ID = "99-EXAMPLE000000001"
TRACKING_URL = "https://www.ups.com/track?tracknum=1Z999AA10123456784"
ORDER_URL = "https://www.att.com/acctmgmt/order/details?email=ada%40example.com&zip=49503"
ALLOWED_EMAILS = {"ava@enhancedfi.pro", "dom@enhancedfi.com", "ada@example.com"}

_RESEND_DIR = None
_SAVED_ENV = {}


def _load_sender():
    spec = importlib.util.spec_from_file_location("send_shipment_updates", SENDER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SENDER = _load_sender()


def setUpModule():
    global _RESEND_DIR, _SAVED_ENV
    _RESEND_DIR = tempfile.mkdtemp()
    (Path(_RESEND_DIR) / "resend.py").write_text(
        "import json, os\n"
        "def _request(method, path, payload=None):\n"
        "    log = os.environ.get('RESEND_STUB_LOG')\n"
        "    if log:\n"
        "        with open(log, 'a', encoding='utf-8') as handle:\n"
        "            handle.write(json.dumps({'method': method, 'path': path, 'payload': payload}) + '\\n')\n"
        "    if path == '/domains':\n"
        "        return 200, {'data': [{'name': 'enhancedfi.pro', 'status': 'verified'}]}\n"
        "    status = int(os.environ.get('RESEND_EMAIL_STATUS', '202'))\n"
        "    if status not in (200, 202):\n"
        "        return status, {'message': 'rejected'}\n"
        "    return status, {'id': 'email_test_1'}\n",
        encoding="utf-8",
    )
    keys = (
        "SHIPMENT_RESEND_BIN",
        "SHIPMENT_CONTACTS",
        "SHIPMENT_COMMS_DIR",
        "SHIPMENT_EMAIL_STATE",
        "SHIPMENT_WATCH_STATE",
        "SHIPMENT_ALLOW_EMAIL",
        "RESEND_STUB_LOG",
        "RESEND_EMAIL_STATUS",
    )
    _SAVED_ENV = {key: os.environ.get(key) for key in keys}
    os.environ["SHIPMENT_RESEND_BIN"] = _RESEND_DIR
    os.environ.pop("SHIPMENT_ALLOW_EMAIL", None)


def tearDownModule():
    for key, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    if _RESEND_DIR:
        shutil.rmtree(_RESEND_DIR, ignore_errors=True)


class SenderRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.contacts = self.root / "contacts.json"
        self.state = self.root / "shipments.json.email.json"
        self.log = self.root / "resend.jsonl"
        _write_private(self.contacts, _book())
        os.environ["SHIPMENT_CONTACTS"] = str(self.contacts)
        os.environ["SHIPMENT_COMMS_DIR"] = str(self.root / "comms")
        os.environ["SHIPMENT_EMAIL_STATE"] = str(self.state)
        os.environ.pop("SHIPMENT_WATCH_STATE", None)
        os.environ["RESEND_STUB_LOG"] = str(self.log)
        os.environ["RESEND_EMAIL_STATUS"] = "202"
        os.environ.pop("SHIPMENT_ALLOW_EMAIL", None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_body_includes_both_links_when_the_handoff_has_them(self):
        _write_state(self.state, _staged(tracking_url=TRACKING_URL, order_status_url=ORDER_URL))
        self.assertEqual(SENDER.main(), 0)
        payload = _posts(self.log)[0]
        self.assertIn(TRACKING_URL, payload["text"])
        self.assertIn(ORDER_URL, payload["text"])
        self.assertIn("Track it here:", payload["text"])
        self.assertIn("AT&T order status:", payload["text"])
        self.assertIn(TRACKING_URL, payload["html"])
        self.assertIn("acctmgmt/order/details", payload["html"])
        self.assertIn("zip=49503", payload["html"])
        self.assertEqual(len(_posts(self.log)), 1)

    def test_a_missing_link_is_left_out(self):
        _write_state(
            self.state,
            _staged(carrier="ups", tracking_number="1Z999AA10123456784", status="shipped"),
        )
        self.assertEqual(SENDER.main(), 0)
        payload = _posts(self.log)[0]
        for blob in (payload["text"], payload["html"]):
            self.assertNotIn("ups.com", blob)
            self.assertNotIn("fedex.com", blob)
            self.assertNotIn("usps.com", blob)
            self.assertNotIn("att.com", blob)
            self.assertNotIn("Track it here", blob)
            self.assertNotIn("AT&T order status", blob)
        self.assertIn("1Z999AA10123456784", payload["text"])

    def test_missing_contact_prints_dominic_notify_and_does_not_send(self):
        _write_state(self.state, _staged(order_id="99-NOT-LISTED"))
        with self.assertLogsStdout() as stdout:
            code = SENDER.main()
        self.assertEqual(code, 2)
        self.assertIn("DOMINIC-NOTIFY", stdout)
        self.assertIn("no CONTACTS entry", stdout)
        self.assertEqual(_posts(self.log), [])

    def test_loose_contacts_file_is_refused(self):
        loose = self.root / "loose.json"
        previous = os.umask(0o022)
        try:
            descriptor = os.open(loose, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            os.write(descriptor, _book().encode("utf-8"))
            os.close(descriptor)
        finally:
            os.umask(previous)
        self.assertNotEqual(stat.S_IMODE(loose.stat().st_mode) & 0o077, 0)
        os.environ["SHIPMENT_CONTACTS"] = str(loose)
        _write_state(self.state, _staged())
        with self.assertLogsStdout() as stdout:
            code = SENDER.main()
        self.assertEqual(code, 2)
        self.assertIn("DOMINIC-NOTIFY", stdout)
        self.assertIn("0600", stdout)
        self.assertEqual(_posts(self.log), [])

    def test_shipped_and_delivered_ignore_the_six_hour_throttle(self):
        throttle = self.root / "comms" / "rate-limit.json"
        throttle.parent.mkdir(parents=True)
        throttle.write_text(
            json.dumps({"ada@example.com": [datetime.now(timezone.utc).isoformat()]}),
            encoding="utf-8",
        )
        _write_state(self.state, _staged(event="delayed", status="delayed", status_description="Weather delay"))
        self.assertEqual(SENDER.main(), 3)
        self.assertEqual(_posts(self.log), [])
        _write_state(self.state, _staged(event="shipped", status="shipped"))
        self.assertEqual(SENDER.main(), 0)
        self.assertEqual(len(_posts(self.log)), 1)
        self.assertIn("on its way", _posts(self.log)[0]["subject"])

    def test_a_delivered_line_does_not_send_a_shipped_email(self):
        _write_state(
            self.state,
            _staged(event="shipped", status="delivered", delivered=True),
        )
        with self.assertLogsStdout() as stdout:
            code = SENDER.main()
        self.assertEqual(code, 0)
        self.assertIn("does not send shipped", stdout)
        self.assertEqual(_posts(self.log), [])

    def test_closed_order_stays_silent(self):
        _write_state(self.state, _staged(order_closed=True, event="shipped", status="shipped"))
        with self.assertLogsStdout() as stdout:
            code = SENDER.main()
        self.assertEqual(code, 0)
        self.assertIn("order closed", stdout)
        self.assertEqual(_posts(self.log), [])

    def test_every_delivered_line_sends_one_email_then_the_order_stays_closed(self):
        _write_state(self.state, _delivered_order())
        self.assertEqual(SENDER.main(), 0)
        payload = _posts(self.log)[0]
        self.assertEqual(payload["subject"], "All your phones have arrived!")
        self.assertIn("Phone A", payload["text"])
        self.assertIn("Phone B", payload["text"])
        self.assertIn(TRACKING_URL, payload["text"])
        self.assertIn(ORDER_URL, payload["text"])
        self.assertEqual(SENDER.main(), 0)
        self.assertEqual(len(_posts(self.log)), 1)

    def test_a_partial_delivery_waits_for_the_rest_of_the_order(self):
        lines = _delivered_order()["order_lines"]
        lines[1]["status"] = "shipped"
        lines[1]["delivered"] = False
        _write_state(self.state, _delivered_order(order_lines=lines))
        self.assertEqual(SENDER.main(), 4)
        self.assertEqual(_posts(self.log), [])

    def test_successful_send_deletes_the_state_file_and_failure_keeps_it(self):
        script = SENDER_PATH
        shipment = _shipment()
        trigger = EmailTrigger(script, mode="execute", state_path=self.state)
        result = trigger.send(shipment, "shipped", "On the way", "2026-10-13T12:00:00+00:00")
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "sent")
        self.assertFalse(self.state.exists())
        payload = _posts(self.log)[0]
        self.assertIn("track?tracknum=1Z999AA10123456784", payload["text"])
        self.assertIn("acctmgmt/order/details", payload["text"])
        self.assertIn("zip=49503", payload["text"])

        os.environ["RESEND_EMAIL_STATUS"] = "500"
        failed = self.root / "failed.email.json"
        os.environ["SHIPMENT_COMMS_DIR"] = str(self.root / "comms-fail")
        trigger = EmailTrigger(script, mode="execute", state_path=failed)
        result = trigger.send(shipment, "shipped", "On the way", "2026-10-13T12:00:00+00:00")
        self.assertFalse(result.ok)
        self.assertTrue(failed.is_file())
        self.assertEqual(stat.S_IMODE(failed.stat().st_mode), 0o600)
        saved = json.loads(failed.read_text(encoding="utf-8"))
        self.assertEqual(saved["customer_email"], "ada@example.com")
        self.assertEqual(saved["order_id"], ORDER_ID)

    def test_committed_sender_has_no_customer_contact_list(self):
        emails = set()
        for path in (ROOT / "customer-comms").rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".json", ".md"}:
                continue
            emails.update(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", path.read_text(encoding="utf-8")))
        self.assertLessEqual(emails, ALLOWED_EMAILS)
        self.assertNotIn("CONTACTS = {", SENDER_PATH.read_text(encoding="utf-8"))
        self.assertIn("from resend import _request", SENDER_PATH.read_text(encoding="utf-8"))
        self.assertIn("customer-comms/contacts.json", (ROOT / ".gitignore").read_text(encoding="utf-8"))
        example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(example["contacts"][ORDER_ID]["email"], "ada@example.com")

    def assertLogsStdout(self):
        return _Stdout(self)


class _Stdout:
    def __init__(self, test):
        self.test = test
        self.buffer = None

    def __enter__(self):
        import io
        from contextlib import redirect_stdout
        self.buffer = io.StringIO()
        self._redirect = redirect_stdout(self.buffer)
        self._redirect.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._redirect.__exit__(exc_type, exc, tb)
        return False

    def __contains__(self, item):
        return item in self.buffer.getvalue()


def _book():
    return json.dumps({
        "contacts": {
            ORDER_ID: {
                "email": "ada@example.com",
                "first_name": "Ada",
                "account": "Example Studio",
            }
        },
        "skip_status_quo": [],
    })


def _write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.write(descriptor, text.encode("utf-8"))
    os.close(descriptor)


def _write_state(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _staged(**overrides) -> dict:
    payload = {
        "event": "shipped",
        "order_id": ORDER_ID,
        "company": "Example Studio",
        "customer_name": "Ada Lovelace",
        "shipment_id": "line-1",
        "device": "Phone A",
        "line": "6165550101",
        "carrier": "ups",
        "tracking_number": "1Z999AA10123456784",
        "status": "shipped",
        "status_description": "On the way",
    }
    payload.update(overrides)
    return payload


def _delivered_order(**overrides) -> dict:
    payload = {
        "event": "delivered",
        "order_id": ORDER_ID,
        "company": "Example Studio",
        "shipment_id": "line-2",
        "order_status_url": ORDER_URL,
        "order_lines": [
            {
                "shipment_id": "line-1",
                "device": "Phone A",
                "line": "6165550101",
                "status": "delivered",
                "delivered": True,
                "carrier": "ups",
                "tracking": "1Z999AA10123456784",
                "tracking_url": TRACKING_URL,
            },
            {
                "shipment_id": "line-2",
                "device": "Phone B",
                "line": "6165550102",
                "status": "delivered",
                "delivered": True,
                "carrier": "fedex",
                "tracking": "877000000010",
            },
        ],
    }
    payload.update(overrides)
    return payload


def _shipment() -> Shipment:
    return Shipment(
        shipment_id="line-1",
        order_id=ORDER_ID,
        customer_name="Ada Lovelace",
        customer_email="ada@example.com",
        company="Example Studio",
        line="6165550101",
        device="Phone A",
        carrier="ups",
        tracking_number="1Z999AA10123456784",
        status="shipped",
        lookup_email="buyer@example.com",
        lookup_zip="49503",
    )


def _posts(path: Path) -> list:
    if not path.exists():
        return []
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item["method"] == "POST" and item["path"] == "/emails":
            found.append(item["payload"])
    return found


if __name__ == "__main__":
    unittest.main()
