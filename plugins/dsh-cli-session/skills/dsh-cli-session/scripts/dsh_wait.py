#!/usr/bin/env python3
"""Resumable read-only supervision wait and compact assignment inspection."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from dsh_transport import DshClient, DshError, _inbox_has_request, choose_session, projection_seq, sessions
from dsh_store import decode_cursor, encode_cursor, get_assignment, initial_cursor, update_assignment
from dsh_history import (
    _event_data,
    _find_enclosing_turn,
    _source_rpc_id,
    _turn_from_data,
    _turn_reason_state,
    current_session,
    read_events_since,
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

_TERMINAL_STATES = {"completed", "cancelled", "blocked", "failed", "rejected"}


def _validate_wait(
    timeout_s: int,
    max_items: int,
    max_message_chars: int,
    max_batch_chars: int,
) -> None:
    if not isinstance(timeout_s, int) or timeout_s < 0 or timeout_s > MAX_WAIT_TIMEOUT:
        raise DshError(f"timeout_s must be between 0 and {MAX_WAIT_TIMEOUT}")
    if not isinstance(max_items, int) or max_items < 1 or max_items > MAX_BATCH_ITEMS:
        raise DshError(f"max_items must be between 1 and {MAX_BATCH_ITEMS}")
    if not isinstance(max_message_chars, int) or max_message_chars < 1 or max_message_chars > MAX_MESSAGE_CHARS:
        raise DshError(f"max_message_chars must be between 1 and {MAX_MESSAGE_CHARS}")
    if not isinstance(max_batch_chars, int) or max_batch_chars < 1 or max_batch_chars > MAX_BATCH_CHARS:
        raise DshError(f"max_batch_chars must be between 1 and {MAX_BATCH_CHARS}")


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
    }
    if item is not None:
        result["session"] = {
            "running": bool(item.get("running")),
            "updatedAt": item.get("updatedAt"),
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
    head = projection_seq(item)
    if head is not None and head > initial:
        events, _, _ = read_events_since(
            client,
            assignment["sessionId"],
            initial,
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
    observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assignment["lastCursor"] = cursor
    update_assignment(dsh_home, assignment)
    return {
        "ok": True,
        "assignmentId": assignment["assignmentId"],
        "sessionId": assignment["sessionId"],
        "state": assignment.get("state"),
        "admissionState": assignment.get("admissionState"),
        "terminal": assignment.get("state") in _TERMINAL_STATES,
        "terminalReason": assignment.get("terminalReason"),
        "turn": assignment.get("turn"),
        "items": items,
        "cursor": cursor,
        "hasMore": has_more,
        "timedOut": timed_out,
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
) -> dict[str, Any]:
    _validate_wait(timeout_s, max_items, max_message_chars, max_batch_chars)
    assignment = get_assignment(dsh_home, assignment_id)
    token = after_cursor or initial_cursor(client.secret, assignment)
    after_seq, partial = decode_cursor(client.secret, token, assignment)
    deadline = time.monotonic() + timeout_s

    while True:
        try:
            assignment, item = reconcile_assignment_state(
                client,
                dsh_home,
                assignment,
                deadline=deadline if timeout_s > 0 else None,
            )
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
                    observation={"state": "unavailable", "error": str(exc)},
                )
            time.sleep(min(0.4, max(0.01, deadline - time.monotonic())))
            continue

        head = projection_seq(item) if item is not None else None
        if head is not None and (head > after_seq or partial is not None):
            try:
                events, _, _ = read_events_since(
                    client,
                    assignment["sessionId"],
                    after_seq,
                    deadline=deadline if timeout_s > 0 else None,
                )
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
                    deadline=deadline if timeout_s > 0 else None,
                )
                assignment = get_assignment(dsh_home, assignment_id)
                token = encode_cursor(client.secret, assignment, cursor_after, cursor_partial)
                after_seq, partial = cursor_after, cursor_partial
                if items or has_more:
                    return _result(
                        client,
                        dsh_home,
                        assignment,
                        items=items,
                        cursor=token,
                        timed_out=False,
                        has_more=has_more,
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
                    observation={"state": "gap" if "gap" in str(exc).lower() else "unavailable", "error": str(exc)},
                )

        assignment = get_assignment(dsh_home, assignment_id)
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
            )
        time.sleep(min(0.4, max(0.01, deadline - time.monotonic())))


def inspect_assignment(
    client: DshClient,
    dsh_home: Path,
    assignment_id: str,
) -> dict[str, Any]:
    assignment = get_assignment(dsh_home, assignment_id)
    observation: dict[str, Any] | None = None
    item: dict[str, Any] | None = None
    try:
        assignment, item = reconcile_assignment_state(client, dsh_home, assignment)
    except DshError as exc:
        observation = {"state": "unavailable", "error": str(exc)}
    result = _assignment_view(assignment, item)
    result["cursor"] = assignment.get("lastCursor") or initial_cursor(client.secret, assignment)
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
