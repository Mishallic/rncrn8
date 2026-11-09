# Security

## Reporting a vulnerability

Please report security problems privately, through GitHub's private vulnerability reporting:
open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a
public issue for a security problem. If the Security tab does not offer that, open an issue
asking for a private way to report, with no details of the problem.

rncrn8 is maintained by one person, so there is no guaranteed response time. Reports about the issues below are the most useful.

## What to know before you use it

- **Reviewed material leaves your machine.** Every review sends the reviewed text (a diff,
  a document, a plan) and the personas' selected memories to the Anthropic API, under your
  own API key and your own Anthropic data terms. Only review material you have the right to
  send there. Nothing is sent to the author of rncrn8.
- **Memory is kept on disk.** Each persona's memory, every review's prompts and results, and
  the outcomes you record are plain files under `.rncrn8/` in your project. They can contain
  excerpts of what was reviewed. When rncrn8 creates the folder in a git repository, through
  `rncrn8 init` or setup from the assistant, it keeps it out of git with `.git/info/exclude`.
  That line is local: if you run `git init` later, or copy the project elsewhere, check that
  `.rncrn8/` is still left out, and keep it out of anything you publish.
- **Reviewed material can carry instructions.** A diff or a document can contain text written
  to steer a model. rncrn8 fences reviewed material as data and its personas are told to treat
  it as such, but no prompt makes a model immune. Text from a reviewed target can end up in a
  persona's findings, and findings are stored as memories that later reviews read. Review
  untrusted material in a separate project, and check what a persona remembers with
  `rncrn8 recall <persona>`; `rncrn8 retire` removes an entry.
- **The assistant can record outcomes, and it writes their label.** Through the MCP tools,
  your coding assistant records which findings turned out right, and labels each outcome as
  coming from you (`user`) or from a check you were shown (`check`); an agent running
  unattended in an automated loop, with no person present, labels its outcomes `loop`. Only
  `user` and `check` outcomes change trust and weights. The assistant is instructed never to
  record its own opinion as yours, but nothing enforces the label, and an assistant steered by
  text in a reviewed target could mislabel an outcome. `rncrn8 decisions` lists every outcome
  with its source and when it was recorded. In 0.2.0 a recorded outcome cannot be undone, so if
  you review untrusted material, do it in a separate project (see above).

## Supported versions

Only the latest release receives fixes.
