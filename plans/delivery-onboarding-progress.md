# Progress: phase-6-delivery-onboarding

**Phase file:** plans/phase-6-delivery-onboarding.md
**Started:** 2026-06-18
**Base HEAD:** 1e8d29f (Phase 5 + saved Stage-5 wiring draft handoff)
**Baseline tests:** 115 passing
**Execution mode:** SEQUENTIAL (1→2→3→4). Rationale: strict dependency chain (config → wizard writes config → delivery reads config → README documents all); Sub-phases 2 and 3 BOTH edit orbit.py (wizard wiring vs final delivery stage); Sub-phase 4 is ⚠ irreversible (finalizes marketplace.json for public plugin distribution). No worktree parallelism.

## STATUS: COMPLETE — committed atomically at Step 4 (7a1847a). Tree clean. 163 tests passing.

### Phase-level passes (Step 3) — ALL PASS
- **3a DoD: PASS** — 163 tests green. Full Stage 0→7 runs end-to-end: bare CLI run (ORBIT_STAGE0_SKIP_NETWORK=1, no LLM/network) exits 0 through cluster→trending→scoop→rank→render→delivery (imessage_skipped no-op); the wiring test exercises the M3 path on fixtures producing HTML with all three sections (overlap-block/trending-rail/scoops-strip). --setup wizard writes a load_config-valid orbit.config.json + prints a cron line containing `claude -p "/orbit"`. README §8.1-§8.6 complete + cost + scheduling.
- **3b Slop scan: PASS** — no TODO/FIXME, no bare excepts, no hardcoded hosts, no marketing voice, ruff clean. The 2 print()s in setup_wizard.py are the REQUIRED user-facing cron output (brief §8.3 step 5), not slop.
- **3c CSO-lite: PASS** — no real-secret-shaped values anywhere; .env.example placeholders empty + commented Twilio block; example.json carries no secret (sample phone only). iMessage = local osascript, list-argv (shell=False) + escaped text → no injection; Twilio creds env-only, never logged (presence-booleans). No new dependencies (stdlib-only). No PII/secret logged. No medium/low → no cso-findings file.

### Manual-review items flagged (not auto-verifiable)
- README cover-to-cover honesty read (the brief's designated human-review item).
- macOS Automation-permission UX smoke (the OS prompt for AppleScript) — documented in §8.4/§8.6, needs a real-Mac smoke.

## Orbit.py M3 wiring (folded into Sub-phase 1 per the handoff)
Sub-phase 1 ALSO adopts the validated draft `.agents/handoff/phase6-orbit-stage5-wiring.draft.patch` (signatures verified against the committed libs by the orchestrator), wiring Stage 5 (run_stage5_overlap_trending_scoops) between classify and rank, threading trending_multipliers into Stage 6, and the cluster/trending/scoop args into Stage 7 — plus the wiring test. orbit.py and tests/test_scoops_and_render.py are added to Sub-phase 1's Files-touched for this.

## Sub-phase progress
- [x] 1: Config schema + validation (+ adopt the orbit.py M3 Stage-5 wiring draft + test) — COMPLETED (DoD PASS). config.py +cron/delivery/creator_weights validation +is_valid_cron_expression helper; orbit.config.example.json; test_config.py (17 tests); orbit.py M3 Stage-5 wiring adopted from draft + wiring test in test_scoops_and_render.py. fake_store verified against real compute_history_sample_counts (list_sources()/get_seen_ids(source_id)); scoop fires genuinely (no fudging). Verified: 133 passed.
- [x] 2: /orbit --setup wizard + cron-entry generation — COMPLETED (DoD PASS). lib/setup_wizard.py (483 lines): generate_cron_entry (pure, default cmd `cd {repo} && claude -p "/orbit"`, validates via is_valid_cron_expression) + run_setup_wizard (injectable subs/follows loaders, llm_classifier, input_fn, store_module, output path; auto-classifies via existing classify_item path; writes valid orbit.config.json; prints cron). orbit.py run_setup() delegates to it. SKILL.md updated. tests/test_setup_wizard.py (6 tests). Justified divergence: injectable store_module (classify_item touches SQLite). Verified: 139 passed.
- [x] 3: iMessage delivery (core) + optional WhatsApp/Briefcast (stretch) — COMPLETED (DoD PASS). lib/deliver.py (451 lines): deliver_imessage (opt-in no-op when imessage_to unset; AppleScript via injectable osascript runner; backslash-then-quote escaping = injection-safe), deliver_whatsapp (skip when null, fail-loud without env cred, injectable http_post), emit_briefcast_payload (writes episode list). orbit.py: _build_delivery_summary + run_stage7_deliver wired post-render (bare path = clean no-op). .env.example: commented Twilio block (placeholders only). tests/test_deliver.py (12 tests). SECURITY VERIFIED by orchestrator: no hardcoded secrets, logs carry presence-booleans only (no cred values), creds from os.environ only, osascript local-only. Verified: 151 passed.
- [x] 4: permissions/onboarding README + plugin packaging (⚠ irreversible) — COMPLETED (DoD PASS). skills/orbit/README.md (full §8.1-§8.6 + cost-by-depth §7 + scheduling §2; honest tone, §8.5 un-softened, no marketing voice — orchestrator-verified). marketplace.json finalized (tightened description, added version 0.1.0, no invented schema fields; parses, declares orbit plugin+skill). Root README.md (points at §8 doc, shipped M1-M4 feature set, quick-start, env setup, local-only security note). tests/test_readme_packaging.py (12 structural tests). Manual-review items FLAGGED: README cover-to-cover honesty read; macOS Automation-permission UX smoke. Divergence: Python 3.12+ (codebase floor) vs brief 3.11+. Verified: 163 passed.
