#!/usr/bin/env python3
"""Bounded reads of assignment-owned outward message blocks and explicitly published deliverables."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any

from dsh_transport import DshClient, DshError
from dsh_store import get_assignment
from dsh_history import (
    _assistant_blocks,
    _event_data,
    _redact_text,
    _turn_from_data,
    read_event_at,
)
from dsh_wait import reconcile_assignment_state

DEFAULT_EVIDENCE_CHARS = 6000
MAX_EVIDENCE_CHARS = 20000
MAX_EVIDENCE_FILE_BYTES = 8 * 1024 * 1024

_MESSAGE_RE = re.compile(r"^message:(\d+):(\d+)$")
_ARTIFACT_RE = re.compile(r"^artifact:(\d+):(\d+)$")


def _validate_bounds(start: int, max_chars: int) -> None:
    if not isinstance(start, int) or start < 0:
        raise DshError("start must be a non-negative integer")
    if not isinstance(max_chars, int) or max_chars < 1 or max_chars > MAX_EVIDENCE_CHARS:
        raise DshError(f"max_chars must be between 1 and {MAX_EVIDENCE_CHARS}")


def _owned_event(assignment: dict[str, Any], event: dict[str, Any]) -> None:
    seq = event.get("seq")
    claim = assignment.get("claimSeq")
    if not isinstance(seq, int) or not isinstance(claim, int) or seq < claim:
        raise DshError("evidence reference does not belong to this assignment")
    terminal = assignment.get("terminalSeq")
    if isinstance(terminal, int) and seq > terminal:
        raise DshError("evidence reference is after this assignment's terminal boundary")
    assignment_turn = assignment.get("turn")
    event_turn = _turn_from_data(event)
    if isinstance(assignment_turn, int) and isinstance(event_turn, int) and assignment_turn != event_turn:
        raise DshError("evidence reference belongs to another turn")


def _slice_text(
    *,
    ref: str,
    kind: str,
    text: str,
    start: int,
    max_chars: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if start > len(text):
        raise DshError("start is past the end of the evidence")
    end = min(len(text), start + max_chars)
    return {
        "ok": True,
        "ref": ref,
        "kind": kind,
        "start": start,
        "end": end,
        "totalChars": len(text),
        "hasMore": end < len(text),
        "nextStart": end if end < len(text) else None,
        "text": text[start:end],
        **(extra or {}),
    }


def _resolve_published_path(assignment: dict[str, Any], published: str) -> Path:
    path = Path(published).expanduser()
    if path.is_absolute():
        return path
    cwd = assignment.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise DshError("assignment has no working directory for this relative deliverable")
    root = Path(cwd).expanduser().resolve(strict=True)
    candidate = root / path
    resolved = candidate.resolve(strict=True)
    try:
        common = Path(os.path.commonpath([str(root), str(resolved)]))
    except ValueError as exc:
        raise DshError("published relative artifact no longer resolves inside the assignment workspace") from exc
    if common != root:
        raise DshError("published relative artifact no longer resolves inside the assignment workspace")
    return candidate


def _read_regular_file(path: Path) -> tuple[bytes, os.stat_result]:
    if path.is_symlink():
        raise DshError("published artifact is currently a symbolic link; refusing stale path")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DshError(f"cannot open published artifact: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise DshError("published artifact is no longer a regular file")
        if info.st_size > MAX_EVIDENCE_FILE_BYTES:
            raise DshError(
                f"published artifact is {info.st_size} bytes; maximum evidence file size is {MAX_EVIDENCE_FILE_BYTES}"
            )
        chunks: list[bytes] = []
        remaining = info.st_size + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_EVIDENCE_FILE_BYTES:
            raise DshError("published artifact grew beyond the evidence size limit while being read")
        return data, info
    finally:
        os.close(fd)


def read_evidence(
    client: DshClient,
    dsh_home: Path,
    assignment_id: str,
    evidence_ref: str,
    start: int = 0,
    max_chars: int = DEFAULT_EVIDENCE_CHARS,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    _validate_bounds(start, max_chars)
    assignment = get_assignment(dsh_home, assignment_id)
    if not isinstance(assignment.get("claimSeq"), int):
        assignment, _ = reconcile_assignment_state(client, dsh_home, assignment)
    if not isinstance(assignment.get("claimSeq"), int):
        raise DshError("assignment has not yet been durably claimed; no assignment-owned evidence is available")

    message_match = _MESSAGE_RE.match(evidence_ref)
    if message_match:
        seq = int(message_match.group(1))
        block_index = int(message_match.group(2))
        event = read_event_at(client, assignment["sessionId"], seq)
        _owned_event(assignment, event)
        if event.get("type") != "assistant/message":
            raise DshError("message evidence ref does not point to a finalized assistant message")
        blocks = _assistant_blocks(event)
        if block_index >= len(blocks):
            raise DshError("message evidence block index is out of range")
        block = blocks[block_index]
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            raise DshError("message evidence ref does not point to outward text")
        text, redactions = _redact_text(block["text"], client)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if expected_sha256 is not None and expected_sha256 != digest:
            raise DshError("message evidence changed from the expected SHA-256")
        return _slice_text(
            ref=evidence_ref,
            kind="message",
            text=text,
            start=start,
            max_chars=max_chars,
            extra={
                "seq": seq,
                "block": block_index,
                "sha256": digest,
                "redactions": redactions,
            },
        )

    artifact_match = _ARTIFACT_RE.match(evidence_ref)
    if artifact_match:
        seq = int(artifact_match.group(1))
        index = int(artifact_match.group(2))
        event = read_event_at(client, assignment["sessionId"], seq)
        _owned_event(assignment, event)
        if event.get("type") != "deliverables/presented":
            raise DshError("artifact evidence ref does not point to a published deliverable")
        files = _event_data(event).get("files")
        if not isinstance(files, list) or index >= len(files) or not isinstance(files[index], dict):
            raise DshError("artifact evidence index is out of range")
        item = files[index]
        published = item.get("path")
        if not isinstance(published, str) or not published:
            raise DshError("published artifact has no valid path")
        path = _resolve_published_path(assignment, published)
        data, info = _read_regular_file(path)
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None and expected_sha256 != digest:
            raise DshError("published artifact changed from the expected SHA-256")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DshError("published artifact is not UTF-8 text; model-visible evidence reads are text-only") from exc
        return _slice_text(
            ref=evidence_ref,
            kind="artifact",
            text=text,
            start=start,
            max_chars=max_chars,
            extra={
                "seq": seq,
                "index": index,
                "publishedPath": published,
                "sha256": digest,
                "bytes": len(data),
                "mtimeNs": info.st_mtime_ns,
                **({"description": item["description"]} if isinstance(item.get("description"), str) else {}),
            },
        )

    raise DshError("unsupported evidence ref; expected message:<seq>:<block> or artifact:<seq>:<index>")
