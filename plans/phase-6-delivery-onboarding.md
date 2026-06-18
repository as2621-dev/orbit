# Phase 6: Delivery, config/setup wizard, cron generation, and the permissions README

**Milestone:** M4 — Delivery & onboarding
**Status:** Not started
**Estimated effort:** L

## Goal
Orbit becomes installable and self-running for a new user: an `orbit.config.json` schema with a `/orbit --setup` wizard (reads subs/follows, auto-classifies, confirms categories, picks priority creators, sets delivery + schedule, prints a cron entry), iMessage delivery via AppleScript, and the honest permissions/onboarding README (brief §8) as the primary deliverable — with optional WhatsApp and Briefcast payload as clearly-marked stretch.

## Resolved open questions folded into this phase
- No new master-plan open questions land here. The §8 README is the **primary** M4 deliverable (Sub-phase 4); WhatsApp + Briefcast are **optional stretch** (folded into Sub-phase 3, gated and skippable). iMessage is the core delivery path. Cron (OS cron → `claude -p "/orbit"`) is the default scheduler — cloud schedulers are out per master-plan (cookie constraint). Per the build directive, M4 is one phase with exactly 4 sub-phases; WhatsApp/Briefcast ride inside Sub-phase 3 rather than spawning a 5th phase.

## Sub-phases

### Sub-phase 1: Config schema + validation
- **Files touched:** `skills/orbit/scripts/lib/config.py`, `orbit.config.example.json`, `.env.example`
- **What ships:** The full `orbit.config.json` schema per `reference/api-contracts.md` finalized and validated in `config.py` (extends Phase 1's loader): `cookie_source`, `creator_weights` (map channel_id/handle → float), `interests` (string list), `depth` (quick|default|deep), `delivery` (`{html_path, imessage_to?, whatsapp_to?}`), `schedule` (cron expression). Validation rejects malformed values with actionable errors (bad cron, bad depth, bad cookie_source). `orbit.config.example.json` is a documented template; `.env.example` carries placeholders for `AUTH_TOKEN`/`CT0` (cookie_source=env) and optional Twilio/WhatsApp creds — NO real secrets (security rule).
- **Definition of done:** A test asserts a valid config loads into the typed `OrbitConfig`; a test asserts each invalid field (bad `depth`, malformed cron, unknown `cookie_source`) raises a clear, field-named error (fail-loud, Rule 12). A test asserts `orbit.config.example.json` parses and validates as a complete example. A test asserts `.env.example` contains only placeholders (no value that looks like a real token).
- **Dependencies:** none (extends Phase 1's `config.py`)

### Sub-phase 2: `/orbit --setup` wizard + cron-entry generation
- **Files touched:** `skills/orbit/scripts/lib/setup_wizard.py`, `skills/orbit/scripts/orbit.py`, `skills/orbit/SKILL.md`
- **What ships:** `run_setup_wizard()` (wired to `orbit.py --setup`, lifting the reference `setup_wizard.py` shape): reads the user's YouTube subs + X follows (M1/M2 loaders), auto-classifies channels from recent titles into signal/noise (the M1 classify path), presents categories for confirmation, lets the user pick priority creators (writes `creator_weights`), seeds `interests` from subscriptions, sets the delivery target and schedule, writes `orbit.config.json`, and PRINTS the exact OS cron entry (`<cron_expr> cd <repo> && claude -p "/orbit"`) for the user to add (brief §8.3 step 5). `generate_cron_entry(schedule, command) -> str` is a pure function. SKILL.md documents `/orbit` and `/orbit --setup`.
- **Definition of done:** A test drives `run_setup_wizard` with mocked subs/follows loaders and scripted user input and asserts it writes a valid `orbit.config.json` containing the chosen `creator_weights`, seeded `interests`, and `schedule`. A test asserts `generate_cron_entry("0 7 * * *", ...)` returns a syntactically valid crontab line containing `claude -p "/orbit"` (the brief's default scheduler). A test asserts the wizard auto-classifies via the existing classify path (no separate classifier). Loaders/LLM mocked — no live calls.
- **Dependencies:** Sub-phase 1

### Sub-phase 3: iMessage delivery (core) + optional WhatsApp/Briefcast (stretch)
- **Files touched:** `skills/orbit/scripts/lib/deliver.py`, `skills/orbit/scripts/orbit.py`
- **What ships:** `deliver_imessage(summary, html_path, imessage_to)` sending a short message (one-line TL;DR + scoops + a link to the local HTML page) via AppleScript (`osascript`), triggered ONLY if `delivery.imessage_to` is set — skipped silently-but-logged otherwise (brief integration §4). `orbit.py`'s final stage calls delivery after render. Optional, clearly-gated stretch in the same module: `deliver_whatsapp(...)` (Twilio/Business API, requires `.env` cred, only if `whatsapp_to` set) and `emit_briefcast_payload(...)` (writes the TL;DR + episode list as a Briefcast file). All credential use stays in `.env`; no cookie/credential is logged.
- **Definition of done:** A test mocks `osascript`/subprocess and asserts `deliver_imessage` issues an AppleScript send containing the TL;DR and the HTML link when `imessage_to` is set, and is a no-op (logged `imessage_skipped`) when unset (opt-in intent — never message without a configured target). A test asserts `deliver_whatsapp` is skipped when `whatsapp_to` is null AND raises a clear error if set without the `.env` cred (fail-loud). A test asserts `emit_briefcast_payload` writes a file with the episode list. No real iMessage/Twilio call; subprocess + HTTP boundaries mocked.
- **Dependencies:** Sub-phase 1 (delivery config)

### Sub-phase 4: The permissions/onboarding README + plugin packaging ⚠ irreversible
- **Files touched:** `skills/orbit/README.md`, `.claude-plugin/marketplace.json`, `README.md` (repo root)
- **What ships:** The brief §8 README in full and honest tone — §8.1 what Orbit does/expects, §8.2 prerequisites (Python 3.12+, Node 22+, yt-dlp, browser login), §8.3 the 5-step setup, §8.4 the permissions table (cookies/filesystem/network/AppleScript/LLM with the "why" and "what we do/don't do"), §8.5 the un-softened risk disclosure (`auth_token` = full account access; X cookie reads are ToS-gray; revocation = log out; everything local), §8.6 troubleshooting (X 404 / stale queryId, no cookies found / DB lock, expired cookies, rate-limited). A rough daily-cost-by-`depth` estimate (brief §7). `marketplace.json` finalized for plugin distribution; repo-root `README.md` updated to point at the skill README and reflect the shipped feature set + env setup (global README-update discipline).
- **Definition of done:** A structural test/check asserts `skills/orbit/README.md` contains all of §8.1-§8.6 headings, the permissions table with a `why`/`what we do/don't do` row for each of the five permissions, and the §8.5 risk lines (`auth_token`, ToS-gray, revocation, local-only) verbatim-in-spirit (grep for the required clauses). A test asserts `marketplace.json` parses and declares the `orbit` plugin/skill. A check asserts the cost estimate and the default-`depth` recommendation are present. (Manual smoke: the README reads honestly end-to-end — flagged as the one human-review item.)
- **Dependencies:** Sub-phases 1-3 (the README documents the config, wizard, delivery, and permissions they implement)

## Phase-level definition of done
`pytest tests/` passes. A new user can: install the plugin (`marketplace.json`), run `/orbit --setup` to generate a validated `orbit.config.json` and get a copy-pasteable cron entry, receive the digest TL;DR via iMessage when configured (skipped cleanly otherwise), and read a complete, honest §8 README covering setup, the five permissions with their why, the un-softened risk disclosure, troubleshooting, and a cost estimate. WhatsApp + Briefcast exist as clearly-marked, gated optionals. Orbit is end-to-end installable and self-running on the user's machine.

## Out of scope
- No cloud scheduling (Trigger.dev/Claude Routines) — cron-on-device only (cookie constraint).
- No hosted service, web app, or multi-tenant backend (per-user-local only).
- WhatsApp and Briefcast are optional/stretch — built minimally and gated, not polished.
- No automated test that actually sends an iMessage/WhatsApp or hits Twilio — boundaries mocked.

## Open questions
- The daily-cost estimate numbers (per `depth`) are first-cut from token-per-stage assumptions; refine after the maintainer's real runs. Not blocking.
- macOS Automation-permission UX (the OS prompt the user must approve for AppleScript) is documented in §8.4/§8.6 but cannot be unit-tested — covered by the README and the manual-smoke flag. Not blocking.

## Self-critique

**Product lens:** PASS. Delivers brief §6 (config), §8.3 (setup wizard + cron), integration §4 (iMessage), and §8 (the README — explicitly the *primary* deliverable) and treats WhatsApp/Briefcast as the brief's marked optionals — no scope creep, no under-delivery on the §8 centerpiece. By phase end Orbit is installable and self-running, completing the MVP arc from the brief (a busy person can set it up once and get a daily digest). The riskiest-assumption tuning loop (classification overrides, priority creators) is operationalized by the setup wizard.
**Engineering lens:** PASS. Within stack (Python config/wizard/deliver, AppleScript via subprocess, OS cron string generation — no new framework; Twilio only behind the gated optional and only via `.env`). DoDs are fresh-context checkable (config rejects bad fields; cron line contains `claude -p "/orbit"`; AppleScript send contains TL;DR; README contains the §8 headings/rows). Rule 5 honored: the wizard's only LLM use is the existing auto-classify path; cron-string building, validation, and delivery routing are deterministic. Sub-phase 4 (the README) correctly comes last — it documents what 1-3 built, locking nothing prematurely.
**Risk lens:** Findings + fixes. (1) **File-boundary conflict:** Sub-phases 2 and 3 both edit `orbit.py` (wizard wiring vs final delivery stage) — resolved by sequential dependency on Sub-phase 1 and distinct functions/regions; flagged so `/run-phase` orders them. (2) **⚠ irreversible:** Sub-phase 4 finalizes `marketplace.json` for *public plugin distribution* — publishing-shaped; marked so `/run-phase` treats packaging with care (the plugin is the outward-facing artifact). The README itself is reversible text. (3) **Security:** `.env.example` placeholder-only test + no-credential-in-logs posture carried from earlier phases; the §8.5 honest-risk disclosure is a product requirement, not boilerplate, and is grep-checked. (4) Test coverage per Rule 9: opt-in iMessage (no-target = no-op), fail-loud WhatsApp-without-cred, valid cron line, and config rejection each fail on wrong logic. (5) Painting-into-a-corner: 1(config)→2(wizard writes config)→3(delivery reads config)→4(README documents all); 4 needs 1-3 done — stated. The one non-automatable item (README reads honestly; macOS permission UX) is explicitly flagged as manual-smoke per the Risk-lens "manual smoke is fine for UI but flag it" rule.
**Irreversible sub-phases:** Sub-phase 4 (`⚠ irreversible` — finalizes `marketplace.json` for public plugin distribution; the outward-facing packaging artifact).
