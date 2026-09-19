# DSH CLI Session

DSH CLI Session is a Codex plugin and local MCP server for an already-running
DeepSeek Harness (DSH) web session. It keeps the integration loopback-only and
exposes a deliberately small tool surface for session work.

## Tools

- dsh_list_sessions: list available sessions, with optional DSH home override.
- dsh_inspect_session: inspect one session's latest agent output and full output.
- dsh_add_project: add a local project directory to DSH.
- dsh_new_session: create a new DSH session from a workspace or local directory.
- dsh_send_prompt: queue or explicitly steer a prompt into a session.

The MCP server uses the local DSH API at http://127.0.0.1:3080 by default.
It does not accept remote URLs or credentials through the tool interface.

## Repository layout

- plugins/dsh-cli-session: the installable Codex plugin.
- plugins/dsh-cli-session/scripts/dsh_mcp_server.py: the FastMCP stdio server.
- plugins/dsh-cli-session/skills/dsh-cli-session: operational guidance and CLI helper.
- .agents/plugins/marketplace.json: a repository-local marketplace entry.

## Development

Validate the plugin manifest:

    python3 /home/shehab/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/dsh-cli-session

Compile the Python sources:

    python3 -m py_compile plugins/dsh-cli-session/scripts/dsh_mcp_server.py plugins/dsh-cli-session/skills/dsh-cli-session/scripts/dsh_cli_session.py

Run the MCP server from the plugin directory:

    python3 plugins/dsh-cli-session/scripts/dsh_mcp_server.py

The server communicates over MCP stdio and expects the local DSH web service to
be running when a tool is invoked.
