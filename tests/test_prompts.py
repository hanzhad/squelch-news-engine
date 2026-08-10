"""Prompt assembly from config/prompts.yaml."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from squelch.core.config import Config, load_config
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


@pytest.mark.parametrize("stage", ["classify", "summarize", "digest"])
def test_every_shipped_prompt_file_is_valid(stage: str) -> None:
    loaded = prompts.load_prompt(stage)

    assert loaded.system.strip()
    assert "$focus" in loaded.template


def test_the_shipped_model_ids_are_named_per_stage() -> None:
    models = prompts.load_models()

    assert models.classify and models.summarize and models.digest


def test_classification_and_write_up_use_different_models() -> None:
    """The whole point of the split: the cheap model judges, the good one writes."""
    models = prompts.load_models()

    assert models.classify != models.summarize


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


def test_digest_prompt_lists_every_article(config: Config, issue: IssueRecord) -> None:
    text = prompts.digest_prompt(config, days=7, issues=[issue, issue])

    assert text.count("- Title: Something shipped") == 2
    assert "These 2 articles" in text
    assert "last 7 days" in text


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
        prompts.digest_prompt(config, 7, [issue]),
    ):
        assert "EDITORIAL POLICY" in text
        # Every placeholder the shipped templates declare must have been filled.
        for placeholder in ("$focus", "$topics", "$title", "$source", "$url", "$body",
                            "$count", "$days", "$articles"):
            assert placeholder not in text
