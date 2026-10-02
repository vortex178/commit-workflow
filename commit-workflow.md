# Commit and review workflow

## Commit size
- A single commit must never exceed 400 lines of diff (insertions + deletions). Measure with `git diff --stat -M` so
  moves and renames count once, and leave generated, lock, binary and vendored files out of the count.
- Split larger work into logical commits that each build and pass tests on their own. Split by feature slice and keep
  tests in the same commit as the code they cover.
- A mechanical commit that cannot be split (formatter run, dependency bump, rename sweep) may exceed the cap if its
  message says so; review it on a lower model where the Models rules allow.
- Keep commits readable by a human in one sitting and cheap to review. When work needs many commits (roughly 1000+
  lines in total), publish the commit plan for the user before starting.

## Commit review workflow
- Work one commit per step, on the branch checked out in the session workspace. After each commit, start a
  fresh-context subagent (no inherited conversation) that runs the code-review skill on the commit's diff. Give it the
  commit message and a short statement of intent and known constraints. Amend fixes into the same commit (no fixup
  commits); amend only unpushed commits and never rewrite pushed history (a problem found after a push gets a new
  commit, and say so).
- Tiny commits (under ~10 lines, or comments/docs only): ask the user to review manually; if they decline, run one
  review on Haiku, whatever the committer model.
- When the review is clean, push to the current branch, confirm CI passes, then continue. Without CI, run the
  project's local test and lint commands instead. With slow CI, start the next commit while it runs; a failure then
  gets a new commit. Re-run a suspected flaky failure once before treating it as real.
- Report each finding in one line with its fix (or why it was left).
- Self-check every fixup before re-review. When a comment is flagged, cut or simplify it rather than expanding it;
  think design tweaks through rather than patching symptoms.
- Severity bar (state it in the reviewer prompt): only bugs, behaviour changes, platform/version incompatibilities and
  real maintenance traps block. Comment/wording nits are fixed in one batch without another review round.
- Security-sensitive changes (auth, crypto, input parsing, permissions) also get a security-review pass before pushing.
- Rounds 2+ review only the amended delta against the previous round's commit, still with a fresh-context reviewer.
  If a finding reverses an earlier round's request, ask the user instead of applying it.
- Stop after 3 rounds and report what is left, unless a round finds something major (bug or behaviour change): then
  keep going, up to 5 rounds, then stop and ask the user.
- A non-trivial merge-conflict resolution is its own commit with its own review.
- After the last commit on a branch, review the whole branch diff once (same rules) before opening or merging the PR,
  to catch problems that span commits.
- Models: when proposing commits, give model + effort for the committer and for the reviewer subagents. The user sets
  the committer model; I launch each reviewer subagent on the planned model myself.
  - Reviewer = committer's model for logic-heavy or behaviour-sensitive diffs; a lower model (e.g. Sonnet) for
    mechanical moves, renames and docs. Tell the reviewer whether the commit is meant to preserve behaviour.
  - On a logic-heavy diff, once a round's findings are only minor/nits, run later rounds on a lower model.
  - Lowering applies only when the committer is Sonnet or above; a Haiku committer gets a Haiku reviewer.
- After each commit, suggest model + effort (committer and reviewer) for the next one.

## Languages and file types (untested, revisit)
- Verbose languages (Java, generated Go, SwiftUI, XML layouts, Terraform): the user may set a per-repo cap instead of 400.
- Jupyter notebooks: strip outputs before committing, or review a converted script (e.g. jupytext).
- Infrastructure as code and SQL migrations: give the reviewer the plan output or the up/down SQL; an irreversible
  migration blocks by default.
- Uncommon languages and DSLs: lean on compiler and test output more than on the reviewer's judgment.
