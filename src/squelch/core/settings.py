"""Runtime settings — everything that comes from the environment."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- credentials -------------------------------------------------------
    github_token: str = Field(default="", validation_alias="GITHUB_TOKEN")
    # Actions sets this to "owner/repo" automatically.
    github_repository: str = Field(default="", validation_alias="GITHUB_REPOSITORY")
    gemini_api_key: str = Field(default="", validation_alias="GEMINI_API_KEY")
    # Model ids live in config/models.yaml; these override them when set, so a
    # new model can be tried from the Actions UI without a commit.
    gemini_model: str = Field(default="", validation_alias="GEMINI_MODEL")
    gemini_classify_model: str = Field(default="", validation_alias="GEMINI_CLASSIFY_MODEL")
    discord_webhook_url: str = Field(default="", validation_alias="DISCORD_WEBHOOK_URL")
    # The weekly roundup reads differently from the firehose, so it can have a
    # channel of its own. Unset means both go to the same webhook.
    discord_digest_webhook_url: str = Field(
        default="", validation_alias="DISCORD_DIGEST_WEBHOOK_URL"
    )

    # --- throughput --------------------------------------------------------
    # GitHub throttles *content creation* (issues, comments) far below the
    # 5000 req/h REST budget, so the scraper deliberately leaves work on the
    # table and picks it up on the next cron tick.
    scrape_max_new_issues: int = 15
    scrape_delay_seconds: float = 2.0

    # Gemini free tier is measured in requests per minute; one call per article
    # with a pause between them keeps us clear of it.
    llm_batch_size: int = 20
    llm_delay_seconds: float = 5.0
    # Stage one sees only the top of an article — enough to tell news from noise.
    classify_body_chars: int = 1500

    publish_batch_size: int = 10
    publish_delay_seconds: float = 2.0

    # How far back the site build reads. Without a bound it would page through
    # every article ever published on every rebuild, and that cost grows with
    # the archive forever. The page and the feed therefore show a rolling
    # window, not the whole history — the issue tracker is the archive. Raise it
    # to show more; the extra requests are paid on every build.
    feed_window_days: int = 3

    # --- misc --------------------------------------------------------------
    request_timeout: float = 30.0
    # Bounded by GitHub's 65536-character issue body, not by disk.
    seen_max_entries: int = 3500

    @property
    def digest_webhook_url(self) -> str:
        """Where the weekly roundup goes — its own channel, or the feed's."""
        return self.discord_digest_webhook_url or self.discord_webhook_url

    @property
    def repo_owner(self) -> str:
        return self.github_repository.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        return self.github_repository.split("/", 1)[1]


@lru_cache
def get_settings() -> Settings:
    return Settings()
