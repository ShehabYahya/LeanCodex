# Migration to output-only supervision

Version 0.2 replaces the original prompt-prefix waiter with durable assignment correlation and an incremental output feed.

## Existing calls

`dsh_send_prompt` still accepts `prompt`, `session_id`/`cwd`, `mode`, and `wait_seconds`. `queue` remains the default. Legacy `wait_seconds > 0` still waits for a terminal assignment state; it is not redefined to return on the first progress message.

New callers should additionally provide a stable `submission_key`. When omitted, the server generates a one-off key for compatibility, which cannot protect a caller that retries after losing the response.

## New normal flow

1. `dsh_send_prompt(..., submission_key="stable-logical-task-key")`
2. Persist the returned `assignmentId` and `cursor`.
3. `dsh_wait_output(assignment_id=..., after_cursor=...)`
4. Replace the saved cursor with the returned cursor and repeat.
5. When `hasMore=true`, call again immediately. Otherwise wait again unless a question/blocker/terminal result needs a decision.

`dsh_wait_output` is read-only. It never sends prompts, heartbeats, steering, cancellation, restarts, or nudges.

## Correctness changes

- Prompt text/prefix matching is removed from response attribution.
- The plugin persists a caller submission-key mapping and one native DSH `requestId` before prompt admission.
- Submission keys are globally bound to the selected session + payload. Reusing the same key with the same binding reconciles the same assignment; reusing it with another session or payload fails.
- Ambiguous transport settlement is returned as `admission_unknown` with the same assignment/request identity intact. Observation can reconcile it from the native `inbox` projection or the exact durable `user/message.source.rpcId`; a retry still reuses the same native request id.
- Cursor tokens are HMAC signed and bound to one assignment/session.
- Finalized assistant text is allowed through; reasoning, raw tool calls/results, and stream/replay payloads are excluded.
- Provider retries, waiting-for-input, child workflow lifecycle, evidence publication, cancellation/failure/blocking, and terminal completion remain distinct where the durable DSH log provides evidence.
- Session discovery is bounded and continuation cursors fail stale if the ordered roster changes.

## Local state

Correlation state is stored under `${DSH_HOME:-~/.dsh}/.dsh-cli-session/assignments-v1.json` with best-effort directory mode `0700` and file mode `0600`. It stores hashes/identities/cursors, not prompt bodies, reasoning, tool histories, or provider credentials.

## Supervisor-only deployment

The skill and MCP server instruct Astra to delegate implementation and broad reads to DSH. This is guidance, not a technical host sandbox. If the deployment must enforce that Astra cannot use shell/edit/broad-read tools, configure those restrictions in the host policy separately.
