#!/usr/bin/env python3
"""Event-driven /goal waiting. Python 3.10+, Linux; no third-party modules."""

import argparse
import ctypes
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import select
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid

from codex_rpc import Client

TERMINAL = {"resumed", "suppressed", "cancelled", "error", "orphaned"}


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temp.open("w") as output:
        os.chmod(temp, 0o600)
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)


def read_json(path):
    return json.loads(Path(path).read_text())


def proc_identity(pid):
    try:
        # comm may contain spaces and parentheses.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError):
        return None


def same_goal(a, b):
    return bool(a and b) and all(a.get(k) == b.get(k) for k in (
        "threadId", "createdAt", "objective", "tokenBudget"))


def endpoint_default():
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return os.environ.get("GOAL_WAIT_ENDPOINT", "unix://" + str(home / "app-server-control/app-server-control.sock"))


def state_default():
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return os.environ.get("GOAL_WAIT_STATE_DIR", str(root / "goal-wait"))


class CompletionWatch:
    """Register directory watch BEFORE checking an already published result."""
    def __init__(self, path):
        self.path = Path(path)
        libc = ctypes.CDLL(None, use_errno=True)
        self.fd = libc.inotify_init1(os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        mask = 0x80 | 0x8 | 0x400 | 0x800  # MOVED_TO, CLOSE_WRITE, DELETE_SELF, MOVE_SELF
        if libc.inotify_add_watch(self.fd, os.fsencode(self.path.parent), mask) < 0:
            error = ctypes.get_errno()
            os.close(self.fd)
            raise OSError(error, "inotify_add_watch", str(self.path.parent))

    def wait(self):
        try:
            while not self.path.exists():
                events = os.read(self.fd, 65536)
                offset = 0
                while offset < len(events):
                    _, mask, _, size = struct.unpack_from("iIII", events, offset)
                    offset += 16 + size
                    if mask & (0x400 | 0x800 | 0x4000 | 0x8000):
                        raise RuntimeError("Completion watch lost (directory removed/moved or event overflow)")
            return read_json(self.path)
        finally:
            os.close(self.fd)


class Worker:
    def __init__(self, directory, ready_fd):
        self.directory = Path(directory)
        self.state = read_json(self.directory / "state.json")
        self.ready_fd = ready_fd
        self.events = queue.Queue()
        self.client = None
        self.active = set()
        self.pause_notice_pending = False
        self.suppressed = None
        self.suppression = None
        self.reauthorization_deadline = None
        self.superseding_turn = None
        self.result = None
        self.child = None
        self.job_thread = None
        self.notice_turn = None
        self.acknowledged = False
        self.ack_deadline = None

    def save(self, **changes):
        self.state.update(changes, updated_at=time.time())
        atomic_json(self.directory / "state.json", self.state)

    def ready(self, error=None):
        if self.ready_fd is not None:
            descriptor = self.ready_fd
            self.ready_fd = None
            try:
                with os.fdopen(descriptor, "w") as output:
                    json.dump({"ok": error is None, "job_id": self.state["job_id"],
                               "job_dir": str(self.directory), "error": error,
                               "goal_status": self.state.get("paused_goal", {}).get("status")}, output)
                    output.write("\n")
            except BrokenPipeError:
                # A timed-out launcher must not kill a job that already started.
                print("Launcher disconnected; inspect persisted state before retrying", file=sys.stderr)

    @staticmethod
    def goal_summary(goal):
        if not isinstance(goal, dict):
            return None
        return {
            "thread_id": goal.get("threadId"),
            "created_at": goal.get("createdAt"),
            "token_budget": goal.get("tokenBudget"),
            "status": goal.get("status"),
            "updated_at": goal.get("updatedAt"),
        }

    def suppress(self, reason, message=None, observed_goal=None, changed_fields=None):
        if self.suppressed == reason and self.state.get("phase") == "suppressed":
            return
        first_suppression = self.suppressed is None
        if first_suppression:
            params = message.get("params", {}) if isinstance(message, dict) else {}
            original = self.state.get("original_goal")
            paused = self.state.get("paused_goal")
            if changed_fields is None and isinstance(observed_goal, dict) and isinstance(paused, dict):
                changed_fields = [key for key in (
                    "threadId", "createdAt", "objective", "tokenBudget", "status")
                    if observed_goal.get(key) != paused.get(key)]
            self.suppression = {
                "suppressed_at": datetime.now(timezone.utc).isoformat(),
                "reason_code": reason,
                "triggering_method": message.get("method") if isinstance(message, dict) else None,
                "triggering_turn_id": params.get("turnId") or params.get("turn_id"),
                "triggering_client_id": params.get("clientId") or params.get("client_id"),
                "triggering_event_id": params.get("eventId") or params.get("event_id"),
                "original_goal": self.goal_summary(original),
                "paused_goal": self.goal_summary(paused),
                "observed_goal": self.goal_summary(observed_goal),
                "changed_fields": sorted(set(changed_fields or [])),
                "pause_notice_pending": self.pause_notice_pending,
                "notification_attempted": bool(self.state.get("notification_attempted")),
                "notification_queued": bool(self.state.get("notification_queued")),
            }
            suppression_path = self.directory / "suppression.json"
            if not suppression_path.exists():
                atomic_json(suppression_path, self.suppression)
            self.state["suppression_file"] = str(suppression_path)
        # Keep the first trigger record immutable, while status reports the latest
        # reason automatic recovery remains revoked (for example, a later cancel).
        self.suppressed = reason
        self.save(phase="suppressed", wake_suppressed=reason)

    def goal(self):
        return self.client.call("thread/goal/get", {"threadId": self.state["thread_id"]}).get("goal")

    def check_goal(self):
        current = self.goal()
        if not same_goal(current, self.state["paused_goal"]) or current.get("status") != "paused":
            self.suppress("goal_changed", {"method": "thread/goal/get"}, observed_goal=current)
        return not self.suppressed

    def event(self, message):
        method, params = message.get("method"), message.get("params", {})
        if method == "_cancel":
            self.suppress("cancelled_by_user", message)
            if (self.directory / "cancel.json").exists() and read_json(self.directory / "cancel.json").get("terminate"):
                if self.child and self.child.poll() is None:
                    os.killpg(self.child.pid, signal.SIGTERM)
            return
        if method == "_reauthorize":
            if self.suppressed != "new_user_input":
                return
            if (self.directory / "cancel.json").exists() or self.active - {self.superseding_turn}:
                self.suppressed = "reauthorization_rejected"
                self.save(phase="suppressed", wake_suppressed=self.suppressed,
                          reauthorization_error_code="cancelled_or_turn_active")
                return
            current = self.goal()
            if not same_goal(current, self.state.get("paused_goal")) or current.get("status") != "paused":
                self.suppressed = "reauthorization_rejected"
                self.save(phase="suppressed", wake_suppressed=self.suppressed,
                          reauthorization_error_code="goal_changed")
                return
            self.suppressed = None
            self.reauthorization_deadline = None
            self.save(phase="result_ready" if self.result is not None else "waiting",
                      wake_suppressed=None, reauthorized_at=time.time())
            return
        if method == "_disconnected":
            self.suppress("app_server_disconnected", message)
            return
        if method == "_result":
            self.result = params
            atomic_json(self.directory / "result.json", params)
            self.save(result_ready=True, phase="suppressed" if self.suppressed else "result_ready")
            return
        if method == "_child_started":
            self.save(child_pid=params["pid"])
            if (self.directory / "cancel.json").exists():
                self.event({"method": "_cancel"})
            return
        if params.get("threadId") != self.state["thread_id"]:
            return
        if method in ("thread/goal/cleared", "thread/archived", "thread/closed", "thread/deleted"):
            self.suppress(method.replace("thread/", "").replace("/", "_"), message)
        elif method == "thread/goal/updated":
            goal = params.get("goal")
            if (self.pause_notice_pending and same_goal(goal, self.state["paused_goal"])
                    and goal.get("status") == "paused"):
                self.pause_notice_pending = False
            elif (self.pause_notice_pending and same_goal(goal, self.state["original_goal"])
                  and goal.get("status") == "active"):
                # 0.163 emits a subscription snapshot on thread/resume. It may be
                # received after the next RPC; it precedes our own pause event.
                return
            elif not same_goal(goal, self.state["paused_goal"]) or goal.get("status") != "paused":
                self.suppress("goal_modified", message, observed_goal=goal)
            # Once our pause is confirmed, identical paused goal payloads are
            # idempotent snapshots. The protocol has no source/revision field
            # that can distinguish these from an explicit no-op pause.
        elif method == "turn/started":
            self.active.add(params["turn"]["id"])
        elif method in ("item/started", "item/completed"):
            item = params.get("item", {})
            if item.get("type") == "userMessage":
                if item.get("clientId") == self.state["event_id"]:
                    self.notice_turn = params["turnId"]
                    self.save(notification_turn=self.notice_turn)
                else:
                    self.superseding_turn = params.get("turnId")
                    diagnostic_message = {"method": method, "params": {
                        **params, "clientId": item.get("clientId")}}
                    self.suppress("new_user_input", diagnostic_message)
        elif method == "turn/completed":
            turn = params["turn"]
            self.active.discard(turn["id"])
            if turn["id"] == self.notice_turn:
                if turn.get("status") == "completed":
                    self.acknowledged = True
                else:
                    diagnostic_message = {"method": method, "params": {
                        **params, "turn_id": turn.get("id")}}
                    self.suppress("notification_turn_failed", diagnostic_message)

    def drain(self):
        while True:
            try:
                self.event(self.events.get_nowait())
            except queue.Empty:
                break
        if (self.directory / "cancel.json").exists():
            self.event({"method": "_cancel"})

    def run_job(self, watch):
        started = time.time()
        result = {"job_id": self.state["job_id"], "thread_id": self.state["thread_id"],
                  "started_at": started, "exit_code": None}
        try:
            if watch:
                payload = watch.wait()
                if not isinstance(payload, dict) or payload.get("job_id") != self.state["job_id"]:
                    raise ValueError("Completion file job_id does not match this job")
                if type(payload.get("exit_code")) is not int:
                    raise ValueError("Completion file must contain integer exit_code")
                result.update(exit_code=payload["exit_code"], completion=payload)
            else:
                with (self.directory / "stdout.log").open("ab") as out, (self.directory / "stderr.log").open("ab") as err:
                    self.child = subprocess.Popen(self.state["command"], cwd=self.state["cwd"],
                                                  stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                                  start_new_session=True)
                    self.events.put({"method": "_child_started", "params": {"pid": self.child.pid}})
                    result["exit_code"] = self.child.wait()
        except Exception as error:
            result["error"] = str(error)
        result.update(finished_at=time.time(), stdout=str(self.directory / "stdout.log"),
                      stderr=str(self.directory / "stderr.log"))
        self.events.put({"method": "_result", "params": result})

    def run(self):
        os.umask(0o077)
        self.save(helper_pid=os.getpid(), helper_identity=proc_identity(os.getpid()))
        signal.signal(signal.SIGUSR1, lambda *_: self.events.put({"method": "_cancel"}))
        signal.signal(signal.SIGUSR2, lambda *_: self.events.put({"method": "_reauthorize"}))
        # Never kill the user's daemon. Keep this one subscription until delivery.
        self.client = Client(self.state["endpoint"], self.events)
        thread_id = self.state["thread_id"]
        metadata = self.client.call("thread/read", {"threadId": thread_id})["thread"]
        if metadata["status"]["type"] in ("notLoaded", "systemError"):
            raise RuntimeError("Thread must already be loaded on this app-server")
        original = self.goal()
        if not original or original["status"] != "active":
            raise RuntimeError("An existing active goal is required; no goal was created or resumed")
        attached = self.client.call("thread/resume", {"threadId": thread_id})["thread"]
        self.active = {t["id"] for t in attached.get("turns", []) if t["status"] == "inProgress"}
        watch = CompletionWatch(self.state["completion_file"]) if self.state.get("completion_file") else None
        # Preflight has finished. Any new user input/mutation from here revokes ownership.
        self.drain_pre_pause()
        current = self.goal()
        if not same_goal(current, original) or current["status"] != "active":
            raise RuntimeError("Goal changed during preflight")
        paused = self.client.call("thread/goal/set", {"threadId": thread_id, "status": "paused", "origin": "automatic"})["goal"]
        if not same_goal(original, paused) or paused["status"] != "paused":
            raise RuntimeError("Could not establish ownership of the paused goal")
        self.pause_notice_pending = True
        self.save(phase="waiting", original_goal=original, paused_goal=paused)
        self.job_thread = threading.Thread(target=self.run_job, args=(watch,), daemon=True)
        self.job_thread.start()
        self.ready()
        while True:
            self.drain()
            if self.result is not None:
                if self.suppressed:
                    if self.suppressed != "new_user_input":
                        return
                    if self.reauthorization_deadline is None:
                        self.reauthorization_deadline = time.monotonic() + self.state["ack_timeout_seconds"]
                    remaining = self.reauthorization_deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    try:
                        self.event(self.events.get(timeout=remaining))
                    except queue.Empty:
                        return
                    continue
                if not self.active and not self.state.get("notification_queued"):
                    if not self.check_goal():
                        continue
                    self.drain()
                    if self.suppressed or self.active:
                        continue
                    # Persist intent first. On an uncertain RPC result, never blindly retry.
                    self.save(phase="notifying", notification_attempted=True)
                    text = (f"GOAL_WAIT_DONE {self.state['event_id']}\n"
                            f"Job {self.state['job_id']} finished (exit_code={self.result.get('exit_code')}). "
                            f"Private result: {self.directory / 'result.json'}\n"
                            "This is the completion ACK turn. Reply GOAL_WAIT_ACK and end this turn. "
                            "Do not change the goal or run follow-up work in this ACK turn. "
                            "The goal-wait helper will restore this same paused goal after turn/completed. "
                            "In the subsequent goal turn, read the result/logs, verify outputs, then continue.\n"
                            f"Saved continuation: {self.state['continuation']}")
                    self.client.call("thread/queue/add", {"threadId": thread_id,
                                     "clientUserMessageId": self.state["event_id"],
                                     "input": [{"type": "text", "text": text}]})
                    self.save(notification_queued=True)
                    self.ack_deadline = time.monotonic() + self.state["ack_timeout_seconds"]
                if self.acknowledged and not self.active:
                    if not self.check_goal():
                        continue
                    self.drain()
                    if self.suppressed or self.active:
                        continue
                    self.save(phase="resuming", resume_attempted=True)
                    resumed = self.client.call("thread/goal/set", {
                        "threadId": thread_id, "status": "active", "origin": "automatic"})["goal"]
                    if not same_goal(resumed, self.state["paused_goal"]) or resumed["status"] != "active":
                        raise RuntimeError("Unexpected goal after resume")
                    self.save(phase="resumed", resumed_at=time.time())
                    return
            remaining = None if self.ack_deadline is None else self.ack_deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                self.suppress("notification_turn_timeout", {"method": "local_timeout"})
                continue
            try:
                event = self.events.get(timeout=remaining)
            except queue.Empty:
                continue
            self.event(event)

    def drain_pre_pause(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            method, params = event.get("method"), event.get("params", {})
            if method == "_disconnected":
                raise RuntimeError("App-server disconnected during preflight")
            if params.get("threadId") == self.state["thread_id"]:
                if method == "turn/started":
                    self.active.add(params["turn"]["id"])
                elif method == "turn/completed":
                    self.active.discard(params["turn"]["id"])


def start(args):
    if not args.authorize_auto_pause_resume:
        raise ValueError("Requires --authorize-auto-pause-resume backed by explicit user authorization")
    if not math.isfinite(args.estimated_minutes) or args.estimated_minutes <= args.threshold_minutes:
        raise ValueError(f"Estimated remaining runtime must exceed {args.threshold_minutes:g} minutes")
    if not math.isfinite(args.threshold_minutes) or args.threshold_minutes <= 0:
        raise ValueError("Threshold must be finite and positive")
    if not args.thread:
        raise ValueError("Pass --thread UUID or set CODEX_THREAD_ID; never guess --last")
    thread_id = str(uuid.UUID(args.thread))
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if bool(command) == bool(args.completion_file):
        raise ValueError("Supply either a command after -- OR --completion-file")
    if args.ack_timeout_seconds <= 0:
        raise ValueError("ACK timeout must be positive")
    root = Path(args.state_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("State directory must be owned by you and private (chmod 700)")
    job_id = str(uuid.UUID(args.job_id)) if args.job_id else str(uuid.uuid4())
    directory = root / job_id
    directory.mkdir(mode=0o700)
    atomic_json(directory / "state.json", {
        "version": 1, "job_id": job_id, "event_id": str(uuid.uuid4()), "thread_id": thread_id,
        "endpoint": args.endpoint, "phase": "starting", "created_at": time.time(),
        "cwd": str(Path(args.cwd).resolve()), "command": command,
        "completion_file": str(Path(args.completion_file).resolve()) if args.completion_file else None,
        "estimated_minutes": args.estimated_minutes, "threshold_minutes": args.threshold_minutes,
        "continuation": args.continuation, "authorized_auto_pause_resume": True,
        "ack_timeout_seconds": args.ack_timeout_seconds,
    })
    read_fd, write_fd = os.pipe()
    with (directory / "helper.log").open("ab") as log:
        helper = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_worker",
                                   str(directory), str(write_fd)], stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=log, pass_fds=(write_fd,), start_new_session=True)
    os.close(write_fd)
    try:
        if not select.select([read_fd], [], [], 45)[0]:
            raise RuntimeError(f"Helper readiness timed out; inspect {directory}; do not start a duplicate")
        with os.fdopen(read_fd) as pipe:
            read_fd = None
            line = pipe.readline()
        if not line:
            raise RuntimeError(f"Helper exited without readiness; inspect {directory / 'helper.log'}")
        response = json.loads(line)
        response["helper_pid"] = helper.pid
        print(json.dumps(response, ensure_ascii=False))
        return 0 if response["ok"] else 1
    finally:
        if read_fd is not None:
            os.close(read_fd)


def inspect_job(args):
    directory = Path(args.state_dir).expanduser().resolve() / str(uuid.UUID(args.job_id))
    state = read_json(directory / "state.json")
    pid = state.get("helper_pid")
    alive = bool(pid and state.get("helper_identity") == proc_identity(pid))
    if args.action == "cancel":
        atomic_json(directory / "cancel.json", {"at": time.time(), "terminate": args.terminate})
        if alive:
            try:
                os.kill(pid, signal.SIGUSR1)
            except ProcessLookupError:
                pass
    if args.action == "reauthorize":
        if not args.authorize_auto_pause_resume:
            raise ValueError("Reauthorization requires fresh explicit user authorization")
        if not alive:
            raise ValueError("Helper is no longer running; inspect the result and recover manually without restarting the job")
        if state.get("phase") != "suppressed" or state.get("wake_suppressed") != "new_user_input":
            raise ValueError("Only a helper suppressed by new user input can be reauthorized")
        if (directory / "cancel.json").exists():
            raise ValueError("This job was explicitly cancelled and cannot be reauthorized")
        try:
            os.kill(pid, signal.SIGUSR2)
        except ProcessLookupError as error:
            raise ValueError("Helper exited before reauthorization; inspect the result and recover manually") from error
    if args.action == "reconcile" and not alive and state["phase"] not in TERMINAL:
        state.update(phase="orphaned", recovery="Inspect result/logs; no automatic resume after lost ownership")
        atomic_json(directory / "state.json", state)
    result_path = directory / "result.json"
    suppression_path = directory / "suppression.json"
    print(json.dumps({"job_dir": str(directory), "helper_alive": alive, "state": state,
                      "automatic_resume": "suppressed" if state["phase"] == "suppressed" else state["phase"],
                      "suppression": read_json(suppression_path) if suppression_path.exists() else None,
                      "cancel_requested": (directory / "cancel.json").exists(),
                      "result": read_json(result_path) if result_path.exists() else None}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("start", "doctor"):
        item = sub.add_parser(action)
        item.add_argument("--endpoint", default=endpoint_default())
        item.add_argument("--thread", default=os.environ.get("CODEX_THREAD_ID"))
        if action == "doctor":
            continue
        item.add_argument("--state-dir", default=state_default())
        item.add_argument("--estimated-minutes", type=float, required=True)
        item.add_argument("--threshold-minutes", type=float, default=20)
        item.add_argument("--authorize-auto-pause-resume", action="store_true")
        item.add_argument("--continuation", required=True)
        item.add_argument("--cwd", default=os.getcwd())
        item.add_argument("--completion-file")
        item.add_argument("--job-id")
        item.add_argument("--ack-timeout-seconds", type=int, default=600)
        item.add_argument("command", nargs=argparse.REMAINDER)
    for action in ("status", "cancel", "reauthorize", "reconcile"):
        item = sub.add_parser(action)
        item.add_argument("job_id")
        item.add_argument("--state-dir", default=state_default())
        if action == "cancel":
            item.add_argument("--terminate", action="store_true", help="Also SIGTERM the command process group")
        if action == "reauthorize":
            item.add_argument("--authorize-auto-pause-resume", action="store_true")
    worker = sub.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("directory")
    worker.add_argument("ready_fd", type=int)
    args = parser.parse_args()
    if args.action == "_worker":
        instance = Worker(args.directory, args.ready_fd)
        # A process-lifetime lock prevents two helpers from controlling one goal.
        key = hashlib.sha256(instance.state["thread_id"].encode()).hexdigest()
        with (Path(args.directory).parent / (key + ".lock")).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                instance.run()
            except Exception as error:
                instance.save(phase="error", error=str(error))
                instance.ready(str(error))
                # Keep collecting the result even if wake delivery failed. Never rerun it.
                if instance.job_thread is not None:
                    instance.suppress("worker_error", {"method": "_worker_exception"})
                    instance.job_thread.join()
                    instance.drain()
                return 1
            finally:
                if instance.client:
                    instance.client.close()
        return 0
    if args.action == "start":
        return start(args)
    if args.action == "doctor":
        client = Client(args.endpoint)
        try:
            result = {"ok": True, "endpoint": args.endpoint, "server": client.info,
                      "platform": sys.platform, "python": sys.version.split()[0]}
            if args.thread:
                result["thread"] = client.call("thread/read", {"threadId": args.thread})["thread"]["status"]
                result["goal"] = client.call("thread/goal/get", {"threadId": args.thread}).get("goal")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            client.close()
        return 0
    inspect_job(args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, RuntimeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
