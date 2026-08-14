#!/usr/bin/env python3
"""
zeinan/tel — pulse collector

Queries the Globalping API (https://globalping.io) from probes located in
(or close to) each AWS region, measures HTTP reachability / TTFB, and pulls
DNS + TLS certificate info from one of the checks. Writes everything to
data/pulse-status.json, which pulse.html fetches at page load.

Design notes:
- Globalping is free, community-run, and doesn't require an API key for
  light use (~250 anonymous tests/hour, shared across the runner's IP).
  If you create a free account and put its token in the GLOBALPING_TOKEN
  secret, the script will use it automatically and get a higher, dedicated
  quota — recommended if you tighten the schedule below 10 minutes.
- Some regions (e.g. me-south-1, sa-east-1) have thinner probe coverage on
  Globalping's network. If a region can't be measured, it's marked as
  status "down" with http_status "no probe" rather than crashing the run.
- "uptime" is tracked from the first time this script ever ran successfully
  for that region — not a true 30-day SLA number — so pulse.html labels it
  as "tracked" rather than "30d".
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

GLOBALPING_API = "https://api.globalping.io/v1/measurements"
TARGET_URL = os.environ.get("PULSE_TARGET_URL", "https://zeinan.fyi")
GLOBALPING_TOKEN = os.environ.get("GLOBALPING_TOKEN", "").strip()

DATA_PATH = os.environ.get("PULSE_DATA_PATH", "data/pulse-status.json")

# region code -> Globalping magic location string
REGIONS = [
    {"code": "us-east-1",      "magic": "us-east-1"},
    {"code": "us-east-2",      "magic": "us-east-2"},
    {"code": "us-west-2",      "magic": "us-west-2"},
    {"code": "us-west-1",      "magic": "us-west-1"},
    {"code": "eu-west-1",      "magic": "eu-west-1"},
    {"code": "eu-central-1",   "magic": "eu-central-1"},
    {"code": "me-south-1",     "magic": "me-south-1"},
    {"code": "ap-southeast-1", "magic": "ap-southeast-1"},
    {"code": "ap-southeast-2", "magic": "ap-southeast-2"},
    {"code": "sa-east-1",      "magic": "sa-east-1"},
]

# thresholds, in ms, for classifying a successful response
WARN_ABOVE_MS = 150
DOWN_ABOVE_MS = 3000  # effectively unreachable / timed out territory

HISTORY_MAX_POINTS = 48
EVENTS_MAX = 30
POLL_INTERVAL_S = 1.5
POLL_MAX_TRIES = 12
REQUEST_TIMEOUT_S = 10


def http_json(url, method="GET", body=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if GLOBALPING_TOKEN:
        headers["Authorization"] = f"Bearer {GLOBALPING_TOKEN}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def create_measurement(magic_location):
    body = {
        "target": TARGET_URL,
        "type": "http",
        "locations": [{"magic": magic_location, "limit": 1}],
        "measurementOptions": {
            "request": {"method": "HEAD"},
        },
    }
    return http_json(GLOBALPING_API, method="POST", body=body)


def poll_measurement(measurement_id):
    url = f"{GLOBALPING_API}/{measurement_id}"
    for _ in range(POLL_MAX_TRIES):
        result = http_json(url, method="GET")
        if result.get("status") != "in-progress":
            return result
        time.sleep(POLL_INTERVAL_S)
    return result  # return whatever we last got, even if still in-progress


def classify(status_code, ttfb_ms, probe_error):
    if probe_error or status_code is None:
        return "down"
    if not (200 <= status_code < 400):
        return "down"
    if ttfb_ms is None or ttfb_ms > DOWN_ABOVE_MS:
        return "down"
    if ttfb_ms > WARN_ABOVE_MS:
        return "warn"
    return "good"


def check_region(region):
    code = region["code"]
    try:
        created = create_measurement(region["magic"])
        measurement_id = created.get("id")
        if not measurement_id:
            return {"code": code, "status": "down", "http_status": "no probe", "ttfb_ms": None}

        result = poll_measurement(measurement_id)
        probes = result.get("results", [])
        if not probes:
            return {"code": code, "status": "down", "http_status": "no probe", "ttfb_ms": None}

        probe_result = probes[0].get("result", {})
        probe_status = probes[0].get("result", {}).get("status")  # 'finished' | 'failed' etc (varies by type)
        status_code = probe_result.get("statusCode")
        timings = probe_result.get("timings") or {}
        ttfb = timings.get("firstByte")
        dns_ms = timings.get("dns")
        tls = probe_result.get("tls")

        probe_error = probe_status == "failed" or status_code is None
        status = classify(status_code, ttfb, probe_error)

        http_status_label = f"{status_code}" if status_code else ("timeout" if probe_error else "—")

        return {
            "code": code,
            "status": status,
            "http_status": http_status_label,
            "ttfb_ms": round(ttfb) if isinstance(ttfb, (int, float)) else None,
            "dns_ms": round(dns_ms) if isinstance(dns_ms, (int, float)) else None,
            "tls": tls,
        }
    except urllib.error.HTTPError as e:
        return {"code": code, "status": "down", "http_status": f"http {e.code}", "ttfb_ms": None}
    except Exception as e:  # noqa: BLE001 — this is a scheduled job, must not hard-crash on one bad region
        return {"code": code, "status": "down", "http_status": "error", "ttfb_ms": None, "error": str(e)}


def load_previous():
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def main():
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_hms = now.strftime("%H:%M:%S")

    previous = load_previous() or {}
    prev_regions = previous.get("regions", {})
    prev_history = previous.get("history", {})
    prev_events = previous.get("events", [])
    prev_uptime = previous.get("uptime_tracking", {})
    tracking_started_at = previous.get("tracking_started_at", now_iso)

    results = [check_region(r) for r in REGIONS]

    regions_out = {}
    history_out = {}
    new_events = []

    for r in results:
        code = r["code"]
        prev = prev_regions.get(code, {})

        # uptime bookkeeping: cumulative good/total checks since tracking began
        counters = prev_uptime.get(code, {"total": 0, "good": 0})
        counters["total"] += 1
        if r["status"] == "good":
            counters["good"] += 1
        prev_uptime[code] = counters
        uptime_pct = round((counters["good"] / counters["total"]) * 100, 2) if counters["total"] else None

        regions_out[code] = {
            "status": r["status"],
            "http_status": r.get("http_status"),
            "ttfb_ms": r.get("ttfb_ms"),
            "uptime_tracked_pct": uptime_pct,
        }

        # rolling history for the chart
        hist = list(prev_history.get(code, []))
        hist.append({"t": now_hms, "ttfb": r.get("ttfb_ms")})
        hist = hist[-HISTORY_MAX_POINTS:]
        history_out[code] = hist

        # event log: only record on status change
        prev_status = prev.get("status")
        if prev_status is not None and prev_status != r["status"]:
            if r["status"] == "down":
                text = f"unreachable — {r.get('http_status')}"
            elif r["status"] == "warn":
                ttfb = r.get("ttfb_ms")
                text = f"latency degraded — {ttfb} ms" if ttfb is not None else "degraded"
            else:
                ttfb = r.get("ttfb_ms")
                text = f"recovered — {ttfb} ms" if ttfb is not None else "recovered"
            new_events.append({"t": now_hms, "region": code, "kind": r["status"], "text": text})

    events_out = (new_events + prev_events)[:EVENTS_MAX]

    overall = (
        "down" if any(r["status"] == "down" for r in results)
        else "warn" if any(r["status"] == "warn" for r in results)
        else "good"
    )

    # Global TLS/DNS card: reuse the first region that returned tls info
    tls_info = None
    dns_ms = None
    for r in results:
        if r.get("tls") and not tls_info:
            tls_info = r["tls"]
        if r.get("dns_ms") is not None and dns_ms is None:
            dns_ms = r["dns_ms"]

    global_out = {"tls": None, "dns": None}
    if tls_info and tls_info.get("expiresAt"):
        try:
            expires = datetime.fromisoformat(tls_info["expiresAt"].replace("Z", "+00:00"))
            days_left = (expires - now).days
            issuer = (tls_info.get("issuer") or {}).get("O", "unknown issuer")
            global_out["tls"] = {
                "days_left": days_left,
                "issuer": issuer,
                "status": "good" if days_left > 14 else ("warn" if days_left > 0 else "down"),
            }
        except Exception:
            pass
    if dns_ms is not None:
        global_out["dns"] = {"ms": dns_ms, "status": "good" if dns_ms < 50 else "warn"}

    output = {
        "generated_at": now_iso,
        "target": TARGET_URL,
        "overall": overall,
        "tracking_started_at": tracking_started_at,
        "global": global_out,
        "regions": regions_out,
        "history": history_out,
        "events": events_out,
        "uptime_tracking": prev_uptime,
    }

    os.makedirs(os.path.dirname(DATA_PATH) or ".", exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"wrote {DATA_PATH}: overall={overall}, regions={len(regions_out)}, events_added={len(new_events)}")


if __name__ == "__main__":
    sys.exit(main())
