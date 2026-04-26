# convert2md — architecture

> If you're new here, start with the [project README](../README.md). This doc is for people changing the code. The output-format contract lives in [format.md](format.md).

## Table of contents

- [Product principles](#product-principles)
- [Repository layout](#repository-layout)
- [Settings — single source of truth](#settings--single-source-of-truth)
- [The document model](#the-document-model)
- [Converter design](#converter-design)
- [The Gemini layer](#the-gemini-layer)
- [URL rendering — crawl4ai + adapters](#url-rendering--crawl4ai--adapters)
- [Writer contract](#writer-contract)
- [Extension architecture](#extension-architecture)
- [Shared data, not shared code](#shared-data-not-shared-code)
- [Quality gates](#quality-gates)
- [What's deliberately out of scope](#whats-deliberately-out-of-scope)

## Product principles

- **One command for the common case.** `convert2md -i source -i source -o context.md`. The user does not need to know what kind of source each input is — `detect()` figures it out.
- **Sensible defaults.** Crawling, OCR, image policy, and AI Extract all have safe defaults. Power-user knobs exist but live in env vars / `.env`, not in the hot path.
- **Two surfaces, one contract.** CLI and extension are frontends over the same `Section` model and the same `.md` file format. The format spec ([format.md](format.md)) is the contract that lets the two evolve independently.
- **Few modules with clear ownership** beats many small helpers. ~10 Python files in `convert2md/`, ~10 JS files in `extension/`. No build step on the extension side.
- **Share data, not code.** Where the two runtimes need to agree (Gemini prompts), the truth is in `convert2md/prompts/*.md` and synced to `extension/prompts/`. We don't try to share Python and JavaScript implementations.

## Repository layout

```text
convert2md/
├── convert2md/
│   ├── __init__.py             # __version__
│   ├── __main__.py             # python -m convert2md → cli.app
│   ├── cli.py                  # Typer app, orchestration, progress
│   ├── settings.py             # one Pydantic Settings model — every default lives here
│   ├── document.py             # Section, Asset, OutputWriter, frontmatter helpers
│   ├── render.py               # async render() wrapping crawl4ai (URL → markdown + screenshot + pdf)
│   ├── adapters.py             # per-host adapter registry (Confluence/SharePoint/GitHub/...)
│   ├── sources.py              # detect() + Url/File/Git/Pdf/Video/Image converters
│   ├── gemini.py               # transcribe() primitive + describe_assets, ocr_pdf_pages
│   └── prompts/                # canonical caption.md + transcribe.md
├── extension/
│   ├── manifest.json           # MV3, options_ui, web_accessible_resources for prompts/
│   ├── popup.{html,css,js}     # 3 primary actions: Save / Copy / AI Extract
│   ├── options.{html,js}       # Google API key entry, validate-on-save
│   ├── sw.js                   # service worker orchestrator
│   ├── extract.js              # DOM-side Readability/Turndown pipeline + adapters
│   ├── gemini.js               # native fetch to Gemini REST (mirrors gemini.py)
│   ├── md.js                   # document assembly — same shape as Python document.py
│   ├── offscreen.{html,js}     # offscreen doc for large Blob downloads
│   ├── prompts/                # synced copy of convert2md/prompts/
│   ├── *.test.js               # node:test suites
│   └── vendor/                 # Readability + Turndown + GFM (no build step)
├── tests/
│   └── test_core.py            # Python unit tests + integration smoke for converters
├── docs/
│   ├── architecture.md         # this file
│   └── format.md               # output Markdown contract
├── Makefile                    # install, check, clean, nuke, help
└── pyproject.toml              # crawl4ai, pymupdf, typer, pydantic-settings, ...
```

There is no `scripts/` directory. Everything lives in the Makefile. Prompt-sync and the quality gate are inlined into `make check`.

Imports are absolute and stable:

```python
from convert2md.sources import detect
from convert2md.settings import Settings
from convert2md.document import OutputWriter
from convert2md.render import render
```

## Settings — single source of truth

There is exactly one configuration object: `convert2md.settings.Settings`. Every CLI flag default and every Gemini knob lives there. Converters and CLI commands read from it; they never carry their own defaults.

Precedence (highest first):

1. CLI flags passed into `Settings(**overrides)` via `settings_from_cli()`
2. Shell environment variables (no prefix; field name uppercased — `OUTPUT`, `CONCURRENCY`, `GOOGLE_API_KEY`, `AI_EXTRACT`, `BROWSER_HEADLESS`, ...)
3. Project `.env`
4. `~/.config/convert2md/.env`
5. Pydantic defaults declared on the `Settings` class

Adding a new knob means adding one field to `Settings`, optionally one CLI flag in `cli.py`, and a line in `.env.example`.

## The document model

Every converter returns a list of `Section` objects:

```python
@dataclass(slots=True, frozen=True)
class Section:
    title: str
    url: str | None
    source: SourceKind            # "url" | "git" | "pdf" | "video" | "file" | "image" | "extension"
    captured_at: datetime
    body: str                     # the DOM / native / parsed Markdown
    assets: list[Asset]           # successfully embedded image bytes (PDF / extension paths)
    site: str | None              # hostname; surfaced in frontmatter
    ai_visual: str | None         # Gemini's transcription of a rendered page or image (when --ai-extract)
```

`ai_visual` is the merge point for the unified vision pipeline. When set, the writer appends a `<!-- === VISUAL TRANSCRIPTION === -->` block under the DOM body inside the same Section — one file, two perspectives.

`Asset` is only for successfully embedded binary content. Failed image fetches become normal Markdown links so frontmatter `images:` stays accurate.

## Converter design

`detect(source)` is the only auto-routing function:

| Input shape | → | Kind |
|---|---|---|
| local directory | → | `git` |
| local `.pdf` | → | `pdf` |
| local `.png` / `.jpg` / `.jpeg` / `.webp` / `.gif` / `.bmp` | → | `image` |
| any other local file | → | `file` |
| YouTube URL (youtube.com / youtu.be / shorts / embed) | → | `video` |
| GitHub repo / tree / blob URL | → | `git` |
| `.pdf` URL or arxiv `/pdf/...` | → | `pdf` |
| image-suffix URL | → | `image` |
| anything else public | → | `url` |

Hosts known to require browser auth (`*.atlassian.net`, `*.sharepoint.com`, `*.notion.so`, `*.notion.site`) raise `AuthRequired` from `require_public_host()` and tell the user to use the extension.

Each converter is a class extending `BaseConverter`:

- **`FileConverter`** — passes through `.md` / `.mdx`, wraps source code in fenced blocks.
- **`UrlConverter`** — calls `render()` (crawl4ai); when `ai_extract=True`, also pipes the screenshot through `gemini.transcribe`. Supports same-site crawl up to `crawl_depth` / `max_pages`, with `follow` / `exclude` regex filters.
- **`GitConverter`** — clones / copies a tree, normalises GitHub `tree`/`blob` URLs, includes useful code/docs files. Renders `.ipynb` cells as Markdown and cleans `.md` / `.mdx` (frontmatter / MDX imports / inline HTML) when `git_convert_ipynb` / `git_clean_markdown` are on.
- **`PdfConverter`** — native text + embedded images via PyMuPDF. Optional OCR with two engines: `tesseract` (offline, PyMuPDF's `get_textpage_ocr`) or `gemini` (page-image OCR via `gemini.ocr_pdf_pages`).
- **`VideoConverter`** — fetches YouTube transcripts with language fallback and optional Webshare proxy.
- **`ImageConverter`** — reads bytes (local or http), calls `gemini.transcribe` with the page-transcribe prompt. Always uses Gemini.

## The Gemini layer

`convert2md/gemini.py` exposes one primitive and two specialised wrappers, all built on the SDK's native async client (`client.aio.models.generate_content`) and tenacity-backed retry. Concurrency is bounded by `Settings.gemini_concurrency` via `asyncio.Semaphore`.

```python
async def transcribe(image, mime, settings, *, page_number=None, total_pages=None) -> str:
    """One image → Markdown. The whole ball game."""

async def transcribe_pages(images: list[tuple[int, bytes]], settings) -> str:
    """N images → concatenated Markdown, one ## Page N per entry, parallel."""

async def describe_assets(assets: list[Asset], settings) -> None:
    """Fill Asset.description for every collected image (in-place, parallel, bounded)."""
```

Three call sites reuse `transcribe`:

- PDF Gemini OCR → `transcribe_pages(rendered_pdf_pages, settings)`
- URL `--ai-extract` → `transcribe(screenshot_png, "image/png", settings)`
- `ImageConverter` → `transcribe(image_bytes, mime, settings)`

Prompts come from `convert2md/prompts/*.md` via `importlib.resources`. Two files cover everything:

- `caption.md` — one paragraph for an image asset (used by `describe_assets`)
- `transcribe.md` — full Markdown transcription of any rendered surface; accepts optional `{page_number}` / `{total_pages}` markers

## URL rendering — crawl4ai + adapters

`convert2md.render.render(url, settings, *, want_screenshot=False, want_pdf=False)` is the single rendering entry point. It opens an `AsyncWebCrawler` with our settings (`browser_headless`, `browser_timeout_ms`) and returns a `RenderResult` containing `markdown`, `cleaned_html`, optional `screenshot_png` (decoded from crawl4ai's base64), optional `pdf_bytes`, `media`, `status_code`, and `title`.

Per-host behaviour is data-first via `SiteAdapter` entries in `convert2md/adapters.py`. Each adapter declares `scope` (CSS selectors that get passed to crawl4ai's `target_elements`), `strip` (selectors fed to `excluded_selector`), and an optional `transform` callable. Transforms run against `cleaned_html` via a thin `Convert2mdMarkdownGenerator` subclass of crawl4ai's `DefaultMarkdownGenerator`.

Adding a new site is one entry in `ADAPTERS`. Most sites are pure data (scope + strip). Only sites with semantic transforms (Confluence info-macro emoji, SharePoint FileViewer-as-link) need code.

## Writer contract

`OutputWriter.finalize(path, settings, sections)` is the only writer entry point. It writes once, in deterministic input order. The exact output is specified in [format.md](format.md). Both Python `document.py` and extension `md.js` must produce byte-identical output for the shared parts. Drift is caught by tests and by the `scripts/check.sh` prompt-sync check.

Container shape:

- file-level YAML frontmatter
- `<!-- === SECTION === -->` marker between sections
- section-level YAML frontmatter (string fields quoted, numeric fields unquoted)
- the section body
- when `section.ai_visual` is set: `<!-- === VISUAL TRANSCRIPTION === -->` block, then the Gemini transcription
- timestamps in UTC, second precision, trailing `Z`
- one final newline at EOF

## Extension architecture

The extension stays vanilla JavaScript with no build step. It loads directly from `extension/` in `chrome://extensions`.

- **`popup.{html,js,css}`** — three primary actions (Save / Copy / AI Extract), saved preferences, optional multi-tab selection. ⚙ link opens the options page.
- **`options.{html,js}`** — Google API key + Gemini model + concurrency, stored in `chrome.storage.local` (never synced). Validate-on-save makes a tiny generateContent call.
- **`sw.js`** — service-worker orchestrator. Injects vendor scripts + `extract.js`, runs DOM extraction per tab. For AI Extract, also calls `chrome.tabs.captureVisibleTab` and pipes the screenshot through `gemini.js`. Assembles via `md.js`, downloads via `chrome.downloads`.
- **`extract.js`** — runs in the target tab (ISOLATED world). Site adapters (Confluence, SharePoint, GitHub), Readability + Turndown pipeline, lazy-image resolution, image fetch with browser credentials.
- **`gemini.js`** — direct REST client for Gemini. Mirrors `convert2md/gemini.py`'s `transcribe` primitive: same prompts (loaded via `chrome.runtime.getURL("prompts/transcribe.md")`), same generation config (`temperature: 0`, `responseMimeType: "text/plain"`).
- **`md.js`** — document assembly + timestamp rules. Same shape as Python `document.py`; locked by parallel JS tests.
- **`offscreen.{html,js}`** — offscreen document so the SW can call `URL.createObjectURL` for large Markdown downloads.

## Shared data, not shared code

Cross-language code-sharing (Python ↔ JavaScript) is a tarpit. We share *data* instead:

- **Prompts** live in `convert2md/prompts/`. `make check` mirrors them into `extension/prompts/` automatically — edit the canonical file, run `make check`, the extension copy stays fresh.
- **Output format** is documented in [format.md](format.md). Both writers produce the same bytes; tests on both sides assert it.
- **Settings names match** — `gemini_model` / `geminiModel`, `gemini_concurrency` / `geminiConcurrency`, etc.
- **API keys are stored separately** by necessity (env vs `chrome.storage.local`). Behaviour stays identical because both sides read the same prompt files and use the same model defaults.

## Quality gates

Required check before any commit:

```bash
make check
#   sync convert2md/prompts/*.md → extension/prompts/
#   uv run ruff format --check convert2md tests
#   uv run ruff check convert2md tests
#   uv run mypy convert2md
#   uv run pytest -q
#   npm run lint --prefix extension
#   npm test --prefix extension
```

Tests focus on the shared contract, settings precedence, source detection, writer behavior, adapters, and smoke-level converter behavior. Network-touching tests use monkey-patched `render()` and `gemini.transcribe`. PyMuPDF tests build PDFs in-memory; no external fixtures.

## What's deliberately out of scope

- **Auth-cookie / CDP capture in the CLI.** crawl4ai supports persistent profiles (`use_persistent_context`), but our CLI does not expose it yet. The Chrome extension is the auth-gated capture story today.
- **Deep crawling.** `crawl_depth` is capped at 3. We do not aim to replace a real crawler.
- **Chrome Web Store packaging flow.** The extension is meant to be loaded unpacked from `extension/`; we do not package or publish to a registry.
- **Vector indexing / chunking / RAG.** convert2md is the *Markdown* layer. Anything downstream is the user's choice.
