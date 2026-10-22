"""Read a shell command line as the simple commands it runs, for the gate hook.

It looks through what an agent ordinarily writes: `cd`/`pushd`/`popd` and `( ... )` scopes, separators and pipes,
`if`/`for` bodies, `bash -lc '...'`, `eval`, `$(...)`, backticks and `<(...)` (outside single quotes, nested),
comments, line continuations, here-documents, wrappers such as `env` and `timeout`, git aliases and the scripts of
`git submodule foreach` and `git rebase -x`. It does not follow variables, functions or scripts on disk, and raises
ValueError for what it cannot read: a shell reading commands from a pipe, or nesting deeper than MAX_DEPTH.
"""
import functools
import os
import re
import shlex

import review_notes
import shell_scan

KEYWORDS = {"{", "}", "!", "if", "then", "elif", "else", "do", "while", "until"}
TIMEOUT_OPTIONS = {"-s", "--signal", "-k", "--kill-after"}
WRAPPERS = {  # wrapper: options taking a value
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}, "command": set(), "exec": {"-a"},
    "nohup": set(), "time": {"-f", "--format", "-o", "--output"}, "chronic": set(),
    "sudo": {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from", "-D", "--chdir",
             "-U", "--other-user", "-r", "--role", "-t", "--type", "-T", "--command-timeout"},
    "doas": {"-u", "-C"}, "xargs": {"-I", "-n", "-L", "-P", "-d", "-E", "-s", "-a", "--arg-file", "--delimiter"},
    "nice": {"-n", "--adjustment"}, "ionice": {"-c", "--class", "-n", "--classdata"}, "timeout": TIMEOUT_OPTIONS,
    "gtimeout": TIMEOUT_OPTIONS, "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "caffeinate": {"-t", "-w"}}
RUNNERS = {  # `uv run CMD`: options (before and after `run`) taking a value
    "uv": {"--with", "-w", "--with-editable", "--with-requirements", "--python", "-p", "--project", "--directory",
           "--package", "--extra", "--group", "--only-group", "--no-group", "--env-file", "--index", "--default-index",
           "--index-url", "-i", "--extra-index-url", "--find-links", "-f", "--config-file", "--cache-dir", "--color"},
    "poetry": {"-C", "--directory", "-P", "--project"}, "pipenv": set()}
DIRECTORY_OPTIONS = {("env", "-C"), ("env", "--chdir"), ("sudo", "-D"), ("sudo", "--chdir"), ("uv", "--directory"),
                     ("poetry", "-C"), ("poetry", "--directory")}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix"}
GIT_BUILTINS = {  # git ignores aliases with these names (a missing one only costs an alias lookup)
    "add", "am", "apply", "archive", "bisect", "blame", "branch", "bundle", "cat-file", "checkout", "cherry",
    "cherry-pick", "clean", "clone", "commit", "config", "describe", "diff", "fetch", "for-each-ref",
    "format-patch", "fsck", "gc", "grep", "help", "init", "log", "ls-files", "ls-remote", "ls-tree", "merge",
    "merge-base", "mv", "notes", "pull", "push", "range-diff", "rebase", "reflog", "remote", "replace", "reset",
    "restore", "rev-list", "rev-parse", "revert", "rm", "shortlog", "show", "show-ref", "sparse-checkout", "stash",
    "status", "submodule", "switch", "symbolic-ref", "tag", "update-ref", "version", "worktree"}
PYTHON_VALUE_OPTIONS = {"-W", "-X", "--check-hash-based-pycs"}
SHELL_C_RE = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
MAX_DEPTH = 4
KNOWN = {"$(git branch --show-current)": "HEAD", "$(git rev-parse --abbrev-ref HEAD)": "HEAD", "$(pwd)": ".",
         "$PWD": ".", "${PWD}": "."}  # substitutions agents often use, as the gate can read them


def git_parts(words, cwd, settings=()):
    """(directory, subcommand, args, -c settings) of a git command, or None if not git."""
    if not words or os.path.basename(words[0]) != "git":
        return None
    index, work, git_dir, configs = 1, cwd, None, []
    for setting in settings:
        if setting.startswith("GIT_DIR="):
            git_dir = setting[len("GIT_DIR="):]
    while index < len(words) and words[index].startswith("-"):
        option, value = words[index], words[index + 1] if index + 1 < len(words) else ""
        if option == "-C":
            work = os.path.join(work, os.path.expanduser(value))
        elif option == "--git-dir" or option.startswith("--git-dir="):
            git_dir = value if option == "--git-dir" else option[len("--git-dir="):]
        elif option == "-c":
            configs.append(value)
        elif option == "--config-env" or option.startswith("--config-env="):  # KEY=ENVVAR: a value the gate cannot see
            key, _, variable = (value if option == "--config-env" else option[len("--config-env="):]).rpartition("=")
            configs.append("%s=$%s" % (key, variable))
        index += 2 if option in GIT_VALUE_OPTIONS else 1
    if index >= len(words):
        return None
    directory = os.path.join(work, os.path.expanduser(git_dir)) if git_dir else work  # git applies -C first
    return directory, words[index], words[index + 1:], configs


@functools.lru_cache(maxsize=None)
def _alias(directory, configs, sub):
    options = [item for config in configs if not config.lower().startswith(("include.", "includeif."))
               for item in ("-c", config)]  # an included file could be a FIFO that never closes
    return review_notes.git([*options, "config", "--get", "alias." + sub], directory).stdout.strip()


def alias_command(words, settings, cwd):
    """The command line a git alias in words stands for, or None. (The outer words keep its -c and VAR= settings.)"""
    parts = git_parts(words, cwd, settings)
    if not parts or parts[1] in GIT_BUILTINS:
        return None
    directory, sub, args, configs = parts
    lookup = directory
    while not os.path.isdir(lookup) and os.path.dirname(lookup) != lookup:
        lookup = os.path.dirname(lookup)  # for a directory the gate cannot know, the nearest it can
    alias = _alias(lookup, tuple(configs), sub) if os.path.isdir(lookup) else ""
    if not alias:
        return None
    if alias.startswith("!"):  # git runs it with sh from the top of the work tree, adding the arguments
        top = os.path.isdir(directory) and review_notes.git(["rev-parse", "--show-toplevel"], directory).stdout.strip()
        top = top or directory
        return "cd %s && %s %s" % (shlex.quote(top), alias[1:], shlex.join(args))
    head = words[:len(words) - len(args) - 1]
    return shlex.join(settings + head + shlex.split(alias) + args)


def python_target(words):
    """What a python command runs: ("-c", code), ("-m", module), ("file", path), ("-", "") for stdin, or ("", "")
    for --version or --help."""
    index = 1
    while index < len(words):
        word = words[index]
        if word in ("-V", "--version", "-h", "--help"):
            return "", ""
        if word in ("-c", "-m"):
            return word, words[index + 1] if index + 1 < len(words) else ""
        if word[:2] in ("-c", "-m"):  # `-mmodule`
            return word[:2], word[2:]
        if word == "-" or not word.startswith("-"):
            return ("-", "") if word == "-" else ("file", word)
        index += 2 if word in PYTHON_VALUE_OPTIONS else 1
    return "-", ""


def known(word, cwd):
    """The word with a common substitution, whole or at the start of a path, replaced by its value."""
    head, slash, rest = word.partition("/")
    if head == "$(git rev-parse --show-toplevel)" and os.path.isdir(cwd):
        top = review_notes.git(["rev-parse", "--show-toplevel"], cwd).stdout.strip()
        return top + slash + rest if top else word
    if head in KNOWN and (not slash or KNOWN[head] == "."):
        return KNOWN[head] + slash + rest
    return word


def options(name, words, here):
    """The words after a wrapper's or runner's options and their values, and the directory those set."""
    values, words = WRAPPERS.get(name) or RUNNERS.get(name) or set(), list(words)
    while words and words[0] != "--" and (words[0].startswith("-") or
                                          name in WRAPPERS and re.match(r"^[0-9.]+[smhd]?$", words[0])):
        word = words.pop(0)
        if word.startswith("--") and "=" in word:
            option, value = word.split("=", 1)
        elif word in values:
            option, value = word, words.pop(0) if words else ""
        elif word[:2] in values:  # `-C/dir`
            option, value = word[:2], word[2:]
        else:
            option, value = word, ""
        if (name, option) in DIRECTORY_OPTIONS and value:
            here = os.path.join(here, os.path.expanduser(value))
    return words[1:] if words[:1] == ["--"] else words, here


def git_scripts(parts):
    """(directory, script) for each command line a git command runs itself: `submodule foreach` and `rebase -x`."""
    directory, sub, args, _ = parts
    if sub == "submodule" and "foreach" in args:
        script = [word for word in args[args.index("foreach") + 1:] if word not in ("-q", "--quiet", "--recursive")]
        return [(os.path.join(directory, "$(each submodule)"), " ".join(script))]
    if sub == "rebase":
        return [(directory, value) for option, value in zip(args, args[1:] + [""]) if option in ("-x", "--exec")] + \
            [(directory, option[len("--exec="):]) for option in args if option.startswith("--exec=")]
    return []


def commands(command, cwd, depth=0):
    """(directory, words, settings, bodies, plain) for each simple command the line runs; settings are the VAR=value
    words before it (words is empty for a command that only assigns), bodies its here-document bodies, and plain is
    False when it runs inside a substitution, shell script, eval or git alias, or behind a wrapper. ValueError if the
    line cannot be read."""
    return _walk(shell_scan.parse(command), cwd, depth)


def _nested(depth):
    if depth >= MAX_DEPTH:
        raise ValueError("commands nested more than %d deep" % MAX_DEPTH)
    return depth + 1


def _shell_scripts(name, words, bodies):
    """The command lines a shell or eval runs; ValueError when they come from a pipe."""
    if name == "eval":
        return [" ".join(words[1:])]
    flag = next((number for number, word in enumerate(words) if number and SHELL_C_RE.match(word)), None)
    if flag is not None:
        return words[flag + 1:flag + 2]
    flags = [word for word in words[1:] if word.startswith("-")]
    if len(flags) < len(words) - 1 and not any(re.match(r"^-[A-Za-z]*s", word) for word in flags):
        return []  # a script on disk
    if not bodies and not any(word.startswith("--") for word in flags):  # `--version` reads nothing
        raise ValueError("a shell reading commands from a pipe")
    return bodies  # `bash <<EOF` and `bash -s x <<EOF` run the body


def _walk(nodes, cwd, depth):
    found, scopes, pushed, previous = [], [], [], os.path.join(cwd, "$OLDPWD")
    for node in nodes:
        if node == "(":
            scopes.append((cwd, previous, list(pushed)))
            continue
        if node == ")":
            cwd, previous, pushed = scopes.pop() if scopes else (cwd, previous, pushed)
            continue
        kind, words, subs, bodies = node
        for sub in subs:
            found.extend(_walk(sub, cwd, _nested(depth)))
        settings, here, words, wrapped = [], cwd, [known(word, cwd) for word in words], False
        while words:
            name = os.path.basename(words[0])
            if re.match(r"^\w+=", words[0]):
                settings.append(words[0])
                words = words[1:]
            elif words[0] in KEYWORDS:
                words = words[1:]
            elif name in RUNNERS:
                rest, there = options(name, words[1:], here)
                if rest[:1] != ["run"]:
                    break
                words, here, wrapped = *options(name, rest[1:], there), True
            elif name in WRAPPERS:
                words, here, wrapped = *options(name, words[1:], here), True
            else:
                break
        if not words:
            if settings:
                found.append((here, [], settings, bodies, depth == 0 and not wrapped))
            continue
        name = os.path.basename(words[0])
        if name in ("cd", "pushd", "popd"):
            targets = [word for word in words[1:] if word not in ("-P", "-L", "-q", "--")]
            if kind == "pipe":  # a subshell in bash, but the current shell at the end of a zsh pipeline
                cwd = os.path.join(cwd, "$(%s in a pipeline)" % name)
            elif name == "cd" and targets in ([], ["-"]):
                cwd, previous = previous if targets else os.path.expanduser("~"), cwd
            elif name == "popd" and not targets:
                cwd, previous = pushed.pop() if pushed else cwd, cwd
            elif name != "popd" and len(targets) == 1 and not targets[0].startswith(("-", "+")):
                pushed.append(cwd) if name == "pushd" else None
                cwd, previous = os.path.join(cwd, os.path.expanduser(targets[0])), cwd
            else:  # a stack swap, +N/-N, pushd -n or zsh's `cd OLD NEW`: a directory the gate does not follow
                cwd = os.path.join(cwd, "$(%s)" % " ".join(words))
        elif name in SHELLS or name == "eval":
            for script in _shell_scripts(name, words, bodies):
                found.extend(commands(script, here, _nested(depth)))
        elif name == "git":
            expanded, parts = alias_command(words, settings, here), git_parts(words, here, settings)
            scripts = ([(here, expanded)] if expanded else []) + (git_scripts(parts) if parts else [])
            for directory, script in scripts:
                found.extend(commands(script, directory, _nested(depth)))
        found.append((here, words, settings, bodies, depth == 0 and not wrapped))
    return found
