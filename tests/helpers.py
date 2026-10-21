"""Throwaway git repos and hook runners shared by the tests."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

# Keep the caller's git environment (GIT_DIR and friends) and Claude Code project out of the test repos, in this
# process and in children.
for _key in [key for key in os.environ if key.startswith("GIT_") or key == "CLAUDE_PROJECT_DIR"]:
    del os.environ[_key]
ENV = dict(os.environ)


def run(args, cwd, shell=False):
    return subprocess.run(args, cwd=cwd, check=not shell, capture_output=True, text=True, env=ENV,
                          shell=shell).stdout.strip()


def run_hook(script, event, cwd=None, env=None):
    """Run a hook script with event as JSON on stdin and env added to ENV; returns the CompletedProcess."""
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, script)], input=json.dumps(event),
                          capture_output=True, text=True, env=dict(ENV, **(env or {})), cwd=cwd)


class RepoCase(unittest.TestCase):
    def setUp(self, object_format="sha1"):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.realpath(self._tmp.name)
        self.git("init", "-q", "-b", "main", *(["--object-format=" + object_format] if object_format != "sha1" else []))
        for key, value in (("user.email", "test@example.com"), ("user.name", "Test"), ("commit.gpgsign", "false")):
            self.git("config", key, value)
        self.commit("a.txt", "one\n")

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args):
        return run(["git", *args], self.repo)

    def head(self):
        return self.git("rev-parse", "HEAD")

    def commit(self, name, text, amend=False):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)
        self.git("add", name)
        self.git("commit", "-q", *(["--amend", "--no-edit"] if amend else ["-m", "change " + name]))
        return self.head()

    def transcript(self, *calls):
        """A reviewer transcript of Bash calls, kept inside .git.

        Each call is a command, which is run in the repo for its real output, or a (command, output, is_error) tuple.
        """
        path = os.path.join(self.repo, ".git", "agent-transcript.jsonl")
        with open(path, "w") as f:
            for number, call in enumerate(calls):
                command, output, is_error = call if isinstance(call, tuple) else (call, None, False)
                if output is None:
                    output = run(command, self.repo, shell=True)
                use = {"type": "tool_use", "id": "t%d" % number, "name": "Bash", "input": {"command": command}}
                result = {"type": "tool_result", "tool_use_id": "t%d" % number, "content": output,
                          "is_error": is_error}
                f.write(json.dumps({"type": "assistant", "message": {"content": [use]}}) + "\n")
                f.write(json.dumps({"type": "user", "message": {"content": [result]}}) + "\n")
        return path
