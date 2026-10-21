import os
import unittest

from helpers import RepoCase

import review_notes


class ReviewNotesTest(RepoCase):
    def test_mark_round_trip_names_its_commit(self):
        sha = self.head()
        self.assertIsNone(review_notes.read_mark(sha, self.repo))
        review_notes.write_mark(sha, self.repo, {"verdict": "clean", "kind": "agent"})
        mark = review_notes.read_mark(sha, self.repo)
        self.assertEqual((mark["verdict"], mark["commit"]), ("clean", sha))
        self.assertTrue(review_notes.is_clean(mark))
        self.assertFalse(review_notes.is_clean({"verdict": "blocking"}))
        self.assertFalse(review_notes.is_clean(None))

    def test_note_copied_to_a_rewritten_commit_is_ignored(self):
        self.git("config", "notes.rewriteRef", review_notes.NOTES_REF)
        review_notes.write_mark(self.head(), self.repo, {"verdict": "clean"})
        amended = self.commit("a.txt", "changed\n", amend=True)
        self.assertIn('"commit"', self.git("notes", "--ref", review_notes.NOTES_REF, "show", amended))
        self.assertIsNone(review_notes.read_mark(amended, self.repo))

    def test_no_mark_in_a_shallow_or_grafted_repo(self):
        sha = self.head()
        for name in ("shallow", "info/grafts"):
            path = os.path.join(self.repo, ".git", name)
            with open(path, "w") as f:
                f.write(sha + "\n")
            with self.assertRaisesRegex(RuntimeError, "shallow or has grafts"):
                review_notes.write_mark(sha, self.repo, {"verdict": "clean"})
            os.remove(path)
        self.assertIsNone(review_notes.read_mark(sha, self.repo))

    def test_clean_marks_reads_every_clean_mark(self):
        first = self.head()
        second = self.commit("b.txt", "two\n")
        third = self.commit("c.txt", "three\n")
        self.git("notes", "--ref", review_notes.NOTES_REF, "add", "-m",
                 '{"commit": "%s", "verdict": "clean", "by": "Ren\u00e9e"}' % first, first)
        review_notes.write_mark(second, self.repo, {"verdict": "blocking"})
        review_notes.write_mark(third, self.repo, {"verdict": "clean"})
        self.assertEqual(sorted(review_notes.clean_marks(self.repo)), sorted([first, third]))

    def test_resolve_and_is_ancestor(self):
        first = self.head()
        second = self.commit("b.txt", "two\n")
        self.assertEqual(review_notes.resolve(first[:7], self.repo), first)
        self.assertIsNone(review_notes.resolve("0" * 40, self.repo))
        self.assertIsNone(review_notes.resolve("--all", self.repo))
        self.assertTrue(review_notes.is_ancestor(first, second, self.repo))
        self.assertTrue(review_notes.is_ancestor(second, second, self.repo))
        self.assertFalse(review_notes.is_ancestor(second, first, self.repo))


if __name__ == "__main__":
    unittest.main()
