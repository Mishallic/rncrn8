import re
import subprocess
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from rncrn8.cli import main
from rncrn8.config import SourceType
from rncrn8.memory import MemoryStore, Workspace
from rncrn8.memory.store import WORLD_TEMPLATE
from rncrn8.models import DryRunAdapter, ModelRequest
from rncrn8.onboard import Brief, brief_from_line, persona_from_item, quote_found, scan_project
from rncrn8.persona import list_personas, load_persona
from rncrn8.review.runner import resolve_panel
from rncrn8.service import record_decision

RULE_AR = "تغيير حرف أو حركة تغيّر المعنى خطأ في الاقتباس"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args], check=True, capture_output=True)


def _project(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "README.md").write_text(
        "# Antara\n\nAntara is a product-neutral Arabic language intelligence core, starting with Gulf Arabic.\n\n"
        "The fidelity checker is model-free: it never calls a model to judge an answer.\n",
        encoding="utf-8",
    )
    (root / "docs").mkdir()
    (root / "docs" / "sources.md").write_text(
        "# Reference texts\n\nThe Quran reference is **Tanzil Uthmani 1.1** (Hafs, Kufan count), downloaded 2026-10-01.\n\n"
        f"Misquote rule: «{RULE_AR}» and spelling conventions are forgiven.\n\nConstraint: Python 3.11 only.\n",
        encoding="utf-8",
    )
    (root / "src").mkdir()
    (root / "src" / "fidelity.py").write_text('"""Fidelity checker: compares quotes with reference texts."""\n\n\ndef check(answer):\n    return []\n', encoding="utf-8")
    (root / "data" / "quran").mkdir(parents=True)
    (root / "data" / "quran" / "quran-uthmani.txt").write_text(
        "# Tanzil Quran Text (Uthmani, Version 1.1)\n" + ("1|1|بسم الله الرحمن الرحيم\n" * 12000) + "# License: Creative Commons Attribution 3.0\n", encoding="utf-8"
    )
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "README.md").write_text("never read this\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-qb", "fidelity")
    (root / "src" / "fidelity.py").write_text('"""Fidelity checker: compares quotes with reference texts."""\n\n\ndef check(answer):\n    return ["x"]\n', encoding="utf-8")
    _git(root, "commit", "-qam", "fidelity checker returns verdicts")
    return root


PROFILE = {
    "name": "Antara",
    "summary": "Antara is a product-neutral Arabic language intelligence core. This branch adds a model-free fidelity checker.",
    "kinds": ["code", "document"],
    "project": [{"text": "Antara starts with Gulf Arabic", "source": "README.md", "quote": "starting with Gulf Arabic"}],
    "constraints": [{"text": "Python 3.11 only", "source": "docs\\sources.md", "quote": "Python 3.11 only"}],
    "decisions": [
        {"claim": "The Quran reference is Tanzil Uthmani 1.1, Hafs, Kufan count", "why": "", "source": "docs/sources.md", "quote": "Tanzil Uthmani 1.1 (Hafs, Kufan count)", "date": "2026-10-01"},
        {"claim": "A changed letter or meaning-bearing diacritic is a misquote; spelling conventions are forgiven", "why": "", "source": "./docs/sources.md", "quote": f"«{RULE_AR}»", "date": "2025-02-30"},
        {"claim": "The law text is the 2023 amended version", "why": "", "source": "docs/sources.md", "quote": "amended in 2023"},
        {"claim": "Answers are cached for a week", "why": "", "source": "docs/cache.md", "quote": "cached for a week"},
    ],
    "templates": [{"name": "tester", "why": "edge inputs to the checker"}, {"name": "auditor", "why": "claims that rest on records"}],
    "new_personas": [
        {"name": "quran-scholar", "display_name": "The Quran Scholar", "role": "checks riwaya, verse counts and boundaries", "why": "no seed knows the readings"},
        {"name": "tester", "display_name": "dup", "role": "dup", "why": "duplicates a seed"},
    ],
    "panels": [{"kind": "code", "personas": ["quran-scholar", "tester", "auditor", "ghost"]}],
    "facts": [{"claim": "The fidelity checker never calls a model", "origin": "README.md", "who": "", "date": "", "confidence": 0.9, "tags": ["checker"]}],
    "notes": "The PDPL version is not stated.",
}


def _item(name: str) -> dict:
    return {
        "name": name,
        "display_name": f"The {name.replace('-', ' ').title()}",
        "character": "Reads every verdict against the reference text and the reading it comes from, and distrusts a verdict that cannot show its source.",
        "rubric": ["Which reading is the reference?", "Is every verdict shown with its source?", "Could a valid reading be called a misquote?"],
        "applies_to": ["*"],
        "speaks": "early",
        "trust": {"project": "high", "history": "default", "internet": "low", "human": "medium"},
    }


class Scripted(DryRunAdapter):
    def __init__(self):
        super().__init__(self.answer)

    def answer(self, req: ModelRequest) -> dict:
        if req.label == "setup:profile":
            return PROFILE
        if req.label == "setup:personas":
            names = re.findall(r"^\d+\. name: ([a-z0-9-]+|\(choose a short slug\))", req.user, re.M)
            return {"personas": [_item("chosen-one" if n.startswith("(") else n) for n in names]}
        if req.label.startswith("ingest-history"):
            return {"facts": [{"claim": "The fidelity branch adds the checker", "origin": "git log abc1234", "who": "t", "date": "", "confidence": 0.8, "tags": []}], "notes": ""}
        raise AssertionError(f"unexpected call {req.label}")


@pytest.fixture
def antara(tmp_path, monkeypatch):
    root = _project(tmp_path / "antara")
    model = Scripted()
    monkeypatch.setattr("rncrn8.cli.make_adapter", lambda *a, **k: model)
    monkeypatch.chdir(root / "src")  # setup finds the project root from anywhere inside it
    return root, model


def test_setup_interactive_end_to_end(antara):
    root, model = antara
    res = CliRunner().invoke(main, ["setup"], input="y\ny\ngulf-linguist: a Gulf Arabic dialectologist\n\n", catch_exceptions=False)
    out = res.output
    assert res.exit_code == 0, out
    assert f"created {root / '.rncrn8'}" in out and "kept .rncrn8/ out of git" in out
    assert ".rncrn8/" in (root / ".git" / "info" / "exclude").read_text()

    profile = model.requests[0]
    assert profile.label == "setup:profile" and "SEED PERSONAS" in profile.user and "===== docs/sources.md =====" in profile.user
    assert "never read this" not in profile.user and "===== src/fidelity.py =====" in profile.user
    assert "first and last 1,500 characters of a" in profile.user and "Version 1.1" in profile.user and "Creative Commons" in profile.user
    assert "current branch: fidelity" in profile.user and "fidelity checker returns verdicts" in profile.user

    world = (root / ".rncrn8" / "world.md").read_text(encoding="utf-8")
    assert "## Project\n\nAntara is a product-neutral Arabic language intelligence core." in world
    assert "- Antara starts with Gulf Arabic [README.md]" in world and "- Python 3.11 only [docs/sources.md]" in world
    assert "The Quran reference is Tanzil Uthmani 1.1, Hafs, Kufan count [docs/sources.md]" in world
    assert "spelling conventions are forgiven [docs/sources.md]" in world
    assert "2023 amended" not in world and "cached for a week" not in world and "(what this project is" not in world
    assert "left out    decision: The law text is the 2023 amended version (the excerpt it cites is not in docs/sources.md)" in out
    assert "left out    decision: Answers are cached for a week (cites docs/cache.md, which was not among the files read)" in out
    assert "recorded 2 decision(s)" in out and "record it with `rncrn8 decide" in out
    assert world.index("Tanzil Uthmani 1.1, Hafs") < world.index("spelling conventions are forgiven")  # in the order proposed
    assert "- (2026-10-01) The Quran reference is Tanzil Uthmani 1.1" in world  # the date the file writes
    assert f"- ({date.today().isoformat()}) A changed letter" in world  # not a real date, and not in the file: dated today

    ws = Workspace(root / ".rncrn8")
    shared = MemoryStore(ws).load("shared")
    decisions = [e for e in shared if "decision" in e.tags]
    assert len(decisions) == 2 and all(e.source == SourceType.PROJECT and "ingest:setup" in e.tags and e.who is None for e in decisions)
    assert any('Found in docs/sources.md: "Tanzil Uthmani 1.1 (Hafs, Kufan count)"' in (e.note or "") for e in decisions)
    fact = next(e for e in shared if e.claim == "The fidelity checker never calls a model")
    assert any(t.startswith("src:") for t in fact.tags) and fact.origin == "README.md"
    assert any(e.claim == "The fidelity branch adds the checker" for e in shared)

    for name in ("tester", "auditor", "quran-scholar", "gulf-linguist"):
        assert load_persona(ws, name).name == name
    scholar = load_persona(ws, "quran-scholar")
    assert scholar.loss.threshold == pytest.approx(0.25) and scholar.trust_profile[SourceType.PROJECT].a == 9
    assert "quran-scholar   The Quran Scholar: checks riwaya" in out and "dup" not in out  # a seed proposed as new is dropped
    assert ws.load_config().review.panels == {"code": ["quran-scholar", "tester", "auditor", "gulf-linguist"]}
    assert "default panel for code reviews: quran-scholar, tester, auditor" in out and "joined the default code panel" in out
    assert "setup used 4 model call(s)" in out and "not found in the files: The PDPL version is not stated." in out
    assert "dry run" not in out  # the model answered, whatever adapter carried it
    assert "next: `rncrn8 review --diff main..HEAD --dry-run`" in out


def test_setup_yes_twice_adds_nothing_twice(antara):
    root, model = antara
    r = CliRunner()
    first = r.invoke(main, ["setup", "--yes", "--add", "gulf-linguist: a Gulf Arabic dialectologist"], catch_exceptions=False)
    assert first.exit_code == 0, first.output
    assert "add another persona" not in first.output  # --yes asks nothing
    assert "created gulf-linguist" in first.output and "write these to world.md and shared memory? yes (--yes)" in first.output
    world_once = (root / ".rncrn8" / "world.md").read_text(encoding="utf-8")
    second = r.invoke(main, ["setup", "--yes"], catch_exceptions=False)
    out = second.output
    profiles = [q for q in model.requests if q.label == "setup:profile"]
    assert len(profiles) == 2 and "do not propose these again, in any wording):\nnone" in profiles[0].user
    assert "- The Quran reference is Tanzil Uthmani 1.1, Hafs, Kufan count" in profiles[1].user  # the second read is told what is on record
    assert second.exit_code == 0, out
    assert "created" not in out.split("read ", 1)[0]  # the workspace was found, not created again
    assert "kept Project, Constraints as you wrote them" in out and "recorded 0 decision(s), 2 already on record" in out
    assert "\npanel\n" not in out  # every proposed persona is already here
    assert (root / ".rncrn8" / "world.md").read_text(encoding="utf-8") == world_once
    assert (root / ".git" / "info" / "exclude").read_text().count(".rncrn8/") == 1


def test_setup_declined_writes_no_world_and_only_the_personas_named(antara):
    root, _ = antara
    res = CliRunner().invoke(main, ["setup"], input="n\nn\ntester\n\n", catch_exceptions=False)
    assert res.exit_code == 0, res.output
    ws = Workspace(root / ".rncrn8")
    assert ws.read_world() == WORLD_TEMPLATE
    assert [p.name for p in list_personas(ws)] == ["tester"]
    assert ws.load_config().review.panels == {"code": ["tester"]}


def test_setup_without_answers_says_how_to_run_it(antara):
    res = CliRunner().invoke(main, ["setup"], input="")
    assert res.exit_code != 0 and "pass --yes to accept every suggestion" in res.output


def test_quote_found_forgives_layout_not_wording():
    text = "The Quran reference is **Tanzil Uthmani 1.1** (Hafs, Kufan count).\n\nRule: «" + RULE_AR + "»"
    assert quote_found("Tanzil Uthmani 1.1 (Hafs, Kufan count)", text)
    assert quote_found('"the quran reference is  tanzil uthmani 1.1"', text)  # case, quotes, spacing, markdown
    assert quote_found("The Quran reference ... Kufan count", text)  # an ellipsis skips text, in order
    assert not quote_found("Kufan count ... The Quran reference", text)
    assert quote_found(RULE_AR, text)
    assert not quote_found(RULE_AR.replace("تغيّر", "تغير"), text)  # a dropped shadda is a different quote
    assert not quote_found("Tanzil 1.1", text)  # too short to prove anything
    assert not quote_found("Tanzil Uthmani 2.0 (Hafs, Kufan count)", text)


def test_scan_reads_what_matters_first_and_skips_what_does_not(tmp_path):
    root = _project(tmp_path / "p")
    (root / "package-lock.json").write_text("{}" * 100)
    (root / "blob.bin").write_bytes(b"\x00\x01" * 100)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("def test(): pass\n")
    scan = scan_project(root)
    paths = [f.path for f in scan.files]
    assert paths[:2] == ["README.md", "docs/sources.md"] and "src/fidelity.py" in paths
    assert not any(p.startswith(("node_modules", ".git")) for p in paths)
    assert "package-lock.json" not in paths and "blob.bin" not in paths and "tests/test_x.py" not in paths
    big = next(f for f in scan.files if f.path == "data/quran/quran-uthmani.txt")
    assert big.note.startswith("first and last 1,500 characters") and "Version 1.1" in big.text and "Creative Commons" in big.text
    small = scan_project(root, max_chars=400)
    assert small.skipped > 0 and small.chars <= 400
    assert scan.branch == "fidelity" and scan.commits[0].endswith("fidelity checker returns verdicts")


def test_brief_and_persona_from_model_output():
    assert brief_from_line("gulf-linguist: a Gulf Arabic dialectologist") == Brief("gulf-linguist", "", "a Gulf Arabic dialectologist", "added by hand")
    assert brief_from_line("Quran Scholar = checks readings").name == "quran-scholar"
    assert brief_from_line("dialect-expert - checks Khaleeji forms").name == "dialect-expert"
    assert brief_from_line("someone who checks dialect handling in every answer").name == ""
    assert brief_from_line("مدقق: يراجع اللهجة") == Brief("", "", "مدقق: يراجع اللهجة", "added by hand")
    taken = {"jurist"}
    p = persona_from_item(_item("whatever"), Brief("jurist", "", "reads statutes"))
    # the typed name is kept even when taken; write_personas refuses it instead of numbering it
    assert p.name == "jurist" and p.applies_to == ["*"] and p.loss.threshold == pytest.approx(0.25)
    assert persona_from_item({**_item("x"), "character": "too short"}, Brief("x", "", "r")) is None
    assert persona_from_item({**_item("x"), "rubric": ["one?"]}, Brief("x", "", "r")) is None
    assert persona_from_item({**_item("x"), "applies_to": ["code", "sculpture"], "speaks": "nonsense"}, Brief("", "", "r")).applies_to == ["code"]


def test_review_uses_the_configured_default_panel(ws):
    cfg = ws.load_config()
    cfg.review.panels = {"document": ["skimmer", "ghost"]}
    cfg.save(ws.config_path)
    personas, warnings = resolve_panel(ws, None, "document")
    assert [p.name for p in personas] == ["skimmer"] and "names 'ghost', which is not a persona here" in warnings[0]
    assert [p.name for p in resolve_panel(ws, ["critic"], "document")[0]] == ["critic"]  # --panel wins
    assert {p.name for p in resolve_panel(ws, None, "idea")[0]} == {"critic", "skimmer"}  # no default: every persona that applies


def test_a_decision_found_in_a_file_names_the_file(ws):
    e = record_decision(ws, "The reference is Tanzil Uthmani 1.1", origin="docs/sources.md", source=SourceType.PROJECT, note='Found in docs/sources.md: "x"')
    assert e.source == SourceType.PROJECT and e.who is None and e.root is None
    assert "The reference is Tanzil Uthmani 1.1 [docs/sources.md]\n" in ws.read_world()


def test_setup_with_the_dry_run_adapter_says_no_model_read_the_files(tmp_path, monkeypatch):
    root = _project(tmp_path / "antara")
    monkeypatch.chdir(root)
    res = CliRunner().invoke(main, ["setup", "--yes", "--adapter", "dry-run"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert "dry run: no model read the files" in res.output and "Top-level layout" in "".join(
        e.claim for e in MemoryStore(Workspace(root / ".rncrn8")).load("shared")
    )


def test_setup_in_the_chat_proposes_once_then_applies_the_answers(antara, monkeypatch):
    import asyncio
    import json

    from rncrn8.mcp_server import build_server

    root, model = antara
    monkeypatch.setattr("rncrn8.mcp_server.make_adapter", lambda *a, **k: model)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test-only")
    server = build_server(root)

    def call(name, **args):
        result = asyncio.run(server.call_tool(name, args))
        data = getattr(result, "structured_content", None) or json.loads(result.content[0].text)
        return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data

    p = call("setup")
    assert p["project"] == "Antara" and isinstance(p["cost"], dict) and p["cost"]["model_calls"] == 1
    decisions = [x for x in p["world"] if x["kind"] == "decision"]
    assert [x["source"] for x in decisions] == ["docs/sources.md", "docs/sources.md"] and decisions[0]["decided"] == "2026-10-01"
    assert any("2023 amended" in r for r in p["left_out"]) and "PDPL" in p["not_found_in_files"]
    assert [(x["name"], x["new"]) for x in p["panel"]] == [("tester", False), ("auditor", False), ("quran-scholar", True)]
    again = call("setup")
    assert again["cost"].startswith("kept") and len(model.requests) == 1  # the proposal is kept, not paid for twice

    done = call("setup_apply", personas=["tester", "quran-scholar"], add=["gulf-linguist: a Gulf Arabic dialectologist"])
    assert {x["name"] for x in done["personas_created"]} == {"tester", "quran-scholar", "gulf-linguist"}  # auditor left out
    assert done["decisions_recorded"] == 2 and "Project" in done["world_written"]
    assert done["facts_from_history"] == 2  # git's activity snapshot, and the one fact the model read from the log
    assert done["default_panels"]["code"] == ["quran-scholar", "tester", "gulf-linguist"]
    gulf = next(x for x in done["personas_created"] if x["name"] == "gulf-linguist")
    assert gulf["joined_panels"] == ["code"] and "Tanzil Uthmani 1.1" in (root / ".rncrn8" / "world.md").read_text(encoding="utf-8")
    assert ".rncrn8/" in (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")

    later = call("add_persona", line="hadith-checker: checks hadith citations against their collections")
    assert [x["name"] for x in later["created"]] == ["hadith-checker"] and later["created"][0]["joined_panels"] == ["code"]
    assert [r.label for r in model.requests].count("setup:profile") == 1
