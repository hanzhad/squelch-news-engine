"""Canonicalization is the whole dedup contract: same article -> same uid."""

from __future__ import annotations

import pytest

from aetherfeed.core.urls import canonicalize, url_uid


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/post?utm_source=feedly&utm_medium=rss&utm_campaign=x",
        "https://example.com/post?fbclid=IwAR123",
        "https://example.com/post?gclid=abc",
        "https://example.com/post?dclid=abc",
        "https://example.com/post?gbraid=a&wbraid=b",
        "https://example.com/post?yclid=1&msclkid=2&twclid=3",
        "https://example.com/post?igshid=1&igsh=2",
        "https://example.com/post?mc_cid=1&mc_eid=2",
        "https://example.com/post?ref_src=twsrc&ref_url=http%3A%2F%2Fx",
        "https://example.com/post?cmpid=1&campaign_id=2",
        "https://example.com/post?at_medium=rss&at_campaign=64",
        "https://example.com/post?spm=a&share_id=b",
    ],
)
def test_known_tracking_params_are_stripped(url: str) -> None:
    assert canonicalize(url) == "https://example.com/post"


@pytest.mark.parametrize(
    "key",
    ["utm_source", "_hsenc", "pk_campaign", "mtm_source", "vero_id", "hsa_acc"],
)
def test_tracking_prefixes_are_stripped(key: str) -> None:
    assert canonicalize(f"https://example.com/post?{key}=value") == "https://example.com/post"


def test_tracking_params_are_matched_case_insensitively() -> None:
    assert canonicalize("https://example.com/post?UTM_Source=x&FBCLID=y") == (
        "https://example.com/post"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/item?id=42", "https://example.com/item?id=42"),
        ("https://example.com/list?page=2", "https://example.com/list?page=2"),
        ("https://example.com/s?q=rust&page=3", "https://example.com/s?page=3&q=rust"),
        # A meaningful param must survive even when it travels with tracking.
        ("https://example.com/item?id=42&utm_source=rss", "https://example.com/item?id=42"),
    ],
)
def test_meaningful_query_params_are_preserved(url: str, expected: str) -> None:
    assert canonicalize(url) == expected


def test_query_params_that_differ_only_in_value_stay_distinct() -> None:
    assert url_uid("https://example.com/item?id=42") != url_uid("https://example.com/item?id=43")


def test_www_prefix_is_removed() -> None:
    assert canonicalize("https://www.example.com/post") == "https://example.com/post"


def test_only_the_leading_www_is_removed() -> None:
    # "wwwsomething" is a real host, not a www prefix.
    assert canonicalize("https://wwwtest.example.com/post") == (
        "https://wwwtest.example.com/post"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://example.com:80/post", "http://example.com/post"),
        ("https://example.com:443/post", "https://example.com/post"),
        ("https://example.com:8443/post", "https://example.com:8443/post"),
        ("http://example.com:8080/post", "http://example.com:8080/post"),
    ],
)
def test_default_ports_are_dropped_and_others_kept(url: str, expected: str) -> None:
    assert canonicalize(url) == expected


def test_trailing_slash_is_trimmed() -> None:
    assert canonicalize("https://example.com/post/") == "https://example.com/post"
    assert canonicalize("https://example.com/a/b/") == "https://example.com/a/b"


def test_fragment_is_removed() -> None:
    assert canonicalize("https://example.com/post#section-2") == "https://example.com/post"


def test_scheme_and_host_are_lowercased_but_path_is_not() -> None:
    assert canonicalize("HTTPS://Example.COM/Post/Slug") == "https://example.com/Post/Slug"


def test_path_case_still_distinguishes_documents() -> None:
    assert url_uid("https://example.com/Post") != url_uid("https://example.com/post")


def test_surrounding_whitespace_is_ignored() -> None:
    assert canonicalize("  https://example.com/post  ") == "https://example.com/post"


def test_blank_query_values_are_kept() -> None:
    # "?draft" is not tracking; dropping it would merge two different pages.
    assert canonicalize("https://example.com/post?draft") == "https://example.com/post?draft="


def test_same_article_from_two_feeds_produces_one_uid() -> None:
    from_rss = "https://www.example.com/2025/01/big-story/?utm_source=rss&utm_medium=feed"
    from_newsletter = "HTTPS://example.com/2025/01/big-story#top"
    from_share = "https://www.example.com/2025/01/big-story/?fbclid=IwAR9&igshid=abc"

    assert url_uid(from_rss) == url_uid(from_newsletter) == url_uid(from_share)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://example.com/a", "https://example.com/b"),
        ("https://example.com/a", "https://other.com/a"),
        ("https://example.com/a", "http://example.com/a"),
        ("https://example.com/news?id=1", "https://example.com/news?id=2"),
        ("https://example.com/a", "https://example.com/a/b"),
    ],
)
def test_genuinely_different_urls_do_not_collide(left: str, right: str) -> None:
    assert url_uid(left) != url_uid(right)


def test_canonicalize_is_idempotent() -> None:
    # RawArticle canonicalizes on validation and url_uid canonicalizes again,
    # so a second pass must be a no-op or uids drift between call sites.
    for url in [
        "https://www.example.com/post/?utm_source=rss&b=2&a=1#frag",
        "http://example.com:80/x/y/",
        "https://example.com/s?q=a+b",
        "https://example.com/s?q=%D0%BD%D0%BE%D0%B2%D0%BE%D1%81%D1%82%D0%B8",
    ]:
        once = canonicalize(url)
        assert canonicalize(once) == once
        assert url_uid(once) == url_uid(url)


def test_uid_is_a_short_stable_hex_digest() -> None:
    uid = url_uid("https://example.com/post")
    assert len(uid) == 16
    assert all(char in "0123456789abcdef" for char in uid)
    assert uid == url_uid("https://example.com/post")


def test_bare_domain_and_root_slash_are_the_same_document() -> None:
    assert url_uid("https://example.com") == url_uid("https://example.com/")
