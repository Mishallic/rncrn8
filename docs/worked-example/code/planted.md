# Planted flaws in the invoicing diff (HEAD~1..HEAD)
1. SQL injection: find_customers builds the LIKE query with an f-string from user input.
2. Mutable default argument: Invoice(lines=[]) shares one list across every invoice made without lines.
3. Paging off by one: pages start at 1, but the slice uses page * page_size, so page 1 skips the first 20 invoices (and every page loads all rows).
4. Money in floats: amounts are floats and the VAT total is rounded with round() on a float.
   Correction after a reviewer checked it: with prices in whole pence and VAT at 20%, a total
   never lands on a half penny, so this code cannot actually produce a wrong total today. It is
   a fragile pattern (it breaks once a price or rate has more precision), not a live bug.
5. A hard-coded SMTP password in the source.
6. A swallowed exception: a failed email is ignored and the invoice is still marked sent.
