#!/usr/bin/env python3
"""Discord and email alerts for the Minecraft Cape Tracker.

Standard library only. Runs two things in one process:
  * a small HTTP API (behind nginx at /api/) where visitors subscribe a
    Discord webhook URL or an email address and pick which alerts they want,
  * a loop that re-reads site/data/capes.json every few minutes, works out
    what changed, and sends the matching alerts to every subscriber.

Alert types (subscribers choose any of them):
  new      a cape that can be earned now or soon is added to capes.json
  open     an earn window starts, or a venue city opens
  closing  a cape stops being earnable, or a code-redemption deadline passes, within 24 hours
  changed  dates move, a venue city is added, or a new alert (e.g. "codes ran out") appears

Email uses double opt-in: nothing is sent until the address owner clicks the
confirmation link, and every email has a one-click unsubscribe link. Email is
off unless SMTP_HOST, SMTP_FROM and SITE_URL are set.

Subscriptions and alert history live in DATA_DIR (a Docker volume), never in
the repo or under site/, because webhook URLs are secrets and emails are
personal data.
"""

import datetime as dt
import hashlib
import html
import json
import os
import re
import secrets
import smtplib
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPES_JSON = os.path.join(os.environ.get("SITE_DIR", os.path.join(ROOT, "site")), "data", "capes.json")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SUBS_JSON = os.path.join(DATA_DIR, "subscriptions.json")
EMAILS_JSON = os.path.join(DATA_DIR, "emails.json")
STATE_JSON = os.path.join(DATA_DIR, "state.json")
STATE_VERSION = 2

SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
PORT = int(os.environ.get("NOTIFIER_PORT", "8000"))
CHECK_EVERY = int(os.environ.get("NOTIFY_CHECK_MINUTES", "5")) * 60
MAX_SUBS = int(os.environ.get("MAX_SUBSCRIPTIONS", "2000"))
MAX_EMAILS = int(os.environ.get("MAX_EMAIL_SUBSCRIBERS", "5000"))
SUBS_PER_IP_PER_DAY = 5
CONFIRM_EXPIRES = dt.timedelta(hours=48)
CONFIRM_RESEND = dt.timedelta(minutes=10)
CLOSING_WARNING = dt.timedelta(hours=24)
OPEN_GRACE = dt.timedelta(hours=24)  # don't announce windows that opened longer ago than this
HIDE_AFTER_MONTHS = 2

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT") or 587)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "")
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls").lower()  # starttls, ssl or none
EMAIL_ENABLED = bool(SMTP_HOST and SMTP_FROM and SITE_URL)

CENTRAL = ZoneInfo("America/Chicago")
UA = "MinecraftCapeTracker/1.0 (https://github.com/ethanocurtis/cape-website)"
EVENTS = ("new", "open", "closing", "changed")
EDITIONS = ("java", "bedrock")
EDITION_NAMES = {"java": "Java Edition", "bedrock": "Bedrock Edition"}
EVENT_LABELS = {"new": "new capes", "open": "capes opening", "closing": "capes ending within 24 hours",
                "changed": "date changes and warnings"}
WEBHOOK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/(\d{15,25})/([A-Za-z0-9_-]{30,100})/?$")
EMAIL_RE = re.compile(r"^[^@\s<>\"',;]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
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


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


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


# ---------------------------------------------------------------- time formatting per channel

class DiscordTime:
    """Discord timestamps show in each reader's own time zone."""

    @staticmethod
    def full(when):
        return f"<t:{int(when.timestamp())}:F>"

    @staticmethod
    def short(when):
        return f"<t:{int(when.timestamp())}:f>"

    @staticmethod
    def rel(when):
        return f"<t:{int(when.timestamp())}:R>"


class EmailTime:
    """Email can't localize, so use US Central like the site does."""

    @staticmethod
    def full(when):
        t = when.astimezone(CENTRAL)
        return t.strftime("%a, %b %-d, %Y, %-I:%M %p ") + t.tzname()

    @staticmethod
    def short(when):
        t = when.astimezone(CENTRAL)
        return t.strftime("%b %-d, %Y, %-I:%M %p ") + t.tzname()

    @staticmethod
    def rel(when):
        secs = (when - now_utc()).total_seconds()
        if secs <= 0:
            return "now"
        hours = round(secs / 3600)
        if hours < 1:
            return f"in {max(1, round(secs / 60))} min"
        if hours < 48:
            return f"in {hours} hour{'s' if hours != 1 else ''}"
        return f"in {round(hours / 24)} days"


def fmt_date(v):
    return dt.date.fromisoformat(v).strftime("%b %-d, %Y")


def fmt_window(w, T):
    if w.get("dateOnly"):
        if w.get("start") and w.get("end"):
            return f"{fmt_date(w['start'])} → {'before ' if w.get('exclusiveEnd') else ''}{fmt_date(w['end'])}"
        return ("until " if w.get("end") else "from ") + fmt_date(w.get("end") or w["start"])
    s, e = window_start(w), window_end(w)
    if s and e:
        return f"{T.short(s)} → {T.short(e)}"
    return f"until {T.short(e)}" if e else f"from {T.short(s)}"


def fmt_loc(loc):
    if loc.get("tba"):
        return "dates TBA" if loc["tba"].lower() == "tba" else f"dates {loc['tba']}"
    return f"{fmt_date(loc['start'])} → {fmt_date(loc['end'])}"


def cape_name(cape, editions):
    ed = next((e for e in editions if e in cape["editions"]), cape["editions"][0])
    return cape["names"].get(ed) or cape["names"]["java"], ed


def cape_url(cape, edition):
    return f"{SITE_URL}/#{edition}-{cape['id']}" if SITE_URL else None


# ---------------------------------------------------------------- change detection

WINDOW_KEYS = ("label", "start", "end", "dateOnly", "exclusiveEnd")


def snapshot(cape):
    return {
        "windows": {f"{w.get('role')}:{w.get('label')}": {k: w.get(k) for k in WINDOW_KEYS}
                    for w in cape.get("windows", [])},
        "locations": {loc["city"]: {k: loc.get(k) for k in ("start", "end", "tba")}
                      for loc in cape.get("locations", [])},
        "alerts": list(cape.get("alerts", [])),
    }


def diff(old, new):
    """Changes as plain data; they're turned into text per channel when sent."""
    out = []
    for k, w in new["windows"].items():
        if k not in old["windows"]:
            out.append({"kind": "window_new", "new": w})
        elif old["windows"][k] != w:
            out.append({"kind": "window", "new": w, "old": old["windows"][k]})
    for city, loc in new["locations"].items():
        if city not in old["locations"]:
            out.append({"kind": "city_new", "city": city, "new": loc})
        elif old["locations"][city] != loc:
            out.append({"kind": "city", "city": city, "new": loc, "old": old["locations"][city]})
    for a in new["alerts"]:
        if a not in old["alerts"]:
            out.append({"kind": "alert", "text": a})
    return out


def detect(catalog, state, now):
    """Return a list of events and update state in place. First run only records a baseline."""
    first_run = not state.get("initialized")
    if state.get("v") != STATE_VERSION:  # snapshot format changed: re-record quietly
        state["capes"] = {}
        rebaseline = True
    else:
        rebaseline = False
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
        is_new = cid not in known and not rebaseline  # the "new" alert already says whether it's open

        if is_new:
            if not first_run and st["code"] != "ended":
                events.append({"type": "new", "cape": cape, "status": st})
        elif cid in known and not gone and st["code"] != "always":
            changes = diff(known[cid], snap)
            if changes:
                events.append({"type": "changed", "cape": cape, "changes": changes})
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
    state["v"] = STATE_VERSION
    return events


# ---------------------------------------------------------------- alert text (shared by Discord and email)

def change_line(c, T):
    if c["kind"] == "window_new":
        return f"New date: **{c['new']['label']}**: {fmt_window(c['new'], T)}"
    if c["kind"] == "window":
        return f"**{c['new']['label']}** is now {fmt_window(c['new'], T)} (was {fmt_window(c['old'], T)})"
    if c["kind"] == "city_new":
        return f"New city: **{c['city']}**, {fmt_loc(c['new'])}"
    if c["kind"] == "city":
        return f"**{c['city']}** is now {fmt_loc(c['new'])} (was {fmt_loc(c['old'])})"
    return f"⚠ {c['text']}"


def describe(ev, editions, T):
    """One alert as {title, lines, url, color, footer}. Lines may contain **bold**."""
    cape = ev["cape"]
    name, ed = cape_name(cape, editions)
    both = "java" in cape["editions"] and "bedrock" in cape["editions"]
    out = {"color": COLORS[ev["type"]], "url": cape_url(cape, ed), "lines": [],
           "footer": "Java & Bedrock" if both else EDITION_NAMES[cape["editions"][0]]}

    if ev["type"] == "new":
        st = ev["status"]
        out["title"] = f"New cape: {name}"
        if cape.get("category"):
            out["lines"].append(cape["category"])
        if st["code"] == "live":
            out["lines"].append("**Available now**" + (f", ends {T.full(st['until'])} ({T.rel(st['until'])})" if st.get("until") else ""))
        elif st["code"] == "upcoming" and st.get("from"):
            out["lines"].append(f"Starts {T.full(st['from'])} ({T.rel(st['from'])})")
        elif st["code"] == "upcoming":
            out["lines"].append("Dates TBA")
    elif ev["type"] == "open":
        if ev["city"]:
            out["title"] = f"{name}: now open in {ev['label']}"
            out["lines"].append(f"Earnable in person through {ev['end'].astimezone(CENTRAL).strftime('%b %-d, %Y')} (local date).")
        else:
            out["title"] = f"{name} is available now"
            out["lines"].append(f"**{ev['label']}**")
            if ev["end"]:
                out["lines"].append(f"Ends {T.full(ev['end'])} ({T.rel(ev['end'])})")
    elif ev["type"] == "closing":
        if ev["redeem"]:
            out["title"] = f"Last chance to redeem your {name} code"
            out["lines"].append(f"Code redemption closes {T.full(ev['end'])} ({T.rel(ev['end'])}).")
        else:
            out["title"] = f"{name} ends soon"
            out["lines"].append(f"You can earn it until {T.full(ev['end'])} ({T.rel(ev['end'])}).")
    else:
        out["title"] = f"{name}: updated"
        out["lines"] = ["• " + change_line(c, T) for c in ev["changes"]]
    return out


def wants(sub, ev):
    return ev["type"] in sub["events"] and set(ev["cape"]["editions"]) & set(sub["editions"])


# ---------------------------------------------------------------- Discord

def discord_embed(ev, editions):
    d = describe(ev, editions, DiscordTime)
    e = {"color": d["color"], "title": d["title"], "description": "\n".join(d["lines"])[:4000],
         "footer": {"text": f"{d['footer']} · Minecraft Cape Tracker"}}
    if d["url"]:
        e["url"] = d["url"]
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


def deliver_discord(events):
    with lock:
        subs = load_json(SUBS_JSON, [])
    removed, reached = set(), 0
    for sub in subs:
        mine = [ev for ev in events if wants(sub, ev)]
        if mine:
            reached += 1
        for i in range(0, len(mine), 10):  # Discord allows 10 embeds per message
            try:
                post_webhook(sub["url"], {"embeds": [discord_embed(ev, sub["editions"]) for ev in mine[i:i + 10]]})
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
    if reached:
        log(f"discord: sent alerts to {reached - len(removed)} webhook(s)")


# ---------------------------------------------------------------- email

def bold_html(s):
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(s))


def bold_text(s):
    return s.replace("**", "")


def email_html(heading, blocks, footer_html, button=None):
    """blocks: list of (title, url, lines); button: optional (label, url). Inline styles only, as email clients need."""
    parts = []
    for title, url, lines in blocks:
        t = html.escape(title)
        if url:
            t = f'<a href="{html.escape(url)}" style="color:#3f7d2a;text-decoration:none">{t}</a>'
        body = "".join(f'<p style="margin:4px 0;font-size:15px;line-height:1.5">{bold_html(x)}</p>' for x in lines)
        if button:
            body += (f'<p style="margin:14px 0 2px"><a href="{html.escape(button[1])}" style="display:inline-block;background:#3f7d2a;'
                     f'color:#fff;padding:10px 18px;border-radius:999px;text-decoration:none;font-weight:600">{html.escape(button[0])}</a></p>')
        parts.append(f'<div style="border:1px solid #dedcd3;border-radius:10px;padding:14px 16px;margin:0 0 12px;background:#fff">'
                     f'<h2 style="margin:0 0 6px;font-size:17px">{t}</h2>{body}</div>')
    return (f'<!doctype html><html><body style="margin:0;padding:24px 12px;background:#f6f5f1;color:#1d1f1c;'
            f'font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif">'
            f'<div style="max-width:560px;margin:0 auto">'
            f'<p style="margin:0 0 14px;font-size:13px;color:#62665f;font-weight:600">{html.escape(heading)}</p>'
            f'{"".join(parts)}'
            f'<p style="margin:18px 0 0;font-size:12px;line-height:1.5;color:#62665f">{footer_html}</p>'
            f'</div></body></html>')


def build_email(to, subject, text, html_body, unsub_url=None):
    msg = EmailMessage()
    name, addr = parseaddr(SMTP_FROM)
    msg["From"] = formataddr((name or "Cape Tracker", addr))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=addr.split("@")[-1] or None)
    if unsub_url:
        msg["List-Unsubscribe"] = f"<{unsub_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    return msg


def smtp_connect():
    if SMTP_SECURITY == "ssl":
        s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30, context=ssl.create_default_context())
    else:
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        if SMTP_SECURITY == "starttls":
            s.starttls(context=ssl.create_default_context())
    if SMTP_USER:
        s.login(SMTP_USER, SMTP_PASSWORD)
    return s


def send_emails(messages):
    """Send a batch over one connection. Returns how many were accepted."""
    if not messages:
        return 0
    ok = 0
    with smtp_connect() as s:
        for msg in messages:
            try:
                s.send_message(msg)
                ok += 1
            except smtplib.SMTPRecipientsRefused:
                log("email: a recipient was refused")
            time.sleep(0.2)
    return ok


def unsub_url(sub):
    return f"{SITE_URL}/api/email/unsubscribe?t={sub['unsub']}"


def alert_email(sub, mine):
    blocks = [(d["title"], d["url"], d["lines"]) for d in (describe(ev, sub["editions"], EmailTime) for ev in mine)]
    subject = blocks[0][0] if len(blocks) == 1 else "Cape alerts: " + " · ".join(b[0] for b in blocks)
    if len(subject) > 120:
        subject = subject[:117].rstrip() + "…"
    link = unsub_url(sub)
    text = "\n\n".join(
        f"{title}\n" + "\n".join(bold_text(x) for x in lines) + (f"\n{url}" if url else "") for title, url, lines in blocks
    ) + f"\n\nTimes are US Central.\nYou signed up for cape alerts at {SITE_URL}.\nUnsubscribe: {link}\n"
    footer = (f'Times are US Central. You signed up for cape alerts at <a href="{html.escape(SITE_URL)}" style="color:#62665f">'
              f'{html.escape(SITE_URL.split("//")[-1])}</a>. To change what you get, sign up again with new choices. '
              f'<a href="{html.escape(link)}" style="color:#62665f">Unsubscribe</a>')
    return build_email(sub["email"], subject, text, email_html("Minecraft Cape Tracker", blocks, footer), link)


def deliver_email(events):
    if not EMAIL_ENABLED:
        return
    with lock:
        subs = [s for s in load_json(EMAILS_JSON, []) if s.get("confirmed")]
    messages = []
    for sub in subs:
        mine = [ev for ev in events if wants(sub, ev)]
        if mine:
            messages.append(alert_email(sub, mine))
    if messages:
        try:
            log(f"email: sent {send_emails(messages)} of {len(messages)} alert email(s)")
        except Exception as e:  # noqa: BLE001
            log(f"email: sending failed: {e}")


def deliver(events):
    if not events:
        return
    deliver_discord(events)
    deliver_email(events)
    log(f"processed {len(events)} alert(s)")


def prune_unconfirmed():
    with lock:
        subs = load_json(EMAILS_JSON, [])
        now = now_utc().isoformat()
        changed = False
        for s in subs:
            if s.get("pending") and s["pending"]["expires"] < now:
                s.pop("pending")
                changed = True
        keep = [s for s in subs if s.get("confirmed") or s.get("pending")]
        if changed:
            save_json(EMAILS_JSON, keep)


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
            prune_unconfirmed()
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


def ip_allowed(ip):
    day_ago = time.time() - 86400
    return len([t for t in recent_ips.get(sha(ip), []) if t > day_ago]) < SUBS_PER_IP_PER_DAY


def ip_record(ip):
    recent_ips.setdefault(sha(ip), []).append(time.time())


def read_prefs(body):
    events = [e for e in body.get("events", []) if e in EVENTS]
    editions = [e for e in body.get("editions", []) if e in EDITIONS]
    if not events:
        return None, "Pick at least one kind of alert."
    if not editions:
        return None, "Pick at least one edition."
    return (events, editions), None


def describe_prefs(events, editions):
    return ("alerts for: " + ", ".join(EVENT_LABELS[e] for e in events) +
            ". Editions: " + " and ".join(EDITION_NAMES[e] for e in editions) + ".")


def normalize_webhook(url):
    m = WEBHOOK_RE.match((url or "").strip())
    if not m:
        return None, None
    return m.group(1), f"https://discord.com/api/webhooks/{m.group(1)}/{m.group(2)}"


def subscribe(body, ip):
    wid, url = normalize_webhook(body.get("url"))
    if not url:
        return 400, "That doesn't look like a Discord webhook URL. It should start with https://discord.com/api/webhooks/"
    prefs, err = read_prefs(body)
    if err:
        return 400, err
    events, editions = prefs

    with lock:
        subs = load_json(SUBS_JSON, [])
        existing = next((s for s in subs if s["id"] == wid), None)
        if not existing:
            if not ip_allowed(ip):
                return 429, "Too many new sign-ups from your network today. Try again tomorrow."
            if len(subs) >= MAX_SUBS:
                return 503, "Alerts are full right now. Please try again later."

    try:
        post_webhook(url, {"embeds": [{
            "color": COLORS["test"],
            "title": "Cape Tracker alerts are set up" if not existing else "Cape Tracker alerts updated",
            "description": "This channel will get " + describe_prefs(events, editions) +
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
            ip_record(ip)
    log(f"{'updated' if existing else 'new'} webhook …{wid[-4:]} ({len(subs)} total)")
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
    log(f"unsubscribed webhook …{wid[-4:]}")
    return 200, "Unsubscribed. You won't get any more alerts."


def email_subscribe(body, ip):
    if not EMAIL_ENABLED:
        return 503, "Email alerts aren't set up on this site yet."
    email = (body.get("email") or "").strip()
    if len(email) > 254 or not EMAIL_RE.match(email):
        return 400, "That doesn't look like an email address."
    prefs, err = read_prefs(body)
    if err:
        return 400, err
    events, editions = prefs
    key = email.lower()
    now = now_utc()

    with lock:
        subs = load_json(EMAILS_JSON, [])
        sub = next((s for s in subs if s["key"] == key), None)
        if sub and sub.get("pending") and now - dt.datetime.fromisoformat(sub["pending"]["sent"]) < CONFIRM_RESEND:
            return 429, "We just sent you a confirmation email. Check your inbox (and spam folder)."
        if not sub:
            if not ip_allowed(ip):
                return 429, "Too many new sign-ups from your network today. Try again tomorrow."
            if len(subs) >= MAX_EMAILS:
                return 503, "Email alerts are full right now. Please try again later."
            sub = {"key": key, "email": email, "confirmed": False, "events": events, "editions": editions,
                   "unsub": secrets.token_urlsafe(24), "created": now.isoformat(timespec="seconds")}
            subs.append(sub)
            ip_record(ip)
        token = secrets.token_urlsafe(24)
        sub["pending"] = {"hash": sha(token), "events": events, "editions": editions,
                          "sent": now.isoformat(timespec="seconds"),
                          "expires": (now + CONFIRM_EXPIRES).isoformat(timespec="seconds")}
        save_json(EMAILS_JSON, subs)
        changing = sub["confirmed"]

    link = f"{SITE_URL}/api/email/confirm?t={token}"
    what = describe_prefs(events, editions)
    action = "update your cape alerts" if changing else "start getting cape alerts"
    text = (f"Click this link to {action}:\n{link}\n\nYou'll get {what}\n\n"
            f"The link works for 48 hours. If you didn't ask for this, ignore this email and nothing will be sent.\n")
    footer = "If you didn't ask for this, ignore this email and nothing will be sent."
    blocks = [("Confirm your email" if not changing else "Confirm your new choices", None,
               [f"You'll get {what}", "The link works for 48 hours."])]
    html_body = email_html("Minecraft Cape Tracker", blocks, footer,
                           button=("Save my choices" if changing else "Confirm", link))
    try:
        send_emails([build_email(email, "Confirm your Minecraft cape alerts", text, html_body)])
    except Exception as e:  # noqa: BLE001
        log(f"email: confirmation failed: {e}")
        return 502, "Couldn't send the confirmation email. Please try again later."
    log("email: confirmation sent" + (" (preference change)" if changing else ""))
    return 200, "Check your inbox and click the link to confirm. It may take a minute or land in spam."


def email_confirm(token):
    if not token:
        return 400, "That link is missing its code."
    h, now = sha(token), now_utc().isoformat()
    with lock:
        subs = load_json(EMAILS_JSON, [])
        sub = next((s for s in subs if s.get("pending", {}).get("hash") == h), None)
        if not sub or sub["pending"]["expires"] < now:
            return 404, "That link has expired or was already used. Sign up again on the site to get a new one."
        p = sub.pop("pending")
        sub.update(events=p["events"], editions=p["editions"], confirmed=True)
        save_json(EMAILS_JSON, subs)
    log(f"email: confirmed ({len([s for s in subs if s.get('confirmed')])} confirmed total)")
    return 200, "You're subscribed. You'll get " + describe_prefs(p["events"], p["editions"])


def email_unsubscribe(token):
    with lock:
        subs = load_json(EMAILS_JSON, [])
        keep = [s for s in subs if s["unsub"] != token]
        if not token or len(keep) == len(subs):
            return 200, "You're unsubscribed. You won't get any more cape alerts."
        save_json(EMAILS_JSON, keep)
    log("email: unsubscribed")
    return 200, "You're unsubscribed. You won't get any more cape alerts."


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{title} · Minecraft Cape Tracker</title>
<style>
:root{{--bg:#f6f5f1;--surface:#fff;--text:#1d1f1c;--muted:#62665f;--border:#dedcd3;--accent:#3f7d2a;--ink:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141614;--surface:#1c1f1c;--text:#e9ebe6;--muted:#9ea39a;--border:#2f342e;--accent:#7cc35a;--ink:#10220a}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
main{{max-width:480px;margin:12vh auto 0;padding:0 16px}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px}}
h1{{margin:0 0 8px;font-size:1.3rem}} p{{margin:0 0 12px}} a{{color:var(--accent)}}
button{{font:inherit;font-weight:600;cursor:pointer;background:var(--accent);color:var(--ink);border:0;border-radius:999px;padding:9px 20px}}
</style></head><body><main><div class="card"><h1>{title}</h1><p>{message}</p>{extra}
<p><a href="{home}">Back to the Cape Tracker</a></p></div></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "CapeNotifier"

    def log_message(self, *args):
        pass

    def send(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()
        self.wfile.write(data)

    def reply(self, code, message, **extra):
        self.send(code, json.dumps({"ok": code == 200, "message": message, **extra}).encode(), "application/json")

    def page(self, code, title, message, extra=""):
        body = PAGE.format(title=html.escape(title), message=html.escape(message), extra=extra,
                           home=html.escape(SITE_URL or "/"))
        self.send(code, body.encode(), "text/html; charset=utf-8")

    def route(self):
        u = urllib.parse.urlparse(self.path)
        return u.path, urllib.parse.parse_qs(u.query).get("t", [""])[0]

    def do_POST(self):  # noqa: N802
        path, token = self.route()
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            return self.reply(413, "Request too large.")
        raw = self.rfile.read(length)

        if path == "/api/email/unsubscribe":  # button on our page, or a mail client's one-click unsubscribe
            code, msg = email_unsubscribe(token)
            return self.page(code, "Unsubscribed", msg)

        handlers = {"/api/subscribe": lambda b, ip: subscribe(b, ip),
                    "/api/unsubscribe": lambda b, ip: unsubscribe(b),
                    "/api/email/subscribe": lambda b, ip: email_subscribe(b, ip)}
        if path not in handlers:
            return self.reply(404, "Not found.")
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.reply(400, "Bad request.")
        ip = self.headers.get("X-Real-IP") or self.client_address[0]
        try:
            code, msg = handlers[path](body, ip)
        except Exception as e:  # noqa: BLE001
            log(f"api error: {e}")
            code, msg = 500, "Something went wrong. Please try again."
        self.reply(code, msg)

    def do_GET(self):  # noqa: N802
        path, token = self.route()
        if path == "/api/health":
            return self.reply(200, "ok")
        if path == "/api/config":
            return self.reply(200, "ok", email=EMAIL_ENABLED)
        if path == "/api/email/confirm":
            code, msg = email_confirm(token)
            return self.page(code, "Email confirmed" if code == 200 else "Link not valid", msg)
        if path == "/api/email/unsubscribe":
            # Link scanners open links in emails, so unsubscribing takes a button press (or a one-click POST).
            form = (f'<form method="post" action="/api/email/unsubscribe?t={urllib.parse.quote(token)}">'
                    f'<p><button type="submit">Unsubscribe</button></p></form>')
            return self.page(200, "Unsubscribe from cape alerts?", "You'll stop getting all cape alert emails.", form)
        self.reply(404, "Not found.")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    threading.Thread(target=check_loop, daemon=True).start()
    log(f"listening on :{PORT}, checking every {CHECK_EVERY // 60} min, email {'on' if EMAIL_ENABLED else 'off'}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
