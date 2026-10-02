# commit-workflow

Rules for an AI coding agent (Claude Code) to commit and review code in small, reviewed steps. The full rules are in
[commit-workflow.md](commit-workflow.md).

## Summary

- **Small commits:** 400 lines of diff at most, not counting generated, lock or vendored files. Each commit builds
  and passes tests on its own. Large work starts with a published commit plan.
- **Review every commit:** a fresh-context subagent reviews each commit, and fixes are amended in until the review
  is clean. Only bugs, behaviour changes, incompatibilities and maintenance traps block.
- **Bounded rounds:** later rounds review only what changed since the last round. Review stops after 3 rounds, or 5
  if something major keeps turning up.
- **Push and CI:** a clean commit is pushed to the current branch and checked against CI before work continues.
  Pushed history is never rewritten.
- **Model choice:** the reviewer runs on the committer's model for logic-heavy diffs and on a lower model for
  mechanical changes and docs. The user reviews tiny commits.
- **Whole-branch review:** the full branch gets one more review before it is merged.

## Use

Copy the rules into `~/.claude/CLAUDE.md` (all projects) or a project's `CLAUDE.md`.
