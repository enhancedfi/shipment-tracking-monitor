"""Private store files, the email handoff lifetime, and the store lock."""

import errno
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from shipment_monitor.cli import main as cli_main
from shipment_monitor.email_trigger import EmailTrigger, write_email_state
from shipment_monitor.models import Shipment
from shipment_monitor.store import (
    LOCK_TIMEOUT_SECONDS,
    PRIVATE_MODE,
    Store,
    StoreLockTimeout,
    exclusive_store_lock,
    lockfile_for,
    write_private_text,
)

ROOT = Path(__file__).resolve().parents[1]


class PrivateFileTests(unittest.TestCase):
    def test_store_email_state_and_lock_are_created_at_0600_without_chmod(self):
        self.assertEqual(PRIVATE_MODE, 0o600)
        previous = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store_path = root / "shipments.json"
                state_path = root / "shipments.json.email.json"
                def forbid(*_args, **_kwargs):
                    raise AssertionError("chmod after create")

                with patch("shipment_monitor.store.os.chmod", forbid), patch(
                    "shipment_monitor.store.os.fchmod", forbid
                ):
                    store = Store(store_path)
                    store.shipments = [_shipment()]
                    store.save()
                    write_email_state(state_path, {"customer_email": "riley@example.com", "order_id": "99-sample"})
                    with exclusive_store_lock(store_path, timeout=1):
                        pass
                self.assertEqual(_mode(store_path), 0o600)
                self.assertEqual(_mode(state_path), 0o600)
                self.assertEqual(_mode(lockfile_for(store_path)), 0o600)
                saved = json.loads(store_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["shipments"][0]["customer_email"], "riley@example.com")
        finally:
            os.umask(previous)


class EmailStateLifetimeTests(unittest.TestCase):
    def test_successful_send_deletes_the_state_file(self):
        previous = os.umask(0o022)
        os.environ.pop("SHIPMENT_EMAIL_STATE", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "shipments.json.email.json"
                script = root / "send-shipment-updates.py"
                script.write_text("raise SystemExit(0)\n", encoding="utf-8")
                seen = {}

                def runner(_argv):
                    seen["mode"] = _mode(state)
                    seen["payload"] = json.loads(state.read_text(encoding="utf-8"))
                    return 0

                email = EmailTrigger(script, mode="execute", runner=runner, state_path=state)
                result = email.send(_shipment(), "shipped", "On the way", "2026-10-13T12:00:00+00:00")
                self.assertTrue(result.ok)
                self.assertEqual(result.reason, "sent")
                self.assertEqual(seen["mode"], 0o600)
                self.assertEqual(seen["payload"]["customer_email"], "riley@example.com")
                self.assertFalse(state.exists())
        finally:
            os.umask(previous)

    def test_failed_send_keeps_the_state_file_at_0600_for_retry(self):
        previous = os.umask(0o022)
        os.environ.pop("SHIPMENT_EMAIL_STATE", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "shipments.json.email.json"
                script = root / "send-shipment-updates.py"
                script.write_text("raise SystemExit(1)\n", encoding="utf-8")

                def runner(_argv):
                    return 1

                email = EmailTrigger(script, mode="execute", runner=runner, state_path=state)
                result = email.send(_shipment(), "shipped", "On the way", "2026-10-13T12:00:00+00:00")
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, "sender_exit_1")
                self.assertTrue(state.is_file())
                self.assertEqual(_mode(state), 0o600)
                kept = json.loads(state.read_text(encoding="utf-8"))
                self.assertEqual(kept["customer_email"], "riley@example.com")
                self.assertEqual(kept["order_id"], "99-sample")
                self.assertIn("body", kept)

                def succeed(_argv):
                    retry = json.loads(state.read_text(encoding="utf-8"))
                    self.assertEqual(retry["customer_email"], kept["customer_email"])
                    self.assertEqual(retry["order_id"], kept["order_id"])
                    return 0

                email.runner = succeed
                second = email.send(_shipment(), "shipped", "On the way", "2026-10-13T12:00:00+00:00")
                self.assertTrue(second.ok)
                self.assertFalse(state.exists())
        finally:
            os.umask(previous)

    def test_sender_exception_keeps_the_state_file(self):
        os.environ.pop("SHIPMENT_EMAIL_STATE", None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "shipments.json.email.json"
            script = root / "send-shipment-updates.py"
            script.write_text("raise SystemExit(0)\n", encoding="utf-8")

            def runner(_argv):
                raise RuntimeError("sender down")

            email = EmailTrigger(script, mode="execute", runner=runner, state_path=state)
            with self.assertRaises(RuntimeError):
                email.send(_shipment(), "delayed", "Weather delay", "2026-10-13T12:00:00+00:00")
            self.assertTrue(state.is_file())
            self.assertEqual(_mode(state), 0o600)
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["customer_email"], "riley@example.com")
            self.assertNotIn("SHIPMENT_EMAIL_STATE", os.environ)


class StoreLockTests(unittest.TestCase):
    def test_timeout_is_about_twenty_minutes_and_skips_with_a_log(self):
        self.assertEqual(LOCK_TIMEOUT_SECONDS, 20 * 60)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "shipments.json"
            marker = root / "held"
            holder = subprocess.Popen(
                [sys.executable, "-c", _HOLDER, str(ROOT), str(store), str(marker)],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            failed = None
            child = None
            try:
                deadline = time.monotonic() + 5
                while not marker.exists():
                    if holder.poll() is not None:
                        failed = "holder exited before the lock was held"
                        break
                    if time.monotonic() > deadline:
                        failed = "holder did not take the lock"
                        break
                    time.sleep(0.02)
                if failed is None:
                    env = os.environ.copy()
                    env["SHIPMENT_STORE_LOCK_HELD"] = "1"
                    child = subprocess.run(
                        [sys.executable, "-c", _WAITER, str(ROOT), str(store)],
                        cwd=ROOT,
                        check=False,
                        capture_output=True,
                        text=True,
                        env=env,
                    )
            finally:
                out, err = holder.communicate(timeout=10)
            if failed is not None or child is None:
                self.fail(f"{failed} code={holder.returncode}\n{out}\n{err}")
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertIn("timed_out", child.stdout)
            self.assertNotIn("acquired", child.stdout)
            self.assertIn("ALERT", child.stderr)
            self.assertIn("skipping run; lock still held:", child.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "held")

    def test_cli_skips_when_the_lock_times_out(self):
        @contextmanager
        def locked_out(_path, timeout=LOCK_TIMEOUT_SECONDS):
            raise StoreLockTimeout("skipping run; lock still held: test")
            yield

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "shipments.json"
            incoming = Path(tmp) / "incoming.json"
            incoming.write_text("[]", encoding="utf-8")
            stderr = io.StringIO()
            with patch("shipment_monitor.cli.exclusive_store_lock", locked_out), patch("sys.stderr", stderr):
                code = cli_main(["import-shipments", "--store", str(store), "--input", str(incoming)])
            self.assertEqual(code, 1)
            self.assertIn("ALERT", stderr.getvalue())
            self.assertFalse(store.exists())

    def test_in_process_reentry_does_not_deadlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "shipments.json"
            with exclusive_store_lock(store, timeout=0.5):
                with exclusive_store_lock(store, timeout=0.2):
                    write_private_text(store, "{}\n")
            self.assertEqual(_mode(store), 0o600)

    def test_lock_open_refuses_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "secret"
            secret.write_text("keep", encoding="utf-8")
            lock = lockfile_for(root / "shipments.json")
            lock.symlink_to(secret)
            with self.assertRaises(OSError) as caught:
                with exclusive_store_lock(root / "shipments.json", timeout=0.2):
                    pass
            self.assertEqual(caught.exception.errno, errno.ELOOP)
            self.assertEqual(secret.read_text(encoding="utf-8"), "keep")
            self.assertTrue(lock.is_symlink())


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _shipment() -> Shipment:
    return Shipment(
        shipment_id="sample-1",
        order_id="99-sample",
        customer_name="Riley Chen",
        customer_email="riley@example.com",
        company="Example Studio",
        line="6165550101",
        device="iPhone 17",
        carrier="ups",
        tracking_number="1Z999AA10123456784",
    )


_HOLDER = """
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from shipment_monitor.store import exclusive_store_lock

store = Path(sys.argv[2])
marker = Path(sys.argv[3])
with exclusive_store_lock(store, timeout=5):
    marker.write_text("held", encoding="utf-8")
    time.sleep(2)
"""

_WAITER = """
import os
import sys
from pathlib import Path

os.environ["SHIPMENT_STORE_LOCK_HELD"] = "1"
sys.path.insert(0, sys.argv[1])
from shipment_monitor.store import StoreLockTimeout, exclusive_store_lock

try:
    with exclusive_store_lock(Path(sys.argv[2]), timeout=0.4):
        print("acquired", flush=True)
except StoreLockTimeout as exc:
    print("timed_out", flush=True)
    print(exc, file=sys.stderr)
"""


if __name__ == "__main__":
    unittest.main()
