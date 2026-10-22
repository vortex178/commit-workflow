import os
import unittest

from helpers import run, run_hook
from test_gate import CLEAN, GateCase
from test_marks import REVIEWER, show

import review_notes
import show_change


class BranchGateTest(GateCase):
    def setUp(self):
        super().setUp()
        remote = os.path.join(self.repo, ".git", "remote.git")
        run(["git", "init", "-q", "--bare", remote], self.repo)
        self.git("remote", "add", "origin", remote)
        self.git("push", "-q", "-u", "origin", "main")
        self.git("remote", "set-head", "origin", "main")
        self.base = self.head()
        self.git("checkout", "-q", "-b", "feat")
        for name in ("b.txt", "c.txt"):
            review_notes.write_mark(self.commit(name, name + "\n"), self.repo, CLEAN)
        self.git("push", "-q", "-u", "origin", "feat")

    def review_branch(self, base, message=None, read=None):
        tip = self.head()
        return run_hook("on_reviewer_stop.py", {
            "hook_event_name": "SubagentStop", "cwd": self.repo, "agent_type": REVIEWER, "agent_id": "a1",
            "agent_transcript_path": self.transcript(read or show(tip, "--base", base)),
            "last_assistant_message": message or "VERDICT: branch-clean " + tip})

    def test_the_branch_view_shows_the_whole_branch(self):
        tip = self.head()
        shown = show_change.parts(tip, self.base, 3, self.repo, branch=True)
        self.assertTrue(shown[0].startswith("== commit-review %s...%s -U3 part 1/1" % (self.base, tip)))
        self.assertIn("+b.txt", shown[0])
        self.assertIn("+c.txt", shown[0])
        self.assertIn("not an earlier commit", show_change.parts(self.base, tip, 3, self.repo, branch=True))
        self.assertIn("not an earlier commit", show_change.parts(tip, tip, 3, self.repo, branch=True))

    def test_a_branch_clean_verdict_records_the_base_and_keeps_the_commit_mark(self):
        tip = self.head()
        result = self.review_branch(self.base)
        self.assertIn("Branch review mark written", result.stdout)
        self.assertEqual(review_notes.branch_base(tip, self.repo), self.base)
        self.assertTrue(review_notes.is_clean(review_notes.read_mark(tip, self.repo)))
        review_notes.write_mark(tip, self.repo, CLEAN)  # a later commit mark keeps the branch mark
        self.assertEqual(review_notes.branch_base(tip, self.repo), self.base)
        unread = self.review_branch(self.base, read=show(tip))
        self.assertEqual(unread.returncode, 2)
        self.assertIn("--base <base>", unread.stderr)
        self.assertIn("VERDICT: branch-clean " + tip, self.review_branch(self.base, "VERDICT: branch-clean").stderr)
        parent = self.git("rev-parse", "HEAD~1")
        self.assertIn("marks the branch tip", self.review_branch(self.base, "VERDICT: branch-clean " + parent).stderr)

    def test_pull_requests_need_a_branch_review_from_their_base_branch(self):
        for command in ["gh pr create --fill", "gh pr merge --squash -t done", "gh pr create -H feat -B main"]:
            self.assert_denied(command, "lacks a whole-branch review from origin/main")
        self.assert_denied("gh pr create --fill", "--base %s" % self.base[:12])
        self.review_branch(self.head() + "~1")  # a base on the branch covers only part of it
        self.assert_denied("gh pr create --fill", "lacks a whole-branch review")
        self.review_branch(self.base)
        for command in ["gh pr create --fill", "gh pr create -Hfeat --base=main", "gh pr merge --squash -t done",
                        "cd . && gh pr merge -d"]:
            self.assert_allowed(command)
        self.assert_denied("git status && gh pr merge -d", "run it as its own command")
        self.assert_denied("GIT_DIR=.git gh pr merge -d", "without VAR= settings")
        self.assert_allowed("GH_PAGER=cat NO_COLOR=1 gh pr merge -d")
        unrelated = self.git("commit-tree", "HEAD^{tree}", "-m", "unrelated")
        self.git("push", "-q", "origin", unrelated + ":refs/heads/other")
        self.assert_denied("gh pr create -B other", "shares no history with origin/other")
        self.assert_denied("gh pr create -B missing", "could not tell the pull request's base branch")
        self.assert_denied("gh pr create -t -Bmain -B other", "with origin/other")  # -Bmain is the title
        self.git("config", "branch.feat.gh-merge-base", "other")
        self.assert_denied("gh pr create --fill", "with origin/other")
        self.git("config", "--unset", "branch.feat.gh-merge-base")
        self.git("remote", "add", "upstream", self.repo)
        self.assert_denied("gh pr merge", "run `gh repo set-default`")
        self.git("config", "remote.upstream.gh-resolved", "o/r")  # a mapping, not the default
        self.assert_denied("gh pr merge", "run `gh repo set-default`")
        self.git("config", "remote.origin.gh-resolved", "base")
        self.assert_allowed("gh pr merge")

    def test_only_a_plain_pull_request_from_its_branch_is_read(self):
        self.review_branch(self.base)
        for command, text in [("gh pr merge 12", "without a pull request argument"),
                              ("gh pr merge https://github.com/o/r/pull/12", "without a pull request argument"),
                              ("gh pr create -H other", "without --head naming another branch"),
                              ("gh pr create -H me:feat", "or a fork"),
                              ("gh pr create -R o/r", "without -R"), ("gh pr merge --repo=o/r", "without -R"),
                              ("gh -R o/r pr merge", "without -R"), ("gh pr merge 'x", "could not check"),
                              ("gh pr merge -m 12", "without a pull request argument"),
                              ("gh pr merge -r --squash 12", "without a pull request argument"),
                              ("gh pr create -B main -B other", "at most once"),
                              ("gh pr create -dHother", "at most once"),
                              ("GH_REPO=o/r gh pr merge", "GH_REPO"), ("export GH_HOST=x; gh pr merge", "GH_REPO"),
                              ("bash -c 'gh pr merge'", "as a plain command"), ("sudo gh pr merge", "plain command"),
                              ("git commit -q --allow-empty -m x && gh pr merge", "run it as its own command"),
                              ("gh pr create -B $base", "give values literally")]:
            self.assert_denied(command, text)
        review_notes.write_mark(self.commit("d.txt", "d\n"), self.repo, CLEAN)
        self.assert_denied("gh pr merge", "HEAD is not what its upstream branch holds")
        self.assert_denied("gh pr create", "HEAD is not what its upstream branch holds")


if __name__ == "__main__":
    unittest.main()
