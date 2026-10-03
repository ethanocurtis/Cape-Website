#!/usr/bin/env python3
"""Discord webhook alerts for the Minecraft Cape Tracker.

Standard library only. Runs two things in one process:
  * a small HTTP API (behind nginx at /api/) where visitors subscribe or
    unsubscribe a Discord webhook URL and pick which alerts they want,
  * a loop that re-reads site/data/capes.json every few minutes, works out
    what changed, and posts the matching alerts to each subscribed webhook.

Alert types (subscribers choose any of them):
  new      a cape that can be earned now or soon is added to capes.json
  open     an earn window starts, or a venue city opens
  closing  a cape stops being earnable, or a code-redemption deadline passes, within 24 hours
  changed  dates move, a venue city is added, or a new alert (e.g. "codes ran out") appears

Subscriptions and alert history live in DATA_DIR (a Docker volume), never in
the repo or under site/, because webhook URLs are secrets.
"""

import datetime as dt
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPES_JSON = os.path.join(os.environ.get("SITE_DIR", os.path.join(ROOT, "site")), "data", "capes.json")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SUBS_JSON = os.path.join(DATA_DIR, "subscriptions.json")
STATE_JSON = os.path.join(DATA_DIR, "state.json")

SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
PORT = int(os.environ.get("NOTIFIER_PORT", "8000"))
CHECK_EVERY = int(os.environ.get("NOTIFY_CHECK_MINUTES", "5")) * 60
MAX_SUBS = int(os.environ.get("MAX_SUBSCRIPTIONS", "2000"))
SUBS_PER_IP_PER_DAY = 5
CLOSING_WARNING = dt.timedelta(hours=24)
OPEN_GRACE = dt.timedelta(hours=24)  # don't announce windows that opened longer ago than this
HIDE_AFTER_MONTHS = 2

CENTRAL = ZoneInfo("America/Chicago")
UA = "MinecraftCapeTracker/1.0 (https://github.com/ethanocurtis/cape-website)"
EVENTS = ("new", "open", "closing", "changed")
EDITIONS = ("java", "bedrock")
EDITION_NAMES = {"java": "Java Edition", "bedrock": "Bedrock Edition"}
WEBHOOK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/(\d{15,25})/([A-Za-z0-9_-]{30,100})/?$")
COLORS = {"new": 0x3F7D2A, "open": 0x1F8A3B, "closing": 0xC0392B, "changed": 0x2A4F86, "test": 0x3F7D2A}

lock = threading.RLock()


def log(msg):
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] notifier: {msg}", flush=True)


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- dates (mirror site/assets/app.js)

def day_start(d):
    return dt.datetime.fromisoformat(d).replace(tzinfo=CENTRAL).astimezone(dt.timezone.utc)


def day_end(d):
    return day_start(d) + dt.timedelta(days=1) - dt.timedelta(milliseconds=1)


def parse_instant(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def window_start(w):
    if not w.get("start"):
        return None
    return day_start(w["start"]) if w.get("dateOnly") else parse_instant(w["start"])


def window_end(w):
    if not w.get("end"):
        return None
    if w.get("dateOnly"):
        return day_start(w["end"]) if w.get("exclusiveEnd") else day_end(w["end"])
    return parse_instant(w["end"])


def earn_windows(cape):
    """Every earn period as (key, label, start, end); venue cities count as earn periods."""
    out = []
    for w in cape.get("windows", []):
        if w.get("role") != "redeem":
            out.append((w.get("label", "earn"), w.get("label") or "Earn window", window_start(w), window_end(w)))
    for loc in cape.get("locations", []):
        if not loc.get("tba"):
            out.append(("city:" + loc["city"], loc["city"], day_start(loc["start"]), day_end(loc["end"])))
    return out


def cape_status(cape, now):
    if cape.get("always"):
        return {"code": "always"}
    open_until, next_start, closed_at = None, None, None
    has_tba = any(loc.get("tba") for loc in cape.get("locations", []))
    for _, _, s, e in earn_windows(cape):
        s = s or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        if s <= now and e is None:
            return {"code": "live", "until": None}
        if s <= now < e:
            open_until = e if open_until is None else max(open_until, e)
        elif s > now:
            next_start = s if next_start is None else min(next_start, s)
        if e is not None:
            closed_at = e if closed_at is None else max(closed_at, e)
    if open_until is not None:
        return {"code": "live", "until": open_until}
    if next_start is not None or has_tba:
        return {"code": "upcoming", "from": next_start}
    return {"code": "ended", "closedAt": closed_at}


def long_gone(cape, st, now):
    if st["code"] != "ended" or st["closedAt"] is None:
        return False
    redeem = [window_end(w) for w in cape.get("windows", []) if w.get("role") == "redeem"]
    if any(e and e > now for e in redeem):
        return False
    return now > st["closedAt"] + dt.timedelta(days=30 * HIDE_AFTER_MONTHS)


# ---------------------------------------------------------------- text helpers

def ts(when, style="F"):
    """Discord timestamp: shows in each reader's own time zone."""
    return f"<t:{int(when.timestamp())}:{style}>"


def show_window(w):
    if w.get("dateOnly"):
        def d(v):
            return dt.date.fromisoformat(v).strftime("%b %-d, %Y")
        if w.get("start") and w.get("end"):
            return f"{d(w['start'])} → {'before ' if w.get('exclusiveEnd') else ''}{d(w['end'])}"
        return ("until " if w.get("end") else "from ") + d(w.get("end") or w["start"])
    s, e = window_start(w), window_end(w)
    if s and e:
        return f"{ts(s, 'f')} → {ts(e, 'f')}"
    return f"until {ts(e, 'f')}" if e else f"from {ts(s, 'f')}"


def show_loc(loc):
    if loc.get("tba"):
        return f"dates {loc['tba']}" if loc["tba"].lower() != "tba" else "dates TBA"
    d = lambda v: dt.date.fromisoformat(v).strftime("%b %-d, %Y")  # noqa: E731
    return f"{d(loc['start'])} → {d(loc['end'])}"


def cape_name(cape, editions):
    ed = next((e for e in editions if e in cape["editions"]), cape["editions"][0])
    return cape["names"].get(ed) or cape["names"]["java"], ed


def cape_url(cape, edition):
    return f"{SITE_URL}/#{edition}-{cape['id']}" if SITE_URL else None


# ---------------------------------------------------------------- change detection

def snapshot(cape):
    return {
        "windows": {f"{w.get('role')}:{w.get('label')}": show_window(w) for w in cape.get("windows", [])},
        "labels": {f"{w.get('role')}:{w.get('label')}": w.get("label") for w in cape.get("windows", [])},
        "locations": {loc["city"]: show_loc(loc) for loc in cape.get("locations", [])},
        "alerts": list(cape.get("alerts", [])),
    }


def diff(old, new):
    lines = []
    for k, v in new["windows"].items():
        label = new["labels"].get(k) or k
        if k not in old["windows"]:
            lines.append(f"New date: **{label}**: {v}")
        elif old["windows"][k] != v:
            lines.append(f"**{label}** is now {v} (was {old['windows'][k]})")
    for city, v in new["locations"].items():
        if city not in old["locations"]:
            lines.append(f"New city: **{city}**, {v}")
        elif old["locations"][city] != v:
            lines.append(f"**{city}** is now {v} (was {old['locations'][city]})")
    for a in new["alerts"]:
        if a not in old["alerts"]:
            lines.append(f"⚠ {a}")
    return lines


def detect(catalog, state, now):
    """Return a list of events and update state in place. First run only records a baseline."""
    first_run = not state.get("initialized")
    known = state.setdefault("capes", {})
    sent = state.setdefault("sent", {})
    events = []

    def once(key, event, quiet):
        if key in sent:
            return
        sent[key] = now.isoformat(timespec="seconds")
        if not (first_run or quiet):
            events.append(event)

    for cape in catalog["capes"]:
        cid = cape["id"]
        st = cape_status(cape, now)
        snap = snapshot(cape)
        gone = long_gone(cape, st, now)
        is_new = cid not in known  # the "new" alert already says whether it's open

        if is_new:
            if not first_run and st["code"] != "ended":
                events.append({"type": "new", "cape": cape, "status": st})
        elif not gone and st["code"] != "always":
            changes = diff(known[cid], snap)
            if changes:
                events.append({"type": "changed", "cape": cape, "lines": changes})
        known[cid] = snap

        if st["code"] == "always" or gone:
            continue

        for key, label, s, e in earn_windows(cape):
            if s and s <= now and (e is None or now < e) and now - s <= OPEN_GRACE:
                once(f"open:{cid}:{key}:{s.isoformat()}",
                     {"type": "open", "cape": cape, "label": label, "start": s, "end": e,
                      "city": key.startswith("city:")}, is_new)

        if st["code"] == "live" and st["until"] and now < st["until"] <= now + CLOSING_WARNING:
            once(f"closing:{cid}:{st['until'].isoformat()}",
                 {"type": "closing", "cape": cape, "end": st["until"], "redeem": False}, is_new)

        if st["code"] == "ended":
            for w in cape.get("windows", []):
                e = window_end(w) if w.get("role") == "redeem" else None
                if e and now < e <= now + CLOSING_WARNING:
                    once(f"redeem:{cid}:{e.isoformat()}",
                         {"type": "closing", "cape": cape, "end": e, "redeem": True}, is_new)

    for cid in list(known):
        if cid not in {c["id"] for c in catalog["capes"]}:
            del known[cid]
    cutoff = (now - dt.timedelta(days=180)).isoformat()
    for k in [k for k, v in sent.items() if v < cutoff]:
        del sent[k]
    state["initialized"] = True
    return events


# ---------------------------------------------------------------- Discord

def embed_for(ev, editions):
    cape = ev["cape"]
    name, ed = cape_name(cape, editions)
    both = "java" in cape["editions"] and "bedrock" in cape["editions"]
    footer = "Java & Bedrock" if both else EDITION_NAMES[cape["editions"][0]]
    e = {"color": COLORS[ev["type"]], "footer": {"text": f"{footer} · Minecraft Cape Tracker"}}
    url = cape_url(cape, ed)
    if url:
        e["url"] = url

    if ev["type"] == "new":
        st = ev["status"]
        e["title"] = f"New cape: {name}"
        desc = [cape.get("category", "")]
        if st["code"] == "live":
            desc.append("**Available now**" + (f", ends {ts(st['until'])} ({ts(st['until'], 'R')})" if st.get("until") else ""))
        elif st["code"] == "upcoming" and st.get("from"):
            desc.append(f"Starts {ts(st['from'])} ({ts(st['from'], 'R')})")
        elif st["code"] == "upcoming":
            desc.append("Dates TBA")
        e["description"] = "\n".join(d for d in desc if d)
    elif ev["type"] == "open":
        if ev["city"]:
            e["title"] = f"{name}: now open in {ev['label']}"
            e["description"] = f"Earnable in person through {ev['end'].astimezone(CENTRAL).strftime('%b %-d, %Y')} (local date)."
        else:
            e["title"] = f"{name} is available now"
            e["description"] = f"**{ev['label']}**" + (f"\nEnds {ts(ev['end'])} ({ts(ev['end'], 'R')})" if ev["end"] else "")
    elif ev["type"] == "closing":
        if ev["redeem"]:
            e["title"] = f"Last chance to redeem your {name} code"
            e["description"] = f"Code redemption closes {ts(ev['end'])} ({ts(ev['end'], 'R')})."
        else:
            e["title"] = f"{name} ends soon"
            e["description"] = f"You can earn it until {ts(ev['end'])} ({ts(ev['end'], 'R')})."
    else:
        e["title"] = f"{name}: updated"
        e["description"] = "\n".join("• " + line for line in ev["lines"])[:4000]
    return e


class WebhookGone(Exception):
    pass


def post_webhook(url, payload, retries=3):
    body = json.dumps({**payload, "username": "Cape Tracker", "allowed_mentions": {"parse": []}}).encode()
    for _ in range(retries):
        req = urllib.request.Request(url + "?wait=true", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=15):
                return
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise WebhookGone(str(e.code)) from e
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 2))
                except ValueError:
                    wait = 2
                time.sleep(min(wait, 30))
                continue
            raise
    raise RuntimeError("rate limited")


def deliver(events):
    if not events:
        return
    with lock:
        subs = load_json(SUBS_JSON, [])
    removed = set()
    for sub in subs:
        mine = [ev for ev in events
                if ev["type"] in sub["events"] and set(ev["cape"]["editions"]) & set(sub["editions"])]
        for i in range(0, len(mine), 10):  # Discord allows 10 embeds per message
            try:
                post_webhook(sub["url"], {"embeds": [embed_for(ev, sub["editions"]) for ev in mine[i:i + 10]]})
            except WebhookGone:
                removed.add(sub["id"])
                break
            except Exception as e:  # noqa: BLE001
                log(f"webhook …{sub['id'][-4:]} failed: {e}")
                break
            time.sleep(0.3)
    if removed:
        with lock:
            save_json(SUBS_JSON, [s for s in load_json(SUBS_JSON, []) if s["id"] not in removed])
        log(f"removed {len(removed)} deleted webhook(s)")
    log(f"sent {len(events)} alert(s) to {len(subs) - len(removed)} webhook(s)")


def check_loop():
    me = os.path.abspath(__file__)
    mtime = os.path.getmtime(me)
    while True:
        try:
            with open(CAPES_JSON) as f:
                catalog = json.load(f)
            state = load_json(STATE_JSON, {})
            first = not state.get("initialized")
            events = detect(catalog, state, now_utc())
            save_json(STATE_JSON, state)  # first, so a crash mid-send never repeats alerts
            deliver(events)
            if first:
                log(f"baseline recorded for {len(catalog['capes'])} capes")
        except Exception as e:  # noqa: BLE001
            log(f"check failed: {e}")
        time.sleep(CHECK_EVERY)
        if os.path.getmtime(me) != mtime:  # the updater pulled a new version from GitHub
            log("code changed; restarting")
            os.execv(sys.executable, [sys.executable, me])


# ---------------------------------------------------------------- HTTP API

recent_ips = {}  # hashed IP -> [timestamps of new subscriptions]


def normalize_webhook(url):
    m = WEBHOOK_RE.match((url or "").strip())
    if not m:
        return None, None
    return m.group(1), f"https://discord.com/api/webhooks/{m.group(1)}/{m.group(2)}"


def subscribe(body, ip):
    wid, url = normalize_webhook(body.get("url"))
    if not url:
        return 400, "That doesn't look like a Discord webhook URL. It should start with https://discord.com/api/webhooks/"
    events = [e for e in body.get("events", []) if e in EVENTS]
    editions = [e for e in body.get("editions", []) if e in EDITIONS]
    if not events:
        return 400, "Pick at least one kind of alert."
    if not editions:
        return 400, "Pick at least one edition."

    with lock:
        subs = load_json(SUBS_JSON, [])
        existing = next((s for s in subs if s["id"] == wid), None)
        if not existing:
            key = hashlib.sha256(ip.encode()).hexdigest()
            day_ago = time.time() - 86400
            hits = [t for t in recent_ips.get(key, []) if t > day_ago]
            if len(hits) >= SUBS_PER_IP_PER_DAY:
                return 429, "Too many new webhooks from your network today. Try again tomorrow."
            if len(subs) >= MAX_SUBS:
                return 503, "Alerts are full right now. Please try again later."

    labels = {"new": "new capes", "open": "capes opening", "closing": "capes ending within 24 hours",
              "changed": "date changes and alerts"}
    try:
        post_webhook(url, {"embeds": [{
            "color": COLORS["test"],
            "title": "Cape Tracker alerts are set up" if not existing else "Cape Tracker alerts updated",
            "description": "This channel will get alerts for: " + ", ".join(labels[e] for e in events) +
                           ".\nEditions: " + " and ".join(EDITION_NAMES[e] for e in editions) + "." +
                           (f"\n\nChange or stop alerts any time at {SITE_URL}" if SITE_URL else ""),
        }]})
    except WebhookGone:
        return 400, "Discord says that webhook doesn't exist. Check you copied the whole URL."
    except Exception as e:  # noqa: BLE001
        log(f"test message failed: {e}")
        return 502, "Couldn't reach Discord. Please try again in a minute."

    with lock:
        subs = [s for s in load_json(SUBS_JSON, []) if s["id"] != wid]
        subs.append({"id": wid, "url": url, "events": events, "editions": editions,
                     "created": (existing or {}).get("created") or now_utc().isoformat(timespec="seconds")})
        save_json(SUBS_JSON, subs)
        if not existing:
            key = hashlib.sha256(ip.encode()).hexdigest()
            recent_ips.setdefault(key, []).append(time.time())
    log(f"{'updated' if existing else 'new'} subscription …{wid[-4:]} ({len(subs)} total)")
    return 200, ("Updated. Check your Discord channel for a confirmation." if existing
                 else "Done! Check your Discord channel for a test message.")


def unsubscribe(body):
    wid, url = normalize_webhook(body.get("url"))
    if not url:
        return 400, "That doesn't look like a Discord webhook URL."
    with lock:
        subs = load_json(SUBS_JSON, [])
        keep = [s for s in subs if s["id"] != wid]
        if len(keep) == len(subs):
            return 404, "That webhook isn't subscribed."
        save_json(SUBS_JSON, keep)
    try:
        post_webhook(url, {"embeds": [{"color": COLORS["changed"], "title": "Cape Tracker alerts stopped",
                                       "description": "This channel won't get any more cape alerts."}]})
    except Exception:  # noqa: BLE001
        pass
    log(f"unsubscribed …{wid[-4:]}")
    return 200, "Unsubscribed. You won't get any more alerts."


class Handler(BaseHTTPRequestHandler):
    server_version = "CapeNotifier"

    def log_message(self, *args):
        pass

    def reply(self, code, message):
        data = json.dumps({"ok": code == 200, "message": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        routes = {"/api/subscribe": "sub", "/api/unsubscribe": "unsub"}
        route = routes.get(self.path.split("?")[0])
        if not route:
            return self.reply(404, "Not found.")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            return self.reply(413, "Request too large.")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.reply(400, "Bad request.")
        ip = self.headers.get("X-Real-IP") or self.client_address[0]
        try:
            code, msg = subscribe(body, ip) if route == "sub" else unsubscribe(body)
        except Exception as e:  # noqa: BLE001
            log(f"api error: {e}")
            code, msg = 500, "Something went wrong. Please try again."
        self.reply(code, msg)

    def do_GET(self):  # noqa: N802
        if self.path == "/api/health":
            return self.reply(200, "ok")
        self.reply(404, "Not found.")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    threading.Thread(target=check_loop, daemon=True).start()
    log(f"listening on :{PORT}, checking every {CHECK_EVERY // 60} min")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
