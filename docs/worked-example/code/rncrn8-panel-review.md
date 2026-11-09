# Review r-1a1c02a7bca733d50dd28b2: Invoicing: totals, search, paging, emailing
kind: code · panel: auditor, historian, maintainer, operator, security, tester · model: claude-opus-5-5 · review + debate + summaries updated · 2026-11-09 11:56 UTC

## Agreed (11)
_raised, backed by at least one other reviewer, nobody disagreeing_
1. [auditor:F3] invoicing/invoice.py:16-18 · problem: Invoice uses a mutable default `lines: list = []`, so every Invoice created without explicit lines shares one list, and lines added to one invoice appear on all the others.
   belief 1.00 (round 1: 0.95) vs threshold 0.25 · severity 3.00
   why: Python evaluates the default once, at function definition. `add_line` appends to `self.lines`, which mutates the shared default. Totals saved by save_invoice would then include other customers' lines. The single test only creates one Invoice, so it cannot catch this.
   fix: Use `lines: list | None = None` and `self.lines = list(lines) if lines else []`. Add a test that creates two invoices and checks they are independent.
   evidence: own reading 0.95 (+1.20), historian agrees 0.95 (+1.20), maintainer agrees 0.95 (+1.20), operator agrees 0.95 (+1.20), tester agrees 0.95 (+1.20), security agrees 0.94 (+1.12)
   decision: d-1a1c02b49571148c32b6826  (record what happened with `rncrn8 outcome d-1a1c02b49571148c32b6826 --confirmed|--refuted`)
   also raised as: historian:F3, maintainer:F3, operator:F6, security:F4, tester:F1 (merged into this one)
   historian agrees (0.95): Identical to auditor:F3: the mutable default list at invoice.py:16-18.
   maintainer agrees (0.95): Identical mutable-default finding at the same lines.
   operator agrees (0.95): Same lines and same mutable-default point. The long-running-worker consequence is implied by the merged finding's 'a second invoice in production will accumulate lines'.
   security agrees (0.94): Same lines and same point as auditor:F3: the mutable default list is shared across Invoice instances. My framing as cross-customer data exposure is a consequence of the same bug, not a separate finding.
   tester agrees (0.95): Same lines and same point: the mutable default list is shared across Invoice instances.
2. [auditor:F1] invoicing/db.py:24 · problem: find_customers builds its SQL by f-string interpolation of the user-supplied search term, so any name containing a quote breaks the query and a crafted one can inject arbitrary SQL.
   belief 1.00 (round 1: 0.95) vs threshold 0.25 · severity 3.00
   why: The docstring says the value comes from the search box, which is untrusted input. Every other query in this file and in mailer.py uses `?` placeholders, so this one is inconsistent with the file's own convention.
   fix: Use `conn.execute("SELECT * FROM customers WHERE name LIKE ?", (f"%{name}%",))`, and escape `%` and `_` in `name` if literal matching matters.
   evidence: own reading 0.95 (+1.20), historian agrees 0.95 (+1.20), maintainer agrees 0.95 (+1.20), tester agrees 0.95 (+1.20), security agrees 0.93 (+1.06), operator agrees 0.92 (+1.00)
   decision: d-1a1c02b4950572afee4a0e6  (record what happened with `rncrn8 outcome d-1a1c02b4950572afee4a0e6 --confirmed|--refuted`)
   also raised as: historian:F1, maintainer:F1, operator:F4, security:F1, tester:F3 (merged into this one)
   historian agrees (0.95): Same lines and same point as auditor:F1: f-string interpolation of search-box input at db.py:24.
   maintainer agrees (0.95): Same lines, same point: interpolated search input, quote breakage and injection, outlier against the `?` convention.
   operator agrees (0.92): Same line and same point: f-string SQL injection that also breaks on an apostrophe.
   security agrees (0.93): Same location and same point as auditor:F1: untrusted search-box input is f-string interpolated into SQL. My extra detail is a correction rather than an addition: execute() allows only one statement, so the realistic impact is UNION/boolean reads, not alteration.
   tester agrees (0.95): Same interpolated LIKE query, with the same quote-breaks and injection consequences.
3. [auditor:F4] invoicing/mailer.py:19-28 · problem: send_invoice swallows every exception from SMTP and then unconditionally marks the invoice sent in memory and in the database, so failed deliveries are recorded as sent.
   belief 1.00 (round 1: 0.92) vs threshold 0.25 · severity 2.99
   why: The docstring promises 'Email the invoice ... and mark it sent', but the code marks it sent whether or not the email went. `except Exception: pass` also hides login, TLS and connection failures. The persisted `sent` flag is therefore not evidence that anything was delivered.
   fix: Only set `sent` after `send_message` succeeds. Let the exception propagate, or log it and re-raise. Do not mark the invoice sent on failure.
   evidence: operator agrees 0.93 (+1.06), own reading 0.92 (+1.00), historian agrees 0.92 (+1.00), tester agrees 0.92 (+1.00), maintainer agrees 0.90 (+0.90), security agrees 0.90 (+0.90)
   decision: d-1a1c02b495b1b0ffadb2213  (record what happened with `rncrn8 outcome d-1a1c02b495b1b0ffadb2213 --confirmed|--refuted`)
   also raised as: historian:F5, maintainer:F4, operator:F1, security:F3, tester:F4 (merged into this one)
   historian agrees (0.92): Same lines (19-28) and same point as auditor:F4: exceptions are swallowed, then the invoice is marked sent unconditionally.
   maintainer agrees (0.90): Identical: the broad except followed by an unconditional sent=1.
   operator agrees (0.93): This makes the same point at the same location: the exception is swallowed and the invoice is still marked sent, so it is never retried. My extra emphasis is the missing log signal, which the merged finding also notes by saying failures are masked and never retried.
   security agrees (0.90): Same lines and same point as auditor:F4: every exception is swallowed, then the invoice is unconditionally marked sent. The panel is unanimous and I have no counter-evidence.
   tester agrees (0.92): Same swallowed exception followed by an unconditional sent=1, on the same lines.
4. [auditor:F2] invoicing/db.py:28-30 · problem: list_invoices contradicts its own docstring: with 'Pages start at 1', page=1 returns rows 20-39, so the newest 20 invoices are never shown.
   belief 0.99 (round 1: 0.90) vs threshold 0.25 · severity 2.98
   why: The slice is `rows[page*page_size:(page+1)*page_size]`, which is zero-based indexing. The docstring states a 1-based contract. A caller following the docstring loses the first page. The function also fetches the whole table on every call, so cost grows with total invoices rather than page size.
   fix: Use `LIMIT ? OFFSET ?` with an offset of `(page - 1) * page_size`, and validate page >= 1. Add a test for page 1.
   evidence: own reading 0.90 (+0.90), historian agrees 0.90 (+0.90), maintainer agrees 0.90 (+0.90), tester agrees 0.90 (+0.90), operator agrees 0.88 (+0.81), security agrees 0.87 (+0.78)
   decision: d-1a1c02b4954ae359acb3db4  (record what happened with `rncrn8 outcome d-1a1c02b4954ae359acb3db4 --confirmed|--refuted`)
   also raised as: historian:F2, maintainer:F2, operator:F5, security:F5, tester:F2 (merged into this one)
   historian agrees (0.90): Same lines (28-30) and same point as auditor:F2: a 0-based slice against a 1-based docstring, plus fetching the whole table.
   maintainer agrees (0.90): Identical point: the docstring says 1-based, the slice is 0-based, and the whole table is fetched.
   operator agrees (0.88): maintainer:F2 covers both halves of my point, the off-by-one against the docstring and the full-table fetch, on the same lines.
   security agrees (0.87): Same lines and same two points as maintainer:F2: the 0-based slice contradicts the 1-based docstring, and the whole table is fetched per call. My 'resource-exhaustion vector' wording is the security angle on the same fact.
   tester agrees (0.90): Same off-by-one against the 1-based docstring and the same full-table fetch, on the same lines.
5. [operator:F3] invoicing/mailer.py:20 · risk: smtplib.SMTP is opened with no timeout, so an unresponsive mail relay can hang the calling request or worker indefinitely.
   belief 0.91 (round 1: 0.75) vs threshold 0.25 · severity 2.72
   why: smtplib.SMTP(host, port) uses the global socket default timeout, which is None unless set elsewhere. A blackholed relay blocks the thread forever. The broad except never fires because nothing raises.
   fix: Pass timeout=, e.g. smtplib.SMTP(SMTP_HOST, 587, timeout=10). Consider sending from a background job instead of inline.
   evidence: own reading 0.75 (+0.45), historian agrees 0.72 (+0.39), security agrees 0.72 (+0.39), auditor agrees 0.70 (+0.35), maintainer agrees 0.70 (+0.35), tester agrees 0.70 (+0.35)
   decision: d-1a1c02b49a389a90eba9143  (record what happened with `rncrn8 outcome d-1a1c02b49a389a90eba9143 --confirmed|--refuted`)
   auditor agrees (0.70): smtplib.SMTP(SMTP_HOST, 587) passes no timeout, so it falls back to socket's global default, which is None unless set.
      strongest objection: socket.setdefaulttimeout might be set at application startup, outside this diff.
   historian agrees (0.72): smtplib.SMTP(host, 587) with no timeout argument falls back to the global socket default, which is None unless set elsewhere.
      strongest objection: The application might call socket.setdefaulttimeout at startup, or run sends in a background job where a hang is less harmful. Neither is visible.
   maintainer agrees (0.70): smtplib.SMTP uses the global socket default timeout, which is None unless set elsewhere.
      strongest objection: The application might set socket.setdefaulttimeout at startup, or run sends in a background job where a hang is less visible to users; neither is visible here.
   security agrees (0.72): smtplib.SMTP(host, 587) with no timeout uses the global socket default, which is None unless set elsewhere. A relay that accepts the connection and then stalls blocks the worker indefinitely. This is a fail mode I missed.
      strongest objection: A process-wide socket.setdefaulttimeout may be set elsewhere. An unreachable host fails at the OS connect timeout rather than hanging forever, so only a stalling peer hangs indefinitely.
   tester agrees (0.70): smtplib.SMTP(host, 587) with no timeout argument uses the socket default, which is None unless set globally. A blackholed host blocks indefinitely and the except never runs.
      strongest objection: An application might set socket.setdefaulttimeout at startup, which this diff would not show. In practice many networks also reject rather than blackhole, which fails fast.
6. [tester:F7] tests/test_invoice.py:4-7 · problem: The single test asserts only the simplest VAT case and would still pass with the mutable-default, rounding-at-the-penny, and empty-invoice behaviours all broken or untested.
   belief 0.97 (round 1: 0.85) vs threshold 0.33 · severity 1.94
   why: 2 × 10.0 × 1.2 does not exercise the rounding to the penny. There is no case for an empty invoice, a fractional price such as 0.10 × 3, or two invoices. The new code in db.py and mailer.py has no tests at all.
   fix: Add tests for:
- an empty invoice (total 0)
- a rounding case such as 3 × 0.10
- two independent invoices
- paging at page 1 and at a boundary
- search with a quote
- mailer failure with a mocked SMTP
   evidence: own reading 0.85 (+0.71), auditor agrees 0.80 (+0.57), historian agrees 0.80 (+0.57), maintainer agrees 0.80 (+0.57), operator agrees 0.80 (+0.57), security agrees 0.78 (+0.52)
   decision: d-1a1c02b49df918f171661ff  (record what happened with `rncrn8 outcome d-1a1c02b49df918f171661ff --confirmed|--refuted`)
   auditor agrees (0.80): 2×10×1.2 never exercises penny rounding, and db and mailer have no tests.
      strongest objection: Other test files may exist outside this diff.
   historian agrees (0.80): 2 × 10.0 × 1.2 = 24.0 exactly, so no rounding or cross-invoice case is exercised.
      strongest objection: It overlaps with operator:F7. The 'rounding broken' part presumes a rounding defect that is itself unproven.
   maintainer agrees (0.80): 2×10×1.2 exercises no rounding, and there are no tests for db or mailer.
      strongest objection: A thin test is a gap, not a defect in itself; calling it a 'problem' rather than a 'risk' is arguable.
   operator agrees (0.80): There is one trivial case, and no db or mailer tests.
      strongest objection: 20 × 1.2 in float does pass through round(), so rounding is touched minimally. Thin coverage is a risk rather than a defect.
   security agrees (0.78): 2×10×1.2 is exact, so penny rounding is never exercised. db and mailer have no tests.
      strongest objection: The finding calls the rounding behaviour 'broken'. Nothing shows it is broken; only that it is untested.
7. [auditor:F6] invoicing/invoice.py:5 · risk: VAT_RATE = 0.20 is presented as a settled business rule, but no decision in the shared world records the rate, which jurisdiction it covers, or whether one flat rate applies to all lines.
   belief 0.58 (round 1: 0.60) vs threshold 0.25 · severity 1.73
   why: The shared world's Decisions and Constraints sections are empty, so the 20% figure rests only on this line. The '£' in mailer.py:18 suggests UK standard rate, but zero-rated or reduced-rate items would be overcharged. Every stored total inherits the assumption, because save_invoice persists `total()`. total() also rounds floats with Python's round(), which uses banker's rounding on binary floats; that may not match accounting rounding at half-penny boundaries.
   fix: Record the VAT decision (rate, scope, per-line versus per-invoice) in the shared world. Consider Decimal with ROUND_HALF_UP for money.
   evidence: own reading 0.60 (+0.20), historian agrees 0.55 (+0.10), maintainer agrees 0.50 (+0.00), tester agrees 0.50 (+0.00)
   decision: d-1a1c02b4963efdf52b9ab1b  (record what happened with `rncrn8 outcome d-1a1c02b4963efdf52b9ab1b --confirmed|--refuted`)
   historian agrees (0.55): The shared world records no VAT decision, and a single flat rate is applied to every line.
      strongest objection: 20% is the UK standard rate, and the £ sign points to the UK. A small shop selling only standard-rated goods may be correctly served by a flat rate.
   maintainer agrees (0.50): A flat module constant hides a business rule, and the shared world records no decision on it.
      strongest objection: A single standard rate is a reasonable first cut for a small UK shop, and a constant is the simplest way to change it later; this may simply be unrecorded rather than wrong.
   operator is unsure (0.45): A flat 20% is plausibly the UK standard rate, and the shared world is empty on every topic, not only this one.
      strongest objection: A named constant for the standard rate is a reasonable default. Mixed-rate lines are a feature request, not a defect in this diff.
   security is unsure (0.50): This is a business-rule question I cannot judge from the code. It has no security impact.
      strongest objection: 20% is the UK standard rate, and the £ sign points to the UK. A shop selling only standard-rated goods would be correct with a flat rate.
   tester agrees (0.50): No recorded decision backs 20% or one flat rate. Reduced-rate lines cannot be represented in Line.
      strongest objection: A flat 20% is the UK standard rate and may be entirely correct for this shop. The shared world being empty is not evidence the rate is wrong, so this is closer to a question than a risk.
8. [historian:F4] invoicing/mailer.py:6-8 · problem: An SMTP password is committed in source, so it is now in git history and must be treated as leaked.
   belief 0.99 (round 1: 0.90) vs threshold 0.40 · severity 1.49
   why: SMTP_PASSWORD is a literal string in a committed file. Removing it in a later commit does not remove it from history.
   fix: Rotate the credential and load host, user and password from environment or secret config.
   evidence: own reading 0.90 (+0.90), maintainer agrees 0.90 (+0.90), operator agrees 0.90 (+0.90), tester agrees 0.90 (+0.90), auditor agrees 0.88 (+0.81), security agrees 0.87 (+0.78)
   decision: d-1a1c02b4975038b3e5297f2  (record what happened with `rncrn8 outcome d-1a1c02b4975038b3e5297f2 --confirmed|--refuted`)
   also raised as: auditor:F5, maintainer:F5, operator:F2, security:F2, tester:F5 (merged into this one)
   auditor agrees (0.88): Same lines and same point as security:F2: a literal password is committed to history.
   maintainer agrees (0.90): Identical committed-credential finding.
   operator agrees (0.90): maintainer:F5 makes the same point at the same lines, including the rotation and per-environment angle I raised.
   security agrees (0.87): Same point as historian:F4: a committed password in git history. I lowered confidence slightly. auditor:F5 raised the placeholder possibility, and the host smtp.shopmail.example is in the RFC 2606 reserved .example TLD, so this may be a sample value. The practice is still the defect.
   tester agrees (0.90): Same hardcoded committed credential.
9. [historian:F6] tests/test_invoice.py:4-7 · risk: The only test exercises a single fresh Invoice, so it cannot catch the shared-default-list bug or the paging off-by-one, and the mailer and db changes are untested.
   belief 0.96 (round 1: 0.80) vs threshold 0.40 · severity 1.45
   why: The test passes only because it is the first Invoice in the process to use the default. A second test using the default would see the earlier lines and fail.
   fix: Add tests for two independent invoices, for list_invoices page 1, for find_customers with a quote in the name, and for mailer failure handling with a mocked SMTP.
   evidence: own reading 0.80 (+0.57), auditor agrees 0.80 (+0.57), maintainer agrees 0.80 (+0.57), security agrees 0.80 (+0.57), tester agrees 0.80 (+0.57), operator agrees 0.75 (+0.45)
   decision: d-1a1c02b497cacafd9705b7b  (record what happened with `rncrn8 outcome d-1a1c02b497cacafd9705b7b --confirmed|--refuted`)
   also raised as: operator:F7 (merged into this one)
   operator agrees (0.75): historian:F6 makes the same point on the same lines: the single fresh Invoice cannot catch the shared default, and the db and mailer changes are untested.
   auditor agrees (0.80): The test constructs one Invoice. No tests exist for db.py or mailer.py.
      strongest objection: Other test files outside the diff might cover db and mailer. The packet only shows the files that were added.
   maintainer agrees (0.80): One test covers one invoice, and db.py and mailer.py have no tests.
      strongest objection: Test coverage might be planned for a follow-up commit, and a thin first test is not itself a defect.
   security agrees (0.80): The only test builds one Invoice. db.py and mailer.py have no tests.
      strongest objection: Test coverage for the db and mailer code may be planned for a later commit. That would be scope, not a defect in this change.
   tester agrees (0.80): One test, one fresh Invoice, and no tests for the db or mailer code.
      strongest objection: The test is not wrong in what it asserts. It is just insufficient, so 'risk' rather than 'problem' is the right label.
10. [tester:F6] invoicing/mailer.py:13-15 · risk: send_invoice raises TypeError when invoice.customer_id has no matching customer row, and it also requires the connection's row_factory to be sqlite3.Row; neither is checked or tested.
   belief 0.64 (round 1: 0.60) vs threshold 0.33 · severity 1.28
   why: `fetchone()` returns None for a missing customer, so `customer["email"]` fails. On a connection without the Row factory, indexing by the string "email" also fails. I cannot see from this diff whether the row factory is set elsewhere.
   fix: Check for a missing customer and raise a clear error. Add mailer tests against an in-memory database with SMTP mocked.
   evidence: own reading 0.60 (+0.17), auditor agrees 0.60 (+0.17), historian agrees 0.55 (+0.08), operator agrees 0.55 (+0.08), security agrees 0.55 (+0.08), maintainer agrees 0.50 (+0.00)
   decision: d-1a1c02b49db44dc9d1feeb8  (record what happened with `rncrn8 outcome d-1a1c02b49db44dc9d1feeb8 --confirmed|--refuted`)
   also raised as: auditor:F7 (merged into this one)
   auditor agrees (0.60): tester:F6 makes the same point at the same lines: fetchone() returns None, and the subscript then raises TypeError. It also adds the row_factory dependency.
   historian agrees (0.55): A None customer causes a TypeError. Without a Row factory, string indexing fails.
      strongest objection: The return annotation on find_customers (list[sqlite3.Row]) suggests the row factory is set elsewhere, which weakens the second half of the claim.
   maintainer agrees (0.50): The None-customer part is right.
      strongest objection: db.py annotates its returns as list[sqlite3.Row], which strongly suggests the connection already sets row_factory, so the second half is likely a non-issue.
   operator agrees (0.55): The missing-customer TypeError is real. The row_factory dependency is an undocumented environment requirement.
      strongest objection: db.py annotates its returns as sqlite3.Row, which suggests the connection factory sets row_factory elsewhere. A missing customer fails loudly, which is the safe outcome.
   security agrees (0.55): The None subscript is certain for a missing row.
      strongest objection: The row_factory may be set where the connection is created, outside the diff. A missing customer fails closed before the invoice is marked sent.
11. [historian:F7] invoicing/invoice.py:24-29 · question: Money is computed in float and rounded once on the VAT-inclusive total, which may give penny discrepancies against per-line VAT or accounting expectations.
   belief 0.53 (round 1: 0.50) vs threshold 0.40 · severity 0.79
   why: round() on binary floats can mis-round half-pennies, for example values like x.xx5. Whether this matters depends on how invoices are reconciled, which the packet does not say.
   fix: Consider Decimal with ROUND_HALF_UP and document whether VAT is computed per line or per invoice.
   evidence: maintainer agrees 0.55 (+0.12), own reading 0.50 (+0.00), auditor agrees 0.50 (+0.00)
   decision: d-1a1c02b4980dc61d351789c  (record what happened with `rncrn8 outcome d-1a1c02b4980dc61d351789c --confirmed|--refuted`)
   also raised as: maintainer:F6 (merged into this one)
   maintainer agrees (0.55): historian:F7 makes the same float and round-once-on-total point at the same lines. Theirs is framed as a question, which fits better because the rounding rule is a business decision.
   auditor agrees (0.50): Money is held as float, and there is a single round() on the VAT-inclusive total.
      strongest objection: For typical two-decimal prices at 20%, discrepancies are rare, and rounding on the total may be the intended rule.
   operator is unsure (0.45): Float rounding can mis-round half-pennies, but whether that matters depends on how invoices are reconciled, and the packet does not show that.
      strongest objection: For a small shop the discrepancy is at most a penny per invoice. Rounding once on the total is a legitimate convention.
   security is unsure (0.45): A float rounding discrepancy is plausible, but its impact depends on unstated reconciliation rules.
      strongest objection: For typical 2-decimal prices, the error after round(...,2) is rarely visible. Whether a single total-level rounding is acceptable is a business decision.
   tester is unsure (0.50): Python's round() acts on the binary value, so a decimal half such as 2.675 rounds to 2.67. Whether per-line or per-invoice VAT is wanted is a business rule nobody has recorded.
      strongest objection: Rounding once on the total is a legitimate and common method. For typical prices the float error is far below a penny before the single round, so practical discrepancies may be rare.

## Disputed (0)
_another reviewer disagreed; the disagreement is left standing for you_
None.

## Unchallenged (2)
_raised, no other reviewer took a position_
1. [security:F6] invoicing/mailer.py:15 · risk: Customer email from the DB is placed in the To header without validation, so a malformed or multi-address email value could send an invoice to an unintended recipient.
   belief 0.40 (round 1: 0.45) vs threshold 0.20 · severity 1.60
   why: add_customer accepts any email string. EmailMessage rejects CR/LF header injection in modern Python, but a comma-separated value would add extra recipients and leak invoice data.
   fix: Validate email format at add_customer and parse with email.utils before sending.
   evidence: own reading, counts against 0.40 (-0.41)
   decision: d-1a1c02b49c6d6a559be33e3  (record what happened with `rncrn8 outcome d-1a1c02b49c6d6a559be33e3 --confirmed|--refuted`)
   auditor is unsure (0.40): EmailMessage would accept a comma-separated To value, but the email is the customer's own stored address.
      strongest objection: Whoever enters the customer email already decides the recipient, so a 'wrong recipient' needs bad data entry rather than an attack.
   historian is unsure (0.45): A comma-separated email value in To would add recipients. I can't judge whether customer emails are user-supplied.
      strongest objection: Emails are entered via add_customer, likely by staff. Without a self-service path, this needs a malicious or careless insider.
   maintainer is unsure (0.40): A comma-separated email would add recipients, but customer emails are presumably entered by staff via add_customer, not by attackers.
      strongest objection: Customer email values come from the shop's own data entry, so this is a data-quality issue more than a security one, and validation belongs at add_customer, not here.
   operator is unsure (0.40): A comma in the stored email would add recipients, but that requires bad data entered via add_customer.
      strongest objection: Customer emails are likely entered by staff. Modern EmailMessage blocks CR/LF injection.
   tester is unsure (0.40): A comma-separated email stored via add_customer would add recipients, and nothing validates it.
      strongest objection: Customer emails are probably entered by the shop itself rather than an attacker. Modern EmailMessage blocks CR/LF injection, so the remaining risk depends on who can write customers, which the packet does not show.
   security holds at 0.40 (was 0.45): No other reviewer addressed it, so nothing changes my position. The objection I see myself: if customers set their own email, a comma-separated value only sends their own invoice to addresses they chose, which is low impact. The risk matters only if staff entry or import errors can misdirect invoices. I keep it as a low-confidence risk.
2. [tester:F8] invoicing/mailer.py:20-25 · risk: No test can exercise send_invoice without a real network call, because the SMTP host and port are module constants and smtplib.SMTP is constructed inline. Its failure path (except/pass, then sent=1) is therefore untested and hard to test.
   belief 0.60 vs threshold 0.33 · severity 1.20
   added in the debate, prompted by operator:F3
   why: operator:F3's no-timeout point made me look at how a test would drive the failure path. The only seams are monkeypatching smtplib.SMTP or the module globals. Without that, a test either hangs or swallows the error, so the 'marks sent on failure' bug cannot be pinned by an assertion as written.
   fix: Inject an SMTP factory or client, with a timeout, into send_invoice. Then add a test where the factory raises and assert that sent stays 0 and the error propagates.
   evidence: own reading 0.60 (+0.41)
   decision: d-1a1c02c66a8f955e4f08505  (record what happened with `rncrn8 outcome d-1a1c02c66a8f955e4f08505 --confirmed|--refuted`)

## Held (2)
_below the owner's threshold after all evidence_
- [maintainer:F8] invoicing/mailer.py:13-28 · risk: mailer.py issues its own SQL against customers and invoices (lines 13 and 27) instead of going through db.py, so persistence logic now lives in two modules and depends on connection details such as row_factory that db.py owns.
   belief 0.60 vs threshold 0.60 · severity 0.60
   added in the debate, prompted by tester:F6, auditor:F7
   why: db.py is the repository's data-access layer: add_customer, find_customers, list_invoices and save_invoice all live there. The mailer adds a customer lookup and a mark-sent UPDATE inline. The next reader looking for where 'sent' is written, or why customer lookups fail on a plain connection, will not find it in db.py.
   fix: Add get_customer(conn, id) and mark_invoice_sent(conn, invoice_id) to db.py and call them from send_invoice. Keep mailer.py about composing and sending email.
   evidence: own reading 0.60 (+0.41)
   decision: d-1a1c02c66a44c4fdf4da272  (record what happened with `rncrn8 outcome d-1a1c02c66a44c4fdf4da272 --confirmed|--refuted`)
- [maintainer:F7] invoicing/db.py:33-39 · risk: save_invoice persists only customer_id, total and sent, so invoice lines are lost once the object goes away. A saved invoice cannot be reloaded or re-emailed with its details.
   belief 0.50 vs threshold 0.60 · severity 0.50
   why: Mailer takes the in-memory invoice plus a separate invoice_id because the stored row cannot reconstruct the invoice. That coupling will confuse the next reader. It may be intentional for this stage, but nothing says so.
   fix: Add a lines table and persist the lines, or add a comment stating that only the total is stored by design.
   evidence: security agrees 0.55 (+0.08), auditor agrees, counts against 0.45 (-0.08), own reading 0.50 (+0.00), historian agrees 0.50 (+0.00), operator agrees 0.50 (+0.00), tester agrees 0.50 (+0.00)
   decision: d-1a1c02b49995fa29f6cbcc3  (record what happened with `rncrn8 outcome d-1a1c02b49995fa29f6cbcc3 --confirmed|--refuted`)
   auditor agrees (0.45): save_invoice inserts only customer_id, total and sent. The mailer needs the in-memory invoice plus a separate id.
      strongest objection: The schema is not shown. A lines table may exist and be written elsewhere, or this staging may be deliberate.
   historian agrees (0.50): save_invoice inserts only customer_id, total and sent, and the mailer needs the in-memory object.
      strongest objection: A lines table may exist in a schema not shown, or this may be a deliberate first stage.
   operator agrees (0.50): Only customer_id, total and sent are stored, so the lines are gone once the object is discarded.
      strongest objection: The mailer only uses customer_id and total(), both of which are persisted. A retry job could re-send from the stored row, so the operational impact is smaller than implied.
   security agrees (0.55): save_invoice inserts only customer_id, total and sent, so the lines are not persisted by this code.
      strongest objection: A separate lines table and persistence path may exist outside the diff. This may also be intentional staging.
   tester agrees (0.50): save_invoice persists only the total, so a reloaded invoice cannot be emailed with its lines. That is why mailer needs the in-memory object.
      strongest objection: There may be a lines table or a later commit planned. The title scopes this change to totals, so it may be a deliberate staging step rather than a defect.

## Dropped (0)
_conceded by the reviewer who raised it_
None.

## Merged (28)
_the same point raised by another reviewer; folded in as agreement_
- [security:F3] -> [auditor:F4]: send_invoice swallows every SMTP exception and still marks the invoice sent in memory and in the DB, so delivery failures are recorded as success and never retried.
- [security:F2] -> [historian:F4]: The SMTP password is hardcoded in source, so it will be committed to git history and readable by anyone with repo access.
- [security:F4] -> [auditor:F3]: Invoice uses a mutable default `lines: list = []`, so every Invoice created without explicit lines shares one list, and one customer's lines leak into another customer's invoice and total.
- [security:F1] -> [auditor:F1]: find_customers builds SQL by f-string interpolation of the search-box `name`, so input like `' OR 1=1 --` or a UNION SELECT can dump or alter arbitrary tables.
- [security:F5] -> [auditor:F2]: list_invoices treats page as 0-based despite its docstring saying pages start at 1, so page 1 skips the newest 20 invoices; it also loads every invoice into memory on each call.
- [operator:F6] -> [auditor:F3]: Invoice uses a mutable default `lines=[]`, so every Invoice created without explicit lines shares one list and accumulates other invoices' lines, inflating totals across a long-running process.
- [operator:F4] -> [auditor:F1]: find_customers interpolates user input directly into SQL, allowing injection from the search box and breaking on any name containing a quote.
- [operator:F1] -> [auditor:F4]: send_invoice swallows every SMTP exception without logging and then marks the invoice sent in both memory and the DB, so failed emails are silently recorded as delivered and can never be retried.
- [operator:F5] -> [auditor:F2]: list_invoices skips the newest page_size invoices for page=1 because it slices from page*page_size while the docstring says pages start at 1, and it loads the whole invoices table on every call.
- [operator:F2] -> [historian:F4]: The SMTP password and host are hardcoded in source and committed to git, so the secret is exposed to anyone with repo access and can only be rotated by a code deploy.
- [auditor:F5] -> [historian:F4]: A live-looking SMTP password is hardcoded and committed to the repository.
- [operator:F7] -> [historian:F6]: The only test exercises a single fresh Invoice, so it cannot catch the shared-lines default and will itself become order-dependent once a second test constructs an Invoice.
- [auditor:F7] -> [tester:F6]: send_invoice raises an unhandled TypeError if the customer row does not exist, because fetchone() returns None and is then subscripted.
- [tester:F1] -> [auditor:F3]: Invoice uses a mutable default argument (`lines: list = []`), so every Invoice created without explicit lines shares one list; a second `Invoice(customer_id=2).add_line(...)` changes the first invoice's total.
- [tester:F3] -> [auditor:F1]: find_customers interpolates user input into SQL, so a search for `O'Brien` raises sqlite3.OperationalError and `' OR '1'='1` returns every customer.
- [tester:F4] -> [auditor:F4]: send_invoice swallows every SMTP exception and still marks the invoice sent, so a failed login or an unreachable host leaves the database saying sent=1 while the customer got nothing.
- [tester:F2] -> [auditor:F2]: list_invoices has an off-by-one: the docstring says pages start at 1, but `rows[page*page_size:(page+1)*page_size]` makes page=1 return rows 20-39, so the newest 20 invoices can never be reached through a 1-based page.
- [tester:F5] -> [historian:F4]: The SMTP password is hardcoded in source and is now part of the repository history.
- [historian:F3] -> [auditor:F3]: Invoice uses a mutable default argument lines=[], so invoices created without explicit lines share one list, and add_line on one invoice changes the others' totals.
- [historian:F1] -> [auditor:F1]: find_customers builds its SQL by interpolating the search-box string into the query, so a name containing a quote breaks the query and allows SQL injection.
- [historian:F5] -> [auditor:F4]: send_invoice swallows every exception from the SMTP send and still marks the invoice sent, both in memory and in the database, so failed emails are recorded as delivered.
- [historian:F2] -> [auditor:F2]: list_invoices claims pages start at 1 but slices from page*page_size, so page 1 returns the second page and the newest page_size invoices are unreachable.
- [maintainer:F3] -> [auditor:F3]: Invoice.__init__ uses a mutable default `lines: list = []`, so every Invoice created without explicit lines shares one list. Lines added to one invoice appear on all the others, and their totals inflate.
- [maintainer:F1] -> [auditor:F1]: find_customers builds its SQL by f-string interpolation of user input, unlike every other query in the repo. Any name containing a quote breaks the query, and a crafted name is a SQL injection from the search box.
- [maintainer:F4] -> [auditor:F4]: send_invoice swallows every exception from SMTP and then marks the invoice sent in memory and in the database. A failed email is recorded as delivered, and the failure is never surfaced.
- [maintainer:F2] -> [auditor:F2]: The docstring of list_invoices says pages start at 1, but the slice treats page as 0-based, so page=1 returns invoices 21–40 and the newest 20 are never shown. It also loads the whole invoices table into memory on every call.
- [maintainer:F5] -> [historian:F4]: SMTP credentials, including a plaintext password, are hard-coded in source and committed to the repository.
- [maintainer:F6] -> [historian:F7]: Money is held as float, and VAT is rounded only on the grand total. This will produce penny discrepancies against per-line VAT expectations and binary-float artefacts in stored totals.

## What each persona understood
- auditor: A commit adding invoice totals with VAT, customer search, invoice paging, persistence and an SMTP mailer to a small sqlite-backed invoicing package, plus one unit test. I checked each claim the code makes against what the code actually does, and looked for any recorded decision behind its constants.
- historian: A commit adding invoice totals with VAT, customer search, paged invoice listing, an SMTP mailer that marks invoices sent, and one test for the VAT total. The record has no history for this repo, so every finding below comes from my own reading of the diff.
- maintainer: This commit adds an Invoice class with VAT totals, a customer name search, paged invoice listing, invoice saving, an SMTP mailer that emails an invoice and marks it sent, and a single test for the VAT total. As the future owner, I checked whether each piece follows the repo's existing patterns and whether a later reader can trust what the docstrings say.
- operator: Adds VAT totals to Invoice, a customer name search, paged invoice listing, invoice persistence, and an SMTP mailer that emails an invoice and marks it sent. There is one unit test for the VAT total.
- security: A commit adding customer search, invoice paging, invoice persistence, an Invoice class with VAT totals, and an SMTP mailer that emails invoices and marks them sent, plus one test for the VAT total.
- tester: This change adds VAT totals to Invoice, customer search and invoice paging in db.py, and a new SMTP mailer that emails an invoice and marks it sent. One test covers a single-invoice VAT total; nothing else that changed is tested.

## Notes from the panel
- auditor: Memory is empty and the shared world holds only template placeholders: no project description, decisions or constraints. So nothing here can be checked against a recorded decision, and the VAT rate, SMTP host and credentials all rest only on the code itself. I could not see the schema, so I cannot tell whether `sent` or a foreign key on customer_id are enforced at the database level. The test suite covers only the single-invoice VAT happy path. It would pass despite the mutable-default, paging and mailer defects.
- auditor: Debate round. Every reviewer independently raised the same five defects: SQL injection, the paging off-by-one, the mutable default, the swallowed SMTP failure and the committed password. I merged my F1–F5 into one representative each. My F6 (an unrecorded VAT decision) stays standalone; it is the one finding about a missing decision record, and the empty shared world is its whole basis. F7 merges with tester:F6. No memory entries exist, so all stances rest on my own reading. The panel converges because the defects are visible directly in the diff, not because anyone deferred. I could not verify the DB schema, foreign-key settings, the row_factory configuration, or any global socket timeout; findings that depend on these carry lower confidence.
- historian: MEMORY is empty and the shared world has no recorded decisions, constraints or project description. So I cannot say whether any of this was tried and reverted before, or whether these files are known hotspots. All findings rest on my own reading of the diff with no memory support. Worth recording for future sessions: the hardcoded SMTP credential (it needs rotation tracked) and the 'mark sent on failure' behaviour, in case it is changed and later reverted.
- historian: My memory is empty and the shared world records no decisions, reverts or hotspots. So I have no historical evidence for or against any finding on this target. Every position below rests on my own reading of the diff. The panel's findings converge strongly on five defects: SQL injection, the paging off-by-one, the mutable default, the hardcoded secret, and send_invoice marking failed sends as sent. Even so, objections are worth recording. security:F1's word 'alter' overstates the impact under sqlite3's single-statement execute, and nobody has seen the schema, which limits maintainer:F7, auditor:F7 and tester:F6. It would help to record a decision on the VAT rate and the rounding policy (auditor:F6, maintainer:F6) and where credentials live, so future reviews have a record to check against.
- maintainer: I have no memory entries and the shared world records no decisions, so every finding rests on my reading of the diff alone. I could not see the schema for the customers and invoices tables, other modules, or how find_customers and list_invoices are called. In particular I cannot tell whether callers pass 0-based pages and the docstring is the part that is wrong. Test coverage is a single happy-path test; it does not exercise search, paging or mailing, and it masks the mutable-default bug.
- maintainer: The panel converges on five defects: SQL injection, the paging off-by-one, the mutable default, swallowed SMTP errors marked as sent, and the committed password. I merged my duplicates of those rather than restating them. I could not see the connection setup, so I cannot tell whether row_factory or foreign keys are enabled; that limits my view of tester:F6 and auditor:F7. I hold my persistence-shape point (F7) on my own reading. My new finding (mailer issuing its own SQL) is a maintainability point, not a correctness one.
- operator: No memory entries exist, so all findings are my own reading of the diff. I could not see the schema migration for invoices/customers (e.g. whether `sent` exists, or whether row_factory is set to sqlite3.Row, which customer["email"] needs). I also could not see how send_invoice is invoked (inline request vs job), which affects how bad the missing timeout is. send_invoice also raises TypeError outside the try if the customer row is missing. There is no feature flag or kill switch for emailing: to stop sends you must deploy. Totals use float arithmetic; this is minor next to the above.
- operator: I merged six of my findings into duplicates raised by other reviewers. F3 (no SMTP timeout) is the only point unique to me, so I hold it. From an on-call view, the most dangerous items are mailer.py:24-28 and invoice.py:16-18. Both produce wrong persisted state with no error and no log. One objection runs across the secret findings: the host and user use the reserved .example TLD, which suggests sample config. The password literal should still be treated as leaked. I could not see callers, the connection setup (row_factory, foreign keys) or deployment config, so environment-dependent claims stay below certainty.
- security: No memory exists yet. Floats are used for money (round on float); this is a correctness concern rather than a security one, so I left it out. The single test does not exercise the mutable-default bug, paging, search or mailer failure. I cannot see the schema or the callers of find_customers, so I am assuming the search input reaches it unsanitized, as its docstring says.
- security: The panel converges on the five core defects: SQL injection, the hardcoded secret, fail-open mail sending, the shared mutable default, and the paging off-by-one. I merged my duplicates into those findings rather than inflating the count. Caveats I would flag: (1) sqlite3 execute() runs one statement, so the injection is a read (UNION/boolean) exposure, not table alteration; my original F1 wording overstated 'alter'. (2) The SMTP host uses the reserved .example TLD, so the password may be a sample. The practice is still wrong, but 'treat as leaked' depends on whether the value is real. (3) operator:F3 (no SMTP timeout) is a real availability issue I missed; I agree with it rather than duplicate it. I cannot see the schema, the row_factory setup, the callers of list_invoices, or who can write customer emails, so the paging-impact and To-header findings rest on assumptions.
- tester: Memory is empty and the shared world records no decisions, so every finding rests on my own reading of the diff. I could not see the schema or the connection setup; whether row_factory is sqlite3.Row is unknown, which is why the mailer None/row-factory finding is rated lower. Storing money as float with round() is a further, minor concern that I did not raise separately.
- tester: All six reviewers converge on the same five core defects: SQL injection at db.py:24, the 1-based/0-based paging mismatch at db.py:27-30, the mutable default list at invoice.py:16-18, the swallowed SMTP failure marked as sent at mailer.py:19-28, and the hardcoded password at mailer.py:6-8. I merged my duplicates of these and kept two items that add something distinct: F6, the row_factory dependency, and F7, the missing rounding and fractional-price test cases. Where agreement is near-total, my objections mostly target overstatement rather than the core claim. Examples: 'can never be retried', 'alter arbitrary tables' under sqlite3's single-statement execute, 'banker's rounding' for a binary-float effect, and 'cross-customer data exposure' for what is mainly wrong billing. From the packet I cannot see where connections are created (row_factory), whether FK enforcement exists, or whether a global socket timeout is set. Those decide F6, auditor:F7 and operator:F3.

## Panel health
- auditor: 7 findings, 0 memories shown, 3 calls, uncalibrated (no outcomes yet), summary updated, tokens in 15363 / out 8640 / cache wrote 6345 read 5676, calls: review in 67 read 0 wrote 5676; debate in 9382 read 5676 wrote 0; summary in 5914 read 0 wrote 669
- historian: 7 findings, 0 memories shown, 3 calls, uncalibrated (no outcomes yet), summary updated, tokens in 14973 / out 8922 / cache wrote 1959 read 10010, calls: review in 67 read 4356 wrote 1298; debate in 9389 read 5654 wrote 0; summary in 5517 read 0 wrote 661
- maintainer: 8 findings, 0 memories shown, 3 calls, uncalibrated (no outcomes yet), summary updated, tokens in 14653 / out 9644 / cache wrote 1863 read 9957, calls: review in 67 read 4356 wrote 1245; debate in 9385 read 5601 wrote 0; summary in 5201 read 0 wrote 618
- operator: 7 findings, 0 memories shown, 3 calls, uncalibrated (no outcomes yet), summary updated, tokens in 14873 / out 10281 / cache wrote 1922 read 9996, calls: review in 67 read 4356 wrote 1284; debate in 9389 read 5640 wrote 0; summary in 5417 read 0 wrote 638
- security: 6 findings, 0 memories shown, 3 calls, uncalibrated (no outcomes yet), summary updated, tokens in 14831 / out 10085 / cache wrote 1853 read 9947, calls: review in 67 read 4356 wrote 1235; debate in 9415 read 5591 wrote 0; summary in 5349 read 0 wrote 618
- tester: 8 findings, 0 memories shown, 3 calls, uncalibrated (no outcomes yet), summary updated, tokens in 14906 / out 10056 / cache wrote 1923 read 9997, calls: review in 67 read 4356 wrote 1285; debate in 9389 read 5641 wrote 0; summary in 5450 read 0 wrote 638

