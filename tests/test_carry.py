import os
import subprocess
import sys
import unittest

from helpers import ENV, SCRIPTS, RepoCase

import review_notes

CLEAN = {"verdict": "clean", "kind": "agent"}


class CarryTest(RepoCase):
    def carry(self, *args):
        result = subprocess.run([sys.executable, os.path.join(SCRIPTS, "carry.py"), *args], cwd=self.repo,
                                capture_output=True, text=True, env=ENV)
        return result.returncode, result.stdout

    def mark(self, sha):
        return review_notes.read_mark(sha, self.repo)

    def approve(self, sha=None):
        review_notes.write_mark(sha or self.head(), self.repo, CLEAN)

    def test_unchanged_patch_carries_after_an_earlier_commit_is_amended(self):
        first = self.head()
        second = self.commit("b.txt", "two\n")
        self.approve(second)
        self.git("switch", "-q", "--detach", first)
        self.commit("a.txt", "one fixed\n", amend=True)
        self.git("cherry-pick", second)
        rebased = self.head()
        carried = "Carried the mark of %s to %s (same change).\n" % (second[:12], rebased[:12])
        self.assertEqual(self.carry(), (0, carried))
        self.assertEqual((self.mark(rebased)["via"], self.mark(rebased)["from"]), ("same-change", second))
        self.assertEqual(self.carry(), (0, "No marks to carry.\n"))

    def test_a_full_review_is_preferred_and_binary_changes_never_carry(self):
        first = self.head()
        second = self.commit("b.txt", "two\n")
        self.approve(second)
        self.git("switch", "-q", "--detach", first)
        self.commit("a.txt", "one fixed\n", amend=True)
        self.git("cherry-pick", second)
        nits = self.head()
        review_notes.write_mark(nits, self.repo, dict(CLEAN, kind="carried", via="nits"))
        self.git("commit", "-q", "--amend", "-m", "reworded")
        self.carry()
        self.assertEqual((self.mark(self.head())["via"], self.mark(self.head())["from"]), ("same-change", second))
        self.git("switch", "-q", "--detach", first)
        with open(os.path.join(self.repo, "bin.dat"), "wb") as f:
            f.write(b"\0\1")
        self.git("add", "bin.dat")
        self.git("commit", "-q", "-m", "binary")
        self.approve()
        self.git("commit", "-q", "--amend", "-m", "binary reworded")
        self.assertEqual(self.carry(), (0, "No marks to carry.\n"))

    def test_whitespace_or_mode_differences_are_not_the_same_change(self):
        first = self.head()
        second = self.commit("b.txt", "if x:\n    y()\nz()\n")
        self.approve(second)
        self.git("switch", "-q", "--detach", first)
        self.commit("b.txt", "if x:\n    y()\n    z()\n")
        self.assertEqual(self.carry(), (0, "No marks to carry.\n"))
        self.git("reset", "-q", "--hard", second)
        self.git("update-index", "--chmod=+x", "b.txt")
        self.git("commit", "-q", "--amend", "--no-edit")
        self.assertEqual(self.carry(), (0, "No marks to carry.\n"))

    def test_nit_fix_carries_within_the_limit(self):
        reviewed = self.commit("b.txt", "two\n")
        self.approve()
        fixed = self.commit("b.txt", "two\n" + "nit\n" * 9, amend=True)
        self.assertEqual(self.carry(reviewed)[0], 0)
        self.assertEqual((self.mark(fixed)["via"], self.mark(fixed)["lines"]), ("nits", 9))

    def test_nit_carry_refusals(self):
        reviewed = self.commit("b.txt", "two\n")
        self.approve()
        big = self.commit("b.txt", "two\n" + "x\n" * 11, amend=True)
        self.assertIn("changes 11 lines", self.carry(reviewed)[1])
        self.assertIsNone(self.mark(big))
        self.assertIn("no clean review mark", self.carry("HEAD~1", "HEAD")[1])
        self.git("update-ref", "refs/remotes/origin/main", big)
        self.assertIn("on a remote", self.carry(reviewed)[1])
        base = self.git("rev-parse", "HEAD~1")
        self.approve(base)
        self.assertIn("still on this branch", self.carry(base)[1])

    def test_nit_carry_only_edits_files_the_reviewed_commit_changed(self):
        reviewed = self.commit("b.txt", "two\n")
        self.approve()
        self.commit("a.txt", "one sneaked\n", amend=True)
        self.assertIn("edits a.txt, which %s did not change" % reviewed[:12], self.carry(reviewed)[1])
        self.assertIsNone(self.mark(self.head()))

    def test_nit_carry_refuses_anything_but_small_edits_of_text_files(self):
        reviewed = self.commit("b.txt", "two\n")
        self.approve()
        changes = [["mv", "b.txt", "c.txt"], ["update-index", "--chmod=+x", "b.txt"],
                   ["update-index", "--add", "--cacheinfo", "160000,%s,sub" % reviewed],
                   ["update-index", "--add", "--cacheinfo", "120000,%s,b.txt" % self.git("hash-object", "-w", "b.txt")]]
        for change in changes:
            self.git("reset", "-q", "--hard", reviewed)
            self.git(*change)
            self.git("commit", "-q", "--amend", "--no-edit")
            self.assertIn("adds, deletes or renames", self.carry(reviewed)[1], change)
        self.git("reset", "-q", "--hard", reviewed)
        self.commit("empty.txt", "", amend=True)
        self.assertIn("adds, deletes or renames", self.carry(reviewed)[1])
        self.git("reset", "-q", "--hard", reviewed)
        with open(os.path.join(self.repo, "b.txt"), "wb") as f:
            f.write(b"two\0\n")
        self.git("commit", "-q", "--amend", "-a", "--no-edit")
        self.assertIn("binary file", self.carry(reviewed)[1])
        self.assertIsNone(self.mark(self.head()))

    def test_a_replace_ref_does_not_shrink_the_change(self):
        reviewed = self.commit("b.txt", "two\n")
        self.approve()
        big = self.commit("b.txt", "two\n" + "x\n" * 30, amend=True)
        self.git("replace", big, reviewed)
        self.assertIn("changes 30 lines", self.carry(reviewed)[1])
        self.assertIsNone(self.mark(big))

    def test_marked_targets_merges_and_unborn_heads_are_refused(self):
        reviewed = self.commit("b.txt", "two\n")
        self.approve()
        fixed = self.commit("b.txt", "two fixed\n", amend=True)
        self.approve(fixed)
        self.assertIn("already has a clean mark", self.carry(reviewed)[1])
        self.git("switch", "-q", "-c", "side", "HEAD~1")
        self.commit("c.txt", "side\n")
        self.git("switch", "-q", "-")
        self.git("merge", "-q", "--no-edit", "side")
        self.assertIn("different bases (or one is a merge)", self.carry(reviewed)[1])
        self.git("switch", "-q", "--orphan", "fresh")
        self.assertEqual(self.carry(reviewed), (1, "Unknown commit: HEAD\n"))

    def test_nit_carry_needs_the_same_base(self):
        reviewed = self.commit("b.txt", "two\n")
        self.approve()
        self.git("reset", "-q", "--hard", "HEAD~1")
        self.commit("a.txt", "one changed\n", amend=True)
        self.commit("b.txt", "two\n")
        self.assertIn("different bases", self.carry(reviewed)[1])

    def test_nit_carries_do_not_chain(self):
        reviewed = self.commit("b.txt", "two\n")
        self.approve()
        once = self.commit("b.txt", "two\n" + "x\n" * 6, amend=True)
        self.carry(reviewed)
        self.commit("b.txt", "two\n" + "x\n" * 12, amend=True)
        self.assertIn("itself carried for nit fixes", self.carry(once)[1])
        self.git("reset", "-q", "--hard", once)
        self.git("commit", "-q", "--amend", "-m", "reworded")
        self.carry()
        self.assertEqual(self.mark(self.head())["via"], "nits")
        self.git("commit", "-q", "--amend", "-m", "reworded again")
        self.assertIn("itself carried", self.carry("HEAD@{1}")[1])


if __name__ == "__main__":
    unittest.main()
