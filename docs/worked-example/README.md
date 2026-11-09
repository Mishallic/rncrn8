# Worked examples: a plan and a code change

These compare rncrn8 with a plain Claude Code review of the same material, all run on
2026-11-09 with `claude-opus-5-5`. Read the results before deciding whether rncrn8 is worth
its cost to you: on a single review, it was not clearly better.

Both examples follow one method. The author of rncrn8 wrote the material with six flaws planted
on purpose. rncrn8 reviewed it on a fresh project with empty memory and the default panel, with
debate and summaries. Separately, a Claude Code agent on the same model got only the material
and a one-line request, was not told about the planted flaws, and read nothing else. The
author then scored both, knowing which review was which, after reading the outputs. Every
output is in this folder, so you can check every cell. Both reviews also found real problems
that were not planted, so the planted list is a floor, not the whole truth.

"yes" below means a finding states it. "In a finding's reasoning" means it appears only in the
reasoning or fix of a finding about something else. "Notes only" means a reviewer said it in
its notes, which the report shows but which are not findings.

## Part 1: a business plan

- **The plan**: [plan.md](plan.md), 31 lines, a paid weekly email briefing for UK dental clinics.
- **rncrn8**: the default document panel (critic, skimmer, stranger, auditor). Report:
  [rncrn8-panel-review.md](rncrn8-panel-review.md). Cost $0.89, 2.3 minutes.
- **rncrn8, quick**: a second review right after the first, `--panel critic,auditor
  --no-debate --no-summaries`, with the first review's memories shown. Report:
  [rncrn8-quick-review.md](rncrn8-quick-review.md). Cost $0.21, 1.2 minutes.
- **Plain Claude Code**, asked: "Review this plan and tell me the problems that matter, with
  where each one is and how to fix it." Review:
  [plain-claude-code-review.md](plain-claude-code-review.md). The plan was saved as
  `clinic-newsletter-plan.md` at the time, which is the name that review shows.

| Planted flaw | rncrn8 panel | rncrn8 quick | Plain Claude Code |
|---|---|---|---|
| The funnel (20% open × 10% subscribe) gives at most 240 subscribers, not 600 | yes | yes | yes |
| Cold-emailing a bought list risks UK PECR/GDPR breaches and deliverability | yes | yes | yes, in more depth |
| Week 1 both hires the writer and sends the sample the writer must write | yes | yes | yes |
| 12,000 counts all UK practices, not the independent clinics targeted | yes | yes | yes |
| Break-even is 91, not 90, and ignores the annual price | yes | yes | yes |
| The risk section names one minor risk | yes | yes | yes |

| Point (not planted) | rncrn8 panel | rncrn8 quick | Plain Claude Code |
|---|---|---|---|
| 600 by month 3 contradicts 600 in the first year | yes | yes | yes |
| First-year revenue ignores the ramp, annual plans and churn | yes | yes | yes |
| VAT: the business passes the registration threshold | in a finding's reasoning (break-even) | no | yes, with the effect on break-even |
| Pre-written buffer issues go stale for a news briefing | in a finding's reasoning (risks); a separate finding on it was held | in a finding's reasoning (risks) | in a finding's reasoning (risks) |
| The 20% and 10% rates have no source | yes | yes | in a finding's reasoning (funnel) |
| UK has four dental regulators (CQC, HIS, HIW, RQIA) | no | no | yes |
| Liability if a regulatory summary is wrong; indemnity insurance | no | no | yes |
| Missing costs: email platform at 12,000 contacts, accounting, legal advice | partly (list cost, founder time) | partly | yes |
| Purchased lists are banned by most email platforms; domain warm-up | no | no | yes |
| What the plan does well | yes | notes only | no |
| No evidence anyone wants it (interviews, a waitlist) | notes only | no | no |

All three found all six planted flaws; the plain review raised more of the other real points.

## Part 2: a code change

- **The change**: a 74-line commit to a small invoicing tool (totals with VAT, customer search,
  paging, emailing invoices). Patches to rebuild the repository: [code/](code/); the planted
  flaws: [code/planted.md](code/planted.md).
- **rncrn8**: `rncrn8 review --diff HEAD~1..HEAD` with the default code panel (security,
  maintainer, tester, operator, auditor, historian). Report:
  [code/rncrn8-panel-review.md](code/rncrn8-panel-review.md). Cost $1.60, 2.3 minutes.
- **Plain Claude Code**, in its own copy of the repository, asked: "Review my last commit and
  tell me the problems that matter, with where each one is and how to fix it." It could read
  the repository and run code in it. Review:
  [code/plain-claude-code-review.md](code/plain-claude-code-review.md).

| Planted flaw | rncrn8 panel | Plain Claude Code |
|---|---|---|
| SQL injection: the customer search builds its query with an f-string | yes | yes, shown with a working input |
| Mutable default argument: every invoice without lines shares one list | yes | yes, shown by running it |
| Paging off by one: page 1 skips the newest 20 invoices | yes | yes, shown by running it |
| Money in floats, rounded with `round()` on a float (not a live bug here; see below) | yes, as a low-confidence question (belief 0.53) | no |
| A hard-coded SMTP password | yes | yes |
| A failed email is swallowed and the invoice still marked sent | yes | yes, shown by running it |

| Point (not planted) | rncrn8 panel | Plain Claude Code |
|---|---|---|
| No timeout on the SMTP connection | yes | in a finding's reasoning (failed email) |
| Crashes with an unhelpful error when the customer is missing | yes | yes |
| The new code is nearly untested | yes | yes, with the tests to add |
| A user's `%` or `_` acts as a wildcard in the search | in a finding's fix (SQL injection) | in a finding's reasoning (SQL injection) |
| The VAT rate is not recorded as a decision anywhere | yes | no |
| The customer's email goes into the To header unchecked | yes, nobody else took a position | no |
| The mailer cannot be tested without a real network call | yes, nobody else took a position | no |
| Saved invoices lose their lines, so they cannot be re-sent | held below threshold | no |
| The mailer runs its own SQL instead of going through db.py | held below threshold | no |

Both found the same five live bugs. The plain review checked four of them by running the code,
which the panel cannot do. The panel also raised the floats question, which the plain review
did not, but a later review showed that flaw was planted wrongly: with prices in whole pence and
VAT at 20%, a total never lands on a half penny, so this code cannot produce a wrong total today.
It is a fragile pattern, not a live bug, and counting it as a win for the panel would overstate
the result.

## What this shows, and what it does not

- **On one review, there is no clear winner.** On the plan the plain review raised more real
  points. On the code both found the same five live bugs; the panel added a few design points,
  and the plain review proved its findings by running them. This is one run of
  each, and model output varies between runs, so none of it is a ranking. The plain review also
  needs no API key of its own: it runs inside your Claude Code use. If one-off reviews are all
  you need, use your assistant directly.
- **What the panel adds on a single review is mostly in the report**: how many reviewers backed
  each point, which points were held below a threshold, notes on what each reviewer could not
  verify, and a decision id on every finding so you can record later whether it was right.
- **The quick second review of the plan was shown the first's memories.** What visibly changed
  is how it counts: each reviewer's earlier readings are counted once, not as fresh agreement
  ("past readings cited and counted once"). It found nothing new, and with two personas and no
  debate it missed the VAT point. One run cannot say whether memory helped.
- **What rncrn8 is for cannot show up in one review.** Its claim is that across many reviews,
  memory of earlier findings and the outcomes you record (which findings turned out right)
  make the panel better than a single reviewer: confidence that matches how often each reviewer
  is right, and more weight on the reviewers who are right more often. That claim is unproven.
  Measuring it needs real use with recorded outcomes over weeks, which is what the README asks
  for.

## How the costs were worked out

From the token counts in each report's "Panel health" section, at the list prices for
`claude-opus-5-5` on 2026-11-09, per million tokens: $4 input, $20 output, $5 for a 5-minute
cache write, $0.20 for a cache read (Anthropic prices cache reads on this model at 0.05 times
input; see https://platform.claude.com/docs/en/about-claude/pricing). The plain reviews' costs
were not measured.

| Review | Input | Output | Cache written | Cache read | Cost | Time |
|---|---|---|---|---|---|---|
| Plan, rncrn8 panel (4 personas) | 41,675 | 33,336 | 10,259 | 24,770 | $0.89 | 2.3 min |
| Plan, rncrn8 quick (2 personas, no debate) | 134 | 7,784 | 10,916 | 2,838 | $0.21 | 1.2 min |
| Code, rncrn8 panel (6 personas, 74-line diff) | 89,599 | 57,628 | 15,865 | 55,583 | $1.60 | 2.3 min |
| Code, rncrn8 panel (6 personas, 166-line diff) | 95,200 | 74,736 | 22,018 | 131,294 | $2.01 | 3.2 min |

The last row is a review of an earlier rncrn8 commit (144 lines added and 22 removed across 7
files), measured the same way. Its report is not in this folder, because it reviews code from
before the public history began.

## Reproducing them

You need Python 3.11 or newer, `pip install rncrn8`, git, and an Anthropic API key in
`RNCRN8_API_KEY` (see the README; a Claude subscription does not cover API calls). Run each
line on its own, in bash or PowerShell.

The plan, after saving `plan.md` from this folder into a new, empty folder:

```bash
rncrn8 init --no-connect
```

```bash
rncrn8 review plan.md --title "Clinic briefing plan"
```

```bash
rncrn8 review plan.md --title "Clinic briefing plan (quick)" --panel critic,auditor --no-debate --no-summaries
```

The code change, after saving the two `.patch` files from `code/` into a new, empty folder:

```bash
git init
```

```bash
git am 0001-Customers-and-invoice-lines.patch 0002-Invoices-totals-with-VAT-search-paging-and-emailing.patch
```

```bash
rncrn8 init --no-connect
```

```bash
rncrn8 review --diff HEAD~1..HEAD
```

Model output varies from run to run, so your findings will differ in wording and sometimes in
substance.
