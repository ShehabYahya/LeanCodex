#!/usr/bin/env python3
"""Lossless outward-output and compact lifecycle projection for one assignment."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dsh_transport import DshClient, DshError
from dsh_store import update_assignment
from dsh_history import (
    _assistant_blocks, _assistant_message_id, _attachment_projection, _event_data,
    _find_enclosing_turn, _redact_text, _safe_turn_reason_details, _source_rpc_id,
    _turn_from_data, _turn_reason_state,
)

def _safe_retry_signal(event: dict[str, Any]) -> dict[str, Any]:
    data = _event_data(event)
    signal: dict[str, Any] = {
        "kind": "provider_retry" if event.get("type") == "llm/retry" else "provider_retry_started",
        "eventId": f"seq:{event['seq']}",
        "seq": event["seq"],
    }
    for key in ("turn", "step", "provider", "policyKey", "retry", "maxRetries", "delayMs"):
        value = data.get(key)
        if isinstance(value, (str, int)):
            signal[key] = value
    failure = data.get("failure")
    if isinstance(failure, dict):
        clean: dict[str, Any] = {}
        for key in ("code", "status", "providerRetryAfterMs"):
            value = failure.get(key)
            if isinstance(value, (str, int)):
                clean[key] = value
        if clean:
            signal["failure"] = clean
    return signal

def _child_signal(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event.get("type")
    if event_type not in {"tool-workflow/agent-start", "tool-workflow/agent-end", "tool-workflow/run-end"}:
        return None
    data = _event_data(event)
    signal: dict[str, Any] = {
        "kind": "child_lifecycle",
        "event": event_type,
        "eventId": f"seq:{event['seq']}",
        "eventSeq": event["seq"],
    }
    for key in ("runId", "childId", "label", "phase", "outcome", "stopReason"):
        value = data.get(key)
        if isinstance(value, (str, int, bool)):
            signal[key] = value
    if isinstance(data.get("seq"), int):
        signal["memberSeq"] = data["seq"]
    return signal

def _deliverable_signal(event: dict[str, Any], assignment: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("type") != "deliverables/presented":
        return None
    data = _event_data(event)
    files = data.get("files")
    if not isinstance(files, list):
        return None
    projected = []
    for index, item in enumerate(files[:32]):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        projected.append({
            "ref": f"artifact:{event['seq']}:{index}",
            "path": item["path"],
            **({"description": item["description"]} if isinstance(item.get("description"), str) else {}),
        })
    return {
        "kind": "evidence_published",
        "eventId": f"seq:{event['seq']}",
        "seq": event["seq"],
        "assignmentId": assignment["assignmentId"],
        "files": projected,
        "truncated": len(files) > len(projected),
    }

def _approval_signal(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event.get("type")
    data = _event_data(event)
    if event_type == "approval/asked":
        signal: dict[str, Any] = {
            "kind": "waiting_for_input", "eventId": f"seq:{event['seq']}", "seq": event["seq"]
        }
        # Tool identity helps the supervisor understand the blocker without
        # copying approval reason text or raw call arguments into its context.
        for key in ("id", "toolName"):
            if isinstance(data.get(key), str):
                signal[key] = data[key]
        return signal
    if event_type == "approval/decided":
        signal = {"kind": "input_resolved", "eventId": f"seq:{event['seq']}", "seq": event["seq"]}
        for key in ("id", "outcome"):
            if isinstance(data.get(key), str):
                signal[key] = data[key]
        return signal
    return None

def _item_chars(item: dict[str, Any]) -> int:
    # Visible text is the dominant model-context payload and has its own exact
    # character budget; metadata-only signals are bounded by their serialized size.
    if isinstance(item.get("text"), str):
        return len(item["text"])
    return len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))

def reduce_events(
    client: DshClient,
    dsh_home: Path,
    assignment: dict[str, Any],
    events: list[dict[str, Any]],
    after_seq: int,
    partial: dict[str, int] | None,
    *,
    max_items: int,
    max_message_chars: int,
    max_batch_chars: int,
    deadline: float | None = None,
) -> tuple[list[dict[str, Any]], int, dict[str, int] | None, bool]:
    outputs: list[dict[str, Any]] = []
    batch_chars = 0
    cursor_after = after_seq
    cursor_partial = partial
    changed = False
    request_id = assignment.get("nativeRequestId")

    active_turn = assignment.get("turn") if isinstance(assignment.get("turn"), int) else None
    claim_seq = assignment.get("claimSeq") if isinstance(assignment.get("claimSeq"), int) else None
    terminal_bound = assignment.get("terminalSeq") if isinstance(assignment.get("terminalSeq"), int) else None

    def persist_if_changed() -> None:
        if changed:
            update_assignment(dsh_home, assignment)

    def append_item(item: dict[str, Any], continuation: dict[str, int]) -> bool:
        nonlocal batch_chars, cursor_partial
        size = _item_chars(item)
        if len(outputs) >= max_items or batch_chars + size > max_batch_chars:
            if not outputs and size > max_batch_chars:
                raise DshError("max_batch_chars is too small to encode one supervision item; increase the limit")
            cursor_partial = continuation
            return False
        outputs.append(item)
        batch_chars += size
        return True

    for event in events:
        seq = int(event["seq"])
        if terminal_bound is not None and seq > terminal_bound:
            break
        if cursor_partial is not None and seq < cursor_partial["seq"]:
            continue
        if cursor_partial is None and seq <= cursor_after:
            continue

        if claim_seq is None and _source_rpc_id(event) == request_id:
            claim_seq = seq
            assignment["claimSeq"] = seq
            active_turn = _find_enclosing_turn(client, assignment["sessionId"], seq, deadline=deadline)
            assignment["turn"] = active_turn
            assignment["admissionState"] = "accepted"
            assignment["state"] = "running"
            changed = True

        belongs = claim_seq is not None and seq >= claim_seq
        event_turn = _turn_from_data(event)
        if active_turn is not None and event_turn is not None:
            belongs = belongs and event_turn == active_turn

        if belongs and event.get("type") == "assistant/message":
            blocks = _assistant_blocks(event)
            start_block = 0
            start_offset = 0
            if cursor_partial is not None and cursor_partial["seq"] == seq:
                start_block = cursor_partial["block"]
                start_offset = cursor_partial["offset"]
            for block_index, block in enumerate(blocks):
                if block_index < start_block:
                    continue
                block_type = block.get("type")
                if block_type == "text" and isinstance(block.get("text"), str):
                    full_text, redactions = _redact_text(block["text"], client)
                    offset = start_offset if block_index == start_block else 0
                    # Empty visible text is not an outward output but the event still advances.
                    while offset < len(full_text):
                        room = min(max_message_chars, max_batch_chars - batch_chars)
                        if room <= 0 or len(outputs) >= max_items:
                            cursor_partial = {"seq": seq, "block": block_index, "offset": offset}
                            persist_if_changed()
                            return outputs, cursor_after, cursor_partial, True
                        end = min(len(full_text), offset + room)
                        item = {
                            "kind": "output",
                            "eventId": f"seq:{seq}:block:{block_index}:offset:{offset}",
                            "messageId": _assistant_message_id(event),
                            "seq": seq,
                            "turn": event_turn,
                            "block": block_index,
                            "offset": offset,
                            "endOffset": end,
                            "totalChars": len(full_text),
                            "complete": end == len(full_text),
                            "text": full_text[offset:end],
                            "ref": f"message:{seq}:{block_index}",
                        }
                        if redactions:
                            item["redactions"] = redactions
                        if not append_item(item, {"seq": seq, "block": block_index, "offset": offset}):
                            persist_if_changed()
                            return outputs, cursor_after, cursor_partial, True
                        offset = end
                        if offset < len(full_text):
                            cursor_partial = {"seq": seq, "block": block_index, "offset": offset}
                            persist_if_changed()
                            return outputs, cursor_after, cursor_partial, True
                    start_offset = 0
                elif block_type in {"reasoning", "tool-call", "tool-result"}:
                    # Explicit denylist inside an allowlisted finalized Assistant event.
                    continue
                elif block_type in {"image", "file"}:
                    item = {
                        "kind": "attachment",
                        "eventId": f"seq:{seq}:block:{block_index}",
                        "messageId": _assistant_message_id(event),
                        "seq": seq,
                        "turn": event_turn,
                        "block": block_index,
                        "attachment": _attachment_projection(block),
                    }
                    if not append_item(item, {"seq": seq, "block": block_index, "offset": 0}):
                        persist_if_changed()
                        return outputs, cursor_after, cursor_partial, True
                else:
                    item = {
                        "kind": "unsupported_content",
                        "eventId": f"seq:{seq}:block:{block_index}",
                        "messageId": _assistant_message_id(event),
                        "seq": seq,
                        "turn": event_turn,
                        "block": block_index,
                        "contentType": str(block_type) if block_type is not None else "unknown",
                    }
                    if not append_item(item, {"seq": seq, "block": block_index, "offset": 0}):
                        persist_if_changed()
                        return outputs, cursor_after, cursor_partial, True

        elif belongs and event.get("type") in {"llm/retry", "llm/retry-started"}:
            item = _safe_retry_signal(event)
            assignment["state"] = "retrying" if event.get("type") == "llm/retry" else "running"
            changed = True
            if not append_item(item, {"seq": seq, "block": 0, "offset": 0}):
                persist_if_changed()
                return outputs, cursor_after, cursor_partial, True

        elif belongs and event.get("type") in {"tool-workflow/agent-start", "tool-workflow/agent-end", "tool-workflow/run-end"}:
            child = _child_signal(event)
            if child is not None and not append_item(child, {"seq": seq, "block": 0, "offset": 0}):
                persist_if_changed()
                return outputs, cursor_after, cursor_partial, True

        elif belongs and event.get("type") == "deliverables/presented":
            evidence = _deliverable_signal(event, assignment)
            if evidence is not None and not append_item(evidence, {"seq": seq, "block": 0, "offset": 0}):
                persist_if_changed()
                return outputs, cursor_after, cursor_partial, True

        elif belongs and event.get("type") in {"approval/asked", "approval/decided"}:
            signal = _approval_signal(event)
            if signal is not None:
                assignment["state"] = "waiting_for_input" if event.get("type") == "approval/asked" else "running"
                changed = True
                if not append_item(signal, {"seq": seq, "block": 0, "offset": 0}):
                    persist_if_changed()
                    return outputs, cursor_after, cursor_partial, True

        elif belongs and event.get("type") == "turn/end" and (active_turn is None or event_turn == active_turn):
            data = _event_data(event)
            state, reason = _turn_reason_state(data.get("reason"))
            assignment["state"] = state
            assignment["terminalSeq"] = seq
            assignment["terminalReason"] = reason
            terminal_bound = seq
            changed = True
            raw_reason = data.get("reason")
            item = {
                "kind": "lifecycle",
                "eventId": f"seq:{seq}",
                "seq": seq,
                "state": state,
                "terminal": True,
                "reason": reason,
                "turn": event_turn,
                **_safe_turn_reason_details(raw_reason),
            }
            if not append_item(item, {"seq": seq, "block": 0, "offset": 0}):
                persist_if_changed()
                return outputs, cursor_after, cursor_partial, True

        cursor_after = seq
        cursor_partial = None
        if terminal_bound is not None and seq >= terminal_bound:
            break

    persist_if_changed()
    return outputs, cursor_after, cursor_partial, False

