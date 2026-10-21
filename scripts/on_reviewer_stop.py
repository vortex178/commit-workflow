#!/usr/bin/env python3
"""SubagentStop hook: turn a commit-reviewer's clean verdict into a review mark on the reviewed commit.

The mark is written only when the verdict names a commit on the current branch and the reviewer saw that commit's
change: every part that show_change.py prints for it must appear unaltered in the output of one of the reviewer's
successful Bash commands. The hook recomputes the parts itself, so a filtered, truncated or stale read cannot stand in
for them. A later round may instead read the change from <prev> (`show_change.py <sha> --from <prev>`), when <prev>
has the same parent and was itself seen that way. Problems the reviewer can fix make it continue once with
instructions.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_notes  # noqa: E402
import show_change  # noqa: E402

REVIEWER = "commit-workflow:commit-reviewer"
VERDICT_RE = re.compile(r"^[ \t>*`]*VERDICT:[ \t]*([a-z-]+)(?:[ \t]+([0-9a-f]{7,64}))?[ \t*`.]*$", re.M | re.I)
HEADER_RE = re.compile(r"^== commit-review (?:([0-9a-f]{40,64})\.\.)?([0-9a-f]{40,64}) -U([0-9]{1,4}) part 1/", re.M)
MAX_ROUNDS = 5
MAX_READS = 50


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


def bash_outputs(transcript_path):
    """The output of each Bash call in the agent's JSONL transcript that completed without error, normalized."""
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
                        calls.append(block.get("id"))
                    elif block.get("type") == "tool_result" and not block.get("is_error"):
                        results[block.get("tool_use_id")] = _text(block.get("content"))
    except (OSError, TypeError):
        pass
    return ["\n" + "\n".join(show_change.normalize(results[call])) + "\n" for call in calls if call in results]


def saw(sha, outputs, cwd, rounds=0, memo=None):
    """Every part of sha's change, or of a change from a commit that was itself seen, is among outputs."""
    memo = {} if memo is None else memo
    if (sha, rounds) not in memo:  # MAX_ROUNDS ends a cycle of --from reads
        memo[sha, rounds] = _saw(sha, outputs, cwd, rounds, memo)
    return memo[sha, rounds]


def _saw(sha, outputs, cwd, rounds, memo):
    reads = {(prev or "", int(context)) for output in outputs for prev, name, context in HEADER_RE.findall(output)
             if name == sha}
    for prev, context in sorted(reads)[:MAX_READS]:
        if prev and (rounds >= MAX_ROUNDS or prev == sha):
            continue
        parts = show_change.parts(sha, prev, context, cwd)
        expected = ["\n" + "\n".join(show_change.normalize(part)) + "\n" for part in parts] \
            if isinstance(parts, list) else []
        if expected and all(any(part in output for output in outputs) for part in expected) \
                and (not prev or saw(prev, outputs, cwd, rounds + 1, memo)):
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
    if not saw(sha, bash_outputs(event.get("agent_transcript_path") or ""), cwd):
        if show_change.parts(sha, "", 3, cwd) == show_change.BINARY:
            return report("Review mark not written for %s: %s" % (sha[:12], show_change.BINARY))
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "show_change.py")
        return problem(event, "Before a clean verdict, run `python3 \"%s\" %s` and each further part it names, each "
                              "in its own command, without filtering its output (in a later round, the same script "
                              "with `%s --from <prev>`, once you have read <prev> that way), then repeat the verdict."
                       % (script, sha, sha), "the reviewer's transcript shows no complete read of %s." % sha[:12])
    try:
        review_notes.write_mark(sha, cwd, {"verdict": "clean", "kind": "agent",
                                           "reviewer": REVIEWER, "agent_id": event.get("agent_id")})
    except RuntimeError as error:
        return report("Review mark not written: %s" % error)
    return report("Review mark written for %s." % sha[:12])


if __name__ == "__main__":
    sys.exit(main())
