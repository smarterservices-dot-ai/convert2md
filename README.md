# convert2md

> Turn anything you read on the web — articles, docs, dashboards, repos, PDFs, YouTube — into one clean Markdown file your LLM can actually use.

`convert2md` is two tools sharing one output contract:

- **CLI** — for batch / scripted work. Renders URLs in headless Chromium via [crawl4ai](https://github.com/unclecode/crawl4ai), reads local files, clones Git repos, parses PDFs, fetches YouTube transcripts, and (optionally) sends a screenshot of any page to Gemini for an LLM-grade visual transcription.
- **Chrome extension** — for the page you're reading right now. One click → clean Markdown on disk or in your clipboard. A second click → AI Extract that screenshots and transcribes the rendered page.

Both tools write the same `.md` shape so you can mix them freely.

---

## Table of contents

- [Install](#install)
- [Quickstart — CLI](#quickstart--cli)
- [Quickstart — Chrome extension](#quickstart--chrome-extension)
- [What you get back](#what-you-get-back)
- [CLI reference](#cli-reference)
- [Configuration](#configuration)
- [Optional: Gemini for AI Extract & captions](#optional-gemini-for-ai-extract--captions)
- [Site adapters](#site-adapters)
- [Project layout](#project-layout)
- [Development](#development)
- [Deeper docs](#deeper-docs)
- [Contact](#contact)
- [License & attribution](#license--attribution)

---

## Install

You'll need [`uv`](https://docs.astral.sh/uv/) (the Python toolchain) and `make`. Most macOS / Linux setups already have `make`; install `uv` with `curl -LsSf https://astral.sh/uv/install.sh | sh`.

```bash
git clone https://github.com/smarterservices-dot-ai/convert2md.git
cd convert2md
make install
```

`make install` runs `uv sync` and then `crawl4ai-setup`, which downloads the headless Chromium build crawl4ai needs (~200 MB, one time). When it finishes you should see:

```
✔ convert2md installed.
  CLI:        uv run python -m convert2md --help
  Extension:  load extension/ unpacked in chrome://extensions
```

**Verify it works** (optional, hits the network):

```bash
uv run crawl4ai-doctor   # crawl4ai's own health check — confirms Chromium can render
```

> Prefer pip? `pip install -e .` then `crawl4ai-setup`. The Makefile is a thin wrapper around those two commands.

---

## Quickstart — CLI

The same command works for any source — `convert2md` auto-detects what you give it.

```bash
# A single file → Markdown
uv run python -m convert2md -i README.md -o readme.md

# A web page (headless render via crawl4ai, with site-aware cleanup)
uv run python -m convert2md -i https://github.com/anthropics/claude-code -o claude-code.md

# A whole GitHub repo
uv run python -m convert2md -i https://github.com/python/cpython -o cpython.md

# A PDF (native text + images)
uv run python -m convert2md -i paper.pdf -o paper.md

# A YouTube transcript
uv run python -m convert2md -i https://youtu.be/dQw4w9WgXcQ -o video.md

# Mix everything in one .md
uv run python -m convert2md \
    -i README.md \
    -i https://example.com \
    -i paper.pdf \
    -i https://youtu.be/dQw4w9WgXcQ \
    -o context.md
```

Need to force a source kind or pass source-specific options? Use the subcommands:

```bash
uv run python -m convert2md url   -i https://example.com/docs --crawl-depth 1
uv run python -m convert2md git   -i https://github.com/org/repo
uv run python -m convert2md pdf   -i paper.pdf --ocr --ocr-engine gemini
uv run python -m convert2md video -i https://youtu.be/abc --language en --language de
uv run python -m convert2md image -i diagram.png             # needs GOOGLE_API_KEY
```

Every command writes one `.md` document with all sources interleaved. See [What you get back](#what-you-get-back).

---

## Quickstart — Chrome extension

For pages behind login (Confluence, SharePoint, internal SSO docs), for pages where DOM extraction fails (heavy SPAs, canvas-rendered dashboards), or for the multi-tab "save what I'm reading right now" workflow.

### Load the extension once

The extension is unpacked — no zip, no Chrome Web Store. You point Chrome directly at the `extension/` folder in this repo.

1. **Open** `chrome://extensions` in Chrome (or Edge, Brave — any Chromium).
2. **Toggle on** "Developer mode" (top-right corner).
3. **Click** the "Load unpacked" button (top-left).
4. **Select** the `extension/` folder inside this repo (e.g. `~/code/convert2md/extension`).
5. The convert2md icon appears in your toolbar. Pin it (puzzle icon → pin) so it's one click away.

### Use it

Click the convert2md icon on any page. The popup shows three primary actions:

```text
┌──────────────────────────────────────┐
│ convert2md                       ⚙   │
├──────────────────────────────────────┤
│ favicon  Page title                  │
│          domain.com                  │
├──────────────────────────────────────┤
│ [ Save Markdown ] [ Copy ] [ AI… ]   │
├──────────────────────────────────────┤
│ ▾ More                               │
│   Filename, multi-tab, image policy  │
└──────────────────────────────────────┘
```

- **Save Markdown** — clean DOM extraction → file in your Downloads folder.
- **Copy** — same Markdown, on your clipboard. Paste straight into Claude.ai or ChatGPT.
- **AI Extract** — screenshots the visible viewport, sends to Gemini, appends a `<!-- === VISUAL TRANSCRIPTION === -->` block. Best for pages full of charts, diagrams, or dashboards. Disabled until you set an API key (see next section).

Open **▾ More** for a custom filename and the "Include images (inline as data URIs)" toggle (off by default — most page images are decoration that wastes LLM tokens).

**Two bonus actions you can use without opening the popup:**

- **Right-click any image → "Transcribe image with convert2md"** — fetches the image (with the page's session cookies, so it works on auth-gated images), sends it to Gemini, downloads a `.md` file with the transcription. Same Gemini settings as AI Extract. Needs the API key.
- **AI Extract on a YouTube tab** — automatically routes through Gemini's native YouTube support (no scraping, no transcript-API workaround). The downloaded `.md` is the video's transcript directly. Works for `youtube.com/watch`, `youtu.be`, `/shorts`, `/embed`, `/live`.

### Optional: enable AI Extract

Click the ⚙ icon in the popup header → paste a Google API key from [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) → **Save & validate**. The settings page makes a one-token Gemini call to confirm the key works before storing it.

The key is stored in `chrome.storage.local` only — never synced to your Google account, never sent anywhere except `generativelanguage.googleapis.com`.

### Reload after editing the extension

Hot-reload isn't a thing for unpacked extensions. After you change anything in `extension/`:

1. `chrome://extensions` → find the convert2md card.
2. Click the **circular reload arrow** in the bottom-right of the card.
3. Re-open the popup. You'll see your changes.

### Debug it

- **Popup console** — right-click the popup → **Inspect** → Console tab. Errors in `popup.js` show up here.
- **Service worker console** — `chrome://extensions` → click the **service worker** link on the convert2md card. Errors in `sw.js`, `gemini.js`, `md.js` show up here.
- **Page-side errors** — open DevTools on the page itself (F12). Errors in `extract.js` (which runs in the target tab) show up there.

---

## What you get back

A single Markdown file with YAML frontmatter and a section per source. Image placeholders, AI visual transcriptions, and the file format are all defined precisely in [docs/format.md](docs/format.md). A short example:

```markdown
---
convert2md: 1
generated_at: "2026-04-26T12:00:00Z"
sources: 1
---

<!-- === SECTION === -->
---
title: "Free Claude Code"
url: "https://github.com/Alishahryar1/free-claude-code/blob/main/README.md"
source: "url"
captured_at: "2026-04-26T12:00:00Z"
site: "github.com"
images: 0
ai_visual: true
---

# Free Claude Code

A lightweight proxy that routes Claude Code's Anthropic API calls to NVIDIA NIM…

<!-- === VISUAL TRANSCRIPTION === -->

# Free Claude Code
The README displays five badges: License MIT, Python 3.14, uv, Pytest, …
A central screenshot shows Claude Code running in NVIDIA NIM mode, transcribing…
```

The `<!-- === SECTION === -->` and `<!-- === VISUAL TRANSCRIPTION === -->` markers are stable. An LLM can split on them; a human can read both.

---

## CLI reference

Top-level command — auto-detects each input's source kind:

| Flag | Default | Notes |
|---|---|---|
| `-i`, `--input TEXT` | required | Repeat per source: file, URL, repo, PDF, YouTube link, image. |
| `-o`, `--output PATH` | `out.md` | Final Markdown path; parent directories are created. |
| `-j`, `--concurrency INT` | `4` | Parallel source conversions, 1–16. |
| `--inline-images / --no-inline-images` | inline | Inline collected images as base64 data URIs, or keep their source URL. |
| `--crawl-depth INT` | `0` | URL crawl depth, 0–3. `0` converts only the input pages. |
| `--max-pages INT` | `25` | Hard cap on URL pages captured per input, 1–500. |
| `--follow REGEX` | none | Repeatable. Only crawl URLs matching any pattern. |
| `--exclude REGEX` | none | Repeatable. Skip URLs matching any pattern. |
| `--ai-extract / --no-ai-extract` | off | Also screenshot rendered URLs/images and append a Gemini transcription. |
| `-v`, `--verbose` | off | Debug logging. |

Subcommands force a source kind and add source-specific options:

| Subcommand | Extra options |
|---|---|
| `convert2md url`   | `--crawl-depth`, `--max-pages`, `--follow`, `--exclude`, `--ai-extract` |
| `convert2md git`   | (none) |
| `convert2md pdf`   | `--ocr / --no-ocr`, `--ocr-engine [tesseract\|gemini]`, `--ocr-language CODE`, `--ocr-dpi INT`, `--ocr-render-dpi INT`, `--ocr-prompt-file PATH` |
| `convert2md video` | `-l`, `--language CODE` (repeat for fallback order; default `en`) |
| `convert2md image` | (none — always uses Gemini) |
| `convert2md config show` | Print effective settings as JSON. |
| `convert2md config path` | Print the user-wide env path. |

Knobs without a CLI flag — image bounds, fetch/converter timeouts, browser headless/timeout, YouTube proxy credentials, Gemini model/concurrency — live in env vars or one of the `.env` files. See [.env.example](.env.example) for the full list.

---

## Configuration

Precedence (highest first):

1. CLI flags
2. Shell environment variables
3. Project `.env`
4. `~/.config/convert2md/.env`
5. Built-in defaults

Variable names match the field name in upper case, no prefix: `OUTPUT`, `CONCURRENCY`, `GOOGLE_API_KEY`, `AI_EXTRACT`, `BROWSER_HEADLESS`, etc. Copy [`.env.example`](.env.example) to either `.env` or `~/.config/convert2md/.env` and uncomment what you need.

---

## Optional: Gemini for AI Extract & captions

Both surfaces use the same model and the same prompts (`convert2md/prompts/{transcribe,caption}.md`).

```bash
# Set your key once, anywhere convert2md will see it:
export GOOGLE_API_KEY=AIza...                      # shell, project .env, or ~/.config/convert2md/.env

# CLI: append a visual transcription of the rendered page
uv run python -m convert2md -i https://example.com --ai-extract -o out.md

# CLI: standalone image → Markdown
uv run python -m convert2md image -i diagram.png -o diagram.md

# Extension: open ⚙ in the popup, paste the key, then click "AI Extract" on any page.
```

Tunables (env vars, all optional): `GEMINI_MODEL` (default `gemini-2.5-flash`), `GEMINI_CONCURRENCY` (1–32, default 4), `TRANSCRIBE_PROMPT_FILE`, `CAPTION_PROMPT_FILE`, `DESCRIBE_IMAGES=true` (caption every collected image asset).

---

## Site adapters

Some sites benefit from a per-host pre-pass. The CLI's `convert2md/adapters.py` ships adapters for:

| Host | Behaviour |
|---|---|
| `*.atlassian.net`     | Confluence: scope to `#main-content`, strip nav/breadcrumbs, render info-macro types as ℹ️/⚠️/🚫/💡/✅ blockquotes |
| `*.sharepoint.com`    | SharePoint: scope to `pageContentArea`, replace FileViewer widgets with simple links |
| `github.com`          | Scope to `article.markdown-body` (kills SPA chrome + JSON props) |
| `*.medium.com`        | Scope to `article`, strip social share + audio player |
| `learn.microsoft.com` | Scope to `main#main`, strip TOC sidebar, breadcrumbs, feedback widgets |
| `*.mozilla.org`       | MDN: scope to `.main-page-content`, strip sidebar |
| `*.wikipedia.org`     | Scope to `#mw-content-text`, strip [edit] links, navboxes, infoboxes |
| `stackoverflow.com`, `superuser.com`, `serverfault.com` | Scope to `#mainbar`, strip vote/comment widgets |

Adding a new site is one entry in `ADAPTERS`. Most adapters are pure data (scope + strip selectors); only ones with semantic transforms need code. The Chrome extension carries its own adapter set in `extension/extract.js` covering the same hosts.

---

## Project layout

```
convert2md/        Python package: CLI, settings, document contract, source converters, render layer
  prompts/         Canonical Gemini prompts (caption.md, transcribe.md)
extension/         Chrome MV3 extension, no build step
  prompts/         Mirror of convert2md/prompts/ (synced automatically by `make check`)
docs/              Architecture, output contract
tests/             Python tests
Makefile           install, check, clean
```

---

## Development

```bash
make install   # one-shot setup (uv sync + crawl4ai-setup)
make check     # ruff format check + ruff lint + mypy + pytest + JS lint + JS tests; auto-syncs prompts
make clean     # remove caches and build artifacts
make nuke      # clean + remove the local virtualenv
make help      # list every target
```

`make check` is the only quality gate you need. It auto-mirrors `convert2md/prompts/*.md` into `extension/prompts/` so you never hit a drift error — just edit the canonical file and re-run `make check`.

To re-format Python in place: `uv run ruff format convert2md tests`.

---

## Deeper docs

- [Architecture](docs/architecture.md) — module boundaries, settings precedence, converter contract, the merged DOM-plus-AI-Visual schema.
- [Output format](docs/format.md) — the exact bytes the writer emits; lock for downstream tooling.

---

## Contact

Built and maintained by **Rostand Kennezangue / smarterservices.ai**.

For commercial licensing, paid support, integration help, custom site adapters, or any other questions: **<info@smarterservices.ai>**.

---

## License & attribution

convert2md is released under the **MIT License** — see [LICENSE](LICENSE) for the full text.

You can use it commercially, modify it, embed it, redistribute it. The one thing the MIT terms ask for in return is **attribution**: any copy or substantial portion of the software must include the copyright notice. Please honour that. If you build on convert2md in a product, internal tool, research paper, blog post, or video, credit the project visibly. Suggested form:

> Built with [convert2md](https://github.com/smarterservices-dot-ai/convert2md) by Rostand - info@smarterservices.ai.
