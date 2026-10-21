#!/usr/bin/env python3
"""SubagentStop hook: turn a commit-reviewer's clean verdict into a review mark on the reviewed commit.

The mark is written only when the verdict names a commit on the current branch and the reviewer's own transcript
shows a successful Bash `git show <sha>` or `git diff <rev> <sha>` that printed a patch. The command must name the
commit itself, not HEAD, so a read from an earlier round cannot stand in for the amended commit. Problems the
reviewer can fix make it continue once with instructions. The committer never writes marks itself.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_notes  # noqa: E402

REVIEWER = "commit-workflow:commit-reviewer"
VERDICT_RE = re.compile(r"^[ \t>*`]*VERDICT:[ \t]*([a-z-]+)(?:[ \t]+([0-9a-f]{7,64}))?[ \t*`.]*$", re.M | re.I)
SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")
GIT_READ_RE = re.compile(r"^\s*(?:\w+=\S*\s+)*git(?:\s+-[Cc]\s+\S+|\s+--[\w-]+(?:=\S+)?)*\s+(show|diff)(?:\s+(.*))?$")
NO_PATCH = {"-s", "--no-patch", "-q", "--quiet", "--check", "--stat", "--shortstat", "--numstat", "--name-only",
            "--name-status", "--raw", "--summary", "--dirstat", "--compact-summary"}
REDIRECT_RE = re.compile(r"\d*(?:>&\d+|[<>]{1,2}\s*\S+)")


def parse_verdict(text):
    """(word, sha prefix or "") from the last VERDICT line, lowercased, or None."""
    matches = VERDICT_RE.findall(text or "")
    return tuple(part.lower() for part in matches[-1]) if matches else None


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


def bash_reads(transcript_path):
    """(command, output) for each Bash call in the agent's JSONL transcript that completed without error."""
    calls, results = [], {}
    try:
        with open(os.path.expanduser(transcript_path), encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                message = entry.get("message") if isinstance(entry, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use" and block.get("name") == "Bash":
                        tool_input = block.get("input")
                        command = tool_input.get("command") if isinstance(tool_input, dict) else None
                        if isinstance(command, str):
                            calls.append((block.get("id"), command))
                    elif block.get("type") == "tool_result" and not block.get("is_error"):
                        results[block.get("tool_use_id")] = _text(block.get("content"))
    except (OSError, TypeError):
        pass
    return [(command, results[call_id]) for call_id, command in calls if call_id in results]


def names_commit(token, sha):
    return bool(review_notes.HEX_RE.match(token)) and sha.startswith(token)


def read_the_patch(reads, sha):
    for command, output in reads:
        if "diff --git" not in output and "diff --cc" not in output:
            continue
        for segment in SEGMENT_SPLIT_RE.split(command):
            match = GIT_READ_RE.match(segment)
            if not match:
                continue
            args = REDIRECT_RE.sub(" ", match.group(2) or "").split()
            if "--" in args:  # limited to some paths
                continue
            if NO_PATCH.intersection(arg.split("=")[0] for arg in args) and not {"-p", "--patch"}.intersection(args):
                continue
            revisions = [arg for arg in args if not arg.startswith("-")]
            if match.group(1) == "show" and all(review_notes.HEX_RE.match(rev) for rev in revisions) \
                    and any(names_commit(rev, sha) for rev in revisions):
                return True
            if match.group(1) == "diff":
                if len(revisions) == 2 and names_commit(revisions[1], sha):
                    return True
                if len(revisions) == 1 and "..." not in revisions[0] and ".." in revisions[0] \
                        and names_commit(revisions[0].split("..")[1], sha):
                    return True
    return False


def report(message):
    print(json.dumps({"systemMessage": message}))
    return 0


def problem(event, instruction, outcome):
    """Ask the reviewer to fix it once; after that, tell the user no mark was written."""
    if event.get("stop_hook_active"):
        return report("Review mark not written: " + outcome)
    sys.stderr.write(instruction + "\n")
    return 2


def main():
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except ValueError:
        return 0
    if not isinstance(event, dict) or event.get("agent_type") != REVIEWER:
        return 0
    cwd = event.get("cwd") or os.getcwd()
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        return 0
    head = review_notes.resolve("HEAD", cwd) if review_notes.is_repo(cwd) else None
    if head is None:
        return 0

    verdict = parse_verdict(event.get("last_assistant_message"))
    if verdict is None or (verdict[0] == "clean" and not verdict[1]):
        return problem(event, "End your report with a verdict line naming the commit: 'VERDICT: clean %s' or "
                              "'VERDICT: blocking'." % head, "the reviewer gave no verdict line with a commit.")
    word, prefix = verdict
    if word == "blocking":
        return 0
    if word != "clean":
        return problem(event, "'%s' is not a verdict. End with 'VERDICT: clean %s' or 'VERDICT: blocking'."
                       % (word, head), "the verdict word was '%s'." % word)

    sha = review_notes.resolve(prefix, cwd)
    if sha is None or not review_notes.is_ancestor(sha, head, cwd):
        return problem(event, "The verdict names %s, which is not a commit on the current branch (HEAD is %s). "
                              "Name the commit you reviewed." % (prefix, head), "the verdict names %s." % prefix)
    if not read_the_patch(bash_reads(event.get("agent_transcript_path") or ""), sha):
        return problem(event, "Before a clean verdict, run `git show %s` (or `git diff <prev> %s` for a later round) "
                              "so the patch is in your transcript, then repeat the verdict." % (sha, sha),
                       "the reviewer's transcript shows no git show/diff of %s." % sha[:12])
    try:
        review_notes.write_mark(sha, cwd, {"verdict": "clean", "kind": "agent",
                                           "reviewer": REVIEWER, "agent_id": event.get("agent_id")})
    except RuntimeError as error:
        return report("Review mark not written: %s" % error)
    return report("Review mark written for %s." % sha[:12])


if __name__ == "__main__":
    sys.exit(main())
