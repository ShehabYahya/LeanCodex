---
name: dsh-cli-session
description: Access an already-running local DeepSeek Harness session through MCP tools.
---

# LeanCodex DSH Session Bridge

Use this skill when a local `dsh web` session needs to be inspected or interacted with. The skill does not prescribe prompt wording, task decomposition, model choice, or orchestration strategy.

## Tools

- `dsh_list_sessions`: list a bounded page of local DSH sessions.
- `dsh_inspect_session`: inspect any listed session by its exact `session_id`; this is read-only and does not require a task, assignment, submission key, or cursor.
- `dsh_inspect_assignment`: recover compact state for an assignment created through this bridge.
- `dsh_send_prompt`: send a prompt to one DSH session with durable request correlation.
- `dsh_wait_output`: wait for an assignment notification using `kind="waiting_for_input"` or `kind="time_limit"`.
- `dsh_read_evidence`: read a bounded continuation of an assignment-owned message or published file.
- `dsh_add_project`: register an existing local project/workspace.
- `dsh_new_session`: create a DSH session.

## Technical semantics

- For an unowned session, call `dsh_list_sessions`, select its `sessionId`, then call `dsh_inspect_session(session_id=...)`. Do not ask for or invent an assignment ID.
- `steer` is the default send mode; it targets the active turn. Use `queue` explicitly when work must wait for the next turn.
- `submission_key` identifies a logical submission. Reusing the same key with the same session and payload resolves to the same assignment/native request. Reusing it with a different binding is an error.
- `accepted: true` means DSH acknowledged prompt admission. `admission_unknown` means the transport outcome was ambiguous; retrying the same logical submission requires the same `submission_key` and identical payload, or the existing `assignmentId` can be inspected.
- `dsh_wait_output` is read-only. It returns finalized outward assistant text and compact lifecycle/evidence events. Internal reasoning, raw tool calls/results, and transient stream/replay payloads are excluded.
- `dsh_wait_output` always returns the same notification envelope: `state_change`, `terminal`, `timeout`, and `unavailable`. `waiting_for_input` waits for an approval/input request, terminal state, timeout, or unavailable observation; `time_limit` waits for a state change, terminal state, timeout, or unavailable observation.
- Ordinary assistant output does not wake an explicit notification wait by itself. The existing signed cursor remains authoritative: output is left unread until a notification is returned, then delivered with that notification.
- Wait cursors are assignment-bound and replayable. `hasMore: true` means additional already-available output can be read from the returned cursor.
- `dsh_read_evidence` accepts only references derived from assignment-owned output or explicitly published deliverables.
- The server accepts loopback DSH endpoints only. Browser-session credentials are read locally and are not tool arguments.
