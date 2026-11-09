import asyncio
import json

import pytest

from rncrn8.mcp_server import build_server


def payload(result):
    data = getattr(result, "structured_content", None)
    if data is None:
        data = json.loads(result.content[0].text)
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


def call(server, name, **args):
    return payload(asyncio.run(server.call_tool(name, args)))


def test_server_lists_tools_and_round_trips(ws):
    ws.config_path.write_text(ws.config_path.read_text().replace("adapter: claude", "adapter: dry-run"))
    server = build_server(ws.project_dir)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names >= {"personas", "recall", "remember", "review", "decisions", "outcome", "followup"}

    people = call(server, "personas")
    assert {p["name"] for p in people} == {"critic", "skimmer"}

    e = call(server, "remember", persona="critic", claim="Teams mute noisy bots", source="human", origin="call", who="lead", tags=["noise"])
    assert e["id"].startswith("m-") and e["source"] == "human" and e["confidence"] == pytest.approx(0.8, abs=0.001)
    recalled = call(server, "recall", persona="critic", tag="noise")
    assert [r["id"] for r in recalled] == [e["id"]]

    digest = call(server, "review", text="We sell memory to AI personas.", panel=["critic"], summaries=False)
    assert digest["review_id"].startswith("r-") and digest["findings"] == [] and digest["report"].endswith("report.md")
    full = call(server, "review", text="We sell memory to AI personas.", panel=["critic"], summaries=False, full_report=True)
    assert full["report_markdown"].startswith("# Review r-")

    assert call(server, "decisions") == []  # dry-run adapter raised no findings
    with pytest.raises(Exception):
        call(server, "review", text="x", path="y")


def test_server_without_workspace_fails_clearly(tmp_path):
    server = build_server(tmp_path)
    with pytest.raises(Exception) as info:
        call(server, "personas")
    chain = " ".join(str(x) for x in (info.value, info.value.__cause__, info.value.__context__) if x)
    assert "rncrn8 init" in chain


def _debating(request):
    me = request.label
    if "YOUR CURRENT SUMMARY" in request.user:
        return {"summary_markdown": "# summary\n"}
    if "DEBATE ROUND" in request.user:
        if me == "critic":
            return {"positions": [{"finding_id": "critic:F1", "action": "hold", "confidence": 0.8, "because": "", "changed_by": [], "revised_claim": ""}],
                    "responses": [{"finding_id": "skimmer:F1", "stance": "disagree", "confidence": 0.6, "because": "the first line names the product", "supports": [], "opposes": []}],
                    "new_findings": [], "notes": ""}
        return {"positions": [], "responses": [{"finding_id": "critic:F1", "stance": "agree", "confidence": 0.8, "because": "ten is too many to read", "supports": [], "opposes": []}],
                "new_findings": [], "notes": ""}
    if me == "critic":
        return {"understood": "a plan", "notes": "", "prior_findings": [], "findings": [
            {"kind": "problem", "claim": "Ten findings per pull request will get the bot muted", "where": "§1", "rationale": "noise", "fix": "cap at three", "confidence": 0.8, "supports": [], "opposes": []},
            {"kind": "question", "claim": "Who reads these reviews?", "where": "§1", "rationale": "", "fix": "", "confidence": 0.1, "supports": [], "opposes": []}]}
    return {"understood": "a plan", "notes": "", "prior_findings": [], "findings": [
        {"kind": "risk", "claim": "Nothing in the first line says what this is", "where": "§1", "rationale": "", "fix": "say it in the first line", "confidence": 0.97, "supports": [], "opposes": []}]}


def _reviewed(ws, monkeypatch):
    from rncrn8.models import DryRunAdapter

    ws.config_path.write_text(ws.config_path.read_text().replace("adapter: claude", "adapter: dry-run"))
    monkeypatch.setattr("rncrn8.mcp_server.make_adapter", lambda *a, **k: DryRunAdapter(_debating))
    server = build_server(ws.project_dir)
    return server, call(server, "review", text="We will post ten findings per pull request.", panel=["critic", "skimmer"], summaries=False)


def test_the_assistant_gets_a_digest_records_answers_and_reads_status(ws, monkeypatch):
    from rncrn8.memory import MemoryStore

    server, d = _reviewed(ws, monkeypatch)
    assert d["counts"] == {"agreed": 1, "disputed": 1, "unchallenged": 0, "held": 1, "dropped": 0, "merged": 0}
    assert [(f["finding"], f["status"]) for f in d["findings"]] == [("critic:F1", "agreed"), ("skimmer:F1", "disputed"), ("critic:F2", "held")]
    agreed, disputed, held = d["findings"]
    assert agreed["agree"] == [{"who": "skimmer", "says": "ten is too many to read"}] and agreed["fix"] == "cap at three"
    assert disputed["disagree"] == [{"who": "critic", "says": "the first line names the product"}] and disputed["raised_by"] == "The Skimmer"
    assert all(f["decision_id"].startswith("d-") for f in d["findings"]) and held["kind"] == "question"

    a = call(server, "answer", answers=[{"decision_id": held["decision_id"], "right": False, "reason": "the team lead reads them, every morning",
                                         "fact": "The team lead reads every review each morning", "source": "user"}])
    from rncrn8.memory.legacy import git

    assert a["not_recorded"] == [] and len(a["recorded"]) == 1
    first = a["recorded"][0]
    assert first["recorded"] == "refuted" and first["decision_id"] == held["decision_id"] and first["fact_kept"]["source"] == "human"
    assert first["fact_kept"]["who"] == (git(ws.project_dir, "config", "user.name") or "the user")  # the person git knows, by default
    assert first["fact_kept"]["origin"] == f"answer to {held['decision_id']}"
    assert {f["decision_id"] for f in a["still_open"]} == {agreed["decision_id"], disputed["decision_id"]}
    shared = [e.claim for e in MemoryStore(ws).load("shared")]
    assert "The team lead reads every review each morning" in shared
    closed = MemoryStore(ws).get_decision(held["decision_id"])
    assert closed.outcome is False and closed.outcome_note == "the team lead reads them, every morning"

    st = call(server, "status")
    assert st["workspace_in_git"] == "not in a git repository"
    assert st["open_count"] == 2 and {f["decision_id"] for f in st["open_findings"]} == {agreed["decision_id"], disputed["decision_id"]}
    assert st["recent_reviews"][0]["review_id"] == d["review_id"] and st["recent_reviews"][0]["counts"]["agreed"] == 1
    critic = next(p for p in st["personas"] if p["name"] == "critic")
    assert critic["calibration"] == "taken at its word until 3 outcomes recorded by a person; has 1" and critic["outcomes"] == 1
    # it asked its question at 0.1 and the answer was no: a reading close to what happened
    assert critic["score"] == 0.01 and critic["right_side"] == "1/1" and "follows score, not right_side" in st["weights_note"]


def test_answer_records_every_entry_and_says_what_is_still_open(ws, monkeypatch):
    from rncrn8.memory import MemoryStore

    server, d = _reviewed(ws, monkeypatch)
    agreed, disputed, held = d["findings"]
    assert "weights_note" not in call(server, "status")  # nothing recorded yet, no weights to explain
    a = call(server, "answer", answers=[
        {"decision_id": agreed["decision_id"], "right": True, "reason": "they muted it within a week", "source": "user"},
        {"decision_id": "d-nonexistent", "right": True, "reason": "a wrong id", "source": "user"},
        {"decision_id": held["decision_id"], "right": False, "reason": "the lead reads them", "source": "user"},
    ])
    assert [r["decision_id"] for r in a["recorded"]] == [agreed["decision_id"], held["decision_id"]]  # one wrong id costs nothing else
    assert a["not_recorded"] == [{"decision_id": "d-nonexistent", "why": "no decision 'd-nonexistent'"}]
    assert [f["decision_id"] for f in a["still_open"]] == [disputed["decision_id"]]
    assert MemoryStore(ws).get_decision(agreed["decision_id"]).outcome is True
    again = call(server, "answer", answers=[{"decision_id": agreed["decision_id"], "right": True, "reason": "still true", "source": "user"}])
    assert again["recorded"] == [] and "already has an outcome" in again["not_recorded"][0]["why"]
    assert [f["decision_id"] for f in again["still_open"]] == [disputed["decision_id"]]  # the review it belongs to, even so
    with pytest.raises(Exception):
        call(server, "answer", answers=[])


def test_review_with_no_target_takes_the_branch_against_its_base(ws, monkeypatch):
    import subprocess

    g = lambda *a: subprocess.run(["git", "-C", str(ws.project_dir), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *a], check=True, capture_output=True)
    g("init", "-q")
    (ws.project_dir / "app.py").write_text("x = 1\n")
    g("add", "app.py")
    g("commit", "-qm", "base")
    g("checkout", "-qb", "feature")
    (ws.project_dir / "app.py").write_text("x = 2\n")
    g("commit", "-qam", "change")
    ws.config_path.write_text(ws.config_path.read_text().replace("adapter: claude", "adapter: dry-run"))
    server = build_server(ws.project_dir)
    assert call(server, "status")["workspace_in_git"].startswith("not ignored: a broad git add would commit .rncrn8/")
    (ws.project_dir / ".git" / "info" / "exclude").write_text(".rncrn8/\n")
    assert call(server, "status")["workspace_in_git"] == "kept out of git by .git/info/exclude"
    d = call(server, "review", panel=["critic"], summaries=False)
    assert d["target"] == "diff:main..HEAD" and d["kind"] == "code"


def test_review_without_a_model_key_says_where_to_set_it(ws, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(Exception) as info:
        call(build_server(ws.project_dir), "review", text="a plan", panel=["critic"])
    chain = " ".join(str(x) for x in (info.value, info.value.__cause__, info.value.__context__) if x)
    assert "no Anthropic key where the rncrn8 server runs" in chain and "setx RNCRN8_API_KEY" in chain and "ant auth login" in chain


def test_slash_commands_and_instructions(ws, tmp_path, monkeypatch):
    from rncrn8.mcp_server import INSTRUCTIONS

    assert len(INSTRUCTIONS) < 4096  # Claude Code cuts server instructions at 4,096 characters
    assert "`answer`" in INSTRUCTIONS and "Never record your own opinion" in INSTRUCTIONS
    assert "one entry for every finding they answered, all in one call" in INSTRUCTIONS and "still open" in INSTRUCTIONS
    assert 'such as "notes never go on invoices"' in INSTRUCTIONS and "record it with `decide` and say so" in INSTRUCTIONS
    server = build_server(ws.project_dir)
    assert {p.name for p in asyncio.run(server.list_prompts())} == {"setup", "review", "outcomes", "status"}
    text = asyncio.run(server.get_prompt("review", {"target": "main..HEAD"})).messages[0].content.text
    assert "`main..HEAD`" in text and "`review`" in text
    plain = asyncio.run(server.get_prompt("review", {})).messages[0].content.text
    assert "my current branch against its base" in plain
    # with no --workspace, the project folder the client names is used
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ws.project_dir))
    monkeypatch.chdir(tmp_path.parent)
    assert {p["name"] for p in call(build_server(), "personas")} == {"critic", "skimmer"}


def test_a_target_too_large_says_what_it_is_made_of(ws, monkeypatch):
    import subprocess

    g = lambda *a: subprocess.run(["git", "-C", str(ws.project_dir), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *a], check=True, capture_output=True)
    g("init", "-q")
    (ws.project_dir / "README.md").write_text("x\n")
    g("add", "README.md")
    g("commit", "-qm", "base")
    g("checkout", "-qb", "feature")
    for rel, lines in (("scripts/fidelity/verdicts.py", 300), ("scripts/fidelity/study.py", 200), ("src/core/api.py", 40)):
        path = ws.project_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"value_{i} = {i}  # a line of the change\n" for i in range(lines)))
    g("add", "-A")
    g("commit", "-qm", "big change")
    cfg = ws.config_path.read_text().replace("adapter: claude", "adapter: dry-run").replace("max_packet_chars: 400000", "max_packet_chars: 5000")
    ws.config_path.write_text(cfg)
    with pytest.raises(Exception) as info:
        call(build_server(ws.project_dir), "review", diff="main..HEAD")
    msg = str(info.value)
    assert "so it was not reviewed" in msg and "Characters by folder: scripts/ " in msg and "scripts/fidelity/ " in msg and "src/core/ " in msg
    assert msg.index("scripts/fidelity/verdicts.py") < msg.index("scripts/fidelity/study.py") < msg.index("src/core/api.py")  # largest first
    assert "':(exclude)path'" in msg


def test_a_document_named_by_a_relative_path_is_read_from_the_project(ws, tmp_path, monkeypatch):
    (ws.project_dir / "docs").mkdir()
    (ws.project_dir / "docs" / "marketing.md").write_text("# Marketing plan\n\nWe sell to clinic owners through dental associations.\n")
    ws.config_path.write_text(ws.config_path.read_text().replace("adapter: claude", "adapter: dry-run"))
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the server's own folder is not the project
    d = call(build_server(ws.project_dir), "review", path="docs/marketing.md", panel=["critic"], summaries=False)
    assert d["kind"] == "document" and d["title"] == "marketing" and d["target"] == "docs/marketing.md"


def test_ready_made_personas_from_the_chat(ws):
    ws.config_path.write_text(ws.config_path.read_text().replace("adapter: claude", "adapter: dry-run"))
    server = build_server(ws.project_dir)
    seeds = call(server, "seed_personas")
    ideas = next(g for g in seeds["groups"] if g["reads"] == "Ideas and documents")
    assert [p["name"] for p in ideas["personas"]] == ["investor", "customer", "counsel"] and ideas["personas"][0]["asks_first"].startswith("Who exactly pays")
    critic = next(p for g in seeds["groups"] for p in g["personas"] if p["name"] == "critic")
    assert critic["in_this_project"] and seeds["panels"]["pitch"] == ["critic", "investor", "customer", "counsel", "auditor"]

    added = call(server, "add_seed_personas", panel="pitch")
    assert {p["name"] for p in added["created"]} == {"investor", "customer", "counsel", "auditor"} and added["already_here"] == ["critic"]
    assert added["default_panels_set"] == {"idea": ["critic", "investor", "customer", "counsel", "auditor"]}
    again = call(server, "add_seed_personas", panel="pitch")
    assert again["created"] == [] and sorted(again["already_here"]) == ["auditor", "counsel", "critic", "customer", "investor"]

    historian = call(server, "add_seed_personas", names=["historian"])["created"][0]
    assert historian["name"] == "historian" and historian["joined_panels"] == ["idea"]  # it reads anything, so it joins the idea panel
    with pytest.raises(Exception, match="no ready-made persona named oracle"):
        call(server, "add_seed_personas", names=["oracle"])

    d = call(server, "review", text="Antara: an Arabic fidelity checker sold to publishers of religious apps.", summaries=False)
    assert list(d["panel"]) == ["critic", "investor", "customer", "counsel", "auditor", "historian"]  # the idea panel, by default


def _sources(request):
    if request.label.startswith("research:"):
        return {"facts": [{"claim": "Small dental clinics buy software through their national association", "origin": "https://example.org/clinic-survey",
                           "who": "Example Dental Association", "date": "2026-09-01", "confidence": 0.7, "tags": []}], "notes": "No pricing data found."}
    if request.label.startswith("ingest-notes:"):
        return {"facts": [{"claim": "The clinic would pay per chair, not per clinic", "origin": "", "who": "", "date": "", "confidence": 0.8, "tags": []}], "notes": ""}
    raise AssertionError(request.label)


def test_research_and_notes_from_the_chat(ws, monkeypatch):
    from rncrn8.memory import MemoryStore
    from rncrn8.models import DryRunAdapter

    ws.config_path.write_text(ws.config_path.read_text().replace("adapter: claude", "adapter: dry-run"))
    monkeypatch.setattr("rncrn8.mcp_server.make_adapter", lambda *a, **k: DryRunAdapter(_sources))
    server = build_server(ws.project_dir)

    r = call(server, "research", persona="critic", question="How do small dental clinics buy software?", budget=2)
    assert r["remembered_by"] == "critic" and r["found"][0]["source"] == "https://example.org/clinic-survey" and r["notes"].startswith("No pricing data found.")
    kept = [e for e in MemoryStore(ws).load("critic") if e.origin == "https://example.org/clinic-survey"]
    assert len(kept) == 1 and kept[0].source.value == "internet"
    shared = call(server, "research", persona="critic", question="How do small clinics choose software?", shared=True)
    assert shared["remembered_by"] == "every persona"

    n = call(server, "ingest_notes", who="clinic owner", origin="call with a clinic owner 2026-11-10", text="She said they would pay per chair.", date="2026-11-10")
    assert n["remembered_by"] == "every persona" and n["facts"] == [{"claim": "The clinic would pay per chair, not per clinic", "who": "clinic owner", "date": "2026-11-10"}]
    said = next(e for e in MemoryStore(ws).load("shared") if e.claim.startswith("The clinic would pay per chair"))
    assert said.source.value == "human" and said.origin == "call with a clinic owner 2026-11-10"

    (ws.project_dir / "notes").mkdir()
    (ws.project_dir / "notes" / "call.md").write_text("Pricing: per chair.\n")
    from_file = call(server, "ingest_notes", who="second clinic owner", origin="call 2026-11-11", path="notes/call.md", persona="critic")
    assert from_file["remembered_by"] == "critic" and from_file["facts"][0]["who"] == "second clinic owner"
    with pytest.raises(Exception, match="give the notes as a file or as text"):
        call(server, "ingest_notes", who="x", origin="y")
