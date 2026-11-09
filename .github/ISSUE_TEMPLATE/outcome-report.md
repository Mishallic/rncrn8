---
name: Outcome report
about: Tell us whether the panel learned anything on your own work
title: "Outcome report: "
labels: outcome-report
---

Leave out anything confidential. Numbers and a sentence are enough.

**What you reviewed**: kind (code, documents, plans), roughly how big, and how many reviews.

**Outcomes recorded**: how many findings you later marked confirmed or refuted yourself.
`rncrn8 decisions` shows each one with its source.

**Comparison, if you ran one**: on the same targets, did you also ask your assistant for a
plain review? Of the findings that turned out right, roughly how many came only from the panel,
only from the plain review, and from both?

**Did the learning help?** Paste the output of:

```
rncrn8 replay --without-learning
```

It fits calibration and panel weights on your older outcomes and scores your newer ones with
and without them, with a 95% interval on the difference. Below 10 held-out decisions it says
there are too few to tell; please send the output anyway, with your count of outcomes.

**Cost and time**: roughly what a review cost you, and whether that was worth it.

**Anything else**: what was useful, what was noise, what you stopped using.
