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
    # The article-by-article feed. That channel is the project's own working
    # view of the pipeline rather than something to be read start to finish, so
    # nothing else falls back to it — see discord_digest_webhook_url below.
    discord_webhook_url: str = Field(default="", validation_alias="DISCORD_WEBHOOK_URL")
    # The channel both roundups go to, daily and weekly. Required rather than
    # optional, and deliberately without the fallback to the feed webhook it
    # used to have: the roundups are the part that is read, and one landing in
    # the feed channel would be published to nobody.
    discord_digest_webhook_url: str = Field(
        default="", validation_alias="DISCORD_DIGEST_WEBHOOK_URL"
    )
    # Whether the digest channel is a forum rather than a text channel. A forum
    # has no message stream — every post *is* a thread — so the webhook has to
    # name the thread it is creating, and Discord answers 400 to a message
    # carrying neither a name nor an existing thread id. A text channel refuses
    # the same field just as firmly, so it cannot simply always be sent. Getting
    # this wrong costs a red run with Discord's own complaint in it, not silence.
    digest_forum: bool = Field(default=False, validation_alias="DIGEST_FORUM")
    # The channel that shows what the classifier threw away. Deliberately not
    # falling back to the feed webhook: rejects in the main feed would defeat
    # the point, so the publish-rejected stage refuses to run without this.
    discord_rejected_webhook_url: str = Field(
        default="", validation_alias="DISCORD_REJECTED_WEBHOOK_URL"
    )
    # The channel the skills rubric is routed to (see `only`/`skip` in
    # delivery.yaml). No fallback either: routing exists precisely so these
    # posts stay out of the feed channel.
    discord_skills_webhook_url: str = Field(
        default="", validation_alias="DISCORD_SKILLS_WEBHOOK_URL"
    )
    # The community forum's credential, and the only one that can *read* a
    # channel: a webhook posts and nothing else, so the case pipeline needs a
    # bot token or it needs to not exist. It also sends the replies, which is
    # why there is no webhook for that channel — see forum/bot.py.
    discord_bot_token: str = Field(default="", validation_alias="DISCORD_BOT_TOKEN")

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

    # How far back the rejected channel and the rescue pass look. Bounded so
    # that neither pages through every rejection ever made: the channel shows
    # a recent window rather than replaying the archive on first enable, and a
    # 👍 arriving after the rescue window has passed is simply too late.
    rejected_window_days: int = 3
    rescue_window_days: int = 14
    # 👍 reactions needed on a rejected issue before it is voted back in.
    rescue_min_reactions: int = 1

    # How far back the forum is read, and how many posts one tick will take on.
    # The window is what stops a first run — or a newly pointed forum_url —
    # from opening an issue for every case ever posted; anything older than
    # this was never going to get an answer worth having anyway. The batch caps
    # exist for the same reason every other stage has them: leftover work waits
    # for the next tick rather than being pushed through.
    cases_window_days: int = 3
    cases_max_new_issues: int = 10
    cases_batch_size: int = 10

    # How far back the site build reads. Without a bound it would page through
    # every article ever published on every rebuild, and that cost grows with
    # the archive forever. The page and the feed therefore show a rolling
    # window, not the whole history — the issue tracker is the archive. Raise it
    # to show more; the extra requests are paid on every build.
    feed_window_days: int = 3

    # --- misc --------------------------------------------------------------
    request_timeout: float = 30.0
    # The roundup's own ceiling. Every other stage sends one article and gets a
    # paragraph; a roundup sends up to sixty write-ups and thinks before it
    # answers, so the shared thirty seconds is not a slow call — it is the
    # normal one. It cost a day's digest on 2026-08-13, and the client-side
    # timeout that followed was not even retried.
    digest_request_timeout: float = 120.0
    # Bounded by GitHub's 65536-character issue body, not by disk.
    seen_max_entries: int = 3500

    @property
    def repo_owner(self) -> str:
        return self.github_repository.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        return self.github_repository.split("/", 1)[1]


@lru_cache
def get_settings() -> Settings:
    return Settings()
