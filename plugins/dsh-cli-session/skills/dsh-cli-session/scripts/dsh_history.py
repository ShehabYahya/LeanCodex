#!/usr/bin/env python3
"""Bounded durable Session-journal reads and safe event/message helpers."""
from __future__ import annotations

import re
import time
from typing import Any, Iterable

from dsh_transport import (
    DshClient, DshError, DshGapError, b64url, choose_session, projection_seq, sessions,
)

DEFAULT_PAGE_MESSAGES = 50
MAX_HISTORY_PAGES = 100

def _page(
    client: DshClient,
    session_id: str,
    through_seq: int,
    before_seq: int | None = None,
    max_messages: int = DEFAULT_PAGE_MESSAGES,
    timeout: float = 20.0,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "address": {"kind": "session", "sessionId": session_id},
        "throughSeq": through_seq,
        "maxMessages": max_messages,
    }
    if before_seq is not None:
        request["beforeSeq"] = before_seq
    value = client.rpc("session/page", "request", request, timeout=timeout)
    if not isinstance(value, dict) or not isinstance(value.get("records"), list):
        raise DshError("DSH session/page returned invalid history")
    return value

def _event_records(page: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in page.get("records", []):
        if not isinstance(record, dict) or record.get("type") != "event":
            continue
        event = record.get("event")
        if isinstance(event, dict) and isinstance(event.get("seq"), int):
            result.append(event)
    return result

def current_session(client: DshClient, session_id: str, timeout: float = 20.0) -> dict[str, Any]:
    return choose_session(sessions(client, timeout=timeout), session_id, None)


def read_recent_events(
    client: DshClient,
    session_id: str,
    *,
    max_messages: int = 12,
    timeout: float = 5.0,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    """Read a bounded recent raw-log window for compact inspection, never full history."""
    item = current_session(client, session_id, timeout=timeout)
    head = projection_seq(item)
    if head is None:
        raise DshError("session projection cursor is unavailable; cannot inspect a coherent recent window")
    if head < 0:
        return [], item, False
    page = _page(client, session_id, head, None, max_messages, timeout=timeout)
    return _event_records(page), item, page.get("hasMore") is True

def read_events_since(
    client: DshClient,
    session_id: str,
    after_seq: int,
    *,
    page_messages: int = DEFAULT_PAGE_MESSAGES,
    max_pages: int = MAX_HISTORY_PAGES,
    deadline: float | None = None,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    first_timeout = 20.0 if deadline is None else max(0.1, min(20.0, deadline - time.monotonic()))
    if deadline is not None and time.monotonic() >= deadline:
        raise DshError("wait observation deadline expired before session inspection")
    item = current_session(client, session_id, timeout=first_timeout)
    head = projection_seq(item)
    if head is None:
        raise DshError("session projection cursor is unavailable; cannot prove incremental continuity")
    if head < after_seq:
        raise DshGapError(f"supervision cursor seq {after_seq} is ahead of session cursor {head}")
    if head == after_seq:
        return [], head, item
    before: int | None = None
    pages: list[list[dict[str, Any]]] = []
    reached = False
    for _ in range(max_pages):
        remaining = 20.0 if deadline is None else max(0.2, min(20.0, deadline - time.monotonic()))
        if deadline is not None and remaining <= 0.2 and time.monotonic() >= deadline:
            break
        page = _page(client, session_id, head, before, page_messages, timeout=remaining)
        records = _event_records(page)
        if not records:
            break
        pages.append(records)
        oldest = min(int(event["seq"]) for event in records)
        if oldest <= after_seq + 1:
            reached = True
            break
        if page.get("hasMore") is not True:
            break
        before = oldest
    merged: dict[int, dict[str, Any]] = {}
    for chunk in reversed(pages):
        for event in chunk:
            seq = int(event["seq"])
            if after_seq < seq <= head:
                merged[seq] = event
    events = [merged[key] for key in sorted(merged)]
    if events and events[0]["seq"] != after_seq + 1:
        raise DshGapError(f"session history gap after seq {after_seq}; first available seq is {events[0]['seq']}")
    expected = after_seq + 1
    for event in events:
        if event["seq"] != expected:
            raise DshGapError(f"session history gap at seq {expected}")
        expected += 1
    if expected <= head and not reached:
        raise DshError(f"session history continuity could not be established within the read budget before seq {expected}")
    return events, head, item

def read_event_at(client: DshClient, session_id: str, seq: int) -> dict[str, Any]:
    if seq < 0:
        raise DshError("event sequence must be non-negative")
    page = _page(client, session_id, seq, None, 2)
    for event in _event_records(page):
        if event.get("seq") == seq:
            return event
    raise DshError(f"session event {seq} is unavailable")

def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("data")
    return value if isinstance(value, dict) else {}

def _source_rpc_id(event: dict[str, Any]) -> str | None:
    if event.get("type") != "user/message":
        return None
    source = _event_data(event).get("source")
    if not isinstance(source, dict) or source.get("kind") != "user":
        return None
    value = source.get("rpcId")
    return value if isinstance(value, str) else None

def _turn_from_data(event: dict[str, Any]) -> int | None:
    value = _event_data(event).get("turn")
    return value if isinstance(value, int) and value >= 0 else None

def _find_open_turn(events: Iterable[dict[str, Any]], before_or_at_seq: int) -> int | None:
    stack: list[int] = []
    for event in sorted((e for e in events if isinstance(e.get("seq"), int) and e["seq"] <= before_or_at_seq), key=lambda e: e["seq"]):
        if event.get("type") == "turn/start":
            turn = _turn_from_data(event)
            if turn is not None:
                stack.append(turn)
        elif event.get("type") == "turn/end":
            turn = _turn_from_data(event)
            if turn in stack:
                stack = [item for item in stack if item != turn]
    return stack[-1] if stack else None

def _find_enclosing_turn(client: DshClient, session_id: str, seq: int, deadline: float | None = None) -> int | None:
    before: int | None = None
    through = seq
    gathered: list[dict[str, Any]] = []
    for _ in range(20):
        remaining = 20.0 if deadline is None else max(0.1, min(20.0, deadline - time.monotonic()))
        if deadline is not None and time.monotonic() >= deadline:
            raise DshError("wait observation deadline expired while correlating the accepted request")
        page = _page(client, session_id, through, before, 20, timeout=remaining)
        events = _event_records(page)
        if not events:
            break
        gathered = events + gathered
        turn = _find_open_turn(gathered, seq)
        if turn is not None:
            return turn
        if page.get("hasMore") is not True:
            break
        before = min(int(e["seq"]) for e in events)
    return None

def _turn_reason_state(reason: Any) -> tuple[str, str | None]:
    if not isinstance(reason, dict):
        return "unknown", None
    kind = reason.get("kind")
    if kind == "completed":
        return "completed", "completed"
    if kind == "aborted":
        return "cancelled", "aborted"
    if kind == "blocked":
        return "blocked", "blocked"
    if kind == "error":
        return "failed", "error"
    if kind == "max-tokens":
        return "failed", "max-tokens"
    if kind == "interrupted":
        return "failed", "interrupted"
    return "unknown", str(kind) if kind is not None else None

def _safe_turn_reason_details(reason: Any) -> dict[str, Any]:
    if not isinstance(reason, dict):
        return {}
    kind = reason.get("kind")
    if kind == "aborted":
        cause = reason.get("reason")
        if isinstance(cause, dict) and isinstance(cause.get("kind"), str):
            return {"cancelCause": cause["kind"]}
    if kind == "error":
        failure = reason.get("error")
        if isinstance(failure, dict):
            clean: dict[str, Any] = {}
            for key in ("code", "status", "providerRetryAfterMs"):
                value = failure.get(key)
                if isinstance(value, (str, int)):
                    clean[key] = value
            return {"failure": clean} if clean else {}
    return {}

def _redact_text(text: str, client: DshClient) -> tuple[str, int]:
    count = 0
    secrets = {b64url(client.secret), client.cookie}
    for token in sorted((s for s in secrets if s), key=len, reverse=True):
        occurrences = text.count(token)
        if occurrences:
            text = text.replace(token, "[REDACTED:dsh-credential]")
            count += occurrences
    patterns = [
        re.compile(r"(?i)\b(authorization\s*:\s*bearer)\s+[^\s]+"),
        re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret)\s*[:=]\s*['\"]?([A-Za-z0-9_\-./+=]{12,})['\"]?"),
        re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    ]
    for pattern in patterns:
        def repl(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            if match.lastindex and match.lastindex >= 1:
                return f"{match.group(1)} [REDACTED:secret]"
            return "[REDACTED:secret]"
        text = pattern.sub(repl, text)
    return text, count

def _assistant_message(event: dict[str, Any]) -> dict[str, Any]:
    message = _event_data(event).get("message")
    return message if isinstance(message, dict) else {}

def _assistant_message_id(event: dict[str, Any]) -> str:
    value = _assistant_message(event).get("id")
    return value if isinstance(value, str) and value else f"seq:{event.get('seq')}"

def _assistant_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    content = _assistant_message(event).get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []

def _attachment_projection(block: dict[str, Any]) -> dict[str, Any]:
    attachment = block.get("attachment")
    if not isinstance(attachment, dict):
        return {"type": str(block.get("type")), "available": False}
    result: dict[str, Any] = {"type": str(block.get("type")), "available": True}
    for key in ("attachmentId", "mediaType", "name", "bytes", "width", "height"):
        value = attachment.get(key)
        if isinstance(value, (str, int)):
            result[key] = value
    return result

