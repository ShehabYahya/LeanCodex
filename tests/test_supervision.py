from __future__ import annotations

import hashlib
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
    load_store,
    prepare_assignment,
    save_store,
    update_assignment,
    MAX_ASSIGNMENTS,
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
        self.calls: list[str] = []

    def _sync_head(self) -> None:
        head = self.events[-1]["seq"] if self.events else -1
        for row in self.rows:
            row.setdefault("projections", {"asOfSeq": head, "values": {}})
            row["projections"]["asOfSeq"] = head

    def rpc(self, method: str, argument_name: str, argument: object, timeout: float = 20.0) -> object:
        self.calls.append(method)
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
                    timeout_s=0, max_items=1, max_message_chars=5, max_batch_chars=256,
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
            self.assertEqual(result["outcome"], "unavailable")
            self.assertEqual(result["cursor"], cursor)
            self.assertEqual(result["kind"], "time_limit")
            self.assertEqual(result["timeout"], {"limitSeconds": 0, "occurred": False})
            self.assertEqual(result["unavailable"]["state"], "unavailable")

    def test_explicit_time_limit_leaves_output_cursor_replayable_until_notification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target = row(seq=0)
            assignment, _ = prepare_assignment(home, target, "task", "queue", "notify-key")
            request_id = assignment["nativeRequestId"]
            client = FakeClient([target], [
                event(1, "turn/start", {"turn": 1}),
                user(2, request_id),
                assistant(3, 1, [{"type": "text", "text": "first"}]),
            ])
            assignment = admit_assignment(client, home, assignment, "task")
            cursor = initial_cursor(client.secret, assignment)
            first = wait_output(client, home, assignment["assignmentId"], cursor, timeout_s=0)
            cursor = first["cursor"]

            client.events.append(assistant(4, 1, [{"type": "text", "text": "quiet progress"}]))
            quiet = wait_output(
                client, home, assignment["assignmentId"], cursor,
                timeout_s=0, kind="time_limit",
            )
            self.assertEqual(quiet["outcome"], "timeout")
            self.assertTrue(quiet["timedOut"])
            self.assertEqual(quiet["items"], [])
            self.assertEqual(quiet["cursor"], cursor)

            client.events.append(event(5, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}))
            final = wait_output(
                client, home, assignment["assignmentId"], cursor,
                timeout_s=0, kind="time_limit",
            )
            self.assertTrue(final["terminal"])
            self.assertIn("quiet progress", json.dumps(final))
            self.assertEqual(final["kind"], "time_limit")
            self.assertTrue(final["state_change"]["changed"])

    def test_waiting_for_input_uses_same_notification_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target = row(seq=0)
            assignment, _ = prepare_assignment(home, target, "task", "queue", "input-key")
            request_id = assignment["nativeRequestId"]
            client = FakeClient([target], [
                event(1, "turn/start", {"turn": 1}),
                user(2, request_id),
                event(3, "approval/asked", {"id": "ask-1", "toolName": "bash"}),
            ])
            assignment = admit_assignment(client, home, assignment, "task")
            result = wait_output(
                client, home, assignment["assignmentId"], initial_cursor(client.secret, assignment),
                timeout_s=0, kind="waiting_for_input",
            )
            self.assertEqual(result["kind"], "waiting_for_input")
            self.assertEqual(result["state"], "waiting_for_input")
            self.assertTrue(result["state_change"]["changed"])
            self.assertEqual(result["timeout"], {"limitSeconds": 0, "occurred": False})
            self.assertIsNone(result["unavailable"])
            self.assertIn("waiting_for_input", [item["kind"] for item in result["items"]])

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

    def test_multiple_progress_messages_then_terminal_are_delivered_in_source_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target = row(seq=0)
            assignment, _ = prepare_assignment(home, target, "task", "queue", "progress-key")
            request_id = assignment["nativeRequestId"]
            client = FakeClient([target], [
                event(1, "turn/start", {"turn": 1}),
                user(2, request_id),
                assistant(3, 1, [{"type": "text", "text": "progress-1"}]),
                event(4, "step/end", {"turn": 1, "step": 1}),
                assistant(5, 1, [{"type": "text", "text": "progress-2"}]),
                assistant(6, 1, [{"type": "text", "text": "final"}]),
                event(7, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
            ])
            assignment = admit_assignment(client, home, assignment, "task")
            cursor = initial_cursor(client.secret, assignment)
            seen: list[dict] = []
            for _ in range(10):
                result = wait_output(
                    client, home, assignment["assignmentId"], cursor,
                    timeout_s=0, max_items=2, max_batch_chars=1000,
                )
                cursor = result["cursor"]
                seen.extend(result["items"])
                if result["terminal"]:
                    break
            self.assertEqual(
                [item["text"] for item in seen if item["kind"] == "output"],
                ["progress-1", "progress-2", "final"],
            )
            self.assertEqual(seen[-1]["kind"], "lifecycle")
            self.assertEqual(seen[-1]["state"], "completed")
            self.assertTrue(result["terminal"])
            self.assertFalse(result["hasMore"])

    def test_retry_child_failure_waiting_input_and_cancellation_remain_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target = row(seq=0)
            assignment, _ = prepare_assignment(home, target, "task", "queue", "state-key")
            request_id = assignment["nativeRequestId"]
            client = FakeClient([target], [
                event(1, "turn/start", {"turn": 1}),
                user(2, request_id),
                event(3, "llm/retry", {
                    "retryId": "r1", "turn": 1, "step": 1, "provider": "p", "mode": "normal",
                    "policyKey": "default", "retry": 1, "maxRetries": 3, "delayMs": 10,
                    "failure": {"code": "TIMEOUT", "message": "PROVIDER_RAW_MESSAGE"},
                }),
                event(4, "llm/retry-started", {"retryId": "r1", "turn": 1, "step": 1, "retry": 1}),
                event(5, "tool-workflow/agent-start", {
                    "runId": "run", "seq": 1, "label": "worker", "childId": "child-1",
                }),
                event(6, "tool-workflow/agent-end", {"runId": "run", "seq": 1, "outcome": "failed"}),
                event(7, "approval/asked", {"id": "ask-1", "toolName": "bash", "reason": "RAW_APPROVAL_REASON"}),
                event(8, "approval/decided", {"id": "ask-1", "outcome": "allowed-once"}),
                event(9, "turn/end", {
                    "turn": 1, "reason": {"kind": "aborted", "reason": {"kind": "user"}},
                }),
            ])
            assignment = admit_assignment(client, home, assignment, "task")
            result = wait_output(
                client, home, assignment["assignmentId"], initial_cursor(client.secret, assignment),
                timeout_s=0, max_items=32, max_batch_chars=10000,
            )
            kinds = [item["kind"] for item in result["items"]]
            self.assertIn("provider_retry", kinds)
            self.assertIn("provider_retry_started", kinds)
            self.assertIn("child_lifecycle", kinds)
            self.assertIn("waiting_for_input", kinds)
            self.assertIn("input_resolved", kinds)
            child_end = next(
                item for item in result["items"]
                if item["kind"] == "child_lifecycle" and item.get("event") == "tool-workflow/agent-end"
            )
            self.assertEqual(child_end["outcome"], "failed")
            self.assertEqual(result["state"], "cancelled")
            self.assertEqual(result["terminalReason"], "aborted")
            payload = json.dumps(result)
            self.assertNotIn("PROVIDER_RAW_MESSAGE", payload)
            self.assertNotIn("RAW_APPROVAL_REASON", payload)

    def test_excluded_nested_stream_tool_result_error_and_unknown_payload_do_not_leak(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target = row(seq=0)
            assignment, _ = prepare_assignment(home, target, "task", "queue", "leak-key")
            request_id = assignment["nativeRequestId"]
            message = assistant(3, 1, [
                {"type": "reasoning", "text": "REASONING_SENTINEL"},
                {"type": "tool-call", "id": "c1", "name": "bash", "arguments": "{\"x\":\"TOOL_ARG_SENTINEL\"}"},
                {"type": "tool-result", "toolCallId": "c0", "content": [{"type": "text", "text": "TOOL_RESULT_SENTINEL"}]},
                {"type": "future-block", "secretPayload": "UNKNOWN_SENTINEL"},
                {"type": "text", "text": "safe outward"},
            ])
            message["data"]["stream"] = [{"type": "text-delta", "text": "STREAM_SENTINEL"}]
            client = FakeClient([target], [
                event(1, "turn/start", {"turn": 1}),
                user(2, request_id),
                message,
                event(4, "tool/result", {
                    "turn": 1, "step": 1,
                    "message": {"content": [{"type": "tool-result", "content": [{"type": "text", "text": "RAW_TOOL_EVENT_SENTINEL"}]}]},
                }),
                event(5, "turn/end", {
                    "turn": 1,
                    "reason": {"kind": "error", "error": {"code": "BROKEN", "message": "ERROR_MESSAGE_SENTINEL"}},
                }),
            ])
            assignment = admit_assignment(client, home, assignment, "task")
            result = wait_output(
                client, home, assignment["assignmentId"], initial_cursor(client.secret, assignment),
                timeout_s=0, max_items=32, max_batch_chars=10000,
            )
            payload = json.dumps(result)
            self.assertIn("safe outward", payload)
            self.assertIn("unsupported_content", payload)
            for sentinel in (
                "REASONING_SENTINEL", "TOOL_ARG_SENTINEL", "TOOL_RESULT_SENTINEL",
                "UNKNOWN_SENTINEL", "STREAM_SENTINEL", "RAW_TOOL_EVENT_SENTINEL", "ERROR_MESSAGE_SENTINEL",
            ):
                self.assertNotIn(sentinel, payload)

    def test_same_cursor_replays_stable_ids_and_foreign_future_cursors_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target = row(seq=0)
            assignment, _ = prepare_assignment(home, target, "task", "queue", "replay-key")
            request_id = assignment["nativeRequestId"]
            client = FakeClient([target], [
                event(1, "turn/start", {"turn": 1}),
                user(2, request_id),
                assistant(3, 1, [{"type": "text", "text": "hello"}]),
                event(4, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
            ])
            assignment = admit_assignment(client, home, assignment, "task")
            original = initial_cursor(client.secret, assignment)
            first = wait_output(client, home, assignment["assignmentId"], original, timeout_s=0)
            second = wait_output(client, home, assignment["assignmentId"], original, timeout_s=0)
            self.assertEqual(
                [item["eventId"] for item in first["items"]],
                [item["eventId"] for item in second["items"]],
            )

            other, _ = prepare_assignment(home, row("s2", seq=0), "other", "queue", "other-key")
            with self.assertRaisesRegex(DshError, "another assignment|another.*session"):
                decode_cursor(client.secret, original, other)

            future = encode_cursor(client.secret, assignment, 99)
            gap = wait_output(client, home, assignment["assignmentId"], future, timeout_s=0)
            self.assertEqual(gap["outcome"], "gap")
            self.assertEqual(gap["observation"]["state"], "gap")

    def test_no_change_is_timeout_and_wait_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            client = FakeClient([row(seq=0)], [event(0, "turn/start", {"turn": 0})])
            assignment, _ = prepare_assignment(home, client.rows[0], "task", "queue", "readonly-key")
            assignment = admit_assignment(client, home, assignment, "task")
            prompts_before = len(client.prompt_requests)
            calls_before = len(client.calls)
            result = wait_output(
                client, home, assignment["assignmentId"], initial_cursor(client.secret, assignment), timeout_s=0
            )
            self.assertEqual(result["outcome"], "timeout")
            self.assertTrue(result["timedOut"])
            self.assertEqual(len(client.prompt_requests), prompts_before)
            read_calls = client.calls[calls_before:]
            self.assertTrue(set(read_calls).issubset({"session/list", "session/page"}))
            self.assertNotIn("session/prompt", read_calls)
            self.assertNotIn("session/cancel", read_calls)

    def test_compaction_markers_and_restart_do_not_break_raw_sequence_resume(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target = row(seq=0)
            assignment, _ = prepare_assignment(home, target, "task", "queue", "restart-key")
            request_id = assignment["nativeRequestId"]
            events = [
                event(1, "turn/start", {"turn": 1}),
                user(2, request_id),
                assistant(3, 1, [{"type": "text", "text": "before"}]),
                event(4, "compaction/start", {"turn": 1, "compactionId": "c"}),
                event(5, "compaction/summary", {"turn": 1, "compactionId": "c", "summary": [{"type": "text", "text": "SUMMARY_RAW"}]}),
                event(6, "compaction/end", {"turn": 1, "compactionId": "c"}),
                assistant(7, 1, [{"type": "text", "text": "after"}]),
                event(8, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
            ]
            client1 = FakeClient([target], events)
            assignment = admit_assignment(client1, home, assignment, "task")
            cursor = initial_cursor(client1.secret, assignment)
            first = wait_output(
                client1, home, assignment["assignmentId"], cursor,
                timeout_s=0, max_items=1, max_batch_chars=1000,
            )
            self.assertEqual([item.get("text") for item in first["items"] if item["kind"] == "output"], ["before"])

            client2 = FakeClient([target], events)
            second_texts: list[str] = []
            cursor = first["cursor"]
            for _ in range(10):
                result = wait_output(
                    client2, home, assignment["assignmentId"], cursor,
                    timeout_s=0, max_items=2, max_batch_chars=1000,
                )
                cursor = result["cursor"]
                second_texts.extend(item["text"] for item in result["items"] if item["kind"] == "output")
                if result["terminal"]:
                    break
            self.assertEqual(second_texts, ["after"])
            self.assertNotIn("SUMMARY_RAW", json.dumps(result))

    def test_secret_redaction_and_published_symlink_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "dsh"
            workspace = Path(td) / "workspace"
            workspace.mkdir()
            target_file = workspace / "target.txt"
            target_file.write_text("safe", encoding="utf-8")
            link = workspace / "report.txt"
            link.symlink_to(target_file)
            target = row(seq=0, cwd=str(workspace))
            assignment, _ = prepare_assignment(home, target, "task", "queue", "secret-key")
            request_id = assignment["nativeRequestId"]
            client = FakeClient([target])
            client.events = [
                event(1, "turn/start", {"turn": 1}),
                user(2, request_id),
                assistant(3, 1, [{"type": "text", "text": f"credential={client.cookie} visible"}]),
                event(4, "deliverables/presented", {
                    "turn": 1, "callId": "p", "files": [{"path": "report.txt"}],
                }),
                event(5, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
            ]
            assignment = admit_assignment(client, home, assignment, "task")
            result = wait_output(
                client, home, assignment["assignmentId"], initial_cursor(client.secret, assignment), timeout_s=0
            )
            payload = json.dumps(result)
            self.assertNotIn(client.cookie, payload)
            output = next(item for item in result["items"] if item["kind"] == "output")
            self.assertGreater(output.get("redactions", 0), 0)
            with self.assertRaisesRegex(DshError, "symbolic link"):
                read_evidence(client, home, assignment["assignmentId"], "artifact:4:0")

    def test_supervisor_fixture_send_wait_progress_wait_terminal_uses_one_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target = row(seq=0)
            assignment, _ = prepare_assignment(home, target, "implement bounded change", "queue", "workflow-key")
            request_id = assignment["nativeRequestId"]
            client = FakeClient([target], [
                event(1, "turn/start", {"turn": 1}),
                user(2, request_id),
                assistant(3, 1, [{"type": "text", "text": "milestone"}]),
            ])
            assignment = admit_assignment(client, home, assignment, "implement bounded change")
            cursor = initial_cursor(client.secret, assignment)
            progress = wait_output(client, home, assignment["assignmentId"], cursor, timeout_s=0)
            self.assertEqual(progress["outcome"], "output")
            self.assertFalse(progress["terminal"])
            cursor = progress["cursor"]

            client.events.extend([
                assistant(4, 1, [{"type": "text", "text": "done"}]),
                event(5, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
            ])
            final = wait_output(client, home, assignment["assignmentId"], cursor, timeout_s=0)
            self.assertIn("done", json.dumps(final))
            self.assertTrue(final["terminal"])
            self.assertEqual(len(client.prompt_requests), 1)

    def test_assignment_store_prunes_old_terminal_state_but_keeps_active_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            active, _ = prepare_assignment(home, row(seq=0), "active", "queue", "active-retention-key")
            store = load_store(home)
            for index in range(MAX_ASSIGNMENTS + 20):
                assignment_id = f"old-terminal-{index}"
                submission_key = f"old-key-{index}"
                store["assignments"][assignment_id] = {
                    "version": 1,
                    "assignmentId": assignment_id,
                    "sessionId": "s1",
                    "payloadHash": "deadbeef",
                    "submissionKeyHash": hashlib.sha256(submission_key.encode("utf-8")).hexdigest(),
                    "nativeRequestId": f"request-{index}",
                    "mode": "queue",
                    "createdAt": 0,
                    "updatedAt": 0,
                    "admissionState": "accepted",
                    "initialSeq": 0,
                    "cwd": "/tmp/project",
                    "claimSeq": 1,
                    "turn": 1,
                    "terminalSeq": 2,
                    "terminalReason": "completed",
                    "state": "completed",
                }
                slot = hashlib.sha256(submission_key.encode("utf-8")).hexdigest()
                store["submissionKeys"][slot] = assignment_id
            save_store(home, store)

            fresh, created = prepare_assignment(home, row(seq=0), "fresh", "queue", "fresh-retention-key")
            self.assertTrue(created)
            retained = load_store(home)
            self.assertIn(active["assignmentId"], retained["assignments"])
            self.assertIn(fresh["assignmentId"], retained["assignments"])
            self.assertLessEqual(len(retained["assignments"]), MAX_ASSIGNMENTS)
            self.assertFalse(any(key.startswith("old-terminal-") for key in retained["assignments"]))



if __name__ == "__main__":
    unittest.main()
