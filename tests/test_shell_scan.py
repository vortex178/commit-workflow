import unittest

import helpers  # noqa: F401 (puts scripts/ on the path)

import shell_scan


def run_words(text):
    """Word lists of the commands in text, substitutions first."""
    found = []

    def walk(nodes):
        for node in nodes:
            if node not in ("(", ")"):
                for sub in node[2]:
                    walk(sub)
                found.append(node[1])
    walk(shell_scan.parse(text))
    return found


class ScanTest(unittest.TestCase):
    def test_separators_subshells_and_substitutions_split_commands(self):
        for command in ["echo ok; git push 2>&1", "a && git push | tail -2", "(git push)", "x || git push &",
                        'out="$(git push 2>&1)"', "out=`git push`", "echo \"$(echo '(x)'; git push)\"",
                        'out="$(git log -1 $(git rev-parse HEAD); git push)"', "diff <(git push) x",
                        "cat <<EOF\n$(git push)\nEOF", "echo $((1<<2))\ngit push", "case x in a) git push;; esac"]:
            self.assertIn(["git", "push"], run_words(command), command)
        self.assertEqual(shell_scan.parse("(cd a && b) && c"), ["(", ("cmd", ["cd", "a"], [], []),
                                                                ("cmd", ["b"], [], []), ")", ("cmd", ["c"], [], [])])
        self.assertEqual([node[0] for node in shell_scan.parse("a | b & c; d && e |& f")],
                         ["pipe", "pipe", "cmd", "cmd", "pipe", "pipe"])

    def test_comments_continuations_and_here_documents_do_not_hide_commands(self):
        for command in ["git status # don't\ngit push", "git \\\n push", "cat > m.txt <<'X'\ndon't\nX\ngit push",
                        "cat <<-X\n\tit's\n\tX\ngit push",
                        "git commit -m \"$(cat <<'EOF'\nIt's \"done\"\nEOF\n)\" && git push"]:
            self.assertIn(["git", "push"], run_words(command), command)
        self.assertEqual(run_words("git commit -m fix#1 # a comment"), [["git", "commit", "-m", "fix#1"]])
        self.assertEqual(run_words("echo $# ${#x}"), [["echo", "$#", "${#x}"]])
        self.assertEqual(run_words("cat <<<'it''s'"), [["cat"]])
        self.assertEqual(run_words("git commit -m 'run `git push` or $(git push)' 2>&1"),
                         [["git", "commit", "-m", "run `git push` or $(git push)"]])
        self.assertEqual(run_words("cat > n.md <<'EOF'\nit's\nEOF\nbash test.sh"), [["cat"], ["bash", "test.sh"]])
        self.assertEqual(run_words("cat <<\\EOF\nit's $(x)\nEOF\ngit push"), [["cat"], ["git", "push"]])
        self.assertEqual(run_words('git commit -m "$(echo "it\'s")"')[0], ["echo", "it's"])
        self.assertEqual(run_words("echo a\\ #b; head -n 2 > out"), [["echo", "a #b"], ["head", "-n", "2"]])

    def test_here_document_bodies_are_kept_apart(self):
        nodes = shell_scan.parse("python3 - <<'EOF'\nimport os\nEOF\ncat <<X\n$(ls)\nX")
        self.assertEqual([(node[1], node[3]) for node in nodes],
                         [(["python3", "-"], ["import os"]), (["cat"], ["$(ls)"])])
        self.assertEqual(run_words("cat <<X\n$(ls)\nX"), [["ls"], ["cat"]])  # an unquoted body's substitutions run

    def test_redirections_arithmetic_and_expansions_do_not_hide_commands(self):
        for command in ["while read b; do :; done < <(git branch)\ngit push", "echo $'it\\'s'; git push",
                        "echo $((cd a; git push) && x)", "out=$((cd a; git push))", "echo $(( $(git push) + 1 ))",
                        "(( x = 1 << 2 ))\ngit push", "for ((i=0; i<3; i++))\ngit push",
                        '"${x:-"a;b"}"; git push', "<<EOF\n$(git push)\nEOF", "x=$(cat <<EOF\nhi\nEOF\n); git push",
                        "x=$(cat <<EOF\nhi\nEOF)\ngit push"]:
            self.assertIn(["git", "push"], run_words(command), command)
        self.assertEqual(shell_scan.parse("do (cd a && b)")[:2], ["(", ("cmd", ["do", "cd", "a"], [], [])])
        self.assertEqual(run_words("echo ${x%%;*} &>log"), [["echo", "${x%%;*}"]])

    def test_unbalanced_text_raises(self):
        for text in ["echo 'unbalanced", "echo `x", "echo $(x", "echo $((1", 'echo "x', "cat <<"]:
            with self.assertRaises(ValueError, msg=text):
                shell_scan.parse(text)


if __name__ == "__main__":
    unittest.main()
