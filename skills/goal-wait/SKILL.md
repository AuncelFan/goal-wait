---
name: goal-wait
description: Pause an authorized Codex /goal while an external job is estimated to need more than 20 minutes, then use process-exit or inotify events to notify the same thread and resume the original goal without model polling.
---

# Goal Wait

Use when an existing `/goal` has an authorized unattended external job with **estimated remaining runtime >20 minutes**, and other useful independent work is finished. The threshold is configurable; 20 minutes is the default. Do not create a goal just to use this skill.

Automatic pause/resume needs explicit user authorization in the conversation or applicable instructions. Installing this skill alone is not authorization. An explicit request to use `$goal-wait` to pause and automatically resume a goal supplies it. Respect later stop, cancellation, goal changes, and existing execution permissions; the helper runs commands with the current OS user's access.

Read [the workflow](references/workflow.md) for setup, external completion files, cancellation, or recovery. Requires Linux/WSL, Python 3.10+, and the **same running Codex app-server** that owns the thread. The tested protocol versions are 0.162.1 and 0.163.0.

1. Resolve `scripts/goal_wait.py` relative to this skill's installed folder. Get the exact thread UUID from `CODEX_THREAD_ID` or trusted session metadata. Never select the last thread or a title. Run `doctor --thread UUID` to verify the endpoint and existing active goal. Use `--endpoint` if automatic socket discovery is wrong; do not start a separate server for the original thread.
2. Save concrete continuation steps, expected outputs, and success checks in `--continuation`. Launch the authorized command with an argument array after `--`; use `bash -lc '...'` only when the command actually needs shell syntax.

   ```bash
   python3 /installed/goal-wait/scripts/goal_wait.py start \
     --thread "$CODEX_THREAD_ID" --estimated-minutes 45 \
     --authorize-auto-pause-resume \
     --continuation 'Read exit code/logs; validate output/report.json; continue the original goal.' \
     --cwd /path/to/project -- python3 long_job.py
   ```

3. A successful start returns `ok:true`, the job ID/directory, and `goal_status:paused`. The detached helper subscribes and pauses the original goal itself using `origin:automatic`. **End the current turn immediately.** Include the job ID and result location in the final reply. Do not pause again, loop on status, use timed model turns, or keep calling wait tools. A failed/uncertain start requires inspecting its state before retrying; never launch a duplicate command.
4. On `GOAL_WAIT_DONE`, this is the **notification ACK turn**. Reply `GOAL_WAIT_ACK` and end the turn. The helper waits for that specific turn to finish, checks ownership, then restores the same goal to active. Do not change goal state during this ACK turn.
5. In the subsequent automatic goal turn, read the persisted `result.json` and logs, check the actual exit code and artifacts, then continue the original work. A notification also fires on failure; it does not mean the job succeeded.

The helper blocks on ordinary OS events while the model is idle. `status` is for user-requested diagnosis, not periodic checks. `cancel JOB_ID` revokes this helper's automatic wake; `cancel JOB_ID --terminate` also sends SIGTERM to its command process group. Cancel before manually changing the goal when possible. On disconnect, lost helper, or ambiguous state, preserve the result and use the manual recovery procedure; v1 does not automatically restore ownership across restarts.
