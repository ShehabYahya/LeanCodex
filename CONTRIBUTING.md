# Contributing

Thanks for helping improve LeanCodex.

## Before opening a pull request

Please keep changes focused and explain the user-visible behavior they affect. For protocol or compatibility changes, include the relevant DeepSeek Harness event/RPC contract and a deterministic regression test.

## Local checks

```bash
python -m compileall -q plugins/dsh-cli-session scripts tests
node --check plugins/dsh-cli-session/scripts/launch_mcp.js
python -m unittest -v tests/test_supervision.py
python tests/test_mcp_stdio.py
python tests/test_install_smoke.py
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
