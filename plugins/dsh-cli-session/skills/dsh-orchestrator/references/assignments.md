# Assignment examples

Adapt the relevant example to the actual task. Replace illustrative paths and decisions with verified context. Omit fields that add no information; these are prompts, not a form the worker must fill out before working.

## Investigation before a decision

Use when a missing fact affects the implementation. Keep the answer narrow enough to guide the next decision.

```text
Determine why cancelling a queued job sometimes leaves the UI showing Running.
Read-only ownership: queue state transitions, their API response, and the UI consumer.
Locate the relevant symbols and tests; do not inspect unrelated subsystems.
Return the supported cause with file/symbol references, any uncertainty, and the
smallest plausible fix. Do not edit files or change a live queue. Stop after
answering this question; the host will decide the implementation scope.
```

The host reads the findings and the decisive source locations before freezing a fix. If the evidence is incomplete, request the missing observation rather than assuming the proposed cause is correct.

## Implementation with clear ownership

Use once the relevant architecture and constraints are established. The details below are illustrative, not universal queue policy.

```text
Implement the reviewed cancellation-state fix.
Own src/jobs/state.py and its focused tests. The UI worker owns frontend/;
you are not alone in the repository, so preserve unrelated edits.

Established decisions: the API keeps its current schema; cancelled is terminal;
the existing transition operation remains the single writer of job state.
If the inspected code contradicts these assumptions, report the evidence before
changing the public contract. Choose ordinary implementation details yourself.

Acceptance: a queued job cancelled before claim cannot become Running, and a
running job retains the existing cooperative-cancellation behavior. Add or adjust
the focused regression coverage and run the relevant tests. No service restart,
dependency installation, broader queue refactor, commit, or push in this assignment.

Return the change, affected files, test commands/results, and unresolved issues.
Leave the patch for host review and stop after these checks pass.
```

The host reviews the resulting transitions and relevant diff. It does not repeat the worker's entire search or run a larger suite merely because the worker finished.

## Specific review correction

Use for a demonstrated defect in an otherwise accepted change.

```text
In src/jobs/state.py, the new claim path reads state before acquiring the existing
transaction. Cancellation can therefore race with the later write. Move the
state check into that transaction using the established pattern; preserve the
API and the rest of the reviewed implementation. Own only this fix and its
focused regression test. Verify the concurrent cancellation case, report the
result, and stop. No new review areas or unrelated cleanup.
```

## Small change

For a simple documentation correction, one paragraph is sufficient:

```text
Own only docs/OPERATIONS.md. Its launch example uses the old port; the verified
current default is 8767. Correct that example, preserve unrelated changes, and
run git diff --check. Report the changed line and stop. No source changes,
service operations, or test-suite run is needed for this edit.
```

## Waiting and scope changes

- If a watcher times out while execution remains active, keep the assignment and cursor and wait again. Do not send the prompt again.
- If a check fails, inspect its actual failure and authorize the next bounded correction. Do not relax acceptance merely to get a passing result.
- If the user removes recipe design from an integration task, record that exclusion and redirect affected active work. Complete the remaining integration; do not resume the old design assignment later.
- If the user requests a status update, answer from the latest observed state and continue the existing assignment. The question does not create another worker task.

## Completion report

A useful report is usually a few lines:

```text
Outcome: completed, partially completed, or blocked, with the concrete result.
Changed: relevant files or artifacts.
Verified: checks actually run, results, and published evidence references.
Unresolved: material limits or the decision needed; omit when there are none.
```

Keep reported evidence distinct from host verification. A compact report must still disclose failed checks, skipped required work, or changes to the agreed scope.
