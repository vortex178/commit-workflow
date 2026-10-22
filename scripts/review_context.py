#!/usr/bin/env python3
"""Print context for a commit's reviewer: the functions it touches, where they are called, and runtime hazards.

    review_context.py SHA [--calls N]

The committer pastes the output into the reviewer prompt, so the reviewer explores less. It is plain text search
over the commit's tree, so it works in any language but is approximate: names come from the definitions in hunk
headers and changed lines (outside test files), uses are whole-word matches, and hazards are lines in the changed
files, outside tests, that print, exit, touch global state, start processes or use the network. Read-only; it runs
git only.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_notes  # noqa: E402

DEF_RE = re.compile(r"\b(?:def|class|func(?:\s*\([^)]*\))?|fn|function|sub|struct|interface|trait|impl|module)"
                    r"\s+\*?([A-Za-z_]\w*)")  # func (r *T) Name: a Go method
TEST_RE = re.compile(r"(?:^|/)(?:tests?|spec)/|(?:^|/)test_|_test\.|\.(?:test|spec)\.")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@ ?(.*)$")
HAZARDS = [
    ("output", r"\bprint\s*\(|\bconsole\.(?:log|error|warn)|sys\.std(?:out|err)|\becho\b|fmt\.Print|"
               r"System\.(?:out|err)|\bprintf\s*\(|\blog(?:ging)?\.\w+\("),
    ("exit", r"\bsys\.exit|\bos\._exit|\bexit\s*\(|process\.exit|\bpanic\s*\(|SystemExit|\babort\s*\("),
    ("global state", r"\bglobal\s|os\.environ|\bsetenv\b|\bputenv\b|\bchdir\s*\(|process\.env|\bexport\s+\w+="),
    ("process", r"\bsubprocess\.|\bos\.system|\bos\.exec|\bexecv|\bspawn\s*\(|\bPopen\b|child_process"),
    ("network", r"\brequests\.|\burllib|\bhttp\.client|\bsocket\.|\bfetch\s*\(|\bcurl\b|\bwget\b|net/http|"
                r"\bHttpClient\b|\baxios\b"),
]
MAX_NAMES = 30
MAX_HAZARDS = 15


def changed(sha, cwd):
    """(names of the functions and definitions the commit touches, paths it changes), from its first-parent diff."""
    result = review_notes.git(review_notes.DIFF + ["-p", "-U0", "--root", "--first-parent", "-m", sha], cwd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff-tree failed")
    names, paths, path = [], [], ""
    for line in result.stdout.splitlines():
        if line.startswith(("+++ ", "--- ")):
            path = line[len("+++ b/"):] if line.startswith(("+++ b/", "--- a/")) else path
            if line.startswith("+++ b/"):
                paths.append(path)
            continue
        hunk = HUNK_RE.match(line)
        match = DEF_RE.search(hunk.group(1) if hunk else line[1:] if line[:1] in "+-" else "")
        if match and match.group(1) not in names and not TEST_RE.search(path):
            names.append(match.group(1))
    return names[:MAX_NAMES], paths


def grep(sha, cwd, args):
    """(path, line number, text) for each match of git grep at sha."""
    result = review_notes.git(["grep", "-I", "-n", "--no-color", *args, sha, "--"], cwd)
    found = []
    for line in result.stdout.splitlines():
        parts = line[len(sha) + 1:].split(":", 2) if line.startswith(sha + ":") else []
        if len(parts) == 3 and parts[1].isdigit():
            found.append((parts[0], int(parts[1]), parts[2].strip()))
    return found


def context(sha, cwd, calls):
    """The report as lines."""
    names, paths = changed(sha, cwd)
    lines = ["== review context for %s ==" % sha[:12], "", "Changed files: %s" % (", ".join(paths) or "none"), ""]
    lines.append("Touched functions and where they are used (whole-word matches, approximate):")
    for name in names:
        uses = [item for item in grep(sha, cwd, ["-w", "-F", "-e", name]) if not DEF_RE.search(item[2])]
        shown = ", ".join("%s:%d" % (path, number) for path, number, _ in uses[:calls])
        lines.append("- %s: %s%s" % (name, shown or "no other uses",
                                     " (+%d more)" % (len(uses) - calls) if len(uses) > calls else ""))
    if not names:
        lines.append("- none found")
    lines += ["", "Runtime hazards in the changed files outside tests (check what the change calls into as well):"]
    texts = {path: review_notes.git(["cat-file", "blob", "%s:%s" % (sha, path)], cwd).stdout for path in paths}
    for kind, pattern in HAZARDS:
        hits = [(path, number) for path in paths if "\0" not in texts[path] and not TEST_RE.search(path)
                for number, text in enumerate(texts[path].splitlines(), 1) if re.search(pattern, text)]
        if hits:
            lines.append("- %s: %s%s" % (kind, ", ".join("%s:%d" % hit for hit in hits[:MAX_HAZARDS]),
                                         " (+%d more)" % (len(hits) - MAX_HAZARDS) if len(hits) > MAX_HAZARDS
                                         else ""))
    if lines[-1].startswith("Runtime hazards"):
        lines.append("- none found")
    return lines


def main(argv):
    parser = argparse.ArgumentParser(description="Print context for a commit's reviewer.")
    parser.add_argument("sha")
    parser.add_argument("--calls", type=int, default=8, help="call sites to list per function")
    args = parser.parse_args(argv)
    cwd = os.getcwd()
    sha = review_notes.resolve(args.sha, cwd)
    if sha is None:
        print("Unknown commit: %s" % args.sha)
        return 1
    try:
        print("\n".join(context(sha, cwd, max(args.calls, 1))))
    except RuntimeError as error:
        print("git failed: %s" % error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
