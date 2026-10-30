# Domain Docs

## Layout

This repository uses a single-context layout:

- `CONTEXT.md` at the repository root: domain vocabulary and resolved concepts.
- `docs/adr/`: architecture decision records.

These locations are conventions, not evidence that the files already exist.

## Reading rules

Before exploring, read root `CONTEXT.md` if present and ADRs relevant to the task.
Continue to read `README.md` and relevant project docs as required by `AGENTS.md`.

If domain documents do not exist, proceed silently. Do not create placeholders
or propose creating them solely because they are absent. The domain-modeling
skill creates them when terminology or decisions are actually resolved.

Use the vocabulary defined in `CONTEXT.md` when naming concepts in issues,
designs, code and tests. If a needed concept is missing, reconsider the wording
or identify the gap for domain modeling.

If a proposal conflicts with an ADR, identify the specific conflict and explain
why the decision should be revisited. Do not silently override it.

Distinguish implemented behavior, facts, inferences and proposals.
