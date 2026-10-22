#!/usr/bin/env python3
"""PreToolUse hook: keep the agent away from review marks.

On Bash, and on any tool whose input has a shell `command`, it denies a command that
- writes refs/notes/review, moves a remote-tracking ref, runs the hook scripts or imports their mark-writing code,
  which leaves carry.py as the only mark writer the agent can run;
- starts another Claude session with a prompt mentioning approval.
On the tools that send prompts later or elsewhere (scheduled prompts, loops, messages to sessions) it denies a
prompt that starts with "Approved", since only the user may approve a commit.

It reads commands as shell_words.py does, but not scripts written to disk or edits of .git files; it stops
shortcuts, not an adversary (see the README for sandbox rules). If it cannot read a command that may touch the
marks, it denies it.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import on_prompt  # noqa: E402
import review_notes  # noqa: E402
import shell_words  # noqa: E402

PUSH_VALUE_OPTIONS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
FETCH_VALUE_OPTIONS = {"--depth", "--deepen", "-j", "--jobs", "--shallow-since", "--shallow-exclude", "--upload-pack",
                       "-o", "--server-option", "--negotiation-tip", "--refmap", "--recurse-submodules-default",
                       "--filter", "-s", "--strategy", "-X", "--strategy-option"}
NOTES_READS = {"", "show", "list", "get-ref"}
READ_COMMANDS = {"log", "show", "rev-parse", "for-each-ref", "show-ref", "cat-file", "ls-tree", "rev-list", "grep",
                 "diff", "ls-remote", "reflog", "describe", "merge-base", "name-rev", "shortlog", "format-patch",
                 "status", "checkout"}
HOOK_SCRIPTS = {"on_prompt.py", "on_reviewer_stop.py"}
LAUNCHERS = {"npx", "bunx", "pnpx", "pnpm", "yarn"}
IMPORT_RE = re.compile(r"\b(?:review_notes|on_prompt|on_reviewer_stop|write_mark)\b")
NOTES_REF_RE = re.compile(r"(?:^|[:+])refs/notes/review(?:$|[:~^@])")
NOTES_SETTING_RE = re.compile(r"^(?:git_notes_ref|git_notes_rewrite_ref|core\.notesref|notes\.rewriteref)=.*"
                              r"(?:review|\*)", re.I)
NOTES_REF_OPTION_RE = re.compile(r"^--r(?:e|ef)?(?:=(.*))?$")  # git accepts --ref abbreviated
REMOTE_DEST_RE = re.compile(r":(?:refs/)?remotes/([^/]*)")
NOTES_DEST_RE = re.compile(r":(?:refs/)?notes/(?:review|\*)$")
NOTES_SOURCE_RE = re.compile(r"^\+?(?:refs/)?notes/(?:review|\*)(?:$|:)")
OVERRIDE_RE = re.compile(r"^(?:url\..*\.(?:push)?insteadof|remote\..*\.(?:url|fetch))=", re.I)
PYTHON_RE = re.compile(r"^python[0-9.]*$")
RISKY_RE = re.compile(r"\b(?:push|notes|update-ref|symbolic-ref|fetch|pull|fast-import|claude)\b|review_notes|"
                      r"write_mark|on_prompt|on_reviewer_stop")
APPROVAL_RE = re.compile(r"\bapproved?\b", re.I)
LEAD_RE = re.compile(r"^\s*(?:/[\w:-]+\s+)?(?:(?:every\s+)?\d+\s*[smhd]\w*\s+)?", re.I)  # "/loop 5m ..."
PROMPT_KEYS = {"prompt", "message", "messages", "text", "content", "body", "args", "input"}


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    return 0


def is_hook_script(directory, path):
    """Whether path is one of the plugin's hook scripts: a copy anywhere, recognised by review_notes.py beside it, or
    a path through a variable such as ${CLAUDE_PLUGIN_ROOT}."""
    if os.path.basename(path) not in HOOK_SCRIPTS:
        return False
    path = os.path.join(directory, os.path.expanduser(path))
    return "$" in path or os.path.isfile(os.path.join(os.path.dirname(path),
                                                                                  "review_notes.py"))


def operands(args, value_options):
    """The arguments that are not options or option values."""
    result, index = [], 0
    while index < len(args):
        if args[index] in value_options:
            index += 1
        elif not args[index].startswith("-"):
            result.append(args[index])
        index += 1
    return result


def remotes(directory):
    return review_notes.git(["remote"], directory).stdout.split() if os.path.isdir(directory) else []


def covers(pattern, namespace):
    """Whether a wildcard ref pattern such as `refs/*` or `refs/r*` reaches refs/<namespace>/."""
    prefix = pattern.lstrip("+").split("*")[0] if "*" in pattern else None
    return prefix is not None and any(name.startswith(prefix) for name in ("refs/%s/" % namespace, namespace + "/"))


def notes_ref_and_action(args):
    ref, action, index = "", "", 0
    while index < len(args):
        match = NOTES_REF_OPTION_RE.match(args[index])
        if match:
            ref = match.group(1) if match.group(1) is not None else args[index + 1] if index + 1 < len(args) else ""
            index += 1 if match.group(1) is not None else 2
            continue
        if not args[index].startswith("-") and not action:
            action = args[index]
        index += 1
    return ref, action


def python_problem(command, directory, words, bodies):
    kind, value = shell_words.python_target(words)
    code = value if kind == "-c" else ("\n".join(bodies) or command) if kind == "-" else ""  # or piped in
    if IMPORT_RE.search(code):
        return "the plugin's mark-writing code runs only from its hooks"
    if kind == "file" and is_hook_script(directory, value) or kind == "-m" and \
            value.rsplit(".", 1)[-1] + ".py" in HOOK_SCRIPTS:
        return "the plugin's hook scripts run only as hooks"
    return None


def git_problem(command, directory, words, settings):
    parts = shell_words.git_parts(words, directory, settings)
    if not parts:
        return None
    directory, sub, args, configs = parts
    setting = "=".join(operands(args, set())[:2]) if sub == "config" else ""  # `git config KEY VALUE`
    named = any(NOTES_REF_RE.search(arg) for arg in args) or \
        any(NOTES_SETTING_RE.match(item) for item in settings + configs + [setting])
    reading = sub in READ_COMMANDS
    if sub == "notes":
        ref, action = notes_ref_and_action(args)
        named = named or ref in ("review", "notes/review", review_notes.NOTES_REF) or \
            bool(re.search(r"\bGIT_NOTES_REF=\S*review", command))  # also when exported earlier in the line
        reading = action in NOTES_READS
    if sub in ("fetch", "pull", "push"):  # only what they write counts; a fetched source only fills FETCH_HEAD
        specs = operands(args, FETCH_VALUE_OPTIONS | PUSH_VALUE_OPTIONS)
        remote, refspecs = (specs[0], specs[1:]) if specs else ("", [])
        refspecs += [value for option, value in zip(args, args[1:]) if option == "--refmap"] + \
            [arg[len("--refmap="):] for arg in args if arg.startswith("--refmap=")]
        if sub != "push" and ("--stdin" in args and re.search(r"notes/|remotes/", command) or
                              any(OVERRIDE_RE.match(item) for item in configs) or
                              any(item.startswith("GIT_CONFIG") for item in settings)):
            return "a fetch uses the remote's configured URL and refspecs, since remote-tracking refs show what " \
                "is pushed"
        targets = [spec.split(":", 1)[1] for spec in refspecs if ":" in spec]
        if any(NOTES_DEST_RE.search(spec) for spec in refspecs) or any(covers(dest, "notes") for dest in targets) or \
                sub == "push" and any(NOTES_SOURCE_RE.match(spec) or covers(spec.split(":")[0], "notes")
                                      for spec in refspecs):
            return "review marks are written only by the review hooks and carry.py"
        moved = [match.group(1) for match in map(REMOTE_DEST_RE.search, refspecs) if match] + \
            ["*" for dest in targets if covers(dest, "remotes")]
        if moved and (sub == "push" or remote not in remotes(directory) or any(name != remote for name in moved)):
            return "remote-tracking refs move only by fetching from their own remote, since they show what is pushed"
        return None
    if named and not reading:
        return "review marks are written only by the review hooks and carry.py"
    if sub in ("update-ref", "fast-import") and ("--stdin" in args or sub == "fast-import") \
            and re.search(r"notes/review|remotes/", command):
        return "review marks and remote-tracking refs are not written through --stdin or fast-import"
    writes = sub == "update-ref" and "-d" not in args or sub == "symbolic-ref" and \
        ("-d" in args or "--delete" in args or len(operands(args, {"-m"})) > 1)
    if writes and any(arg.startswith("refs/remotes/") for arg in args):
        return "remote-tracking refs move only by fetching from their own remote, since they show what is pushed"
    return None


def lock_problem(command, found):
    """Why the command touches the marks or the hooks, or None."""
    for directory, words, settings, bodies, _ in found:
        name = os.path.basename(words[0])
        if PYTHON_RE.search(name):
            reason = python_problem(command, directory, words, bodies)
        elif is_hook_script(directory, words[0]):
            reason = "the plugin's hook scripts run only as hooks"
        elif (name == "claude" or name in LAUNCHERS and any("claude-code" in word for word in words)) \
                and APPROVAL_RE.search(command):
            reason = "only the user can approve commits, so another Claude session cannot be sent an approval"
        else:
            reason = git_problem(command, directory, words, settings)
        if reason:
            return reason
    return None


def prompts(value, every):
    """The strings in a tool input; for MCP tools (every False) only those under prompt-like keys."""
    if isinstance(value, str):
        if every:
            yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from prompts(item, every or key in PROMPT_KEYS)
    elif isinstance(value, list):
        for item in value:
            yield from prompts(item, every)


def main():
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return 0
    if not isinstance(event, dict):
        return 0
    tool, tool_input = event.get("tool_name"), event.get("tool_input")
    if tool not in ("Bash", "Monitor") and any(on_prompt.APPROVED_RE.match(LEAD_RE.sub("", text, count=1)) for text in
                              prompts(tool_input, not str(tool).startswith("mcp__"))):
        return deny("Blocked: only the user can approve commits; a tool cannot send a prompt starting with "
                    "'Approved'.")
    command, cwd = tool_input.get("command") if isinstance(tool_input, dict) else None, event.get("cwd")
    if not isinstance(command, str):
        return 0
    if (tool_input.get("tab_id") or "terminal" in str(tool)) and on_prompt.APPROVED_RE.match(command.strip()):
        return deny("Blocked: only the user can approve commits; a tool cannot type 'Approved' into a terminal.")
    cwd = cwd if isinstance(cwd, str) and os.path.isdir(cwd) else os.getcwd()
    if isinstance(tool_input.get("cwd"), str):  # a terminal tool's own directory
        cwd = os.path.join(cwd, os.path.expanduser(tool_input["cwd"]))
    elif tool_input.get("tab_id"):  # a terminal tab whose directory the gate cannot know
        cwd = os.path.join(cwd, "$(terminal tab directory)")
    try:
        found = shell_words.commands(command, cwd)
        reason = lock_problem(command, found)
        return deny("Blocked: %s." % reason) if reason else 0
    except Exception as error:  # fail closed for the commands this hook guards, open for the rest
        if not RISKY_RE.search(command):
            return 0
        return deny("Blocked: the review gate could not check this command (%s: %s); split it into simpler "
                    "commands, or ask the user to run it." % (type(error).__name__, error))


if __name__ == "__main__":
    sys.exit(main())
