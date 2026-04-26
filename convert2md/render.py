"""Thin async wrapper around crawl4ai. Renders a URL via headless Chromium and
returns clean Markdown plus optional screenshot/PDF bytes.

This is the single rendering entry point used by `UrlConverter`. Adapters
register declarative `target_elements` / `excluded_selector` config and can plug
a custom `MarkdownGenerationStrategy` through `convert2md/adapters.py`.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from typing import Any

from convert2md.settings import Settings

logger = logging.getLogger("convert2md")


@dataclass(slots=True)
class RenderResult:
    url: str
    title: str
    markdown: str
    cleaned_html: str
    screenshot_png: bytes | None = None
    pdf_bytes: bytes | None = None
    media: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    status_code: int | None = None


async def render(
    url: str,
    settings: Settings,
    *,
    want_screenshot: bool = False,
    want_pdf: bool = False,
) -> RenderResult:
    """Render `url` once via crawl4ai. Returns Markdown + optional bytes."""
    from crawl4ai import (  # type: ignore[import-untyped]
        AsyncWebCrawler,
        BrowserConfig,
        CrawlerRunConfig,
    )

    from convert2md.adapters import adapter_for, build_markdown_generator

    adapter = adapter_for(url)
    browser_cfg = BrowserConfig(
        headless=settings.browser_headless,
        verbose=False,
    )
    run_cfg = CrawlerRunConfig(
        screenshot=want_screenshot,
        pdf=want_pdf,
        page_timeout=settings.browser_timeout_ms,
        target_elements=adapter.scope or None,
        excluded_selector=",".join(adapter.strip) if adapter.strip else None,
        markdown_generator=build_markdown_generator(adapter),
        verbose=False,
    )

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        result = await crawler.arun(url=url, config=run_cfg)

    if not result.success:
        raise RuntimeError(f"crawl4ai failed for {url}: {result.error_message or 'unknown error'}")

    markdown = _coerce_markdown(result.markdown)
    title = _extract_title(result, markdown, fallback=url)
    screenshot_png = (
        base64.b64decode(result.screenshot) if want_screenshot and result.screenshot else None
    )
    return RenderResult(
        url=getattr(result, "redirected_url", None) or url,
        title=title,
        markdown=markdown,
        cleaned_html=result.cleaned_html or "",
        screenshot_png=screenshot_png,
        pdf_bytes=result.pdf if want_pdf else None,
        media=result.media or {},
        status_code=result.status_code,
    )


def _coerce_markdown(value: Any) -> str:
    """`result.markdown` may be a string, a `MarkdownGenerationResult`, or our adapter's str."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    # MarkdownGenerationResult has .raw_markdown / .fit_markdown / __str__
    raw = getattr(value, "raw_markdown", None) or getattr(value, "fit_markdown", None)
    if isinstance(raw, str) and raw:
        return raw
    return str(value)


def _extract_title(result: Any, markdown: str, *, fallback: str) -> str:
    metadata = getattr(result, "metadata", None) or {}
    title = metadata.get("title") or metadata.get("og:title")
    if title:
        return str(title).strip()
    # Fallback: first H1 line in markdown.
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback
