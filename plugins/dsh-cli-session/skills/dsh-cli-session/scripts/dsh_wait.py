#!/usr/bin/env python3
"""Resumable read-only supervision wait and compact assignment inspection."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Literal

from dsh_transport import DshClient, DshError, _inbox_has_request, choose_session, projection_seq, sessions
from dsh_store import decode_cursor, encode_cursor, get_assignment, initial_cursor, update_assignment
from dsh_history import (
    _assistant_blocks,
    _event_data,
    _find_enclosing_turn,
    _source_rpc_id,
    _turn_from_data,
    _turn_reason_state,
    current_session,
    read_events_since,
    read_recent_events,
)
from dsh_projection import reduce_events

DEFAULT_WAIT_TIMEOUT = 30
MAX_WAIT_TIMEOUT = 120
DEFAULT_MAX_BATCH_ITEMS = 8
MAX_BATCH_ITEMS = 32
DEFAULT_MAX_MESSAGE_CHARS = 6000
MAX_MESSAGE_CHARS = 20000
DEFAULT_MAX_BATCH_CHARS = 12000
MAX_BATCH_CHARS = 50000

WAIT_KINDS = {"waiting_for_input", "time_limit"}

_TERMINAL_STATES = {"completed", "cancelled", "blocked", "failed", "rejected"}


def _validate_wait(
    timeout_s: int,
    max_items: int,
    max_message_chars: int,
    max_batch_chars: int,
    kind: str | None = None,
) -> None:
    if not isinstance(timeout_s, int) or timeout_s < 0 or timeout_s > MAX_WAIT_TIMEOUT:
        raise DshError(f"timeout_s must be between 0 and {MAX_WAIT_TIMEOUT}")
    if not isinstance(max_items, int) or max_items < 1 or max_items > MAX_BATCH_ITEMS:
        raise DshError(f"max_items must be between 1 and {MAX_BATCH_ITEMS}")
    if not isinstance(max_message_chars, int) or max_message_chars < 1 or max_message_chars > MAX_MESSAGE_CHARS:
        raise DshError(f"max_message_chars must be between 1 and {MAX_MESSAGE_CHARS}")
    if not isinstance(max_batch_chars, int) or max_batch_chars < 256 or max_batch_chars > MAX_BATCH_CHARS:
        raise DshError(f"max_batch_chars must be between 256 and {MAX_BATCH_CHARS}")
    if kind is not None and kind not in WAIT_KINDS:
        raise DshError(f"kind must be one of {sorted(WAIT_KINDS)}")


def _assignment_view(assignment: dict[str, Any], item: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "assignmentId": assignment.get("assignmentId"),
        "sessionId": assignment.get("sessionId"),
        "requestId": assignment.get("nativeRequestId"),
        "mode": assignment.get("mode"),
        "state": assignment.get("state"),
        "admissionState": assignment.get("admissionState"),
        "createdAt": assignment.get("createdAt"),
        "admittedAt": assignment.get("admittedAt"),
        "cwd": assignment.get("cwd"),
        "claimSeq": assignment.get("claimSeq"),
        "turn": assignment.get("turn"),
        "terminalSeq": assignment.get("terminalSeq"),
        "terminalReason": assignment.get("terminalReason"),
        "lastCursor": assignment.get("lastCursor"),
        "reconcileSeq": assignment.get("reconcileSeq"),
    }
    if item is not None:
        result["session"] = {
            "running": bool(item.get("running")),
            "updatedAt": item.get("updatedAt"),
            "projectionSeq": projection_seq(item),
        }
        result["freshness"] = {
            "observedAtMs": int(time.time() * 1000),
            "projectionSeq": projection_seq(item),
        }
    return result


def reconcile_assignment_state(
    client: DshClient,
    dsh_home: Path,
    assignment: dict[str, Any],
    *,
    deadline: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Refresh compact state from native request identity, inbox, and durable turn events."""
    if assignment.get("state") in _TERMINAL_STATES and isinstance(assignment.get("terminalSeq"), int):
        try:
            return assignment, current_session(client, assignment["sessionId"])
        except DshError:
            return assignment, None

    timeout = 20.0 if deadline is None else max(0.1, min(20.0, deadline - time.monotonic()))
    if deadline is not None and time.monotonic() >= deadline:
        raise DshError("wait observation deadline expired")
    item = current_session(client, assignment["sessionId"], timeout=timeout)
    changed = False
    request_id = assignment.get("nativeRequestId")

    if _inbox_has_request(item, request_id):
        if assignment.get("admissionState") != "accepted":
            assignment["admissionState"] = "accepted"
            changed = True
        if assignment.get("claimSeq") is None and assignment.get("state") not in {"retrying", "waiting_for_input"}:
            assignment["state"] = "queued"
            changed = True

    initial = assignment.get("initialSeq")
    if not isinstance(initial, int) or initial < -1:
        initial = -1
    reconcile_seq = assignment.get("reconcileSeq")
    if not isinstance(reconcile_seq, int) or reconcile_seq < initial:
        reconcile_seq = initial
    head = projection_seq(item)
    if head is not None and head > reconcile_seq:
        events, _, _ = read_events_since(
            client,
            assignment["sessionId"],
            reconcile_seq,
            deadline=deadline,
        )
        claim_seq = assignment.get("claimSeq") if isinstance(assignment.get("claimSeq"), int) else None
        active_turn = assignment.get("turn") if isinstance(assignment.get("turn"), int) else None

        if claim_seq is None:
            for event in events:
                if _source_rpc_id(event) == request_id:
                    claim_seq = int(event["seq"])
                    assignment["claimSeq"] = claim_seq
                    active_turn = _find_enclosing_turn(
                        client,
                        assignment["sessionId"],
                        claim_seq,
                        deadline=deadline,
                    )
                    assignment["turn"] = active_turn
                    assignment["admissionState"] = "accepted"
                    assignment["state"] = "running"
                    changed = True
                    break

        if claim_seq is not None:
            latest_state: str | None = None
            for event in events:
                seq = int(event["seq"])
                if seq < claim_seq:
                    continue
                event_turn = _turn_from_data(event)
                if active_turn is not None and event_turn is not None and event_turn != active_turn:
                    continue
                event_type = event.get("type")
                if event_type == "llm/retry":
                    latest_state = "retrying"
                elif event_type == "llm/retry-started":
                    latest_state = "running"
                elif event_type == "approval/asked":
                    latest_state = "waiting_for_input"
                elif event_type == "approval/decided":
                    latest_state = "running"
                elif event_type == "turn/end" and (active_turn is None or event_turn == active_turn):
                    state, reason = _turn_reason_state(_event_data(event).get("reason"))
                    assignment["state"] = state
                    assignment["terminalSeq"] = seq
                    assignment["terminalReason"] = reason
                    changed = True
                    latest_state = None
                    break
            if latest_state is not None and assignment.get("state") != latest_state:
                assignment["state"] = latest_state
                changed = True
        assignment["reconcileSeq"] = head
        changed = True

    if changed:
        update_assignment(dsh_home, assignment)
    return assignment, item


def _result(
    client: DshClient,
    dsh_home: Path,
    assignment: dict[str, Any],
    *,
    items: list[dict[str, Any]],
    cursor: str,
    timed_out: bool,
    has_more: bool,
    wait_kind: str = "time_limit",
    timeout_s: int = DEFAULT_WAIT_TIMEOUT,
    state_changed: bool = False,
    previous_state: str | None = None,
    observation: dict[str, Any] | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    assignment["lastCursor"] = cursor
    update_assignment(dsh_home, assignment)
    cursor_after, cursor_partial = decode_cursor(client.secret, cursor, assignment)
    terminal_observed = assignment.get("state") in _TERMINAL_STATES
    terminal_seq = assignment.get("terminalSeq")
    terminal_delivered = (
        terminal_observed
        and isinstance(terminal_seq, int)
        and cursor_partial is None
        and cursor_after >= terminal_seq
        and not has_more
    )
    if outcome is None:
        if observation is not None and observation.get("state") == "gap":
            outcome = "gap"
        elif observation is not None and observation.get("state") == "unavailable":
            outcome = "unavailable"
        elif timed_out:
            outcome = "timeout"
        elif any(item.get("kind") in {"output", "attachment", "unsupported_content"} for item in items):
            outcome = "output"
        else:
            outcome = "state_change"
    return {
        "ok": True,
        "assignmentId": assignment["assignmentId"],
        "sessionId": assignment["sessionId"],
        "state": assignment.get("state"),
        "admissionState": assignment.get("admissionState"),
        "kind": wait_kind,
        "outcome": outcome,
        "state_change": {
            "changed": state_changed,
            "from": previous_state,
            "to": assignment.get("state"),
        },
        "terminalObserved": terminal_observed,
        "terminal": terminal_delivered,
        "terminalReason": assignment.get("terminalReason"),
        "turn": assignment.get("turn"),
        "items": items,
        "cursor": cursor,
        "hasMore": has_more,
        "timedOut": timed_out,
        "timeout": {"limitSeconds": timeout_s, "occurred": timed_out},
        "unavailable": observation if observation is not None and observation.get("state") == "unavailable" else None,
        **({"observation": observation} if observation is not None else {}),
    }


def wait_output(
    client: DshClient,
    dsh_home: Path,
    assignment_id: str,
    after_cursor: str | None = None,
    timeout_s: int = DEFAULT_WAIT_TIMEOUT,
    max_items: int = DEFAULT_MAX_BATCH_ITEMS,
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
    max_batch_chars: int = DEFAULT_MAX_BATCH_CHARS,
    kind: Literal["waiting_for_input", "time_limit"] | None = None,
) -> dict[str, Any]:
    # ``None`` preserves the pre-notification behavior for the internal legacy
    # helper. The MCP surface always supplies one of the two explicit policies.
    legacy_immediate = kind is None
    wait_kind = kind or "time_limit"
    _validate_wait(timeout_s, max_items, max_message_chars, max_batch_chars, kind)
    assignment = get_assignment(dsh_home, assignment_id)
    token = after_cursor or initial_cursor(client.secret, assignment)
    after_seq, partial = decode_cursor(client.secret, token, assignment)
    previous_state = assignment.get("state") if isinstance(assignment.get("state"), str) else None
    state_change_seen = False
    last_state = previous_state
    deadline = time.monotonic() + timeout_s
    # timeout_s=0 means one non-waiting observation, not an unbounded upstream RPC.
    observation_deadline = deadline if timeout_s > 0 else time.monotonic() + 1.0

    while True:
        try:
            state_before_reconcile = assignment.get("state")
            assignment, item = reconcile_assignment_state(
                client,
                dsh_home,
                assignment,
                deadline=observation_deadline,
            )
            if assignment.get("state") != state_before_reconcile:
                state_change_seen = True
            if assignment.get("state") != last_state:
                state_change_seen = True
                last_state = assignment.get("state")
        except DshError as exc:
            # Observation failure is not assignment failure. Keep the same signed cursor.
            if timeout_s == 0 or time.monotonic() >= deadline:
                return _result(
                    client,
                    dsh_home,
                    assignment,
                    items=[],
                    cursor=token,
                    timed_out=False,
                    has_more=False,
                    wait_kind=wait_kind,
                    timeout_s=timeout_s,
                    state_changed=state_change_seen,
                    previous_state=previous_state,
                    observation={"state": "unavailable", "error": str(exc)},
                )
            time.sleep(min(0.4, max(0.01, deadline - time.monotonic())))
            continue

        head = projection_seq(item) if item is not None else None
        if head is not None and after_seq > head:
            return _result(
                client,
                dsh_home,
                assignment,
                items=[],
                cursor=token,
                timed_out=False,
                has_more=False,
                wait_kind=wait_kind,
                timeout_s=timeout_s,
                state_changed=state_change_seen,
                previous_state=previous_state,
                observation={
                    "state": "gap",
                    "error": f"supervision cursor seq {after_seq} is ahead of current session cursor {head}",
                },
            )
        if head is not None and (head > after_seq or partial is not None):
            try:
                events, _, _ = read_events_since(
                    client,
                    assignment["sessionId"],
                    after_seq,
                    deadline=observation_deadline,
                )
                notification_ready = (
                    legacy_immediate
                    or (not legacy_immediate and wait_kind == "time_limit" and state_change_seen)
                    or (not legacy_immediate and wait_kind == "waiting_for_input" and assignment.get("state") == "waiting_for_input")
                )
                if not legacy_immediate:
                    claim_seq = assignment.get("claimSeq")
                    active_turn = assignment.get("turn")
                    for event in events:
                        seq = event.get("seq")
                        if not isinstance(seq, int):
                            continue
                        if isinstance(claim_seq, int) and seq < claim_seq:
                            continue
                        event_turn = _turn_from_data(event)
                        if isinstance(active_turn, int) and isinstance(event_turn, int) and event_turn != active_turn:
                            continue
                        event_type = event.get("type")
                        if event_type in {"llm/retry", "llm/retry-started", "approval/asked", "approval/decided", "turn/end"}:
                            state_change_seen = True
                            notification_ready = True
                        if wait_kind == "waiting_for_input" and event_type == "approval/asked":
                            notification_ready = True

                if notification_ready:
                    items, cursor_after, cursor_partial, has_more = reduce_events(
                        client,
                        dsh_home,
                        assignment,
                        events,
                        after_seq,
                        partial,
                        max_items=max_items,
                        max_message_chars=max_message_chars,
                        max_batch_chars=max_batch_chars,
                        deadline=observation_deadline,
                    )
                    assignment = get_assignment(dsh_home, assignment_id)
                    if assignment.get("state") != last_state:
                        state_change_seen = True
                        last_state = assignment.get("state")
                    token = encode_cursor(client.secret, assignment, cursor_after, cursor_partial)
                    after_seq, partial = cursor_after, cursor_partial
                    if legacy_immediate or items or has_more or state_change_seen:
                        return _result(
                            client,
                            dsh_home,
                            assignment,
                            items=items,
                            cursor=token,
                            timed_out=False,
                            has_more=has_more,
                            wait_kind=wait_kind,
                            timeout_s=timeout_s,
                            state_changed=state_change_seen,
                            previous_state=previous_state,
                        )
            except DshError as exc:
                return _result(
                    client,
                    dsh_home,
                    assignment,
                    items=[],
                    cursor=token,
                    timed_out=False,
                    has_more=False,
                    wait_kind=wait_kind,
                    timeout_s=timeout_s,
                    state_changed=state_change_seen,
                    previous_state=previous_state,
                    observation={"state": "gap" if "gap" in str(exc).lower() else "unavailable", "error": str(exc)},
                )

        assignment = get_assignment(dsh_home, assignment_id)
        if not legacy_immediate and wait_kind == "waiting_for_input" and assignment.get("state") == "waiting_for_input":
            return _result(
                client,
                dsh_home,
                assignment,
                items=[],
                cursor=token,
                timed_out=False,
                has_more=False,
                wait_kind=wait_kind,
                timeout_s=timeout_s,
                state_changed=state_change_seen,
                previous_state=previous_state,
            )
        if not legacy_immediate and wait_kind == "time_limit" and state_change_seen:
            return _result(
                client,
                dsh_home,
                assignment,
                items=[],
                cursor=token,
                timed_out=False,
                has_more=False,
                wait_kind=wait_kind,
                timeout_s=timeout_s,
                state_changed=True,
                previous_state=previous_state,
            )
        terminal_seq = assignment.get("terminalSeq")
        if assignment.get("state") in _TERMINAL_STATES and isinstance(terminal_seq, int):
            if partial is None and after_seq >= terminal_seq:
                return _result(
                    client,
                    dsh_home,
                    assignment,
                    items=[],
                    cursor=token,
                    timed_out=False,
                    has_more=False,
                    wait_kind=wait_kind,
                    timeout_s=timeout_s,
                    state_changed=state_change_seen,
                    previous_state=previous_state,
                )

        if timeout_s == 0 or time.monotonic() >= deadline:
            return _result(
                client,
                dsh_home,
                assignment,
                items=[],
                cursor=token,
                timed_out=True,
                has_more=False,
                wait_kind=wait_kind,
                timeout_s=timeout_s,
                state_changed=state_change_seen,
                previous_state=previous_state,
            )
        time.sleep(min(0.4, max(0.01, deadline - time.monotonic())))


def _bounded_tail_summary(
    assignment: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    claim = assignment.get("claimSeq")
    terminal = assignment.get("terminalSeq")
    turn = assignment.get("turn")
    filtered: list[dict[str, Any]] = []
    for event in events:
        seq = event.get("seq")
        if not isinstance(seq, int):
            continue
        if isinstance(claim, int) and seq < claim:
            continue
        if isinstance(terminal, int) and seq > terminal:
            continue
        event_turn = _turn_from_data(event)
        if isinstance(turn, int) and isinstance(event_turn, int) and event_turn != turn:
            continue
        filtered.append(event)

    output_refs: list[str] = []
    evidence_refs: list[str] = []
    approvals: dict[str, dict[str, Any]] = {}
    children: dict[tuple[str, int], dict[str, Any]] = {}
    for event in filtered:
        seq = int(event["seq"])
        event_type = event.get("type")
        data = _event_data(event)
        if event_type == "assistant/message":
            for index, block in enumerate(_assistant_blocks(event)):
                if block.get("type") == "text":
                    output_refs.append(f"message:{seq}:{index}")
        elif event_type == "deliverables/presented":
            files = data.get("files")
            if isinstance(files, list):
                evidence_refs.extend(
                    f"artifact:{seq}:{index}"
                    for index, value in enumerate(files[:32])
                    if isinstance(value, dict) and isinstance(value.get("path"), str)
                )
        elif event_type == "approval/asked":
            approval_id = data.get("id")
            if isinstance(approval_id, str):
                approvals[approval_id] = {
                    "kind": "waiting_for_input",
                    "id": approval_id,
                    **({"toolName": data["toolName"]} if isinstance(data.get("toolName"), str) else {}),
                    "seq": seq,
                }
        elif event_type == "approval/decided":
            approval_id = data.get("id")
            if isinstance(approval_id, str):
                approvals.pop(approval_id, None)
        elif event_type == "tool-workflow/agent-start":
            run_id = data.get("runId")
            member_seq = data.get("seq")
            if isinstance(run_id, str) and isinstance(member_seq, int):
                children[(run_id, member_seq)] = {
                    "runId": run_id,
                    "memberSeq": member_seq,
                    "state": "running",
                    **({"childId": data["childId"]} if isinstance(data.get("childId"), str) else {}),
                    **({"label": data["label"]} if isinstance(data.get("label"), str) else {}),
                    **({"phase": data["phase"]} if isinstance(data.get("phase"), str) else {}),
                }
        elif event_type == "tool-workflow/agent-end":
            run_id = data.get("runId")
            member_seq = data.get("seq")
            if isinstance(run_id, str) and isinstance(member_seq, int):
                summary = children.setdefault((run_id, member_seq), {
                    "runId": run_id, "memberSeq": member_seq,
                })
                outcome = data.get("outcome")
                summary["state"] = outcome if isinstance(outcome, str) else "settled"

    blocker = None
    if assignment.get("state") == "waiting_for_input":
        if approvals:
            blocker = sorted(approvals.values(), key=lambda value: value["seq"])[-1]
        else:
            blocker = {"kind": "waiting_for_input", "detailsAvailable": False}

    return {
        "latestOutputRefs": output_refs[-5:],
        "latestEvidenceRefs": evidence_refs[-8:],
        "blocker": blocker,
        "children": list(children.values())[-8:],
    }


def inspect_assignment(
    client: DshClient,
    dsh_home: Path,
    assignment_id: str,
) -> dict[str, Any]:
    assignment = get_assignment(dsh_home, assignment_id)
    observation: dict[str, Any] | None = None
    item: dict[str, Any] | None = None
    tail: list[dict[str, Any]] = []
    tail_has_more = False
    try:
        assignment, item = reconcile_assignment_state(client, dsh_home, assignment)
        tail, recent_item, tail_has_more = read_recent_events(
            client, assignment["sessionId"], max_messages=12, timeout=5.0
        )
        item = recent_item
    except DshError as exc:
        observation = {"state": "unavailable", "error": str(exc)}
    result = _assignment_view(assignment, item)
    result["cursor"] = assignment.get("lastCursor") or initial_cursor(client.secret, assignment)
    result["recent"] = {
        **_bounded_tail_summary(assignment, tail),
        "tailHasMore": tail_has_more,
    }
    if observation is not None:
        result["observation"] = observation
    return result


def wait_for_terminal_legacy(
    client: DshClient,
    dsh_home: Path,
    assignment_id: str,
    cursor: str,
    timeout_s: int,
) -> tuple[str | None, bool, str]:
    """Compatibility helper for the old --wait/dsh_send_prompt(wait_seconds) behavior."""
    deadline = time.monotonic() + max(0, timeout_s)
    texts: list[str] = []
    current = cursor
    while True:
        remaining = max(0, int(deadline - time.monotonic()))
        result = wait_output(
            client,
            dsh_home,
            assignment_id,
            current,
            timeout_s=min(DEFAULT_WAIT_TIMEOUT, remaining),
            max_items=DEFAULT_MAX_BATCH_ITEMS,
            max_message_chars=DEFAULT_MAX_MESSAGE_CHARS,
            max_batch_chars=DEFAULT_MAX_BATCH_CHARS,
        )
        current = result["cursor"]
        for item in result.get("items", []):
            if item.get("kind") == "output" and isinstance(item.get("text"), str):
                texts.append(item["text"])
        if result.get("terminal") is True:
            return "".join(texts) or None, result.get("state") == "completed", current
        if time.monotonic() >= deadline:
            return "".join(texts) or None, False, current
        if result.get("hasMore") is True:
            continue
