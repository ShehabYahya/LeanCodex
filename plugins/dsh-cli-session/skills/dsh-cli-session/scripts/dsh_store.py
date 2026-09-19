#!/usr/bin/env python3
"""Durable assignment identity, admission, and signed supervision cursors."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

from dsh_transport import (
    DshClient, DshError, DshTransportError, b64url, b64url_decode, projection_seq,
)

STORE_VERSION = 1
CURSOR_VERSION = 1
MAX_ASSIGNMENTS = 512
TERMINAL_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
_TERMINAL_STATES = {"completed", "cancelled", "blocked", "failed", "rejected"}
_STORE_LOCK = threading.RLock()

def _store_path(dsh_home: Path) -> Path:
    return dsh_home / ".dsh-cli-session" / "assignments-v1.json"

def _empty_store() -> dict[str, Any]:
    return {"version": STORE_VERSION, "assignments": {}, "submissionKeys": {}}

def _prune_store(data: dict[str, Any], now_ms: int | None = None) -> int:
    """Bound retained correlation state without ever discarding active assignments."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    assignments = data["assignments"]
    submission_keys = data["submissionKeys"]
    removed: set[str] = set()
    cutoff = now - TERMINAL_RETENTION_MS

    for assignment_id, assignment in list(assignments.items()):
        if not isinstance(assignment, dict) or assignment.get("state") not in _TERMINAL_STATES:
            continue
        touched = assignment.get("updatedAt", assignment.get("admittedAt", assignment.get("createdAt", now)))
        if isinstance(touched, int) and touched < cutoff:
            assignments.pop(assignment_id, None)
            removed.add(assignment_id)

    if len(assignments) > MAX_ASSIGNMENTS:
        terminal = sorted(
            (
                (
                    int(value.get("updatedAt", value.get("admittedAt", value.get("createdAt", now)))),
                    assignment_id,
                )
                for assignment_id, value in assignments.items()
                if isinstance(value, dict) and value.get("state") in _TERMINAL_STATES
            ),
            key=lambda pair: pair[0],
        )
        for _, assignment_id in terminal:
            if len(assignments) <= MAX_ASSIGNMENTS:
                break
            assignments.pop(assignment_id, None)
            removed.add(assignment_id)

    if removed:
        for slot, assignment_id in list(submission_keys.items()):
            if assignment_id in removed or assignment_id not in assignments:
                submission_keys.pop(slot, None)
    return len(removed)

def _load_store_unlocked(dsh_home: Path) -> dict[str, Any]:
    path = _store_path(dsh_home)
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DshError(f"cannot read DSH supervision store: {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
        raise DshError(f"unsupported DSH supervision store format: {path}")
    if not isinstance(data.get("assignments"), dict) or not isinstance(data.get("submissionKeys"), dict):
        raise DshError(f"invalid DSH supervision store: {path}")
    return data

def _save_store_unlocked(dsh_home: Path, data: dict[str, Any]) -> None:
    path = _store_path(dsh_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix="assignments-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

@contextmanager
def _store_guard(dsh_home: Path) -> Iterator[None]:
    """Serialize assignment-store read/modify/write across threads and POSIX processes."""
    directory = _store_path(dsh_home).parent
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    lock_path = directory / ".assignments.lock"
    with _STORE_LOCK:
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

def load_store(dsh_home: Path) -> dict[str, Any]:
    with _store_guard(dsh_home):
        return _load_store_unlocked(dsh_home)

def save_store(dsh_home: Path, data: dict[str, Any]) -> None:
    with _store_guard(dsh_home):
        _save_store_unlocked(dsh_home, data)

def _submission_slot(submission_key: str) -> str:
    # Submission keys are globally stable within this plugin store. Binding the
    # stored assignment to session + payload makes an accidental session switch
    # fail instead of silently admitting duplicate work elsewhere.
    return hashlib.sha256(submission_key.encode("utf-8")).hexdigest()

def _payload_hash(prompt: str, mode: str) -> str:
    return hashlib.sha256((mode + "\0" + prompt).encode("utf-8")).hexdigest()

def prepare_assignment(
    dsh_home: Path,
    session: dict[str, Any],
    prompt: str,
    mode: str,
    submission_key: str,
) -> tuple[dict[str, Any], bool]:
    if not submission_key or len(submission_key) > 256:
        raise DshError("submission_key must contain 1 to 256 characters")
    session_id = str(session.get("sessionId") or "")
    if not session_id:
        raise DshError("selected DSH session has no id")
    payload_hash = _payload_hash(prompt, mode)
    slot = _submission_slot(submission_key)
    with _store_guard(dsh_home):
        store = _load_store_unlocked(dsh_home)
        pruned = _prune_store(store)
        if pruned:
            _save_store_unlocked(dsh_home, store)
        existing_id = store["submissionKeys"].get(slot)
        if isinstance(existing_id, str):
            existing = store["assignments"].get(existing_id)
            if not isinstance(existing, dict):
                raise DshError("supervision store contains a broken submission-key mapping")
            if existing.get("sessionId") != session_id or existing.get("payloadHash") != payload_hash:
                raise DshError("submission_key was already used for a different session or payload")
            return existing, False

        if len(store["assignments"]) >= MAX_ASSIGNMENTS:
            raise DshError(
                f"supervision store reached its {MAX_ASSIGNMENTS}-assignment retention limit; "
                "finish or explicitly clear old active work before admitting another assignment"
            )
        assignment_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        now = int(time.time() * 1000)
        assignment = {
            "version": 1,
            "assignmentId": assignment_id,
            "sessionId": session_id,
            "payloadHash": payload_hash,
            "submissionKeyHash": hashlib.sha256(submission_key.encode("utf-8")).hexdigest(),
            "nativeRequestId": request_id,
            "mode": mode,
            "createdAt": now,
            "updatedAt": now,
            "admissionState": "prepared",
            "initialSeq": projection_seq(session) if projection_seq(session) is not None else -1,
            "cwd": session.get("cwd"),
            "claimSeq": None,
            "turn": None,
            "terminalSeq": None,
            "terminalReason": None,
            "state": "pending_admission",
        }
        store["assignments"][assignment_id] = assignment
        store["submissionKeys"][slot] = assignment_id
        _save_store_unlocked(dsh_home, store)
        return assignment, True

def update_assignment(dsh_home: Path, assignment: dict[str, Any]) -> None:
    with _store_guard(dsh_home):
        store = _load_store_unlocked(dsh_home)
        aid = assignment.get("assignmentId")
        if not isinstance(aid, str) or aid not in store["assignments"]:
            raise DshError("assignment is not registered in the supervision store")
        assignment["updatedAt"] = int(time.time() * 1000)
        store["assignments"][aid] = assignment
        _prune_store(store)
        _save_store_unlocked(dsh_home, store)

def get_assignment(dsh_home: Path, assignment_id: str) -> dict[str, Any]:
    with _store_guard(dsh_home):
        store = _load_store_unlocked(dsh_home)
        value = store["assignments"].get(assignment_id)
        if not isinstance(value, dict):
            raise DshError(f"assignment not found: {assignment_id}")
        return value

def admit_assignment(client: DshClient, dsh_home: Path, assignment: dict[str, Any], prompt: str) -> dict[str, Any]:
    state = assignment.get("admissionState")
    if state == "accepted":
        return assignment
    request = {
        "requestId": assignment["nativeRequestId"],
        "sessionId": assignment["sessionId"],
        "mode": assignment["mode"],
        "content": [{"type": "text", "text": prompt}],
    }
    try:
        value = client.rpc("session/prompt", "request", request)
    except DshTransportError:
        assignment["admissionState"] = "admission_unknown"
        assignment["state"] = "admission_unknown"
        update_assignment(dsh_home, assignment)
        raise
    except DshError:
        assignment["admissionState"] = "rejected"
        assignment["state"] = "rejected"
        update_assignment(dsh_home, assignment)
        raise
    if not isinstance(value, dict) or value.get("accepted") is not True:
        assignment["admissionState"] = "admission_unknown"
        assignment["state"] = "admission_unknown"
        update_assignment(dsh_home, assignment)
        raise DshError("DSH session/prompt returned no accepted receipt")
    assignment["admissionState"] = "accepted"
    assignment["admittedAt"] = int(time.time() * 1000)
    assignment["state"] = "queued"
    update_assignment(dsh_home, assignment)
    return assignment

def _cursor_key(secret: bytes) -> bytes:
    return hmac.new(secret, b"dsh-cli-session/cursor/v1", hashlib.sha256).digest()

def encode_cursor(secret: bytes, assignment: dict[str, Any], after_seq: int, partial: dict[str, int] | None = None) -> str:
    payload: dict[str, Any] = {
        "v": CURSOR_VERSION,
        "assignmentId": assignment["assignmentId"],
        "sessionId": assignment["sessionId"],
        "afterSeq": after_seq,
    }
    if partial is not None:
        payload["partial"] = partial
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    body = b64url(raw)
    sig = b64url(hmac.new(_cursor_key(secret), body.encode("ascii"), hashlib.sha256).digest())
    return f"dshc1.{body}.{sig}"

def decode_cursor(secret: bytes, token: str, assignment: dict[str, Any]) -> tuple[int, dict[str, int] | None]:
    try:
        prefix, body, signature = token.split(".", 2)
    except ValueError as exc:
        raise DshError("invalid supervision cursor") from exc
    if prefix != "dshc1":
        raise DshError("unsupported supervision cursor version")
    expected = b64url(hmac.new(_cursor_key(secret), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise DshError("forged or corrupted supervision cursor")
    try:
        payload = json.loads(b64url_decode(body))
    except json.JSONDecodeError as exc:
        raise DshError("invalid supervision cursor payload") from exc
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise DshError("unsupported supervision cursor payload")
    if payload.get("assignmentId") != assignment.get("assignmentId") or payload.get("sessionId") != assignment.get("sessionId"):
        raise DshError("cursor belongs to another assignment or session")
    after = payload.get("afterSeq")
    if not isinstance(after, int) or after < -1:
        raise DshError("invalid supervision cursor sequence")
    partial = payload.get("partial")
    if partial is not None:
        if not isinstance(partial, dict):
            raise DshError("invalid supervision cursor continuation")
        required = {"seq", "block", "offset"}
        if not required.issubset(partial) or not all(isinstance(partial[key], int) and partial[key] >= 0 for key in required):
            raise DshError("invalid supervision cursor continuation")
        partial = {key: int(partial[key]) for key in required}
    return after, partial

def initial_cursor(secret: bytes, assignment: dict[str, Any]) -> str:
    seq = assignment.get("initialSeq")
    if not isinstance(seq, int) or seq < -1:
        raise DshError("assignment has no reliable initial session cursor")
    return encode_cursor(secret, assignment, seq)

