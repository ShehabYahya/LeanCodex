# Native DSH contract used by the adapter

This adapter was implemented against the public DeepSeek Harness source at commit `ddefc45fbc7f8e46dd73185e68295696d1297887` (September 2026). Runtime compatibility still depends on the locally installed DSH version; unsupported or missing observations are reported rather than fabricated.

## Prompt identity and idempotency

`session/prompt` accepts a client-minted `requestId`. DSH persists that exact identity on the accepted durable `user/message` as `data.source.rpcId`. Current DSH also deduplicates prompt retries when that `requestId` is already present in the Agent inbox or log. The plugin therefore persists its assignment/native request identity before attempting admission and reuses the same native `requestId` after an ambiguous transport outcome.

The MCP-layer `rpcId` used to correlate one HTTP Remote request is separate from DSH's prompt `requestId` and is never treated as assignment identity. While a prompt remains queued, the native `inbox` projection can also carry the same `source.rpcId`, allowing an ambiguous admission to be reconciled before the message is claimed into a turn.

## Durable observation

DSH exposes:

- `session/list`: session summaries. When projection hints are present, `projections.asOfSeq` is the shared projection watermark for the latest event reflected by the cached projection block.
- `session/page`: a cold-safe, message-aligned backwards page of the durable Session journal through an inclusive `throughSeq`.
- `session/follow`: a native streaming surface whose opening snapshot carries the durable cursor and ordered history records followed by gap-free durable event frames.

The Python plugin uses unary `session/list` + `session/page` polling for its compatibility path so it does not add a WebSocket dependency to the MCP process. It reconstructs the contiguous durable suffix after the signed cursor and returns an explicit gap/unavailable outcome when continuity cannot be proven. Unsolicited push into the MCP client or model is not claimed.

The MCP wait surface has two policies, `waiting_for_input` and `time_limit`. Both return the common `state_change`, `terminal`, `timeout`, and `unavailable` fields. A finalized assistant message by itself is not a notification condition for an explicit wait; the existing signed cursor is advanced only when a notification is returned, so ordinary output remains replayable until it can be delivered with that notification.

## Finalized output projection

Canonical finalized model output is the durable `assistant/message` event. Its `data.message.content` is merge-extensible and currently includes blocks such as:

- `text` — outward user-visible text; included.
- `reasoning` — internal thinking; excluded.
- `tool-call` — raw model tool request; excluded.
- `tool-result` — excluded from the normal output feed.
- `image` / `file` — projected only as bounded attachment metadata when present.
- unknown future block types — fail closed as a compact `unsupported_content` signal; raw payload is not dumped.

The embedded `assistant/message.data.stream` and process-local assistant-stream frames are never returned by the normal output feed. This avoids duplicate stream material and prevents reasoning deltas or raw tool arguments from entering the caller-visible result.

## Assignment-to-turn correlation

An assignment is first considered claimed when a durable `user/message` with `source.kind == "user"` and `source.rpcId == nativeRequestId` is observed. The adapter records the enclosing turn if it can prove one from durable turn boundaries. This is correlation, not a claim that every assignment is exclusively one turn: steering or coalesced inputs may share an existing turn.

Relevant durable lifecycle facts include:

- `turn/end.reason.kind`: `completed`, `aborted`, `blocked`, `error`, `max-tokens`, or `interrupted`.
- `llm/retry` / `llm/retry-started`: provider retry lifecycle.
- `approval/asked` / `approval/decided`: mapped to waiting-for-input/resolved signals where present.
- `tool-workflow/agent-*` / `tool-workflow/run-end`: compact child/workflow lifecycle signals.
- `deliverables/presented`: assignment-owned evidence references.

A non-empty `assistant/message` is progress, not terminal completion. A `turn/end` event provides execution state; it does not independently establish application-level task success.

## Evidence ownership

`dsh_read_evidence` accepts only references derived from already-observed assignment events:

- `message:<seq>:<block>` — bounded continuation of a text block from an assignment-owned `assistant/message`.
- `artifact:<seq>:<index>` — bounded read of a file explicitly named by an assignment-owned `deliverables/presented` event.

For artifact reads the plugin revalidates the exact event/index, assignment turn/range, current regular-file status, refuses current symlinks, and returns a SHA-256/size/mtime version. A later read may supply the prior SHA-256 and fails explicitly if the file changed. It does not expose arbitrary model-supplied absolute-path reads.

## What is not claimed

- No guarantee that an MCP notification wakes the client or reaches model context without an ordinary tool call.
- No exactly-once claim derived from the MCP layer alone. Duplicate-admission safety relies on persisting assignment identity before admission and reusing DSH's native request identity/deduplication contract.
- No inference of task success from process liveness, silence, or child failure.
- No claim that this plugin disables unrelated host tools.
