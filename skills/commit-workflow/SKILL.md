---
name: commit-workflow
description: >-
  Rules for committing, pushing and reviewing code in small, reviewed steps. Use whenever you are about to commit,
  amend, push, open or merge a pull request, or review a commit or branch, and when planning work that needs several
  commits.
---

# Commit and review workflow

## Reference
Thresholds are collected here; the procedure and the other sections refer back to them. This skill ships with the
commit-workflow plugin (reviewer agent, scripts, hooks) and its script paths use `${CLAUDE_PLUGIN_ROOT}`; if the
plugin is not available, follow the same steps by hand: a general-purpose reviewer, `git show` for the change, no
marks.
- Cap: a code commit must never exceed 400 lines of diff (insertions + deletions). Measure with `git diff --stat -M`
  so moves and renames count once, and leave generated, lock, binary and vendored files out of the count. In verbose
  languages (Java, generated Go, SwiftUI, XML layouts, Terraform) the user may set a per-repo cap instead.
- The cap applies to code. Documentation and static data (e.g. fixtures, datasets) may exceed it in a single commit
  (review is still mandatory). Config files and seed scripts count as code.
- Aim for about 350 lines in a first commit so review fixes fit. If fixes would push it past 400, plan the split once
  by feature rather than trimming line by line.
- Plan: work that needs many commits (roughly 1000+ lines in total) gets a published commit plan before the first
  commit.
- Tiny commit: under ~10 lines, or comment/docs edits under ~50 lines.
- Nit carry: an amended commit takes over the reviewed commit's mark only for nit fixes of at most 10 changed lines,
  once per commit; anything larger needs a review.
- Severity bar (state it in the reviewer prompt): only bugs, behaviour changes, platform/version incompatibilities and
  real maintenance traps block. Comment/wording nits are fixed in one batch without another review round. Tell the
  reviewer to report every instance of a problem, not just the first.
- Rounds: stop after 3 rounds and report what is left, unless a round finds something major (bug or behaviour
  change): then keep going, up to 5 rounds, then stop and ask the user. Rounds 2+ resume the round-1 reviewer
  unless the fix delta is over ~50 lines or changes the design.
- Models: when proposing commits, give model + effort for the committer and for the reviewer subagents. The user sets
  the committer model; I launch each reviewer subagent on the planned model myself.
  - Reviewer = committer's model for logic-heavy or behaviour-sensitive diffs and for prompt files; a lower model
    (e.g. Sonnet) for mechanical moves, renames and docs. Tell the reviewer whether the commit is meant to preserve
    behaviour.
  - On a logic-heavy diff, once a round's findings are only minor/nits, any fresh reviewer started for later rounds
    runs on a lower model; a resumed reviewer keeps its model. A finding that today's code cannot reach counts as
    minor here; it is still fixed.
  - Lowering applies only when the committer is Sonnet or above; a Haiku committer gets a Haiku reviewer.

## Procedure for each commit
Work one commit per step, on the branch checked out in the session workspace.
1. Before the first commit, check how the code being changed or called into behaves at runtime (writes to
   stdout/stderr, process exits, global state, network calls). Keep what you found, plus the relevant files, for the
   reviewer's intent note. If the work needs a plan (Reference), publish it and wait for the user's approval: order the
   commits so that those needing the same committer model (and, as far as possible, the same reviewer model and
   effort) sit next to each other, because the user switches models by hand; build the dependency graph first so each
   commit still builds and passes tests on its own; give model + effort for the committer and the reviewers; say
   where the user has to switch models.
2. Commit within the cap. Split larger work into logical commits that each build and pass tests on their own, by
   feature slice, with the tests in the same commit as the code they cover. Keep commits readable by a human in one
   sitting and cheap to review. A mechanical commit that cannot be split (formatter run, dependency bump, rename
   sweep) may exceed the cap if its message says so; review it on a lower model where the Models rules allow.
3. Gather context for the reviewer: run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/review_context.py" <sha>` (touched
   functions and where they are used, runtime hazards; plain text search, so approximate) and paste the output into
   the reviewer prompt. Add the commit message, a short statement of intent and known constraints, the runtime
   findings from step 1, the classification (logic-heavy, behaviour-sensitive, mechanical, docs, security-sensitive,
   prompt file), whether behaviour should be preserved, and the severity bar.
4. Review, always; a request to commit or push does not skip it, including the user review of tiny commits. Tiny
   commits: ask the user to review manually, with the line `Reply 'Approved' to mark <sha> as manually reviewed`; if
   they decline, run one review on Haiku, whatever the committer model. Every other commit: start a fresh-context
   subagent (no inherited conversation), the `commit-workflow:commit-reviewer` agent (without the plugin, a
   general-purpose subagent that runs the code-review skill), on the commit's diff, on the model the Models rules
   give. Security-sensitive changes (auth, crypto, input parsing, permissions) also get a security-review pass before
   pushing.
5. Fix the findings. Report each in one line with its fix (or why it was left). Amend fixes into the same commit (no
   fixup commits); amend only unpushed commits and never rewrite pushed history (a problem found after a push gets a
   new commit, and say so). When fixing a finding, search sibling code paths for the same pattern, fix every instance,
   and tell the next reviewer which ones were checked. Self-check every fixup before re-review. When a comment is
   flagged, cut or simplify it rather than expanding it; think design tweaks through rather than patching symptoms.
   When only comment/wording nits were fixed, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/carry.py" <old-sha>`
   (Reference: Nit carry); if it refuses or cannot write, have the commit reviewed again.
6. Later rounds review only the amended delta against the previous round's commit, with nearby code: `python3
   "${CLAUDE_PLUGIN_ROOT}/scripts/show_change.py" <sha> --from <prev> --context 30`. Resume the round-1 reviewer
   (SendMessage) instead of starting a new one, so its earlier reading is reused. Its job is verify-only: check that
   each finding is fixed and the delta adds no new problem, and raise new findings on unchanged code only if they are
   major. Start a fresh-context reviewer only in the cases the Reference names, or when the reviewer can no longer be
   resumed. If a finding reverses an earlier round's request, ask the user instead of applying it. Observe the round cap
   (Reference).
7. When the review is clean, push to the current branch, confirm CI passes, then continue. Without CI, run the
   project's local test and lint commands instead. With slow CI, start the next commit while it runs; a failure then
   gets a new commit. Re-run a suspected flaky failure once before treating it as real. While an earlier commit
   awaits the user's approval, the push gate blocks every push: keep the later commits local and push at the end of
   the run, once approved.
8. After each commit, suggest model + effort (committer and reviewer) for the next one.
After the last commit on a branch, review the whole branch once (same rules) before opening or merging the PR, to
catch problems that span commits: the reviewer reads
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/show_change.py" <tip> --base <merge-base with the PR's base branch>` and ends
with `VERDICT: branch-clean <tip>`.
Keep context lean: fetch only the needed part of a page (a targeted query or one section), and never print the whole
environment or other large outputs.

## By kind of change
- Bug fix: write the regression test first and see it fail; the fix and the test go in one commit.
- Dependency bump: read the changelog for breaking changes (only the relevant section); mechanical commit.
- Refactor: behaviour must not change; the same tests pass before and after (add missing tests in an earlier
  commit); tell the reviewer behaviour should be preserved.
- Prompt files (rules, agent or skill text) change behaviour like code: review them as logic-heavy, on the committer's
  model.
- Docs and comments: reviewed on a lower model where the Models rules allow.
- A non-trivial merge-conflict resolution is its own commit with its own review.

## Multi-commit requests
Load the procedure once and repeat it for each commit. Stop only at: a published plan, until the user approves it; a
model-switch point in the plan; a round cap, or a finding that reverses an earlier one; the end of the run, to ask
once for the queued tiny-commit approvals, listing every pending SHA and asking for `Approved <sha> <sha> ...` (a
bare `Approved` marks nothing when several are pending; commits waiting for approval stay local, and pushing resumes
after the user's `Approved`). If the user's request asks for a Haiku review of small commits, tiny commits go to a
Haiku reviewer and the run does not stop for them; this applies to that request only.

## Hooks
- Marks: the reviewer's last line is `VERDICT: clean <sha>`, `VERDICT: blocking` or (whole branch)
  `VERDICT: branch-clean <tip>`. A hook, not the model, writes the mark as a git note in `refs/notes/review`, and only
  after the reviewer's transcript shows every part of the change printed by `show_change.py <sha>`, unfiltered, each
  part in its own command (a later round reads `show_change.py <sha> --from <prev> --context 30`, after reading
  <prev> that way). Binary content cannot be shown: say so, and ask the user to approve it manually. Amending makes a
  new SHA with no mark.
- Never write, move or copy marks or remote-tracking refs yourself, run the hook scripts, or ask another session to
  approve anything. Marks carry over only through `carry.py`: automatically when a rewritten commit's patch is
  unchanged, and by `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/carry.py" <old-sha>` for nit fixes (Reference: Nit carry).
- Approval: only a message the user writes, starting with `Approved`, marks tiny commits as manually reviewed. Never
  send, schedule or type that word as an agent.
- Push gate: `git push` (and `gh pr create`) is denied while a commit it sends lacks a mark. Run it as its own plain
  command: not inside `$(...)`, `bash -c`, eval, an alias or a wrapper such as env or sudo, not after other commands
  in the same line (a `cd` is fine), with the remote and branches named literally, without VAR= settings other than
  GIT_DIR, GIT_TERMINAL_PROMPT and GIT_TRACE* (for gh: GH_*, NO_COLOR, PAGER, GIT_TERMINAL_PROMPT and GIT_TRACE*).
  Commits that change a submodule are pushed with `--recurse-submodules=check`, after the submodule itself. Commits
  already on the push target or on a remote with a network URL count as pushed, and a merge git made without
  conflicts needs no mark.
- Pull request gate: `gh pr create` and `gh pr merge` are denied until the branch tip has a `branch-clean` mark
  from the PR's base branch. Run them plainly from the branch's checkout, with HEAD equal to its upstream, without
  `-R`, `GH_REPO`/`GH_HOST`, a PR argument to merge, or `--head` naming another branch or a fork.

## Languages and file types (untested, revisit)
- Jupyter notebooks: strip outputs before committing, or review a converted script (e.g. jupytext).
- Infrastructure as code and SQL migrations: give the reviewer the plan output or the up/down SQL; an irreversible
  migration blocks by default.
- Uncommon languages and DSLs: lean on compiler and test output more than on the reviewer's judgment.
