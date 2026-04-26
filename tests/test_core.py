import pathlib
import re
from datetime import UTC, datetime

import pytest

from convert2md.document import Asset, iso_utc, rewrite_placeholders, yaml_quote
from convert2md.settings import Settings
from convert2md.sources import (
    AuthRequired,
    ImageConverter,
    UrlConverter,
    clean_markdown_content,
    detect,
    extract_pdf,
    notebook_to_markdown,
    parse_git_source,
    pick_fence,
    should_follow_url,
    youtube_id,
)


def test_detect_core_sources() -> None:
    assert detect("https://youtu.be/abc") == "video"
    assert detect("https://github.com/org/repo") == "git"
    assert detect("https://github.com/org/repo/tree/main/docs") == "git"
    assert detect("paper.pdf") == "pdf"
    assert detect("README.md") == "file"
    assert detect("https://example.com/post") == "url"


def test_detect_auth_host() -> None:
    try:
        detect("https://tenant.sharepoint.com/sites/x")
    except AuthRequired:
        return
    raise AssertionError("expected AuthRequired")


def test_load_prompt_returns_canonical_text() -> None:
    from convert2md.gemini import load_prompt

    transcribe = load_prompt("transcribe")
    caption = load_prompt("caption")
    assert "{page_number}" in transcribe
    assert "transcribing a rendered surface" in transcribe
    assert "Describe this image" in caption


def test_writer_emits_visual_transcription_block(tmp_path: pathlib.Path) -> None:
    import asyncio

    from convert2md.document import OutputWriter, Section

    sections = [
        Section(
            title="demo",
            url="https://example.com/x",
            source="url",
            captured_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
            body="# Heading\n\nbody text\n",
            site="example.com",
            ai_visual="# Heading (visual)\n\nA flowchart with three nodes.\n",
        )
    ]
    out = tmp_path / "out.md"
    asyncio.run(OutputWriter.finalize(out, Settings(), sections))
    text = out.read_text()
    assert "ai_visual: true" in text
    assert "<!-- === VISUAL TRANSCRIPTION === -->" in text
    assert "A flowchart with three nodes." in text
    assert text.index("body text") < text.index("VISUAL TRANSCRIPTION")


def test_writer_helpers() -> None:
    assert iso_utc(datetime(2026, 4, 25, 10, 11, 12, 999, tzinfo=UTC)) == "2026-04-25T10:11:12Z"
    assert yaml_quote('a "b"\n') == '"a \\"b\\"\\n"'
    body = "![x](convert2md://asset/0)"
    asset = Asset(data=b"abc", mime="image/png", source_url="https://example.com/x.png")
    assert "data:image/png;base64,YWJj" in rewrite_placeholders(body, [asset], Settings())
    assert "https://example.com/x.png" in rewrite_placeholders(
        body,
        [asset],
        Settings(inline_images=False),
    )


def test_settings_accept_csv_env_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOLLOW", "docs,api")
    monkeypatch.setenv("YOUTUBE_LANGUAGES", "en,de")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.follow == ["docs", "api"]
    assert settings.youtube_languages == ["en", "de"]


def test_rewrite_placeholders_appends_description() -> None:
    body = "![diagram](convert2md://asset/0)\n\nnext paragraph"
    asset = Asset(
        data=b"png-bytes",
        mime="image/png",
        description="A flowchart with three nodes.",
        source_url="https://example.com/x.png",
    )
    out = rewrite_placeholders(body, [asset], Settings(inline_images=False))
    assert "https://example.com/x.png" in out
    assert "> A flowchart with three nodes." in out
    assert "next paragraph" in out


def test_parse_github_tree_and_blob_sources() -> None:
    tree = parse_git_source("https://github.com/python/cpython/tree/main/Doc")
    assert tree.remote == "https://github.com/python/cpython.git"
    assert tree.ref == "main"
    assert str(tree.subpath) == "Doc"

    blob = parse_git_source("https://github.com/org/repo/blob/main/README.md")
    assert blob.remote == "https://github.com/org/repo.git"
    assert blob.ref == "main"
    assert str(blob.subpath) == "README.md"


async def test_url_converter_fills_ai_visual_when_ai_extract_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UrlConverter renders via render() and, when ai_extract is on, also calls gemini.transcribe."""
    from convert2md import gemini, sources
    from convert2md.render import RenderResult

    fake_render = RenderResult(
        url="https://example.com/page",
        title="Real Title",
        markdown="# Real Title\n\nbody text\n",
        cleaned_html="<h1>Real Title</h1><p>body text</p>",
        screenshot_png=b"\x89PNG\r\n\x1a\n",
    )

    async def fake_render_call(url, settings, *, want_screenshot=False, want_pdf=False):
        assert want_screenshot is True
        return fake_render

    async def fake_transcribe(image, mime, settings, **kwargs):
        assert image == fake_render.screenshot_png
        assert mime == "image/png"
        return "# Real Title (visual)\n\nA flowchart.\n"

    monkeypatch.setattr(sources, "render", fake_render_call)
    monkeypatch.setattr(gemini, "transcribe", fake_transcribe)

    converter = UrlConverter(Settings(ai_extract=True, google_api_key="fake-key"))  # type: ignore[arg-type]
    sections = await converter.convert("https://example.com/page")

    assert len(sections) == 1
    section = sections[0]
    assert section.title == "Real Title"
    assert section.body.startswith("# Real Title")
    assert section.ai_visual is not None
    assert "flowchart" in section.ai_visual
    assert section.site == "example.com"


async def test_image_converter_calls_transcribe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """ImageConverter reads bytes and feeds them to gemini.transcribe."""
    from convert2md import gemini

    image_path = tmp_path / "diagram.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    seen: dict[str, object] = {}

    async def fake_transcribe(image, mime, settings, **kwargs):
        seen["image_len"] = len(image)
        seen["mime"] = mime
        return "# Diagram\n\nA flowchart with three nodes."

    monkeypatch.setattr(gemini, "transcribe", fake_transcribe)

    converter = ImageConverter(Settings(google_api_key="fake-key"))  # type: ignore[arg-type]
    sections = await converter.convert(str(image_path))

    assert len(sections) == 1
    assert sections[0].body.startswith("# Diagram")
    assert sections[0].source == "image"
    assert seen["mime"] == "image/png"
    assert seen["image_len"] == len(b"\x89PNG\r\n\x1a\nfake")


def test_should_follow_url_uses_same_path_default_and_regex_overrides() -> None:
    assert should_follow_url(
        "https://example.com/docs/page",
        root_url="https://example.com/docs",
        follow_rules=[],
        exclude_rules=[],
    )
    assert not should_follow_url(
        "https://example.com/blog/page",
        root_url="https://example.com/docs",
        follow_rules=[],
        exclude_rules=[],
    )
    assert not should_follow_url(
        "https://example.com/docs-v2/page",
        root_url="https://example.com/docs",
        follow_rules=[],
        exclude_rules=[],
    )
    assert should_follow_url(
        "https://example.com/blog/page",
        root_url="https://example.com/docs",
        follow_rules=[re.compile(r"/blog/")],
        exclude_rules=[],
    )
    assert not should_follow_url(
        "https://example.com/blog/page?print=1",
        root_url="https://example.com/docs",
        follow_rules=[re.compile(r"/blog/")],
        exclude_rules=[re.compile(r"print=1")],
    )


def test_notebook_to_markdown_renders_cells() -> None:
    notebook = """{
      "cells": [
        {"cell_type": "markdown", "source": ["# Title\\n", "intro"]},
        {"cell_type": "code", "source": "print('hi')"},
        {"cell_type": "raw", "source": "ignored"}
      ]
    }"""
    out = notebook_to_markdown(notebook)
    assert "# Title" in out
    assert "```python\nprint('hi')\n```" in out
    assert "ignored" in out  # raw cells are kept verbatim


def test_notebook_to_markdown_picks_longer_fence_for_code_with_backticks() -> None:
    notebook = '{"cells":[{"cell_type":"code","source":"print(\\"```\\")"}]}'
    out = notebook_to_markdown(notebook)
    assert out.startswith("````python")
    assert out.endswith("````")


def test_notebook_to_markdown_handles_invalid_json() -> None:
    out = notebook_to_markdown("not-json")
    assert out.startswith("_Failed to parse notebook")


def test_clean_markdown_preserves_autolinks() -> None:
    src = "see <https://example.com> and <em>nope</em>"
    out = clean_markdown_content(src)
    assert "<https://example.com>" in out
    assert "<em>" not in out


def test_clean_markdown_strips_frontmatter_imports_and_html() -> None:
    src = (
        "---\n"
        "title: foo\n"
        "tags: [a, b]\n"
        "---\n"
        "import Foo from './foo';\n"
        "\n"
        "# Heading\n"
        "<table><tr><td>name</td><td>value</td></tr></table>\n"
        "para<br>line\n"
    )
    out = clean_markdown_content(src)
    assert "title: foo" not in out
    assert "import Foo" not in out
    assert "<table>" not in out
    assert "name | value" in out
    assert "para\nline" in out


def test_pick_fence_grows_to_avoid_collision() -> None:
    assert pick_fence("plain") == "```"
    assert pick_fence("contains ``` already") == "````"
    assert pick_fence("nested ````` deeper") == "``````"


def test_extract_pdf_dedups_repeated_image_across_pages() -> None:
    fitz = pytest.importorskip("fitz")
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 64, 64))
    pixmap.set_rect(pixmap.irect, (200, 80, 80))
    png_bytes = pixmap.tobytes("png")
    doc = fitz.open()
    try:
        rect = fitz.Rect(0, 0, 200, 200)
        for _ in range(3):
            page = doc.new_page(width=200, height=200)
            page.insert_image(rect, stream=png_bytes)
        data = doc.tobytes()
    finally:
        doc.close()
    _, assets = extract_pdf(data, Settings(min_image_bytes=1))
    assert len(assets) == 1, "logo on three pages must yield one asset, not three"


def test_youtube_id_handles_common_url_forms() -> None:
    assert youtube_id("https://youtu.be/dQw4w9WgXcQ?t=1") == "dQw4w9WgXcQ"
    assert youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
