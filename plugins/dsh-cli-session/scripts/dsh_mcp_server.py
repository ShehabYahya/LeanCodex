#!/usr/bin/env python3
"""Small MCP wrapper around the bundled loopback-only DSH client."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP


_ROOT = Path(__file__).resolve().parent.parent
_CLI_PATH = _ROOT / "skills" / "dsh-cli-session" / "scripts" / "dsh_cli_session.py"
_SPEC = importlib.util.spec_from_file_location("dsh_cli_session_impl", _CLI_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - packaging failure
    raise RuntimeError(f"cannot load bundled DSH client: {_CLI_PATH}")
_CLIENT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CLIENT)


mcp = FastMCP(
    "DSH CLI Session",
    instructions=(
        "Operate only on an already-running loopback DSH web server. "
        "Never expose credentials or send a prompt without an explicit tool call."
    ),
)


def _client(base_url: str, dsh_home: str | None) -> Any:
    home = Path(dsh_home).expanduser() if dsh_home else Path.home() / ".dsh"
    return _CLIENT.DshClient(base_url, _CLIENT.read_secret(home))


def _error(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": str(exc)}


def _absolute_directory(value: str, label: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    if not path.is_dir():
        raise ValueError(f"{label} must be an existing directory: {path}")
    return str(path)


@mcp.tool()
def dsh_list_sessions(
    base_url: str = "http://127.0.0.1:3080",
    dsh_home: str | None = None,
) -> dict[str, Any]:
    """List live DSH sessions without sending or changing anything."""
    try:
        client = _client(base_url, dsh_home)
        rows = [_CLIENT.describe(item) for item in _CLIENT.sessions(client)]
        return {"ok": True, "sessions": rows}
    except _CLIENT.DshError as exc:
        return _error(exc)


@mcp.tool()
def dsh_inspect_session(
    session_id: str,
    base_url: str = "http://127.0.0.1:3080",
    dsh_home: str | None = None,
) -> dict[str, Any]:
    """Inspect one exact DSH session by id, without sending a prompt."""
    try:
        client = _client(base_url, dsh_home)
        item = _CLIENT.choose_session(_CLIENT.sessions(client), session_id, None)
        return {"ok": True, "session": _CLIENT.describe(item)}
    except _CLIENT.DshError as exc:
        return _error(exc)


@mcp.tool()
def dsh_add_project(
    path: str,
    base_url: str = "http://127.0.0.1:3080",
    dsh_home: str | None = None,
) -> dict[str, Any]:
    """Register an existing local directory as a DSH project/workspace."""
    try:
        project_path = _absolute_directory(path, "path")
        client = _client(base_url, dsh_home)
        value = client.rpc("workspace/create", "request", {"path": project_path})
        if not isinstance(value, dict) or not isinstance(value.get("workspace"), dict):
            raise _CLIENT.DshError("DSH workspace/create returned no workspace")
        return {
            "ok": True,
            "created": bool(value.get("created")),
            "project": value["workspace"],
        }
    except (ValueError, _CLIENT.DshError) as exc:
        return _error(exc)


@mcp.tool()
def dsh_new_session(
    workspace_id: str | None = None,
    cwd: str | None = None,
    agent_preset: str | None = None,
    base_url: str = "http://127.0.0.1:3080",
    dsh_home: str | None = None,
) -> dict[str, Any]:
    """Create a new ordinary DSH session in a project or absolute working directory."""
    if workspace_id and cwd:
        return {"ok": False, "error": "provide workspace_id or cwd, not both"}
    try:
        request: dict[str, str] = {}
        if workspace_id:
            request["workspaceId"] = workspace_id
        elif cwd:
            request["cwd"] = _absolute_directory(cwd, "cwd")
        if agent_preset:
            request["agentPreset"] = agent_preset
        client = _client(base_url, dsh_home)
        value = client.rpc("session/create", "request", request)
        if not isinstance(value, dict) or not isinstance(value.get("sessionId"), str):
            raise _CLIENT.DshError("DSH session/create returned no session id")
        result: dict[str, Any] = {
            "ok": True,
            "sessionId": value["sessionId"],
        }
        if value.get("agentPreset") is not None:
            result["agentPreset"] = value["agentPreset"]
        return result
    except (ValueError, _CLIENT.DshError) as exc:
        return _error(exc)


@mcp.tool()
def dsh_send_prompt(
    prompt: str,
    session_id: str | None = None,
    cwd: str | None = None,
    mode: Literal["queue", "steer"] = "queue",
    wait_seconds: int = 0,
    base_url: str = "http://127.0.0.1:3080",
    dsh_home: str | None = None,
) -> dict[str, Any]:
    """Queue or explicitly steer one prompt and optionally wait for its response."""
    if not prompt.strip():
        return {"ok": False, "error": "prompt text is required"}
    if wait_seconds < 0:
        return {"ok": False, "error": "wait_seconds must be non-negative"}
    try:
        client = _client(base_url, dsh_home)
        target = _CLIENT.choose_session(_CLIENT.sessions(client), session_id, cwd)
        target_id = str(target["sessionId"])
        request_id = str(_CLIENT.uuid.uuid4())
        client.rpc("session/prompt", "request", {
            "requestId": request_id,
            "sessionId": target_id,
            "mode": mode,
            "content": [{"type": "text", "text": prompt}],
        })
        result: dict[str, Any] = {
            "ok": True,
            "accepted": True,
            "requestId": request_id,
            "session": _CLIENT.describe(target),
            "mode": mode,
        }
        if wait_seconds:
            response = _CLIENT.wait_for_response(client, target_id, prompt, wait_seconds)
            result["response"] = response
            result["completed"] = response is not None
        return result
    except _CLIENT.DshError as exc:
        return _error(exc)


if __name__ == "__main__":
    mcp.run("stdio")
