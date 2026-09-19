---
name: dsh-orchestrator
description: Orchestrate engineering work delegated to local DeepSeek Harness workers. Use when the user wants DSH implementation with the host retaining architecture, scope, and review. Covers bounded assignments, patient supervision, selective evidence reads, and targeted corrections. Simple session inspection or verbatim prompt relay uses dsh-cli-session instead.
---

# DSH Orchestrator

Keep the host responsible for architecture, scope, acceptance, and integration. Use DSH for bounded investigation and implementation. The goal is fewer unnecessary decisions and repeated reads while preserving correctness. Respect a request for direct work; this skill does not require delegation of every task or authorize work beyond the user's request.

## Establish the assignment

Read the applicable project instructions and enough context to identify the requested outcome, existing contracts, and material risks. Do not explore the whole repository before assigning a worker the same exploration. When a missing fact affects the design, send a bounded investigation first; distinguish confirmed decisions from assumptions.

Choose the smallest coherent deliverable that can be implemented and reviewed on its own. Give each worker:

- the outcome and relevant context;
- ownership of files, modules, or a search area, with explicit read-only or editing authority;
- established interfaces and invariants, plus any open question;
- task-specific exclusions and execution limits;
- observable acceptance checks and the expected evidence;
- a stopping condition.

Use a short paragraph for a small fix. Use the [assignment examples](references/assignments.md) when an investigation, implementation, or correction needs more structure. Do not send the entire conversation when a few decisions and source pointers suffice.

Allow ordinary implementation choices within the assignment. Ask the worker to surface evidence that contradicts the design or requires a material scope change, rather than forcing a guessed solution. Preserve other people's edits. Parallelize only independent assignments with separate ownership when useful and authorized; avoid overlapping editors and recursive delegation trees.

The worker should return the outcome, changed files, checks actually run and their results, unresolved issues, and evidence references. Request a concise completion report; use short progress updates for meaningful changes or blockers. Do not request private reasoning, impose an arbitrary reasoning budget, or change model settings to simulate efficiency.

## Dispatch once and supervise patiently

Read the available `dsh-cli-session` skill once for the transport contract. Use its MCP tools. Select and verify the intended session and working directory before sending; name an alternate target directory explicitly if an existing session is used for another workspace. Do not guess among ambiguous sessions or create a new one merely because a worker is quiet.

Keep the session ID, assignment ID, submission key, and latest cursor with the assignment. Use `steer` for ordinary prompts so Codex input reaches the active turn. Use `queue` explicitly when work must wait for the next turn; do not steer merely to hurry the worker when the requested delivery is intentionally deferred.

Admission is not completion. For an ambiguous send, inspect the same assignment or retry the identical payload with the same submission key. Never create a new logical submission to retry an uncertain one.

Wait with `dsh_wait_output(kind="time_limit", timeout_s=...)` and the latest cursor, normally in 30–60 second intervals with compact output bounds. Use `kind="waiting_for_input"` when the worker is expected to ask for approval or another answer. Inspect the returned `state_change`, `terminal`, `timeout`, and `unavailable` fields; drain `hasMore` output using the returned cursor. A watch timeout, provider retry, or quiet interval alone does not mean the worker failed. Continue observing the same assignment. Investigate a reported failure, missing observation, or concrete evidence of a stall without blindly resending the work.

While waiting, do only useful independent work already within scope. If there is none, wait. Do not invent audits, repeat the worker's implementation, or send reminders that become duplicate assignments. Keep the user informed of meaningful progress and required decisions.

## Review enough to decide

Start with the completion report and changed-file list. Use bounded `dsh_read_evidence` reads for published details. Inspect the relevant diff and critical code paths yourself; widen the review when risk, missing evidence, or contradictions justify it. Do not scrape raw session logs or reasoning to reconstruct work already summarized through the bridge.

Treat worker claims as reports, not proof. Check acceptance against actual artifacts and results. Reuse valid test evidence; repeat or broaden checks only for changed behavior, gaps, failures, or material risk. If independent review is warranted, give it a narrow question and separate ownership rather than opening another unrestricted audit.

For defects, send a concrete correction: location, observed problem, required behavior, scope of the fix, and necessary verification. Preserve accepted decisions unless new evidence changes them. When a correction loop repeats without progress, identify the unresolved decision or missing evidence and adjust the assignment; do not keep requesting a general re-review.

Once acceptance is met, close that assignment and perform the remaining authorized integration or delivery steps. Stop when the user's outcome is complete. Put unrelated improvements in a brief deferred note only when useful; do not turn them into new work.

## Preserve scope across turns

Maintain a compact handoff in the task context or existing task notes: current objective, confirmed decisions, active assignments and cursors, evidence already reviewed, open blockers, and explicitly deferred work. Do not add a project management subsystem for this record.

A user correction updates the objective and affected assignments. Reconcile pending work before issuing a replacement; do not let obsolete instructions revive deferred work. After an interruption or compaction, inspect the recorded assignment before sending again. Reuse prior evidence and recheck facts that may have changed.

Authorization persists, but delegation does not expand it. Carry relevant limits into worker prompts, including read-only requests, downloads, live services, expensive runs, and publication. Report unavailable tools or genuine blockers plainly; never claim a worker ran, a check passed, or a result was published without evidence.
