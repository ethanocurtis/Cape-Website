# Daily cape research: instructions for the scheduled routine

You maintain `site/data/capes.json` for the Minecraft Cape Tracker. The Raspberry Pi pulls `main` every morning, so anything you push to `main` goes live that day.

## 0. Check push access first

Run `git push --dry-run origin HEAD:main`. If it fails with an auth or permission error, keep going with the research, but at the end report that push access is missing. Say that the fix is to edit this routine at claude.ai/code → Routines and select the `ethanocurtis/Cape-Website` repository.

## 1. Research

Today's date is in your environment. Check for **new capes** and **changes to tracked capes** (dates, requirements, code supply, extensions, new or closed venue cities):

1. Minecraft.net news. The RSS feed `https://www.minecraft.net/en-us/feeds/community-content/rss` is UTF-16, and minecraft.net needs a browser User-Agent. Read any article from the last ~3 weeks that mentions capes, promos, Live, events, challenges or Twitch/TikTok.
2. The article bodies on minecraft.net are server-rendered HTML. Fetch them with curl and a browser User-Agent, strip the tags, and search the text. Also check `https://www.minecraft.net/en-us/redeem` (deadline text) and the Minecraft Help Center (`help.minecraft.net`) promotion terms.
3. Minecraft Wiki via the API, with exactly this User-Agent: `MinecraftCapeTracker/1.0 (https://github.com/ethanocurtis/Cape-Website)`. Never put an email address or other personal info in a User-Agent. Example request: `https://minecraft.wiki/api.php?action=parse&page=Cape&prop=wikitext&format=json&formatversion=2`. Use the "Cape release history" table and each cape's page (texture id in the infobox, distribution, history).
4. `https://www.minecraftexperience.com/` and city pages for Minecraft Experience openings and closings.
5. Run `python3 updater/update.py` and read `site/data/status.json`. `untrackedCapes` lists capes on the wiki that aren't covered yet.

**Source priority:** Minecraft.net > Help Center > official @Minecraft social posts > minecraftexperience.com/ticket sites > Minecraft Wiki > news sites. If times conflict, use Minecraft.net and don't mention the discrepancy on the site.

## 2. Edit `site/data/capes.json`

- Follow the existing schema (see README "Editing `capes.json`").
- **Times:**
  - Exact instants go in ISO UTC (`2026-10-15T06:59:00Z`). Convert from PT/UTC carefully, and mind daylight saving time. The page displays them in US Central.
  - Day-only deadlines use `"dateOnly": true`. Add `"exclusiveEnd": true` for "before <date>".
  - Venue dates in `locations` are local calendar dates.
- **New cape that can be earned now, or will be soon:**
  - Add a full entry: `names` for both editions, `editions`, `category`, `texture` (wiki infobox `texture-id`), `wiki`, `appearance`, `windows` and/or `locations`, numbered `steps`, `editionNotes`, `alerts`, `notes`, `links` (streams, Twitch drops inventory, TikTok, redeem page, tickets), and `sources`.
  - Every source key must exist in the top-level `sources` map. Add new ones with title, publisher, date and url.
  - Add the cape's wiki name to `reviewedWikiCapes`.
- **New cape that is already closed, or was closed more than 2 months ago:** just add its wiki name to `reviewedWikiCapes`. Add a short entry only if it closed less than 2 months ago.
- **Existing capes:** update dates, alerts (e.g. "Twitch codes ran out") and city lists when sources change. Remove alerts that are no longer true.
- Don't delete old entries. The page hides capes 2 months after they close.
- Keep the writing plain, short and neutral. Don't invent details. If a requirement or date is unannounced, say "TBA" in a note rather than guessing.

## 3. Validate

```bash
python3 -c "import json; d=json.load(open('site/data/capes.json')); assert all(s in d['sources'] for c in d['capes'] for s in c.get('sources', [])); print('ok', len(d['capes']))"
node -e "require('vm').createScript(require('fs').readFileSync('site/assets/app.js','utf8'))"
python3 updater/update.py   # untrackedCapes should be empty afterwards
```

Optionally serve `site/` (`python3 -m http.server --directory site`) and load the page in headless Chromium (Playwright) to confirm it renders without console errors.

## 4. Publish

- Only touch `site/data/capes.json`, unless a real bug blocks the data from displaying. In that case, make the smallest possible fix.
- Never commit `site/data/status.json` or `site/textures/*.png`. They are gitignored.
- If nothing changed, don't commit.
- Otherwise commit to `main` with a message like `Daily cape update: add X Cape; extend Y deadline` and run `git push origin HEAD:main`. If the push is rejected, run `git pull --rebase origin main` and push again.
- Finish with a 2–4 line summary of what changed and the sources used.
