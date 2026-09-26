# Security Policy

## Scope

LeanCodex handles local DSH browser-session credentials, assignment identity, cursors, and model-visible output. Security reports involving credential leakage, cursor forgery, cross-assignment data exposure, path traversal, symlink races, or non-loopback transport are especially important.

## Reporting a vulnerability

Please use GitHub's **Report a vulnerability** / private security advisory flow for this repository if it is available.

If private reporting is unavailable, open a minimal issue asking for a private contact channel **without including secrets, exploit payloads, credentials, or sensitive session data**.

## Supported version

Security fixes target the latest public release and `main`.

## Please do not include

- DSH browser-session secrets;
- signed cookies;
- API keys;
- private prompts or transcripts;
- private repository contents;
- provider credentials.

## Security properties

The current release is designed to:

- refuse non-loopback DSH endpoints;
- keep browser-session credentials out of MCP tool arguments;
- use signed assignment-bound cursors;
- restrict evidence reads to assignment-owned references;
- revalidate published artifact paths and reject current symlinks;
- exclude internal reasoning and raw tool traffic from the normal output feed.

See [`docs/NATIVE_CONTRACT.md`](docs/NATIVE_CONTRACT.md) for the protocol assumptions behind these properties.

## Runtime dependency bootstrap

The local launcher requires Python 3.11+ and pins the MCP SDK in `plugins/dsh-cli-session/requirements.txt`. If the selected Python does not already provide that exact MCP version, the launcher uses that Python's pip to install dependencies into LeanCodex's private cache. It does not install into the selected interpreter's site-packages. Set `LEANCODEX_RUNTIME_DIR` to control the cache location or preinstall the pinned dependency to avoid bootstrap network access.
