#!/usr/bin/env python3
"""DSH CLI compatibility facade plus manual fallback for the supervisory modules."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from dsh_transport import (  # noqa: E402,F401
    DshClient, DshError, DshGapError, DshTransportError, b64url, b64url_decode,
    choose_session, decode_secret, describe, local_authority, projection,
    projection_seq, read_secret, sessions,
)
from dsh_store import (  # noqa: E402,F401
    admit_assignment, decode_cursor, encode_cursor, get_assignment, initial_cursor,
    load_store, prepare_assignment, save_store, update_assignment,
)
from dsh_history import (  # noqa: E402,F401
    _redact_text, _turn_reason_state, read_event_at, read_events_since,
)
from dsh_projection import reduce_events  # noqa: E402,F401
from dsh_wait import (  # noqa: E402,F401
    DEFAULT_MAX_BATCH_CHARS, DEFAULT_MAX_BATCH_ITEMS, DEFAULT_MAX_MESSAGE_CHARS,
    DEFAULT_WAIT_TIMEOUT, MAX_BATCH_CHARS, MAX_BATCH_ITEMS, MAX_MESSAGE_CHARS,
    MAX_WAIT_TIMEOUT, inspect_assignment, reconcile_assignment_state, wait_for_terminal_legacy,
    wait_output,
)
from dsh_evidence import (  # noqa: E402,F401
    DEFAULT_EVIDENCE_CHARS, MAX_EVIDENCE_CHARS, MAX_EVIDENCE_FILE_BYTES, read_evidence,
)
from dsh_discovery import (  # noqa: E402,F401
    DEFAULT_DISCOVERY_LIMIT, MAX_DISCOVERY_LIMIT, paginate_sessions,
)

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Prompt and supervise a live local DSH web session.")
    p.add_argument("prompt", nargs="?", help="prompt text to queue or steer")
    p.add_argument("--list", action="store_true", help="list sessions and exit")
    p.add_argument("--session-id", help="exact session id; required when selection is ambiguous")
    p.add_argument("--cwd", help="when auto-selecting, require this session cwd")
    p.add_argument("--mode", choices=("queue", "steer"), default="steer")
    p.add_argument("--submission-key", help="stable caller retry key")
    p.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="wait for the assignment terminal state")
    p.add_argument("--base-url", default=os.environ.get("LEANCODEX_DSH_URL", "http://127.0.0.1:3080"))
    p.add_argument("--dsh-home", default=os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
    p.add_argument("--credentials-file", default=os.environ.get("LEANCODEX_CREDENTIALS_FILE"))
    p.add_argument("--json", action="store_true", help="emit machine-readable output")
    p.add_argument("--dry-run", action="store_true", help="select and display the target without sending")
    return p

def main() -> int:
    args = parser().parse_args()
    home = Path(args.dsh_home).expanduser()
    try:
        secret = read_secret(home, args.credentials_file)
        client = DshClient(args.base_url, secret)
        if args.list:
            result = paginate_sessions(client, secret)
            print(json.dumps(result, indent=2) if args.json else "\n".join(
                f"{row['sessionId']}\trunning={row['running']}\tcwd={row['cwd']}\ttitle={row['title']}"
                for row in result["sessions"]
            ))
            return 0
        if not args.prompt or not args.prompt.strip():
            raise DshError("prompt text is required unless --list is used")
        target = choose_session(sessions(client), args.session_id, args.cwd)
        target_id = str(target["sessionId"])
        if args.dry_run:
            output = {"target": describe(target), "mode": args.mode, "prompt": args.prompt}
            print(json.dumps(output, indent=2) if args.json else f"target={target_id} mode={args.mode} title={projection(target).get('title')!r}")
            return 0

        submission_key = args.submission_key or f"cli-{uuid.uuid4()}"
        assignment, _created = prepare_assignment(home, target, args.prompt, args.mode, submission_key)
        try:
            assignment = admit_assignment(client, home, assignment, args.prompt)
        except DshTransportError as exc:
            output = {
                "accepted": None,
                "admissionState": "admission_unknown",
                "assignmentId": assignment["assignmentId"],
                "requestId": assignment["nativeRequestId"],
                "submissionKey": submission_key,
                "error": str(exc),
            }
            print(json.dumps(output, indent=2) if args.json else f"admission unknown assignment={assignment['assignmentId']} request_id={assignment['nativeRequestId']}")
            return 2
        cursor = initial_cursor(secret, assignment)
        output: dict[str, Any] = {
            "accepted": True,
            "admissionState": assignment["admissionState"],
            "assignmentId": assignment["assignmentId"],
            "requestId": assignment["nativeRequestId"],
            "submissionKey": submission_key,
            "session": describe(target),
            "mode": args.mode,
            "cursor": cursor,
        }
        if args.wait > 0:
            response, completed, cursor = wait_for_terminal_legacy(client, home, assignment["assignmentId"], cursor, args.wait)
            output["response"] = response
            output["completed"] = completed
            output["cursor"] = cursor
        if args.json:
            print(json.dumps(output, indent=2))
        else:
            print(f"accepted session={target_id} mode={args.mode} assignment={assignment['assignmentId']} request_id={assignment['nativeRequestId']}")
            if args.wait > 0:
                if output["completed"]:
                    print("--- assistant response ---")
                    print(output["response"] or "")
                else:
                    print(f"wait ended after {args.wait}s without verified completed terminal state")
        return 0
    except DshError as exc:
        print(f"dsh-cli-session: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
