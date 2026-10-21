import json
import os
import unittest

from helpers import RepoCase, run_hook

import review_notes


class ApprovedPromptTest(RepoCase):
    def prompt(self, text, shown="", env=None, after=(), logged=True, **extra):
        transcript = os.path.join(self.repo, ".git", "session.jsonl")
        entries = [("assistant", [{"type": "text", "text": shown}]), ("assistant", [{"type": "tool_use", "id": "t"}]),
                   *after, *([("user", text)] if logged else [])]
        with open(transcript, "w") as f:
            for entry in entries:  # a str is a raw line
                f.write((entry if isinstance(entry, str) else
                         json.dumps({"type": entry[0], "message": {"content": entry[1]}})) + "\n")
        event = dict({"hook_event_name": "UserPromptSubmit", "cwd": self.repo, "prompt": text,
                      "transcript_path": transcript}, **extra)
        result = run_hook("on_prompt.py", event, env=env)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return json.loads(result.stdout)["systemMessage"] if result.stdout else ""

    def mark(self, sha):
        return review_notes.read_mark(sha, self.repo)

    def test_bare_approved_marks_the_single_pending_commit_the_agent_asked_about(self):
        pushed = self.head()
        self.git("update-ref", "refs/remotes/origin/main", pushed)
        reviewed = self.commit("b.txt", "two\n")
        review_notes.write_mark(reviewed, self.repo, {"verdict": "clean", "kind": "agent"})
        tiny = self.commit("c.txt", "three\n")
        ask = "Commit %s is tiny.\nReply 'Approved' to mark %s as manually reviewed." % (tiny, tiny[:7])
        for shown, after in (("Here is the plan for %s." % tiny, ()),
                             (ask, [("user", "Wait, what about tests?"), ("assistant", [{"type": "tool_use"}])])):
            self.assertIn("did not ask", self.prompt("Approved", shown=shown, after=after))
        self.assertIsNone(self.mark(tiny))
        self.assertIn("did not ask", self.prompt("Approved", shown=ask, after=[("user", "Approved"), ("user", "Hm")]))
        tool_only = ("assistant", [{"type": "tool_use"}])
        for after in ([("user", "Approved"), tool_only], [("user", "Approved? Not yet."), tool_only]):
            self.assertIn("did not ask", self.prompt("Approved", shown=ask, after=after, logged=False))
        blank = ["[1]", "null", ("assistant", [{"type": "text", "text": " "}])]
        self.assertIn("marked as manually", self.prompt("Approved", shown=ask, after=blank, logged=False))
        self.git("notes", "--ref", review_notes.NOTES_REF, "remove", tiny)
        local = [("user", "<bash-input>git show %s</bash-input>" % tiny), ("user", "<bash-stdout>diff</bash-stdout>"),
                 ("user", "<command-name>/cost</command-name>"),
                 ("user", "<task-notification>done</task-notification>"),
                 ("user", [{"type": "text", "text": "<ide_selection>x</ide_selection>"},
                           {"type": "text", "text": "Approved."}])]
        message = self.prompt("Approved.", shown=ask, after=local, logged=False)
        self.assertIn("%s (main): marked as manually reviewed: %s change c.txt" % (self.repo, tiny[:7]), message)
        self.assertEqual((self.mark(tiny)["kind"], self.mark(reviewed)["kind"]), ("manual", "agent"))
        self.assertIsNone(self.mark(pushed))

    def test_unclear_approvals_mark_nothing_and_list_pending_commits(self):
        first = self.head()
        second = self.commit("b.txt", "two\n")
        shown = "Commits %s and %s" % (first, second)
        for text in ("Approved", "Approved the first one", "Approved?", "Approved, except " + first[:7],
                     "Approved %s-ish" % first[:7]):
            message = self.prompt(text, shown=shown)
            self.assertIn("no commits marked", message, text)
            self.assertIn("pending: %s change b.txt; %s" % (second[:7], first[:7]), message)
        self.assertIsNone(self.mark(first))

    def test_named_shas_mark_only_those(self):
        first = self.head()
        second = self.commit("b.txt", "two\n")
        self.assertIn("reply 'Approved <sha> ...'", self.prompt("Approved %s thanks" % first[:9]))
        self.assertIsNone(self.mark(first))
        message = self.prompt("Approved %s, %s abcdef0." % (first[:9].upper(), first[:9]))
        self.assertTrue(review_notes.is_clean(self.mark(first)))
        self.assertIsNone(self.mark(second))
        self.assertIn("abcdef0 matches no pending commit", message)
        self.assertIn("pending: %s change b.txt." % second[:7], message)
        self.assertEqual(message.count(first[:7]), 1)

    def test_full_sha256_names_one_commit(self):
        self.tearDown()
        self.setUp(object_format="sha256")
        first = self.head()
        second = self.commit("b.txt", "two\n")
        self.prompt("Approved " + first)
        self.assertTrue(review_notes.is_clean(self.mark(first)))
        self.assertIsNone(self.mark(second))

    def test_the_project_repo_wins_over_the_agents_directory(self):
        sha = self.head()
        self.assertEqual(self.prompt("Approved " + sha, cwd="/", env={"CLAUDE_PROJECT_DIR": self.repo})[:3], "In ")
        self.assertTrue(review_notes.is_clean(self.mark(sha)))

    def test_other_prompts_and_odd_events_do_nothing(self):
        sha = self.head()
        for text in ("Not approved", "I approved it", "Approvedness", "", "Push it"):
            self.assertEqual(self.prompt(text, shown=sha), "", text)
        self.assertEqual(self.prompt("Approved", shown=sha, agent_id="a1", agent_type="x"), "")
        self.assertIn("no commits marked", self.prompt("Approved", shown=sha, agent_type="main-agent"))
        for cwd in (5, "/no/such/dir", "/"):
            self.assertIn("not a git repository", self.prompt("Approved", shown=sha, cwd=cwd), cwd)
        self.assertIn("the project directory is not", self.prompt("Approved", cwd=None))
        self.assertIsNone(self.mark(sha))


if __name__ == "__main__":
    unittest.main()
