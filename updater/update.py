#!/usr/bin/env python3
"""Daily refresh for the Minecraft Cape Tracker.

Standard library only. Each run:
  * reads the Minecraft.net news RSS feed and flags articles that mention capes,
  * reads the Minecraft Wiki "Cape" release table and reports capes that
    site/data/capes.json does not track yet,
  * caches every tracked cape texture from textures.minecraft.net,
  * writes site/data/status.json, which the page uses for "last checked",
    the news list and new-cape banners.

Whether a cape is open, upcoming or closed is worked out in the browser from
the dates in capes.json, so the page stays correct between runs.

Usage:
  python3 update.py            # run once
  python3 update.py --loop     # run now, then every day at UPDATE_TIME (default 05:00) in TZ
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.environ.get("SITE_DIR", os.path.join(ROOT, "site"))
CAPES_JSON = os.path.join(SITE, "data", "capes.json")
STATUS_JSON = os.path.join(SITE, "data", "status.json")
TEXTURE_DIR = os.path.join(SITE, "textures")

# The wiki asks bots to identify themselves; minecraft.net only answers browser-like agents.
BOT_UA = "MinecraftCapeTracker/1.0 (https://github.com/ethanocurtis/cape-website)"
BROWSER_UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
RSS_URL = "https://www.minecraft.net/en-us/feeds/community-content/rss"
WIKI_API = "https://minecraft.wiki/api.php"
TEXTURE_URL = "https://textures.minecraft.net/texture/"
CAPE_WORDS = re.compile(r"\bcapes?\b", re.I)


def log(msg):
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def fetch(url, timeout=30):
    ua = BROWSER_UA if urllib.parse.urlparse(url).hostname.endswith("minecraft.net") else BOT_UA
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def wiki_wikitext(page):
    q = urllib.parse.urlencode({
        "action": "parse", "page": page, "prop": "wikitext",
        "format": "json", "formatversion": "2", "redirects": "1",
    })
    data = json.loads(fetch(f"{WIKI_API}?{q}"))
    return data["parse"]["wikitext"]


def atomic_write(path, data):
    tmp = path + ".tmp"
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(tmp, mode) as f:
        f.write(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- news

def check_news(errors):
    try:
        root = ET.fromstring(fetch(RSS_URL))
    except Exception as e:  # noqa: BLE001
        errors.append(f"news feed: {e}")
        return []
    items = []
    for it in root.iter("item"):
        link = it.find("{http://www.w3.org/2005/Atom}link")
        url = link.get("href") if link is not None else (it.findtext("link") or "")
        title = html.unescape((it.findtext("title") or "").strip())
        try:
            date = parsedate_to_datetime(it.findtext("pubDate")).astimezone(dt.timezone.utc).isoformat()
        except Exception:  # noqa: BLE001
            date = None
        mentions = bool(CAPE_WORDS.search(title))
        if not mentions and url:
            try:
                body = fetch(url).decode("utf-8", "replace")
                body = re.sub(r"<(script|style|nav|header|footer)\b.*?</\1>", " ", body, flags=re.S | re.I)
                mentions = bool(CAPE_WORDS.search(re.sub(r"<[^>]+>", " ", body)))
            except Exception as e:  # noqa: BLE001
                errors.append(f"article {url}: {e}")
        items.append({"title": title, "url": url, "date": date, "mentionsCape": mentions})
    log(f"news: {len(items)} articles, {sum(i['mentionsCape'] for i in items)} mention capes")
    return items


# ---------------------------------------------------------------- wiki

def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("the ", "").replace("cape", ""))


def check_wiki(catalog, errors):
    """Return capes from the wiki's release table that capes.json doesn't track."""
    try:
        text = wiki_wikitext("Cape")
    except Exception as e:  # noqa: BLE001
        errors.append(f"wiki Cape page: {e}")
        return [], None

    table = text.split("=== Cape release history ===", 1)[-1].split("|}", 1)[0]
    rows = []
    for row in table.split("\n|-")[1:]:
        m = re.search(r"\{\{PersonaLink\|([^}]*)\}\}", row)
        if not m:
            continue
        parts = [p for p in m.group(1).split("|") if p]
        name = parts[-1] if len(parts) > 1 else parts[0]
        link = next((p[5:] for p in parts if p.startswith("link=")), name)
        rows.append({"name": name.strip(), "page": link.strip()})

    capes = catalog["capes"]
    tracked = {norm(n) for n in catalog.get("reviewedWikiCapes", [])}
    for c in capes:
        for n in c["names"].values():
            tracked.add(norm(n))
        if c.get("wiki"):
            tracked.add(norm(c["wiki"].replace("_", " ")))

    new = [
        {"name": r["name"], "url": "https://minecraft.wiki/w/" + urllib.parse.quote(r["page"].replace(" ", "_"))}
        for r in rows
        if norm(r["name"]) not in tracked
    ]
    log(f"wiki: {len(rows)} capes in release table, {len(new)} not reviewed yet")
    return new, len(rows)


# ---------------------------------------------------------------- textures

def cache_textures(capes, errors):
    os.makedirs(TEXTURE_DIR, exist_ok=True)
    got = 0
    for c in capes:
        tid = c.get("texture")
        if not tid or not re.fullmatch(r"[0-9a-f]{20,80}", tid):
            continue
        path = os.path.join(TEXTURE_DIR, tid + ".png")
        if os.path.exists(path):
            continue
        try:
            png = fetch(TEXTURE_URL + tid)
            if png[:8] != b"\x89PNG\r\n\x1a\n":
                raise ValueError("not a PNG")
            atomic_write(path, png)
            got += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"texture {c['id']}: {e}")
    log(f"textures: downloaded {got} new")


# ---------------------------------------------------------------- main

def git_pull(errors):
    """Pull curated data updates (capes.json) when GIT_PULL=1 and the repo is mounted."""
    if os.environ.get("GIT_PULL", "0").lower() not in ("1", "true", "yes"):
        return
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-c", f"safe.directory={ROOT}", "-C", ROOT, "pull", "--ff-only"],
            capture_output=True, text=True, timeout=120,
        )
        log("git pull: " + (out.stdout.strip() or out.stderr.strip()))
        if out.returncode != 0:
            errors.append("git pull failed: " + out.stderr.strip()[:300])
    except Exception as e:  # noqa: BLE001
        errors.append(f"git pull: {e}")


def run_once():
    errors = []
    git_pull(errors)
    with open(CAPES_JSON) as f:
        catalog = json.load(f)
    capes = catalog["capes"]

    try:
        with open(STATUS_JSON) as f:
            previous = json.load(f)
    except (OSError, ValueError):
        previous = {}

    n_err = len(errors)
    news = check_news(errors)
    if len(errors) > n_err and not news:
        news = previous.get("news", [])  # feed unreachable: keep the last good list
    n_err = len(errors)
    untracked, wiki_count = check_wiki(catalog, errors)
    if len(errors) > n_err and wiki_count is None:
        untracked, wiki_count = previous.get("untrackedCapes", []), previous.get("wikiCapeCount")
    cache_textures(capes, errors)

    recent_cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=21)
    cape_news = [
        n for n in news
        if n["mentionsCape"] and n["date"] and dt.datetime.fromisoformat(n["date"]) >= recent_cutoff
    ]

    status = {
        "lastRun": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "news": news,
        "capeNews": cape_news,
        "untrackedCapes": untracked,
        "wikiCapeCount": wiki_count,
        "errors": errors,
    }
    atomic_write(STATUS_JSON, json.dumps(status, indent=2))
    for e in errors:
        log(f"warning: {e}")
    log(f"wrote {STATUS_JSON}")
    return not errors


def seconds_until(hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    now = dt.datetime.now()  # container local time (TZ env)
    nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    return (nxt - now).total_seconds()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="run now, then daily at UPDATE_TIME")
    args = ap.parse_args()

    if not args.loop:
        sys.exit(0 if run_once() else 1)

    at = os.environ.get("UPDATE_TIME", "05:00")
    while True:
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            log(f"run failed: {e}")
        wait = seconds_until(at)
        log(f"next run at {at} ({wait / 3600:.1f} h)")
        time.sleep(wait)


if __name__ == "__main__":
    main()
