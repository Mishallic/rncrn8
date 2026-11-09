import json
import re
import subprocess
from pathlib import Path

import pytest

from rncrn8.adapters import AdapterError, build_packet, detect, git_diff_packet, text_packet
from rncrn8.adapters.diff import lines_touched, packet_from_diff, parse_unified_diff, parse_where, touched_ranges
from rncrn8.adapters.video import fmt_time, parse_subtitles, parse_time, timeline_packet

DIFF = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -38,6 +38,8 @@ def handler(req):
     user = req.args["u"]
-    query = "select * from t where u=" + user
+    query = "select * from t where u=?"
+    params = (user,)
+    log(query)
     return run(query)
 
 def other():
@@ -80,3 +82,2 @@ def tail():
-    a = 1
-    b = 2
+    a = b = 1
     return a
diff --git a/README.md b/README.md
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/README.md
@@ -0,0 +1,2 @@
+# Title
+text
diff --git a/old.txt b/old.txt
deleted file mode 100644
index 4444444..0000000
--- a/old.txt
+++ /dev/null
@@ -1,1 +0,0 @@
-gone
diff --git a/img.png b/img.png
index 5555555..6666666 100644
Binary files a/img.png and b/img.png differ
diff --git a/a.py b/b.py
similarity index 100%
rename from a.py
rename to b.py
"""


def test_parse_unified_diff_structure():
    files = parse_unified_diff(DIFF)
    by = {f.path: f for f in files}
    app = by["src/app.py"]
    assert app.status == "modified" and len(app.hunks) == 2
    h = app.hunks[0]
    assert (h.old_start, h.old_count, h.new_start, h.new_count) == (38, 6, 38, 8)
    assert h.ref == "src/app.py:38-45" and h.header == "def handler(req):"
    assert app.hunks[1].ref == "src/app.py:82-83"
    assert by["README.md"].status == "added" and by["README.md"].hunks[0].ref == "README.md:1-2"
    assert by["old.txt"].status == "deleted" and by["old.txt"].hunks[0].ref == "old.txt:0"
    assert by["img.png"].status == "binary"
    assert by["b.py"].status == "renamed" and by["b.py"].old_path == "a.py"


def test_annotated_lines_carry_new_side_numbers():
    h = parse_unified_diff(DIFF)[0].hunks[0]
    text = h.annotated()
    assert '   38  ' in text.splitlines()[0] and 'user = req.args' in text.splitlines()[0]
    assert '     - ' in text and 'select * from t where u=" + user' in text
    assert '   39+ ' in text and 'where u=?' in text
    assert '   41+ ' in text and 'log(query)' in text
    assert '   42  ' in text and 'return run(query)' in text


def test_packet_from_diff_sections_and_metadata():
    pk = packet_from_diff(DIFF, "change")
    assert pk.kind == "code"
    refs = pk.refs()
    assert "src/app.py:38-45" in refs and "README.md:1-2" in refs and "img.png" in refs and "b.py" in refs
    assert pk.metadata["files"] == "5" and pk.metadata["insertions"] == "6" and pk.metadata["deletions"] == "4"
    rendered = pk.render()
    assert "[src/app.py:38-45] src/app.py @@ def handler(req):" in rendered
    assert "(binary file changed)" in rendered and "a.py -> b.py" in rendered
    with pytest.raises(AdapterError):
        packet_from_diff("", "empty")


def test_where_parsing_and_touch_detection():
    assert parse_where("src/app.py:40-52") == ("src/app.py", 40, 52)
    assert parse_where("[src/app.py:40]") == ("src/app.py", 40, 40)
    assert parse_where("§3") is None and parse_where("t=00:03") is None
    touched = touched_ranges(DIFF)
    # only the removed lines count: old line 39 in the first hunk, old lines 80-81 in the second; context never does
    assert touched["src/app.py"] == [(39, 39), (80, 81)]
    assert touched["old.txt"] == [(1, 1)]
    assert lines_touched("src/app.py:39", touched) and lines_touched("src/app.py:38-40", touched)
    assert not lines_touched("src/app.py:40-41", touched)
    assert not lines_touched("src/app.py:43", touched)  # unchanged context line inside the hunk
    assert not lines_touched("src/app.py:50-60", touched)
    assert not lines_touched("missing.py:1", touched)
    # a pure insertion touches the two old lines on either side of the insertion point
    ins = touched_ranges("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -10,0 +11,2 @@\n+a\n+b\n")
    assert ins["x.py"] == [(10, 11)] and lines_touched("x.py:10", ins) and lines_touched("x.py:11", ins) and not lines_touched("x.py:8", ins)
    # an insertion inside a hunk with context: between old lines 5 and 6
    mid = touched_ranges("diff --git a/y.py b/y.py\n--- a/y.py\n+++ b/y.py\n@@ -3,5 +3,6 @@\n c\n d\n e\n+new\n f\n g\n")
    assert mid["y.py"] == [(5, 6)]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "app.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "one")
    (r / "app.py").write_text("def f():\n    return 10\n\n\ndef g():\n    return 2\n")
    _git(r, "commit", "-q", "-am", "two")
    return r


def test_git_diff_packet_range_and_worktree(repo: Path):
    pk = git_diff_packet(repo, "HEAD~1..HEAD")
    assert pk.kind == "code" and pk.metadata["committed"] == "true"
    assert pk.metadata["reviewed"] == _git(repo, "rev-parse", "HEAD").strip()
    assert pk.refs() == ["app.py:1-5"]
    assert pk.metadata["target"] == "repo"
    assert "return 10" in pk.render()
    with pytest.raises(AdapterError):
        git_diff_packet(repo, "HEAD")  # nothing uncommitted
    (repo / "app.py").write_text("def f():\n    return 11\n\n\ndef g():\n    return 2\n")
    wt = git_diff_packet(repo, "HEAD")
    assert wt.metadata["committed"] == "false" and "return 11" in wt.render()
    _git(repo, "add", ".")
    assert "return 11" in git_diff_packet(repo, "staged").render()
    with pytest.raises(AdapterError):
        git_diff_packet(repo, "nonexistent-rev")
    with pytest.raises(AdapterError):
        git_diff_packet(repo, "--output=/tmp/x..HEAD")  # option-shaped revisions never reach git


def test_external_packet_json_cannot_claim_git_provenance(tmp_path: Path):
    from rncrn8.packet import ReviewPacket, Section

    pk = ReviewPacket(kind="code", title="evil", sections=[Section(ref="a.py:1", text="x")], metadata={"committed": "true", "reviewed": "--output=/tmp/pwned", "repo": "/somewhere", "duration": "9s"})
    path = tmp_path / "evil.packet.json"
    path.write_text(pk.model_dump_json())
    loaded = build_packet(path)
    assert loaded.metadata == {"duration": "9s"}


def test_video_time_parsing_and_subtitles():
    assert parse_time("00:03") == 3.0 and parse_time("01:02:03.500") == 3723.5 and parse_time(12) == 12.0 and parse_time("7.5") == 7.5
    assert fmt_time(65) == "01:05" and fmt_time(3700) == "01:01:40"
    srt = "1\n00:00:01,000 --> 00:00:03,500\nHello <i>there</i>\n\n2\n00:00:07,000 --> 00:00:09,000\nSecond line\ncontinued\n"
    cues = parse_subtitles(srt)
    assert [(c.start, c.text) for c in cues] == [(1.0, "Hello there"), (7.0, "Second line continued")]
    vtt = "WEBVTT\n\n00:12.000 --> 00:14.000\nhi\n"
    assert parse_subtitles(vtt)[0].start == 12.0
    with pytest.raises(AdapterError):
        parse_time("nonsense")


def test_video_folder_packet(tmp_path: Path):
    folder = tmp_path / "cut07"
    folder.mkdir()
    (folder / "transcript.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\nWelcome back\n\n2\n00:00:06,000 --> 00:00:08,000\nHere is the thing\n")
    (folder / "frames.json").write_text(json.dumps([{"t": 0, "description": "wide shot of a desk"}, {"t": "00:06", "description": "close-up on hands"}, {"t": 12.5, "description": "title card"}]))
    (folder / "meta.json").write_text(json.dumps({"title": "Cut 07", "duration": "15s", "summary": "a short"}))
    pk = build_packet(folder)
    assert pk.kind == "video" and pk.title == "Cut 07" and pk.summary == "a short"
    assert pk.refs() == ["t=00:00", "t=00:05", "t=00:10"]
    first = pk.sections[0].text
    assert first.startswith("VISUAL: wide shot of a desk") and "AUDIO: Welcome back" in first
    assert pk.sections[1].text == "VISUAL: close-up on hands\nAUDIO: Here is the thing"
    assert pk.metadata["duration"] == "15s" and pk.metadata["window_seconds"] == "5" and pk.metadata["end"] == "00:12"
    narrow = build_packet(folder, window=2)
    assert narrow.refs()[:2] == ["t=00:00", "t=00:06"]
    srt_only = build_packet(folder / "transcript.srt")
    assert srt_only.kind == "video" and "transcript" in srt_only.summary and "frame" not in srt_only.summary
    # csv frames and txt transcript
    other = tmp_path / "cut08"
    other.mkdir()
    (other / "frames.csv").write_text("t,description\n0,open on street\n5,cut to face\n")
    (other / "transcript.txt").write_text("[00:01] hello\n00:06 - second\n")
    pk2 = build_packet(other)
    assert [s.text for s in pk2.sections] == ["VISUAL: open on street\nAUDIO: hello", "VISUAL: cut to face\nAUDIO: second"]
    with pytest.raises(AdapterError):
        timeline_packet([], "x")


def test_detect_and_build(tmp_path: Path):
    md = tmp_path / "notes.md"
    md.write_text("# A\n\nb\n")
    assert detect(md).name == "document"
    assert detect(md, kind="idea").name == "idea"
    assert build_packet(md, kind="idea").kind == "idea"
    patch = tmp_path / "x.patch"
    patch.write_text(DIFF)
    assert detect(patch).name == "diff" and build_packet(patch).kind == "code"
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(AdapterError, match="directory"):
        detect(empty)
    binary = tmp_path / "x.bin"
    binary.write_bytes(b"\x00")
    with pytest.raises(AdapterError, match="no adapter"):
        detect(binary)
    with pytest.raises(FileNotFoundError):
        detect(tmp_path / "missing")
    pk = text_packet("Problem: x\n\nSolution: y", "pitch")
    assert pk.kind == "idea" and pk.title == "pitch" and pk.target_tag == "target:pitch"
    assert build_packet(md).metadata["target"] == "notes.md" and build_packet(md).target_tag == "target:notes-md"


def test_core_never_learns_what_a_target_is():
    """The guard: core modules do not import adapters and do not know hunks or subtitles."""
    root = Path(__file__).resolve().parents[1] / "src" / "rncrn8"
    core = [root / "packet.py", root / "service.py", root / "track.py", root / "replay.py", *(root / "engine").glob("*.py"), *(root / "memory").glob("*.py"), *(root / "review").glob("*.py"), *(root / "persona").glob("*.py")]
    forbidden_imports = ("from rncrn8.adapters", "from ..adapters", "from .adapters", "import rncrn8.adapters")
    forbidden_tokens = ("hunk", "srt", "vtt", "unified_diff", "subtitle")
    for path in core:
        text = path.read_text(encoding="utf-8")
        code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
        for imp in forbidden_imports:
            assert imp not in code, f"{path.name} imports adapters"
        lowered = code.lower()
        for tok in forbidden_tokens:
            assert not re.search(rf"\b{tok}s?\b", lowered), f"{path.name} mentions '{tok}'"


def test_git_output_is_decoded_as_utf8(repo: Path):
    (repo / "app.py").write_text("def f():\n    return 'a · b — ü'\n\n\ndef g():\n    return 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "unicode")
    pk = git_diff_packet(repo, "HEAD~1..HEAD")
    assert "a · b — ü" in pk.render() and "Â" not in pk.render()


def _added_file(path: str, lines: list[str]) -> str:
    body = "".join(f"+{l}\n" for l in lines)
    return f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


def test_large_data_files_and_lockfiles_are_shown_as_head_and_tail():
    quran = ["# Tanzil Quran Text (Uthmani, Version 1.1)"] + [f"{i}|1|verse text {i}" for i in range(1, 3000)] + ["# License: Creative Commons Attribution 3.0"]
    law_md = ["# PDPL"] + [f"Article {i}: " + "x" * 60 for i in range(1, 2000)]
    small = ['{"threshold": 0.8}']
    text = (
        _added_file("data/quran/quran-uthmani.txt", quran)
        + _added_file("uv.lock", ["a = 1", "b = 2"])
        + _added_file("docs/pdpl-law.md", law_md)
        + _added_file("config/checker.json", small)
        + _added_file("src/fidelity.py", ["def check(answer):", "    return []"])
    )
    pk = packet_from_diff(text, "branch")
    by = {s.ref: s for s in pk.sections}
    q = by["data/quran/quran-uthmani.txt"]
    assert "a large data file, +3,001 -0 lines; first and last lines shown" in q.title
    assert "    1+ # Tanzil Quran Text (Uthmani, Version 1.1)" in q.text and " 3001+ # License: Creative Commons Attribution 3.0" in q.text
    assert "[... 2,966 lines not shown: a large data file; the full file is in the repository ...]" in q.text
    assert len(q.text.splitlines()) == 25 + 1 + 10
    assert by["files not shown"].text.startswith("uv.lock (added, a lockfile, +2 -0): a = 1")
    assert "a very large file" in by["docs/pdpl-law.md"].title  # markdown outside a data folder is not data, but 120k characters is not reviewable either
    assert by["config/checker.json:1"].text == '    1+ {"threshold": 0.8}'  # small data stays in full, with its line ref
    assert by["src/fidelity.py:1-2"].text.startswith("    1+ def check(answer):")
    assert pk.metadata["summarized"] == "3" and pk.metadata["insertions"] == str(3001 + 2 + 2000 + 1 + 2)
    assert "2 large file(s) are shown as their first and last lines only." in pk.summary
    assert "1 more file(s), lockfiles and data, are listed under 'files not shown' without their content." in pk.summary
    assert pk.char_count < 12_000


def test_many_medium_data_files_share_one_budget_and_the_workspace_is_left_out():
    surahs = "".join(_added_file(f"data/quran/{i:03}.json", [f'{{"surah": {i}, "ayah": {j}, "text": "' + "a" * 80 + '"}' for j in range(60)]) for i in range(1, 41))
    workspace = _added_file(".rncrn8/personas/critic/memory.jsonl", ['{"id": "m-1"}'] * 50) + _added_file(".rncrn8/world.md", ["# Shared world"])
    code = _added_file("src/fidelity.py", ["def check(answer):", "    return []"])
    pk = packet_from_diff(surahs + workspace + code, "branch")
    full = [s for s in pk.sections if s.ref.startswith("data/quran/")]
    listed = next(s for s in pk.sections if s.ref == "files not shown").text.splitlines()
    assert len(full) + len(listed) == 40 and 0 < len(full) < 40  # each file is under the per-file limit; together they are not
    assert sum(len(s.text) for s in full) <= 30_000
    assert all(", one of many data files, +60 -0): {\"surah\": " in line for line in listed)
    assert not any(s.ref.startswith(".rncrn8/") for s in pk.sections) and pk.metadata["workspace_files_left_out"] == "2"
    assert "2 file(s) under .rncrn8/, the panel's own workspace, are left out." in pk.summary
    assert next(s for s in pk.sections if s.ref == "src/fidelity.py:1-2").text.startswith("    1+ def check(answer):")
    assert pk.char_count < 45_000
    # 114 surahs, each a large file: a few show their first and last lines, the rest are one line each
    big = "".join(_added_file(f"data/quran/{i:03}.txt", [f"{i}|{j}|" + "b" * 200 for j in range(200)]) for i in range(1, 115))
    pk = packet_from_diff(big + code, "branch")
    shown = [s for s in pk.sections if "first and last lines shown" in s.title]
    assert len(shown) == 5 and all(len(l) <= 206 for s in shown for l in s.text.splitlines())
    assert shown[0].text.splitlines()[0].startswith("    1+ 1|0|bbbb") and shown[0].text.splitlines()[0].endswith(" [...]")
    assert len(next(s for s in pk.sections if s.ref == "files not shown").text.splitlines()) == 109
    assert pk.char_count < 60_000
    with pytest.raises(AdapterError, match="only changes .rncrn8/"):
        packet_from_diff(workspace, "only the workspace")
    # a workspace still under the name from before the rename is the panel's own too
    old = _added_file(".reincarnate/world.md", ["# Shared world"])
    pk = packet_from_diff(workspace + old + code, "branch")
    assert [s.ref for s in pk.sections] == ["src/fidelity.py:1-2"] and pk.metadata["workspace_files_left_out"] == "3"
    assert "3 file(s) under .reincarnate/, .rncrn8/, the panel's own workspace, are left out." in pk.summary


def test_path_limits_a_diff_review_and_the_size_error_names_the_largest_parts(repo: Path, monkeypatch):
    from click.testing import CliRunner

    from rncrn8.adapters import git_diff_packet
    from rncrn8.cli import main

    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "big.py").write_text("\n".join(f"x{i} = {i}" for i in range(3000)) + "\n")
    (repo / "notes.md").write_text("a note\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "big"], check=True)
    pk = git_diff_packet(repo, "HEAD~1..HEAD", paths=["notes.md"])
    assert [s.ref for s in pk.sections] == ["notes.md:1"] and pk.metadata["paths"] == "notes.md" and "(notes.md)" in pk.title
    with pytest.raises(AdapterError, match="in nowhere/ is empty"):
        git_diff_packet(repo, "HEAD~1..HEAD", paths=["nowhere/"])

    monkeypatch.chdir(repo)
    r = CliRunner()
    assert r.invoke(main, ["init"]).exit_code == 0
    assert r.invoke(main, ["persona", "new", "critic", "--template", "critic"]).exit_code == 0
    (repo / ".rncrn8" / "config.yaml").write_text("review:\n  max_packet_chars: 5000\n")
    res = r.invoke(main, ["review", "--diff", "HEAD~1..HEAD"])
    assert res.exit_code != 0 and "The largest parts: src/big.py (" in res.output and "Review part of it with --path, for example --path src" in res.output
    res = r.invoke(main, ["review", "app.py", "--path", "src"])
    assert res.exit_code != 0 and "--path works with --diff only" in res.output
    res = r.invoke(main, ["review", "--diff", "HEAD~1..HEAD", "--path", "notes.md", "--dry-run"])
    assert res.exit_code == 0 and "notes.md" in res.output
