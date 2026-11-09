"""A review where personas fail must say so, never read like a clean review."""

from rncrn8.models import DryRunAdapter, ModelError, ModelRequest
from rncrn8.packet import packet_from_text
from rncrn8.review import run_review
from rncrn8.review.digest import digest
from rncrn8.review.report import render_report

PACKET = packet_from_text("# Plan\n\nShip it.\n", "plan")
FINDING = {"understood": "", "notes": "", "prior_findings": [], "findings": [
    {"kind": "problem", "claim": "x", "where": "§1", "rationale": "", "fix": "", "confidence": 0.7, "supports": [], "opposes": []}]}


def _out_of_credit(request: ModelRequest):
    raise ModelError(f"{request.label}: the Anthropic account behind this key has run out of credit")


def test_a_review_where_every_persona_failed_says_so(ws):
    out = run_review(ws, PACKET, DryRunAdapter(_out_of_credit), panel=["critic", "skimmer"], debate=False, summaries=False)
    assert out.review.all_failed()
    report = render_report(out.review)
    assert "REVIEW FAILED: no persona could run" in report and "run out of credit" in report
    assert "Nothing crossed a persona's threshold" not in report
    assert "review_failed" in digest(out.review)


def test_a_partly_failed_review_names_who_is_missing(ws):
    def script(request: ModelRequest):
        if "skimmer" in (request.label or ""):
            raise ModelError("skimmer: rate limited")
        return FINDING

    out = run_review(ws, PACKET, DryRunAdapter(script), panel=["critic", "skimmer"], debate=False, summaries=False)
    assert not out.review.all_failed() and [p.persona for p in out.review.failed()] == ["skimmer"]
    assert "1 of 2 personas failed and are missing from this review (skimmer)" in render_report(out.review)
    assert "review_failed" not in digest(out.review)
