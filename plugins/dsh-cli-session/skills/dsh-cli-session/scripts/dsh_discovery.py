#!/usr/bin/env python3
"""Bounded DSH session discovery with signed stale-detecting continuation cursors."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from dsh_transport import DshClient, DshError, b64url, b64url_decode, describe, sessions

DEFAULT_DISCOVERY_LIMIT = 20
MAX_DISCOVERY_LIMIT = 100
_CURSOR_VERSION = 1


def _digest_rows(rows: list[dict[str, Any]]) -> str:
    stable = [
        {
            "sessionId": row.get("sessionId"),
            "updatedAt": row.get("updatedAt"),
            "running": bool(row.get("running")),
            "cwd": row.get("cwd"),
        }
        for row in rows
    ]
    raw = json.dumps(stable, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cursor_key(secret: bytes) -> bytes:
    return hmac.new(secret, b"dsh-cli-session/discovery-cursor/v1", hashlib.sha256).digest()


def _encode_cursor(
    secret: bytes,
    *,
    offset: int,
    roster_hash: str,
    running_only: bool,
    cwd: str | None,
) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "offset": offset,
        "rosterHash": roster_hash,
        "runningOnly": running_only,
        "cwd": cwd,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    body = b64url(raw)
    sig = b64url(hmac.new(_cursor_key(secret), body.encode("ascii"), hashlib.sha256).digest())
    return f"dshd1.{body}.{sig}"


def _decode_cursor(
    secret: bytes,
    token: str,
    *,
    roster_hash: str,
    running_only: bool,
    cwd: str | None,
) -> int:
    try:
        prefix, body, signature = token.split(".", 2)
    except ValueError as exc:
        raise DshError("invalid discovery cursor") from exc
    if prefix != "dshd1":
        raise DshError("unsupported discovery cursor version")
    expected = b64url(hmac.new(_cursor_key(secret), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise DshError("forged or corrupt discovery cursor")
    try:
        payload = json.loads(b64url_decode(body))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DshError("invalid discovery cursor payload") from exc
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise DshError("unsupported discovery cursor payload")
    if payload.get("rosterHash") != roster_hash:
        raise DshError("stale discovery cursor: the DSH session roster changed")
    if payload.get("runningOnly") is not running_only or payload.get("cwd") != cwd:
        raise DshError("discovery cursor does not match the current filters")
    offset = payload.get("offset")
    if not isinstance(offset, int) or offset < 0:
        raise DshError("invalid discovery cursor offset")
    return offset


def paginate_sessions(
    client: DshClient,
    secret: bytes,
    limit: int = DEFAULT_DISCOVERY_LIMIT,
    cursor: str | None = None,
    running_only: bool = False,
    cwd: str | None = None,
) -> dict[str, Any]:
    if not isinstance(limit, int) or limit < 1 or limit > MAX_DISCOVERY_LIMIT:
        raise DshError(f"limit must be between 1 and {MAX_DISCOVERY_LIMIT}")
    rows = sessions(client)
    if running_only:
        rows = [row for row in rows if row.get("running") is True]
    if cwd is not None:
        rows = [row for row in rows if row.get("cwd") == cwd]
    roster_hash = _digest_rows(rows)
    offset = 0 if cursor is None else _decode_cursor(
        secret,
        cursor,
        roster_hash=roster_hash,
        running_only=running_only,
        cwd=cwd,
    )
    if offset > len(rows):
        raise DshError("stale discovery cursor: offset is past the current roster")
    page = rows[offset: offset + limit]
    next_offset = offset + len(page)
    next_cursor = None
    if next_offset < len(rows):
        next_cursor = _encode_cursor(
            secret,
            offset=next_offset,
            roster_hash=roster_hash,
            running_only=running_only,
            cwd=cwd,
        )
    return {
        "sessions": [describe(row) for row in page],
        "count": len(page),
        "totalMatching": len(rows),
        "nextCursor": next_cursor,
        "hasMore": next_cursor is not None,
    }
