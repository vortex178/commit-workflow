#!/usr/bin/env python3
"""Carry a clean review mark to a rewritten commit.

    carry.py            mark each of the newest SCAN unmarked commits not on a remote whose change is identical,
                        whitespace and modes included, to that of a commit with a clean mark: e.g. after an earlier
                        commit was amended under it. Binary changes are never carried this way.
    carry.py OLD [NEW]  carry the clean mark of OLD, a commit no longer on HEAD, to NEW (default HEAD, unmarked) when
                        NEW has the same base and edits at most NIT_LIMIT lines of OLD's text files: the nit fixes
                        made after a clean review. Renames, added or deleted files, modes, symlinks and submodules
                        need a review.

A mark carried for nit fixes cannot be carried for nit fixes again, so small changes cannot add up unreviewed.
Diffs come from diff-tree with textconv, external diff and replace objects off, and review_notes refuses marks in
shallow or grafted repos, so local config cannot hide a change.
"""
import hashlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_notes  # noqa: E402

NIT_LIMIT = 10
SCAN = 50
DIFF = review_notes.DIFF + ["--no-renames"]
HUNK_RE = re.compile(rb"^@@ -[0-9,]+ \+[0-9,]+ @@")
INDEX_RE = re.compile(rb"^index \S+")


def checked(result):
    """The output of a git run, or RuntimeError if it failed."""
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else result.stderr
        raise RuntimeError("git failed: " + error.strip())
    return result.stdout


def change_ids(shas, cwd):
    """{commit: hash of its exact patch without hunk positions and blob ids} for the non-merge commits that change
    something and touch no binary file."""
    stdin = "".join(sha + "\n" for sha in shas).encode()
    output = checked(review_notes.git(DIFF + ["--root", "-p", "--binary", "--stdin"], cwd, stdin=stdin, text=False))
    wanted, patches, current = {sha.encode() for sha in shas}, {}, None
    for line in output[:-1].split(b"\n") if output.endswith(b"\n") else output.split(b"\n"):
        if line in wanted:
            current = patches.setdefault(line.decode(), [])
        elif current is not None:
            current.append(INDEX_RE.sub(b"index", HUNK_RE.sub(b"@@ @@", line)))
    patches = {sha: b"\n".join(lines) for sha, lines in patches.items()}
    return {sha: hashlib.sha256(patch).hexdigest() for sha, patch in patches.items()
            if patch and b"\nGIT binary patch\n" not in patch}


def carry_unchanged(cwd, candidates=None):
    """Carry marks to commits (candidates, or the unpushed ones on HEAD) whose change was reviewed under another SHA.
    Returns ([(new, old)], error or None)."""
    marks = review_notes.clean_marks(cwd)
    todo = [sha for sha in (review_notes.unpushed(cwd) if candidates is None else candidates)
            if sha not in marks][:SCAN]
    carried = []
    if not marks or not todo:
        return carried, None
    try:
        reviewed = {}
        sources = change_ids(list(marks), cwd)
        for sha in sorted(sources, key=lambda sha: (marks[sha].get("via") == "nits", sha)):  # prefer a full review
            reviewed.setdefault(sources[sha], sha)
        for sha, change in change_ids(todo, cwd).items():
            old = reviewed.get(change)
            if old:
                via = "nits" if marks[old].get("via") == "nits" else "same-change"
                review_notes.write_mark(sha, cwd, {"verdict": "clean", "kind": "carried", "via": via, "from": old})
                carried.append((sha, old))
    except RuntimeError as error:
        return carried, str(error)
    return carried, None


def base_tree(sha, cwd):
    """Tree of sha's only parent, "" for a root commit, or None for a merge."""
    parents = review_notes.parents(sha, cwd)
    if len(parents) > 1:
        return None
    return checked(review_notes.git(["rev-parse", parents[0] + "^{tree}"], cwd)).strip() if parents else ""


def nit_lines(old, new, cwd):
    """Lines changed from old to new, or an error message when the change is not a plain edit of old's text files."""
    touched = set(checked(review_notes.git(DIFF + ["--root", "--no-commit-id", "--name-only", old], cwd)).splitlines())
    counts = []
    for line in checked(review_notes.git(DIFF + ["--raw", "--numstat", old, new], cwd)).splitlines():
        if line.startswith(":"):
            fields = line.split("\t")[0].lstrip(":").split()
            if len(fields) != 5 or fields[4] != "M" or fields[0] != fields[1] or fields[0] in ("120000", "160000"):
                return "the change adds, deletes or renames a file, or changes a mode, symlink or submodule"
            if line.split("\t", 1)[1] not in touched:
                return "the change edits %s, which %s did not change" % (line.split("\t", 1)[1], old[:12])
        else:
            counts.extend(line.split("\t")[:2])
    if "-" in counts:
        return "the change includes a binary file"
    return sum(int(count) for count in counts)


def carry_nits(old, new, cwd):
    """Carry old's clean mark to new. Returns an error message, or None."""
    marks = review_notes.clean_marks(cwd)
    mark, head = marks.get(old), review_notes.resolve("HEAD", cwd)
    if mark is None:
        return "%s has no clean review mark." % old[:12]
    if new in marks:
        return "%s already has a clean mark." % new[:12]
    if mark.get("via") == "nits":
        return "%s's mark was itself carried for nit fixes; get a review." % old[:12]
    if head and review_notes.is_ancestor(old, head, cwd):
        return "%s is still on this branch, so %s does not replace it." % (old[:12], new[:12])
    if new not in review_notes.unpushed(cwd):
        return "%s is on a remote or not on this branch." % new[:12]
    old_base, new_base = base_tree(old, cwd), base_tree(new, cwd)
    if old_base is None or old_base != new_base:
        return "%s and %s have different bases (or one is a merge); get a review." % (old[:12], new[:12])
    lines = nit_lines(old, new, cwd)
    if isinstance(lines, str):
        return "Not carried: %s; get a review." % lines
    if lines > NIT_LIMIT:
        return "%s changes %d lines against %s, over %d; get a review." % (new[:12], lines, old[:12], NIT_LIMIT)
    review_notes.write_mark(new, cwd, {"verdict": "clean", "kind": "carried", "via": "nits", "from": old,
                                       "lines": lines})
    return None


def main(argv):
    cwd = os.getcwd()
    if not review_notes.is_repo(cwd):
        print("Not in a git repository.")
        return 1
    if not argv:
        carried, error = carry_unchanged(cwd)
        for new, old in carried:
            print("Carried the mark of %s to %s (same change)." % (old[:12], new[:12]))
        if error:
            print("Mark not written: %s" % error)
        elif not carried:
            print("No marks to carry.")
        return 1 if error else 0
    if len(argv) > 2:
        print("Usage: carry.py [OLD [NEW]]")
        return 1
    names = [argv[0], argv[1] if len(argv) > 1 else "HEAD"]
    old, new = (review_notes.resolve(name, cwd) for name in names)
    if old is None or new is None:
        print("Unknown commit: %s" % names[0 if old is None else 1])
        return 1
    try:
        error = carry_nits(old, new, cwd)
    except RuntimeError as failure:
        error = "Mark not written: %s" % failure
    print(error or "Carried the mark of %s to %s (nit fixes)." % (old[:12], new[:12]))
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
