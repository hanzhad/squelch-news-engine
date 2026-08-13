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
| [case.md](case.md) | The reply under somebody's own post in the community forum | every case posted |
| [digest-daily.md](digest-daily.md) | The morning roundup over the day just gone | once a day |
| [digest-weekly.md](digest-weekly.md) | The look back over the whole week | once a week |

`case.md` is the odd one out and the one to read carefully before editing: it is
the only prompt here that answers a person rather than describing an article,
and its input is text a stranger wrote knowing a model would read it. It is
given no `$focus` on purpose — that policy decides what the feed publishes, and
a reply is not a verdict on whether somebody's experiment deserved to exist.

Its quoting discipline is not unique to it, though. Both roundups wrap their
article block in `ARTICLES BEGIN` / `ARTICLES END` and say in the system
instruction that what is inside is material, never instructions. An article is
second-hand by then — a roundup reads the write-up the feed published, not the
page — but stage two read the page, and a sentence aimed at a model can survive
being summarised. `_quoted` in `src/squelch/llm/prompts.py` mangles a marker
that appears inside the block, visibly rather than silently, so the pair the
prompt promises stays a pair.

The two digests share a Discord channel and a response schema, and differ only
in what they are told — which is the point of the split: the daily reports, the
weekly synthesises, and a reader who gets both must not feel they got the same
message twice. Sharing a schema has one standing hazard: a field description
travels with every request, so a rule written there reaches both roundups and
outranks the prose that contradicts it. Nothing about a period goes in the
schema — not a count, not a cap.

What the two do share on purpose is the `HOUSE STYLE` block, which is the same
text in both files and is compared by the tests. It holds what a reader would
notice in either roundup — sentence length, named subjects, the rule about
numbers, the banned nouns. Anything that is true of one period only belongs in
that period's own section, a few lines above it.

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
every stage still parses, still quotes `$focus`, leaves no placeholder unfilled,
keeps the two `HOUSE STYLE` blocks identical, and keeps quoted text from closing
the block it sits in. It cannot tell you the writing got worse.
