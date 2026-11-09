"""Where a change is used: unchanged callers shown to the panel as context."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rncrn8.adapters import git_diff_packet
from rncrn8.adapters import usages
from rncrn8.adapters.diff import parse_unified_diff
from rncrn8.adapters.usages import context_sections, definition, touched_names
from rncrn8.packet import ReviewPacket, Section, size_by_path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    for name, text in files.items():
        (r / name).parent.mkdir(parents=True, exist_ok=True)
        (r / name).write_text(text)
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "base")
    return r


INVOICE = 'import { Phase } from "./types";\n\nexport function buildInvoice(phases: Phase[]) {\n  const lines = phases.map((p) => p.amount);\n  return lines;\n}\n'
CLI = ('import { buildInvoice } from "./invoice";\n\n' + "".join(f"function helper{i}(x: number) {{ return x + {i}; }}\n" for i in range(30))
       + '\nexport function invoiceCommand(project) {\n  const invoice = buildInvoice(project.phases.filter((p) => p.status === "delivered"));\n  return invoice;\n}\n')


def _undelivered_billing(r: Path) -> None:
    (r / "src/invoice.ts").write_text(INVOICE.replace(
        "  const lines = phases.map((p) => p.amount);\n",
        "  const lines = phases.map((p) => p.amount);\n  const deposits = phases.filter((p) => p.status !== \"delivered\");\n"))


def test_a_caller_outside_the_diff_is_shown_as_unchanged_context(tmp_path: Path):
    r = _repo(tmp_path, {"src/invoice.ts": INVOICE, "src/cli.ts": CLI})
    _undelivered_billing(r)
    _git(r, "commit", "-q", "-am", "bill deposits")
    pk = git_diff_packet(r, "HEAD~1..HEAD")
    context = [s for s in pk.sections if s.context]
    assert len(context) == 1 and pk.metadata["context_sections"] == "1"
    s = context[0]
    call = next(i for i, line in enumerate((r / "src/cli.ts").read_text().splitlines(), start=1) if "buildInvoice(project" in line)
    assert s.ref == f"src/cli.ts:{call - 3}-{call + 2}"  # three lines above; the file ends two lines below
    assert "buildInvoice(project.phases.filter" in s.text and f"{call:>5}  " in s.text
    assert s.title == "src/cli.ts, unchanged: uses buildInvoice, which the change modifies"
    assert "titled 'unchanged' are not part of the change" in pk.summary
    assert pk.reviewed_chars == pk.char_count - len(s.text)
    assert "src/cli.ts" not in dict(size_by_path(pk)[1])  # the size limit and its breakdown ignore context


def test_a_removed_function_still_called_elsewhere_is_shown(tmp_path: Path):
    r = _repo(tmp_path, {"lib/totals.py": "def old_total(xs):\n    return sum(xs)\n\n\ndef new_total(xs):\n    return sum(xs)\n",
                         "app/report.py": "from lib.totals import old_total\n\n\ndef report(xs):\n    return old_total(xs)\n"})
    (r / "lib/totals.py").write_text("def new_total(xs):\n    return sum(xs)\n")
    _git(r, "commit", "-q", "-am", "drop old_total")
    context = [s for s in git_diff_packet(r, "HEAD~1..HEAD").sections if s.context]
    assert [s.ref for s in context] == ["app/report.py:2-5"]
    assert context[0].title == "app/report.py, unchanged: uses old_total, which the change removes"


def test_working_tree_and_staged_reviews_read_their_own_version(tmp_path: Path):
    r = _repo(tmp_path, {"src/invoice.ts": INVOICE, "src/cli.ts": CLI})
    _undelivered_billing(r)
    worktree = [s for s in git_diff_packet(r, "HEAD").sections if s.context]
    assert [s.ref.split(":")[0] for s in worktree] == ["src/cli.ts"]
    _git(r, "add", ".")
    (r / "src/cli.ts").write_text(CLI.replace("buildInvoice(project", "renamedOnlyInTheWorkingTree(project"))
    staged = [s for s in git_diff_packet(r, "staged").sections if s.context]
    assert len(staged) == 1 and "buildInvoice(project" in staged[0].text  # the index, not the working tree


def test_lines_the_diff_shows_and_the_workspace_are_never_repeated(tmp_path: Path):
    body = "def compute_fee(x):\n    return x * 2\ndef charge(x):\n    return compute_fee(x)\n"
    r = _repo(tmp_path, {"fees.py": body, ".rncrn8/memory/notes.py": "compute_fee(1)\n", "sub/.reincarnate/x.py": "compute_fee(2)\n"})
    (r / "fees.py").write_text(body.replace("x * 2", "x * 3"))
    _git(r, "commit", "-q", "-am", "raise the fee")
    pk = git_diff_packet(r, "HEAD~1..HEAD")
    assert not [s for s in pk.sections if s.context]  # the only real use is inside the hunk already shown
    assert "context_sections" not in pk.metadata and "unchanged" not in pk.summary


def test_a_name_used_everywhere_is_left_out_and_context_has_its_own_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    callers = {f"m{i}.py": f"from lib import shared_step\n\n\ndef use{i}():\n    return shared_step({i})\n" for i in range(4)}
    r = _repo(tmp_path, {"lib.py": "def shared_step(x):\n    return x\n", **callers})
    (r / "lib.py").write_text("def shared_step(x):\n    return x + 1\n")
    _git(r, "commit", "-q", "-am", "change")
    assert len([s for s in git_diff_packet(r, "HEAD~1..HEAD").sections if s.context]) == usages.USES_PER_NAME
    monkeypatch.setattr(usages, "TOO_COMMON", 3)
    assert not [s for s in git_diff_packet(r, "HEAD~1..HEAD").sections if s.context]
    monkeypatch.setattr(usages, "TOO_COMMON", 30)
    monkeypatch.setattr(usages, "CONTEXT_CHARS", 100)  # each use here is 67 characters
    assert len([s for s in git_diff_packet(r, "HEAD~1..HEAD").sections if s.context]) == 1


def test_context_is_best_effort(tmp_path: Path):
    files = parse_unified_diff("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n def compute_fee(x):\n-    return 1\n+    return 2\n")
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert context_sections(not_a_repo, files, "worktree") == ([], {"names": 0, "too_common": 0, "uses": 0})


@pytest.mark.parametrize("line,suffix,name", [
    ("def build_invoice(phases):", ".py", "build_invoice"),
    ("    async def fetch_rates(self):", ".py", "fetch_rates"),
    ("class Invoice(Base):", ".py", "Invoice"),
    ("export async function buildInvoice(phases: Phase[]): Promise<Invoice> {", ".ts", "buildInvoice"),
    ("export const buildInvoice = async (phases: Phase[]): Promise<Invoice> => {", ".ts", "buildInvoice"),
    ("const total = (xs) => xs.length", ".js", "total"),
    ("  private computeTotal(lines: Line[]): number {", ".ts", "computeTotal"),
    ("export default class InvoiceView extends Base {", ".tsx", "InvoiceView"),
    ("func (s *Server) ServeInvoice(w http.ResponseWriter, r *http.Request) {", ".go", "ServeInvoice"),
    ("pub(crate) fn total_due(&self) -> u64 {", ".rs", "total_due"),
    ("    public Invoice buildFor(List<Phase> phases) {", ".java", "buildFor"),
    ("fun totalDue(): Int {", ".kt", "totalDue"),
    ("  def self.from_phases(phases)", ".rb", "from_phases"),
    ("    public function totalDue() {", ".php", "totalDue"),
    ("  if (phase.paid) {", ".ts", None),
    ("  } else if (x) {", ".java", None),
    ("  for (const p of phases) {", ".ts", None),
    ("  describe('invoice', function () {", ".js", None),
    ("  return buildInvoice(phases);", ".ts", None),
])
def test_definitions_by_language(line: str, suffix: str, name: str | None):
    assert definition(line, suffix) == name


def test_touched_names_follow_the_definition_a_change_sits_in():
    diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
            "@@ -1,8 +1,9 @@\n"
            " def first_step(x):\n"
            "-    return x\n"
            "+    return x + 1\n"
            " \n"
            " \n"
            "+@cached\n"
            " def second_step(x):\n"
            "     return x\n"
            " \n"
            "-LIMIT = 3\n"
            "+LIMIT = 4\n"
            "@@ -40,3 +41,3 @@ def third_step(y):\n"
            "     y = y + 1\n"
            "-    return y\n"
            "+    return y * 2\n"
            "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
            "@@ -1,2 +1,2 @@\n def test_first_step():\n-    assert 1\n+    assert 2\n")
    found = {t.name: t.verb for t in touched_names(parse_unified_diff(diff))}
    # the decorator belongs to second_step, which did not change; LIMIT is outside every definition; tests define nothing others use
    assert found == {"first_step": "modifies", "third_step": "modifies"}


def test_nested_definitions_and_new_ones():
    # tools nested in a server builder: a change in one tool is that tool's; a new tool's decorator arguments are
    # nobody's but the builder's; the new tool itself is left out, since only the change can use it so far
    diff = ("diff --git a/server.py b/server.py\n--- a/server.py\n+++ b/server.py\n"
            "@@ -10,8 +10,14 @@ def build_server(workspace):\n"
            "     def ingest_notes(who):\n"
            "-        return who\n"
            "+        return who.strip()\n"
            " \n"
            "+    @server.tool(description=(\n"
            "+        \"Search the web on a budget.\"))\n"
            "+    def research_web(persona):\n"
            "+        return persona\n"
            "+\n"
            "     return server\n")
    found = {t.name: t.verb for t in touched_names(parse_unified_diff(diff))}
    assert found == {"ingest_notes": "modifies", "build_server": "modifies"}
    removed_then_added = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n"
                          "-def settle_invoice(a):\n-    return a\n+def settle_invoice(a, b):\n+    return a + b\n")
    assert [(t.name, t.verb) for t in touched_names(parse_unified_diff(removed_then_added))] == [("settle_invoice", "modifies")]


@pytest.mark.parametrize("line,used", [
    ("    invoice = build_invoice(phases)", True),
    ("    handlers = {'bill': build_invoice}", True),
    ("    run(build_invoice, phases)", True),
    ("    return build_invoice", True),
    ("    from_invoice = svc.build_invoice", True),
    ('    log("build_invoice failed")  ', False),
    ("    x = 1  # build_invoice is slow", False),
    ("    the panel calls build_invoice once per phase and", False),
    ('    help="what build_invoice returns"', False),
])
def test_only_code_counts_as_a_use(line: str, used: bool):
    from rncrn8.adapters.usages import code_use

    assert code_use(line, "build_invoice", ".py") is used


def test_prose_and_strings_that_name_a_function_are_not_shown(tmp_path: Path):
    lib = "def settle_invoice(x):\n    return x\n"
    doc = '"""How billing works.\n\nThe nightly job calls settle_invoice for each client and\nmails the result.\n"""\nLABEL = "settle_invoice"\n'
    r = _repo(tmp_path, {"lib.py": lib, "notes.py": doc, "job.py": "from lib import settle_invoice\n\n\ndef nightly(c):\n    return settle_invoice(c)\n"})
    (r / "lib.py").write_text(lib.replace("return x", "return x * 2"))
    _git(r, "commit", "-q", "-am", "change")
    assert [s.ref for s in git_diff_packet(r, "HEAD~1..HEAD").sections if s.context] == ["job.py:2-5"]


def test_saved_packets_without_the_flag_still_load():
    old = '{"kind": "code", "title": "t", "sections": [{"ref": "a.py:1", "text": "x"}]}'
    pk = ReviewPacket.model_validate_json(old)
    assert pk.sections[0].context is False and pk.reviewed_chars == pk.char_count == 1
    assert Section(ref="b.py:2", text="yy", context=True).context
