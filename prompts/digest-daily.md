# Digest — daily

The morning-after roundup: everything the feed published since yesterday, read
back as one piece of writing. Runs every day into the same channel as
[digest-weekly.md](digest-weekly.md).

A reader gets three things: the date, a body that says what the day amounted
to, and the articles themselves as bare links at the end. The body is the whole
value — the links are already in the channel, and a paragraph that only
restates their titles has said nothing the list did not.

So the instruction that matters most here is the negative one. The failure mode
is a paragraph shaped like "X released A. Y launched B. Z raised C." — that is
the list again, in prose, and it is what a model reaches for by default. What
is wanted instead is the *outcome*: what these releases together mean for
somebody who builds with this stuff, said in the plainest available words.

Trends are not asked for here. One day rarely has a thread running through it,
and the body already carries whatever connection there is; the weekly is where
patterns across days belong.

The window is deliberately not named in words: it is normally one day, but the
command takes an override, and a prompt that said "yesterday" over a three-day
window would be a lie the model has no way to catch.

`$articles` is assembled in `src/squelch/llm/prompts.py`: title, URL and the
published write-up per article.

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

THE BODY — this is the part that matters

Three to five sentences of connected prose about the whole set.

- Say what actually shipped, then say what it adds up to. The second half is
  the point: the reader can see the titles for themselves, so tell them what
  it means for somebody building with this.
- Never write a sentence per article. "X released A, Y launched B, Z raised C"
  is the list again with commas, and it is the one thing this body must not
  be. Group what belongs together and name the connection.
- When two or three of the articles are really the same story, say so outright
  and spend your sentences there rather than covering everything evenly.
- On a thin day, write two sentences and stop. A quiet day plainly described
  beats a quiet day inflated.
- No throat-clearing. Do not open with "today saw", "the AI world", "a busy
  day" or "several major announcements".

THE REST

- The headline is one sentence naming what happened, for the archive. It is
  not shown to readers, so do not write it as a title.
- List the articles worth opening, at most six, most important first. Titles
  only — copy each title and URL exactly as given below, and never write a URL
  that does not appear in the list. Anything you would have said about an
  individual article belongs in the body.

ARTICLES

$articles
