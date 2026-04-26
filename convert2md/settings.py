from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CONFIG_DIR = Path.home() / ".config" / "convert2md"
USER_ENV_PATH = CONFIG_DIR / ".env"


class Settings(BaseSettings):
    """Single runtime configuration authority for the CLI. See README.md for precedence."""

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=(USER_ENV_PATH, Path(".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    output: Path = Path("out.md")
    concurrency: int = Field(default=4, ge=1, le=16)
    fetch_timeout_s: float = Field(default=30.0, gt=0)
    converter_timeout_s: float = Field(default=300.0, gt=0)

    inline_images: bool = True
    max_image_bytes: int = Field(default=2_000_000, ge=1)
    min_image_bytes: int = Field(default=1_024, ge=0)

    crawl_depth: int = Field(default=0, ge=0, le=3)
    max_pages: int = Field(default=25, ge=1, le=500)
    follow: Annotated[list[str], NoDecode] = Field(default_factory=list)
    exclude: Annotated[list[str], NoDecode] = Field(default_factory=list)

    git_included_extensions: frozenset[str] = frozenset(
        {
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".md",
            ".mdx",
            ".rst",
            ".txt",
            ".toml",
            ".yaml",
            ".yml",
            ".json",
            ".sql",
            ".sh",
            ".go",
            ".rs",
            ".java",
            ".rb",
        }
    )
    git_included_filenames: frozenset[str] = frozenset({"Dockerfile", ".env.example"})
    git_excluded_dirs: frozenset[str] = frozenset(
        {
            ".git",
            ".github",
            ".vscode",
            "__pycache__",
            ".pytest_cache",
            "node_modules",
            "build",
            "dist",
            "venv",
            ".venv",
            "target",
            ".cache",
            "vendor",
        }
    )
    git_excluded_files: frozenset[str] = frozenset(
        {"package-lock.json", "poetry.lock", "uv.lock", "yarn.lock"}
    )
    git_convert_ipynb: bool = True
    git_clean_markdown: bool = True

    pdf_ocr: bool = False
    pdf_ocr_engine: Literal["tesseract", "gemini"] = "tesseract"
    pdf_ocr_language: str = "eng"
    pdf_ocr_dpi: int = Field(default=150, ge=72, le=600)
    pdf_ocr_render_dpi: int = Field(default=220, ge=72, le=600)

    # Headless browser knobs (used by convert2md/render.py via crawl4ai).
    browser_headless: bool = True
    browser_timeout_ms: int = Field(default=30_000, ge=1_000, le=300_000)

    youtube_languages: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["en"])
    youtube_webshare_proxy_username: str | None = None
    youtube_webshare_proxy_password: SecretStr | None = None

    google_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_concurrency: int = Field(default=4, ge=1, le=32)
    describe_images: bool = False
    # Optional file overrides for the canonical prompts in convert2md/prompts/.
    transcribe_prompt_file: Path | None = None
    caption_prompt_file: Path | None = None
    # Opt-in page-as-image transcription on URL/Image converters.
    ai_extract: bool = False

    @field_validator("follow", "exclude", "youtube_languages", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return value
            return [part.strip() for part in text.split(",") if part.strip()]
        return value


def settings_from_cli(**overrides: Any) -> Settings:
    """Build settings from Typer kwargs without clobbering defaults with None."""

    clean = {key: value for key, value in overrides.items() if value is not None}
    return Settings(**clean)
