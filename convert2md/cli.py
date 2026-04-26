import asyncio
import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.traceback import install as install_rich_tracebacks

from convert2md.document import OutputWriter, Section, SourceKind
from convert2md.settings import USER_ENV_PATH, Settings, settings_from_cli
from convert2md.sources import AuthRequired, converter_for, detect


class OcrEngine(StrEnum):
    tesseract = "tesseract"
    gemini = "gemini"


app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Convert files, URLs, repos, PDFs, and YouTube videos into LLM-ready Markdown.",
)
config_app = typer.Typer(help="Inspect convert2md configuration.")
app.add_typer(config_app, name="config")

console = Console()
logger = logging.getLogger("convert2md")


Inputs = Annotated[
    list[str] | None,
    typer.Option(
        "--input",
        "-i",
        help="Input source. Repeat for multiple files, URLs, repos, PDFs, or videos.",
    ),
]
Output = Annotated[Path | None, typer.Option("--output", "-o", help="Output Markdown path.")]
Concurrency = Annotated[
    int | None,
    typer.Option(
        "--concurrency",
        "-j",
        min=1,
        max=16,
        help="Maximum sources to convert in parallel (1-16).",
    ),
]
CrawlDepth = Annotated[
    int | None,
    typer.Option("--crawl-depth", min=0, max=3, help="URL crawl depth. 0 converts only inputs."),
]
MaxPages = Annotated[
    int | None,
    typer.Option("--max-pages", min=1, max=500, help="Maximum URL pages to capture per input."),
]
YoutubeLanguages = Annotated[
    list[str] | None,
    typer.Option(
        "--language",
        "-l",
        help="YouTube transcript language code, in priority order. Repeat for fallback languages.",
    ),
]
Verbose = Annotated[
    bool,
    typer.Option("--verbose", "-v", help="Enable debug logging."),
]
AiExtract = Annotated[
    bool | None,
    typer.Option(
        "--ai-extract/--no-ai-extract",
        help="Also send a screenshot to Gemini and append its transcription. Needs GOOGLE_API_KEY.",
    ),
]


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    inputs: Inputs = None,
    output: Output = None,
    concurrency: Concurrency = None,
    inline_images: Annotated[
        bool | None,
        typer.Option(
            "--inline-images/--no-inline-images", help="Inline image assets as data URIs."
        ),
    ] = None,
    crawl_depth: CrawlDepth = None,
    max_pages: MaxPages = None,
    follow: Annotated[
        list[str] | None,
        typer.Option("--follow", help="Only crawl URLs matching this regex. Repeat to add more."),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Skip crawl URLs matching this regex. Repeat to add more."),
    ] = None,
    ai_extract: AiExtract = None,
    verbose: Verbose = False,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if not inputs:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)
    _install_logging(verbose)
    settings = settings_from_cli(
        output=output,
        concurrency=concurrency,
        inline_images=inline_images,
        crawl_depth=crawl_depth,
        max_pages=max_pages,
        follow=follow,
        exclude=exclude,
        ai_extract=ai_extract,
    )
    raise typer.Exit(asyncio.run(_convert(inputs, settings)))


@app.command("url")
def url_command(
    inputs: Inputs = None,
    output: Output = None,
    concurrency: Concurrency = None,
    crawl_depth: CrawlDepth = None,
    max_pages: MaxPages = None,
    follow: Annotated[
        list[str] | None,
        typer.Option("--follow", help="Only crawl URLs matching this regex. Repeat to add more."),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Skip crawl URLs matching this regex. Repeat to add more."),
    ] = None,
    ai_extract: AiExtract = None,
    verbose: Verbose = False,
) -> None:
    _run_kind(
        "url",
        inputs,
        output,
        concurrency,
        verbose,
        crawl_depth=crawl_depth,
        max_pages=max_pages,
        follow=follow,
        exclude=exclude,
        ai_extract=ai_extract,
    )


@app.command("image")
def image_command(
    inputs: Inputs = None,
    output: Output = None,
    concurrency: Concurrency = None,
    verbose: Verbose = False,
) -> None:
    """Transcribe one or more images (local paths or URLs) to Markdown via Gemini."""
    # Image conversion always requires Gemini; ai_extract is implicit.
    _run_kind("image", inputs, output, concurrency, verbose, ai_extract=True)


@app.command("git")
def git_command(
    inputs: Inputs = None,
    output: Output = None,
    concurrency: Concurrency = None,
    verbose: Verbose = False,
) -> None:
    _run_kind("git", inputs, output, concurrency, verbose)


@app.command("pdf")
def pdf_command(
    inputs: Inputs = None,
    output: Output = None,
    concurrency: Concurrency = None,
    ocr: Annotated[
        bool | None,
        typer.Option("--ocr/--no-ocr", help="Enable OCR on each page."),
    ] = None,
    ocr_engine: Annotated[
        OcrEngine | None,
        typer.Option(
            "--ocr-engine",
            help="OCR backend (only used when --ocr is on).",
            case_sensitive=False,
        ),
    ] = None,
    ocr_language: Annotated[
        str | None,
        typer.Option("--ocr-language", help="Tesseract language code (ignored by gemini)."),
    ] = None,
    ocr_dpi: Annotated[
        int | None,
        typer.Option("--ocr-dpi", min=72, max=600, help="Tesseract render DPI."),
    ] = None,
    ocr_render_dpi: Annotated[
        int | None,
        typer.Option(
            "--ocr-render-dpi",
            min=72,
            max=600,
            help="Page-render DPI used by the gemini engine.",
        ),
    ] = None,
    ocr_prompt_file: Annotated[
        Path | None,
        typer.Option(
            "--ocr-prompt-file",
            help="Custom prompt file for the gemini engine (overrides convert2md/prompts/transcribe.md).",
        ),
    ] = None,
    verbose: Verbose = False,
) -> None:
    _run_kind(
        "pdf",
        inputs,
        output,
        concurrency,
        verbose,
        pdf_ocr=ocr,
        pdf_ocr_engine=ocr_engine.value if ocr_engine else None,
        pdf_ocr_language=ocr_language,
        pdf_ocr_dpi=ocr_dpi,
        pdf_ocr_render_dpi=ocr_render_dpi,
        transcribe_prompt_file=ocr_prompt_file,
    )


@app.command("video")
def video_command(
    inputs: Inputs = None,
    output: Output = None,
    concurrency: Concurrency = None,
    language: YoutubeLanguages = None,
    verbose: Verbose = False,
) -> None:
    _run_kind("video", inputs, output, concurrency, verbose, youtube_languages=language)


@config_app.command("path")
def config_path() -> None:
    console.print(str(USER_ENV_PATH))


@config_app.command("show")
def config_show() -> None:
    settings = Settings()
    console.print_json(json.dumps(settings.model_dump(mode="json"), indent=2))


def _run_kind(
    kind: SourceKind,
    inputs: list[str] | None,
    output: Path | None,
    concurrency: int | None,
    verbose: bool,
    **overrides: object,
) -> None:
    if not inputs:
        raise typer.BadParameter("provide at least one --input/-i")
    _install_logging(verbose)
    settings = settings_from_cli(output=output, concurrency=concurrency, **overrides)
    raise typer.Exit(asyncio.run(_convert(inputs, settings, forced_kind=kind)))


async def _convert(
    inputs: list[str],
    settings: Settings,
    forced_kind: SourceKind | None = None,
) -> int:
    buffers: list[list[Section]] = [[] for _ in inputs]
    failures = 0
    semaphore = asyncio.Semaphore(settings.concurrency)

    async def run_one(index: int, source: str) -> None:
        nonlocal failures
        async with semaphore:
            try:
                kind = forced_kind or detect(source)
                converter = converter_for(kind, settings)
                async with asyncio.timeout(settings.converter_timeout_s):
                    buffers[index].extend(await converter.convert(source))
            except AuthRequired as exc:
                failures += 1
                logger.error("%s", exc)
            except Exception as exc:
                failures += 1
                logger.error("%s failed: %s", source, exc)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Converting inputs", total=None)
        async with asyncio.TaskGroup() as group:
            for index, source in enumerate(inputs):
                group.create_task(run_one(index, source))
        progress.update(task, description="Writing output")

    sections = [section for buffer in buffers for section in buffer]
    if not sections:
        logger.error("No sources converted successfully.")
        return 1
    await OutputWriter.finalize(settings.output, settings, sections)
    console.print(f"Wrote {len(sections)} section(s) to {settings.output}")
    return 0 if failures < len(inputs) else 1


def _install_logging(verbose: bool) -> None:
    install_rich_tracebacks(show_locals=False)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, markup=False)],
        force=True,
    )
