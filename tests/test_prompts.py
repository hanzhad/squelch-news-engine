"""Prompt assembly from the markdown files in prompts/."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from squelch.core.config import Config, DigestPolicy, load_config
from squelch.core.models import Period
from squelch.github.issues import IssueRecord
from squelch.llm import prompts


@pytest.fixture
def config() -> Config:
    return Config(
        title="Test",
        focus="Only substantive releases.\n\nFilter out: hype.",
        topics=["models", "security"],
        max_body_chars=40,
    )


@pytest.fixture
def issue() -> IssueRecord:
    return IssueRecord(
        number=7,
        title="Something shipped",
        labels=["status:1-raw"],
        html_url="https://github.com/o/r/issues/7",
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
        meta={"url": "https://example.com/a", "source": "vendor"},
        original="A long article body that will be truncated by max_body_chars.",
    )


@pytest.mark.parametrize(
    "stage", ["classify", "summarize", "review", "digest-daily", "digest-weekly"]
)
def test_every_shipped_prompt_file_is_valid(stage: str) -> None:
    loaded = prompts.load_prompt(stage)

    assert loaded.system.strip()
    assert "$focus" in loaded.template


def _write(directory: Path, stage: str, text: str) -> Path:
    (directory / f"{stage}.md").write_text(text, encoding="utf-8")
    return directory


def test_notes_above_the_first_heading_never_reach_the_model(tmp_path: Path) -> None:
    """The prose at the top of each file is for whoever edits it. It explains
    the placeholders and the format, and sending it would be both wasteful and
    confusing to the model."""
    directory = _write(
        tmp_path,
        "notes",
        "# Notes\n\nPlaceholders: `$focus`. Editing this changes the feed.\n\n"
        "## System\n\nBe strict.\n\n## Template\n\n$focus\n",
    )

    loaded = prompts.load_prompt("notes", directory)

    assert loaded.system == "Be strict."
    assert loaded.template == "$focus"


def test_prompt_bodies_keep_their_own_whitespace(tmp_path: Path) -> None:
    """Prompts are whitespace-sensitive prose: blank lines separate sections the
    model reads as sections, and continuation lines are indented under their
    bullet. Nothing may reflow them on the way in."""
    body = "EDITORIAL POLICY\n\n$focus\n\nRULES\n\n- One rule,\n  continued here."
    directory = _write(tmp_path, "spaced", f"## System\n\nS.\n\n## Template\n\n{body}\n")

    assert prompts.load_prompt("spaced", directory).template == body


def test_a_renamed_section_is_an_error_rather_than_half_a_prompt(tmp_path: Path) -> None:
    """A file with the heading typed wrong still reads fine to a human, so the
    failure has to be loud — otherwise the stage quietly runs with no system
    instruction at all."""
    directory = _write(tmp_path, "typo", "## System\n\nS.\n\n## Templates\n\n$focus\n")

    with pytest.raises(ValueError, match="`## Template`"):
        prompts.load_prompt("typo", directory)


def test_the_shipped_model_ids_are_named_per_stage() -> None:
    models = prompts.load_models()

    assert models.classify and models.summarize and models.review and models.digest


def test_the_per_article_stages_stay_off_the_digest_s_model() -> None:
    """Requests are metered per model per day, and the ceilings differ by an
    order of magnitude. The digest runs once a week and can afford a scarce,
    stronger model; classify and summarize run on every article and cannot.
    Moving either of them onto the digest's model drains it in an afternoon."""
    models = prompts.load_models()

    assert models.classify != models.digest
    assert models.summarize != models.digest
    assert models.review != models.digest


def test_classify_prompt_quotes_the_policy_verbatim(config: Config, issue: IssueRecord) -> None:
    text = prompts.classify_prompt(config, issue, 40)

    # The policy is the whole point of the prompt; paraphrasing it silently
    # would change what the feed publishes.
    assert config.focus.strip() in text
    assert "models, security" in text
    assert "Something shipped" in text
    assert "https://example.com/a" in text


def test_article_text_is_truncated_to_the_configured_length(
    config: Config, issue: IssueRecord
) -> None:
    text = prompts.classify_prompt(config, issue, 40)

    assert issue.text[:40] in text
    assert issue.text[:41] not in text


def test_missing_article_text_is_stated_rather_than_left_blank(config: Config) -> None:
    empty = IssueRecord(number=1, title="Headline only", meta={})

    assert "(no article text was captured)" in prompts.classify_prompt(config, empty, 40)


def test_unknown_source_and_url_degrade_to_a_word(config: Config) -> None:
    bare = IssueRecord(number=1, title="Hand-opened", meta={}, raw_body="Body.")

    text = prompts.classify_prompt(config, bare, 40)

    assert "Source: unknown" in text
    assert "URL: unknown" in text


@pytest.mark.parametrize("period", list(Period))
def test_digest_prompt_lists_every_article(
    config: Config, issue: IssueRecord, period: Period
) -> None:
    text = prompts.digest_prompt(config, period, period.days, [issue, issue])

    assert text.count("- Title: Something shipped") == 2
    assert "These 2 articles" in text


def test_the_digest_reads_the_write_up_rather_than_the_article(config: Config) -> None:
    """A roundup built from raw text could say things the channel and the
    archive never said. It reads what the feed published, and falls back to the
    article only for an issue opened by hand, which has no write-up."""
    written = IssueRecord(
        number=1, title="t", meta={}, original="The raw article.", summary="The write-up."
    )

    text = prompts.digest_prompt(config, Period.DAILY, 1, [written])

    assert "The write-up." in text
    assert "The raw article." not in text


def test_a_summary_that_fits_is_never_cut(config: Config) -> None:
    # The old 400-character slice landed inside a normal two-to-four sentence
    # write-up, and cut it mid-word with nothing to show it had happened.
    summary = "Word " * 100  # 500 characters
    issue = IssueRecord(number=1, title="t", meta={}, summary=summary)

    assert summary.strip() in prompts.digest_prompt(config, Period.DAILY, 1, [issue])


def test_an_over_long_gist_is_cut_at_a_word_boundary(config: Config) -> None:
    # The fallback path: no write-up, so the raw body stands in and it can run
    # to max_body_chars. Cut visibly, like every other limit in the codebase.
    tight = Config(focus="f", digest=DigestPolicy(summary_chars=100))
    issue = IssueRecord(number=1, title="t", meta={}, original="alpha bravo " * 50)

    text = prompts.digest_prompt(tight, Period.DAILY, 1, [issue])

    assert "…" in text
    assert "  Summary: alpha bravo" in text


def test_the_weekly_prompt_names_its_window_and_the_daily_one_does_not(
    config: Config, issue: IssueRecord
) -> None:
    """The daily takes a --days override for catching up after an outage, so a
    prompt that said "yesterday" would be a lie the model cannot catch. The
    weekly has no such override and reads better for naming the week."""
    assert "last 7 days" in prompts.digest_prompt(config, Period.WEEKLY, 7, [issue])
    # The property itself rather than a banned word: whatever window it is given,
    # the daily prompt comes out identical, so it cannot be claiming one.
    assert prompts.digest_prompt(config, Period.DAILY, 3, [issue]) == prompts.digest_prompt(
        config, Period.DAILY, 1, [issue]
    )


def test_a_literal_brace_in_the_policy_survives(issue: IssueRecord) -> None:
    braced = Config(focus="Let through: JSON like {\"a\": 1} with real detail.", topics=[])

    # str.format would raise here; Template does not care.
    assert '{"a": 1}' in prompts.classify_prompt(braced, issue, 40)


def test_an_unknown_placeholder_is_left_alone_rather_than_raising() -> None:
    assert prompts._fill("keep $unknown here", other="x") == "keep $unknown here"


def test_the_real_config_and_prompts_compose(issue: IssueRecord) -> None:
    config = load_config()

    for text in (
        prompts.classify_prompt(config, issue, 1500),
        prompts.summarize_prompt(config, issue),
        prompts.review_prompt(config, issue),
        prompts.digest_prompt(config, Period.DAILY, 1, [issue]),
        prompts.digest_prompt(config, Period.WEEKLY, 7, [issue]),
    ):
        assert "EDITORIAL POLICY" in text
        # Every placeholder the shipped templates declare must have been filled.
        for placeholder in ("$focus", "$topics", "$title", "$source", "$url", "$body",
                            "$count", "$days", "$articles"):
            assert placeholder not in text
