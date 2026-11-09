"""Where a change is used: unchanged code elsewhere in the repository that uses what the change modifies or
removes, shown to the panel as context.

A diff shows the changed lines and three lines around each. That hides the code that decides whether a
change matters: the caller that only ever passes delivered phases, so a new branch for unpaid ones never
runs; the file that still calls a function the change removed. For each function or class a change
modifies or removes, `git grep` finds where its name is used at the reviewed version, and the lines around
each use become a section marked as context. A persona cites it by path and line like the change itself.
Context has a budget of its own and never counts toward the size limit, and it is best effort: when git
cannot answer, the review goes ahead without it.

Names are found by pattern, not by parsing: a definition line in the languages rncrn8 reads (def, fn,
func, fun, function, class and their kin, a named arrow function, a method in a brace language), and for
a changed line the innermost definition it sits in, by indentation, within the hunk or the one git names
in the hunk header. A use counts only as code: an import, a comment, a string or a docstring's prose names
a function without using it. A name used in more places than `TOO_COMMON` is a common word or used
everywhere, and a few of its uses would say little, so it is left out.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..memory.legacy import LEGACY_WORKSPACE_DIRS
from ..memory.store import WORKSPACE_DIR
from ..packet import Section

CONTEXT_CHARS = 20_000
"""Characters of context in one review, across every section."""
USES_PER_NAME = 3
MAX_NAMES = 15
"""Names looked up, most telling first: removed, then modified."""
TOO_COMMON = 30
AROUND = 3
"""Lines shown on each side of a use."""
SHOWN_LINE_CHARS = 200

CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php", ".cs", ".scala", ".dart", ".c", ".cc", ".cpp", ".h", ".hpp"}
_BRACE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".kt", ".cs", ".scala", ".dart", ".swift", ".php", ".c", ".cc", ".cpp", ".h", ".hpp"}
_TEST_DIRS = {"tests", "test", "spec", "specs", "__tests__", "testdata", "fixtures"}
_TEST_NAME = re.compile(r"(^test_.*\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$|_spec\.rb$|Tests?\.(java|kt|cs)$)")
_SKIP_NAMES = {"main", "init", "setup", "teardown", "run", "get", "set", "new", "call", "test", "self", "this", "constructor", "render",
               "handle", "handler", "index", "default", "value", "values", "string", "tostring", "equals", "hashcode", "error", "update",
               "create", "delete", "apply", "build", "start", "stop", "close", "open", "read", "write", "load", "save", "parse", "format"}
_KEYWORDS = {"if", "for", "while", "switch", "catch", "with", "return", "function", "else", "do", "try", "foreach", "using", "lock",
             "synchronized", "elif", "when", "match", "new", "typeof", "sizeof", "await", "yield", "throw", "case"}

_MODIFIERS = r"(?:(?:export|default|async|pub(?:\([^)]*\))?|public|private|protected|internal|static|final|abstract|override|open|suspend|inline|unsafe|const|virtual|sealed|data)\s+)*"
_DEFINITIONS = (
    # def, fn, func, fun, function, sub: Python, Ruby, Rust, Go (with a receiver), Kotlin, Swift, JS and TS, PHP, Scala
    re.compile(r"^\s*" + _MODIFIERS + r"(?:def|fn|func|fun|function\*?|sub)\s+(?:\([^)]*\)\s*)?(?:self\.)?([A-Za-z_]\w*)"),
    re.compile(r"^\s*" + _MODIFIERS + r"(?:class|struct|trait|interface|module|object|enum|record)\s+([A-Za-z_]\w*)"),
    # JS and TS: a function or an arrow function bound to a name
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::\s*[^=]+)?=>|[A-Za-z_]\w*\s*=>)"),
)
# a method in a brace language: modifiers, maybe a return type, then name(arguments) and an opening brace
_METHOD = re.compile(r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|override|virtual|async|readonly|get|set|synchronized)\s+)*"
                     r"(?:[A-Za-z_][\w<>\[\],.?]*\s+)?([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\([^;'\"`]*\)\s*(?::\s*[^{;=]+)?(?:throws\s+[\w., ]+)?\s*\{\s*$")
"""Arguments holding a string make it a call with a callback, such as describe('x', function () {, not a definition."""
_CLOSERS = ("}", ")", "]")
_NOT_CODE = ("#", "//", "/*", "*", "--", "'''", '"""')
_IMPORTS = ("import ", "from ", "export {", "export *", "#include", "use ", "using ", "require ", "extern crate")
_LISTED = re.compile(r"^[\w$.]+(?:\s+as\s+\w+)?,?$")
"""A line that is only a name, as inside a multi-line import list."""


def definition(line: str, suffix: str) -> str | None:
    """The name a line defines, or None."""
    for pattern in _DEFINITIONS:
        m = pattern.match(line)
        if m:
            return m.group(1)
    if suffix in _BRACE_SUFFIXES:
        m = _METHOD.match(line)
        if m and m.group(1) not in _KEYWORDS:
            return m.group(1)
    return None


def is_test(path: str) -> bool:
    p = PurePosixPath(path)
    return bool({d.lower() for d in p.parts[:-1]} & _TEST_DIRS) or bool(_TEST_NAME.search(p.name))


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" \t"))


@dataclass
class Touched:
    name: str
    path: str
    how: set[str] = field(default_factory=set)

    @property
    def verb(self) -> str:
        if self.how == {"removed"}:
            return "removes"
        if self.how == {"added"}:
            return "adds"
        return "modifies"


def touched_names(files: list) -> list[Touched]:
    """Functions and classes the diff removes or modifies, removed first. `files` are parsed `FileDiff`s. A name
    both removed and added is modified: its signature changed, or it moved. A name only added is left out: code
    can only use it where the change itself does, which the panel already sees."""
    found: dict[str, Touched] = {}

    def note(name: str, path: str, how: str) -> None:
        if len(name) < 3 or name.lower() in _SKIP_NAMES or name.startswith("__"):
            return
        found.setdefault(name, Touched(name, path)).how.add(how)

    for f in files:
        suffix = PurePosixPath(f.path).suffix.lower()
        if f.status == "binary" or suffix not in CODE_SUFFIXES or is_test(f.path):
            continue
        for h in f.hunks:
            # the definitions a line sits in, innermost last: (name, indentation, the definition line's own tag).
            # git names the line a hunk sits under, and its default rule only takes lines that start in column 0.
            # The body of a removed or added definition is part of that removal or addition, not a modification.
            head = definition(h.header, suffix)
            scopes: list[tuple[str, int, str]] = [(head, 0, " ")] if head else []
            for raw in h.lines:
                tag, text = raw[:1], raw[1:]
                if tag == "\\" or not text.strip():
                    continue
                indent = _indent(text)
                if not text.lstrip().startswith(_CLOSERS):
                    while scopes and scopes[-1][1] >= indent:
                        scopes.pop()  # back at a definition's own level or above: outside it
                name = definition(text, suffix)
                if name:
                    if tag in "+-":
                        note(name, f.path, "added" if tag == "+" else "removed")
                    scopes.append((name, indent, tag))
                    continue
                if text.lstrip().startswith("@"):
                    continue  # a decorator or annotation belongs to the definition below it
                if tag in "+-" and scopes and tag != scopes[-1][2]:
                    note(scopes[-1][0], f.path, "changed")
    rank = {"removes": 0, "modifies": 1}
    return sorted((t for t in found.values() if t.verb in rank), key=lambda t: rank[t.verb])


@dataclass
class _Version:
    """The files as the change leaves them: a commit, the index, or the working tree."""

    top: Path
    rev: str | None = None
    index: bool = False
    cache: dict[str, list[str] | None] = field(default_factory=dict)

    def git(self, *args: str) -> str | None:
        try:
            r = subprocess.run(["git", "-c", "core.quotePath=false", "-C", str(self.top), *args], capture_output=True,
                               encoding="utf-8", errors="replace", timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode in (0, 1) else None  # git grep exits 1 when nothing matches

    def grep(self, name: str) -> list[tuple[str, int, str]]:
        where = ["--cached"] if self.index else []
        rev = [self.rev] if self.rev else []
        specs = [f"*{s}" for s in sorted(CODE_SUFFIXES)] + [f":(exclude,glob)**/{d}/**" for d in (WORKSPACE_DIR, *LEGACY_WORKSPACE_DIRS)]
        out = self.git("grep", "-n", "-w", "-I", "-z", "-F", *where, "-e", name, *rev, "--", *specs)
        hits = []
        for record in (out or "").split("\n"):
            parts = record.split("\0")
            if len(parts) < 3 or not parts[1].isdigit():
                continue
            path = parts[0][len(self.rev) + 1:] if self.rev and parts[0].startswith(self.rev + ":") else parts[0]
            hits.append((path, int(parts[1]), "\0".join(parts[2:])))
        return hits

    def lines(self, path: str) -> list[str] | None:
        if path not in self.cache:
            if self.rev or self.index:
                text = self.git("show", f"{self.rev or ''}:{path}")
            else:
                try:
                    text = (self.top / path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = None
            self.cache[path] = text.splitlines() if text is not None else None
        return self.cache[path]


def _top(repo: Path) -> Path | None:
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], capture_output=True, encoding="utf-8", errors="replace", timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


_STRINGS = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\\n])*`")


def code_use(text: str, name: str, suffix: str) -> bool:
    """Whether a line uses `name` as code: called, reached through, passed, assigned, extended or named as a type.
    Strings and comments are removed first, so a description, a log message or a docstring's prose that happens
    to hold the word does not count."""
    code = _STRINGS.sub('""', text)
    code = code.split("#", 1)[0] if suffix in (".py", ".rb") else code.split("//", 1)[0]
    n = re.escape(name)
    return re.search(rf"\b{n}\(|\.{n}\b|\b{n}\s*(?:\.|::|\[)|\bnew\s+{n}\b|[=(\[,<:@!{{]\s*{n}\b|\b{n}\s*[)\],>;}}]|->\s*{n}\b"
                     rf"|\b(?:extends|implements|instanceof|return|await|yield|raise|throw)\s+{n}\b", code) is not None


def _uses(touched: Touched, hits: list[tuple[str, int, str]], shown: dict[str, list[tuple[int, int]]]) -> list[tuple[str, int]]:
    """The uses worth showing: code that uses the name, outside the lines the diff already shows, other than its
    definition, an import or a comment."""
    out = []
    for path, n, text in hits:
        stripped = text.strip()
        suffix = PurePosixPath(path).suffix.lower()
        if any(a <= n <= b for a, b in shown.get(path, [])):
            continue
        if stripped.startswith(_NOT_CODE) or stripped.startswith(_IMPORTS) or "require(" in stripped or _LISTED.match(stripped):
            continue
        if definition(text, suffix) == touched.name or not code_use(text, touched.name, suffix):
            continue
        out.append((path, n))
    return out


def _pick(uses: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Up to USES_PER_NAME uses, one per file before a second from the same file. Code comes first, and when there
    is any, a single test after it: tests show that the change is exercised, callers show what it is for."""

    def spread(group: list[tuple[str, int]]) -> list[tuple[str, int]]:
        group = sorted(group, key=lambda u: (u[0], u[1]))
        firsts = [u for i, u in enumerate(group) if all(v[0] != u[0] for v in group[:i])]
        return firsts + [u for u in group if u not in firsts]

    code = spread([u for u in uses if not is_test(u[0])])
    tests = spread([u for u in uses if is_test(u[0])])
    return (code[:USES_PER_NAME] + tests[:1])[:USES_PER_NAME] if code else tests[:USES_PER_NAME]


def _title(path: str, names: list[Touched]) -> str:
    by_verb: dict[str, list[str]] = {}
    for t in names:
        by_verb.setdefault(t.verb, []).append(t.name)
    said = "; ".join(f"{' and '.join(ns)}, which the change {verb}" for verb, ns in by_verb.items())
    return f"{path}, unchanged: uses {said}"


def context_sections(repo: Path, files: list, mode: str, rev: str | None = None) -> tuple[list[Section], dict[str, int]]:
    """Context sections for a parsed diff of `repo`, read at the reviewed version. `mode` is "rev" (a commit,
    `rev` its SHA), "index" (staged changes) or "worktree". Returns the sections and counts for the summary:
    names looked up, names too common to show, uses shown."""
    counts = {"names": 0, "too_common": 0, "uses": 0}
    touched = touched_names(files)[:MAX_NAMES]
    top = _top(repo) if touched else None
    if top is None:
        return [], counts
    version = _Version(top, rev=rev if mode == "rev" else None, index=mode == "index")
    shown: dict[str, list[tuple[int, int]]] = {}
    for f in files:
        shown.setdefault(f.path, []).extend((h.new_start, h.new_end) for h in f.hunks)
    spans: list[_Span] = []
    used = 0
    for t in touched:
        counts["names"] += 1
        uses = _uses(t, version.grep(t.name), shown)
        if len(uses) > TOO_COMMON:
            counts["too_common"] += 1
            continue
        for path, n in _pick(uses):
            known = next((s for s in spans if s.path == path and s.first <= n <= s.last), None)
            if known:
                if t not in known.names:
                    known.names.append(t)
                continue
            lines = version.lines(path)
            if not lines or n > len(lines):
                continue
            a, b = max(1, n - AROUND), min(len(lines), n + AROUND)
            text = "\n".join(f"{i:>5}  {line if len(line) <= SHOWN_LINE_CHARS else line[:SHOWN_LINE_CHARS] + ' [...]'}" for i, line in enumerate(lines[a - 1:b], start=a))
            if used + len(text) > CONTEXT_CHARS:
                continue
            used += len(text)
            spans.append(_Span(path, a, b, text, [t]))
            counts["uses"] += 1
    sections = [Section(ref=f"{s.path}:{s.first}-{s.last}" if s.last > s.first else f"{s.path}:{s.first}", title=_title(s.path, s.names), text=s.text, context=True) for s in spans]
    return sections, counts


@dataclass
class _Span:
    path: str
    first: int
    last: int
    text: str
    names: list[Touched]
