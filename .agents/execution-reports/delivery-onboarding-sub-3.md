# Phase 6 — Sub-phase 3: iMessage delivery (core) + WhatsApp/Briefcast (stretch)

## Status: SUCCESS

## Implemented
- **`lib/deliver.py` (NEW, 451 lines):**
  - `deliver_imessage(summary, html_path, imessage_to, *, runner=...)` — CORE. Opt-in:
    no-op + `imessage_skipped` log + returns `False` when `imessage_to` is falsy.
    When set, builds an injection-safe AppleScript (TL;DR + `file://` link to the local
    HTML) and runs it via `osascript -e` through an INJECTABLE `runner` (default thin
    `subprocess.run` wrapper). Non-zero exit / `OSError` → `False` with a
    `fix_suggestion` (grant Automation permission). No credential leaves the machine.
  - `deliver_whatsapp(summary, html_path, whatsapp_to, *, env=os.environ, http_post=None)`
    — OPTIONAL/STRETCH. No-op + `whatsapp_skipped` when target None. Target set but Twilio
    cred absent in `env` → raises `RuntimeError` (fail loud, Rule 12) with a `.env`
    fix_suggestion; never logs the cred value (presence-only booleans). Target + cred +
    injected `http_post` → POSTs (creds read from `env` ONLY). Also raises if `http_post`
    not injected (never makes an accidental live HTTP call).
  - `emit_briefcast_payload(summary, episodes, out_path)` — OPTIONAL/STRETCH. Writes a
    JSON payload (summary + episode list) to a file; tolerantly unwraps TieredItem →
    RankableItem. No auth surface. Returns the written Path.
  - Helpers: `_escape_for_applescript` (backslash-first then quote, newlines collapsed),
    `_build_html_link` (`file://` URI), `_build_imessage_applescript`, `build_message_body`.
- **`orbit.py`:** added `deliver` import; `_build_delivery_summary(tiered_items, scoops)`
  (PURE, deterministic TL;DR — leads with top scoop, else top item, else quiet line —
  no LLM, Rule 5); `run_stage7_deliver(...)` (wiring only) called in `run_pipeline` right
  after `run_stage7_render`, using `written_paths[0]` (page 1). iMessage always attempted
  (no-op when unset); Briefcast gated on `delivery.briefcast_path`; WhatsApp gated on its
  config key (HTTP boundary intentionally not wired here so a misconfig fails loud).
- **`.env.example`:** added a COMMENTED Twilio block (`TWILIO_ACCOUNT_SID`,
  `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`) — placeholders only, no values.
- **`tests/test_deliver.py` (NEW):** 12 tests, all boundaries mocked.

## Divergences from spec
- WhatsApp `deliver_whatsapp` additionally raises if `http_post` is not injected (beyond
  the spec's "cred-absent raises"). Rationale: prevents an accidental live HTTP call —
  strictly safer, aligned with the "no real HTTP" mandate.
- The orbit.py seam leaves WhatsApp **unwired** (gated on config key but no HTTP client
  passed) — per the "stretch, gated, skipped by default" directive; the host injects the
  boundary at runtime. Briefcast IS wired (a file, no auth surface).
- Gating WhatsApp/Briefcast uses `config.delivery.get("whatsapp_to")` /
  `.get("briefcast_path")`. `briefcast_path` is a new optional delivery key, read-only
  (config.py validates `imessage_to`/`whatsapp_to`/`html_path`; an unknown key is ignored,
  so no config.py change was needed — and config.py was out of scope).

## Self-review findings + fixes
- **Doctest correctness (low):** the original `_escape_for_applescript` doctest expected
  output didn't match the real repr. Fixed to an `== ...` / `True` form; `python3 -m
  doctest` now passes.
- **Secret-leak audit (critical — clean):** grepped deliver.py for credential values;
  the Twilio token is only read via `env.get(...)` and passed into the injected
  `http_post` auth tuple — never logged. Logs carry presence booleans only
  (`has_auth_token=bool(...)`). No secret hardcoded.
- **Injection (high — clean):** escaping is backslash-first then quote, newlines
  collapsed; covered by two tests (a quoted-summary send + a unit order test).
- No critical/high issues remained; no other changes needed.

## Validation
- `uv run --with pytest pytest tests/ -q` → **151 passed** (139 baseline + 12 new). PASS.
- `ast.parse` clean on `deliver.py` and `orbit.py`. PASS.
- `ruff check` clean on both files. `python3 -m doctest deliver.py` passes.
- deliver.py 451 lines (<500); orbit.py 767 lines (<1000).

## Definition of done: PASS
- [x] osascript/subprocess mocked; asserts AppleScript send contains TL;DR + HTML link;
  runner called with `osascript`.
- [x] no-op (returns False, runner NOT called) when `imessage_to` None/empty.
- [x] AppleScript escaping test: a double-quote in the TL;DR is escaped (no break/inject).
- [x] `deliver_whatsapp` skipped when target None; raises clear error when set without env
  cred (fail loud); env injected.
- [x] `emit_briefcast_payload` writes a file containing the episode list.
- [x] orbit.py seam calls `deliver_imessage` as a no-op on the bare path (extra test).

## Concerns / confirmations
- **No secret hardcoded/logged/transmitted — CONFIRMED.** Twilio creds read from
  `os.environ` (`.env`) only, never logged (presence booleans only), never hardcoded.
  `.env.example` carries commented placeholders with empty values; the existing
  `test_env_example_contains_only_placeholders` only inspects active assignments and
  still passes.
- **iMessage local-only — CONFIRMED.** Uses `osascript` (the user's own Messages session);
  no network credential leaves the machine (integrations §4). The subprocess is injectable
  and mocked in tests — no real iMessage sent.
- **Twilio from env only — CONFIRMED.** Stretch path reads creds from the injected `env`
  (defaults to `os.environ`); the HTTP boundary is injectable and mocked — no live HTTP.
- Rule 5 honored: TL;DR + delivery routing are deterministic code, no LLM.
- macOS Automation-permission UX cannot be unit-tested (OS prompt) — surfaced via the
  osascript-failure `fix_suggestion`; documented further in Sub-phase 4's README §8.4/§8.6.

## NOT committed (per instructions).
Files touched (absolute):
- /Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/deliver.py (NEW)
- /Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/orbit.py
- /Users/asheshsrivastava/frommyfeed/.env.example
- /Users/asheshsrivastava/frommyfeed/tests/test_deliver.py (NEW)
- /Users/asheshsrivastava/frommyfeed/.agents/execution-reports/delivery-onboarding-sub-3.md (this report)
