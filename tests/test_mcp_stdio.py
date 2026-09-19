from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "plugins" / "dsh-cli-session" / "scripts" / "dsh_mcp_server.py"

MODEL_VISIBLE_SURFACES = [
    ROOT / "plugins" / "dsh-cli-session" / "skills" / "dsh-cli-session" / "SKILL.md",
    ROOT / "plugins" / "dsh-cli-session" / "skills" / "dsh-cli-session" / "agents" / "openai.yaml",
    ROOT / "plugins" / "dsh-cli-session" / ".codex-plugin" / "plugin.json",
    ROOT / "plugins" / "dsh-cli-session" / ".mcp.json",
]
BANNED_BEHAVIOR_SHAPING = [
    "a" + "stra",
    "supervisor mode",
    "delegate implementation",
    "delegate bounded work",
    "broad repository reads",
    "acceptance criteria",
    "independent verification",
    "without duplicating",
    "should not duplicate",
]

EXPECTED = {
    "dsh_list_sessions",
    "dsh_inspect_session",
    "dsh_add_project",
    "dsh_new_session",
    "dsh_send_prompt",
    "dsh_wait_output",
    "dsh_read_evidence",
}


async def run_contract() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        cwd=str(ROOT),
    )
    async with Client(params) as client:
        assert not client.instructions, "MCP server should not inject global behavior instructions"
        listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        missing = EXPECTED - set(tools)
        assert not missing, f"missing MCP tools: {sorted(missing)}"

        wait = tools["dsh_wait_output"]
        wait_props = wait.input_schema.get("properties", {})
        assert {"assignment_id", "after_cursor", "kind", "timeout_s", "max_items"} <= set(wait_props)
        assert wait_props["kind"].get("default") == "time_limit"
        assert set(wait_props["kind"].get("enum", [])) == {"waiting_for_input", "time_limit"}
        send_props = tools["dsh_send_prompt"].input_schema.get("properties", {})
        assert "submission_key" in send_props
        assert send_props["mode"].get("default") == "steer"

        wait_output_schema = wait.output_schema or {}
        wait_output_props = wait_output_schema.get("properties", {})
        assert {
            "assignmentId", "kind", "outcome", "items", "cursor", "terminal",
            "state_change", "timeout", "unavailable",
        } <= set(wait_output_props)
        list_output_schema = tools["dsh_list_sessions"].output_schema or {}
        assert {"sessions", "count", "nextCursor", "hasMore"} <= set(list_output_schema.get("properties", {}))
        send_output_schema = tools["dsh_send_prompt"].output_schema or {}
        assert {"assignmentId", "requestId", "submissionKey", "admissionState"} <= set(send_output_schema.get("properties", {}))
        send_required = set(send_output_schema.get("required", []))
        assert {"observationError", "reconcile", "completed", "response"} <= send_required
        for field in ("observationError", "reconcile", "completed", "response"):
            assert "anyOf" in send_output_schema["properties"][field]

        if wait.annotations is not None:
            assert wait.annotations.read_only_hint is True
            assert wait.annotations.idempotent_hint is True
        send = tools["dsh_send_prompt"]
        if send.annotations is not None:
            assert send.annotations.read_only_hint is False

        model_surface_text = "\n".join(path.read_text(encoding="utf-8") for path in MODEL_VISIBLE_SURFACES).lower()
        for phrase in BANNED_BEHAVIOR_SHAPING:
            assert phrase not in model_surface_text, f"behavior-shaping phrase leaked into model-visible surface: {phrase}"

        repository_text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in ROOT.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and path.suffix.lower() in {".py", ".md", ".json", ".yaml", ".yml", ".txt"}
        ).lower()
        forbidden_model_name = "a" + "stra"
        assert forbidden_model_name not in repository_text, "model-specific name must not appear in repository content"

        # Exercise the real stdio error contract without contacting DSH or any model/provider.
        with tempfile.TemporaryDirectory() as td:
            result = await client.call_tool(
                "dsh_list_sessions",
                {"limit": 1, "dsh_home": td},
            )
            assert result.is_error is True
            rendered = "\n".join(getattr(block, "text", "") for block in result.content)
            assert "credentials" in rendered.lower() or "secret" in rendered.lower()


if __name__ == "__main__":
    asyncio.run(run_contract())
