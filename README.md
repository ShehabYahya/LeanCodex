# DSH CLI Session

DSH CLI Session is a Codex plugin and loopback-only MCP server for interacting with an already-running DeepSeek Harness (DSH) web session through bounded structured tools.

The interface provides durable prompt correlation, incremental finalized output, compact lifecycle events, and bounded evidence reads. Internal reasoning, raw tool calls/results, and transient stream/replay payloads are not returned by the normal output feed.

## Tools

- `dsh_list_sessions`: bounded session discovery with continuation cursors.
- `dsh_inspect_session`: compact exact-session or assignment state.
- `dsh_add_project`: register an existing local project/workspace.
- `dsh_new_session`: create an ordinary DSH session.
- `dsh_send_prompt`: send one prompt with durable request correlation.
- `dsh_wait_output`: wait for new finalized output/lifecycle events without sending or steering work.
- `dsh_read_evidence`: read a bounded continuation of assignment-owned output or an explicitly published deliverable.

The MCP server uses the local DSH API at `http://127.0.0.1:3080` by default and refuses non-loopback base URLs. Browser-session credentials remain local and are never accepted as tool arguments.

## Correlation and incremental output

`dsh_send_prompt` returns an `assignmentId`, native `requestId`, and signed cursor. A caller-provided `submission_key` makes retries of the same logical submission resolve to the same assignment/native request. Reusing that key with a different session or payload fails.

A transport outage after prompt admission can return `admission_unknown` while preserving the assignment/native request identity. Retrying the same logical submission uses the same `submission_key` and payload.

`dsh_wait_output` accepts the assignment and cursor and returns an ordered bounded batch plus a replacement cursor. `hasMore` indicates additional already-available output. A repeated cursor is replayable and uses stable event IDs.

The compatibility baseline is wait-based delivery through ordinary MCP tool results. The plugin does not claim unsolicited host wakeup or model-visible push.

## Repository layout

- `plugins/dsh-cli-session/scripts/dsh_mcp_server.py`: MCP stdio server.
- `plugins/dsh-cli-session/skills/dsh-cli-session/scripts/dsh_cli_session.py`: compatibility facade and manual CLI fallback.
- `dsh_transport.py`: loopback Remote transport and exact session metadata.
- `dsh_store.py`: durable assignment/idempotency state and signed cursors.
- `dsh_history.py`: bounded durable journal reads and event helpers.
- `dsh_projection.py`: finalized output/lifecycle reducer.
- `dsh_wait.py`: resumable wait and compact inspection.
- `dsh_evidence.py`: assignment-owned bounded evidence reads.
- `dsh_discovery.py`: bounded session discovery pagination.
- `plugins/dsh-cli-session/skills/dsh-cli-session/SKILL.md`: tool usage and technical semantics.
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
