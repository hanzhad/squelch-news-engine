# Digest — daily

The morning-after roundup: everything the feed published since yesterday, read
back as one piece of writing. Runs every day into the same channel as
[digest-weekly.md](digest-weekly.md).

A reader gets four things: the date, a two-sentence brief, a detailed block,
and the articles as bare links. The layering is the design, and it was arrived
at the hard way. One paragraph asked to be both skimmable and synthesised
produced neither — written plainly it came back a sentence per article, written
analytically it came back prose nobody finishes. Two fields, two jobs, two sets
of rules.

The failure mode to watch is still the same one: a paragraph shaped like "X
released A. Y launched B. Z raised C." is the list again, in prose, and it is
what a model reaches for by default. The brief is too short to become that. The
detail avoids it by grouping, and by ending on what the whole thing means for
somebody who builds with this.

Trends are refused here, out loud. Saying nothing about them was not enough:
the field exists in the schema shared with the weekly, so silence read as an
invitation and the first live daily came back with three of them — restating,
in nominal shorthand, the same three groupings the body had already made.
Which is also the whole distinction between the two periods, so it has to hold.

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

THE BRIEF — for somebody who will read nothing else

One or two sentences. Never three.

- The single most useful thing that happened, in the plainest words you have.
  If two things genuinely tie, name both and stop.
- Written for somebody exhausted. If it needs a second pass to land, rewrite it.
- Never a list. Never a semicolon. No more than two company names.
- This is not a title. Write it as something you would say out loud.

THE DETAIL — for whoever went on

Three to five sentences of connected prose. Four is a good day; six is too many.

- Say what shipped, grouped, and end with what it adds up to. That last
  sentence is the one thing the reader cannot get from the titles, and it is
  the first thing to go missing when the rest is written well. Do not let it.
- Never write a sentence per article. "X released A, Y launched B, Z raised C"
  is the list again with commas, and it is the one thing this must not be.
- When two or three of the articles are really the same story, say so outright
  and spend your sentences there rather than covering everything evenly.
- Do not restate the brief. It is directly above; start where it left off.
- On a thin day, write two sentences and stop. A quiet day plainly described
  beats a quiet day inflated.

WORKED EXAMPLE

Given a small local vision model, a GPU video toolkit update, regional
endpoints with data residency, and a large raise for managed fine-tuning:

  Brief:  Running models yourself got cheaper and easier on the same day
          renting them got easier to justify to a compliance team.

  Detail: Liquid AI put a vision model small enough for a laptop into
          llama.cpp, and Nvidia opened its GPU video decoders to plain Python
          on Jetson boards. Both push the same thing: the work moves onto
          hardware you already own. Renting moved too — Mistral will now keep
          European data in Europe, and River AI raised $1.1B to run
          fine-tuning as an API for teams who will never staff it. What
          changes for you is that the case for self-hosting stopped being
          mostly about price, and the case for a vendor stopped being mostly
          about convenience.

Note what it does not do: name every article, quote a version number, or open
with "several major announcements".

HOW TO WRITE IT

This is read half-awake, over coffee, by somebody who has already had a long
week. It has to give something up on one pass. Earning attention with density
is the failure here, not the goal.

- Short sentences, one idea each. If a sentence needs two commas to stay
  upright, it is two sentences.
- Name who did what. "Liquid AI put a 3B vision model into llama.cpp" — not
  "local multimodal execution gained broader engine support". An abstract noun
  as the subject is the fastest way to make this unreadable, and it is the
  habit to watch hardest.
- Skip version strings, parameter counts and codenames unless the number *is*
  the news. "A vision model small enough for a laptop" tells a reader more
  than "LFM2.5-VL-3B".
- No consultant nouns: capabilities, offerings, solutions, deployment options,
  considerations, developments. No "leverage", "enable", "unlock", "empower",
  "robust", "seamless".
- Say plainly what changes: what somebody can now do, stop doing, or stop
  paying for.
- No throat-clearing. Do not open with "today saw", "the AI world", "a busy
  day", "several major announcements" or "taken together".

THE REST

- Return no trends. A thread running across several days is the weekly
  roundup's job; here the body already carries whatever connection there is,
  and a list under it would say the same thing twice.
- List the articles worth opening, at most six, most important first. Titles
  only — copy each title and URL exactly as given below, and never write a URL
  that does not appear in the list. Anything you would have said about an
  individual article belongs in the body.

ARTICLES

$articles
