# Setup and operation

`goal_wait.py` uses only Python's standard library. It keeps private state under
`${XDG_STATE_HOME:-~/.local/state}/goal-wait`, outside the installed skill. Set
`GOAL_WAIT_STATE_DIR` or pass `--state-dir` to choose another private directory
(owned by you, mode 0700). Commands, continuation text, logs and results can be
sensitive; do not put this directory in a public repository.

The default endpoint is
`unix://${CODEX_HOME:-~/.codex}/app-server-control/app-server-control.sock`.
Override with `GOAL_WAIT_ENDPOINT` or `--endpoint unix:///absolute/path.sock`.
Loopback `ws://127.0.0.1:PORT` endpoints are also supported. This must be the server
already running the original thread. A successful connection to another server
does not prove it owns the loaded thread. `doctor` reads metadata and goal state;
it does not create, pause or resume a goal.

```bash
python3 /installed/goal-wait/scripts/goal_wait.py doctor --thread "$CODEX_THREAD_ID"
```

If no compatible shared server is available, report that limitation and keep the
current goal state. A skill cannot add a new scheduler or a wake API to an
incompatible client. Explicit skill invocation is available after installation;
implicit selection is not a deterministic interceptor of every `/goal` command.

## Standing authorization

When requested, the user can add this to their own global instructions or project
`AGENTS.md` (do not modify it merely because the skill was installed):

> For existing /goal tasks, use goal-wait when the estimated remaining runtime of
> an already authorized unattended external job exceeds 20 minutes and no other
> useful independent work remains. I authorize goal-wait to pause the goal and
> automatically resume the same goal after its registered completion event.
> Respect subsequent stop/cancel instructions and goal or budget changes.

Calling the helper with `--authorize-auto-pause-resume` records that authorization;
it does not grant command execution permissions or bypass the agent's policies.
The helper uses `thread/goal/set` with `origin:automatic`, never impersonating a
user mutation. It omits objective and token budget on state updates.

## Command completion

The helper detaches from the shell, starts the command with stdin disconnected,
captures stdout/stderr separately, and waits with `Popen.wait()`. Do not launch a
command that exits after backgrounding its real work; use its foreground mode or
the completion-file workflow below. Nonzero exit, signal termination and spawn
failure all produce a result and a completion notification.

The helper subscribes with `thread/resume` after verifying the thread is already
loaded, pauses the active goal, and holds that subscription. It retains a result
that arrives before the current turn ends. Only after all observed active turns
finish does it send one `thread/queue/add` notification, with a unique event UUID.
The ACK turn is matched by the user-message `clientId`, rather than assuming the
next arbitrary turn belongs to this job. After its successful `turn/completed`,
the helper rechecks the goal and restores `active`. Codex's goal scheduler starts
the next turn; the helper does not send an extra continue prompt.

Version 0.163.0 sends a current-goal snapshot on subscription. The helper treats
the original active snapshot before its own pause notification as initial state;
the same mutation after that boundary revokes ownership.

There is no model polling and no periodic goal-get loop. State reads occur at
setup, notification delivery and resume. A 600-second timeout bounds a missing
ACK; it never invokes the model again. Adjust using `--ack-timeout-seconds`.

## External completion files (Linux inotify)

Use this mode when a scheduler/other process owns the task. It waits locally;
network filesystems and remote task lifecycles require an adapter on the task
host. Supply a fresh UUID so the producer and listener agree on job identity:

```bash
python3 /installed/goal-wait/scripts/goal_wait.py start \
  --thread "$CODEX_THREAD_ID" --job-id 11111111-1111-4111-8111-111111111111 \
  --estimated-minutes 45 --authorize-auto-pause-resume \
  --completion-file /project/run-42/done.json \
  --continuation 'Read result/logs and verify the outputs before continuing.'
```

The producer must publish a complete JSON file using a temporary file and atomic
rename in the same directory. Publish on both success and failure:

```json
{"job_id":"11111111-1111-4111-8111-111111111111","exit_code":0,"artifact":"output.json"}
```

Registering the inotify watch before checking the path handles preexisting and
early completion. A mismatched ID, malformed result, or lost directory watch
creates an error result to investigate rather than claiming success. This mode
does not monitor or kill the external producer. A missing completion event waits
indefinitely until the user intervenes; use a reliable scheduler/producer contract.

## Status, cancellation and recovery

```bash
python3 /installed/goal-wait/scripts/goal_wait.py status JOB_UUID
python3 /installed/goal-wait/scripts/goal_wait.py cancel JOB_UUID
python3 /installed/goal-wait/scripts/goal_wait.py cancel JOB_UUID --terminate
python3 /installed/goal-wait/scripts/goal_wait.py reconcile JOB_UUID
```

`cancel` writes a persistent cancellation marker and signals the helper. It
revokes automatic wake/resume; the original goal stays paused. Without
`--terminate`, the job continues and its result is still collected. With it, a
helper-owned running command group gets SIGTERM; this is not a forced-kill
escalation. File-watch jobs do not own the producer. `reconcile` reports existing
state and marks a nonterminal job orphaned when its recorded helper is gone; it
does not rerun a command, resend an uncertain notification, or resume a goal.

On failure/disconnect: inspect state/result/logs, establish whether the command
still runs, check the original goal, then ask for or follow a fresh user resume
instruction. Do not blindly start another copy. A pending message may already
have been delivered even if the RPC timed out.

## Support limits

- Supports one helper-controlled job per thread and a continuously running local
  app-server/helper. Persistent subscriptions address the server's documented
  **30-minute** no-subscriber inactivity unload grace period; that server rule is
  separate from this skill's **20-minute** decision threshold.
- New user input, clear/archive/close, changed objective/creation time/budget,
  observed goal state changes and explicit goal mutations suppress automatic
  resume. Usage counters may change during the final original turn and ACK turn.
- The current API exposes no conditional compare-and-set goal update or durable
  ownership lease. Event checks minimize races, but cannot guarantee detection
  of every simultaneous or no-op UI pause. Use `cancel JOB_UUID` for unambiguous
  revocation. Do not advertise stronger cancellation guarantees than this.
- v1 sends at most one notification attempt. UUID correlation prevents confusing
  another user turn with its ACK. Crash recovery and exactly-once delivery across
  Codex and the helper are not claimed. Results survive ordinary delivery errors;
  SIGKILL, server restart, logout, or reboot require manual reconciliation.
- Tests cover short event-driven runs, failures and races. A real model previously
  completed the notification/resume chain; a >=20-minute soak has not been run.

Protocol reference: [official Codex app-server documentation](https://learn.chatgpt.com/docs/app-server).
