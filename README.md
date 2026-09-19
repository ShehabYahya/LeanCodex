# DSH CLI Session

DSH CLI Session is a Codex plugin and loopback-only MCP server for supervising an already-running DeepSeek Harness (DSH) web session. It is designed for a split-cost workflow: an expensive supervisor such as Astra decomposes/reviews work while DSH performs implementation, broad reads, tests, browser verification, and debugging.

The normal supervision path is incremental and output-only. DSH's finalized outward assistant text is returned together with compact lifecycle/evidence signals; internal reasoning, raw tool calls/results, and transient stream/replay payloads are not returned to the supervising model.

## Tools

- `dsh_list_sessions`: bounded session discovery with continuation cursors.
- `dsh_inspect_session`: compact exact-session or assignment state recovery.
- `dsh_add_project`: register an existing local project/workspace.
- `dsh_new_session`: create an ordinary DSH session.
- `dsh_send_prompt`: submit one durable, idempotently correlated assignment. `queue` remains the default.
- `dsh_wait_output`: wait for new finalized outward output/lifecycle events without prompting or steering DSH.
- `dsh_read_evidence`: read a bounded continuation of assignment-owned output or an explicitly published deliverable.

The MCP server uses the local DSH API at `http://127.0.0.1:3080` by default and refuses non-loopback base URLs. Browser-session credentials remain local and are never accepted as tool arguments.

## Supervision flow

1. Discover/choose a session once.
2. Call `dsh_send_prompt` with a caller-stable `submission_key` and save its `assignmentId` and `cursor`.
3. Call `dsh_wait_output(assignment_id, after_cursor=cursor)`.
4. Read the returned finalized DSH output and lifecycle signals. If `hasMore` is true, drain again immediately; otherwise wait again unless a blocker/question/terminal result needs a supervisor decision.
5. Use `dsh_read_evidence` only for small, specific review reads. Delegate broad inspection back to DSH.

A transport outage after prompt admission returns `admission_unknown` while preserving the assignment/native request identity. Retry the same logical assignment with the same `submission_key` and payload; DSH's native `requestId` deduplication is used instead of prompt-prefix matching.

## Delivery model

The compatibility baseline is wait-based delivery through ordinary MCP tool results. The plugin does **not** claim that the ChatGPT/Codex host wakes the supervising model from unsolicited MCP push notifications. `dsh_wait_output` blocks internally for a bounded interval and remains resumable with a signed cursor.

The supervision instructions are behavioral guidance, not host enforcement. A strict "Astra may supervise but not shell/edit/read broadly" deployment must additionally restrict the supervisor's unrelated tools at the host/policy layer.

## Repository layout

- `plugins/dsh-cli-session/scripts/dsh_mcp_server.py`: MCP stdio server.
- `plugins/dsh-cli-session/skills/dsh-cli-session/scripts/dsh_cli_session.py`: compatibility facade and manual CLI fallback.
- `dsh_transport.py`: loopback Remote transport and exact session metadata.
- `dsh_store.py`: durable assignment/idempotency state and signed cursors.
- `dsh_history.py`: bounded durable journal reads and safe event helpers.
- `dsh_projection.py`: outward-message/lifecycle reducer.
- `dsh_wait.py`: resumable wait and compact inspection.
- `dsh_evidence.py`: assignment-owned bounded evidence reads.
- `dsh_discovery.py`: bounded session discovery pagination.
- `plugins/dsh-cli-session/skills/dsh-cli-session/SKILL.md`: supervisor-mode instructions.
- `tests/test_supervision.py`: isolated deterministic regression/integration fixtures; no live model/provider calls.
- `docs/NATIVE_CONTRACT.md`: DSH native identity/history contract used by the adapter.
- `docs/MIGRATION.md`: migration notes from the original prompt-prefix waiter.

## Development

Compile the Python sources:

```bash
python3 -m py_compile \
  plugins/dsh-cli-session/scripts/dsh_mcp_server.py \
  plugins/dsh-cli-session/skills/dsh-cli-session/scripts/dsh_*.py
```

Run deterministic tests:

```bash
python3 -m unittest -v tests/test_supervision.py
```

Run the MCP server from the plugin directory when the MCP Python SDK and local DSH web service are available:

```bash
python3 plugins/dsh-cli-session/scripts/dsh_mcp_server.py
```

No paid provider/model call is required by the test suite.
