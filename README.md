# RA Deal Radar

Weekly India M&A/PE/VC deal-intelligence digest for [Rungta Advisors](https://www.rungtaadvisors.com). Scrapes free public news and regulatory sources, classifies each item with an LLM, dedupes, and emails a categorized summary every Monday morning.

Runs on [GitHub Actions](.github/workflows/weekly-digest.yml) — Monday 9:00 AM IST, no server to maintain.

## What it does

```
ingest → classify → dedupe → build digest → send
```

1. **Ingest** ([ingest/](ingest/)) — pulls raw articles/filings from 9 sources (below)
2. **Classify** ([process/classify.py](process/classify.py)) — a batched Gemini call extracts acquirer, target, deal size, type, stage, cross-border direction, and advisors from each article; drops anything that isn't a real India-connected deal
3. **Dedupe** ([process/dedupe.py](process/dedupe.py)) — merges the same deal when multiple outlets cover it in one week, and tracks a deal's stage progressing across weeks (rumored → announced → completed)
4. **Digest** ([digest/](digest/)) — groups deals into sections (top deals, domestic M&A, cross-border, PE/VC, advisor mandates, rumored) and renders a branded HTML email
5. **Send** ([send/mailer.py](send/mailer.py)) — delivers via SMTP (Gmail app password or any relay)

All state lives in a SQLite file ([store/deal_radar.db](store/db.py)), so re-running never re-classifies or re-sends an article it's already seen.

## Sources

| Source | Type | Status |
|---|---|---|
| Entrackr | RSS | ✅ |
| Inc42 | RSS | ✅ |
| VCCircle | RSS | ❌ disabled — feed 500s server-side; recheck periodically |
| Economic Times | Scraper | ✅ |
| Mint | Scraper | ✅ |
| Business Standard | Scraper | ✅ |
| Moneycontrol | Scraper (parses embedded Next.js JSON) | ✅ |
| NSE corporate announcements | API, filtered to M&A-relevant categories | ✅ |
| CCI combination filings | API, no auth needed | ✅ |
| SEBI SAST takeover filings | Legacy HTML table | ✅ |
| BSE announcements | — | ❌ disabled — sits behind Akamai bot-protection that requires real browser JS; a plain HTTP client gets an empty 200, not an error, so it stays off rather than silently reporting zero BSE news every week |

Selectors and fetch logic live in [ingest/sources.py](ingest/sources.py) — each source is self-contained, so a site redesign only breaks its own function.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in the values below
```

Required in `.env` (or as real env vars / GitHub Actions secrets):

| Variable | What it's for |
|---|---|
| `GEMINI_API_KEY` | Free at [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — no credit card needed for Flash models |
| `SMTP_HOST` | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | e.g. `587` |
| `SMTP_USER` | Sending Gmail address |
| `SMTP_PASS` | A Gmail **app password**, not the account password — generate at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (requires 2-Step Verification) |

## Running locally

```bash
set -a; source .env; set +a
python main.py             # runs the full pipeline and sends the email
python main.py --no-send   # builds the digest but skips delivery
```

Output HTML lands at `output/digest_latest.html` either way.

## Configuration

Everything tunable lives in [config.yaml](config.yaml):

- **`recipients`** — distribution list, from address/name
- **`sources`** — enable/disable individual sources
- **`classification`** — which Gemini model, batch size, and rate-limit pacing (`min_seconds_between_calls`, tuned to the free tier's ~5 requests/minute limit — see note in the file if Google's actual limit changes)
- **`dedupe`** — fuzzy-match thresholds for merging duplicate coverage of the same deal
- **`digest`** — `pe_vc_min_size_usd_m` filters small PE/VC rounds out of that section (keeps it to substantial deal flow); brand colors/logo for the email template

## Automation

The schedule that actually matters is the cron in [.github/workflows/weekly-digest.yml](.github/workflows/weekly-digest.yml), not the one in `config.yaml` (informational only — keep them in sync). On trigger, it:

1. Restores the SQLite DB from the previous run's cache (falls back to a fresh one if none found)
2. Runs the full pipeline, **sending real email** (no `--no-send` on the scheduled trigger)
3. Uploads the rendered digest as a workflow artifact
4. Persists the DB back to cache for next week

Repo secrets needed: `GEMINI_API_KEY`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`.

To test without waiting for Monday or sending anything real, trigger it manually with the dry-run option:

```bash
gh workflow run weekly-digest.yml -f no_send=true
```

## Known limitations

- **BSE isn't scraped** — would need a headless browser (Playwright) to pass its bot-detection; deliberately not added to keep the dependency footprint small
- **Cross-week continuity is lightly tested** — the rumored → announced → completed stage-tracking logic works but has only run within a single real week so far
- **The GitHub Actions cache-based DB persistence isn't bulletproof** — GitHub evicts caches after ~7 days unused; a missed week would reset dedup/continuity history (not classification — already-seen articles still won't be re-fetched from their sources' RSS/listing pages once those pages move on)
- **No automated test suite** — the real dedup bugs found during development (see commit history) were caught by manually inspecting output, not by tests
- **No failure alerting** — if a scheduled run fails, nothing surfaces that beyond the Actions tab

## Project layout

```
config.yaml           # all tunables
main.py                # weekly entrypoint
ingest/                # per-source fetchers
process/
  schema.py            # Deal / RawArticle models
  classify.py           # Gemini extraction
  dedupe.py             # same-week + cross-week merge
store/db.py             # SQLite persistence
digest/
  build_digest.py       # groups deals into sections
  template.html          # email template
send/mailer.py          # SMTP delivery
.github/workflows/      # the actual weekly trigger
```
