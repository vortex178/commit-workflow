"""Review marks: git notes in refs/notes/review recording that one exact commit passed review.

A mark is a JSON object attached to a commit SHA and naming that SHA. Amending or rebasing creates a new SHA; if
git copies the note across (notes.rewriteRef), the copy names the old SHA and is ignored.
"""
import json
import subprocess
from datetime import datetime, timezone

NOTES_REF = "refs/notes/review"


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace")


def is_repo(cwd):
    return _git(["rev-parse", "--git-dir"], cwd).returncode == 0


def resolve(rev, cwd):
    """Full SHA of the commit rev names, or None."""
    result = _git(["rev-parse", "--verify", "--quiet", rev + "^{commit}"], cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_ancestor(sha, head, cwd):
    return _git(["merge-base", "--is-ancestor", sha, head], cwd).returncode == 0


def read_mark(sha, cwd):
    """The mark on sha, or None. A note copied from another commit does not count."""
    result = _git(["notes", "--ref", NOTES_REF, "show", sha], cwd)
    if result.returncode != 0:
        return None
    try:
        mark = json.loads(result.stdout)
    except ValueError:
        return None
    return mark if isinstance(mark, dict) and mark.get("commit") == sha else None


def write_mark(sha, cwd, mark):
    """Attach mark to sha, replacing any earlier mark. Adds the SHA and a UTC timestamp."""
    mark = dict(mark, commit=sha, at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    result = _git(["notes", "--ref", NOTES_REF, "add", "-f", "-m", json.dumps(mark, sort_keys=True), sha], cwd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git notes add failed")
    return mark


def is_clean(mark):
    return bool(mark) and mark.get("verdict") == "clean"
