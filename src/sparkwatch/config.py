from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ValidationError, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

LIVE_FORUM_HOST = "forums.developer.nvidia.com"
MAX_LIVE_REQUESTS_PER_SECOND = 1.0


def config_path() -> Path:
    explicit = os.environ.get("SPARKWATCH_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()
    local = Path.cwd() / "config.toml"
    if local.is_file():
        return local.resolve()
    return PROJECT_ROOT / "config.toml"


class GeneralSettings(BaseModel):
    db_path: str = "data/sparkwatch.db"
    log_level: str = "INFO"


class CategorySettings(BaseModel):
    id: int
    path: str
    name: str


class ForumSettings(BaseModel):
    base_url: str = "https://forums.developer.nvidia.com"
    user_agent: str = "sparkwatch/0.1"
    requests_per_second: float = 1.0
    max_retries: int = 5
    timeout_s: float = 30.0
    posts_batch_size: int = 50
    categories: list[CategorySettings] = []

    @model_validator(mode="after")
    def _check_rate_safety(self) -> ForumSettings:
        if self.requests_per_second <= 0:
            raise ValueError(
                "forum.requests_per_second must be greater than 0; zero never means unlimited"
            )
        host = (urlparse(self.base_url).hostname or "").lower()
        if host == LIVE_FORUM_HOST and self.requests_per_second > MAX_LIVE_REQUESTS_PER_SECOND:
            raise ValueError(
                "forum.requests_per_second must be at most"
                f" {MAX_LIVE_REQUESTS_PER_SECOND} against the live forum;"
                " mock the network or point base_url at a test host instead"
            )
        return self


class SyncSettings(BaseModel):
    lookback_days: int = 60
    refetch_recent_days: int = 7
    sync_now_time_budget_s: int = 90
    missing_post_recheck_days: int = 30


class RelevanceSettings(BaseModel):
    keywords: dict[str, list[str]] = {}
    weights: dict[str, float] = {}
    families: list[str] = []


class HardwareSettings(BaseModel):
    dual_spark_usable_gb: float = 240.0
    single_spark_usable_gb: float = 120.0
    kv_overhead_frac: float = 0.10


class RepoSettings(BaseModel):
    max_compare_commits: int = 50
    max_compare_pages: int = 3


class McpSettings(BaseModel):
    default_limit: int = 20
    max_limit: int = 200
    max_chars: int = 40000


class GlobalDigestSettings(BaseModel):
    weights: dict[str, float] = {
        "posts": 1.0,
        "posters": 1.5,
        "likes": 1.0,
        "new_topic": 1.0,
        "accepted": 1.0,
        "staff": 0.5,
    }
    relevance_cap: float = 40.0


class LlmSettings(BaseModel):
    base_url: str = "http://localhost:8000/v1"
    model: str = "local-model"
    api_key: str = "none"
    structured_output: str = "json_schema"
    chunk_tokens: int = 10000
    prompt_version: int = 1


class WebSearchSettings(BaseModel):
    enabled: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPARKWATCH_",
        env_nested_delimiter="__",
        extra="ignore",
        toml_file=config_path(),
    )

    general: GeneralSettings = GeneralSettings()
    forum: ForumSettings = ForumSettings()
    sync: SyncSettings = SyncSettings()
    relevance: RelevanceSettings = RelevanceSettings()
    hardware: HardwareSettings = HardwareSettings()
    repos: RepoSettings = RepoSettings()
    mcp: McpSettings = McpSettings()
    global_digest: GlobalDigestSettings = GlobalDigestSettings()
    llm: LlmSettings = LlmSettings()
    websearch: WebSearchSettings = WebSearchSettings()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_source = TomlConfigSettingsSource(settings_cls, toml_file=config_path())
        return (init_settings, env_settings, toml_source)

    @property
    def db_path(self) -> Path:
        path = Path(self.general.db_path).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    def forum_ids(self) -> list[int]:
        return [category.id for category in self.forum.categories]


@lru_cache(maxsize=8)
def _cached_settings() -> Settings:
    return Settings()


def get_settings() -> Settings:
    return _cached_settings()


def describe_validation_error(exc: ValidationError) -> str:
    """One readable line from a settings validation failure."""
    errors = exc.errors()
    if not errors:
        return str(exc)
    message = str(errors[0].get("msg", exc))
    return message.removeprefix("Value error, ")


def reset_settings() -> None:
    _cached_settings.cache_clear()
