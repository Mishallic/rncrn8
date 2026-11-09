# rncrn8

A review panel you talk to through your coding assistant: several AI personas review
your work, each from its own remembered evidence, argue it out, and learn from what
turns out true.

"Act as a team of experts" is one model playing five voices from one context
window. They know the same things, cannot disagree from different evidence, and
vanish when the session ends. rncrn8 gives each persona its own memory, as
plain files in your repo, with a provenance header on every entry: where it came
from, who said it, when, and how far to trust it. Personas review separately,
then debate in separate calls, and the outcomes you record move trust in the
memories and readings that produced each finding. The engine behind this is
described in `docs/algorithm.md`.

Works today: in Claude Code or any MCP client, with Claude as the model, or from the
command line; text, markdown, git diffs, patches, video folders, pitches and packet JSON
as targets; a two-round review
with self-updating persona summaries; outcomes on any decision with calibration
and a replay harness; memory filled from the project, git history, notes, a
questionnaire or the web. Status and plan are at the end.

## Status and known limits

Read this first.

- **The learning is unproven.** The mechanism works as described: outcomes you record
  move calibration, panel weights and memory trust. Nobody has yet shown, on real use,
  that this makes the panel's findings better over time than a single good reviewer.
  There are no published results.
- **Claude only, for now.** Every call goes to the Anthropic API. Other model families
  are planned, not built.
- **You pay per review, on your own key.** Measured on 2026-11-09 with `claude-opus-5-5`
  at list prices, on a fresh project with empty memory: about $0.89 and 2 minutes for the
  default four-persona document panel on a one-page plan; $1.60 and 2 minutes for the default
  six-persona code panel on a 74-line diff, and $2.01 and 3 minutes on a 166-line one. A
  quick two-persona review with no debate (`--panel critic,auditor --no-debate
  --no-summaries`), run second with the first review's memories shown, cost $0.21 and 1
  minute. Bigger targets and fuller memories cost more. Prices change; `config.yaml` sets the
  model, and `RNCRN8_MODEL` overrides it. You need an Anthropic API account with credits; a
  Claude subscription does not cover API calls.
- **On a single review, it is not clearly better than asking your assistant directly.** In
  the [worked examples](docs/worked-example/README.md), a plain Claude Code review found all
  six planted flaws in a plan, and more of its other problems than the panel did; on a code
  change it found five of six planted bugs and proved four by running them, while the panel
  found all six, one only as a hedged question. One run each is no ranking. What rncrn8 adds
  is meant to show over many reviews, through memory and recorded outcomes, and that is the
  unproven part above.
- **Maintained by one person**: issues are answered actively for six weeks after the 0.2.0
  release, then on a best-effort basis. Pull requests are not accepted for now (see
  `CONTRIBUTING.md`).
- **A recorded outcome cannot be undone yet.** Check `rncrn8 decisions` if your assistant
  records outcomes for you (see `SECURITY.md`).
- Much of the code was written with an AI coding assistant, under the author's direction.

## What leaves your machine

- The reviewed material, and the memories each persona is shown, go to the Anthropic API
  under your key and your Anthropic data terms. Review only what you have the right to send.
  Nothing goes to the author.
- Memories, prompts, results and outcomes stay as plain files in `.rncrn8/` in your project,
  and can contain excerpts of what was reviewed. Keep that folder out of anything you publish.
- Reviewed material is treated as data, never as instructions, but a model can still be
  steered by text written to steer it, and what a persona concludes is stored as memory that
  later reviews read. See `SECURITY.md`.

## Install and connect

rncrn8 is open source under the Apache License 2.0 (see LICENSE). You need Python
3.11 or newer and an Anthropic API key. Two commands in a terminal, then everything
happens in the chat:

```bash
pip install rncrn8
cd your-project
rncrn8 init
```

In Claude Code you can install it as a plugin instead, which runs the same server
through `uvx` (install [uv](https://docs.astral.sh/uv/) first): `/plugin marketplace add Mishallic/rncrn8`,
then `/plugin install rncrn8@rncrn8`. Use one or the other, not both: with the plugin
and `init`'s connection together, the assistant sees two rncrn8 servers. The plugin's
server reads `RNCRN8_API_KEY` from the environment Claude Code was started with, and
works in the project Claude Code has open; if Claude Code was launched from a desktop
icon rather than a terminal, set the key where that launch can see it, and run
`rncrn8 doctor` in the project to check.

A review from the assistant is a single tool call that takes a few minutes. Claude Code
waits for it, and by default moves a call that runs past two minutes to the background (see
[Claude Code's MCP documentation](https://code.claude.com/docs/en/mcp)).
Other MCP clients may stop waiting sooner; if yours does, run the review from the
command line (`rncrn8 review ...`) and ask the assistant to read the report.

`init` creates the project's workspace, `.rncrn8/`, and keeps it out of git through
`.git/info/exclude`. Then, after asking, it connects rncrn8 to Claude Code for all your
projects (`claude mcp add --scope user`), run by the Python it was installed in, so it
does not depend on your PATH. Without Claude Code it prints that command, and the entry
Cursor, VS Code and other MCP clients take. Run `init` once in each project you want
reviewed; the connection is made once.

Model calls go through your own Anthropic API key, billed to your account. Put it in
`RNCRN8_API_KEY`, which only rncrn8 reads:

```bash
# PowerShell:  setx RNCRN8_API_KEY "sk-ant-api03-..."   then restart VS Code, or the terminal Claude Code runs in
# bash:        export RNCRN8_API_KEY=sk-ant-api03-...
```

`ANTHROPIC_API_KEY` and `ant auth login` work too, but Claude Code reads `ANTHROPIC_API_KEY`
as well: if you log into Claude Code with a Claude subscription, it asks once and then bills
its own use to that key instead of the subscription. `RNCRN8_API_KEY` keeps the two apart.
A key that is not tied to a workspace has to name one on every call: set
`RNCRN8_WORKSPACE_ID`, or `ANTHROPIC_WORKSPACE_ID`, to the workspace id (`wrkspc_...`) the same
way, and rncrn8 sends it in the `anthropic-workspace-id` header. `rncrn8 doctor` shows the key
and the workspace it will use.

When something fails, `rncrn8 doctor` checks the install, the key, the connection to the
assistant and the workspace, one line each, and says what to do about anything wrong;
`rncrn8 doctor --live` also makes one tiny call to the model. Developers working on the
source install the checkout instead: `pip install -e ".[dev]"`, or `uv sync`.

## In the chat

Open Claude Code in the project, in a terminal or in the Claude Code extension for VS Code;
both use the same connection. The assistant is the go-between: you talk to it, it runs the
panel through rncrn8's tools, puts what the panel says in plain words, and records what you
tell it. Each step is also a slash command in Claude Code. The VS Code extension offers a
subset of slash commands (type `/` to see them), and other MCP clients may offer none; where
`/rncrn8:review` is not listed, ask in words, such as "review my branch with rncrn8".

**Set up**, with `/rncrn8:setup` or "set up rncrn8". The assistant reads the project once:
its README and docs, CLAUDE.md or AGENTS.md, manifests, files that record sources and
versions, the opening lines of its source files, the first and last lines of large data
files, and its recent git history. That is how the panel learns what the project is; a
review reads the change itself (see [What happens in a review](#what-happens-in-a-review)).
Then it shows you what it proposes:

- What every persona will be told: what the project is, its constraints, and the
  decisions its files state, each with the file that says it. A line is kept only when the
  excerpt it rests on is found, as written, in that file; anything left out is named with
  the reason. Sections you already wrote in `world.md` are never overwritten.
- A panel: seed personas that fit the project, and new ones written for expertise no seed
  covers, each with why, and a default panel per kind of review, at most six.

You answer in your own words: go ahead, drop someone, add "a privacy officer for EU health
data". Facts from the files and the git history go into shared memory, each with its file
and the hash of the text it was read from. The proposal is kept until you apply it, so
asking to see it again costs nothing, and setting up again adds only what is new.

**Review**, with `/rncrn8:review`, optionally followed by a git range or a file, or "review
my branch". With no target the branch is reviewed against its base. A review takes a
minute or more. The assistant tells you the problems the panel agrees on first, then the
disputed ones and how to settle them, then the panel's questions: those the code can
answer, such as whether a path is ever reached, it checks and tells you what it found; the
rest it asks you. Then it offers to fix, test or ask.

**Answer**, by telling the assistant what turned out right or wrong, in your words;
`/rncrn8:outcomes` goes through the open findings one at a time. It records your verdict
and your reason for every finding you answer, all of them when you answer several in one
message, then says what it recorded and which findings of that review are still open. It
keeps anything you say the panel should know from now on as a fact you stated, which every
persona can use, and records a rule you state, such as "notes never go on invoices", as a
decision. It records an outcome only on your word or on a check it ran and showed you,
never on its own opinion.

**Fix.** When a fix to reviewed code is committed, the assistant runs followup, which
closes the problems and risks whose cited lines changed.

**Status**, with `/rncrn8:status`: what is still open, and each persona's track record. A
persona's weight follows its score, how close its confidence was to what happened, not
only how often it was right: a confident call that turns out right raises it more than a
hesitant one, and a confident miss lowers it more. So a persona right five times out of
seven can outweigh one right six times, if it was surer when right.

**Beyond code.** Ideas, plans, pitches and strategies are reviewed the same way: "have the
rncrn8 panel review this idea: …", or point it at a document such as `docs/marketing.md`.
The panel raises problems, risks, questions and real strengths; it does not write the plan,
so ask Claude for a draft first and have the panel take it apart. A panel set up for code
may have no one for ideas: ask for the ready-made pitch panel (the critic, the investor, the
customer, the counsel and the auditor), which becomes the default panel for ideas, or for
any ready-made persona by name, or describe one to be written for the project.

**Memory from outside.** Paste notes from a call, an interview or a meeting and say who
said it: each fact is kept as that person's word. Ask for research and one persona searches
the web on a search budget, keeping what it found with its URL, publisher and date; that
costs more than a review call, so the assistant asks first. When results come in, from a
campaign or a customer, say so: those are outcomes, and they move trust like any other.

## What happens in a review

**What the panel reads.** For a code change: the changed lines, with three lines around
each, and, marked as unchanged, the code elsewhere that uses each function or class the
change modifies or removes, a few uses each. A diff alone hides the caller that never
reaches a new branch, or the file that still calls a removed function; with the uses shown,
the panel can tell whether the change is reached and fits how it is called. That context
has a budget of its own and does not count toward the size limit. Lockfiles and large data
files are listed or cut short. A document or a pitch is read whole.

1. **Round one.** Every persona reviews alone, in its own model call, with its own
   memory and the shared world. Each finding names where it is, cites the memory
   ids it rests on, and states a confidence.
2. **Round two, the debate.** Every persona reads the others' findings, with their
   evidence described by provenance, and must hold, revise, concede or merge each
   of its own, and agree, disagree or stay unsure on each of theirs. A change of
   position must name what changed it. A finding that makes the same point as
   another reviewer's is merged into it and counts as agreement, so three personas
   noticing one thing produce one item, not three.
3. **The report** lists findings as agreed, disputed, unchallenged, held or dropped,
   with every witness and its weight. Agreement between personas counts for less
   than independent evidence, while memories they cite keep their own weight;
   `docs/algorithm.md` has the rule. Disagreement is left standing for you to settle.
4. **Summaries.** Each persona rewrites its own `summary.md`, which it reads at the
   start of its next session.

Everything is written to `.rncrn8/reviews/<id>/`: the packet, every prompt, the
structured result and `report.md`.

Each outcome moves trust in every witness behind the finding: the persona's own reading,
the memories it cited, and the other personas who agreed or disagreed. Over time this
builds a track record. Calibration compares each persona's stated confidence with what
happened once a person has recorded three outcomes for it, and reviews use the
calibrated value. Panel weights move toward personas whose readings beat a coin flip.
Only outcomes a person stands behind teach: what the user said (`user`), or the result
of a check the assistant ran and showed them, such as a test (`check`). Everything else is
recorded on the finding and linked from the memories it cited, but moves no trust, no
calibration, no panel weight and no part of replay's test set:

- **`loop`**: an outcome from an automated loop or an agent, with nobody confirming it. A
  fix loop edits exactly the lines a finding cited, so its outcomes would credit the panel
  for being obeyed.
- **Self-checks**: when a persona reviews the same target again and reports one of its
  earlier findings as addressed or wrong. A finding on a different target is never closed
  this way.
- **Commit closures**: from `followup`, when a later commit changed the cited lines.
  Changed lines are not proof that the finding was right.

`answer` and `outcome` say which source each outcome has.

## The command line

Everything the assistant does is also a command, for scripts, CI, and anyone who
prefers a terminal:

```bash
rncrn8 setup                         # the same proposal and questions; --yes accepts all, --add "name: role"
rncrn8 review --diff main..HEAD      # --dry-run prints every prompt and calls no model
rncrn8 outcome d-... --confirmed     # or --refuted --note "we shipped it and nothing broke"
rncrn8 followup r-...                # close findings whose cited lines changed
rncrn8 decisions --open
rncrn8 reviews list                  # and `reviews show r-...`
rncrn8 calibration                   # stated confidence against what happened
rncrn8 panel                         # the weight each persona has earned
rncrn8 replay --candidate other.yaml # score a config change on held-out decisions first
rncrn8 replay --without-learning     # and whether calibration and panel weights beat none
```

The report prints a decision id under each finding, and `r-<review>:critic:F1` works in
its place. Review flags: `--panel critic,skimmer`, `--no-debate`, `--no-summaries`,
`--show-prompts`, `--kind idea`. `persona panel`, `ingest` and `decide` do single steps by
hand.

## The panel

Thirteen seed personas, each with a character, an ordered rubric, a trust profile over
source classes and a loss matrix that says which mistake it fears. Reading anything: the
critic (structure), the skimmer (attention), the stranger (missing context), the auditor
(claims that rest on a decision nobody recorded, and defaults treated as choices) and the
historian (what this project already tried). Code: the security reviewer, the maintainer,
the tester and the operator. Ideas and documents: the investor, the customer and the
counsel (licenses, data, claims someone could hold you to). Video: the viewer.

`rncrn8 persona panel code|pitch|document|video|all` creates a recommended set and
leaves personas you already have alone; `--panel` on a review picks any subset. The
historian only earns its place after `ingest history`, and the auditor after decisions are
recorded with `decide`. Cost grows with the panel, and faster in the debate round, where
each persona answers every other reviewer's findings; the Panel health lines of a report
show the tokens every call used, and that is the only cost figure worth quoting. The
counsel's findings are not legal advice; a lawyer signs off before anything rides on them.

## How memory changes a verdict

`review` picks witnesses automatically from what each persona remembers. The
low-level primitive underneath is `judge`, where you name the witnesses yourself.
It is the quickest way to see provenance at work. Attributions below are
synthetic; see the note on sensitivity under Layout.

```bash
rncrn8 remember critic --claim "Interviewee A removed a review bot after two weeks of noise" \
  --source human --who "interviewee A" --origin "user interview 1" --tag noise
rncrn8 remember critic --claim "Forum threads say review bots get muted when they post too much" \
  --source internet --origin "https://example.com/thread" --tag noise
rncrn8 remember skimmer --claim "Interviewee B kept the review bot on and liked the volume" \
  --source human --who "interviewee B" --origin "user interview 2" --tag noise

rncrn8 recall critic --tag noise
# m-1a1ab93ae2c7abc  [human    ] conf=0.80 (n=10.0)
#     Interviewee A removed a review bot after two weeks of noise
#     from: user interview 1 | who: interviewee A | 2026-11-05
# m-1a1ab93ad27e9ec  [internet ] conf=0.57 (n=7.0)
#     Forum threads say review bots get muted when they post too much
#     from: https://example.com/thread | 2026-11-05
rncrn8 recall skimmer --tag noise
# m-1a1ab93b12a9cad  [human    ] conf=0.90 (n=10.0)
#     Interviewee B kept the review bot on and liked the volume
#     ...

rncrn8 judge critic  --claim "Noise will kill adoption" --supports m-1a1ab93ae2c7abc,m-1a1ab93ad27e9ec
#   belief 0.84  (prior 0.50, log-odds +1.62)
#   decision RAISE: threshold 0.33 ...
#   recorded as d-1a1ab93b33bbdb0
rncrn8 judge skimmer --claim "Noise will kill adoption" --opposes m-1a1ab93b12a9cad
#   belief 0.10  (prior 0.50, log-odds -2.20)
#   decision hold: threshold 0.75 ...

rncrn8 outcome d-1a1ab93b33bbdb0 --confirmed
```

Two personas, two memories, two verdicts on one claim. The thresholds differ
because each persona has its own loss matrix in `persona.yaml`: the critic fears
missing a real problem, the skimmer fears crying wolf. Same evidence can cross
one threshold and not the other, on purpose.

Memories that trace to one source share a `root` and count as one witness with
sqrt(n) the weight, not as n witnesses. Two human memories from the same person in
the same conversation share one automatically; `--root` groups anything else, such
as ten posts repeating one study.

## Reviewing other things

`review` takes a text or markdown file, a `.patch`, an `.srt` or `.vtt`, a video
folder, or a packet JSON. Two options replace the path: `--diff` for a git diff
and `--text` for an inline pitch.

```bash
rncrn8 review --diff main..HEAD                 # a committed range
rncrn8 review --diff HEAD                       # your uncommitted changes
rncrn8 review --diff staged
rncrn8 review --text "We sell memory to AI personas." --panel investor,stranger
rncrn8 review cut-07/ --window 5                # a video folder, see below
rncrn8 packet --diff HEAD~1..HEAD               # show the packet an adapter builds, review nothing
```

**Code.** Each hunk is a section with ref `path:start-end` on the new side, and
every line carries its number, so personas cite exact lines. After a review made
with `--diff A..B`, `rncrn8 followup <review-id>` diffs from the reviewed
commit to HEAD and closes every open problem or risk whose cited lines were
themselves changed since. These closures are recorded but teach nothing. Context
lines around a change do not count, strengths and questions are never closed by
a commit, and untouched findings stay open. A packet handed in as JSON cannot
carry git provenance, so follow-ups only work for reviews the git adapter built.

**Video.** This tool does no vision. Point it at a folder holding what your
vision pipeline produced: `transcript.srt`, `.vtt`, `.json` or timestamped `.txt`
for what is said, and `frames.json` or `frames.csv` (`t, description`) for what is
seen, plus an optional `meta.json`. They are merged into windows of N seconds,
each a section `t=mm:ss` with VISUAL and AUDIO lines. An `.srt` alone works.

**Anything else.** Any script can hand in a packet JSON, and nothing in the core
knows what it was. Personas cite the `ref` values.

```json
{
  "kind": "video",
  "title": "cut 07",
  "summary": "90s short, vertical",
  "sections": [
    {"ref": "t=00:00", "title": "frames 0-3s", "text": "Wide shot of ..."},
    {"ref": "t=00:03", "title": "transcript", "text": "..."}
  ],
  "metadata": {"duration": "90s"}
}
```

```bash
rncrn8 review cut-07.packet.json --kind video
```

## Filling memory

Memory has sources, each opt-in, each recorded with provenance so a persona can
weigh it. A person's statement outweighs a forum post; a forum post that keeps
matching outcomes earns its way up.

```bash
rncrn8 ingest project                 # README, docs, manifests -> shared project facts
rncrn8 ingest history --since "90 days ago"   # git log -> reverts, hotspots, fixes, decisions in messages
rncrn8 ingest notes call.md --who "interviewee A" --origin "user interview 2026-11-03"
rncrn8 ask                            # five questions, two minutes, recorded as human memories
rncrn8 research critic -q "do teams turn off noisy review bots?" --budget 5
```

`rncrn8 decide "..." --who founder --origin "planning meeting"` records a
settled decision in `world.md` and shared memory, so personas stop re-arguing it
and raise doubts about it as questions. `ingest` and `ask` write to the shared
store every persona reads, or to one persona with `--persona`. `research` sends one persona to the web through its own
lens, with a cap on searches, and records what it found as internet memories
with the URL, the author or site, and the date; those expire after 180 days
unless re-checked. Every ingest has `--dry-run` to show what it would record.
Without a model (`--adapter dry-run`) the project and history ingests still
record what they can read deterministically, and notes fall back to one claim
per paragraph.

## Connecting an assistant by hand

`rncrn8 init` connects Claude Code. By hand, with the Python rncrn8 is installed in:

```bash
claude mcp add --scope user rncrn8 -- /path/to/python -m rncrn8 mcp
```

With no `--workspace`, the server works in the project Claude Code has open
(`CLAUDE_PROJECT_DIR`), so one registration serves every project that has a workspace.
Cursor, in `.cursor/mcp.json`, names the project:

```json
{"mcpServers": {"rncrn8": {"command": "/path/to/python", "args": ["-m", "rncrn8", "mcp", "--workspace", "/path/to/your-project"]}}}
```

The server offers `setup`, `setup_apply`, `add_persona`, `seed_personas`,
`add_seed_personas`, `review`, `answer`, `followup`, `status`, `research`, `ingest_notes`,
`decide`, `remember`, `recall`, `personas`, `decisions` and `outcome`, the slash
commands `setup`, `review`, `outcomes` and `status`, and instructions that make the
assistant the go-between described above. Whatever assistant you use, the personas and
their memory are the ones in that project's `.rncrn8` folder.

## Layout of a workspace

```
.rncrn8/
  config.yaml          every tunable of the engine and the model settings
  world.md             shared facts every persona reads; put settled decisions here
  reliability.json     what each origin has earned from outcomes
  panel.json           Hedge weights per persona from the outcomes of their readings
  decisions.jsonl      every judgement with its witnesses, for outcomes and replay
  setup-proposal.json  the last setup proposal, kept so answering it costs nothing
  reviews/<id>/        packet.json, prompts/, review.json, report.md
  personas/<name>/
    persona.yaml       identity, rubric, trust profile, loss matrix
    memory.jsonl       append-only log; latest snapshot per id wins
    summary.md         the persona's summary, rewritten by the persona after each review
```

`init` keeps the folder out of git, through `.git/info/exclude`. Delete that line to commit
it instead, and memory travels with the project and diffs like code. If you do, remember
the folder holds excerpts of what was reviewed, and a memory attributed to a named person
from a private conversation does not belong in a repository other people can read.

## Model settings

The Claude adapter uses the Anthropic SDK and resolves credentials from
`RNCRN8_API_KEY` first, then `ANTHROPIC_API_KEY`, then an `ant auth login` profile. With an
API key it sends `RNCRN8_WORKSPACE_ID`, else `ANTHROPIC_WORKSPACE_ID`, as the workspace header;
a profile names its own workspace. Defaults live in
`config.yaml` under `models`: `claude-opus-5-5`, effort `high`, server-side
refusal fallback on. `RNCRN8_MODEL` replaces the default model without editing the file;
`rncrn8 doctor` shows which one is in use and where it came from. A persona's own `model`
field, or `--model` on a review, still wins over both. Like the key, `RNCRN8_MODEL` reaches
the assistant's rncrn8 server only if it is set where the assistant was started.
`--adapter dry-run` calls nothing and records the prompts.

## Status and plan

Done so far. Memory lives in files; the command line and the MCP server read the
same files, so every tool sees the same personas.

1. Workspace, memory format, decision engine, CLI. Done.
2. Claude adapter, review packets, single-round `review`. Done.
3. Debate round, merged statuses, persona self-summaries. Done.
4. Calibration of stated confidence against outcomes, Hedge weights for the panel,
   implicit outcomes when a persona re-checks its own findings, replay harness. Done.
5. Target adapters: video (frames and transcript), git diff, document, idea pitch;
   follow-ups from later commits. Done.
6. Memory sources (project, history, internet, human) and an MCP server. Done.
   Shared cached target block across personas and rounds.
7. Install path: `doctor`, thirteen seed personas and panel presets. Done.
8. `setup`: the world, the decisions the files state, facts and a fitted panel from one read
   of the project. Done.
9. The assistant as go-between: setup, reviews, answers and outcomes in the chat, slash
   commands, and `init` connecting Claude Code. Done.
10. 0.2.0: open source under Apache-2.0; only outcomes a person stands behind teach;
    a Claude Code plugin; CI on Windows, macOS and Linux. Done.

Next, if people use it: undoing a recorded outcome, other model families, a cheaper default
for routine reviews, and a published comparison against a single reviewer on recorded
outcomes.

## Tell me how it went

The one thing that would show whether the learning works is outcomes from real use. Weights
and calibration move whenever you record outcomes, so seeing them move proves nothing; what
matters is whether they make later verdicts better. Two ways to check, on your own work:

- `rncrn8 replay --without-learning` fits calibration and panel weights on your older
  outcomes and scores your newer ones with and without them, with a 95% interval on the
  difference. It says "too few" below 10 held-out decisions, so it needs about 50 recorded
  outcomes at the default split. It does not cover memory and source trust, which stay as
  learned in both.
- On some of the same targets, also ask your assistant for a plain review, and note which
  findings that turned out right came only from the panel, only from the plain review, or
  from both.

Then open an issue with the **Outcome report** template. No confidential detail is needed;
numbers and a sentence are enough.
