---
description: Generate and stress-test grounded idea directions when you have NO specific idea yet. Researches first, generates from many angles, cuts the weak ones, and hands you a ranked shortlist to take into /brainstorm.
argument-hint: [optional: a focus area, a constraint, or "surprise me"]
---

# Ideate — Find Directions Worth Exploring

You are a **discovery partner** for the user when they're staring at a blank page. This is the
*upstream* step: the question is **"which directions even matter here?"** — not "let me refine the
one idea I already have." Faithfully adapted from compound engineering's `ideate` (grounding →
divergent frames → basis requirement → adversarial cut → ranked shortlist).

**When to use this vs `/brainstorm`:** use `/ideate` when you do NOT yet have a specific idea — you
have a vague focus area, or nothing at all. The moment you have one concrete idea to develop, skip
to `/brainstorm`. The output of `/ideate` (one chosen direction) feeds straight into `/brainstorm`.

**The user is NOT technical.** Everything you show them is plain language — directions, bets, and
trade-offs, never architecture or code. Do the heavy lifting in subagents; surface a clean
shortlist.

## Inputs

The focus hint: `$ARGUMENTS` — **optional**. Interpret it loosely:
- a **concept or focus area** ("a tool for freelance designers", "ways to make onboarding stickier")
- a **constraint** ("must be buildable solo", "low-cost quick wins")
- a **volume hint** ("give me 3", "go wide")
- **`surprise me`** — first-class, not a fallback: you discover the subjects yourself from the
  grounding material below.

If `$ARGUMENTS` is empty, ask **one** question: what rough area are they curious about, or do they
want a *surprise me* run? (More than ~2 questions total is a smell that this should be `/brainstorm`
instead.)

## Phase 1 — Grounding (research before generating)

**Generation without grounding is just remixing what's already in your head.** Run grounding
subagents *in parallel* before any idea-making:

- **`general-purpose` agent with `WebSearch`** — external prior art: who already does things in this
  space, what's been tried, what failed, what users complain about. This is critical — without it
  you regurgitate the obvious.
- **`Explore` agent** — if this repo already has product context (`documents/`, `plans/`,
  `reference/`), scan it so ideas build on what exists rather than ignoring it.

Pull back concrete findings: real products, real quotes, real gaps. You'll attach these to ideas as
their *basis* below.

## Phase 1.5 — Decompose the topic into angles

Before generating, split the topic into **3-5 orthogonal angles** ("what *parts* of this space are
there?" — distinct from the *how-to-think* frames below). Spread ideas across these angles so the
shortlist isn't all clustered in one corner. Skip this for atomic topics (naming one thing, a single
tagline) and for `surprise me`.

## Phase 2 — Diverge: generate from six frames

A single "give me ideas" prompt collapses into the model's most-trained, most-obvious directions.
Force breadth by generating through **six different frames** (run as parallel subagents, or inline
if small). Each frame should spread its ideas across the angles from Phase 1.5:

1. **Pain & friction** — where does this space hurt today?
2. **Inversion / removal / automation** — what if you removed a step, or did the opposite of the norm?
3. **Assumption-breaking** — which "everyone knows you have to…" assumption is actually optional?
4. **Leverage & compounding** — what small thing makes everything downstream easier?
5. **Cross-domain analogy** — how does another industry solve the same shape of problem?
6. **Constraint-flipping** — turn a limitation ("solo, no budget") into the defining feature.

### The basis requirement (the anti-fluff core)

**Every idea must carry a tagged basis, or it gets cut.** No exceptions:
- **`direct:`** — backed by a real observation/quote from grounding ("designers in forum X said…")
- **`external:`** — backed by named prior art ("Acme does this; the gap is…")
- **`reasoned:`** — backed by a written-out first-principles argument, not a vibe

An idea with no basis is a guess wearing a costume. Reject it.

## Phase 3 — Adversarial cut (refute, then rank)

Spawn a **fresh-context verifier** subagent (one that did NOT see the generation) and task it to
*refute* each candidate: do the quoted observations actually exist? Is the named prior art real? Is
the reasoning sound, or does it fall apart on one push?

Then arbitrate the survivors against a rubric and **reject with a one-line reason** each time:
- groundedness (is the basis real and strong?)
- expected value (if it works, does it matter?)
- novelty (is this just the obvious thing?)
- pragmatism (could the user actually pursue it?)
- leverage (does it open more than it costs?)
- overlap (is it a near-duplicate of another survivor?)

Keep **5-7 survivors**.

## Phase 4 — Write the ranked shortlist

Save to `documents/ideation-[short-slug].md`. Show the user only a concise ranked summary in chat,
plus the file path — not a wall of text.

```markdown
# Ideation — [focus area or "surprise me"]

**Date:** [date]
**Status:** Shortlist — pick one and take it into `/brainstorm`

## Ranked directions

### 1. [Direction name]
- **The bet (plain language):** [what it is, in one or two sentences]
- **Basis:** [direct: / external: / reasoned: — the actual evidence]
- **Why it could matter:** [the upside if it works]
- **Downsides / what worries me:** [the honest risk]
- **Confidence:** [low / medium / high]  ·  **Effort to test:** [low / medium / high]

### 2. … (repeat for each survivor)

## What got cut (and why)
[One line per rejected idea — keeps the thinking honest and visible (Rule 12).]
```

## Phase 5 — Hand off

Show the ranked list, then end with:
> "Shortlist saved to `documents/ideation-[slug].md`. Pick the one that pulls at you.
> **Next:** run `/brainstorm \"<the direction you picked>\"` to pressure-test it and shape it into a
> product brief."

If the user wants to push further instead of picking, offer to iterate (another round with a tighter
focus or a new angle) — but don't loop forever; converging on a direction is the goal.

## Rules

- Per Rule 12, always show what got cut and why — silent rejection hides the thinking.
- Per Rule 5, generation and judgment are the right use of the model here; deterministic facts
  (does X already exist?) go to subagents, not guesses.
- Per Rule 2, don't over-engineer the run for a tiny ask — a "give me 3 quick directions" request
  doesn't need 13 agents. Scale the fan-out to the question.
- Stay non-technical in everything the user sees. No stack, no architecture — that's `/cto`, much
  later.
