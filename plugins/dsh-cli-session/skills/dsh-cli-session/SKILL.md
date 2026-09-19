---
name: dsh-cli-session
description: Prompt, inspect, and verify an already-running local DeepSeek Harness web session through the bundled CLI/API or DSH plugin tools.
---

# DSH CLI session control

Use this skill when the user asks to inspect or send a prompt to an existing local
`dsh web` session. Use the plugin's `dsh_list_sessions`, `dsh_inspect_session`, and
`dsh_add_project`, `dsh_new_session`, and `dsh_send_prompt` tools when available.
`dsh_add_project` registers an existing directory as a DSH Workspace/project;
`dsh_new_session` creates an ordinary session without sending a prompt. The bundled CLI remains available at
`scripts/dsh_cli_session.py` relative to this skill directory.

## Safety and session selection

- Operate only on a loopback DSH server (`127.0.0.1`, `localhost`, or `::1`). Never
  send the stored browser-session credential to a remote host.
- Inspect sessions before prompting. Prefer an explicit `session_id`; otherwise
  auto-selection is allowed only when exactly one running session matches.
- The default delivery mode is `queue`, which waits behind the current turn. Use
  `steer` only when the user explicitly wants to interrupt or redirect an active turn.
- Do not invent substantive prompt text. If the user did not provide text, ask for it
  or use a clearly labeled harmless connectivity test and report the exact text used.
- An `accepted: true` result proves admission, not completion. Set `wait_seconds` when
  the user needs the assistant response verified.
- Never print, log, or include the browser-session secret, signed cookie, or launch token.

## CLI fallback

List sessions:

```bash
python3 scripts/dsh_cli_session.py --list
```

Queue a prompt to a known session:

```bash
python3 scripts/dsh_cli_session.py \
  --session-id SESSION_ID \
  --wait 120 \
  'Prompt text supplied by the user'
```

The helper reads the persistent browser-session grant from `${DSH_HOME:-$HOME/.dsh}/.credentials.yaml`,
creates the authority-bound signed cookie locally, and calls the live server's
`session/prompt` Remote endpoint. It does not launch a browser or create a separate
headless session.

## Verification

After sending, report the selected session id/title, mode (`queue` or `steer`), whether
the server returned `accepted: true`, and—if waiting—the observed assistant response or
a clear timeout with the session still pending. Preserve server error code/messages and
distinguish local authentication, session selection, prompt admission, and provider/turn
failures.
