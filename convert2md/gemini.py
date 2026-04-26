"""Gemini integration. One primitive (`transcribe`), three call sites.

Lazy-imports `google.genai` so the dependency stays optional. Activate with
`pip install convert2md[gemini]` and `GOOGLE_API_KEY` set. Uses the SDK's
native async client (`client.aio`) and tenacity-backed retry — we add no
retry of our own. Bounds *concurrency* (our policy) via `asyncio.Semaphore`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from importlib import resources
from typing import TYPE_CHECKING, Any

from convert2md.settings import Settings

if TYPE_CHECKING:
    from convert2md.document import Asset

logger = logging.getLogger("convert2md")


def _generation_config() -> Any:
    """Deterministic transcription. Plain text — we want raw Markdown back."""
    from google.genai import types  # type: ignore[import-not-found]

    return types.GenerateContentConfig(
        temperature=0.0,
        max_output_tokens=8192,
        response_mime_type="text/plain",
    )


def load_prompt(name: str) -> str:
    """Read a canonical prompt file from `convert2md/prompts/<name>.md`."""
    return (resources.files("convert2md.prompts") / f"{name}.md").read_text(encoding="utf-8")


def _client(settings: Settings) -> Any:
    from google import genai  # type: ignore[import-not-found]

    if settings.google_api_key is None:
        raise RuntimeError("GOOGLE_API_KEY is required for Gemini features.")
    return genai.Client(api_key=settings.google_api_key.get_secret_value())


def _missing_sdk_warning(feature: str) -> None:
    logger.warning(
        "%s requested but google-genai is not installed. "
        "Install with `pip install convert2md[gemini]`.",
        feature,
    )


def _resolve_transcribe_prompt(settings: Settings) -> str:
    if settings.transcribe_prompt_file is not None:
        return settings.transcribe_prompt_file.expanduser().read_text(encoding="utf-8")
    return load_prompt("transcribe")


def _resolve_caption_prompt(settings: Settings) -> str:
    if settings.caption_prompt_file is not None:
        return settings.caption_prompt_file.expanduser().read_text(encoding="utf-8")
    return load_prompt("caption")


# --- Primitive: one image → Markdown ----------------------------------------


async def transcribe(
    image: bytes,
    mime: str,
    settings: Settings,
    *,
    page_number: int | None = None,
    total_pages: int | None = None,
    prompt_override: str | None = None,
) -> str:
    """Send one image to Gemini and return its Markdown transcription.

    The same primitive serves PDF-page OCR, URL `--ai-extract`, and the
    standalone image converter. The optional page markers append a small
    metadata footer so the model knows which page it is looking at.
    """
    if settings.google_api_key is None:
        raise RuntimeError("GOOGLE_API_KEY is required for transcribe().")
    try:
        from google.genai import types
    except ImportError:
        _missing_sdk_warning("transcribe")
        return ""

    template = (
        prompt_override if prompt_override is not None else _resolve_transcribe_prompt(settings)
    )
    prompt = template
    if page_number is not None and total_pages is not None:
        prompt = (
            template.replace("{page_number}", str(page_number))
            .replace("{total_pages}", str(total_pages))
            .rstrip()
            + f"\n\n### Page Metadata\n- Page number: {page_number}\n- Total pages: {total_pages}\n"
        )

    client = _client(settings)
    config = _generation_config()
    try:
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=types.Content(
                parts=[
                    types.Part.from_bytes(data=image, mime_type=mime),
                    types.Part(text=prompt),
                ]
            ),
            config=config,
        )
    except Exception as exc:
        logger.warning("Gemini transcribe failed (page=%s): %s", page_number, exc)
        return ""
    return (getattr(response, "text", None) or "").strip()


# --- Many images → concatenated, page-numbered Markdown ---------------------


async def transcribe_pages(
    images: list[tuple[int, bytes]],
    settings: Settings,
    *,
    mime: str = "image/png",
) -> str:
    """Transcribe (page_number, image_bytes) pairs in parallel, bounded."""
    if not images:
        return ""
    semaphore = asyncio.Semaphore(settings.gemini_concurrency)
    total = len(images)

    async def one(page_number: int, png: bytes) -> str:
        async with semaphore:
            text = await transcribe(png, mime, settings, page_number=page_number, total_pages=total)
        if not text:
            text = f"_No content extracted from page {page_number}._"
        if not text.startswith(f"## Page {page_number}"):
            text = f"## Page {page_number}\n\n{text}"
        return text

    results = await asyncio.gather(*(one(n, png) for n, png in images))
    return "\n\n".join(results).strip()


# --- Caption every collected image asset ------------------------------------


async def describe_assets(assets: list[Asset], settings: Settings) -> None:
    if not assets or not settings.describe_images or settings.google_api_key is None:
        return
    try:
        from google.genai import types
    except ImportError:
        _missing_sdk_warning("describe_images")
        return

    client = _client(settings)
    semaphore = asyncio.Semaphore(settings.gemini_concurrency)
    prompt = _resolve_caption_prompt(settings)
    config = _generation_config()

    async def describe_one(index: int, asset: Asset) -> None:
        async with semaphore:
            try:
                response = await client.aio.models.generate_content(
                    model=settings.gemini_model,
                    contents=types.Content(
                        parts=[
                            types.Part.from_bytes(data=asset.data, mime_type=asset.mime),
                            types.Part(text=prompt),
                        ]
                    ),
                    config=config,
                )
                text = (getattr(response, "text", None) or "").strip()
                if text:
                    assets[index] = replace(asset, description=text)
            except Exception as exc:
                logger.warning("Gemini describe failed for asset %d: %s", index, exc)

    async with asyncio.TaskGroup() as group:
        for index, asset in enumerate(assets):
            if asset.description is None:
                group.create_task(describe_one(index, asset))


# --- PDF wrapper (kept for the existing PdfConverter call site) -------------


def _render_pdf_pages(data: bytes, dpi: int) -> list[tuple[int, bytes]]:
    import fitz  # type: ignore[import-untyped]

    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pages: list[tuple[int, bytes]] = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for index in range(doc.page_count):
            page = doc.load_page(index)
            png = page.get_pixmap(matrix=matrix, alpha=False, annots=False).tobytes("png")
            pages.append((index + 1, png))
    return pages


async def ocr_pdf_pages(data: bytes, settings: Settings) -> str:
    """Render every PDF page to PNG and transcribe with Gemini in parallel."""
    pages = await asyncio.to_thread(_render_pdf_pages, data, settings.pdf_ocr_render_dpi)
    return await transcribe_pages(pages, settings)
