# Prompts

What the feed says to the model, one file per stage. They sit at the top level
rather than under `config/` because this is the text you reread and rewrite most
often, and because a prompt is prose — a diff on it should read like a diff on
writing, not like a diff on a YAML block scalar.

| File | Stage | When it runs |
| --- | --- | --- |
| [classify.md](classify.md) | The judge: does this article get a slot at all | every scraped item |
| [summarize.md](summarize.md) | The writer: the blurb that stands in for the article | every article that survived |
| [review.md](review.md) | The rubric: a skill collection read skill by skill | articles routed to a channel with `review: true` |
| [digest.md](digest.md) | The weekly roundup over everything published | once a week |

Which model runs each stage is [config/models.yaml](../config/models.yaml);
what the feed is about is [config/feed.yaml](../config/feed.yaml). Neither
belongs here.

## The format

Two sections, matched by their headings:

- `## System` — the system instruction, sent as-is.
- `## Template` — the user turn, after placeholder substitution.

Both are taken **verbatim**, including line breaks and indentation. Everything
before the first heading is notes for whoever is editing, and any other section
is ignored — write as much context above `## System` as the prompt deserves.
The one thing a prompt body may not contain is a line starting with `## `, since
that would start a new section.

Placeholders are `$name` (`string.Template`), not `{name}`, so prompt text may
contain literal braces without blowing up a run. An unknown or mistyped
placeholder survives as text rather than raising — a typo costs you a bad
answer, never a failed cron tick. Which names are available differs per stage
and is listed in each file; they are filled in `src/squelch/llm/prompts.py`.

The JSON shape the model must return is never described in prose anywhere here:
the response schema travels with the request, and repeating it in words degrades
the answer.

## Editing them

Changing a prompt changes what the feed publishes, with no code change and no
deploy — the next scheduled run picks up whatever is on `main`. `tests/test_prompts.py`
runs offline against these exact files and is the only guard: it checks that
every stage still parses, still quotes `$focus`, and leaves no placeholder
unfilled. It cannot tell you the writing got worse.
