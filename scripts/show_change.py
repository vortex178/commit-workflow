#!/usr/bin/env python3
"""Print a commit's change for review, in parts that each fit in one Bash tool result.

    show_change.py SHA [--from PREV] [--context N] [--part N]

The change is SHA's patch against its first parent or, with --from, the patch from PREV to SHA: what an amend
changed, for a later review round. PREV must have the same first parent as SHA. The patch comes from diff-tree with
renames on, every file shown as text, and textconv, external diff and colour off. Each part is framed by lines naming
the commit; the review hook recomputes the parts and writes a mark only when every one appears unaltered in the
reviewer's output.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_notes  # noqa: E402

MAX_CONTEXT = 9999  # the hook reads at most four digits
PART_CHARS = 20000  # UTF-16 units; the Bash tool shows about 30000 inline
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
BINARY = "the change has binary content, which cannot be shown; ask the user to approve it manually."


def normalize(text):
    """Lines without colour codes and trailing whitespace: what survives the trip through the tool result."""
    # Stray escape characters go too, so normalizing twice changes nothing.
    return [ANSI_RE.sub("", line).replace("\x1b", "").rstrip() for line in text.split("\n")]


def width(line):
    """Length as the tool counts it (UTF-16 code units)."""
    return len(line.encode("utf-16-le")) // 2


def parts(sha, prev, context, cwd):
    """The change's parts as printed, or an error message."""
    try:
        first = review_notes.parents(sha, cwd)[:1]
        if prev and review_notes.parents(prev, cwd)[:1] != first:
            return "%s does not have the same parent as %s; review %s in full." % (prev[:12], sha[:12], sha[:12])
    except RuntimeError as error:
        return "git failed: %s" % error
    revisions = [prev, sha] if prev else first + [sha]
    result = review_notes.git(review_notes.DIFF + ["-p", "-M", "--root", "--text", "--full-index",
                                                   "--diff-algorithm=myers", "-U%d" % context, *revisions], cwd)
    if result.returncode != 0:
        return "git failed: " + result.stderr.strip()
    if "\0" in result.stdout:
        return BINARY
    lines = []
    for line in normalize(result.stdout.rstrip("\n")) if result.stdout else []:
        pieces = [line[start:start + PART_CHARS // 2] for start in range(0, len(line), PART_CHARS // 2)]
        lines.extend([piece.rstrip() for piece in pieces] or [""])
    bodies, size = [[]], 0
    for line in lines:
        if size + width(line) + 1 > PART_CHARS and bodies[-1]:
            bodies.append([])
            size = 0
        bodies[-1].append(line)
        size += width(line) + 1
    name = "%s..%s" % (prev, sha) if prev else sha
    return ["\n".join(["== commit-review %s -U%d part %d/%d ==" % (name, context, number, len(bodies)), *body,
                       "== end of part %d/%d ==" % (number, len(bodies))])
            for number, body in enumerate(bodies, 1)]


def main(argv):
    parser = argparse.ArgumentParser(description="Print a commit's change for review.")
    parser.add_argument("sha")
    parser.add_argument("--from", dest="prev")
    parser.add_argument("--context", type=int, default=3)
    parser.add_argument("--part", type=int, default=1)
    args = parser.parse_args(argv)
    cwd = os.getcwd()
    sha, prev = review_notes.resolve(args.sha, cwd), review_notes.resolve(args.prev, cwd) if args.prev else ""
    if sha is None or prev is None:
        print("Unknown commit: %s" % (args.sha if sha is None else args.prev))
        return 1
    if not 0 <= args.context <= MAX_CONTEXT:
        print("--context must be from 0 to %d." % MAX_CONTEXT)
        return 1
    shown = parts(sha, prev, args.context, cwd)
    if isinstance(shown, str):
        print("Cannot show the change: " + shown)
        return 1
    if not 1 <= args.part <= len(shown):
        print("There are %d parts." % len(shown))
        return 1
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(shown[args.part - 1])
    if args.part < len(shown):
        print('Next, in its own command: python3 "%s" %s%s --context %d --part %d'
              % (os.path.abspath(__file__), sha, " --from " + prev if prev else "", args.context, args.part + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
