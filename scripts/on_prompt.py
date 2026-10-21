#!/usr/bin/env python3
"""UserPromptSubmit hook: a message starting with "Approved" marks commits in the project repo as manually reviewed.

"Approved <sha> ..." (only SHAs after the word) marks the named commits: unmarked, on HEAD and on no remote. A bare
"Approved" marks the one such commit only when there is exactly one and the agent's last message asked for it with
the line "Reply 'Approved' to mark <sha> as manually reviewed" and the user has not written since (local `!cmd`,
slash-command and task-notice entries don't count), so a reply to something else does not approve it; otherwise
the hook lists the pending commits and asks for SHAs. The user and the agent are both told what was
marked. The hook cannot tell who typed a prompt, so it relies on the agent being unable to send one.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_notes  # noqa: E402

APPROVED_RE = re.compile(r"^\s*approved\b(?P<rest>.*)", re.I | re.S)
BARE_RE = re.compile(r"^[\s.!]*$")
NAMED_RE = re.compile(r"^[\s,:]*(?:[0-9a-f]{7,64}(?=[\s,.!]|$)[\s,.!]*)+$", re.I)
TOKEN_RE = re.compile(r"[0-9a-f]{7,64}", re.I)
LOCAL_RE = re.compile(r"\s*<(?:bash-input|bash-stdout|bash-stderr|command-name|command-message|command-args|"
                      r"local-command-stdout|local-command-stderr|task-notification)>")
REQUEST_RE = re.compile(r"\breply\W+approved\W+to mark\W+([0-9a-f]{7,64})\W+as manually reviewed", re.I)


def say(message):
    print(json.dumps({"systemMessage": message, "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": "Approval hook: " + message}}))
    return 0


def describe(shas, cwd):
    result = review_notes.git(["log", "--no-walk=unsorted", "--format=%h %s", *shas], cwd)
    return result.stdout.strip().replace("\n", "; ")


def where(cwd):
    top = review_notes.git(["rev-parse", "--show-toplevel"], cwd).stdout.strip() or os.path.abspath(cwd)
    branch = review_notes.git(["branch", "--show-current"], cwd).stdout.strip() or "detached HEAD"
    return "%s (%s)" % (top, branch)


def entry_text(content):
    """The text blocks of a transcript message's content, joined."""
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if isinstance(block, dict)
                   and block.get("type") == "text") if isinstance(content, list) else ""


def last_assistant_text(transcript_path, prompt):
    """Text of the agent's last message in the session transcript, or "" if the user wrote to it since. The prompt
    itself may already be logged last; local command entries (`!cmd`, slash commands, task notices) are not the user
    writing."""
    entries = []  # (role, text), with "" for an agent entry without text such as a tool call
    try:
        with open(os.path.expanduser(transcript_path), encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("isMeta") or entry.get("isSidechain"):
                    continue
                message = entry.get("message")
                said = entry_text(message.get("content")) if isinstance(message, dict) else ""
                if entry.get("type") == "assistant" and isinstance(message, dict):
                    entries.append(("assistant", said))
                elif said.strip() and entry.get("type") == "user" and not LOCAL_RE.match(said):
                    entries.append(("user", said))
    except (OSError, TypeError):
        pass
    if entries and entries[-1][0] == "user" and prompt.strip() in entries[-1][1]:  # logged with any attachments
        entries.pop()
    for role, said in reversed(entries):
        if role == "user":
            return ""
        if said.strip():
            return said
    return ""


def choose(rest, pending, shown):
    """(commits to mark, problems)."""
    if NAMED_RE.match(rest):
        chosen, problems = [], []
        for token in dict.fromkeys(TOKEN_RE.findall(rest.lower())):
            matches = [sha for sha in pending if sha.startswith(token)]
            if len(matches) == 1:
                chosen.append(matches[0])
            else:
                problems.append("%s matches %s" % (token, "several pending commits" if matches else
                                                   "no pending commit (already marked, pushed or not on HEAD)"))
        return list(dict.fromkeys(chosen)), problems
    if not BARE_RE.match(rest):
        return [], ["to mark commits, reply 'Approved <sha> ...'"]
    if len(pending) != 1:
        return [], ["%d commits are pending, so name them: 'Approved <sha> ...'" % len(pending)] if pending else []
    if not any(pending[0].startswith(token) for token in REQUEST_RE.findall(shown.lower())):
        return [], ["the last message did not ask \"Reply 'Approved' to mark %s as manually reviewed\", so name it: "
                    "'Approved <sha>'" % pending[0][:12]]
    return pending, []


def main():
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except ValueError:
        return 0
    if not isinstance(event, dict) or event.get("agent_id"):  # a subagent's event; agent_type alone is `claude --agent`
        return 0
    prompt, cwd = event.get("prompt"), os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd")
    match = APPROVED_RE.match(prompt) if isinstance(prompt, str) else None
    if not match:
        return 0
    if not isinstance(cwd, str) or not os.path.isdir(cwd) or not review_notes.is_repo(cwd) \
            or review_notes.resolve("HEAD", cwd) is None:
        return say("No commits marked: %s is not a git repository with commits."
                   % (cwd if isinstance(cwd, str) else "the project directory"))

    marks = review_notes.clean_marks(cwd)
    pending = [sha for sha in review_notes.unpushed(cwd) if sha not in marks]
    transcript = event.get("transcript_path")
    shown = last_assistant_text(transcript, prompt) if isinstance(transcript, str) else ""
    chosen, problems = choose(match.group("rest"), pending, shown)
    marked = []
    for number, sha in enumerate(chosen):
        try:
            review_notes.write_mark(sha, cwd, {"verdict": "clean", "kind": "manual"})
            marked.append(sha)
        except RuntimeError as error:
            problems.append("%s not marked: %s" % (sha[:12], error))
            if chosen[number + 1:]:
                problems.append("%d more not tried" % len(chosen[number + 1:]))
            break
    parts = ["In %s: marked as manually reviewed: %s" % (where(cwd), describe(marked, cwd))] if marked else \
        ["In %s: no commits marked" % where(cwd)]
    left = [sha for sha in pending if sha not in marked]
    if left and problems:
        problems.append("pending: " + describe(left[:20], cwd))
    return say("; ".join(parts + problems) + ".")


if __name__ == "__main__":
    sys.exit(main())
