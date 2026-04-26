# convert2md — output format

> The exact bytes the writer emits. Both Python `convert2md/document.py` and JS `extension/md.js` produce this. Treat it as a contract: downstream tools may parse on it, so changes here are breaking changes.

## Table of contents

- [The container](#the-container)
- [Rules](#rules)
- [The merged DOM + AI Visual schema](#the-merged-dom--ai-visual-schema)
- [Image placeholders](#image-placeholders)
- [Frontmatter field reference](#frontmatter-field-reference)

## The container

```md
---
convert2md: 1
generated_at: "2026-04-26T18:46:39Z"
sources: 2
---

<!-- === SECTION === -->
---
title: "README.md"
url: "/path/to/README.md"
source: "file"
captured_at: "2026-04-26T18:46:39Z"
images: 0
---

# Markdown body for the first section.

Body content goes here. Headings, paragraphs, lists, fenced code, GFM tables —
whatever the converter or LLM chooses to emit.

<!-- === SECTION === -->
---
title: "Architecture diagram page"
url: "https://example.com/arch"
source: "url"
captured_at: "2026-04-26T18:46:42Z"
site: "example.com"
images: 1
ai_visual: true
---

# DOM-extracted body for the second section.

![Architecture overview](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA…)

<!-- === VISUAL TRANSCRIPTION === -->

# Architecture overview (visual)

The diagram shows three boxes labeled API Gateway, Worker Pool, and Storage,
connected by arrows annotated with "HTTPS" and "S3 PUT". A dashed boundary
labeled "VPC us-east-1" surrounds all three.
```

## Rules

- `convert2md: 1` is the schema version marker. Bump when the container shape changes incompatibly.
- `sources:` is the number of sections in the file (one per converted input, except for URL crawls which can produce multiple).
- The section separator is **exactly** `<!-- === SECTION === -->`. Downstream parsers may split on this line.
- The visual transcription separator is **exactly** `<!-- === VISUAL TRANSCRIPTION === -->`. Present only when `ai_visual: true`.
- String frontmatter fields are double-quoted with `\"`, `\\`, `\n`, `\r`, `\t` escaped.
- Numeric (`images:`) and boolean (`ai_visual:`) fields are unquoted.
- Timestamps are ISO-8601 UTC with second precision and trailing `Z` (e.g. `2026-04-26T12:00:00Z`). No fractional seconds, no offset.
- Files end with exactly one final newline.

## The merged DOM + AI Visual schema

When a converter populates `Section.ai_visual` (CLI `--ai-extract` on URL/Image, extension AI Extract action), the writer:

1. Adds `ai_visual: true` to the section's frontmatter.
2. Emits the DOM body as usual.
3. Appends `<!-- === VISUAL TRANSCRIPTION === -->` followed by a blank line.
4. Emits the Gemini transcription verbatim (right-trimmed).
5. Adds a trailing blank line before the next section separator.

The two bodies live inside the same Section block on purpose. An LLM consumer can choose either or merge them; a human can read both. Either is self-describing in isolation.

## Image placeholders

While the writer is assembling output, image references inside `Section.body` are stored as opaque tokens:

```
![alt text](convert2md://asset/N)
```

`N` indexes into `Section.assets`. The writer then substitutes:

- if `inline_images=true` (default) **or** `Asset.source_url` is unset → `data:<mime>;base64,<b64>`
- if `inline_images=false` and `Asset.source_url` is set → the original source URL

If `Asset.description` is set (by `gemini.describe_assets`), the writer also appends a `> caption` blockquote underneath the image line, indented to match. This happens before the placeholder rewrite, so the caption rides along with whichever URL form the placeholder resolves to.

Successfully embedded images count toward `images:`. Failed image fetches are emitted as plain Markdown links elsewhere in the body and do **not** count.

## Frontmatter field reference

### File-level

| Field | Type | Always present | Notes |
|---|---|---|---|
| `convert2md`     | int    | yes | Schema version. Currently `1`. |
| `generated_at`   | string | yes | UTC, second precision. |
| `sources`        | int    | yes | Number of sections in this file. |

### Section-level

| Field | Type | Always present | Notes |
|---|---|---|---|
| `title`          | string  | yes | Page title, repo file path, PDF filename, or `"YouTube transcript: <id>"`. |
| `url`            | string  | when known | Absent for local files with no resolved source URL. |
| `source`         | string  | yes | One of `url`, `git`, `pdf`, `video`, `file`, `image`, `extension`. |
| `captured_at`    | string  | yes | UTC, second precision. |
| `site`           | string  | when known | Hostname, used for grouping in downstream tools. |
| `images`         | int     | yes | Count of successfully embedded image assets. |
| `ai_visual`      | boolean | only when true | Indicates a `VISUAL TRANSCRIPTION` block follows the body. |

If a downstream parser sees an unknown frontmatter key, it should ignore it (the format may add fields without bumping `convert2md:`). If it sees an unknown `source` value, it should treat the section as opaque text.
