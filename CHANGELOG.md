# Changelog

All notable public releases are documented here.

## [1.0.3] - 2026-09-25

LeanCodex branding and release-preparation update.

### Changed

- rebranded the public project and plugin display surfaces from DSH CLI Session to **LeanCodex**;
- updated repository badges, installation commands, metadata, and review-bundle identity for the renamed GitHub repository;
- preserved the existing `dsh-cli-session` plugin/skill IDs, directories, MCP server key, tool names, and local state path for compatibility;
- replaced the one-off v1.0.0 publisher with a tag-driven release workflow that verifies the tag matches the plugin manifest version and runs the deterministic test suite before publishing.

## [1.0.0] - 2026-09-19

First publication-ready release.

### Added

- durable DSH assignment correlation using native request IDs;
- caller-stable submission keys for retry-safe admission;
- signed, assignment-bound cursors;
- incremental finalized-output delivery;
- lossless chunked continuation for long messages;
- compact provider retry, waiting-for-input, child lifecycle, evidence, and terminal signals;
- bounded session discovery and compact assignment inspection;
- bounded assignment-owned evidence reads;
- deterministic credential redaction and artifact path/symlink protections;
- typed MCP 2.2 input/output contracts;
- model-neutral plugin/skill surfaces;
- deterministic CI coverage and MCP stdio contract verification.

### Changed

- removed prompt-prefix response attribution;
- replaced transcript/script-style supervision with a structured incremental interface;
- removed model-specific orchestration guidance from plugin-visible surfaces.

### Known limitations

- unsolicited MCP push/wakeup into model context is not claimed;
- runtime compatibility depends on the locally installed DeepSeek Harness version;
- the repository currently retains its existing proprietary license metadata.
