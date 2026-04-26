import asyncio
import json
import mimetypes
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast
from urllib.parse import parse_qs, urldefrag, urlparse

import httpx

from convert2md import gemini
from convert2md.document import Asset, Converter, Section, SourceKind
from convert2md.render import RenderResult, render
from convert2md.settings import Settings

AUTH_HOSTS = (
    ".atlassian.net",
    ".sharepoint.com",
    ".notion.so",
    ".notion.site",
)

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})


class AuthRequired(RuntimeError):
    """Raised when the browser extension is the right auth path."""


class BaseConverter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings


def utc_now() -> datetime:
    return datetime.now(UTC)


# ---- File ------------------------------------------------------------------


class FileConverter(BaseConverter):
    source: ClassVar[SourceKind] = "file"

    async def convert(self, source: str) -> list[Section]:
        path = Path(source).expanduser()
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        body = (
            text if path.suffix.lower() in {".md", ".markdown", ".mdx"} else wrap_code(path, text)
        )
        return [
            Section(
                title=path.name,
                url=str(path.resolve()),
                source="file",
                captured_at=utc_now(),
                body=body.rstrip() + "\n",
            )
        ]


# ---- URL (crawl4ai-rendered) ----------------------------------------------


class UrlConverter(BaseConverter):
    """Render a URL via crawl4ai. Supports same-site crawl + optional AI Extract."""

    source: ClassVar[SourceKind] = "url"

    async def convert(self, source: str) -> list[Section]:
        require_public_host(source)
        sections: list[Section] = []
        seen: set[str] = set()
        pending: list[tuple[str, int]] = [(source, 0)]
        follow_rules = [re.compile(p) for p in self.settings.follow]
        exclude_rules = [re.compile(p) for p in self.settings.exclude]

        while pending and len(sections) < self.settings.max_pages:
            current, depth = pending.pop(0)
            normalized_current = normalize_crawl_url(current)
            if normalized_current in seen:
                continue
            seen.add(normalized_current)

            try:
                result = await render(
                    current,
                    self.settings,
                    want_screenshot=self.settings.ai_extract,
                )
            except Exception as exc:
                if not sections:
                    raise
                # Keep partial crawl but record failure in logs.
                import logging

                logging.getLogger("convert2md").warning("render failed for %s: %s", current, exc)
                continue

            ai_visual = None
            if self.settings.ai_extract and result.screenshot_png:
                ai_visual = await gemini.transcribe(
                    result.screenshot_png, "image/png", self.settings
                )

            sections.append(
                Section(
                    title=result.title or current,
                    url=result.url or current,
                    source="url",
                    captured_at=utc_now(),
                    body=result.markdown,
                    site=urlparse(result.url or current).hostname or None,
                    ai_visual=ai_visual or None,
                )
            )

            if depth >= self.settings.crawl_depth:
                continue
            queued = {normalize_crawl_url(url) for url, _ in pending}
            for link in _collected_links(result):
                if len(seen) + len(pending) >= self.settings.max_pages:
                    break
                normalized = normalize_crawl_url(link)
                if normalized in seen or normalized in queued:
                    continue
                if should_follow_url(
                    link,
                    root_url=source,
                    follow_rules=follow_rules,
                    exclude_rules=exclude_rules,
                ):
                    pending.append((link, depth + 1))
                    queued.add(normalized)
        return sections


def _collected_links(result: RenderResult) -> list[str]:
    """Pull http(s) links out of the rendered markdown."""
    return [
        normalize_crawl_url(match.group(1))
        for match in re.finditer(r"\]\((https?://[^\s)]+)\)", result.markdown)
    ]


# ---- Image (single image → Gemini transcribe) -----------------------------


class ImageConverter(BaseConverter):
    """Read a local or remote image and ask Gemini to transcribe it to Markdown."""

    source: ClassVar[SourceKind] = "image"

    async def convert(self, source: str) -> list[Section]:
        data, mime, title, url = await self._read_image(source)
        body = await gemini.transcribe(data, mime, self.settings)
        if not body:
            body = "_no transcription_"
        return [
            Section(
                title=title,
                url=url,
                source="image",
                captured_at=utc_now(),
                body=body.rstrip() + "\n",
                site=urlparse(url or "").hostname or None,
            )
        ]

    async def _read_image(self, source: str) -> tuple[bytes, str, str, str | None]:
        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            async with httpx.AsyncClient(timeout=self.settings.fetch_timeout_s) as client:
                response = await client.get(source)
                response.raise_for_status()
            mime = (response.headers.get("content-type", "") or "").split(";", 1)[0].strip().lower()
            if not mime.startswith("image/"):
                mime = mimetypes.guess_type(parsed.path)[0] or "image/png"
            return (
                response.content,
                mime,
                Path(parsed.path).name or source,
                str(response.url),
            )
        path = Path(source).expanduser()
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return await asyncio.to_thread(path.read_bytes), mime, path.name, None


# ---- Git -------------------------------------------------------------------


class GitConverter(BaseConverter):
    source: ClassVar[SourceKind] = "git"

    async def convert(self, source: str) -> list[Section]:
        with tempfile.TemporaryDirectory(prefix="convert2md-git-") as tmp:
            root = Path(tmp) / "repo"
            git_source = parse_git_source(source)
            await self._clone_or_copy(git_source, root)
            walk_root = root / git_source.subpath if git_source.subpath else root
            if not walk_root.exists():
                raise RuntimeError(f"Git path not found after clone: {git_source.subpath}")
            force_include = walk_root.is_file()
            paths = [walk_root] if walk_root.is_file() else sorted(walk_root.rglob("*"))
            sections = []
            for path in paths:
                if not path.is_file() or (not force_include and not self._include(path, root)):
                    continue
                rel = path.relative_to(root)
                try:
                    content = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                sections.append(
                    Section(
                        title=str(rel),
                        url=git_source.display_url,
                        source="git",
                        captured_at=utc_now(),
                        body=self._render_git_file(rel, path, content),
                        site=urlparse(git_source.display_url).hostname or None,
                    )
                )
            return sections

    def _render_git_file(self, rel: Path, path: Path, content: str) -> str:
        suffix = path.suffix.lower()
        if suffix == ".ipynb" and self.settings.git_convert_ipynb:
            return f"## `{rel}`\n\n{notebook_to_markdown(content)}\n"
        if suffix in {".md", ".mdx"} and self.settings.git_clean_markdown:
            return f"## `{rel}`\n\n{clean_markdown_content(content)}\n"
        fence = pick_fence(content)
        return f"## `{rel}`\n\n{fence}{language_for(path)}\n{content.rstrip()}\n{fence}\n"

    async def _clone_or_copy(self, source: "GitSource", target: Path) -> None:
        local = Path(source.remote).expanduser()
        if local.exists():
            await asyncio.to_thread(copy_tree, local, target, self.settings.git_excluded_dirs)
            return
        command = ["git", "clone", "--depth", "1"]
        if source.ref:
            command.extend(["--branch", source.ref])
        command.extend([source.remote, str(target)])
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode and source.ref:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                "--depth",
                "1",
                source.remote,
                str(target),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "-C",
                    str(target),
                    "fetch",
                    "--depth",
                    "1",
                    "origin",
                    source.ref,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode == 0:
                    proc = await asyncio.create_subprocess_exec(
                        "git",
                        "-C",
                        str(target),
                        "checkout",
                        "--detach",
                        "FETCH_HEAD",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(stderr.decode("utf-8", "replace").strip() or "git clone failed")

    def _include(self, path: Path, root: Path) -> bool:
        rel = path.relative_to(root)
        rel_posix = rel.as_posix()
        if any(part in self.settings.git_excluded_dirs for part in rel.parts):
            return False
        if any(
            rel_posix == item or rel_posix.startswith(f"{item}/")
            for item in self.settings.git_excluded_dirs
        ):
            return False
        if path.name in self.settings.git_excluded_files:
            return False
        return (
            path.suffix in self.settings.git_included_extensions
            or path.name in self.settings.git_included_filenames
        )


# ---- PDF -------------------------------------------------------------------


class PdfConverter(BaseConverter):
    source: ClassVar[SourceKind] = "pdf"

    async def convert(self, source: str) -> list[Section]:
        data, title, url = await self._read_pdf(source)
        if self.settings.pdf_ocr and self.settings.pdf_ocr_engine == "gemini":
            text = await gemini.ocr_pdf_pages(data, self.settings)
            assets: list[Asset] = []
        else:
            text, assets = await asyncio.to_thread(extract_pdf, data, self.settings)
        await gemini.describe_assets(assets, self.settings)
        body = text.strip() or "[PDF contained no extractable text.]"
        return [
            Section(
                title=title,
                url=url,
                source="pdf",
                captured_at=utc_now(),
                body=f"# {title}\n\n{body}\n",
                assets=assets,
                site=urlparse(url or "").hostname or None,
            )
        ]

    async def _read_pdf(self, source: str) -> tuple[bytes, str, str | None]:
        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            async with httpx.AsyncClient(timeout=self.settings.fetch_timeout_s) as client:
                response = await client.get(source)
                response.raise_for_status()
            return response.content, Path(parsed.path).name or source, str(response.url)
        path = Path(source).expanduser()
        return await asyncio.to_thread(path.read_bytes), path.name, None


# ---- Video -----------------------------------------------------------------


class VideoConverter(BaseConverter):
    source: ClassVar[SourceKind] = "video"

    async def convert(self, source: str) -> list[Section]:
        video_id = youtube_id(source)
        transcript = await asyncio.to_thread(fetch_transcript, video_id, self.settings)
        body = "\n".join(
            f"- [{format_seconds(item.get('start', 0.0))}] {item.get('text', '').strip()}"
            for item in transcript
            if item.get("text")
        )
        return [
            Section(
                title=f"YouTube transcript: {video_id}",
                url=source,
                source="video",
                captured_at=utc_now(),
                body=f"# YouTube transcript: {video_id}\n\n{body}\n",
                site="youtube.com",
            )
        ]


# ---- Detection / dispatch --------------------------------------------------


def detect(source: str) -> SourceKind:
    require_public_host(source)
    local = Path(source).expanduser()
    if local.exists() and local.is_dir():
        return "git"
    if local.exists() and local.is_file():
        suffix = local.suffix.lower()
        if suffix == ".pdf":
            return "pdf"
        if suffix in IMAGE_SUFFIXES:
            return "image"
        return "file"
    parsed = urlparse(source)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        return "video"
    if host == "github.com" and ("/tree/" in path or "/blob/" in path or path.count("/") <= 2):
        return "git"
    if host == "arxiv.org" and path.startswith("/pdf/"):
        return "pdf"
    if path.endswith(".pdf") or (not host and Path(source).suffix.lower() == ".pdf"):
        return "pdf"
    if any(path.endswith(ext) for ext in IMAGE_SUFFIXES):
        return "image"
    return "url"


def converter_for(kind: SourceKind, settings: Settings) -> Converter:
    converters: dict[SourceKind, type[BaseConverter]] = {
        "file": FileConverter,
        "url": UrlConverter,
        "image": ImageConverter,
        "git": GitConverter,
        "pdf": PdfConverter,
        "video": VideoConverter,
    }
    try:
        return cast(Converter, converters[kind](settings))
    except KeyError as exc:
        raise ValueError(f"no CLI converter for source kind {kind!r}") from exc


def require_public_host(source: str) -> None:
    parsed = urlparse(source)
    host = (parsed.hostname or "").lower()
    if any(host == value.lstrip(".") or host.endswith(value) for value in AUTH_HOSTS):
        raise AuthRequired(
            f"{host} needs an authenticated browser session. "
            "Use the convert2md Chrome extension for this page."
        )


# ---- Git URL parsing -------------------------------------------------------


@dataclass(slots=True, frozen=True)
class GitSource:
    remote: str
    display_url: str
    ref: str | None = None
    subpath: Path | None = None


def parse_git_source(source: str) -> GitSource:
    parsed = urlparse(source)
    host = (parsed.hostname or "").lower()
    if host != "github.com":
        return GitSource(remote=source, display_url=source)

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return GitSource(remote=source, display_url=source)

    owner, repo = parts[0], parts[1]
    remote = f"https://github.com/{owner}/{repo}.git"
    if len(parts) >= 4 and parts[2] in {"tree", "blob"}:
        ref = parts[3]
        subpath = Path(*parts[4:]) if len(parts) > 4 else None
        return GitSource(remote=remote, display_url=source, ref=ref, subpath=subpath)
    return GitSource(remote=remote, display_url=source)


# ---- URL crawl helpers -----------------------------------------------------


def normalize_crawl_url(url: str) -> str:
    clean, _ = urldefrag(url)
    return clean.rstrip("/")


def should_follow_url(
    url: str,
    *,
    root_url: str,
    follow_rules: list[re.Pattern[str]],
    exclude_rules: list[re.Pattern[str]],
) -> bool:
    parsed = urlparse(url)
    root = urlparse(root_url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if any(rule.search(url) for rule in exclude_rules):
        return False
    if follow_rules:
        return any(rule.search(url) for rule in follow_rules)
    root_path = root.path.rstrip("/")
    if not root_path:
        return parsed.hostname == root.hostname
    return parsed.hostname == root.hostname and (
        parsed.path == root_path or parsed.path.startswith(f"{root_path}/")
    )


# ---- Git tree helpers ------------------------------------------------------


def copy_tree(source: Path, target: Path, excluded: frozenset[str]) -> None:
    import shutil

    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*sorted(excluded)))


def wrap_code(path: Path, text: str) -> str:
    return f"# {path.name}\n\n```{language_for(path)}\n{text.rstrip()}\n```\n"


def pick_fence(text: str) -> str:
    """Choose a backtick fence long enough to wrap any backtick run inside `text`."""
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def notebook_to_markdown(content: str) -> str:
    """Render a Jupyter `.ipynb` JSON document as plain Markdown (cells in order)."""
    try:
        notebook = json.loads(content)
    except json.JSONDecodeError as exc:
        return f"_Failed to parse notebook: {exc}_"
    blocks: list[str] = []
    for cell in notebook.get("cells", []):
        source = cell.get("source", "")
        text = "".join(source) if isinstance(source, list) else str(source)
        text = text.rstrip()
        if not text:
            continue
        if cell.get("cell_type") == "code":
            fence = pick_fence(text)
            blocks.append(f"{fence}python\n{text}\n{fence}")
        else:
            blocks.append(text)
    return "\n\n".join(blocks).strip() or "_Notebook contained no cells._"


_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
_IMPORT_RE = re.compile(r"^import\s+.*$", re.MULTILINE)
_TR_OR_BR_RE = re.compile(r"</?tr>|<br\s*/?>", re.IGNORECASE)
_TABLE_CELL_RE = re.compile(r"</td>|</th>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<(?![a-z][a-z0-9+\-.]*://|mailto:)[^>]+>", re.IGNORECASE)
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def clean_markdown_content(content: str) -> str:
    """Strip YAML frontmatter, MDX imports, and inline HTML from a Markdown body."""
    content = _FRONTMATTER_RE.sub("", content)
    content = _IMPORT_RE.sub("", content)
    content = _TR_OR_BR_RE.sub("\n", content)
    content = _TABLE_CELL_RE.sub(" | ", content)
    content = _HTML_TAG_RE.sub("", content)
    content = _BLANK_LINES_RE.sub("\n\n", content)
    return content.strip()


def language_for(path: Path) -> str:
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "jsx",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".md": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".sh": "bash",
        ".sql": "sql",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".rb": "ruby",
    }.get(path.suffix.lower(), "")


# ---- PDF native extraction (PyMuPDF) ---------------------------------------


def extract_pdf(data: bytes, settings: Settings) -> tuple[str, list[Asset]]:
    """Native PyMuPDF extraction (with optional Tesseract OCR). Gemini OCR is async — see PdfConverter."""
    import fitz  # type: ignore[import-untyped]

    lines: list[str] = []
    assets: list[Asset] = []
    seen_xrefs: set[int] = set()
    with fitz.open(stream=data, filetype="pdf") as doc:
        for index, page in enumerate(doc, start=1):
            if settings.pdf_ocr:
                textpage = page.get_textpage_ocr(
                    language=settings.pdf_ocr_language,
                    dpi=settings.pdf_ocr_dpi,
                    full=False,
                )
                text = page.get_text("text", textpage=textpage).strip()
            else:
                text = page.get_text("text").strip()
            if text:
                lines.append(f"## Page {index}\n\n{text}")
            for asset in extract_pdf_page_images(doc, page, index, settings, seen_xrefs):
                assets.append(asset)
                lines.append(
                    f"![Page {index} image {len(assets)}](convert2md://asset/{len(assets) - 1})"
                )
    return "\n\n".join(lines), assets


def extract_pdf_page_images(
    doc: Any, page: Any, page_number: int, settings: Settings, seen: set[int]
) -> list[Asset]:
    images: list[Asset] = []
    for image in page.get_images(full=True):
        xref = int(image[0])
        if xref in seen:
            continue
        seen.add(xref)
        try:
            extracted = doc.extract_image(xref)
        except Exception:
            continue
        data = extracted.get("image")
        ext = extracted.get("ext") or "png"
        if not isinstance(data, bytes):
            continue
        if len(data) < settings.min_image_bytes or len(data) > settings.max_image_bytes:
            continue
        images.append(Asset(data=data, mime=f"image/{ext}"))
    return images


# ---- YouTube ---------------------------------------------------------------


def youtube_id(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
        if video_id:
            return video_id
    parts = [part for part in parsed.path.split("/") if part]
    if (
        host in {"youtube.com", "www.youtube.com", "m.youtube.com"}
        and parts
        and parts[0] in {"shorts", "embed", "live"}
        and len(parts) > 1
    ):
        return parts[1]
    query_id = parse_qs(parsed.query).get("v", [""])[0]
    if query_id:
        return query_id
    raise ValueError(f"could not detect YouTube video id from {url!r}")


def fetch_transcript(video_id: str, settings: Settings) -> list[dict[str, Any]]:
    from youtube_transcript_api import YouTubeTranscriptApi

    kwargs: dict[str, Any] = {}
    if settings.youtube_webshare_proxy_username and settings.youtube_webshare_proxy_password:
        from youtube_transcript_api.proxies import WebshareProxyConfig

        kwargs["proxy_config"] = WebshareProxyConfig(
            proxy_username=settings.youtube_webshare_proxy_username,
            proxy_password=settings.youtube_webshare_proxy_password.get_secret_value(),
        )
    api = YouTubeTranscriptApi(**kwargs)
    transcript = api.fetch(video_id, languages=settings.youtube_languages)
    return transcript.to_raw_data()


def format_seconds(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
