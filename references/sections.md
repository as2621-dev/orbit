<!--
Orbit section-summary prompt template (Final-design three-section split).

TWO LLM jobs live here, each in a labeled section (delimited by the
`<!-- PROMPT:name -->` / `<!-- /PROMPT:name -->` markers below):

  - `build_sections` — exactly THREE timestamped sections for ONE video, where the
    model picks the split points from a full transcript. One call per video.
  - `build_sections_batch` — summaries for MANY videos in ONE call, where CODE has
    already chosen each video's three anchors. This is what the pipeline runs
    (Rule 6: ~35 videos in a single call instead of ~35 calls). The model writes
    prose only; it never returns a timestamp, so nothing needs snapping.

`lib/sections.py` loads THIS file at runtime, slices the section it needs, and
`.format(...)`-substitutes the placeholders (so the maintainer can tune wording
during real-day usage WITHOUT touching code — mirrors references/summarize.md and
references/chapterize.md). Keep the placeholder tokens intact; renaming one breaks
the renderer. Any LITERAL brace must be DOUBLED (the JSON example braces below are
already doubled for this reason).

The model's ONLY job here is the judgment call — WHERE the three sections start and
WHAT each covers (Rule 5). Snapping each returned offset to a real transcript cue,
enforcing the count, truncating, and fail-soft degradation are deterministic code in
sections.py. The model never invents a timestamp that survives: every offset it
returns is snapped to an actual cue before it becomes a deep link.
-->

<!-- PROMPT:build_sections -->
You are Orbit's section editor. Below is the timestamped transcript of ONE video the
reader follows. Split it into EXACTLY THREE sections and summarize each one.

VIDEO TITLE: {video_title}

TRANSCRIPT (each line is `<start_seconds> <text>`):
{transcript_block}

Pick the three starts at REAL topic shifts — the points where the video moves to a
genuinely different subject. Do NOT split into mechanical equal thirds; if the video
spends most of its time on one argument, the sections should reflect that shape. The
three sections must be in ascending time order and must together cover the whole video:
the first starts at or near the opening, the third covers the closing material.

Every `start_seconds` you return MUST be copied from a `<start_seconds>` value that
appears in the transcript above. Never compute, round, or invent an offset.

For each section write ONE summary of AT MOST 200 characters saying what that stretch
of the video ACTUALLY covers — the specific claim, example, or argument a reader would
get by jumping there. Be concrete: name the thing being discussed. Never write generic
filler that would fit any video ("opens by laying out the problem", "the main
walkthrough", "stress-tests the approach"); a summary that could be pasted onto a
different video is a failed summary. Never invent facts, guests, numbers, or claims
that are not supported by the transcript.

No hashtags, no emoji, no surrounding quotes, no restating the video title verbatim.

Return ONLY a strict JSON array of exactly three objects — no prose, no markdown fence,
no trailing commentary:

[{{"start_seconds": <number>, "text": "<summary>"}}]
<!-- /PROMPT:build_sections -->

<!-- PROMPT:build_sections_batch -->
You are Orbit's section editor. Below are several videos the reader follows. For EACH
video you are given three ANCHORS — already-chosen moments in the video, each with the
material that occurs there. Write ONE summary per anchor.

You do NOT choose where the sections start. The anchors are fixed. Your only job is to
say what each anchor's stretch of the video covers.

VIDEOS:
{videos_block}

For each anchor write ONE summary of AT MOST 200 characters describing what that stretch
ACTUALLY covers — the specific claim, example, or argument a reader would get by jumping
there. Be concrete: name the thing being discussed. Ground every summary ONLY in that
anchor's material plus the video title; never invent facts, guests, numbers, or claims
that are not supported by what you were given.

Never write generic filler that would fit any video ("opens by laying out the problem",
"the main walkthrough", "stress-tests the approach"); a summary that could be pasted onto
a different video is a failed summary. When an anchor's material is a bare label like
"Intro" or "Sponsor", do NOT echo it — say what the video's opening or that segment is
actually about, using the title and the surrounding anchors as context.

No hashtags, no emoji, no surrounding quotes, no restating the video title verbatim.

Return ONLY a strict JSON object mapping each video id to its array of exactly three
summary strings, in the same order as that video's anchors — no prose, no markdown fence,
no trailing commentary:

{{"<video_id>": ["<summary1>", "<summary2>", "<summary3>"]}}
<!-- /PROMPT:build_sections_batch -->
