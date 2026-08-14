# zeinan.fyi

A two-page site: `report` — what your browser and network reveal about you, and `tel` — a network telemetry dashboard. The two pages are linked by a terminal-style nav widget at the top of both.

## Repository structure

ndex.html — report page (zeinan/report)
pulse.html — tel page (zeinan/tel), the dashboard
data/pulse-status.json — telemetry data, written by the collector
scripts/check_pulse.py — collector script (uses Globalping)
.github/workflows/pulse-check.yml — schedule: runs the collector and pushes data to S3
.github/workflows/deploy-site.yml — on push to main: pushes index.html/pulse.html to S3
aws-iam-policy.json — IAM policy for the AWS user both workflows use

Everything from the collector archive needs to live at the repo root, at exactly these paths — `pulse.html`, once deployed, also fetches its data via the relative path `data/pulse-status.json`, so the folder layout in S3 has to mirror the one in the repo.

## Nav terminal

The block at the top of both pages is a small terminal: type `report` or `tel` (or click the `./report` / `./tel` chips), and it plays a short connection animation before navigating to the matching page via `window.location.href`. The prompt — `guest-XXXX@zeinan.fyi` — takes the last 4 digits of the visitor's real IP from `api.ipify.org`.

## How the telemetry (`tel`) works

The data is written by `scripts/check_pulse.py`, run on a schedule by `.github/workflows/pulse-check.yml` (every 10 minutes). The script:

- for each of the 10 regions, runs an HTTP measurement through the free [Globalping](https://globalping.io) API from a probe near that region (a `magic` location like `us-east-1`), and pulls the response time (TTFB) and HTTP status;
- while it's at it, also pulls the TLS certificate (expiry, issuer) and DNS resolution time from one of those same measurements — no separate request needed;
- compares each region's status against the previous run and, if it changed, adds a line to the event log;
- keeps a short rolling history of TTFB for the chart, and counters for "how many checks in a row succeeded" to compute an uptime percentage (this isn't a real 30-day SLA number, just tracking since the collector started — the UI is honest about this and labels it "trk" rather than "30d");
- writes everything to `data/pulse-status.json` and commits the file back to the repo.

## How delivery to the site works

`zeinan.fyi` is hosted on S3, and DNS points there. GitHub is the source of truth for code and data, but doesn't serve anything to visitors on its own — two workflows push changes through to the bucket:

- **`pulse-check.yml`** (scheduled, every 10 minutes) — after the collector updates `data/pulse-status.json` and commits it to the repo, the same run uploads that file to `s3://<bucket>/data/pulse-status.json`.
- **`deploy-site.yml`** (on push to `main`) — only triggers when `index.html` or `pulse.html` change, and uploads them to the bucket. It ignores the pulse-bot's data-only commits — the trigger paths are scoped specifically to those two files.


### Optional: Globalping token

Globalping's free anonymous access is roughly 250 tests/hour, but that quota is shared across the GitHub runner's IP, which many other jobs use too. If the collector starts failing on rate limits often:

1. Sign up at [dash.globalping.io](https://dash.globalping.io) (free).
2. Copy the token and add it to the repo as a secret: **Settings → Secrets and variables → Actions → New repository secret**, named `GLOBALPING_TOKEN`.
3. The script picks it up automatically on the next run — the quota becomes dedicated to you instead of shared by IP.

## Known limitations

- Some regions (e.g. `me-south-1`, `sa-east-1`) have thinner probe coverage on Globalping's network — if no probe can be matched, that region is marked `down` with `no probe` instead of crashing the whole script.
- The chart history isn't literally "the last 24 hours" — it's the last ~48 checks in a row (at a 10-minute interval, that's roughly 8 hours); the chart title switches to "recent checks" when real data is in use, so it doesn't overstate what it's showing.
