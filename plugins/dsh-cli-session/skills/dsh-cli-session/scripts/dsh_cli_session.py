#!/usr/bin/env python3
"""Prompt an already-running local DSH web session through its Remote API."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class DshError(RuntimeError):
    pass


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def decode_secret(value: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, base64.binascii.Error) as exc:
        raise DshError("browser-session secret is not valid base64url") from exc
    if len(raw) != 32:
        raise DshError("browser-session secret is not 32 bytes")
    return raw


def read_secret(dsh_home: Path) -> bytes:
    path = dsh_home / ".credentials.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DshError(f"cannot read DSH credentials: {path}: {exc}") from exc

    # Parse only the browser-session record; do not add a YAML dependency or
    # accidentally accept a refs/API-key value.
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

    def rpc(self, method: str, argument_name: str, argument: object) -> object:
        request_id = str(uuid.uuid4())
        envelope = {
            "type": "client-request",
            "rpcId": request_id,
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
            with urllib.request.urlopen(request, timeout=20) as response:
                wire = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise DshError(f"HTTP {exc.code} from DSH {method}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise DshError(f"cannot reach DSH at {self.base_url}: {exc.reason}") from exc

        result = wire.get("result") if isinstance(wire, dict) else None
        if not isinstance(result, dict):
            raise DshError(f"invalid DSH response for {method}")
        if result.get("ok") is not True:
            error = result.get("error") or {}
            code = error.get("code", "unknown")
            message = error.get("message", "request failed")
            raise DshError(f"DSH {method} failed [{code}]: {message}")
        return result.get("value")


def sessions(client: DshClient) -> list[dict]:
    value = client.rpc("session/list", "_request", {})
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise DshError("DSH session/list returned no session items")
    return [item for item in value["items"] if isinstance(item, dict)]


def projection(item: dict) -> dict:
    return item.get("projections", {}).get("values", {})


def describe(item: dict) -> dict:
    values = projection(item)
    return {
        "sessionId": item.get("sessionId"),
        "running": bool(item.get("running")),
        "cwd": item.get("cwd"),
        "title": values.get("title"),
        "updatedAt": item.get("updatedAt"),
    }


def choose_session(items: list[dict], session_id: str | None, cwd: str | None) -> dict:
    if session_id is not None:
        matches = [item for item in items if item.get("sessionId") == session_id]
        if len(matches) != 1:
            raise DshError(f"session id not found: {session_id}")
        return matches[0]

    candidates = [item for item in items if item.get("running") is True]
    if cwd is not None:
        candidates = [item for item in candidates if item.get("cwd") == cwd]
    if len(candidates) != 1:
        details = "; ".join(
            f"{d['sessionId']} running={d['running']} cwd={d['cwd']} title={d['title']!r}"
            for d in map(describe, candidates or items)
        )
        if not candidates:
            raise DshError("no unambiguous running DSH session; use --session-id. " + details)
        raise DshError("multiple running DSH sessions; use --session-id. " + details)
    return candidates[0]


def find_response(item: dict, marker: str) -> str | None:
    outline = projection(item).get("turnOutline", [])
    if not isinstance(outline, list):
        return None
    for turn in reversed(outline):
        if not isinstance(turn, dict):
            continue
        prompt = str(turn.get("prompt", ""))
        response = turn.get("response")
        if marker in prompt and isinstance(response, str) and response:
            return response
    return None


def wait_for_response(client: DshClient, session_id: str, prompt: str, seconds: int) -> str | None:
    marker = prompt[:80]
    deadline = time.monotonic() + seconds
    while True:
        item = choose_session(sessions(client), session_id, None)
        response = find_response(item, marker)
        if response is not None:
            return response
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(2.0, remaining))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Prompt a live local DSH web session through its CLI API.")
    p.add_argument("prompt", nargs="?", help="prompt text to queue or steer")
    p.add_argument("--list", action="store_true", help="list sessions and exit")
    p.add_argument("--session-id", help="exact session id; required when selection is ambiguous")
    p.add_argument("--cwd", help="when auto-selecting, require this session cwd")
    p.add_argument("--mode", choices=("queue", "steer"), default="queue")
    p.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="wait for and print the assistant response")
    p.add_argument("--base-url", default="http://127.0.0.1:3080")
    p.add_argument("--dsh-home", default=os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
    p.add_argument("--json", action="store_true", help="emit machine-readable output")
    p.add_argument("--dry-run", action="store_true", help="select and display the target without sending")
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        client = DshClient(args.base_url, read_secret(Path(args.dsh_home)))
        items = sessions(client)
        if args.list:
            rows = [describe(item) for item in items]
            print(json.dumps(rows, indent=2) if args.json else "\n".join(
                f"{row['sessionId']}\trunning={row['running']}\tcwd={row['cwd']}\ttitle={row['title']}"
                for row in rows
            ))
            return 0
        if not args.prompt or not args.prompt.strip():
            raise DshError("prompt text is required unless --list is used")
        target = choose_session(items, args.session_id, args.cwd)
        target_id = str(target["sessionId"])
        if args.dry_run:
            output = {"target": describe(target), "mode": args.mode, "prompt": args.prompt}
            print(json.dumps(output, indent=2) if args.json else f"target={target_id} mode={args.mode} title={projection(target).get('title')!r}")
            return 0

        request_id = str(uuid.uuid4())
        client.rpc("session/prompt", "request", {
            "requestId": request_id,
            "sessionId": target_id,
            "mode": args.mode,
            "content": [{"type": "text", "text": args.prompt}],
        })
        output: dict[str, object] = {
            "accepted": True,
            "requestId": request_id,
            "session": describe(target),
            "mode": args.mode,
        }
        if args.wait > 0:
            response = wait_for_response(client, target_id, args.prompt, args.wait)
            output["response"] = response
            output["completed"] = response is not None
        if args.json:
            print(json.dumps(output, indent=2))
        else:
            print(f"accepted session={target_id} mode={args.mode} request_id={request_id}")
            if args.wait > 0:
                if output["completed"]:
                    print("--- assistant response ---")
                    print(output["response"])
                else:
                    print(f"timed out after {args.wait}s; prompt was accepted but no completed response was observed")
        return 0
    except DshError as exc:
        print(f"dsh-cli-session: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
