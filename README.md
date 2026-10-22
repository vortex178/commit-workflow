# commit-workflow

Rules for an AI coding agent (Claude Code) to commit and review code in small, reviewed steps, and a Claude Code
plugin that enforces them: hooks stop `git push` and `gh pr create`/`merge` until the review has happened. The full
rules are in [skills/commit-workflow/SKILL.md](skills/commit-workflow/SKILL.md).

## Summary

- **Small commits:** 400 lines of code diff at most, not counting generated, lock or vendored files; docs and static
  data are exempt. Each commit builds and passes tests on its own. Large work starts with a published commit plan,
  ordered so commits on the same model sit together.
- **Review every commit:** a fresh-context subagent reviews each commit, and fixes are amended in until the review
  is clean. Only bugs, behaviour changes, incompatibilities and maintenance traps block.
- **Bounded rounds:** later rounds resume the first reviewer and check only what changed since the last round. Review
  stops after 3 rounds, or 5 if something major keeps turning up.
- **Push and CI:** a clean commit is pushed to the current branch and checked against CI before work continues.
  Pushed history is never rewritten. The push gate (below) holds back every push while a commit lacks a review mark,
  and tiny commits wait for your `Approved`.
- **Model choice:** the reviewer runs on the committer's model for logic-heavy diffs and on a lower model for
  mechanical changes and docs. The user reviews tiny commits.
- **Whole-branch review:** the full branch gets one more review before the pull request.

## Install

Requires Python 3.9+ and git (2.38+ lets the gate recognise conflict-free merges; with older git such merges need a
review too). The scripts use only the standard library. `gh` is needed only if you open pull requests with it.

```
/plugin marketplace add vortex178/commit-workflow
/plugin install commit-workflow@commit-workflow
```

The plugin brings the `commit-reviewer` agent, the scripts, the hooks and the `commit-workflow` skill, which holds
the rules. Claude loads the skill when it is about to commit, push, open a pull request or review. To make that
certain, add a pointer to your `~/.claude/CLAUDE.md` (all projects) or a project's `CLAUDE.md`:

```
Whenever you commit, amend, push, open or merge a pull request, or review code, first load the `commit-workflow`
skill and follow it. Review is mandatory for every commit.
```

## How it works

- **Review marks.** The `commit-reviewer` agent reads a commit through `scripts/show_change.py`, which prints the
  change in parts, and ends its report with `VERDICT: clean <sha>` or `VERDICT: blocking`. A `SubagentStop` hook
  recomputes the parts, and only if the reviewer's transcript shows all of them unaltered does it write a git note
  in `refs/notes/review` naming that SHA. Amending or rebasing makes a new SHA, which has no mark. Marks stay local:
  notes are not pushed, so a fresh clone starts with none.
- **Later rounds.** `show_change.py <sha> --from <prev> --context 30` shows what an amend changed, so the resumed
  reviewer reads only the delta.
- **Manual approval.** For tiny commits the agent writes `Reply 'Approved' to mark <sha> as manually reviewed`. A
  `UserPromptSubmit` hook marks the commit when your next message starts with `Approved` (or `Approved <sha>` to name
  commits), and tells both of you which SHAs it marked.
- **Carry-forward.** `scripts/carry.py` moves a mark to a rewritten commit: automatically at push time when the patch
  is unchanged, and by `carry.py <old-sha>` for nit fixes of at most 10 changed lines (once per commit).
- **Reviewer context.** `scripts/review_context.py <sha>` lists the functions a commit touches, where they are used,
  and lines that print, exit, touch global state, start processes or use the network. The committer pastes it into
  the reviewer prompt. It is plain text search, so it is approximate.
- **Push gate.** A `PreToolUse` hook on Bash and on tools that can send prompts denies `git push` (and `gh pr create`
  or `gh repo create --push`) while a commit it would send has no clean mark, naming the SHAs. It also denies agent
  writes to `refs/notes/review`, moves of remote-tracking refs, running the hook scripts, and prompts starting with
  `Approved` sent through tools. It reads only a plain `git push` run as its own command: pushes inside `$(...)`,
  `bash -c`, eval, an alias or a wrapper such as `env` or `sudo`, and pushes after a command that may change commits
  in the same line, are denied with a message saying to run them plainly.
- **Pull request gate.** `gh pr create` and `gh pr merge` are denied until the branch tip has a `branch-clean` mark:
  a reviewer read `show_change.py <tip> --base <merge-base>` and ended with `VERDICT: branch-clean <tip>`, and that
  base is on the pull request's base branch.

## Trust and sandbox

The gate stops an agent that forgets or talks itself out of a review. It does not stop one that deliberately writes
to `.git` by other means, so run the agent in the Claude Code sandbox and keep it away from the files that hold the
marks and the git configuration. The sandbox already protects `.git/hooks` and `.git/config`; add `.git/info` (it can
hide files), `.git/refs/notes` (the marks) and, in a linked worktree, the same paths of the main repository's `.git`
directory. In `.claude/settings.json` (check the
[sandbox docs](https://code.claude.com/docs/en/sandboxing) for the path forms your setup needs):

```json
{
  "sandbox": {
    "enabled": true,
    "filesystem": {
      "denyWrite": [".git/config", ".git/info", ".git/hooks", ".git/refs/notes"]
    }
  }
}
```

- **Outside the sandbox.** It covers shell commands only: the file tools (Edit, Write), MCP servers and hooks run
  outside it. Add permission deny rules for the file tools on the same `.git` paths. The hooks write the marks, so
  they still work; but `carry.py <old-sha>` run by the agent cannot write a mark inside the sandbox, so for nit fixes
  either run it outside the sandbox with your approval or have the commit reviewed again. Carrying an unchanged patch
  at push time happens in the hook and is not affected.
- **Prompt-injecting tools.** The approval hook cannot tell who typed a prompt; it relies on the agent being unable
  to send one. The gate denies prompts starting with `Approved` from the tools it covers (scheduled prompts, loops,
  messages to sessions, MCP tools by their prompt-like fields). A tool that injects user-style messages by another
  route, such as a channel that pushes messages into the session, can approve a commit: install none you do not trust.
- **Remotes.** A commit counts as already pushed when it is on the remote being pushed to or on any remote with a
  network URL, so those commits skip the gate. Remotes whose URL is a local path do not count. Add only remotes whose
  branches you trust, and do not point a remote at a fork you do not control.
- **What a mark proves.** That a reviewer ran on that exact SHA and passed it, not that the review was good. The
  committer still writes the reviewer's prompt.

## Known limits

- The gate reads a command line, not scripts written to disk, edits of `.git` files, third-party pushers (`jj`, `gt`,
  `hub`) or tools that commit and push (`npm version`): a push after a command it does not know to be harmless is
  denied, but a push made inside another tool is not seen.
- Denied by design rather than handled: wrappers around `git push` or `gh pr`, `export GIT_SSH_COMMAND=...` before a
  push, loops that push before they commit.
- Falsely denied: a commit message that quotes a notes setting after `;` or `(` (use `git commit -F file`).
- `gh pr` checks: `gh api` merges, `gh` aliases, `gh pr edit --base` and the web UI are not covered; commits added to
  the remote branch since the last fetch are not seen.
- `git remote set-url` and edits to the skill, the agent or the hooks are not watched; the sandbox rules above and
  review of those files are the protection.
- Binary content cannot be shown to a reviewer: such a commit needs your `Approved`, and a branch containing it gets
  no branch mark, so open its pull request yourself.

## Development

```
python3 -m unittest discover -s tests -q
```

CI runs the tests on Ubuntu and macOS with Python 3.10 and 3.13, and on Ubuntu with Python 3.9. Lines are at most 120
characters.
