"""Where an outcome came from, and whether it may teach the track record.

One rule decides it, so calibration, panel weights, memory and origin trust, and replay's test set
can never disagree about which outcomes count. Only outcomes a person stands behind teach:

- ``explicit``: the person said the finding was right or wrong.
- ``check``: the assistant ran a check (a test, a reproduction) and showed the person the result.

Everything else is recorded and shown, but teaches nothing:

- ``loop``: recorded inside an automated loop or by an agent, with nobody confirming it. A fix loop edits
  exactly the lines a finding cited, so its outcomes would credit the panel for being obeyed.
- ``backtest``: scored against history by a harness.
- ``implicit:<review>``: a persona graded its own earlier finding, addressed or wrong. It is the persona marking itself.
- ``implicit:commit:<sha>``: a later commit touched the cited lines. It can only ever confirm, and churn is not confirmation.
"""

from __future__ import annotations

EXPLICIT = "explicit"
CHECK = "check"
LOOP = "loop"
BACKTEST = "backtest"
IMPLICIT = "implicit"

TEACHING = frozenset({EXPLICIT, CHECK})

# what the MCP tools and the CLI accept, mapped to what is stored
FROM_CALLER = {"user": EXPLICIT, "check": CHECK, "loop": LOOP}


def normalise(source: str | None) -> str:
    """A record written before sources existed was recorded by a person."""
    return source or EXPLICIT


def teaches(source: str | None) -> bool:
    """True only for outcomes a person stands behind."""
    return normalise(source) in TEACHING


def describe(source: str | None) -> str:
    """How the outcome was reached, in words, for a persona's prompt and for status."""
    s = normalise(source)
    if s.startswith(f"{IMPLICIT}:commit:"):
        return f"the cited lines changed in commit {s.rsplit(':', 1)[1]}"
    if s.startswith(f"{IMPLICIT}:"):
        return f"inferred from your own report in review {s.split(':', 1)[1]}"
    if s == CHECK:
        return "shown by a check a person saw"
    if s == LOOP:
        return "recorded by an automated loop, not confirmed by a person"
    if s == BACKTEST:
        return "scored by a backtest against history"
    return "recorded by a person"
