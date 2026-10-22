import os
import unittest

from helpers import RepoCase

import shell_words


def run_words(command, cwd="/"):
    return [found[1] for found in shell_words.commands(command, cwd)]


class CommandsTest(unittest.TestCase):
    def test_commands_inside_compound_statements_wrappers_and_shells_are_found(self):
        for command in ["if true; then git push; fi", "for b in main; do git push; done", "{ git push; } 2>&1",
                        "! git push", "echo ok; git push 2>&1", "a=1 && git push | tail -2", "(git push)",
                        "bash -lc 'git push'", "sh -ec 'git push'", "zsh -c 'git push'", "eval git push",
                        "bash <<EOF\ngit push\nEOF", 'out="$(git push 2>&1)"', "out=`git push`",
                        "sudo -u me git push", "env -i A=1 git push", "timeout -s KILL 30 git push",
                        "nice -n 5 git push", "caffeinate -i git push", "GIT_TRACE=1 git push",
                        "time -p git push", "uv run --frozen git push", "uv run --with x -p 3.9 git push",
                        "sudo --user me git push", "xargs -a f git push", "gtimeout 60 git push",
                        "bash -s x <<EOF\ngit push\nEOF", "git rebase -x 'git push' main"]:
            self.assertIn(["git", "push"], run_words(command), command)

    def test_cd_forms_the_gate_does_not_follow_leave_the_directory_unknown(self):
        for command in ["cd /a & x", "cd /a | cat; x", "cat | cd /a; x", "pushd /a && pushd && x", "pushd -n /a; x",
                        "pushd /a; popd +1; x", "cd -0; x", "cd a b; x", "pwd |\ncd /a\nx",
                        "pwd | # c\ncd /a\nx"]:
            self.assertIn("$(", shell_words.commands(command, "/")[-1][0], command)

    def test_directories_follow_cd_and_pushd(self):
        found = shell_words.commands("cd /tmp && git status; pushd -q /usr; ls; popd; (cd /a && make) && pwd; "
                                     "env -C /b ls; out=\"$(git status)\"; env --chdir=/c ls; env -C/d ls; "
                                     "uv --directory /e run ls; cd /f; cd -; ls; cd; ls", "/")
        self.assertEqual([(cwd, words[0]) for cwd, words, *_ in found if words[:1] not in ([], ["cd"], ["pushd"],
                                                                                         ["popd"])],
                         [("/tmp", "git"), ("/usr", "ls"), ("/a", "make"), ("/tmp", "pwd"), ("/b", "ls"),
                          ("/tmp", "git"), ("/c", "ls"), ("/d", "ls"), ("/e", "ls"), ("/tmp", "ls"),
                          (os.path.expanduser("~"), "ls")])
        self.assertEqual(shell_words.commands("git submodule foreach 'git push'", "/r")[0][0], "/r/$(each submodule)")
        self.assertEqual([item[1:3] for item in shell_words.commands("X=1; { Y=2; }; Z=3 true", "/")],
                         [([], ["X=1"]), ([], ["Y=2"]), (["true"], ["Z=3"])])  # assignments are kept
        plain = [(words[0], plain) for _, words, _, _, plain in shell_words.commands(
            "a $(b) && sudo c; bash -c 'd'; GIT_TRACE=1 e; if true; then f; fi", "/")]
        self.assertEqual(plain, [("b", False), ("a", True), ("c", False), ("d", False), ("bash", True), ("e", True),
                                 ("true", True), ("f", True), ("fi", True)])

    def test_what_cannot_be_read_raises(self):
        for command in ["echo 'git push' | sh", "a $(b $(c $(d $(e $(git push)))))"]:
            with self.assertRaises(ValueError, msg=command):
                shell_words.commands(command, "/")

    def test_common_substitutions_are_read(self):
        self.assertEqual(run_words("git push origin $(git branch --show-current) $(other)")[-1],
                         ["git", "push", "origin", "HEAD", "$(other)"])

    def test_git_parts_and_python_targets(self):
        self.assertEqual(shell_words.git_parts(["git", "-C", "a", "-c", "x=1", "--git-dir=b/.git", "push", "-f"],
                                               "/r"), ("/r/a/b/.git", "push", ["-f"], ["x=1"]))
        self.assertEqual(shell_words.git_parts(["git", "status"], "/r", ["GIT_DIR=/g"])[0], "/g")
        self.assertEqual(shell_words.git_parts(["git", "--git-dir=.git", "-C", "a", "p"], "/r")[0], "/r/a/.git")
        self.assertIsNone(shell_words.git_parts(["git", "--version"], "/r"))
        for words, target in [(["python3", "-I", "x.py"], ("file", "x.py")), (["python3", "-c", "1"], ("-c", "1")),
                              (["python3", "-W", "error", "-m", "mod"], ("-m", "mod")), (["python3"], ("-", "")),
                              (["python3", "-mmod"], ("-m", "mod")), (["python3", "--version"], ("", ""))]:
            self.assertEqual(shell_words.python_target(words), target)


class AliasTest(RepoCase):
    def test_plain_shell_nested_and_command_line_aliases_are_expanded(self):
        for key, value in [("alias.pu", "push"), ("alias.p", "pu --force"), ("alias.publish", "!git push -u origin")]:
            self.git("config", key, value)
        self.assertIn(["git", "push", "--force", "origin"], run_words("git p origin", self.repo))
        self.assertIn(["git", "push", "-u", "origin", "HEAD"], run_words("git publish HEAD", self.repo))
        self.assertIn(["git", "-c", "alias.q=push", "push"], run_words("git -c alias.q=push q", self.repo))
        found = shell_words.commands("git -C %s publish" % self.repo, "/")
        self.assertIn((self.repo, ["git", "push", "-u", "origin"]), [(cwd, words) for cwd, words, *_ in found])
        self.assertEqual(run_words("git status", os.path.join(self.repo, "missing")), [["git", "status"]])
        os.mkdir(os.path.join(self.repo, "sub"))
        self.git("config", "alias.pi", "!cd inner && git push")
        self.git("config", "alias.status", "push")
        sub, inner = os.path.join(self.repo, "sub"), os.path.join(self.repo, "inner")
        found = [(cwd, words) for cwd, words, *_ in shell_words.commands("git pi; git status", sub)]
        self.assertIn((inner, ["git", "push"]), found)  # shell aliases run from the top; builtins are not aliased
        self.assertNotIn(["git", "push"], [words for cwd, words in found if cwd != inner])
        self.assertEqual(shell_words.commands("cd $(git rev-parse --show-toplevel) && ls", sub)[-1][0], self.repo)
        found = shell_words.commands('cd "$(git rev-parse --show-toplevel)/sub" && git pu; cd "$x" && git pu', sub)
        self.assertEqual([cwd for cwd, words, *_ in found if words == ["git", "push"]], [sub, sub + "/$x"])


if __name__ == "__main__":
    unittest.main()
