# Contributing

Thanks for helping improve DSH CLI Session.

## Before opening a pull request

Please keep changes focused and explain the user-visible behavior they affect. For protocol or compatibility changes, include the relevant DeepSeek Harness event/RPC contract and a deterministic regression test.

## Local checks

```bash
python3 -m py_compile \
  plugins/dsh-cli-session/scripts/dsh_mcp_server.py \
  plugins/dsh-cli-session/skills/dsh-cli-session/scripts/dsh_*.py

python3 -m unittest -v tests/test_supervision.py
python3 tests/test_mcp_stdio.py
```

The deterministic suite should not require paid model/provider calls.

## Design principles

- preserve exact assignment identity;
- fail closed when continuity or ownership cannot be proven;
- keep the normal output feed bounded;
- do not expose private reasoning or raw tool traffic;
- distinguish observation failures from execution failures;
- keep the MCP interface and `dsh-cli-session` skill neutral about orchestration; workflow guidance belongs in the separately selected `dsh-orchestrator` skill;
- keep credentials local and loopback-only.

## Pull requests

A good pull request includes:

- a concise problem statement;
- the smallest compatible change;
- regression coverage;
- documentation updates when the public contract changes.

For security-sensitive findings, follow [SECURITY.md](SECURITY.md) instead of posting exploit details publicly.
