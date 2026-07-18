---
title: "Spike (#5): headless `claude -p` CANNOT publish a Claude artifact — NO-GO for #7 as designed"
tags: [spike, artifact, claude-cli, headless, claude-p, launchd, chat-bridge, M7, new-q-prefill, url-length, go-nogo]
problem_type: pattern
symptoms: "headless `claude -p` asked to publish a private claude.ai artifact prints PUBLISH_FAILED / 'no artifact tool'; the Artifact tool is absent from the `claude -p` init tool list; ToolSearch does not surface it as a deferred tool either"
root_cause: "the Artifact publish tool is a harness/interactive-only capability (claude.ai Code / FleetView sessions expose it); it is NOT registered in a plain non-interactive `claude -p` session, which is exactly how launchd runs the pipeline"
date: 2026-07-18
---

# Verdict: NO-GO for issue #7 as designed

A launchd-style headless `claude -p` session **cannot publish a claude.ai artifact**. The
`claude.ai/new?q=` prefill link that #7 depends on has nothing to point at. The integrations
§5 mechanism ("the headless `claude -p "/orbit"` session publishes `digest.md` as a private
artifact") is **not buildable as written**. Per Rule 12: this is a plain NO-GO, not a
maybe. #7 should be closed or redesigned (owner's call — do not close it automatically).

This finding gates PRD stories #16, #18, #19. Story #17 (voice) needs no Orbit work — see
the note at the bottom. M5 (email) and M6 are untouched by this result; the PRD isolated M7
behind this spike precisely so a NO-GO costs nothing downstream.

---

## AC1 + AC2 — Headless publish attempt: the mechanism / the failure (verbatim)

Environment: `claude` = `/Users/asheshsrivastava/.local/bin/claude`, version `2.1.214
(Claude Code)`. Invocation mirrors the pipeline's headless pattern
(`scripts/lib/llm.py:219`): `claude -p --dangerously-skip-permissions ...` with
`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` stripped from the env so the Claude Code
subscription auth path is used (same as the cron line
`claude -p --dangerously-skip-permissions "/orbit"`). Prompt is a positional arg — no stdin
redirect. Ran from a plain shell (not from inside an interactive session's turn).

**Attempt 1 — direct publish request:**

```
env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
  claude -p --dangerously-skip-permissions \
  "Publish the markdown file at <scratch>/spike-test-digest.md as a PRIVATE claude.ai
   artifact ... print ARTIFACT_URL=<url> ... else print PUBLISH_FAILED=<reason>."
```

Exit code `0`. STDOUT verbatim:

```
PUBLISH_FAILED=No artifact-publishing tool is available in this headless session (the
claude.ai Artifact tool is not exposed here, and no connected MCP server offers artifact
creation)
```

STDERR: empty.

**Decisive evidence (don't trust the model's self-report) — the init tool list:**

```
claude -p --dangerously-skip-permissions --verbose --output-format stream-json "Say OK."
```

The `system/init` event advertised these tools (model: `claude-fable-5`):

```
Task, Bash, CronCreate, CronDelete, CronList, DesignSync, Edit, EnterWorktree, ExitWorktree,
Monitor, NotebookEdit, PushNotification, Read, RemoteTrigger, ReportFindings, ScheduleWakeup,
SendMessage, Skill, TaskCreate, TaskGet, TaskList, TaskOutput, TaskStop, TaskUpdate,
ToolSearch, WebFetch, WebSearch, Workflow, Write
```

`mcp_servers`: `[{"name":"trigger","status":"pending"}]`. **No `Artifact` tool.** The
interactive session that ran this spike DOES have an `Artifact` tool in its roster — so
artifact publish is a harness/interactive capability that plain `claude -p` does not receive.

**Variant tried (the obvious flag/config lever) — is `Artifact` a *deferred* tool?**
The headless session has `ToolSearch`, which can load deferred tool schemas. Probed it:

```
claude -p --dangerously-skip-permissions "Use ToolSearch to find any tool that can publish a
claude.ai artifact ('artifact publish', 'select:Artifact') ... print DEFERRED_ARTIFACT=YES/NO"
→ DEFERRED_ARTIFACT=NO
```

So it is not hidden behind ToolSearch either. `--dangerously-skip-permissions` (the only
permission lever the pipeline uses) is irrelevant — the failure is a **missing tool**, not a
permission denial or an auth prompt. No flag available to the pipeline changes the answer.

**Failure mode, named precisely:** missing tool. Not an auth prompt, not a permission
denial, not a silent no-op — the tool simply is not registered in a non-interactive
`claude -p` session.

## AC3 — Privacy verification: moot (nothing was published)

No artifact URL was ever produced, so there is nothing to fetch-unauthenticated-and-check.
The planned verification (`curl -s -o /dev/null -w "%{http_code}"` with no cookies; 200+
content = not private, login-wall/403/404 = private) could not run. Recorded as **not
applicable** rather than assumed either way — publish never got far enough to test privacy.

## AC4 — `claude.ai/new?q=` bridge limits (tested empirically; relevant only if #7 is redesigned)

Independent of the publish failure, so documented for a possible redesign.

**Length ceiling (empirical).** Unauthenticated `curl` to `https://claude.ai/new?q=<N x 'x'>`
(the `?q=` is actually consumed client-side by the SPA after login, so 403 here is just
Cloudflare's unauthenticated block — but the length behavior is real at the edge):

| q length (chars) | HTTP |
|---|---|
| 500 – 65,000 | 403 (request reaches the edge) |
| 70,000 – 100,000 | 000 (no response — request line exceeds the server/proxy limit, dropped) |

Hard edge ceiling ≈ **65 KB for the whole URL**; beyond ~65–70 K the request is dropped
outright. Browser-side limits are lower and are the real binding constraint: Chrome address
bar ~2,048 chars (more works programmatically), Firefox ~64 KB display; the safe
cross-browser/CDN ceiling is **~2,048 chars**. **This does not bite #7's real design** — the
prefill is a short instruction + a ~60-char artifact URL (well under 2 KB). Length only
becomes a problem if someone inlines the whole digest into `?q=`, which the design explicitly
forbids (fetch-on-open, not inline).

**Encoding trap (the "works short, silently truncates" failure — this one is real and would
bite).** An artifact URL pasted **raw** into `?q=` truncates the prompt at the first
unescaped `&`, and everything after it (`&foo=bar#section`) leaks out as separate query
params / a fragment and is silently lost:

```
raw q  = 'Summarize this digest: https://claude.ai/.../abc-123?theme=dark'   ← truncated!
         (the trailing '&foo=bar#section' vanished from q)
encoded q (urllib.parse.quote(prompt, safe='')) = full prompt intact
```

Mitigation if #7 is ever redesigned: **percent-encode the entire prompt string** (Python
`urllib.parse.quote(prompt, safe='')`), never string-concatenate a raw URL into `?q=`. Any
artifact/deep-link URL carrying `&`, `#`, or `?` in `?q=` MUST be encoded or the tail is
dropped with no error.

## AC (story #17) — Voice mode is not Orbit work

PRD story #17 (talk through the digest hands-free) is a **claude.ai client capability**, not
an Orbit feature. Nothing in this backlog implements it, and nothing should: it is satisfied
for free the moment a working conversation link exists. Since that link is currently
NOT buildable headless (this spike), #17 is blocked-by-#7 transitively, but it never needs
its own slice. Stop treating it as unsliced work.

---

## What single change would flip NO-GO → GO

The blocker is exclusively "the `Artifact` tool is absent from `claude -p`." It flips only if
the digest is published from a session context that DOES expose an artifact-publish tool.
Concretely, one of:

1. **Run the daily digest from a harness that exposes `Artifact`** instead of plain
   `claude -p` — e.g. a Claude Code Agent SDK / managed-agent runner configured with the
   artifact tool, launched by launchd, rather than the bare CLI. Verify empirically that such
   a runner's init tool list contains `Artifact` before committing to it — do not assume.
2. **A supported artifact-publish path that isn't the interactive tool** — e.g. an MCP server
   that offers artifact creation and can be attached to the `claude -p` session (the init
   showed only the `trigger` MCP server today), or a documented claude.ai artifacts HTTP API.
   None exists today; this is a "watch for it" flip, not an available one.

Absent one of those, publish stays impossible headless. **Recommendation: close or redesign
#7; do not attempt it against the current mechanism.** If redesigned around option 1, carry
the AC4 encoding rule forward (percent-encode the whole `?q=` prompt) so the bridge doesn't
silently truncate. Related: [[launchd-scheduler-install-gotchas]] (the headless runner that
would host any such publish step).
