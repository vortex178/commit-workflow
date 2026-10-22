"""Split shell text into simple commands, the way bash reads quotes, substitutions and here-documents.

parse() returns nodes: ("cmd", words, substitutions, bodies) for each simple command, and "(" and ")" around a
subshell. Words have quotes removed and redirections dropped; a word holding `$(...)`, backticks or `$((...))` keeps
its raw text. Substitutions are the node lists of the `$(...)`, backtick and `<(...)` commands in the command (not
those inside single quotes or quoted here-documents), and bodies its here-document bodies.
"""
import re

OPERATORS = ("&&", "||", ";;", "|&", ";", "&", "|", "\n")
REDIRECTIONS = (">>", ">&", "<&", "<>", ">|", "<<<", "&>>", "&>", ">", "<")
WORD_END = set(" \t\r\n;&|<>()")


class Scanner:
    """Splits shell text into nodes: ("cmd", words, substitutions, here-document bodies), "(" and ")".

    A substitution is the node list of a `$(...)`, backtick or `<(...)` command; its word keeps the raw text."""

    def __init__(self, text):
        self.text, self.index = text, 0

    def char(self, offset=0):
        return self.text[self.index + offset:self.index + offset + 1]

    def parse(self, stop=False):
        """Nodes up to the end of the text or, with stop, up to the `)` closing a substitution."""
        nodes, words, subs, bodies, word, skip, pending, opened = [], [], [], [], None, False, [], 0

        def add(part):
            nonlocal word
            word = word or []
            word.append(part)

        def end_word():
            nonlocal word, skip
            if word is not None and not skip:
                words.append("".join(word))
            elif word is not None:
                skip = False
            word = None

        def end_command():
            nonlocal words, subs, bodies, skip
            end_word()
            if words or subs or any(entry[3] is bodies for entry in pending):
                nodes.append(("cmd", words, subs, bodies))
            words, subs, bodies, skip = [], [], [], False

        text = self.text
        while self.index < len(text):
            char, start = self.char(), self.index
            if char in " \t\r":
                end_word()
                self.index += 1
            elif char == "\\":
                if self.char(1) != "\n":  # a backslash-newline joins the lines
                    add(self.char(1))
                self.index += 2
            elif char == "'" or text.startswith("$'", start):
                end = start + 1 + (char == "$")
                while end < len(text) and text[end] != "'":
                    end += 2 if char == "$" and text[end] == "\\" else 1  # `$'it\'s'`
                if end >= len(text):
                    raise ValueError("unbalanced single quote")
                add(text[start + 1 + (char == "$"):end])
                self.index = end + 1
            elif char == '"':
                self.index += 1
                add(self.double_quoted(subs, closing='"'))
            elif char in "$`" and (char == "`" or self.char(1) in "({"):
                add(self.substitution(subs))
            elif char == "#" and word is None:
                end = text.find("\n", start)
                self.index = len(text) if end < 0 else end
            elif text.startswith(("<(", ">("), start):
                end_word()
                self.index += 2
                subs.append(self.parse(stop=True))
                skip = False  # `< <(cmd)`: the substitution was the redirection's target
            elif text.startswith("<<", start) and not text.startswith("<<<", start):
                end_word()
                pending.append(self.heredoc_word() + (bodies, subs))  # the body belongs to this command
            elif char in "<>" or text.startswith("&>", start):
                if word is not None and "".join(word).isdigit() and text[start - 1:start] not in " \t":
                    word = None  # the fd number of `2>`
                end_word()
                self.index += len(next(op for op in REDIRECTIONS if text.startswith(op, start)))
                skip = True
            elif char in ";&|\n":
                end_command()
                self.index += len(next(op for op in OPERATORS if text.startswith(op, start)))
                if char == "\n":
                    for dash, delimiter, quoted, owner_bodies, owner_subs in pending:
                        owner_bodies.append(self.heredoc_body(dash, delimiter, quoted, owner_subs))
                    pending = []
            elif text.startswith("((", start) and word is None and words in ([], ["for"]) and self.arithmetic(subs):
                pass  # `(( ... ))` and `for (( ... ))`
            elif char == "(" and word is None:
                nodes.append("(")
                opened += 1
                self.index += 1
            elif char == ")":
                end_command()
                self.index += 1
                if opened:
                    nodes.append(")")
                    opened -= 1
                elif stop:
                    return nodes
            else:
                add(char)
                self.index += 1
        if stop:
            raise ValueError("unbalanced substitution")
        end_command()
        return nodes

    def double_quoted(self, subs, closing):
        """Text up to the closing quote (or the end, for closing None), collecting substitutions."""
        out, text = [], self.text
        while self.index < len(text):
            char = self.char()
            if char == closing:
                self.index += 1
                return "".join(out)
            if char == "\\":
                following = self.char(1)
                out.append("" if following == "\n" else following if following in '$`"\\' else char + following)
                self.index += 2
            elif char in "$`" and (char == "`" or self.char(1) in "({"):
                out.append(self.substitution(subs))
            else:
                out.append(char)
                self.index += 1
        if closing:
            raise ValueError("unclosed " + closing)
        return "".join(out)

    def substitution(self, subs):
        """Read `$(...)`, `$((...))`, `${...}` or a backtick command at the index; returns its raw text."""
        start, text = self.index, self.text
        if text.startswith("${", start):
            self.index += 2
            self.double_quoted(subs, closing="}")
            return text[start:self.index]
        self.index += 1
        if text.startswith("$((", start) and self.arithmetic(subs):
            return text[start:self.index]
        self.index = start
        if text[start] == "$":
            self.index += 2
            subs.append(self.parse(stop=True))
            return text[start:self.index]
        end = start + 1
        while end < len(text) and text[end] != "`":
            end += 2 if text[end] == "\\" else 1
        if end >= len(text):
            raise ValueError("unbalanced backtick")
        subs.append(Scanner(re.sub(r"\\([`$\\])", r"\1", text[start + 1:end])).parse())
        self.index = end + 1
        return text[start:self.index]

    def arithmetic(self, subs):
        """Read the `((...))` at the index. Its text is also read as commands when it can be, as bash runs
        `$((cmd))` as a command when it is not arithmetic. False, index unchanged, for `((cmd) ...)`."""
        start, depth = self.index, 0
        while self.index < len(self.text):
            char = self.char()
            depth += {"(": 1, ")": -1}.get(char, 0)
            self.index += 1
            if depth == 1 and char == ")" and self.char() != ")":
                self.index = start  # a subshell inside a subshell or substitution
                return False
            if depth == 0:
                try:
                    subs.append(Scanner(self.text[start + 2:self.index - 2]).parse())
                except ValueError:
                    pass
                return True
        raise ValueError("unbalanced arithmetic")

    def heredoc_word(self):
        """(dash, delimiter, quoted) of the `<<WORD` at the index."""
        self.index += 2
        dash = self.char() == "-"
        self.index += dash
        while self.char() in (" ", "\t"):
            self.index += 1
        delimiter, quoted = [], False
        while self.index < len(self.text) and self.char() not in WORD_END:
            char = self.char()
            if char in "'\"":
                end = self.text.find(char, self.index + 1)
                if end < 0:
                    raise ValueError("unbalanced quote in a here-document word")
                delimiter.append(self.text[self.index + 1:end])
                self.index, quoted = end + 1, True
            else:
                quoted = quoted or char == "\\"
                delimiter.append(self.char(1) if char == "\\" else char)
                self.index += 2 if char == "\\" else 1
        if not delimiter:
            raise ValueError("here-document without a word")
        return dash, "".join(delimiter), quoted

    def heredoc_body(self, dash, delimiter, quoted, subs):
        """Read the body after the newline at the index; an unquoted body's substitutions run, so collect them."""
        lines, text = [], self.text
        while self.index < len(text):
            end = text.find("\n", self.index)
            end = len(text) if end < 0 else end
            line, self.index = text[self.index:end], end + 1
            line_end = line.lstrip("\t") if dash else line
            if line_end == delimiter:
                break
            if line_end.startswith(delimiter + ")"):  # `EOF)` closing `$(cat <<EOF`
                self.index = end - len(line_end) + len(delimiter)
                break
            lines.append(line)
        body = "\n".join(lines)
        if not quoted:
            Scanner(body).double_quoted(subs, closing=None)
        return body


def parse(text):
    """The nodes of text. ValueError if quotes, substitutions or here-document words do not close (RecursionError
    for very deep nesting)."""
    return Scanner(text).parse()
