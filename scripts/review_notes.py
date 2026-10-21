"""Review marks: git notes in refs/notes/review recording that one exact commit passed review.

A mark is a JSON object attached to a commit SHA and naming that SHA. Amending or rebasing creates a new SHA; if
git copies the note across (notes.rewriteRef), the copy names the old SHA and is ignored. Git runs without the
caller's GIT_* variables and with replace objects off, and no mark is written in a shallow repo or one with grafts,
since those change what a commit's parent is.
"""
import json
import os
import subprocess
from datetime import datetime, timezone

NOTES_REF = "refs/notes/review"
ENV = dict({key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
           GIT_NO_REPLACE_OBJECTS="1")
DIFF = ["diff-tree", "-r", "--no-textconv", "--no-ext-diff", "--ignore-submodules=none", "--no-color"]


def git(args, cwd, stdin=None, text=True):
    """Run git; with text=False, stdin and the output are bytes."""
    decode = {"encoding": "utf-8", "errors": "replace"} if text else {}
    return subprocess.run(["git", *args], cwd=cwd, input=stdin, capture_output=True, env=ENV, **decode)


def is_repo(cwd):
    return git(["rev-parse", "--git-dir"], cwd).returncode == 0


def resolve(rev, cwd):
    """Full SHA of the commit rev names, or None."""
    if not rev or rev.startswith("-"):
        return None
    result = git(["rev-parse", "--verify", "--quiet", rev + "^{commit}"], cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def parents(sha, cwd):
    """The parents of commit sha, first parent first. RuntimeError if git fails."""
    result = git(["rev-list", "--parents", "-n", "1", sha], cwd)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(result.stderr.strip() or "git rev-list failed")
    return result.stdout.split()[1:]


def is_ancestor(sha, head, cwd):
    return git(["merge-base", "--is-ancestor", sha, head], cwd).returncode == 0


def unpushed(cwd):
    """Commits on HEAD that no remote-tracking branch contains, newest first."""
    result = git(["rev-list", "HEAD", "--not", "--remotes"], cwd)
    return result.stdout.split() if result.returncode == 0 else []


def parse_mark(text, sha):
    """The mark in a note's text if it is a JSON object naming sha, else None."""
    try:
        mark = json.loads(text)
    except ValueError:
        return None
    return mark if isinstance(mark, dict) and mark.get("commit") == sha else None


def clean_marks(cwd):
    """{sha: mark} for every commit with a clean mark, read in one batch."""
    listed = git(["notes", "--ref", NOTES_REF, "list"], cwd)
    pairs = [line.split() for line in listed.stdout.splitlines()] if listed.returncode == 0 else []
    pairs = [pair for pair in pairs if len(pair) == 2]
    stdin = "".join(note + "\n" for note, _ in pairs).encode()
    rest = git(["cat-file", "--batch"], cwd, stdin=stdin, text=False).stdout
    marks = {}
    for _, sha in pairs:
        header, _, rest = rest.partition(b"\n")
        fields = header.split()
        if len(fields) != 3 or not fields[2].isdigit():
            continue  # missing object: no body follows
        size = int(fields[2])
        mark = parse_mark(rest[:size].decode("utf-8", "replace"), sha)
        rest = rest[size + 1:]
        if is_clean(mark):
            marks[sha] = mark
    return marks


def read_mark(sha, cwd):
    """The mark on sha, or None. A note copied from another commit does not count."""
    result = git(["notes", "--ref", NOTES_REF, "show", sha], cwd)
    return parse_mark(result.stdout, sha) if result.returncode == 0 else None


def write_mark(sha, cwd, mark):
    """Attach mark to sha, replacing any earlier mark. Adds the SHA and a UTC timestamp."""
    shallow = git(["rev-parse", "--is-shallow-repository"], cwd)
    grafts = git(["rev-parse", "--git-path", "info/grafts"], cwd)
    if shallow.returncode != 0 or grafts.returncode != 0:
        raise RuntimeError((shallow.stderr or grafts.stderr).strip() or "git rev-parse failed")
    if shallow.stdout.strip() == "true" or os.path.exists(os.path.join(cwd, grafts.stdout.strip())):
        raise RuntimeError("the repository is shallow or has grafts, so a commit's parent cannot be trusted")
    mark = dict(mark, commit=sha, at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    result = git(["notes", "--ref", NOTES_REF, "add", "-f", "-m", json.dumps(mark, sort_keys=True), sha], cwd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git notes add failed")
    return mark


def is_clean(mark):
    return bool(mark) and mark.get("verdict") == "clean"
