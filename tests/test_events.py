"""Exercise actual blocking OS events and the dependency-free wire transport."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/goal-wait/scripts"))
from codex_rpc import WebSocket
from goal_wait import CompletionWatch, Worker, atomic_json


class EventsTest(unittest.TestCase):
    def test_repeated_paused_snapshots_are_idempotent_until_goal_really_changes(self):
        with tempfile.TemporaryDirectory() as root:
            goal = {"threadId": "fixture", "createdAt": 1, "objective": "Continue",
                    "tokenBudget": 500, "status": "active", "tokensUsed": 10,
                    "timeUsedSeconds": 4, "updatedAt": 1}
            paused = dict(goal, status="paused", updatedAt=2)
            atomic_json(Path(root) / "state.json", {
                "job_id": "fixture", "thread_id": "fixture", "original_goal": goal, "paused_goal": paused})
            worker = Worker(root, None)
            worker.pause_notice_pending = True

            def event(value, turn_id=None):
                worker.event({"method": "thread/goal/updated", "params": {
                    "threadId": "fixture", "turnId": turn_id, "goal": value}})

            event(goal, "subscription-turn")  # Snapshot from before helper's pause.
            event(paused, "pause-turn")  # The actual helper pause notification.
            self.assertIsNone(worker.suppressed)
            self.assertFalse(worker.pause_notice_pending)

            # Re-subscription and accounting updates can emit the same paused goal
            # with turnId=null; neither proves that a user revoked ownership.
            snapshot = dict(paused, tokensUsed=15, timeUsedSeconds=7)
            event(snapshot)
            event(dict(snapshot))
            self.assertIsNone(worker.suppressed)

            event(dict(paused, objective="A different goal"))
            self.assertIsNotNone(worker.suppressed)
            record = json.loads((Path(root) / "suppression.json").read_text())
            self.assertEqual(record["reason_code"], "goal_modified")
            self.assertEqual(record["triggering_method"], "thread/goal/updated")
            self.assertEqual(record["triggering_turn_id"], None)
            self.assertEqual(record["changed_fields"], ["objective"])
            self.assertNotIn("objective", record["observed_goal"])
            self.assertEqual(record["notification_attempted"], False)

            # Later result persistence must not overwrite first suppression evidence.
            first_record = (Path(root) / "suppression.json").read_text()
            worker.event({"method": "_result", "params": {"exit_code": 0}})
            self.assertEqual((Path(root) / "suppression.json").read_text(), first_record)
            self.assertEqual(json.loads((Path(root) / "state.json").read_text())["phase"], "suppressed")

    def test_explicit_cancel_has_a_diagnostic_record(self):
        with tempfile.TemporaryDirectory() as root:
            atomic_json(Path(root) / "state.json", {
                "job_id": "fixture", "thread_id": "fixture",
                "event_id": "wake-client",
                "original_goal": {"threadId": "fixture", "createdAt": 1,
                                  "objective": "Continue", "status": "active"},
                "paused_goal": {"threadId": "fixture", "createdAt": 1,
                                "objective": "Continue", "status": "paused"}})
            worker = Worker(root, None)
            worker.event({"method": "_cancel", "params": {"clientId": "test-client"}})
            record = json.loads((Path(root) / "suppression.json").read_text())
            self.assertEqual(record["reason_code"], "cancelled_by_user")
            self.assertEqual(record["triggering_method"], "_cancel")
            self.assertEqual(record["triggering_client_id"], "test-client")
            self.assertFalse(record["notification_queued"])

    def test_later_cancel_updates_status_but_preserves_first_trigger(self):
        with tempfile.TemporaryDirectory() as root:
            atomic_json(Path(root) / "state.json", {
                "job_id": "fixture", "thread_id": "fixture",
                "event_id": "wake-client",
                "original_goal": {"threadId": "fixture", "createdAt": 1,
                                  "objective": "Continue", "status": "active"},
                "paused_goal": {"threadId": "fixture", "createdAt": 1,
                                "objective": "Continue", "status": "paused"}})
            worker = Worker(root, None)
            worker.event({"method": "item/started", "params": {
                "threadId": "fixture", "turnId": "progress-turn",
                "item": {"type": "userMessage", "clientId": "progress-client"}}})
            first_record = (Path(root) / "suppression.json").read_text()
            worker.event({"method": "_cancel"})
            record = json.loads((Path(root) / "suppression.json").read_text())
            state = json.loads((Path(root) / "state.json").read_text())
            self.assertEqual(record["reason_code"], "new_user_input")
            self.assertEqual(state["wake_suppressed"], "cancelled_by_user")
            self.assertEqual((Path(root) / "suppression.json").read_text(), first_record)

    def test_same_value_pause_payload_is_indistinguishable_and_not_inferred_as_cancel(self):
        with tempfile.TemporaryDirectory() as root:
            paused = {"threadId": "fixture", "createdAt": 1, "objective": "Continue",
                      "tokenBudget": None, "status": "paused", "updatedAt": 2}
            atomic_json(Path(root) / "state.json", {
                "job_id": "fixture", "thread_id": "fixture", "paused_goal": paused,
                "original_goal": dict(paused, status="active")})
            worker = Worker(root, None)
            worker.pause_notice_pending = True
            worker.event({"method": "thread/goal/updated", "params": {
                "threadId": "fixture", "turnId": None, "goal": dict(paused)}})
            worker.event({"method": "thread/goal/updated", "params": {
                "threadId": "fixture", "turnId": None, "goal": dict(paused)}})
            self.assertIsNone(worker.suppressed)
            self.assertFalse((Path(root) / "suppression.json").exists())

    def test_identity_budget_and_status_changes_revoke_old_waiter(self):
        base = {"threadId": "fixture", "createdAt": 1, "objective": "Continue",
                "tokenBudget": 500, "status": "paused", "updatedAt": 2}
        changes = {
            "createdAt": dict(base, createdAt=2),
            "objective": dict(base, objective="Different objective"),
            "tokenBudget": dict(base, tokenBudget=900),
            "status": dict(base, status="active"),
        }
        for changed_field, observed in changes.items():
            with self.subTest(changed_field=changed_field), tempfile.TemporaryDirectory() as root:
                atomic_json(Path(root) / "state.json", {
                    "job_id": "fixture", "thread_id": "fixture",
                    "original_goal": dict(base, status="active"), "paused_goal": base})
                worker = Worker(root, None)
                worker.event({"method": "thread/goal/updated", "params": {
                    "threadId": "fixture", "turnId": None, "goal": observed}})
                self.assertEqual(worker.suppressed, "goal_modified")
                record = json.loads((Path(root) / "suppression.json").read_text())
                self.assertEqual(record["changed_fields"], [changed_field])

    def test_clear_archive_and_close_events_revoke_old_waiter(self):
        for method in ("thread/goal/cleared", "thread/archived", "thread/closed", "thread/deleted"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as root:
                atomic_json(Path(root) / "state.json", {
                    "job_id": "fixture", "thread_id": "fixture"})
                worker = Worker(root, None)
                worker.event({"method": method, "params": {"threadId": "fixture"}})
                record = json.loads((Path(root) / "suppression.json").read_text())
                self.assertEqual(record["reason_code"], method.replace("thread/", "").replace("/", "_"))

    def test_launcher_disconnect_does_not_abort_detached_worker(self):
        with tempfile.TemporaryDirectory() as root:
            atomic_json(Path(root) / "state.json", {"job_id": "fixture"})
            read_fd, write_fd = os.pipe()
            os.close(read_fd)
            worker = Worker(root, write_fd)
            worker.ready()  # Parent timed out/closed; collection must continue.
            self.assertIsNone(worker.ready_fd)

    def test_no_authorization_or_short_estimate_never_launches(self):
        script = Path(__file__).resolve().parents[1] / "skills/goal-wait/scripts/goal_wait.py"
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "must-not-exist"
            base = [sys.executable, str(script), "start", "--state-dir", str(state),
                    "--thread", "11111111-1111-4111-8111-111111111111", "--continuation", "Continue"]
            for flags in (["--estimated-minutes", "45"],
                          ["--authorize-auto-pause-resume", "--estimated-minutes", "20"],
                          ["--authorize-auto-pause-resume", "--estimated-minutes", "nan"]):
                with self.subTest(flags=flags):
                    result = subprocess.run(base + flags + ["--", "true"], text=True, capture_output=True)
                    self.assertEqual(result.returncode, 1)
                    self.assertFalse(state.exists())

    def test_preexisting_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "done.json"
            atomic_json(path, {"exit_code": 7})
            self.assertEqual(CompletionWatch(path).wait(), {"exit_code": 7})

    def test_atomic_publication_after_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "done.json"
            watch = CompletionWatch(path)
            ready = threading.Event()
            result = []

            def read():
                ready.set()
                result.append(watch.wait())

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            self.assertTrue(ready.wait(2))
            # An unrelated event must not be treated as this job's completion.
            atomic_json(Path(directory) / "other.json", {"exit_code": 999})
            self.assertEqual(result, [])
            atomic_json(path, {"exit_code": 0})
            reader.join(2)
            self.assertFalse(reader.is_alive(), "inotify missed an atomic rename")
            self.assertEqual(result, [{"exit_code": 0}])

    def test_lost_watch_fails_instead_of_claiming_success(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "run"
            directory.mkdir()
            watch = CompletionWatch(directory / "done.json")
            directory.rmdir()
            with self.assertRaisesRegex(RuntimeError, "watch lost"):
                watch.wait()

    def test_unix_websocket_mask_ping_and_fragmentation(self):
        with tempfile.TemporaryDirectory() as root:
            endpoint = str(Path(root) / "server.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(endpoint)
            server.listen(1)
            observed, errors = [], []

            def serve():
                try:
                    conn, _ = server.accept()
                    with conn, conn.makefile("rb") as stream:
                        headers = b""
                        while not headers.endswith(b"\r\n\r\n"):
                            headers += stream.read(1)
                        key = next(x.split(b":", 1)[1].strip() for x in headers.split(b"\r\n") if x.lower().startswith(b"sec-websocket-key:"))
                        accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")

                        def frame():
                            op, size = stream.read(2)
                            self.assertTrue(size & 128, "Client frame must be masked")
                            size &= 127
                            if size == 126:
                                size = struct.unpack("!H", stream.read(2))[0]
                            elif size == 127:
                                size = struct.unpack("!Q", stream.read(8))[0]
                            mask = stream.read(4)
                            return op & 15, bytes(v ^ mask[i % 4] for i, v in enumerate(stream.read(size)))

                        observed.append(frame())
                        # Ping between two text fragments; no compression extensions.
                        conn.sendall(b'\x01\x04{"ok' + b'\x89\x01!' + b'\x80\x07":true}')
                        observed.append(frame())
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            client = WebSocket("unix://" + endpoint)
            try:
                payload = "x" * 70000  # Exercise the 64-bit payload length path.
                client.send(payload)
                self.assertEqual(json.loads(client.receive_text()), {"ok": True})
            finally:
                client.close()
                thread.join(3)
                server.close()
            self.assertFalse(thread.is_alive())
            if errors:
                raise errors[0]
            self.assertEqual(observed, [(1, payload.encode()), (10, b"!")])


if __name__ == "__main__":
    unittest.main()
