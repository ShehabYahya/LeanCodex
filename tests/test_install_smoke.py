from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from mcp import Client, StdioServerParameters


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "dsh-cli-session"
MANIFEST = PLUGIN / ".mcp.json"


def venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


async def run_smoke() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    server = manifest["mcpServers"]["dshCliSession"]

    assert server["command"] == "node"
    assert server["args"] == ["./scripts/launch_mcp.js"]
    expected_env = {
        "DSH_HOME",
        "LEANCODEX_CREDENTIALS_FILE",
        "LEANCODEX_DSH_URL",
        "LEANCODEX_PYTHON",
        "LEANCODEX_RUNTIME_DIR",
    }
    assert expected_env <= set(server.get("env_vars", []))

    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        isolated = temp / "python"
        runtime = temp / "runtime"
        subprocess.run(
            [sys.executable, "-m", "venv", str(isolated)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        python = venv_python(isolated)
        before = subprocess.run(
            [str(python), "-c", "import mcp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert before.returncode != 0, "fresh Python unexpectedly already contains mcp"

        env = os.environ.copy()
        env["LEANCODEX_PYTHON"] = str(python)
        env["LEANCODEX_RUNTIME_DIR"] = str(runtime)

        params = StdioServerParameters(
            command=server["command"],
            args=server["args"],
            cwd=str(PLUGIN),
            env=env,
        )
        async with Client(params) as client:
            listed = await client.list_tools()
            names = {tool.name for tool in listed.tools}
            assert {
                "dsh_list_sessions",
                "dsh_inspect_session",
                "dsh_add_project",
                "dsh_new_session",
                "dsh_send_prompt",
                "dsh_wait_output",
                "dsh_read_evidence",
            } <= names

        after = subprocess.run(
            [str(python), "-c", "import mcp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert after.returncode != 0, "bootstrap must not modify the selected Python environment"
        assert any(runtime.rglob("mcp")), "private runtime cache was not created"


if __name__ == "__main__":
    asyncio.run(run_smoke())
