#!/usr/bin/env python3
"""MCP interface for an already-running loopback DSH web session."""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
from typing import Any, Literal
from typing_extensions import NotRequired, TypedDict
import uuid

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # MCP Python SDK 1.x used by current Codex plugin hosts
    from mcp.server.fastmcp import FastMCP
    try:
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError:
        class ToolError(RuntimeError):
            pass

try:
    from mcp.types import ToolAnnotations
except ImportError:
    try:  # MCP Python SDK 2.x split protocol types into mcp_types
        from mcp_types import ToolAnnotations
    except ImportError:  # pragma: no cover - very old SDK fallback
        ToolAnnotations = None  # type: ignore[assignment]


_ROOT = Path(__file__).resolve().parent.parent
_CLI_PATH = _ROOT / "skills" / "dsh-cli-session" / "scripts" / "dsh_cli_session.py"
_SPEC = importlib.util.spec_from_file_location("dsh_cli_session_impl", _CLI_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - packaging failure
    raise RuntimeError(f"cannot load bundled DSH client: {_CLI_PATH}")
_CLIENT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CLIENT)


_DEFAULT_BASE_URL = os.environ.get("LEANCODEX_DSH_URL", "http://127.0.0.1:3080")


mcp = FastMCP("LeanCodex")


class SessionDescriptor(TypedDict, total=False):
    sessionId: str
    running: bool
    cwd: str | None
    title: str | None
    updatedAt: int | None
    projectionSeq: int | None


class ListSessionsResult(TypedDict):
    ok: bool
    sessions: list[SessionDescriptor]
    count: int
    totalMatching: int
    nextCursor: str | None
    hasMore: bool


class InspectSessionResult(TypedDict):
    ok: bool
    session: SessionDescriptor


class InspectAssignmentResult(TypedDict):
    ok: bool
    assignment: dict[str, Any]


class AddProjectResult(TypedDict):
    ok: bool
    created: bool
    project: dict[str, Any]


class NewSessionResult(TypedDict):
    ok: bool
    sessionId: str
    agentPreset: NotRequired[str]


class SendPromptResult(TypedDict):
    ok: bool
    accepted: bool | None
    admissionState: str
    assignmentId: str
    requestId: str
    submissionKey: str
    created: bool
    session: SessionDescriptor
    mode: Literal["queue", "steer"]
    cursor: str | None
    observationError: str | None
    reconcile: str | None
    completed: bool | None
    response: str | None


class WaitOutputResult(TypedDict):
    ok: bool
    assignmentId: str
    sessionId: str
    state: str | None
    admissionState: str | None
    kind: Literal["waiting_for_input", "time_limit"]
    outcome: Literal["output", "state_change", "timeout", "gap", "unavailable"]
    state_change: dict[str, Any]
    terminalObserved: bool
    terminal: bool
    terminalReason: str | None
    turn: int | None
    items: list[dict[str, Any]]
    cursor: str
    hasMore: bool
    timedOut: bool
    timeout: dict[str, Any]
    unavailable: dict[str, Any] | None
    observation: NotRequired[dict[str, Any]]


class EvidenceResult(TypedDict, total=False):
    ok: bool
    ref: str
    kind: str
    start: int
    end: int
    totalChars: int
    hasMore: bool
    nextStart: int | None
    text: str
    seq: int
    block: int
    index: int
    sha256: str
    redactions: int
    publishedPath: str
    bytes: int
    mtimeNs: int
    description: str


def _ann(*, read_only: bool, idempotent: bool = False, destructive: bool = False):
    if ToolAnnotations is None:
        return None
    return ToolAnnotations(
        read_only_hint=read_only,
        idempotent_hint=idempotent,
        destructive_hint=destructive,
        open_world_hint=False,
    )


def _tool(**kwargs: Any):
    annotations = kwargs.pop("annotations", None)
    if annotations is not None:
        return mcp.tool(annotations=annotations, **kwargs)
    return mcp.tool(**kwargs)


def _home(dsh_home: str | None) -> Path:
    configured = dsh_home or os.environ.get("DSH_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".dsh"


def _client(base_url: str, dsh_home: str | None) -> tuple[Any, Path]:
    home = _home(dsh_home)
    credentials_file = os.environ.get("LEANCODEX_CREDENTIALS_FILE")
    return _CLIENT.DshClient(base_url, _CLIENT.read_secret(home, credentials_file)), home


def _raise(exc: Exception) -> None:
    raise ToolError(str(exc)) from exc


def _absolute_directory(value: str, label: str) -> str:
    expanded = str(Path(value).expanduser()) if value.startswith("~") else value
    if not _CLIENT.path_is_absolute(expanded):
        raise ValueError(f"{label} must be an absolute path")
    # DSH owns workspace validation. Do not reject a valid DSH path merely
    # because this MCP process has a different filesystem namespace.
    return expanded


@_tool(annotations=_ann(read_only=True, idempotent=True))
def dsh_list_sessions(
    limit: int = 20,
    cursor: str | None = None,
    running_only: bool = False,
    cwd: str | None = None,
    base_url: str = _DEFAULT_BASE_URL,
    dsh_home: str | None = None,
) -> ListSessionsResult:
    """List a bounded page of local DSH sessions. Use the returned cursor for continuation."""
    try:
        client, _ = _client(base_url, dsh_home)
        return {
            "ok": True,
            **_CLIENT.paginate_sessions(client, client.secret, limit, cursor, running_only, cwd),
        }
    except _CLIENT.DshError as exc:
        _raise(exc)


@_tool(annotations=_ann(read_only=True, idempotent=True))
def dsh_inspect_session(
    session_id: str,
    base_url: str = _DEFAULT_BASE_URL,
    dsh_home: str | None = None,
) -> InspectSessionResult:
    """Inspect any exact DSH session by ID; no task, assignment, or cursor is required."""
    try:
        client, _ = _client(base_url, dsh_home)
        item = _CLIENT.choose_session(_CLIENT.sessions(client), session_id, None)
        return {"ok": True, "session": _CLIENT.describe(item)}
    except _CLIENT.DshError as exc:
        _raise(exc)


@_tool(annotations=_ann(read_only=True, idempotent=True))
def dsh_inspect_assignment(
    assignment_id: str,
    session_id: str | None = None,
    base_url: str = _DEFAULT_BASE_URL,
    dsh_home: str | None = None,
) -> InspectAssignmentResult:
    """Inspect compact state for an assignment created through this bridge."""
    try:
        client, home = _client(base_url, dsh_home)
        result = _CLIENT.inspect_assignment(client, home, assignment_id)
        if session_id is not None and result["sessionId"] != session_id:
            raise _CLIENT.DshError("assignment does not belong to the supplied session_id")
        return {"ok": True, "assignment": result}
    except _CLIENT.DshError as exc:
        _raise(exc)


@_tool(annotations=_ann(read_only=False, idempotent=True))
def dsh_add_project(
    path: str,
    base_url: str = _DEFAULT_BASE_URL,
    dsh_home: str | None = None,
) -> AddProjectResult:
    """Register an existing local directory as a DSH project/workspace."""
    try:
        project_path = _absolute_directory(path, "path")
        client, _ = _client(base_url, dsh_home)
        value = client.rpc("workspace/create", "request", {"path": project_path})
        if not isinstance(value, dict) or not isinstance(value.get("workspace"), dict):
            raise _CLIENT.DshError("DSH workspace/create returned no workspace")
        return {"ok": True, "created": bool(value.get("created")), "project": value["workspace"]}
    except (ValueError, _CLIENT.DshError) as exc:
        _raise(exc)


@_tool(annotations=_ann(read_only=False, idempotent=False))
def dsh_new_session(
    workspace_id: str | None = None,
    cwd: str | None = None,
    agent_preset: str | None = None,
    base_url: str = _DEFAULT_BASE_URL,
    dsh_home: str | None = None,
) -> NewSessionResult:
    """Create a new ordinary DSH session in a project or absolute working directory."""
    if workspace_id and cwd:
        raise ToolError("provide workspace_id or cwd, not both")
    try:
        request: dict[str, str] = {}
        if workspace_id:
            request["workspaceId"] = workspace_id
        elif cwd:
            request["cwd"] = _absolute_directory(cwd, "cwd")
        if agent_preset:
            request["agentPreset"] = agent_preset
        client, _ = _client(base_url, dsh_home)
        value = client.rpc("session/create", "request", request)
        if not isinstance(value, dict) or not isinstance(value.get("sessionId"), str):
            raise _CLIENT.DshError("DSH session/create returned no session id")
        result: dict[str, Any] = {"ok": True, "sessionId": value["sessionId"]}
        if value.get("agentPreset") is not None:
            result["agentPreset"] = value["agentPreset"]
        return result
    except (ValueError, _CLIENT.DshError) as exc:
        _raise(exc)


@_tool(annotations=_ann(read_only=False, idempotent=False))
def dsh_send_prompt(
    prompt: str,
    session_id: str | None = None,
    cwd: str | None = None,
    mode: Literal["queue", "steer"] = "steer",
    submission_key: str | None = None,
    wait_seconds: int = 0,
    base_url: str = _DEFAULT_BASE_URL,
    dsh_home: str | None = None,
) -> SendPromptResult:
    """Send a prompt to one DSH session with durable request correlation; steer is the default."""
    if not prompt.strip():
        raise ToolError("prompt text is required")
    if wait_seconds < 0:
        raise ToolError("wait_seconds must be non-negative")
    try:
        client, home = _client(base_url, dsh_home)
        target = _CLIENT.choose_session(_CLIENT.sessions(client), session_id, cwd)
        key = submission_key or f"auto-{uuid.uuid4()}"
        assignment, created = _CLIENT.prepare_assignment(home, target, prompt, mode, key)
        try:
            assignment = _CLIENT.admit_assignment(client, home, assignment, prompt)
        except _CLIENT.DshTransportError as exc:
            return {
                "ok": True,
                "accepted": None,
                "admissionState": "admission_unknown",
                "assignmentId": assignment["assignmentId"],
                "requestId": assignment["nativeRequestId"],
                "submissionKey": key,
                "created": created,
                "session": _CLIENT.describe(target),
                "mode": mode,
                "cursor": _CLIENT.initial_cursor(client.secret, assignment) if isinstance(assignment.get("initialSeq"), int) else None,
                "observationError": str(exc),
                "reconcile": "retry dsh_send_prompt with the same submission_key and identical payload, or inspect this assignment; do not use a fresh key",
                "completed": None,
                "response": None,
            }
        cursor = _CLIENT.initial_cursor(client.secret, assignment) if isinstance(assignment.get("initialSeq"), int) else None
        result: dict[str, Any] = {
            "ok": True,
            "accepted": assignment.get("admissionState") == "accepted",
            "admissionState": assignment.get("admissionState"),
            "assignmentId": assignment["assignmentId"],
            "requestId": assignment["nativeRequestId"],
            "submissionKey": key,
            "created": created,
            "session": _CLIENT.describe(target),
            "mode": mode,
            "cursor": cursor,
            "observationError": None,
            "reconcile": None,
            "completed": None,
            "response": None,
        }
        if wait_seconds:
            if cursor is None:
                result["completed"] = False
                result["response"] = None
                result["observationError"] = "assignment has no reliable initial session cursor"
            else:
                response, completed, cursor = _CLIENT.wait_for_terminal_legacy(
                    client, home, assignment["assignmentId"], cursor, wait_seconds,
                )
                result["response"] = response
                result["completed"] = completed
                result["cursor"] = cursor
        return result
    except _CLIENT.DshError as exc:
        _raise(exc)


@_tool(annotations=_ann(read_only=True, idempotent=True))
async def dsh_wait_output(
    assignment_id: str,
    after_cursor: str | None = None,
    kind: Literal["waiting_for_input", "time_limit"] = "time_limit",
    timeout_s: int = 30,
    max_items: int = 8,
    max_message_chars: int = 6000,
    max_batch_chars: int = 12000,
    base_url: str = _DEFAULT_BASE_URL,
    dsh_home: str | None = None,
) -> WaitOutputResult:
    """Wait using one explicit policy while returning common state/terminal/timeout signals."""
    try:
        client, home = _client(base_url, dsh_home)
        return await asyncio.to_thread(
            _CLIENT.wait_output,
            client,
            home,
            assignment_id,
            after_cursor,
            timeout_s,
            max_items,
            max_message_chars,
            max_batch_chars,
            kind,
        )
    except _CLIENT.DshError as exc:
        _raise(exc)


@_tool(annotations=_ann(read_only=True, idempotent=True))
def dsh_read_evidence(
    assignment_id: str,
    evidence_ref: str,
    start: int = 0,
    max_chars: int = 6000,
    expected_sha256: str | None = None,
    base_url: str = _DEFAULT_BASE_URL,
    dsh_home: str | None = None,
) -> EvidenceResult:
    """Read a bounded continuation of an assignment-owned output block or published file."""
    try:
        client, home = _client(base_url, dsh_home)
        return _CLIENT.read_evidence(
            client, home, assignment_id, evidence_ref, start, max_chars, expected_sha256
        )
    except _CLIENT.DshError as exc:
        _raise(exc)


if __name__ == "__main__":
    mcp.run("stdio")
