import os
import subprocess
import sys

from helpers import SCRIPTS, RepoCase

import review_context


class ReviewContextTest(RepoCase):
    def setUp(self):
        super().setUp()
        os.mkdir(os.path.join(self.repo, "tests"))

    def report(self, *args):
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, "review_context.py"), *args], cwd=self.repo,
                              capture_output=True, text=True).stdout

    def test_touched_functions_their_uses_and_hazards_are_listed(self):
        self.commit("lib.py", "import sys\n\n\ndef load(path):\n    return path\n\n\ndef other():\n    return 1\n")
        self.commit("app.py", "from lib import load\n\nload('a')\nload('b')\n")
        self.commit("tests/test_lib.py", "def test_load():\n    print(load('x'))\n")
        self.commit("lib.py", "import sys\n\n\ndef load(path):\n    print(path)\n    sys.exit(requests.get(path))\n"
                              "\n\ndef other():\n    return 1\n")
        report = self.report("HEAD", "--calls", "1")
        self.assertIn("Changed files: lib.py", report)
        self.assertIn("- load: app.py:1 (+3 more)", report)  # app.py twice more, and the test
        self.assertNotIn("other", report)
        for line in ["- output: lib.py:5", "- exit: lib.py:6", "- network: lib.py:6"]:
            self.assertIn(line, report)
        self.assertNotIn("process", report)

    def test_test_files_add_no_names_or_hazards_and_a_root_commit_works(self):
        sha = self.commit("tests/test_x.py", "def test_x():\n    print(1)\n")
        report = self.report(sha)
        self.assertIn("Touched functions and where they are used (whole-word matches, approximate):\n- none found",
                      report)
        self.assertTrue(report.rstrip().endswith("- none found"))
        root = self.git("rev-list", "--max-parents=0", "HEAD")
        self.assertIn("Changed files: a.txt", self.report(root))

    def test_unknown_commits_and_definition_names(self):
        self.assertIn("Unknown commit: nope", self.report("nope"))
        for text, name in [("def load(x):", "load"), ("func (s *S) Run() {", "Run"), ("fn parse(", "parse"),
                           ("export function render(props) {", "render"), ("class Gate:", "Gate"),
                           ("func Run() {", "Run")]:
            match = review_context.DEF_RE.search(text)
            self.assertEqual(match.group(1) if match else None, name, text)
