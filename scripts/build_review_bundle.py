#!/usr/bin/env python3
"""Build a sanitized review bundle for the DSH session plugin."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "plugins" / "dsh-cli-session" / "scripts" / "dsh_mcp_server.py"

SOURCE_PATHS = [
    "README.md",
    "docs/MIGRATION.md",
    "docs/NATIVE_CONTRACT.md",
    "plugins/dsh-cli-session/.codex-plugin/plugin.json",
    "plugins/dsh-cli-session/.mcp.json",
    "plugins/dsh-cli-session/scripts/dsh_mcp_server.py",
    "plugins/dsh-cli-session/skills/dsh-cli-session/SKILL.md",
    "plugins/dsh-cli-session/skills/dsh-cli-session/agents/openai.yaml",
    "plugins/dsh-cli-session/skills/dsh-orchestrator/SKILL.md",
    "plugins/dsh-cli-session/skills/dsh-orchestrator/agents/openai.yaml",
    "plugins/dsh-cli-session/skills/dsh-orchestrator/references/assignments.md",
    "plugins/dsh-cli-session/skills/dsh-cli-session/scripts/dsh_cli_session.py",
    "plugins/dsh-cli-session/skills/dsh-cli-session/scripts/dsh_discovery.py",
    "plugins/dsh-cli-session/skills/dsh-cli-session/scripts/dsh_evidence.py",
    "plugins/dsh-cli-session/skills/dsh-cli-session/scripts/dsh_history.py",
    "plugins/dsh-cli-session/skills/dsh-cli-session/scripts/dsh_projection.py",
    "plugins/dsh-cli-session/skills/dsh-cli-session/scripts/dsh_store.py",
    "plugins/dsh-cli-session/skills/dsh-cli-session/scripts/dsh_transport.py",
    "plugins/dsh-cli-session/skills/dsh-cli-session/scripts/dsh_wait.py",
    "tests/test_supervision.py",
    "tests/test_mcp_stdio.py",
    ".github/workflows/test.yml",
    "requirements-test.txt",
]


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


async def tool_schemas() -> list[dict]:
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], cwd=str(ROOT))
    async with Client(params) as client:
        listed = await client.list_tools()
        return [tool.model_dump(by_alias=True, exclude_none=True) for tool in listed.tools]


def encoded_metrics(value: object) -> dict[str, int]:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"serializedChars": len(text), "serializedBytesUtf8": len(text.encode("utf-8"))}


def fixture_samples() -> dict[str, object]:
    session = {
        "sessionId": "session-fixture",
        "running": True,
        "cwd": "/workspace/project",
        "title": "Fixture",
        "updatedAt": 1700000000000,
        "projectionSeq": 40,
    }
    discovery = {
        "ok": True,
        "sessions": [session],
        "count": 1,
        "totalMatching": 1,
        "nextCursor": None,
        "hasMore": False,
    }
    unchanged = {
        "ok": True,
        "assignmentId": "assignment-fixture",
        "sessionId": "session-fixture",
        "state": "running",
        "admissionState": "accepted",
        "outcome": "timeout",
        "terminalObserved": False,
        "terminal": False,
        "terminalReason": None,
        "turn": 3,
        "items": [],
        "cursor": "signed-fixture-cursor",
        "hasMore": False,
        "timedOut": True,
    }
    one_message = {
        **unchanged,
        "outcome": "output",
        "timedOut": False,
        "items": [{
            "kind": "output",
            "eventId": "seq:41:block:0:offset:0",
            "messageId": "message-fixture",
            "seq": 41,
            "turn": 3,
            "block": 0,
            "offset": 0,
            "endOffset": 34,
            "totalChars": 34,
            "complete": True,
            "text": "Implemented the bounded adapter fix.",
            "ref": "message:41:0",
            "source": {"assignmentId": "assignment-fixture", "sessionId": "session-fixture"},
        }],
    }
    burst = {
        **one_message,
        "items": [
            {**one_message["items"][0], "eventId": f"seq:{seq}:block:0:offset:0", "seq": seq, "text": text}
            for seq, text in [
                (41, "Inspected the adapter contract."),
                (42, "Regression tests now pass."),
                (43, "Final verification is running."),
            ]
        ],
        "hasMore": True,
    }
    completion = {
        **unchanged,
        "state": "completed",
        "outcome": "state_change",
        "terminalObserved": True,
        "terminal": True,
        "terminalReason": "completed",
        "timedOut": False,
        "items": [{
            "kind": "lifecycle",
            "eventId": "seq:45",
            "seq": 45,
            "state": "completed",
            "terminal": True,
            "reason": "completed",
            "turn": 3,
            "source": {"assignmentId": "assignment-fixture", "sessionId": "session-fixture"},
        }, {
            "kind": "evidence_published",
            "eventId": "seq:44",
            "seq": 44,
            "assignmentId": "assignment-fixture",
            "files": [{"ref": "artifact:44:0", "path": "report.md", "description": "verification report"}],
            "source": {"assignmentId": "assignment-fixture", "sessionId": "session-fixture"},
        }],
    }
    synthetic_old_discovery = {
        "label": "synthetic-pre-v0.2-shape-not-a-captured-host-response",
        "sessions": [
            {
                "sessionId": f"session-{index:03d}",
                "updatedAt": 1700000000000 - index,
                "running": index == 0,
                "cwd": f"/workspace/project-{index:03d}",
                "title": f"Historical session {index:03d}",
            }
            for index in range(176)
        ],
    }
    return {
        "discovery": discovery,
        "unchangedWait": unchanged,
        "oneMessage": one_message,
        "burst": burst,
        "completionAndEvidence": completion,
        "syntheticOldDiscovery": synthetic_old_discovery,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="dsh-supervision-review.zip")
    parser.add_argument("--test-results", default="test-results.txt")
    args = parser.parse_args()

    schemas = asyncio.run(tool_schemas())
    samples = fixture_samples()
    measurements = {
        name: {
            **encoded_metrics(value),
            "basis": (
                "synthetic pre-v0.2 list shape inferred from original unbounded-list behavior; not a captured host response"
                if name == "syntheticOldDiscovery"
                else "sanitized deterministic v0.2 fixture shape"
            ),
        }
        for name, value in samples.items()
    }

    with tempfile.TemporaryDirectory(prefix="dsh-review-") as td:
        bundle = Path(td) / "dsh-supervision-review"
        (bundle / "source").mkdir(parents=True)
        (bundle / "samples").mkdir()
        for rel in SOURCE_PATHS:
            src = ROOT / rel
            if not src.exists():
                continue
            dst = bundle / "source" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        (bundle / "tool-schemas.json").write_text(
            json.dumps(schemas, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        for name, value in samples.items():
            (bundle / "samples" / f"{name}.json").write_text(
                json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        (bundle / "context-measurements.json").write_text(
            json.dumps(measurements, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        test_results = ROOT / args.test_results
        if test_results.exists():
            shutil.copy2(test_results, bundle / "test-results.txt")
        else:
            (bundle / "test-results.txt").write_text(
                "No test-results file was supplied to the bundle builder.\n", encoding="utf-8"
            )

        (bundle / "HOST_DELIVERY_STATUS.md").write_text(
            "# Host delivery verification status\n\n"
            "- Compatibility baseline: verified application-level bounded wait through ordinary MCP tool results.\n"
            "- Server-emitted unsolicited MCP push: not implemented by this plugin.\n"
            "- Client/UI display of unsolicited push: not tested.\n"
            "- Unsolicited push text entering caller model context or waking the model: not verified and not claimed.\n"
            "- Live paid DSH/model smoke test: intentionally not run.\n"
            "- Installed user plugin/cache refresh in ChatGPT Desktop: not reproducible in GitHub CI; verify after normal local plugin refresh.\n",
            encoding="utf-8",
        )
        (bundle / "REVIEW.md").write_text(
            "# DSH output-only interface review bundle\n\n"
            "The implementation uses a durable assignment/native-request mapping, signed replayable cursors, bounded session discovery, "
            "an allowlisted finalized-assistant projection, compact lifecycle signals, and assignment-owned evidence references. "
            "The incremental interface supports send once -> wait/drain -> read subsequent output.\n\n"
            "Reasoning blocks, raw tool calls/results, assistant stream/replay payloads, and provider failure messages are excluded from "
            "the normal feed. Terminal execution state is separate from application-level task success.\n\n"
            "See HOST_DELIVERY_STATUS.md for intentionally unverified host/push surfaces and docs/NATIVE_CONTRACT.md for the "
            "native DSH contract used by the adapter.\n",
            encoding="utf-8",
        )

        manifest = {
            "repository": "ShehabYahya/dsh-cli-session",
            "commit": os.environ.get("GITHUB_SHA") or git("rev-parse", "HEAD"),
            "branch": os.environ.get("GITHUB_REF_NAME") or git("branch", "--show-current"),
            "base": "main",
            "collection": "GitHub CI deterministic fixtures plus MCP stdio tool discovery",
            "sourcePaths": SOURCE_PATHS,
            "redactions": "No credentials, whole transcripts, reasoning, raw tool payloads, dependency trees, or build output included.",
            "knownOmissions": [
                "No paid live model/provider smoke test",
                "No ChatGPT Desktop installed-plugin/cache refresh test",
                "No verified unsolicited model-visible push/wakeup",
                "Synthetic context samples are not billed-token measurements",
            ],
        }
        (bundle / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        out = ROOT / args.output
        if out.exists():
            out.unlink()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(bundle.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(bundle.parent))
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
