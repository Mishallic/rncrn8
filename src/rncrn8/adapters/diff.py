"""Git diffs: each hunk is a section whose ref is the new-side line range, lines numbered so a persona cites exactly.

Also used by follow-ups: a later diff's old-side ranges tell which reviewed lines were touched.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..memory.legacy import LEGACY_WORKSPACE_DIRS
from ..memory.store import WORKSPACE_DIR
from ..packet import ReviewPacket, Section
from .base import AdapterError

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_DIFF_GIT = re.compile(r'^diff --git (?:"a/((?:[^"\\]|\\.)*)"|a/(.*?)) (?:"b/((?:[^"\\]|\\.)*)"|b/(.*))$')


def _unquote(path: str) -> str:
    """Git quotes paths with special characters and writes bytes as octal escapes; undo that."""
    path = path.strip()
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        inner = path[1:-1]
        try:
            return inner.encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return inner
    return path


def _strip_prefix(path: str, prefix: str) -> str:
    path = _unquote(path)
    return path[2:] if path.startswith(prefix) else path


@dataclass
class Hunk:
    path: str
    old_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: list[str] = field(default_factory=list)

    @property
    def new_end(self) -> int:
        return self.new_start + max(self.new_count, 1) - 1

    @property
    def old_end(self) -> int:
        return self.old_start + max(self.old_count, 1) - 1

    @property
    def ref(self) -> str:
        if self.new_count <= 1:
            return f"{self.path}:{self.new_start}"
        return f"{self.path}:{self.new_start}-{self.new_end}"

    def changed_old_lines(self) -> list[tuple[int, int]]:
        """Old-side line ranges this hunk actually changes: runs of removed lines, and for a pure
        insertion the two old lines on either side of the insertion point. Context lines never count."""
        ranges: set[tuple[int, int]] = set()
        n = self.old_start + (1 if self.old_count == 0 else 0)  # with no old lines, old_start is the line before the insertion
        run_start: int | None = None
        for raw in self.lines:
            tag = raw[:1]
            if tag == "-":
                if run_start is None:
                    run_start = n
                n += 1
            elif tag == "+":
                if run_start is None:
                    ranges.add((max(n - 1, 1), n))
            else:
                if run_start is not None:
                    ranges.add((run_start, n - 1))
                    run_start = None
                if tag == " ":
                    n += 1
        if run_start is not None:
            ranges.add((run_start, n - 1))
        return sorted(ranges)

    def annotated(self) -> str:
        """Context and added lines carry their new-side number; removed lines carry a dash."""
        out = []
        n = self.new_start
        for raw in self.lines:
            tag, text = (raw[0], raw[1:]) if raw else (" ", "")
            if tag == "+":
                out.append(f"{n:>5}+ {text}")
                n += 1
            elif tag == "-":
                out.append(f"{'':>5}- {text}")
            elif tag == "\\":
                out.append(f"{'':>5}  {raw}")
            else:
                out.append(f"{n:>5}  {text}")
                n += 1
        return "\n".join(out)


@dataclass
class FileDiff:
    path: str
    old_path: str
    status: str = "modified"  # modified | added | deleted | renamed | binary
    hunks: list[Hunk] = field(default_factory=list)
    mode_change: bool = False


def parse_unified_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    old_path = new_path = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = _DIFF_GIT.match(line)
            if m:
                old_path = _unquote(f'"{m.group(1)}"') if m.group(1) is not None else m.group(2)
                new_path = _unquote(f'"{m.group(3)}"') if m.group(3) is not None else m.group(4)
            else:
                old_path, new_path = None, None
            current = FileDiff(path=new_path or "?", old_path=old_path or new_path or "?")
            files.append(current)
            hunk = None
            continue
        if current is None:
            continue
        if line.startswith("new file mode"):
            current.status = "added"
        elif line.startswith("deleted file mode"):
            current.status = "deleted"
        elif line.startswith("rename from "):
            current.old_path = line[len("rename from ") :].strip()
            current.status = "renamed"
        elif line.startswith("rename to "):
            current.path = line[len("rename to ") :].strip()
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            current.status = "binary"
        elif line.startswith("--- "):
            p = line[4:].strip()
            if p != "/dev/null":
                current.old_path = _strip_prefix(p, "a/")
        elif line.startswith("+++ "):
            p = line[4:].strip()
            if p != "/dev/null":
                current.path = _strip_prefix(p, "b/")
        elif line.startswith("old mode ") or line.startswith("new mode "):
            current.mode_change = True
        elif line.startswith("@@"):
            m = _HUNK.match(line)
            if not m:
                continue
            hunk = Hunk(
                path=current.path,
                old_path=current.old_path,
                old_start=int(m.group(1)),
                old_count=int(m.group(2)) if m.group(2) is not None else 1,
                new_start=int(m.group(3)),
                new_count=int(m.group(4)) if m.group(4) is not None else 1,
                header=m.group(5).strip(),
            )
            current.hunks.append(hunk)
        elif hunk is not None and (line[:1] in ("+", "-", " ", "\\")):
            hunk.lines.append(line)
    for f in files:
        for h in f.hunks:
            h.path = f.path
            h.old_path = f.old_path
    return files


LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "cargo.lock", "composer.lock", "gemfile.lock", "go.sum", "pipfile.lock", "bun.lock"}
DATA_SUFFIXES = {".txt", ".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".xml", ".html", ".htm", ".yaml", ".yml", ".svg", ".map", ".sql", ".srt", ".vtt"}
DATA_DIRS = {"data", "datasets", "dataset", "fixtures", "testdata", "corpus", "corpora", "resources", "assets", "vendor", "third_party", "third-party", "generated", "dist", "build", "downloads"}
CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php", ".cs", ".scala", ".dart", ".c", ".cc", ".cpp", ".h", ".hpp", ".sh", ".ps1"}
WORKSPACE_PREFIXES = tuple(f"{d}/" for d in (WORKSPACE_DIR, *LEGACY_WORKSPACE_DIRS))
"""The panel's own workspace, under its current name and the one from before the rename."""
LARGE_DATA_CHARS = 30_000
"""Changes to data files shown in full, per file and across the whole diff."""
LARGE_FILE_CHARS = 100_000
HEAD_LINES, TAIL_LINES = 25, 10
HEAD_TAIL_FILES = 5
SHOWN_LINE_CHARS = 200
"""Large files shown as their first and last lines; past this many, the rest are listed one line each."""
NOT_SHOWN_REF = "files not shown"


def _is_data(path: str) -> bool:
    """Data by its suffix, or anything but code inside a data folder such as data/ or fixtures/."""
    p = Path(path)
    name, suffix = p.name.lower(), p.suffix.lower()
    if suffix in DATA_SUFFIXES or name.endswith((".min.js", ".min.css")):
        return True
    return bool({d.lower() for d in p.parts[:-1]} & DATA_DIRS) and suffix not in CODE_SUFFIXES


def _change_size(f: "FileDiff") -> int:
    return sum(len(l) for h in f.hunks for l in h.lines)


def _too_large_to_show(f: "FileDiff") -> str | None:
    """Why a file's changes are shown as head and tail only, or None to show them in full. Lockfiles are
    never read by a reviewer; a large data file, or any very large file such as a downloaded text saved as
    markdown, would fill every persona's call with content nobody reviews line by line."""
    if Path(f.path).name.lower() in LOCKFILES:
        return "a lockfile"
    size = _change_size(f)
    if _is_data(f.path) and size > LARGE_DATA_CHARS:
        return "a large data file"
    if size > LARGE_FILE_CHARS:
        return "a very large file"
    return None


def _head_and_tail(f: "FileDiff", why: str, added: int, removed: int) -> Section:
    lines = [l if len(l) <= SHOWN_LINE_CHARS else l[:SHOWN_LINE_CHARS] + " [...]" for l in "\n\n".join(h.annotated() for h in f.hunks).splitlines()]
    hidden = len(lines) - HEAD_LINES - TAIL_LINES
    shown = lines if hidden <= 0 else [*lines[:HEAD_LINES], f"{'':>7}[... {hidden:,} lines not shown: {why}; the full file is in the repository ...]", *lines[-TAIL_LINES:]]
    status = f"{f.status}, " if f.status != "modified" else ""
    return Section(ref=f.path, title=f"{f.path} ({status}{why}, +{added:,} -{removed:,} lines; first and last lines shown)", text="\n".join(shown))


def packet_from_diff(text: str, title: str, metadata: dict[str, str] | None = None) -> ReviewPacket:
    parsed = parse_unified_diff(text)
    # the panel's own workspace is memory about the project, not the project
    files = [f for f in parsed if not (f.path.startswith(WORKSPACE_PREFIXES) or f.old_path.startswith(WORKSPACE_PREFIXES))]
    workspace = len(parsed) - len(files)
    workspace_dirs = ", ".join(sorted({p.split("/", 1)[0] + "/" for f in parsed for p in (f.path, f.old_path) if p.startswith(WORKSPACE_PREFIXES)}))
    if parsed and not files:
        raise AdapterError(f"the diff only changes {workspace_dirs}, the panel's own workspace: nothing of the project to review")
    reasons = {i: why for i, f in enumerate(files) if (why := _too_large_to_show(f))}
    # many medium data files add up: they share one budget, and the largest are shown as head and tail first
    data = sorted((i for i, f in enumerate(files) if i not in reasons and f.hunks and _is_data(f.path)), key=lambda i: _change_size(files[i]), reverse=True)
    data_total = sum(_change_size(files[i]) for i in data)
    for i in data:
        if data_total <= LARGE_DATA_CHARS:
            break
        reasons[i] = "one of many data files"
        data_total -= _change_size(files[i])
    sections: list[Section] = []
    insertions = deletions = 0
    head_tail = 0
    listed: list[str] = []
    for index, f in enumerate(files):
        if f.status == "binary":
            sections.append(Section(ref=f.path, title=f"{f.path} (binary)", text="(binary file changed)"))
            continue
        if f.status == "renamed" and not f.hunks:
            sections.append(Section(ref=f.path, title=f"{f.old_path} -> {f.path}", text="(renamed, no content change)"))
            continue
        if f.mode_change and not f.hunks:
            sections.append(Section(ref=f.path, title=f"{f.path} (mode change)", text="(mode change only)"))
            continue
        added = sum(1 for h in f.hunks for l in h.lines if l.startswith("+"))
        removed = sum(1 for h in f.hunks for l in h.lines if l.startswith("-"))
        insertions += added
        deletions += removed
        why = reasons.get(index)
        if why:
            # a lockfile, or one of many data files, is a line in a list; a large file shows its first and last
            # lines, where sources and versions are stated, up to a few files
            if why in ("a large data file", "a very large file") and head_tail < HEAD_TAIL_FILES:
                sections.append(_head_and_tail(f, why, added, removed))
                head_tail += 1
            else:
                first = next((l[1:] for h in f.hunks for l in h.lines if l[:1] in "+-" and l[1:].strip()), "")
                status = f"{f.status}, " if f.status != "modified" else ""
                listed.append(f"{f.path} ({status}{why}, +{added:,} -{removed:,}): {' '.join(first.split())[:120]}")
            continue
        for h in f.hunks:
            status = "" if f.status == "modified" else f" ({f.status})"
            sections.append(Section(ref=h.ref, title=f"{f.path}{status} @@ {h.header}".rstrip(" @"), text=h.annotated()))
    if listed:
        sections.append(Section(ref=NOT_SHOWN_REF, title=f"Files not shown in full ({len(listed)}): path, what it is, lines added and removed, first changed line", text="\n".join(listed)))
    if not sections:
        raise AdapterError("the diff is empty: nothing to review")
    meta = {"files": str(len(files)), "insertions": str(insertions), "deletions": str(deletions)}
    summarized = head_tail + len(listed)
    if summarized:
        meta["summarized"] = str(summarized)
    if workspace:
        meta["workspace_files_left_out"] = str(workspace)
    meta.update(metadata or {})
    summary = f"{len(files)} file(s) changed, +{insertions} -{deletions}. Line numbers are the new side; a dash marks a removed line."
    if head_tail:
        summary += f" {head_tail} large file(s) are shown as their first and last lines only."
    if listed:
        summary += f" {len(listed)} more file(s), lockfiles and data, are listed under '{NOT_SHOWN_REF}' without their content."
    if workspace:
        summary += f" {workspace} file(s) under {workspace_dirs}, the panel's own workspace, are left out."
    return ReviewPacket(kind="code", title=title, summary=summary, sections=sections, metadata=meta)


_SHA = re.compile(r"^[0-9a-f]{40}$")


def _git(repo: Path, *args: str) -> str:
    try:
        # git writes UTF-8; never decode it with the console's locale (cp1252 on Windows turns "·" into "Â·")
        # core.quotePath=false keeps non-ASCII file names as UTF-8 instead of octal escapes in quotes
        res = subprocess.run(["git", "-c", "core.quotePath=false", "-C", str(repo), *args], capture_output=True, encoding="utf-8", errors="replace", check=False)
    except FileNotFoundError as exc:
        raise AdapterError("git is not installed or not on PATH") from exc
    if res.returncode != 0:
        raise AdapterError(f"git {' '.join(args)} failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout


def resolve_commit(repo: Path, rev: str) -> str:
    """A revision from a user or a file becomes a full commit SHA, or an error. `--end-of-options`
    keeps a value like `--output=x` from being read as an option; the SHA is the only thing git
    diff is ever given afterwards."""
    if rev.startswith("-") or not rev.strip():
        raise AdapterError(f"'{rev}' is not a revision")
    sha = _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}").strip()
    if not _SHA.match(sha):
        raise AdapterError(f"'{rev}' did not resolve to a commit")
    return sha


def git_diff_packet(repo: Path, spec: str, title: str | None = None, paths: list[str] | tuple[str, ...] | None = None) -> ReviewPacket:
    """spec: 'A..B' (two commits), 'staged' (index vs HEAD), 'worktree' (unstaged), or a single
    revision R (working tree against R; 'HEAD' is your uncommitted changes). `paths` limits the diff to
    those files or folders."""
    repo = Path(repo)
    head = resolve_commit(repo, "HEAD")
    meta: dict[str, str] = {"repo": str(repo.resolve()), "head": head, "target": repo.resolve().name}
    clean = [p.replace("\\", "/").strip() for p in (paths or []) if p and p.strip()]
    pathspec = ["--", *clean] if clean else []
    if ".." in spec:
        base, top = spec.split("..", 1)
        top = top or "HEAD"
        base_sha = resolve_commit(repo, base)
        top_sha = resolve_commit(repo, top)
        text = _git(repo, "diff", base_sha, top_sha, *pathspec)
        meta.update({"base": base_sha, "reviewed": top_sha, "spec": spec, "committed": "true"})
        label = f"{base}..{top}"
    elif spec in ("staged", "--staged", "--cached"):
        text = _git(repo, "diff", "--cached", *pathspec)
        meta.update({"base": head, "spec": "staged", "committed": "false"})
        label = "staged changes"
    elif spec in ("worktree", "unstaged"):
        text = _git(repo, "diff", *pathspec)
        meta.update({"base": head, "spec": "worktree", "committed": "false"})
        label = "unstaged changes"
    else:
        base_sha = resolve_commit(repo, spec)
        text = _git(repo, "diff", base_sha, *pathspec)
        meta.update({"base": base_sha, "spec": spec, "committed": "false"})
        label = f"working tree vs {spec}"
    if clean:
        meta["paths"] = ", ".join(clean)
        label += f" ({', '.join(clean)})"
    if not text.strip():
        raise AdapterError(f"git diff for '{spec}'" + (f" in {', '.join(clean)}" if clean else "") + " is empty: nothing to review")
    packet = packet_from_diff(text, title or f"{repo.resolve().name}: {label}", meta)
    add_usage_context(packet, repo, text, ("rev", meta["reviewed"]) if meta.get("committed") == "true" else ("index", None) if meta["spec"] == "staged" else ("worktree", None))
    return packet


def add_usage_context(packet: ReviewPacket, repo: Path, text: str, version: tuple[str, str | None]) -> None:
    """Append where the changed code is used elsewhere, unchanged, read at the reviewed version: a commit,
    the index, or the working tree. Files the panel only sees listed or cut short are no source of names."""
    from .usages import context_sections

    files = [f for f in parse_unified_diff(text)
             if not (f.path.startswith(WORKSPACE_PREFIXES) or f.old_path.startswith(WORKSPACE_PREFIXES)) and not _too_large_to_show(f) and not _is_data(f.path)]
    sections, _counts = context_sections(repo, files, version[0], version[1])
    if not sections:
        return
    packet.sections.extend(sections)
    packet.metadata["context_sections"] = str(len(sections))
    packet.summary = (packet.summary or "") + (
        f" {len(sections)} section(s) titled 'unchanged' are not part of the change: they show code elsewhere that uses what the "
        "change modifies or removes, so you can tell whether the change is reached and fits how it is called.")


class DiffAdapter:
    name = "diff"
    kind = "code"

    def matches(self, target: Path) -> bool:
        return target.is_file() and target.suffix.lower() in {".diff", ".patch"}

    def build(self, target: Path, **options) -> ReviewPacket:
        text = target.read_text(encoding="utf-8", errors="replace")
        return packet_from_diff(text, options.get("title") or target.stem, {"source": str(target), "target": target.name})


# --- follow-ups -------------------------------------------------------------------------------------

_WHERE = re.compile(r"^\[?(?P<path>(?!t=)[^\s\[\]:=]*[A-Za-z_][^\s\[\]:=]*):(?P<a>\d+)(?:-(?P<b>\d+))?\]?$")


def parse_where(where: str) -> tuple[str, int, int] | None:
    """'src/app.py:40-52' -> ('src/app.py', 40, 52); 'src/app.py:40' -> (path, 40, 40).
    Section refs like '§3' and timestamps like 't=00:03' are not locations in a file."""
    m = _WHERE.match(where.strip())
    if not m:
        return None
    a = int(m.group("a"))
    b = int(m.group("b")) if m.group("b") else a
    return m.group("path"), min(a, b), max(a, b)


def touched_ranges(text: str) -> dict[str, list[tuple[int, int]]]:
    """Per old-side path, the old-side lines a diff actually changes: removed lines, and for a pure
    insertion the two old lines on either side of the insertion point. Context lines never count."""
    out: dict[str, list[tuple[int, int]]] = {}
    for f in parse_unified_diff(text):
        for h in f.hunks:
            out.setdefault(f.old_path, []).extend(h.changed_old_lines())
    return out


_NAMED_LINES = re.compile(r"\blines?\s+(\d+(?:\s*[-\u2013]\s*\d+)?(?:\s*(?:,|and)\s*\d+(?:\s*[-\u2013]\s*\d+)?)*)", re.I)


def cited_ranges(where: str) -> tuple[str, list[tuple[int, int]]] | None:
    """The file and line ranges a finding cites. 'path:a-b' gives one range. '[path:a-b] lines 212,
    1732, 1757-1758' gives the named lines, which narrow the section; named lines outside it are ignored.
    None when the location is not a file and lines."""
    text = " ".join(where.split())
    head, _, rest = text.partition(" ")
    parsed = parse_where(head) if rest else parse_where(text)
    if parsed is None:
        return None
    path, a, b = parsed
    ranges: list[tuple[int, int]] = []
    m = _NAMED_LINES.search(rest)
    if m:
        for part in re.split(r"\s*(?:,|and)\s*", m.group(1)):
            nums = [int(n) for n in re.findall(r"\d+", part)]
            if not nums:
                continue
            lo, hi = min(nums), max(nums)
            if a <= lo and hi <= b:
                ranges.append((lo, hi))
    return path, ranges or [(a, b)]


def ranges_touched(path: str, ranges: list[tuple[int, int]], touched: dict[str, list[tuple[int, int]]]) -> bool:
    return any(lo <= b and a <= hi for a, b in ranges for lo, hi in touched.get(path, []))


def lines_touched(where: str, touched: dict[str, list[tuple[int, int]]]) -> bool:
    parsed = parse_where(where)
    if parsed is None:
        return False
    path, a, b = parsed
    for lo, hi in touched.get(path, []):
        if lo <= b and a <= hi:
            return True
    return False


def is_ancestor(repo: Path, ancestor_sha: str, descendant: str) -> bool:
    if not _SHA.match(ancestor_sha):
        raise AdapterError(f"'{ancestor_sha}' is not a full commit SHA")
    descendant_sha = resolve_commit(repo, descendant)
    res = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor_sha, descendant_sha], capture_output=True, check=False)
    return res.returncode == 0


def git_diff_text(repo: Path, base_sha: str, top: str = "HEAD") -> tuple[str, str]:
    """Diff between two commits. `base_sha` must already be a full SHA; `top` is resolved here."""
    if not _SHA.match(base_sha):
        raise AdapterError(f"'{base_sha}' is not a full commit SHA")
    top_sha = resolve_commit(repo, top)
    return _git(repo, "diff", base_sha, top_sha), top_sha
