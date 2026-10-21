import unittest

from helpers import RepoCase, run_hook

import on_reviewer_stop
import review_notes

REVIEWER = "commit-workflow:commit-reviewer"


class ReviewerStopTest(RepoCase):
    def stop(self, message, transcript, agent_type=REVIEWER, active=False):
        return run_hook("on_reviewer_stop.py", {
            "hook_event_name": "SubagentStop", "cwd": self.repo, "agent_type": agent_type, "agent_id": "a1",
            "agent_transcript_path": transcript, "last_assistant_message": message, "stop_hook_active": active,
        })

    def mark(self, sha=None):
        return review_notes.read_mark(sha or self.head(), self.repo)

    def assert_asked(self, result, text):
        self.assertEqual(result.returncode, 2)
        self.assertIn(text, result.stderr)

    def test_clean_verdict_marks_the_commit(self):
        sha = self.head()
        result = self.stop("No findings.\nVERDICT: clean " + sha, self.transcript("git show " + sha))
        self.assertEqual(result.returncode, 0)
        self.assertIn("Review mark written", result.stdout)
        mark = self.mark()
        self.assertTrue(review_notes.is_clean(mark))
        self.assertEqual((mark["kind"], mark["reviewer"], mark["commit"]), ("agent", REVIEWER, sha))

    def test_short_shas_and_delta_diffs_accepted(self):
        first = self.head()
        second = self.commit("b.txt", "two\n")
        reads = ["git show --stat %s && git -C . show %s" % (second[:7], second[:7]),
                 "git diff -U30 %s %s 2>&1" % (first, second), "git diff %s..%s" % (first[:8], second[:8]),
                 "git show %s > /dev/null; git show %s" % (first, second)]
        for read in reads:
            self.git("notes", "--ref", review_notes.NOTES_REF, "remove", "--ignore-missing", second)
            self.stop("**VERDICT: Clean %s.**" % second[:7], self.transcript(read))
            self.assertTrue(review_notes.is_clean(self.mark()), read)

    def test_blocking_verdict_writes_no_mark(self):
        sha = self.head()
        result = self.stop("[blocking] a.txt:1 - bug - fix\nVERDICT: blocking", self.transcript("git show " + sha))
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertIsNone(self.mark())

    def test_reads_that_show_no_patch_of_the_commit_are_rejected(self):
        old = self.head()
        sha = self.commit("b.txt", "two\n")
        for read in ["git show -s --format=%%B %s" % sha, "git show --stat " + sha, "git show HEAD",
                     "git diff " + sha, "git show %s:b.txt" % sha, "git show %s -- b.txt" % sha,
                     "git show " + old, "git show-ref " + sha, 'grep -rn "git show %s" .' % sha,
                     "git show %s b.txt" % sha, "git show -q %s; git show %s" % (sha, old),
                     "git show --stat=100 %s && git show %s" % (sha, old),
                     ("git show " + sha, "Permission to use Bash was denied.", True)]:
            result = self.stop("VERDICT: clean " + sha, self.transcript(read))
            self.assert_asked(result, "git show " + sha)
            self.assertIsNone(self.mark(), read)

    def test_stale_head_read_from_an_earlier_round_does_not_count(self):
        round_one = self.transcript("git show HEAD")
        amended = self.commit("a.txt", "one fixed\n", amend=True)
        result = self.stop("VERDICT: clean " + amended, round_one, active=True)
        self.assertIn("not written", result.stdout)
        self.assertIsNone(self.mark(amended))

    def test_missing_or_shaless_verdict_asks_once(self):
        transcript = self.transcript("git show " + self.head())
        for message in ("Looks fine.", "VERDICT: clean", "VERDICT: approved " + self.head()):
            self.assert_asked(self.stop(message, transcript), "VERDICT: clean " + self.head())
            second = self.stop(message, transcript, active=True)
            self.assertEqual(second.returncode, 0)
            self.assertIn("not written", second.stdout)
        self.assertIsNone(self.mark())

    def test_commit_off_the_branch_asks_for_the_right_one(self):
        self.git("switch", "-q", "-c", "side")
        side = self.commit("b.txt", "side\n")
        self.git("switch", "-q", "-")
        self.assert_asked(self.stop("VERDICT: clean " + side, self.transcript("git show " + side)), "HEAD is")
        self.assertIsNone(self.mark(side))

    def test_earlier_commit_on_the_branch_can_be_marked(self):
        reviewed = self.head()
        self.commit("b.txt", "two\n")
        self.stop("VERDICT: clean " + reviewed, self.transcript("git show " + reviewed))
        self.assertTrue(review_notes.is_clean(self.mark(reviewed)))

    def test_other_agents_are_ignored(self):
        sha = self.head()
        for agent_type in ("Explore", "commit-reviewer", "other-plugin:commit-reviewer"):
            result = self.stop("VERDICT: clean " + sha, self.transcript("git show " + sha), agent_type=agent_type)
            self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertIsNone(self.mark())

    def test_amend_drops_mark_and_a_later_round_marks_again(self):
        first = self.head()
        self.stop("VERDICT: clean " + first, self.transcript("git show " + first))
        amended = self.commit("a.txt", "one fixed\n", amend=True)
        self.assertIsNone(self.mark(amended))
        self.stop("Fix verified.\nVERDICT: clean " + amended, self.transcript("git diff -U30 %s %s" % (first, amended)))
        self.assertTrue(review_notes.is_clean(self.mark(amended)))

    def test_outside_a_repo_does_nothing(self):
        result = run_hook("on_reviewer_stop.py", {"cwd": "/", "agent_type": REVIEWER,
                                                  "last_assistant_message": "VERDICT: clean abcdef1"})
        self.assertEqual((result.returncode, result.stdout), (0, ""))


class ParseVerdictTest(unittest.TestCase):
    def test_last_verdict_line_wins(self):
        text = "Format: VERDICT: clean <sha>\nVERDICT: blocking\n`VERDICT: CLEAN ABC1234`\n"
        self.assertEqual(on_reviewer_stop.parse_verdict(text), ("clean", "abc1234"))

    def test_verdict_must_be_its_own_line(self):
        self.assertIsNone(on_reviewer_stop.parse_verdict("The VERDICT: clean abc1234 line comes later"))
        self.assertIsNone(on_reviewer_stop.parse_verdict(None))


if __name__ == "__main__":
    unittest.main()
