#!/usr/bin/env python3
"""Loopback-only DeepSeek Harness Remote transport and session discovery primitives."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import ntpath
from pathlib import Path, PurePosixPath, PureWindowsPath
import posixpath
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

class DshError(RuntimeError):
    pass

class DshTransportError(DshError):
    """A request may have reached DSH but no authoritative response was seen."""

class DshGapError(DshError):
    pass

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

def b64url_decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, base64.binascii.Error) as exc:
        raise DshError("invalid base64url value") from exc

def decode_secret(value: str) -> bytes:
    raw = b64url_decode(value)
    if len(raw) != 32:
        raise DshError("browser-session secret is not 32 bytes")
    return raw

def read_secret(dsh_home: Path, credentials_file: str | None = None) -> bytes:
    if credentials_file:
        path = Path(credentials_file).expanduser()
        if not path.is_absolute():
            raise DshError("LEANCODEX_CREDENTIALS_FILE must be an absolute local path")
    else:
        path = dsh_home / ".credentials.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DshError(f"cannot read DSH credentials: {path}: {exc}") from exc

    in_record = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "client-connection/browser-session:":
            in_record = True
            continue
        if in_record and stripped.startswith("secret:"):
            return decode_secret(stripped.split(":", 1)[1].strip())
        if in_record and stripped and not line.startswith(" "):
            break
    raise DshError(f"browser-session secret not found in {path}")

def local_authority(base_url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise DshError("--base-url must be an http URL with a hostname")
    host = parsed.hostname.lower().rstrip(".")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise DshError("refusing non-loopback --base-url; DSH credentials stay local")
    authority = parsed.netloc or host
    return parsed.geturl().rstrip("/"), authority

class DshClient:
    def __init__(self, base_url: str, secret: bytes):
        self.base_url, self.authority = local_authority(base_url)
        self.secret = secret
        now = int(time.time() * 1000)
        expires = now + 30 * 24 * 60 * 60 * 1000
        payload = {
            "version": 1,
            "authority": self.authority,
            "issuedAt": now,
            "expiresAt": expires,
        }
        body = b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = b64url(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
        name = "dsh-auth-" + b64url(hashlib.sha256(self.authority.encode("utf-8")).digest())
        self.cookie = f"{name}=v1.{body}.{signature}"

    def rpc(self, method: str, argument_name: str, argument: object, timeout: float = 20.0) -> object:
        rpc_id = str(uuid.uuid4())
        envelope = {
            "type": "client-request",
            "rpcId": rpc_id,
            "method": method,
            "payload": {"args": {argument_name: argument}},
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/{method}",
            data=json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Cookie": self.cookie,
                "Host": self.authority,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as response:
                try:
                    wire = json.load(response)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise DshTransportError(f"cannot confirm DSH {method} outcome: malformed response") from exc
        except urllib.error.HTTPError as exc:
            # Do not echo arbitrary gateway bodies into model-visible tool errors.
            # A 5xx/timeout can happen after a mutating request reached the Host,
            # so callers must reconcile instead of assuming the mutation failed.
            if exc.code >= 500 or exc.code == 408:
                raise DshTransportError(f"cannot confirm DSH {method} outcome: HTTP {exc.code}") from exc
            raise DshError(f"HTTP {exc.code} from DSH {method}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DshTransportError(f"cannot confirm DSH {method} outcome at {self.base_url}: {exc}") from exc

        result = wire.get("result") if isinstance(wire, dict) else None
        if not isinstance(result, dict):
            raise DshTransportError(f"cannot confirm DSH {method} outcome: invalid response envelope")
        if result.get("ok") is not True:
            error = result.get("error") or {}
            code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
            message = error.get("message", "request failed") if isinstance(error, dict) else "request failed"
            raise DshError(f"DSH {method} failed [{code}]: {message}")
        return result.get("value")

def sessions(client: DshClient, timeout: float = 20.0) -> list[dict[str, Any]]:
    value = client.rpc("session/list", "_request", {}, timeout=timeout)
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise DshError("DSH session/list returned no session items")
    return [item for item in value["items"] if isinstance(item, dict)]

def projection(item: dict[str, Any]) -> dict[str, Any]:
    values = item.get("projections", {}).get("values", {})
    return values if isinstance(values, dict) else {}

def projection_seq(item: dict[str, Any]) -> int | None:
    projections = item.get("projections")
    if not isinstance(projections, dict):
        return None
    value = projections.get("asOfSeq")
    return value if isinstance(value, int) and value >= -1 else None

def _inbox_has_request(item: dict[str, Any], request_id: str | None) -> bool:
    if not isinstance(request_id, str) or not request_id:
        return False
    inbox = projection(item).get("inbox")
    if not isinstance(inbox, dict):
        return False
    for target in ("next-turn", "next-step"):
        messages = inbox.get(target)
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict):
                continue
            source = message.get("source")
            if isinstance(source, dict) and source.get("kind") == "user" and source.get("rpcId") == request_id:
                return True
    return False

def describe(item: dict[str, Any]) -> dict[str, Any]:
    values = projection(item)
    return {
        "sessionId": item.get("sessionId"),
        "running": bool(item.get("running")),
        "cwd": item.get("cwd"),
        "title": values.get("title"),
        "updatedAt": item.get("updatedAt"),
        "projectionSeq": projection_seq(item),
    }

def _bounded_candidates(items: list[dict[str, Any]], maximum: int = 5) -> str:
    shown = [describe(item) for item in items[:maximum]]
    detail = "; ".join(
        f"{row['sessionId']} running={row['running']} cwd={row['cwd']} title={row['title']!r}"
        for row in shown
    )
    if len(items) > maximum:
        detail += f"; … {len(items) - maximum} more"
    return detail

def path_is_absolute(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _cwd_key(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    if PureWindowsPath(value).is_absolute():
        return ("windows", ntpath.normcase(ntpath.normpath(value)))
    if PurePosixPath(value).is_absolute():
        return ("posix", posixpath.normpath(value))
    return ("raw", value)


def choose_session(items: list[dict[str, Any]], session_id: str | None, cwd: str | None) -> dict[str, Any]:
    if session_id is not None:
        matches = [item for item in items if item.get("sessionId") == session_id]
        if len(matches) != 1:
            raise DshError(f"session id not found: {session_id}")
        return matches[0]

    candidates = [item for item in items if item.get("running") is True]
    if cwd is not None:
        requested_cwd = _cwd_key(cwd)
        candidates = [item for item in candidates if _cwd_key(item.get("cwd")) == requested_cwd]
    if len(candidates) != 1:
        visible = candidates or items
        detail = _bounded_candidates(visible)
        if not candidates:
            raise DshError("no unambiguous running DSH session; use session_id. " + detail)
        raise DshError("multiple running DSH sessions; use session_id. " + detail)
    return candidates[0]

