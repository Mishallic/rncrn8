# Review of 2fee95b "Invoices: totals with VAT, search, paging and emailing"

Each problem below was checked by running the code against an in-memory database. pytest is not installed here, so I ran those checks with plain `python3`.

1. **The SMTP password is committed to the repo.** `invoicing/mailer.py:8` (`SMTP_PASSWORD = "Shop-Mail-2026!"`, with the user and host on lines 6-7)
   The live mail password is now in git history. Anyone who can read the repo or a clone of it can send mail as `invoices@shop.example`. Deleting the line in a later commit does not remove it from history.
   **Fix:** Rotate the password now, because it is already exposed. Then read it at runtime, for example `SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]` (do the same for host and user if they differ per environment). If the branch has not been pushed, amend the commit so the secret never reaches the remote. If it has been pushed, rotating is what matters, and rewriting history is optional.

2. **SQL injection in the customer search.** `invoicing/db.py:24`
   `find_customers` puts the search-box text into the SQL with an f-string. Searching for `%' OR 1=1 --` returns every customer. A name with an apostrophe, such as `O'Brien`, crashes with `sqlite3.OperationalError: near "Brien": syntax error`, so ordinary searches fail too.
   **Fix:** Bind the value as a parameter:
   ```python
   return conn.execute(
       "SELECT * FROM customers WHERE name LIKE ? ESCAPE '\\'",
       ("%" + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
   ).fetchall()
   ```
   The escaping stops a user's `%` or `_` from acting as a wildcard. If that doesn't matter to you, `"... LIKE ?", (f"%{name}%",)` is the minimum fix.

3. **Invoices share one list of lines (mutable default argument).** `invoicing/invoice.py:16-18`
   `def __init__(self, customer_id, lines: list = [])` creates one list when the function is defined, and every `Invoice` built without `lines` reuses it. After `Invoice(1).add_line("Widget", 2, 10.0)`, a new `Invoice(2)` already contains the Widget and totals £24.00. In normal use, each customer's invoice would include the items from earlier invoices, so wrong amounts get saved and emailed. The existing test passes only because it creates a single invoice.
   **Fix:**
   ```python
   def __init__(self, customer_id: int, lines: list[Line] | None = None):
       self.customer_id = customer_id
       self.lines = list(lines) if lines else []
   ```
   Add a regression test that builds two invoices and checks that the second has no lines.

4. **A failed email is still marked as sent.** `invoicing/mailer.py:19-28`
   `except Exception: pass` hides every SMTP failure: DNS errors, a refused connection, failed authentication, a rejected recipient. Lines 26-28 then run anyway and set `sent = 1`. I made `smtplib.SMTP` raise, and the invoice row still ended up with `sent = 1`. The customer never receives the invoice, and the system shows nothing to retry.
   **Fix:** Mark the invoice as sent only after `send_message` succeeds, and let the error reach the caller:
   ```python
   with smtplib.SMTP(SMTP_HOST, 587, timeout=30) as smtp:
       smtp.starttls()
       smtp.login(SMTP_USER, SMTP_PASSWORD)
       smtp.send_message(msg)
   invoice.sent = True
   conn.execute("UPDATE invoices SET sent = 1 WHERE id = ?", (invoice_id,))
   conn.commit()
   ```
   If you need to catch the error, catch `smtplib.SMTPException` and `OSError`, log them, and re-raise or return a failure. Never `pass`. The `timeout=` stops one bad connection from hanging the caller forever (there is no timeout today).

5. **Paging skips the first page and loads the whole table.** `invoicing/db.py:27-30`
   The docstring says "Pages start at 1", but the slice `rows[page * page_size:(page + 1) * page_size]` treats page numbers as starting at 0. With 25 invoices, `list_invoices(conn, 1)` returned ids 5..1 (the second page) and page 2 returned nothing. The 20 newest invoices can never be reached. The function also fetches every invoice into memory and then slices, so each page gets slower as the table grows.
   **Fix:** Page in SQL with a 1-based offset:
   ```python
   if page < 1:
       raise ValueError("page starts at 1")
   return conn.execute(
       "SELECT * FROM invoices ORDER BY id DESC LIMIT ? OFFSET ?",
       (page_size, (page - 1) * page_size),
   ).fetchall()
   ```

6. **`send_invoice` crashes on a missing customer.** `invoicing/mailer.py:13-15`
   If `invoice.customer_id` matches no row, `fetchone()` returns `None` and `customer["email"]` raises `TypeError`, an error that doesn't explain what went wrong.
   **Fix:** After the query, add `if customer is None: raise ValueError(f"no customer {invoice.customer_id} for invoice {invoice_id}")`.

7. **The new code has almost no tests.** `tests/test_invoice.py:4-7`
   The only new test checks a single invoice's total, so problems 2-5 all pass CI.
   **Fix:** Add tests for:
   - two `Invoice` objects not sharing lines;
   - `find_customers` with `O'Brien` and with `%' OR 1=1 --`;
   - `list_invoices(conn, 1)` returning the newest `page_size` invoices;
   - `send_invoice` with `smtplib.SMTP` monkeypatched to raise, asserting the row stays `sent = 0`.
