import json
import os
import unittest

from helpers import SCRIPTS, RepoCase, run, run_hook

import review_notes

CLEAN = {"verdict": "clean", "kind": "agent"}


class GateCase(RepoCase):
    def gate(self, command, tool="Bash", cwd=None):
        tool_input = {"command": command} if tool == "Bash" else command
        result = run_hook("gate.py", {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
                                      "cwd": cwd or self.repo})
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"] if result.stdout else ""

    def assert_denied(self, command, text, **kwargs):
        self.assertIn(text, self.gate(command, **kwargs), command)

    def assert_allowed(self, command, **kwargs):
        self.assertEqual(self.gate(command, **kwargs), "", command)


class MarkLockTest(GateCase):
    def test_mark_writes_are_blocked(self):
        self.git("config", "alias.mark", "notes --ref review add")
        for command in ["git notes --ref review add -m x HEAD", "git notes --ref=refs/notes/review add -m x",
                        "git notes --re review append -m x", "git -c core.notesRef=refs/notes/review notes add -m x",
                        "GIT_NOTES_REF=refs/notes/review git notes add -m x", "git update-ref refs/notes/review HEAD",
                        "export GIT_NOTES_REF=refs/notes/review; git notes add -m x", "env -i git mark -m x",
                        "if true; then git notes --ref review add -m x; fi", "git -C . mark", "git \\\n mark",
                        "git push origin refs/notes/review", "git notes --ref review remove HEAD",
                        "git config core.notesRef refs/notes/review", "git fetch origin '+refs/notes/*:refs/notes/*'",
                        "git push origin 'refs/notes/*'", "git fetch origin '+refs/*:refs/*'",
                        "git push origin '+refs/*:refs/*'", "git -c notes.rewriteRef=refs/notes/review commit --amend",
                        "GIT_NOTES_REWRITE_REF=refs/notes/review git commit --amend",
                        "git config notes.rewriteRef 'refs/notes/*'"]:
            self.assert_denied(command, "review marks are written only")
        self.git("remote", "add", "origin", self.repo)
        for command in ["git update-ref refs/remotes/origin/main HEAD", "git fetch . HEAD:refs/remotes/origin/main",
                        "git pull . main:remotes/origin/main", "git fetch origin main:refs/remotes/upstream/main",
                        "git push . HEAD:refs/remotes/origin/main", "git symbolic-ref refs/remotes/origin/HEAD x",
                        "git fetch ../c '+refs/r*:refs/r*'", "git fetch --refmap='+refs/heads/*:refs/remotes/o/*' . x",
                        "git fetch --refmap +refs/heads/*:refs/remotes/o/* . x"]:
            self.assert_denied(command, "remote-tracking refs move only")
        self.assert_denied("echo 'update refs/remotes/origin/main HEAD' | git update-ref --stdin", "--stdin")
        for command in ["echo HEAD:refs/remotes/origin/main | git fetch --stdin .",
                        "git -c url./tmp/c.insteadOf=%s fetch origin" % self.repo, "git -c remote.origin.url=x pull",
                        "GIT_CONFIG_COUNT=1 git fetch origin"]:
            self.assert_denied(command, "configured URL and refspecs")

    def test_hook_scripts_and_their_code_are_blocked(self):
        script = os.path.join(SCRIPTS, "on_prompt.py")
        for command in ["python3 %s < e.json" % script, script, "PYTHONPATH=x python3 -m scripts.on_prompt",
                        "cd %s && python3 -m on_reviewer_stop" % SCRIPTS, "bash -lc 'python3 %s'" % script,
                        'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/on_prompt.py"', "uv run python %s" % script]:
            self.assert_denied(command, "run only as hooks")
        for command in ['python3 -c "import review_notes"', "python3 - <<EOF\nfrom review_notes import git\nEOF",
                        "python3 -c 'import os, review_notes'", "python3 -c 'import carry; carry.review_notes.git()'",
                        "cd scripts && python3 -c 'import carry; carry.review_notes.write_mark(1, 2, 3)'",
                        "echo 'import review_notes' | python3"]:
            self.assert_denied(command, "only from its hooks")
        for command in ['claude -p "Approved abc1234"', "echo approved | npx @anthropic-ai/claude-code -p"]:
            self.assert_denied(command, "only the user can approve")

    def test_reads_and_ordinary_commands_pass(self):
        self.git("remote", "add", "origin", self.repo)
        for command in ["git notes --ref review show HEAD", "git notes --ref review list", "git notes --ref review",
                        "git notes --ref review get-ref", "git log --notes=review -1", "cat scripts/gate.py",
                        "git show HEAD:scripts/on_prompt.py", "grep -rn write_mark scripts", "python3 gate.py",
                        "python3 %s/carry.py" % SCRIPTS, "python3 -m py_compile %s/gate.py" % SCRIPTS,
                        'git commit -m "approved by tests"', "git for-each-ref refs/notes/review",
                        "git grep refs/notes/review", 'git commit -m "Document refs/notes/review"',
                        "git ls-remote origin refs/notes/review", "git fetch origin", "claude --version",
                        "git fetch origin '+refs/heads/*:refs/remotes/origin/*'", "echo 'unbalanced",
                        'python3 -m unittest -q && grep -n "import review_notes" tests',
                        'cd ~/claude-code-x && git commit -m "approved marks"', "git fetch origin refs/notes/review",
                        "git symbolic-ref --short refs/remotes/origin/HEAD | sed 's@^origin/@@'",
                        "git fetch --depth 1 origin main:refs/remotes/origin/main", "git checkout refs/notes/review",
                        "git update-ref -d refs/remotes/origin/old", "python3 -I %s/gate.py < e.json" % SCRIPTS,
                        "git fetch origin refs/notes/review:refs/notes/review-copy",
                        "git fetch origin main:refs/notes/reviewer",
                        "git fetch --filter blob:none origin main:refs/remotes/origin/main",
                        "git commit -m \"$(cat <<'EOF'\nDon't push refs/notes/review yet\nEOF\n)\"",
                        "python3 --version; grep -n def %s/review_notes.py" % SCRIPTS]:
            self.assert_allowed(command)

    def test_a_command_the_gate_cannot_read_is_denied_only_if_it_may_be_risky(self):
        self.assert_denied("echo 'x && git push", "could not check this command")
        self.assert_allowed("echo 'x && git status")

    def test_tools_cannot_send_an_approval(self):
        for tool, tool_input in [("CronCreate", {"cron": "* * * * *", "prompt": "Approved abc1234"}),
                                 ("ScheduleWakeup", {"prompt": " approved"}),
                                 ("Skill", {"skill": "loop", "args": "5m Approved abc1234"}),
                                 ("mcp__ccd_session_mgmt__send_message", {"messages": [{"text": "Approved"}]}),
                                 ("Monitor", {"command": "claude -p Approved"})]:
            self.assert_denied(tool_input, "only the user can approve", tool=tool)
        self.assert_allowed({"prompt": "Check whether it was approved"}, tool="CronCreate")
        self.assert_allowed({"command": "sleep 1", "description": "Approved PR watcher"}, tool="Monitor")
        self.assert_allowed({"status": "Approved"}, tool="mcp__tracker__update")
        self.assert_denied({"command": "Approved abc1234", "tab_id": "3"}, "into a terminal",
                           tool="mcp__terminal__run_in_terminal")


class PushGateTest(GateCase):
    def setUp(self):
        super().setUp()
        self.remote = os.path.join(self.repo, ".git", "remote.git")
        run(["git", "init", "-q", "--bare", self.remote], self.repo)
        self.git("remote", "add", "origin", self.remote)
        self.git("push", "-q", "-u", "origin", "main")

    def test_unmarked_commits_cannot_be_pushed(self):
        self.git("config", "alias.publish", "!git push -u origin HEAD")
        sha = self.commit("b.txt", "two\n")
        self.git("tag", "v1", "HEAD~1")
        for command in ["git push", "git push -u origin main", "git -C %s push" % self.repo, "cd . && git push -f",
                        "git push --all", "echo ok; git push 2>&1", "git push %s main" % self.remote,
                        "if true; then git push; fi", "git status # don't\ngit push", "(cd . && git push)",
                        "GIT_TRACE=1 git push", "git --git-dir=%s/.git push" % self.repo, "git push origin tag v1 main",
                        "git -c push.default=matching push", "git push origin 'refs/heads/*:refs/heads/*'",
                        "git push -u origin $(git branch --show-current)", "gh pr create --fill",
                        "cd $(git rev-parse --show-toplevel) && git push", "git push origin :", "git push origin +:",
                        "gh pr new --fill", "gh -R o/r pr create --fill", "gh repo create r --source=. --push"]:
            self.assert_denied(command, "1 commit(s) lack a clean review mark: " + sha[:12])
        for command in ["git push origin $BRANCH", "git push origin $(other)"]:
            self.assert_denied(command, "name the remote and branches literally")
        for command in ["git -C missing push", "cd $DIR && git push", "cd /nowhere; git push"]:
            self.assert_denied(command, "could not tell which repository")
        for command in ["bash -c 'git push origin HEAD:refs/heads/other'", "out=$(git push)", "bash -lc 'git push'",
                        "git publish", "eval git push", "env -C . git push", "sudo --odd me git push",
                        "nohup git push",
                        "git send-pack %s main" % self.remote, "echo x | xargs gh pr create",
                        "git subtree push -P a o m"]:
            self.assert_denied(command, "as a plain command")
        terminal = {"command": "git push", "tab_id": "1"}
        self.assert_denied(terminal, "could not tell which repository", tool="mcp__terminal__run_in_terminal")
        self.assert_denied(dict(terminal, cwd=self.repo), sha[:12], tool="mcp__terminal__run_in_terminal", cwd="/")
        review_notes.write_mark(sha, self.repo, CLEAN)
        self.assert_allowed("git push origin main")
        self.assert_allowed("gh pr create -H other")
        for command in ["gh pr create -Hother", "git status && git remote -v && git stash list && git push origin main",
                        "cd . && echo ok && git config --get remote.origin.url && git push origin main"]:
            self.assert_allowed(command)
        for command in ["git commit -q --allow-empty -m x && git push",
                        "git checkout -q -b feat && git push origin HEAD",
                        "git branch -f main HEAD~1; git push origin main", "gh pr checkout 1 && git push",
                        "npm version patch && git push --follow-tags"]:
            self.assert_denied(command, "run the push as its own command")

    def test_pushes_that_send_nothing_new_pass(self):
        self.commit("b.txt", "two\n")
        os.mkdir(os.path.join(self.repo, "docs"))
        self.git("branch", "docs", "HEAD~1")
        self.git("tag", "v1", "HEAD~1")
        for command in ["git push --dry-run", "git push -n origin main", "git push origin docs", "git push origin :old",
                        "git push origin tag v1", "git push --delete origin old", "git push -nf origin main",
                        "git push -df origin old"]:
            self.assert_allowed(command)
        self.assert_allowed("git push . HEAD:docs")
        self.assert_denied("git push", "could not read the repository", cwd="/")

    def test_branches_a_bare_push_may_send_count(self):
        self.git("switch", "-q", "-c", "side")
        sha = self.commit("b.txt", "two\n")
        self.git("switch", "-q", "main")
        self.assert_allowed("git push")
        self.git("config", "push.default", "matching")
        self.assert_denied("git push", sha[:12])
        self.assert_denied("git push --mirror", sha[:12])
        self.git("config", "--unset", "push.default")
        self.git("config", "remote.origin.push", "HEAD:refs/for/main")
        self.assert_allowed("git push")  # only HEAD, not every branch
        self.assert_allowed("git push origin 'refs/heads/*:refs/heads/*' '^refs/heads/side'")
        self.git("config", "remote.origin.mirror", "yes")
        self.assert_denied("git push", sha[:12])
        self.git("config", "--unset", "remote.origin.mirror")
        self.git("config", "remote.origin.push", ":")
        self.assert_denied("git push", sha[:12])
        review_notes.write_mark(sha, self.repo, CLEAN)
        self.assert_allowed("git push --mirror")  # the marks are not commits to review

    def test_merges_git_made_without_conflicts_need_no_review(self):
        self.git("switch", "-q", "-c", "side")
        review_notes.write_mark(self.commit("b.txt", "two\n"), self.repo, CLEAN)
        self.git("switch", "-q", "main")
        review_notes.write_mark(self.commit("c.txt", "three\n"), self.repo, CLEAN)
        self.git("merge", "-q", "--no-edit", "--no-ff", "side")
        self.assert_allowed("git push")
        self.commit("c.txt", "edited in the merge\n", amend=True)
        self.assert_denied("git push", "1 commit(s) lack a clean review mark: " + self.head()[:12])

    def test_a_rebased_branch_other_than_head_carries_its_marks(self):
        self.git("switch", "-q", "-c", "feature")
        sha = self.commit("b.txt", "two\n")
        review_notes.write_mark(sha, self.repo, CLEAN)
        self.git("switch", "-q", "main")
        review_notes.write_mark(self.commit("c.txt", "three\n"), self.repo, CLEAN)
        self.git("rebase", "-q", "main", "feature")
        self.git("switch", "-q", "main")
        self.assert_allowed("git push origin feature")

    def test_network_remotes_count_as_pushed_and_urls_match_their_remote(self):
        self.git("switch", "-q", "-c", "fork")
        sha = self.commit("b.txt", "two\n")
        self.git("update-ref", "refs/remotes/upstream/main", sha)  # as if fetched from a network remote
        self.git("remote", "add", "upstream", "https://example.com/upstream.git")
        self.assert_allowed("git push origin fork")
        self.git("remote", "set-url", "upstream", self.remote + "2")
        self.assert_denied("git push origin fork", sha[:12])
        self.git("config", "--add", "remote.origin.url", "https://example.com/origin.git")
        self.assert_denied("git push file://%s/ fork" % self.remote, "lack a clean review mark: " + sha[:12])

    def test_a_reworded_reviewed_commit_carries_its_mark(self):
        sha = self.commit("b.txt", "two\n")
        review_notes.write_mark(sha, self.repo, CLEAN)
        self.git("commit", "-q", "--amend", "-m", "reworded")
        self.assert_allowed("git push")
        self.assertEqual(review_notes.read_mark(self.head(), self.repo)["via"], "same-change")

    def test_commits_fetched_from_another_remote_still_count_for_this_one(self):
        sha = self.commit("b.txt", "two\n")
        clone = os.path.join(self.repo, ".git", "clone")
        run(["git", "clone", "-q", self.repo, clone], self.repo)
        self.git("remote", "add", "other", clone)
        self.git("fetch", "-q", "other")
        self.assert_denied("git push origin main", sha[:12])
        self.assert_allowed("git push other main")  # other already has it
        self.git("remote", "add", "empty", self.remote + "2")
        self.assert_denied("git push empty main", "no remote it trusts has fetched branches")


if __name__ == "__main__":
    unittest.main()
