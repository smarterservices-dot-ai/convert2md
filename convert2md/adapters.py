"""Per-host adapters for the URL renderer.

Each `SiteAdapter` is data-first: a host predicate, an optional list of CSS
selectors to scope the body to, an optional list of selectors to strip, and
optionally a `transform` callable that mutates the cleaned-HTML before
crawl4ai's markdown generator runs. Adding a new site is one entry; complex
transforms are a one-line callback.

The registry powers both:
  * declarative scoping via crawl4ai's `target_elements` / `excluded_selector`
  * a thin `Convert2mdMarkdownGenerator` that runs the transform on
    `cleaned_html` before delegating to crawl4ai's `DefaultMarkdownGenerator`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# Type alias: an adapter transform receives a parsed BeautifulSoup tree and
# mutates it in place (or returns a new tree). Pure data shaping; no I/O.
TransformFn = Callable[[BeautifulSoup], BeautifulSoup]


@dataclass(slots=True, frozen=True)
class SiteAdapter:
    name: str
    host_match: Callable[[str], bool]
    scope: list[str] = field(default_factory=list)
    strip: list[str] = field(default_factory=list)
    transform: TransformFn | None = None


def _identity_adapter() -> SiteAdapter:
    return SiteAdapter(name="default", host_match=lambda _h: True)


def adapter_for(url: str) -> SiteAdapter:
    host = (urlparse(url).hostname or "").lower()
    for adapter in ADAPTERS:
        if adapter.host_match(host):
            return adapter
    return _identity_adapter()


# --- Transforms --------------------------------------------------------------


def _adapt_confluence(soup: BeautifulSoup) -> BeautifulSoup:
    info_emoji = {
        "confluence-information-macro-information": "ℹ️",
        "confluence-information-macro-warning": "⚠️",
        "confluence-information-macro-note": "🚫",
        "confluence-information-macro-tip": "💡",
        "confluence-information-macro-success": "✅",
    }
    for macro in soup.select("div.confluence-information-macro"):
        emoji = "ℹ️"
        raw_classes = macro.get("class")
        macro_classes: list[str] = (
            [raw_classes] if isinstance(raw_classes, str) else list(raw_classes or [])
        )
        for cls, sym in info_emoji.items():
            if cls in macro_classes:
                emoji = sym
                break
        bq = soup.new_tag("blockquote")
        bq.string = f"{emoji} {macro.get_text(' ', strip=True)}"
        macro.replace_with(bq)
    for drawio in soup.select("div.drawioDiagram"):
        img = drawio.select_one("img")
        if img:
            drawio.replace_with(img)
    return soup


def _adapt_sharepoint(soup: BeautifulSoup) -> BeautifulSoup:
    for viewer in soup.select("[data-sp-feature-tag='FileViewer']"):
        link = viewer.select_one("a[href]")
        if link:
            p = soup.new_tag("p")
            a = soup.new_tag("a", attrs={"href": str(link.get("href") or "")})
            a.string = link.get_text(" ", strip=True) or "attached file"
            p.append(a)
            viewer.replace_with(p)
        else:
            viewer.decompose()
    return soup


# --- Registry ---------------------------------------------------------------


ADAPTERS: list[SiteAdapter] = [
    SiteAdapter(
        name="confluence",
        host_match=lambda h: h.endswith(".atlassian.net"),
        scope=["#main-content", "div.wiki-content", "[data-testid='grid']"],
        strip=[
            "#comments-section",
            "#navigation",
            "#breadcrumb-section",
            ".page-metadata",
            "#likes-and-labels-container",
        ],
        transform=_adapt_confluence,
    ),
    SiteAdapter(
        name="sharepoint",
        host_match=lambda h: h.endswith(".sharepoint.com"),
        scope=["[data-automation-id='pageContentArea']", ".CanvasComponent"],
        strip=[
            "[data-automation-id='pageHeader']",
            "[data-automation-id='commentsWrapper']",
            "[data-automation-id='pageProperties']",
            "[data-sp-feature-tag='Navigation']",
            "[data-sp-feature-tag='Spacer']",
        ],
        transform=_adapt_sharepoint,
    ),
    SiteAdapter(
        name="github",
        host_match=lambda h: h == "github.com" or h.endswith(".github.com"),
        scope=["article.markdown-body", "[itemprop='text']", "main"],
    ),
    SiteAdapter(
        name="medium",
        host_match=lambda h: h == "medium.com" or h.endswith(".medium.com"),
        scope=["article", "[data-testid='storyContent']"],
        strip=[
            "[data-testid='headerSocialButtons']",
            "[data-testid='audioPlayButton']",
            "[data-testid='postFooterSocialButtons']",
            "footer",
            "nav",
        ],
    ),
    SiteAdapter(
        name="ms-learn",
        host_match=lambda h: h in {"learn.microsoft.com", "docs.microsoft.com"},
        scope=["main#main", "[role='main']", ".content"],
        strip=["#side-doc-outline", ".breadcrumbs", "#feedback", ".action-area-container"],
    ),
    SiteAdapter(
        name="mdn",
        host_match=lambda h: h.endswith(".mozilla.org"),
        scope=[".main-page-content", "article"],
        strip=[".sidebar", ".document-toc-container"],
    ),
    SiteAdapter(
        name="wikipedia",
        host_match=lambda h: h.endswith(".wikipedia.org"),
        scope=["#mw-content-text"],
        strip=[".mw-editsection", ".navbox", ".infobox", ".reference", "#toc", ".hatnote"],
    ),
    SiteAdapter(
        name="stackoverflow",
        host_match=lambda h: h in {"stackoverflow.com", "superuser.com", "serverfault.com"},
        scope=["#mainbar"],
        strip=[".js-post-menu", ".user-info", ".comments-link"],
    ),
]


# --- Markdown generator that runs the per-host transform --------------------


def build_markdown_generator(adapter: SiteAdapter) -> Any:
    """Return a crawl4ai MarkdownGenerationStrategy that respects the adapter."""
    from crawl4ai.markdown_generation_strategy import (  # type: ignore[import-untyped]
        DefaultMarkdownGenerator,
    )

    if adapter.transform is None:
        return DefaultMarkdownGenerator()
    return _Convert2mdMarkdownGenerator(adapter.transform)


def _Convert2mdMarkdownGenerator(transform: TransformFn) -> Any:
    """Build a subclass of crawl4ai's DefaultMarkdownGenerator that applies a transform.

    Constructed lazily so that importing convert2md.adapters does not require
    crawl4ai to be installed (e.g. during unit tests of pure helpers).
    """
    from crawl4ai.markdown_generation_strategy import (
        DefaultMarkdownGenerator,
    )

    class Convert2mdMarkdownGenerator(DefaultMarkdownGenerator):
        def generate_markdown(  # type: ignore[no-untyped-def]
            self,
            input_html: str,
            base_url: str = "",
            html2text_options: dict[str, Any] | None = None,
            content_filter=None,
            citations: bool = True,
            **kwargs: Any,
        ):
            soup = BeautifulSoup(input_html, "lxml")
            transform(soup)
            return super().generate_markdown(
                input_html=str(soup),
                base_url=base_url,
                html2text_options=html2text_options,
                content_filter=content_filter,
                citations=citations,
                **kwargs,
            )

    return Convert2mdMarkdownGenerator()
