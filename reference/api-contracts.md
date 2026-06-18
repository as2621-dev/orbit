# Data Contracts — Orbit

**Why this doc exists:** Orbit has no frontend/backend HTTP boundary, so "API contracts" here means the two durable data shapes every stage must agree on: the **config schema** (`orbit.config.json`, brief §6) and the **SQLite state tables** (brief §5). Stage modules read/write these; if they drift, the pipeline breaks silently. This is the source of truth.

**When to update:** when a config field or DB table/column is added, renamed, or removed. A schema change here implies a `store.py` migration (keep the WAL + lightweight-migration pattern lifted from last30days).

## Config schema — `orbit.config.json`
Per-user from day one (brief §1). Non-secret config lives here; secrets (explicit cookies, WhatsApp/Twilio creds) live in `.env`.

```jsonc
{
  // which browser to read cookies from, OR "env" to use AUTH_TOKEN/CT0 from .env
  "cookie_source": "chrome",            // chrome | firefox | safari | edge | brave | env

  // creator/channel priority — the thumb on the ranking scale (brief §3 Stage 6)
  "creator_weights": {
    "UC_youtube_channel_id": 1.5,       // map of channel_id OR x handle -> priority_weight (float)
    "some_x_handle": 2.0
  },

  // topic profile driving Axis B (on/off-topic). Seeded from subs on first run, user-editable.
  "interests": ["ai agents", "semiconductors", "formula 1"],

  // the main cost/time lever (brief §7): how many items get transcribed / deep-pulled
  "depth": "default",                   // quick | default | deep

  // where output goes
  "delivery": {
    "html_path": "~/orbit/out/today.html",
    "imessage_to": "+15551234567",      // optional; AppleScript delivery (macOS)
    "whatsapp_to": null                 // optional; Twilio/Business API (M4 stretch)
  },

  // cron expression — used by the README setup step / --setup wizard
  "schedule": "0 7 * * *"
}
```

Field notes:
- `creator_weights` keys are `channel_id` (YouTube) or `creator_handle` (X); values are floats applied in the derank score.
- `interests` drives Axis B classification; first run auto-seeds from subscriptions, user edits persist.
- `depth` is the single throttle: `quick` skips most transcription, `deep` transcribes everything.
- Secrets are NOT in this file. `cookie_source: "env"` defers to `.env` (`AUTH_TOKEN`, `CT0`).

## SQLite state — tables (brief §5)
Lifted `store.py` shape: WAL mode, lightweight in-code migrations, DB at `~/.local/share/orbit/orbit.db`. Verbose, intention-revealing column names. (Types/columns below are the contract; finalize exact DDL in the M1 store phase — see master-plan open question 3.)

### `sources` — channels/handles followed
| column | type | meaning |
|---|---|---|
| `source_id` | INTEGER PK | internal id |
| `platform` | TEXT | `youtube` \| `x` |
| `external_id` | TEXT | `channel_id` (YT) or `creator_handle` (X) |
| `display_name` | TEXT | human-readable creator name |
| `category` | TEXT | classification prior: `signal` \| `noise` (channel-level default for Axis A) |
| `priority_weight` | REAL | user-set ranking weight (mirrors `creator_weights`) |
| `last_refreshed_at` | TEXT | when the source list entry was last refreshed (weekly Stage 0) |

### `seen` — delta engine (per-source last-seen IDs)
| column | type | meaning |
|---|---|---|
| `seen_id` | INTEGER PK | |
| `source_id` | INTEGER FK→sources | |
| `item_external_id` | TEXT | `video_id` (YT) or `tweet_id` (X) |
| `first_seen_at` | TEXT | when Orbit first saw this item |
> Stage 1 reads the max/known `item_external_id`s per source to fetch only what's new.

### `classifications` — item-level overrides
| column | type | meaning |
|---|---|---|
| `classification_id` | INTEGER PK | |
| `item_external_id` | TEXT | the classified item |
| `axis_a_signal` | INTEGER | 1=signal, 0=noise (item-level, overrides channel prior) |
| `axis_b_on_topic` | INTEGER | 1=on-topic, 0=off-topic |
| `is_user_override` | INTEGER | 1 if the user corrected it (persists across runs) |
| `classified_at` | TEXT | |

### `carryforward` — top-tier items the user hasn't opened, resurfaced once
| column | type | meaning |
|---|---|---|
| `carryforward_id` | INTEGER PK | |
| `item_external_id` | TEXT | |
| `density_tier` | TEXT | tier it held: `hero` \| `standard` \| `compact` \| `index` |
| `surfaced_count` | INTEGER | how many times resurfaced (cap at 1 resurface) |
| `created_at` | TEXT | |

### `interests` — the user's topic profile (drives Axis B)
| column | type | meaning |
|---|---|---|
| `interest_id` | INTEGER PK | |
| `keyword` | TEXT UNIQUE | topic keyword |
| `is_seeded` | INTEGER | 1 if auto-seeded from subs, 0 if user-added |
| `created_at` | TEXT | |
> Mirrors `interests` in config; the config array is the editable surface, this table is the persisted profile.

## Derank score contract (Stage 6)
Not stored, but the agreed weighted formula every render depends on. Score per item =
`f(creator priority_weight, cluster size / source diversity, uniqueness boost, engagement relative to creator's own baseline, recency, trending/scoop multiplier)`.
Sort descending → assign `density_tier`: **Hero → Standard → Compact → Index**. **Rank controls density, never inclusion** — nothing is dropped by score.
