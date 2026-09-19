from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "plugins" / "dsh-cli-session" / "scripts" / "dsh_mcp_server.py"

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
        listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        missing = EXPECTED - set(tools)
        assert not missing, f"missing MCP tools: {sorted(missing)}"

        wait = tools["dsh_wait_output"]
        wait_props = wait.input_schema.get("properties", {})
        assert {"assignment_id", "after_cursor", "timeout_s", "max_items"} <= set(wait_props)
        send_props = tools["dsh_send_prompt"].input_schema.get("properties", {})
        assert "submission_key" in send_props
        assert send_props["mode"].get("default") == "queue"

        if wait.annotations is not None:
            assert wait.annotations.read_only_hint is True
            assert wait.annotations.idempotent_hint is True
        send = tools["dsh_send_prompt"]
        if send.annotations is not None:
            assert send.annotations.read_only_hint is False

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
