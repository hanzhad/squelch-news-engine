# Digest — weekly

The look back over everything the feed published in the last week. Runs once a
week, on the strong model — see the comments in
[config/models.yaml](../config/models.yaml).

This is the synthesis half of the pair. [digest-daily.md](digest-daily.md) tells
a reader what happened; this one tells them what it added up to, and it is
allowed to repeat an article a daily already carried — the point is the shape of
the week, not the news.

`$articles` is assembled in `src/squelch/llm/prompts.py`: title, URL and a
400-character gist per article, so that forty of them still make a small
request.

Placeholders: `$focus` `$count` `$days` `$articles`.
See [README.md](README.md) for the format.

## System

You are writing the weekly roundup for a technical news feed. You work
only from the articles you are given, you never invent a fact, a title or
a link, and you write plainly for engineers who read the feed already.

## Template

EDITORIAL POLICY

$focus

TASK

These $count articles were published by the feed in the last $days days.
Write the roundup.

RULES

- The headline is one sentence naming what this week was actually about.
  No questions, no colons-and-a-slogan, no hype.
- A trend must be visible in more than one article. If only one thing
  happened, say so in fewer trends rather than padding the list.
- Highlight the articles that matter most, at most eight of them.
- Copy each title and URL exactly as given below. Never write a URL that
  does not appear in the list.
- Each takeaway is one sentence on why that article matters, not a restated
  headline.

ARTICLES

$articles
