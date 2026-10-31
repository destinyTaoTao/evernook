# Issue tracker: GitHub

Issues and specs live in GitHub Issues for `destinyTaoTao/evernook`.
Use the `gh` CLI from this checkout; infer the repository from the Git remote.

## Conventions

- Create: `gh issue create --title "..." --body-file <path>`
- Read: `gh issue view <number> --comments`
- List: `gh issue list --state open`, adding label filters as needed.
- Comment: `gh issue comment <number> --body-file <path>`
- Apply labels: `gh issue edit <number> --add-label "..."`
- Remove labels: `gh issue edit <number> --remove-label "..."`
- Close: `gh issue close <number> --comment "..."`

For multiline content, write the exact text to a temporary file and use
`--body-file`. Keep private dialogue, persona data and credentials out of issues.

When a skill says "publish to the issue tracker", create a GitHub issue.
When a skill says "fetch the relevant ticket", read the issue and its comments.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub issues and pull requests share a number space. Resolve ambiguous
references before acting.

## Wayfinding operations

- The map is one issue labelled `wayfinder:map`, holding notes, decisions and
  unresolved questions.
- Child tickets use `wayfinder:research`, `wayfinder:prototype`,
  `wayfinder:grilling` or `wayfinder:task`.
- Link children through GitHub sub-issues when available. Otherwise use a task
  list in the map and `Part of #<map>` in each child.
- Record blockers through native issue dependencies when available. Otherwise
  use `Blocked by: #<number>` references.
- A ticket is available only when all blockers are closed and it has no assignee.
  Select the first available ticket in map order.
- Claim by assigning the ticket to the driving developer.
- Resolve by posting the result, closing the ticket and adding a linked decision
  summary to the map.

External writes require authorization from the active task; this configuration
alone does not authorize publishing, commenting, assigning or closing issues.
