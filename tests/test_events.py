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
    def test_subscription_snapshot_is_distinct_from_later_user_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            goal = {"threadId": "fixture", "createdAt": 1, "objective": "Continue", "status": "active"}
            paused = dict(goal, status="paused")
            atomic_json(Path(root) / "state.json", {
                "job_id": "fixture", "thread_id": "fixture", "original_goal": goal, "paused_goal": paused})
            worker = Worker(root, None)
            worker.pause_notice_pending = True

            def event(value):
                worker.event({"method": "thread/goal/updated", "params": {
                    "threadId": "fixture", "turnId": None, "goal": value}})

            event(goal)  # Subscription snapshot from before helper's pause.
            event(paused)  # The actual helper pause notification.
            self.assertIsNone(worker.suppressed)
            event(goal)  # A user resume after helper pause must revoke ownership.
            self.assertIsNotNone(worker.suppressed)

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
