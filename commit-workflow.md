# Commit and review workflow

## Commit size
- A single commit must never exceed 400 lines of diff (insertions + deletions). Check `git diff --stat` before
  committing and split larger work into several logical commits (e.g. engine change, then tests, then data).

## Commit review workflow
- Work one commit per step. After each commit, start a fresh-context subagent (no inherited conversation) that runs
  the code-review skill on the commit's diff. Amend fixes into the same commit (no fixup commits); amend only unpushed
  commits and never rewrite pushed history (a problem found after a push gets a new commit, and say so).
- When the review is clean, push automatically, confirm CI passes, then continue. Report findings and fixes briefly
  with each commit.
- Self-check every fixup before re-review. When a comment is flagged, cut or simplify it rather than expanding it;
  think design tweaks through rather than patching symptoms.
- Severity bar (state it in the reviewer prompt): only bugs, behaviour changes, platform/version incompatibilities and
  real maintenance traps block. Comment/wording nits are fixed in one batch without another review round.
- Rounds 2+ review only the amended delta against the previous round's commit, still with a fresh-context reviewer.
- Stop after 3 rounds and report what is left, unless a round finds something major (bug or behaviour change):
  then keep going.
- Models: when proposing commits, give model + effort for the committer and for the reviewer subagents. The user sets
  the committer model; I launch each reviewer subagent on the planned model myself.
  - Reviewer = committer's model for logic-heavy or behaviour-sensitive diffs; a lower model (e.g. Sonnet) for
    mechanical moves, renames and docs. Tell the reviewer whether the commit is meant to preserve behaviour.
  - On a logic-heavy diff, once a round's findings are only minor/nits, run later rounds on a lower model.
- After each commit, suggest model + effort (committer and reviewer) for the next one.
