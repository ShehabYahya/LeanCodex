# Migration to correlated incremental output

Version 0.2 replaces the original prompt-prefix waiter with durable assignment correlation and an incremental output feed.

## Existing calls

`dsh_send_prompt` still accepts `prompt`, `session_id`/`cwd`, `mode`, and `wait_seconds`. `steer` is now the default; pass `mode="queue"` explicitly when delivery should wait for the next turn. Legacy `wait_seconds > 0` still waits for a terminal assignment state; it is not redefined to return on the first progress message.

New callers may additionally provide a stable `submission_key`. When omitted, the server generates a one-off key for compatibility, which cannot protect a caller that retries after losing the response.

## Incremental flow

1. `dsh_send_prompt(..., submission_key="stable-logical-task-key")`
2. Keep the returned `assignmentId` and `cursor`.
3. `dsh_wait_output(assignment_id=..., after_cursor=..., kind="time_limit", timeout_s=30)`
4. Use the returned cursor for the next read.
5. When `hasMore=true`, more already-available output can be read immediately.

`dsh_wait_output` is read-only. It never sends prompts, heartbeats, steering, cancellation, restarts, or nudges.

`kind` has exactly two values:

- `waiting_for_input` waits for an input/approval request, terminal state, timeout, or unavailable observation.
- `time_limit` waits for a state change, terminal state, timeout, or unavailable observation.

Every response includes `state_change`, `terminal`, `timeout`, and `unavailable`. Ordinary assistant text alone does not wake an explicit wait. The existing assignment-bound signed cursor is reused; text is returned with the next notification so it is not consumed and lost.

## Correctness changes

- Prompt text/prefix matching is removed from response attribution.
- The plugin persists a caller submission-key mapping and one native DSH `requestId` before prompt admission.
- Submission keys are bound to the selected session + payload. Reusing the same key with the same binding resolves to the same assignment; reusing it with another session or payload fails.
- Ambiguous transport settlement is returned as `admission_unknown` with the same assignment/request identity intact. Observation can reconcile it from the native `inbox` projection or the exact durable `user/message.source.rpcId`; a retry still reuses the same native request id.
- Cursor tokens are HMAC signed and bound to one assignment/session.
- Finalized assistant text is included; reasoning, raw tool calls/results, and stream/replay payloads are excluded.
- Provider retries, input waiting, child workflow lifecycle, evidence publication, cancellation/failure/blocking, and terminal completion remain distinct where the durable DSH log provides evidence.
- Session discovery is bounded and continuation cursors fail stale if the ordered roster changes.

## Local state

Correlation state is stored under `${DSH_HOME:-~/.dsh}/.dsh-cli-session/assignments-v1.json` with best-effort directory mode `0700` and file mode `0600`. It stores hashes/identities/cursors, not prompt bodies, reasoning, tool histories, or provider credentials.
