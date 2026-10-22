#!/usr/bin/env python3
"""PreToolUse hook: keep unreviewed commits off the remote and keep the agent away from review marks.

On Bash, and on any tool whose input has a shell `command`, it denies a command that
- runs `git push` (or `gh pr create` or `gh repo create --push`, which may push) while a commit the push would send
  lacks a clean mark. Commits on the target remote or on a network remote count as pushed, and merges git made
  without conflicts need no mark. It first carries marks whose change is unchanged (carry.py), so a rebase under
  reviewed commits needs no new review. It reads only a plain push (after `cd`, `-C` or VAR= settings at most); one
  inside `$(...)`, `bash -c`, eval or an alias, or behind a wrapper, is denied, and so is a push after a command
  that may change commits in the same line, since the gate sees the repository as it was before the line ran;
- opens or merges a pull request (`gh pr create`, `gh pr merge`, run plainly from the pull request's branch) while
  the branch tip lacks a whole-branch review mark whose base is on the pull request's base branch;
- writes refs/notes/review, moves a remote-tracking ref, runs the hook scripts or imports their mark-writing code,
  which leaves carry.py as the only mark writer the agent can run;
- starts another Claude session with a prompt mentioning approval.
On the tools that send prompts later or elsewhere (scheduled prompts, loops, messages to sessions) it denies a
prompt that starts with "Approved", since only the user may approve a commit.

It reads commands as shell_words.py does, but not scripts written to disk or edits of .git files; it stops
shortcuts, not an adversary (see the README for sandbox rules). If it cannot read a command that may push or touch
the marks, it denies it.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import carry  # noqa: E402
import on_prompt  # noqa: E402
import review_notes  # noqa: E402
import shell_words  # noqa: E402

CREATE_VALUE_OPTIONS = {"-B", "--base", "-H", "--head", "-t", "--title", "-b", "--body", "-F", "--body-file", "-a",
                        "--assignee", "-l", "--label", "-m", "--milestone", "-p", "--project", "-r", "--reviewer",
                        "-T", "--template", "--recover"}
MERGE_VALUE_OPTIONS = {"-t", "--subject", "-b", "--body", "-F", "--body-file", "-A", "--author-email",
                       "--match-head-commit"}
GH_ENV_RE = re.compile(r"\bGH_(?:REPO|HOST)=")
PUSH_VALUE_OPTIONS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
FETCH_VALUE_OPTIONS = {"--depth", "--deepen", "-j", "--jobs", "--shallow-since", "--shallow-exclude", "--upload-pack",
                       "-o", "--server-option", "--negotiation-tip", "--refmap", "--recurse-submodules-default",
                       "--filter", "-s", "--strategy", "-X", "--strategy-option"}
NOTES_READS = {"", "show", "list", "get-ref"}
READ_COMMANDS = {"log", "show", "rev-parse", "for-each-ref", "show-ref", "cat-file", "ls-tree", "rev-list", "grep",
                 "diff", "ls-remote", "reflog", "describe", "merge-base", "name-rev", "shortlog", "format-patch",
                 "status", "checkout"}
HARMLESS = {"cd", "pushd", "popd", "echo", "printf", "true", ":", "pwd", "ls", "cat", "head", "tail", "grep", "wc",
            "sleep", "date", "which", "test", "[", "[[", "set", "read"}  # may run before a push in the same line
READ_FORMS = {"branch": ("-v", "-vv", "--verbose", "--merged", "--no-merged", "--contains", "--no-contains",
                         "--points-at"), "tag": ("-l", "--list"), "remote": ("-v", "get-url", "show"),
              "stash": ("list", "show"), "worktree": ("list",),
              "config": ("--get", "--get-all", "--get-regexp", "--list", "-l")}  # or no operand
STILL_COMMANDS = {  # git subcommands that move no commit, branch or HEAD
    "status", "log", "show", "diff", "rev-parse", "ls-remote", "fetch", "describe", "shortlog", "rev-list", "cat-file",
    "ls-files", "ls-tree", "grep", "blame", "show-ref", "for-each-ref", "merge-base", "name-rev", "version", "push"}
HOOK_SCRIPTS = {"on_prompt.py", "on_reviewer_stop.py"}
LAUNCHERS = {"npx", "bunx", "pnpx", "pnpm", "yarn"}
IMPORT_RE = re.compile(r"\b(?:review_notes|on_prompt|on_reviewer_stop|write_mark)\b")
NOTES_REF_RE = re.compile(r"(?:^|[:+])refs/notes/review(?:$|[:~^@])")
ASSIGNMENT_RE = re.compile(r"(?:^|[;&|\n(])\s*(\w+=\S*)")
LINE_NOTES_RE = re.compile(r"\b(?:GIT_NOTES_REWRITE_REF=\S*(?:review|\*)|GIT_CONFIG_\w+=\S*notes)", re.I)
NOTES_SETTING_RE = re.compile(r"^(?:git_notes_ref|git_notes_rewrite_ref|core\.notesref|notes\.rewriteref)=.*"
                              r"(?:review|\*)", re.I)
NOTES_REF_OPTION_RE = re.compile(r"^--r(?:e|ef)?(?:=(.*))?$")  # git accepts --ref abbreviated
REMOTE_DEST_RE = re.compile(r":(?:refs/)?remotes/([^/]*)")
NOTES_DEST_RE = re.compile(r":(?:refs/)?notes/(?:review|\*)$")
NOTES_SOURCE_RE = re.compile(r"^\+?(?:refs/)?notes/(?:review|\*)(?:$|:)")
OVERRIDE_RE = re.compile(r"^(?:url\..*\.(?:push)?insteadof|remote\..*\.(?:url|fetch))=", re.I)
PYTHON_RE = re.compile(r"^python[0-9.]*$")
RISKY_RE = re.compile(r"\b(?:push|notes|update-ref|symbolic-ref|fetch|pull|fast-import|claude|pr)\b|review_notes|"
                      r"write_mark|on_prompt|on_reviewer_stop")
APPROVAL_RE = re.compile(r"\bapproved?\b", re.I)
SHORT_DRY_RE = re.compile(r"^-[46fquv]*[nd][46dfnquv]*$")  # -n or -d among push's short options
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


def git_problem(command, directory, words, settings, assigned):
    parts = shell_words.git_parts(words, directory, settings)
    if not parts:
        return None
    directory, sub, args, configs = parts
    keys = operands(args, set()) if sub == "config" else []
    setting = "=".join(keys[1:3] if keys[:1] == ["set"] else keys[:2])  # `git config [set] KEY VALUE`
    named = any(NOTES_REF_RE.search(arg) for arg in args) or \
        any(NOTES_SETTING_RE.match(item) for item in settings + configs + [setting]) or \
        bool(LINE_NOTES_RE.search(assigned))  # also when exported earlier in the line
    reading = sub in READ_COMMANDS
    if sub == "notes":
        ref, action = notes_ref_and_action(args)
        named = named or ref in ("review", "notes/review", review_notes.NOTES_REF) or \
            bool(re.search(r"\bGIT_NOTES_REF=\S*review", assigned))  # also when exported earlier in the line
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
    assigned = " ".join([word for _, words, settings, *_ in found for word in settings + (
        words[1:] if words[0] in ("export", "declare", "typeset") or words[:2] == ["builtin", "export"] else [])] +
        ASSIGNMENT_RE.findall(command))  # VAR=value words, also of a line that only assigns
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
            reason = git_problem(command, directory, words, settings, assigned)
        if reason:
            return reason
    return None


def config(directory, configs, key, every=False):
    """A git config value (every value, as a list, with every) with the command's -c settings applied."""
    options = [item for setting in configs if not setting.lower().startswith(("include.", "includeif."))
               for item in ("-c", setting)]  # an included file could be a FIFO that never closes
    result = review_notes.git([*options, "config", "--get-all" if every else "--get", key], directory)
    return result.stdout.splitlines() if every else result.stdout.strip()


def sources(specs):
    """The revisions push refspecs send: `tag X` as refs/tags/X, wildcards as --glob, `:` (matching branches) as
    --branches, none for deletes; ^X excludes X from the wildcards."""
    result, excludes, index = [], [], 0
    while index < len(specs):
        if specs[index] == "tag" and index + 1 < len(specs):
            result.append("refs/tags/" + specs[index + 1])
            index += 2
            continue
        spec = specs[index].lstrip("+")
        source = spec.split(":")[0]
        if spec == ":":
            result.append("--branches")
        elif source.startswith("^"):
            excludes.append("--exclude=" + source[1:])
        elif source:
            result.append("--glob=" + source if "*" in source else source)
        index += 1
    return [part for item in result for part in (excludes + [item] if item.startswith("--") else [item])]


MIRROR = ["--exclude=refs/notes/*", "--exclude=refs/stash", "--all"]  # the marks are not commits to review


def push_target(args, directory, configs):
    """(remote, revisions the push sends) from git push's arguments; revisions None if nothing is sent."""
    positional, remote, revisions, index = [], None, [], 0
    while index < len(args):
        arg = args[index]
        if arg in ("--delete", "--dry-run") or SHORT_DRY_RE.match(arg):
            return None, None
        if arg in ("--all", "--branches"):
            revisions.append("--branches")
        elif arg == "--mirror":
            revisions += MIRROR
        elif arg == "--tags":
            revisions.append("--tags")
        elif arg.startswith("--repo="):
            remote = arg[len("--repo="):]
        elif arg in PUSH_VALUE_OPTIONS:
            remote = args[index + 1] if arg == "--repo" and index + 1 < len(args) else remote
            index += 1
        elif not arg.startswith("-"):
            positional.append(arg)
        index += 1
    remote, specs = positional[0] if positional else remote, positional[1:]
    revisions += sources(specs)
    if remote is None:
        branch = review_notes.git(["branch", "--show-current"], directory).stdout.strip()
        for key in ("branch.%s.pushRemote" % branch, "remote.pushDefault", "branch.%s.remote" % branch):
            remote = config(directory, configs, key) or None
            if remote:
                break
    remote = remote or "origin"
    if remote == ".":  # updates local branches only
        return None, None
    if not specs and not revisions:  # what a bare push sends depends on the remote's and push's settings
        pushes = config(directory, configs, "remote.%s.push" % remote, every=True)
        mirror = config(directory, configs, "remote.%s.mirror" % remote).lower() in ("true", "yes", "on", "1")
        revisions = MIRROR if mirror else \
            sources(pushes) if pushes else \
            ["--branches"] if config(directory, configs, "push.default") == "matching" else ["HEAD"]
    return remote, revisions or None


def is_local(url):
    """Whether a remote URL is a path on this machine, which the agent can fill without pushing."""
    return url.startswith("file://") or "://" not in url and not re.match(r"^[^/]+:", url)


def same_url(url, other, directory):
    paths = [os.path.realpath(os.path.join(directory, item[len("file://"):] if item.startswith("file://") else item))
             if is_local(item) else item.rstrip("/") for item in (url, other)]
    return paths[0] == paths[1]


def gh_args(words):
    """A gh command's arguments without -R/--repo, and whether it had one."""
    args, index = [], 1
    while index < len(words):
        if words[index] in ("-R", "--repo"):
            index += 1
        elif not words[index].startswith(("-R", "--repo=")):
            args.append(words[index])
        index += 1
    return args, len(args) < len(words) - 1


def gh_push(words, directory):
    """git_parts of the push a gh command makes: `gh pr create` pushes an unpushed branch unless given --head, and
    `gh repo create --push` pushes its --source; None for other gh commands."""
    args = gh_args(words)[0]
    if args[:2] in (["pr", "create"], ["pr", "new"]) and \
            not any(word.startswith(("-H", "--head")) for word in args):
        return directory, "push", [], []
    if args[:2] == ["repo", "create"] and any(word == "--push" or word == "--push=true" for word in args):
        source = next((value for option, value in zip(args, args[1:] + ["."]) if option in ("-s", "--source")),
                      next((word[len("--source="):] for word in args if word.startswith("--source=")), "."))
        return os.path.join(directory, os.path.expanduser(source)), "push", [], []
    return None


def clean_merge(sha, directory):
    """Whether sha is a merge git made without conflicts or edits: its tree is the one merge-tree builds."""
    parents = review_notes.git(["rev-parse", sha + "^@"], directory).stdout.split()
    if len(parents) != 2:
        return False
    merged = review_notes.git(["merge-tree", "--write-tree", *parents], directory)  # git 2.38+
    tree = review_notes.git(["rev-parse", sha + "^{tree}"], directory).stdout.strip()
    return merged.returncode == 0 and merged.stdout.split()[:1] == [tree]


def pushes(words, directory, settings):
    """git_parts of the push the words run (git push, send-pack or subtree push, or a gh command that pushes)."""
    if os.path.basename(words[0]) == "gh":
        return gh_push(words, directory)
    parts = shell_words.git_parts(words, directory, settings)
    return parts if parts and (parts[1] in ("push", "send-pack") or parts[1] == "subtree" and "push" in parts[2]) \
        else None


def still(directory, words, settings):
    """Whether a command run before a push in the same line surely leaves commits, branches and HEAD alone."""
    parts = shell_words.git_parts(words, directory, settings) or pushes(words, directory, settings)
    if not parts:
        name = os.path.basename(words[0])
        return name in HARMLESS or name == "export" and \
            all(re.match(r"^(?!GIT_)|GIT_TRACE", word) for word in words[1:])  # GIT_DIR and such move the push
    sub, args = parts[1], parts[2]
    return sub in STILL_COMMANDS or sub in READ_FORMS and \
        (not operands(args, ()) or any(arg.split("=")[0] in READ_FORMS[sub] for arg in args))


def push_problem(found):
    """Why a push in the command would send unreviewed commits, or None. Only a plain `git push` (or gh command)
    is read; one inside a substitution, shell script, eval or alias, or behind a wrapper, is denied."""
    for number, (directory, words, settings, _, plain) in enumerate(found):
        parts = pushes(words, directory, settings)
        if not parts and not any(pushes(words[index:], directory, []) for index in range(1, len(words))):
            continue
        if not parts or not plain or parts[1] != "push":
            return "run `git push` (or `gh pr create`) as a plain command, not through `$(...)`, `bash -c`, eval, " \
                "an alias or a wrapper such as env or sudo, so the review gate can check it"
        directory, _, args, configs = parts
        if "$" in directory or "`" in directory or not os.path.isdir(directory):
            return "could not tell which repository `%s` runs in; cd to it or name it with -C" % " ".join(words)
        if not review_notes.is_repo(directory):
            return "could not read the repository `%s` runs in" % " ".join(words)
        if any("$" in arg or "`" in arg for arg in args):
            return "could not tell what `%s` sends; name the remote and branches literally" % " ".join(words)
        if not all(still(*item[:3]) for item in found[:number]):
            return "the gate checks a push against the repository as it is before the command runs; run the push " \
                "as its own command, after the commands that change commits or branches"
        remote, revisions = push_target(args, directory, configs)
        if revisions is None:
            continue
        names = remotes(directory)
        urls = {name: config(directory, configs, "remote.%s.url" % name, every=True) for name in names}
        if remote not in names:  # a URL: the configured remote it names, if any
            remote = next((name for name in names for url in urls[name] + config(
                directory, configs, "remote.%s.pushurl" % name, every=True) if same_url(remote, url, directory)), None)
        # Commits on the target or on a network remote got there by a push; a local path can be filled directly.
        trusted = {name for name in names if urls[name] and not any(is_local(url) for url in urls[name])}
        counted = sorted(trusted | ({remote} if remote else set()))
        pushed = ["--remotes=%s/*" % name for name in counted]
        listed = review_notes.git(["rev-list", *revisions, "--not", *pushed, "--"], directory)
        if listed.returncode != 0:
            return "could not tell what `%s` sends (%s); push named branches" % (" ".join(words),
                                                                                 listed.stderr.strip())
        marks = review_notes.clean_marks(directory)
        missing = [sha for sha in listed.stdout.split() if sha not in marks]
        if missing and carry.carry_unchanged(directory, missing)[0]:
            marks = review_notes.clean_marks(directory)
            missing = [sha for sha in missing if sha not in marks]
        missing = [sha for sha in missing if not clean_merge(sha, directory)]
        if missing:
            prefixes = ["refs/remotes/%s/" % name for name in counted]
            fetched = prefixes and review_notes.git(["for-each-ref", "--count=1", *prefixes], directory).stdout
            hint = "" if fetched else \
                " (no remote it trusts has fetched branches, so all history counts; fetch first, or push this yourself)"
            return ("%d commit(s) lack a clean review mark%s: %s. Review each with the commit-reviewer agent (or, "
                    "for a tiny commit, ask the user to reply 'Approved'; after nit fixes, run carry.py OLD) as the "
                    "commit workflow says" % (len(missing), hint,
                                                    ", ".join(sha[:12] for sha in missing[:20])))
    return None


def gh_pr(words):
    """("create" or "merge", the arguments after it, whether -R/--repo is given) for a gh command that opens or
    merges a pull request, else None."""
    if not words or os.path.basename(words[0]) != "gh":
        return None
    args, repo = gh_args(words)
    if args[:2] in (["pr", "create"], ["pr", "new"], ["pr", "merge"]):
        return "merge" if args[1] == "merge" else "create", args[2:], repo
    return None


def gh_options(args, value_options):
    """({"-B": values, "-H": values}, operands) as gh reads args, or None for a short cluster that may hide -B or -H."""
    given, rest, index = {"-B": [], "-H": []}, [], 0
    while index < len(args):
        arg = args[index]
        name, equals, value = arg.partition("=") if arg.startswith("--") else (arg[:2], "", arg[2:])
        if not arg.startswith("-") or arg == "-":
            rest.append(arg)
        elif name in value_options:
            if not equals and not value:
                value, index = (args[index + 1] if index + 1 < len(args) else ""), index + 1
            given.setdefault({"--base": "-B", "--head": "-H"}.get(name, name), []).append(value)
        elif not arg.startswith("--") and set(arg[2:]) & {"B", "H"}:
            return None
        index += 1
    return given, rest


def pr_problem(command, found):
    """Why a pull request the command opens or merges lacks a whole-branch review, or None. Only a plain gh command,
    run from the pull request's branch, is read."""
    for number, (directory, words, settings, _, plain) in enumerate(found):
        action = gh_pr(words)
        if not action and not any(gh_pr(words[index:]) for index in range(1, len(words))):
            continue
        if not action or not plain or action[2] or GH_ENV_RE.search(command) or \
                os.environ.get("GH_REPO") or os.environ.get("GH_HOST"):
            return "run `gh pr create` or `gh pr merge` as a plain command in the branch's checkout, without -R, " \
                "GH_REPO or GH_HOST, and not through `$(...)`, `bash -c`, eval, an alias or a wrapper, so the " \
                "review gate can check it"
        kind, args, _ = action
        if "$" in directory or "`" in directory or not os.path.isdir(directory) or \
                not review_notes.is_repo(directory) or any("$" in arg or "`" in arg for arg in args):
            return "could not tell which branch `%s` is for; cd to its checkout and give values literally" % \
                " ".join(words)
        if not all(still(*item[:3]) for item in found[:number]):
            return "the gate checks a pull request against the repository as it is before the command runs; run " \
                "it as its own command"
        head = review_notes.resolve("HEAD", directory)
        branch = review_notes.git(["symbolic-ref", "--quiet", "--short", "HEAD"], directory).stdout.strip()
        if head is None:
            return "the branch has no commits yet"
        parsed = gh_options(args, MERGE_VALUE_OPTIONS if kind == "merge" else CREATE_VALUE_OPTIONS)
        if parsed is None or len(parsed[0]["-B"]) > 1 or len(parsed[0]["-H"]) > 1:
            return "give --base and --head at most once each, each as its own option"
        given, rest = parsed
        if kind == "merge" and rest:
            return "run `gh pr merge` without a pull request argument, from the pull request's branch"
        upstream = review_notes.resolve("@{upstream}", directory)
        if (kind == "merge" or upstream) and upstream != head:
            return "HEAD is not what its upstream branch holds; push it (and fetch, if it may have changed " \
                "elsewhere), or check out the pull request's branch"
        if kind == "create" and (given["-H"] or [branch])[0] != branch:
            return "run `gh pr create` from the pull request's branch, without --head naming another branch or a fork"
        names = remotes(directory)
        chosen = [name for name in names if config(directory, [], "remote.%s.gh-resolved" % name) == "base"]
        remote = chosen[0] if len(chosen) == 1 else names[0] if len(names) == 1 and not chosen else None
        if remote is None:
            return "could not tell which repository the pull request is for; run `gh repo set-default`"
        base_name = (given["-B"] or [config(directory, [], "branch.%s.gh-merge-base" % branch)])[0]
        target = "refs/remotes/%s/%s" % (remote, base_name) if base_name else review_notes.git(
            ["symbolic-ref", "--quiet", "refs/remotes/%s/HEAD" % remote], directory).stdout.strip()
        if not target or review_notes.resolve(target, directory) is None:
            return "could not tell the pull request's base branch; fetch it, run `git remote set-head %s --auto`, " \
                "or name it with --base" % remote
        base = review_notes.branch_base(head, directory)
        if base is None or not review_notes.is_ancestor(base, target, directory):
            start = review_notes.git(["merge-base", target, head], directory).stdout.strip()
            if not start:
                return "the branch shares no history with %s" % target[len("refs/remotes/"):]
            return "the branch tip %s lacks a whole-branch review from %s. Have the commit-reviewer agent review " \
                "the branch with `show_change.py %s --base %s` and end with 'VERDICT: branch-clean %s'" % (
                    head[:12], target[len("refs/remotes/"):], head[:12], start[:12], head[:12])
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
        if reason:
            return deny("Blocked: %s." % reason)
        reason = push_problem(found)
        if reason:
            return deny("Push blocked: %s." % reason)
        reason = pr_problem(command, found)
        return deny("Pull request blocked: %s." % reason) if reason else 0
    except Exception as error:  # fail closed for the commands this hook guards, open for the rest
        if not RISKY_RE.search(command):
            return 0
        return deny("Blocked: the review gate could not check this command (%s: %s); split it into simpler "
                    "commands, or ask the user to run it." % (type(error).__name__, error))


if __name__ == "__main__":
    sys.exit(main())
