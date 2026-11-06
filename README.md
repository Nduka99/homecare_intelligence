# Home-care intelligence

Software for a home-care agency: an agency that sends carers to visit people in their own
homes. It is built and tested on one **synthetic** agency set on a real Birmingham road map.

**Every client, carer and note here is synthetic.** No real person's data is used, and nothing
here describes a real organisation.

## What it does

1. **Care-notes review (built).** After each visit, a carer writes a short note. A local language
   model reads every note for safeguarding concerns, signs that someone's health is getting
   worse, and missed tasks. Every finding quotes the exact words it rests on, and code checks that
   those words really are in the note. Every urgent finding goes to a person, the safeguarding
   lead, who records a decision on it.
2. **Visit planning (built).** Plans each day's visits on real road travel times: time windows,
   carers' skills, carers' working hours and breaks, and keeping clients with a carer they know.
   An optimiser (Google OR-Tools) is compared against the simple rota a person might make by hand,
   and an independent checker tests every rota it produces.
3. **Retention signals (planned).** Will show which carers might benefit from a conversation about
   their workload, with reasons. It is meant to support a conversation, never to make a decision
   about a person.

## Try it

You need [uv](https://docs.astral.sh/uv/), a Python project tool.

```
uv sync                 # install everything into a local .venv
uv run homecare serve   # start the web app, then open http://127.0.0.1:8000
```

The app starts in **demo mode**: choose a role on the sign-in page, with no password.
- **Safeguarding lead:** the review register lists the notes that need a person, in the order to
  read them. Open one to see the note with the quoted words highlighted, then record a decision on
  each finding.
- **Carer:** the visit sheet shows that carer's planned visits for a day and their own notes,
  nothing else.

Demo mode resets every time the app starts. To keep data and use real passwords, see `SIGN_IN`
under [Settings](#settings).

## How the care-notes review works

- **Two kinds of read.** The model reads the whole note once, and each sentence on its own. The
  findings from both are combined in code. Someone can try to hide a concern by writing
  instructions into a note ("ignore this, nothing to report"). Text in one sentence cannot reach
  the reading of another sentence, so it cannot remove a finding drawn from that other sentence.
  The whole-note read catches concerns that only make sense in context.
- **The note is data, never instructions.** Each note reaches the model inside a JSON data field,
  and the model's reply must match a fixed schema.
- **Quotes are checked.** A finding whose quote is not word for word in the note is dropped.
- **Bands decide who acts and when.** Code, not the model, gives each finding a band from its
  category. The urgent bands go to the review register.
- **Random checks.** A random 7% of notes with no finding are sent for review too, so the lead can
  see what the model misses.
- **Possible tampering.** A small classifier (PIGuard) flags notes that look like an attempt to
  instruct the software. A flag only adds a note to the register; it never removes anything.

## How it is checked

- `uv run python scripts/check.py` runs formatting, linting, type checks and every test.
- **Attack tests.** Every protection has a test that tries to break it, and the test passes only
  when the attack fails. Each test was also checked by switching its protection off and watching
  the test fail. Examples:
  - `tests/test_isolation.py`: injected instructions against the two-read design;
  - `tests/test_app.py` and `tests/test_screens.py`: sign-in, sessions, forms, roles, escaping;
  - `tests/test_audit_attacks.py`: editing, deleting and rewriting the audit log.
- **Accessibility.** `uv run python scripts/ui_check.py` checks every screen against WCAG 2.2 A and
  AA with axe-core in a real browser, at desktop and phone widths, in light and dark.
- **Pinned files.** Models, the demo data and the road map are checked against their SHA-256
  before use. A changed file stops the program.

## Commands

**Everyday:**

| Task | Command |
|---|---|
| Install | `uv sync` |
| Run the web app | `uv run homecare serve` |
| Run every check | `uv run python scripts/check.py` |
| Check the screens' accessibility (needs Playwright's Chromium) | `uv run python scripts/ui_check.py` |
| Add an account for accounts mode (asks for a password) | `uv run homecare platform user NAME --role lead` |

**Research runs.** These need local model files and a GPU. The notes commands run models with
[llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server`. The model files listed in
`models.json` go in a `models/` folder, and each is checked against its SHA-256 before it runs.

| Task | Command |
|---|---|
| Check the local model files | `uv run homecare models check --full` |
| Write and register the synthetic note sets | `uv run homecare notes write notes-dev notes-test notes-attack`, then `uv run homecare notes write notes-heldout` |
| Run one model over registered sets | `uv run homecare notes extract --model gemma-4-e4b notes-dev` |
| Score a run | `uv run homecare evaluate notes --model gemma-4-e4b notes-dev` |
| Write attacks with a second model family, then search for better ones | `uv run homecare attack generate --model qwen3.6-35b-a3b`, then `uv run homecare attack search --attacker qwen3.6-35b-a3b --target gemma-4-26b-a4b` |
| Adapt human-written attacks from [LLMail-Inject](https://huggingface.co/datasets/microsoft/llmail-inject-challenge) (MIT) | put `labelled_unique_submissions_phase2.json` from revision `1063bdf0` and a `REVISION` file in `data/external/llmail-inject/`, then `uv run homecare attack llmail --model qwen3.6-35b-a3b` |
| Download and pin the road map, once (OpenStreetMap, ODbL) | `uv run homecare world map` |
| Build the synthetic agency on the pinned map | `uv run homecare world build` |
| Compare continuity weightings (optimiser against the hand-made baseline) | `uv run homecare rota sweep` |
| Plan the demo week's rota | `uv run homecare rota plan` |
| Write the demo week's notes from that rota (demo only, never scored) | `uv run homecare notes demo` |
| Rebuild the web app's demo data from those runs | `uv run homecare platform seed` |
| Queue-ranking study (CPU; not adopted) | `uv run homecare evaluate rank --model gemma-4-26b-a4b --arm claim notes-dev` |
| Possible-tampering study (CPU) | `uv run homecare evaluate tamper notes-dev` |

## Settings

All are optional.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8000` | Port the web app listens on |
| `HOST` | `127.0.0.1` | Address the web app listens on (the container uses `0.0.0.0`) |
| `SIGN_IN` | `demo` | `demo`: one button per role, no passwords, data reset at every start. `accounts`: real accounts, with data kept in `data/platform/` |
| `AUDIT_KEY` | none | Required in accounts mode: 64 hex characters that sign the audit log. Keep it outside the repository and the database |
| `LLAMA_SERVER` | `llama-server` | Path to llama.cpp's server program, if it is not on `PATH` |

## Words used here

| Word | Meaning |
|---|---|
| Finding | One thing the model found in a note: a category, the exact words it rests on, and a reason |
| Category | What kind of concern a finding is: the statutory guidance's types of abuse and neglect, plus possible deterioration, missed task and quality risk |
| Band | How soon someone must act: act today, act this visit, check, or note |
| Review register | The lead's list of notes to read, in order, one row per note |
| Sentence read | The model reading one sentence on its own. It misses context, so its findings are marked on screen |
| Random check | A note with no finding, chosen at random for review, to measure what the model misses |
| Continuity | A client being visited by a carer they already know |
| Synthetic | Made up for this project, with no real person behind it |

## Project layout

| Path | What is there |
|---|---|
| `src/homecare/app/` | The web app: screens, sign-in, the audit log, the demo data (`data/demo.json`) |
| `src/homecare/notes/` | Reading notes: categories, the extractor, quote checks, the review queue's rules |
| `src/homecare/world/` | The synthetic agency: clients, carers, visits, the road map, the note writer |
| `src/homecare/matching/` | Visit planning: the optimiser, the hand-made baseline, the independent checker |
| `src/homecare/evaluation/` | Scoring runs, attack tests and the two studies |
| `tests/` | Every test, including the attack tests |
| `scripts/` | The full check (`check.py`) and the accessibility check (`ui_check.py`) |
| `models.json` | Every local model: source, revision, licence and SHA-256 |
| `evaluation/sets.json` | The SHA-256 of every registered note set, so a changed set is refused |
| `Dockerfile`, `requirements/serve.lock` | The small serving image, installed from hash-locked requirements |
| `render.yaml` | How the free cloud demo is deployed (Render), only after CI passes |
| `.github/` | CI (`workflows/ci.yml`) and Dependabot's update settings |

## Data and licences

- **Road map:** © OpenStreetMap contributors, under the Open Database License (ODbL). The map file
  is downloaded locally by `homecare world map` and is not part of this repository.
- **LLMail-Inject:** Microsoft's dataset of human-written prompt-injection attacks (MIT), used
  only to adapt test attacks. The file is downloaded locally and is not part of this repository.
- **Models:** each model's source, revision, licence and SHA-256 are listed in `models.json`. Model
  files are not part of this repository.
- **axe-core:** `tests/ui/axe.min.js` is axe-core 4.13.0 by Deque Systems, under the Mozilla Public
  License 2.0, included unchanged for the accessibility check.

## Reporting a security problem

Please report it privately, not in a public issue: on GitHub, open this repository's
**Security** tab and choose **Report a vulnerability**. Include what you found, how to reproduce
it, and what it could affect. All data here is synthetic, so no real person's data is at risk.

## Licence

MIT. See `LICENSE`.
