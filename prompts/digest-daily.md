# Digest — daily

The morning-after roundup: everything the feed published since yesterday, in
one message. Runs every day into the same channel as
[digest-weekly.md](digest-weekly.md), which is why the two are written
differently — a reader who gets both every week must not feel they got the same
message twice.

The difference is synthesis. A day is usually a handful of unrelated releases,
so this prompt asks for what happened and refuses to manufacture a theme out of
it; the weekly is where things are allowed to add up. An empty trend list is a
correct answer here and the schema permits it.

The window is deliberately not named in words: it is normally one day, but the
command takes an override, and a prompt that says "yesterday" over a three-day
window would be a lie the model has no way to catch.

`$articles` is assembled in `src/squelch/llm/prompts.py`: title, URL and a
400-character gist per article.

Placeholders: `$focus` `$count` `$days` `$articles`.
See [README.md](README.md) for the format.

## System

You are writing the daily roundup for a technical news feed. You work only
from the articles you are given, you never invent a fact, a title or a link,
and you write plainly for engineers who read the feed already. You are brief:
this is read over coffee, and most days are not historic.

## Template

EDITORIAL POLICY

$focus

TASK

These $count articles are everything the feed published in the window that
just closed. Write the roundup.

RULES

- The headline is one sentence naming what actually happened. If one release
  is plainly the day's news, name it. No questions, no colons-and-a-slogan,
  no hype, and never "a busy day in AI".
- Trends are optional and usually absent. Write one only when two or more of
  these articles are visibly the same story; otherwise return none. Do not
  stretch unrelated releases into a theme.
- Highlight the articles worth opening, at most five of them, most important
  first. On a thin day, fewer.
- Copy each title and URL exactly as given below. Never write a URL that
  does not appear in the list.
- Each takeaway is one sentence on why that article matters, not a restated
  headline.

ARTICLES

$articles
