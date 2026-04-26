import asyncio
import base64
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal, Protocol

from convert2md.settings import Settings

SourceKind = Literal["url", "git", "pdf", "video", "file", "image", "extension"]
ASSET_RE = re.compile(r"convert2md://asset/(\d+)")


@dataclass(slots=True, frozen=True)
class Asset:
    data: bytes
    mime: str
    description: str | None = None
    source_url: str | None = None


@dataclass(slots=True, frozen=True)
class Section:
    title: str
    url: str | None
    source: SourceKind
    captured_at: datetime
    body: str
    assets: list[Asset] = field(default_factory=list)
    site: str | None = None
    ai_visual: str | None = None


class Converter(Protocol):
    source: ClassVar[SourceKind]

    async def convert(self, source: str) -> list[Section]:
        """Convert one input into one or more output sections."""


class OutputWriter:
    @classmethod
    async def finalize(cls, path: Path, settings: Settings, sections: list[Section]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        q = yaml_quote
        out = [
            "---",
            "convert2md: 1",
            f"generated_at: {q(iso_utc())}",
            f"sources: {len(sections)}",
            "---",
            "",
        ]

        for section in sections:
            out.extend(
                [
                    "<!-- === SECTION === -->",
                    "---",
                    f"title: {q(section.title)}",
                ]
            )
            if section.url:
                out.append(f"url: {q(section.url)}")
            out.extend(
                [
                    f"source: {q(section.source)}",
                    f"captured_at: {q(iso_utc(section.captured_at))}",
                ]
            )
            if section.site:
                out.append(f"site: {q(section.site)}")
            out.append(f"images: {len(section.assets)}")
            if section.ai_visual:
                out.append("ai_visual: true")
            out.extend(
                [
                    "---",
                    "",
                    rewrite_placeholders(section.body, section.assets, settings),
                    "",
                ]
            )
            if section.ai_visual:
                out.extend(
                    [
                        "<!-- === VISUAL TRANSCRIPTION === -->",
                        "",
                        section.ai_visual.rstrip(),
                        "",
                    ]
                )

        await asyncio.to_thread(path.write_text, "\n".join(out).rstrip() + "\n", encoding="utf-8")


def iso_utc(dt: datetime | None = None) -> str:
    value = dt or datetime.now(UTC)
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def yaml_quote(value: object) -> str:
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def rewrite_placeholders(body: str, assets: list[Asset], settings: Settings) -> str:
    body = _append_descriptions(body, assets)

    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(assets):
            return match.group(0)
        asset = assets[index]
        if not settings.inline_images and asset.source_url:
            return asset.source_url
        encoded = base64.b64encode(asset.data).decode("ascii")
        return f"data:{asset.mime};base64,{encoded}"

    return ASSET_RE.sub(replace, body)


_IMG_LINE_RE = re.compile(r"^(?P<indent>\s*)!\[[^\]]*\]\(convert2md://asset/(\d+)\).*$")


def _append_descriptions(body: str, assets: list[Asset]) -> str:
    if not any(asset.description for asset in assets):
        return body
    out: list[str] = []
    for line in body.splitlines():
        out.append(line)
        match = _IMG_LINE_RE.match(line)
        if not match:
            continue
        index = int(match.group(2))
        if index >= len(assets):
            continue
        description = assets[index].description
        if description:
            out.append(f"{match.group('indent')}> {description.strip()}")
    return "\n".join(out)
