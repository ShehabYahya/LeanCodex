from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "dsh-cli-session" / "skills" / "dsh-cli-session" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from dsh_transport import DshError, DshTransportError
from dsh_store import (
    admit_assignment,
    decode_cursor,
    encode_cursor,
    get_assignment,
    initial_cursor,
    prepare_assignment,
    update_assignment,
)
from dsh_wait import wait_output
from dsh_discovery import paginate_sessions
from dsh_evidence import read_evidence


def event(seq: int, type_: str, data: dict) -> dict:
    return {"type": type_, "seq": seq, "time": 1_700_000_000_000 + seq, "data": data}


def user(seq: int, rpc_id: str, text: str = "task") -> dict:
    return event(seq, "user/message", {
        "id": f"u{seq}",
        "role": "user",
        "source": {"kind": "user", "rpcId": rpc_id},
        "content": [{"type": "text", "text": text}],
    })


def assistant(seq: int, turn: int, blocks: list[dict]) -> dict:
    return event(seq, "assistant/message", {
        "turn": turn,
        "step": 1,
        "message": {
            "id": f"a{seq}",
            "role": "assistant",
            "content": blocks,
        },
        "stream": [],
    })


class FakeClient:
    def __init__(self, rows: list[dict], events: list[dict] | None = None):
        self.secret = b"x" * 32
        self.cookie = "dsh-auth-test=fake-cookie"
        self.rows = rows
        self.events = events or []
        self.prompt_requests: list[dict] = []
        self.prompt_failure: Exception | None = None
        self.list_failure: Exception | None = None

    def _sync_head(self) -> None:
        head = self.events[-1]["seq"] if self.events else -1
        for row in self.rows:
            row.setdefault("projections", {"asOfSeq": head, "values": {}})
            row["projections"]["asOfSeq"] = head

    def rpc(self, method: str, argument_name: str, argument: object, timeout: float = 20.0) -> object:
        if method == "session/list":
            if self.list_failure is not None:
                raise self.list_failure
            self._sync_head()
            return {"items": self.rows}
        if method == "session/prompt":
            assert argument_name == "request"
            assert isinstance(argument, dict)
            self.prompt_requests.append(dict(argument))
            if self.prompt_failure is not None:
                exc = self.prompt_failure
                self.prompt_failure = None
                raise exc
            return {"accepted": True}
        if method == "session/page":
            assert isinstance(argument, dict)
            through = argument["throughSeq"]
            before = argument.get("beforeSeq")
            selected = [
                e for e in self.events
                if e["seq"] <= through and (before is None or e["seq"] < before)
            ]
            return {
                "records": [{"type": "event", "event": e} for e in selected],
                "hasMore": False,
            }
        raise AssertionError(f"unexpected RPC {method}")


def row(session_id: str = "s1", seq: int = -1, running: bool = True, cwd: str | None = "/tmp/project") -> dict:
    return {
        "sessionId": session_id,
        "updatedAt": 1,
        "running": running,
        "blank": False,
        "cwd": cwd,
        "projections": {"asOfSeq": seq, "values": {"title": "test", "inbox": {"next-turn": [], "next-step": []}}},
    }


class SupervisionTests(unittest.TestCase):
    def test_submission_key_reuses_exact_native_request_and_rejects_rebinding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target = row(seq=3)
            first, created = prepare_assignment(home, target, "same task", "queue", "stable-key")
            self.assertTrue(created)
            second, created2 = prepare_assignment(home, target, "same task", "queue", "stable-key")
            self.assertFalse(created2)
            self.assertEqual(first["assignmentId"], second["assignmentId"])
            self.assertEqual(first["nativeRequestId"], second["nativeRequestId"])
            with self.assertRaisesRegex(DshError, "different session or payload"):
                prepare_assignment(home, target, "different task", "queue", "stable-key")
            with self.assertRaisesRegex(DshError, "different session or payload"):
                prepare_assignment(home, row("other", seq=3), "same task", "queue", "stable-key")

    def test_ambiguous_transport_retry_preserves_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            client = FakeClient([row(seq=-1)])
            assignment, _ = prepare_assignment(home, client.rows[0], "do work", "queue", "retry-key")
            client.prompt_failure = DshTransportError("timeout after send")
            with self.assertRaises(DshTransportError):
                admit_assignment(client, home, assignment, "do work")
            stored = get_assignment(home, assignment["assignmentId"])
            self.assertEqual(stored["admissionState"], "admission_unknown")
            retried = admit_assignment(client, home, stored, "do work")
            self.assertEqual(retried["admissionState"], "accepted")
            self.assertEqual(len(client.prompt_requests), 2)
            self.assertEqual(client.prompt_requests[0]["requestId"], client.prompt_requests[1]["requestId"])

    def test_exact_request_correlation_filters_reasoning_tool_calls_and_old_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target = row(seq=3)
            assignment, _ = prepare_assignment(home, target, "Open the repo and fix it", "queue", "prefix-key")
            request_id = assignment["nativeRequestId"]
            events = [
                event(0, "turn/start", {"turn": 0}),
                user(1, "old-request", "Open the repo and fix old thing"),
                assistant(2, 0, [{"type": "text", "text": "OLD RESPONSE"}]),
                event(3, "turn/end", {"turn": 0, "reason": {"kind": "completed"}}),
                event(4, "turn/start", {"turn": 1}),
                user(5, request_id, "Open the repo and fix it"),
                assistant(6, 1, [
                    {"type": "reasoning", "text": "PRIVATE THINKING"},
                    {"type": "tool-call", "id": "c1", "name": "bash", "arguments": "{\"secret\":\"raw\"}"},
                    {"type": "text", "text": "VISIBLE RESULT"},
                ]),
                event(7, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
            ]
            client = FakeClient([target], events)
            assignment = admit_assignment(client, home, assignment, "Open the repo and fix it")
            result = wait_output(client, home, assignment["assignmentId"], initial_cursor(client.secret, assignment), timeout_s=0)
            payload = json.dumps(result)
            self.assertIn("VISIBLE RESULT", payload)
            self.assertNotIn("OLD RESPONSE", payload)
            self.assertNotIn("PRIVATE THINKING", payload)
            self.assertNotIn('\"secret\":\"raw\"', payload)
            self.assertEqual(result["state"], "completed")
            self.assertTrue(result["terminal"])

    def test_long_output_is_losslessly_resumable_by_signed_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target = row(seq=0)
            assignment, _ = prepare_assignment(home, target, "task", "queue", "chunk-key")
            request_id = assignment["nativeRequestId"]
            text = "abcdefghijklmnopqrstuvwxyz"
            client = FakeClient([target], [
                event(1, "turn/start", {"turn": 1}),
                user(2, request_id),
                assistant(3, 1, [{"type": "text", "text": text}]),
                event(4, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
            ])
            assignment = admit_assignment(client, home, assignment, "task")
            cursor = initial_cursor(client.secret, assignment)
            parts: list[str] = []
            for _ in range(20):
                result = wait_output(
                    client, home, assignment["assignmentId"], cursor,
                    timeout_s=0, max_items=8, max_message_chars=5, max_batch_chars=5,
                )
                cursor = result["cursor"]
                parts.extend(item["text"] for item in result["items"] if item["kind"] == "output")
                if result["terminal"] and not result["hasMore"] and not result["items"]:
                    break
            self.assertEqual("".join(parts), text)
            decoded_after, partial = decode_cursor(client.secret, cursor, get_assignment(home, assignment["assignmentId"]))
            self.assertGreaterEqual(decoded_after, 4)
            self.assertIsNone(partial)

    def test_forged_cursor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            assignment, _ = prepare_assignment(home, row(seq=0), "task", "queue", "cursor-key")
            token = initial_cursor(b"x" * 32, assignment)
            forged = token[:-1] + ("A" if token[-1] != "A" else "B")
            with self.assertRaisesRegex(DshError, "forged|corrupted"):
                decode_cursor(b"x" * 32, forged, assignment)

    def test_discovery_is_bounded_and_stale_cursor_fails(self) -> None:
        rows = [row("a", seq=1), row("b", seq=2), row("c", seq=3)]
        client = FakeClient(rows)
        page = paginate_sessions(client, client.secret, limit=1)
        self.assertEqual(len(page["sessions"]), 1)
        self.assertTrue(page["hasMore"])
        cursor = page["nextCursor"]
        assert isinstance(cursor, str)
        rows[0]["updatedAt"] = 999
        with self.assertRaisesRegex(DshError, "stale discovery cursor"):
            paginate_sessions(client, client.secret, limit=1, cursor=cursor)

    def test_observation_failure_is_not_reported_as_assignment_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            client = FakeClient([row(seq=0)])
            assignment, _ = prepare_assignment(home, client.rows[0], "task", "queue", "obs-key")
            assignment = admit_assignment(client, home, assignment, "task")
            cursor = initial_cursor(client.secret, assignment)
            client.list_failure = DshTransportError("temporary local transport outage")
            result = wait_output(client, home, assignment["assignmentId"], cursor, timeout_s=0)
            self.assertEqual(result["state"], "queued")
            self.assertFalse(result["terminal"])
            self.assertEqual(result["observation"]["state"], "unavailable")
            self.assertEqual(result["cursor"], cursor)

    def test_message_and_artifact_evidence_are_assignment_owned_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "dsh"
            workspace = Path(td) / "workspace"
            workspace.mkdir()
            artifact = workspace / "report.txt"
            artifact.write_text("0123456789", encoding="utf-8")
            target = row(seq=0, cwd=str(workspace))
            assignment, _ = prepare_assignment(home, target, "task", "queue", "evidence-key")
            request_id = assignment["nativeRequestId"]
            client = FakeClient([target], [
                event(1, "turn/start", {"turn": 1}),
                user(2, request_id),
                assistant(3, 1, [{"type": "text", "text": "abcdefghij"}]),
                event(4, "deliverables/presented", {
                    "turn": 1,
                    "callId": "present-1",
                    "files": [{"path": "report.txt", "description": "report"}],
                }),
                event(5, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
            ])
            assignment = admit_assignment(client, home, assignment, "task")
            drained = wait_output(client, home, assignment["assignmentId"], initial_cursor(client.secret, assignment), timeout_s=0)
            refs = [
                item["ref"] for item in drained["items"]
                if isinstance(item.get("ref"), str)
            ]
            self.assertIn("message:3:0", refs)
            message = read_evidence(client, home, assignment["assignmentId"], "message:3:0", start=2, max_chars=4)
            self.assertEqual(message["text"], "cdef")
            artifact_result = read_evidence(client, home, assignment["assignmentId"], "artifact:4:0", start=3, max_chars=4)
            self.assertEqual(artifact_result["text"], "3456")
            self.assertEqual(artifact_result["publishedPath"], "report.txt")
            with self.assertRaises(DshError):
                read_evidence(client, home, assignment["assignmentId"], "artifact:3:0")

    def test_relative_artifact_cannot_escape_workspace_after_publication(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "dsh"
            workspace = Path(td) / "workspace"
            workspace.mkdir()
            outside = Path(td) / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            target = row(seq=0, cwd=str(workspace))
            assignment, _ = prepare_assignment(home, target, "task", "queue", "escape-key")
            request_id = assignment["nativeRequestId"]
            client = FakeClient([target], [
                event(1, "turn/start", {"turn": 1}),
                user(2, request_id),
                event(3, "deliverables/presented", {
                    "turn": 1,
                    "callId": "present-1",
                    "files": [{"path": "../outside.txt"}],
                }),
                event(4, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
            ])
            assignment = admit_assignment(client, home, assignment, "task")
            wait_output(client, home, assignment["assignmentId"], initial_cursor(client.secret, assignment), timeout_s=0)
            with self.assertRaisesRegex(DshError, "inside the assignment workspace"):
                read_evidence(client, home, assignment["assignmentId"], "artifact:3:0")


if __name__ == "__main__":
    unittest.main()
