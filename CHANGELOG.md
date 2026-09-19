# Changelog

All notable public releases are documented here.

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
