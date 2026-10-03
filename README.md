# Minecraft Cape Tracker

A clean, single-page site that lists every Minecraft cape you can **still earn**.

- **Java first, then Bedrock:** tabs for each edition, with each cape's edition-specific name and how to equip it.
- **Event deep-dives:** step-by-step requirements, key dates, live countdowns, open and upcoming venue cities, and links to streams, tickets and code redemption.
- **All times in US Central:** shown as CST or CDT, whichever is in effect.
- **Auto-filtering:** a cape moves to *Recently closed* when it ends. It disappears 2 months after closing. Status is calculated from the dates in the data each time the page loads, so it is always current.
- **Sources:** every cape card has numbered citations to a source list. Minecraft.net wins when sources give different times.
- **Daily refresh:** an updater container runs once a day. It checks Minecraft.net news for cape mentions, flags new capes listed on the Minecraft Wiki, caches cape textures locally, and can `git pull` updated cape data.

## Run it on a Raspberry Pi (Docker Compose)

Works on any 64-bit or 32-bit Raspberry Pi OS with Docker installed. Both images are multi-arch.

```bash
# 1. Install Docker (skip if you already have it)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out and back in afterwards

# 2. Get the code
git clone https://github.com/ethanocurtis/cape-website.git
cd cape-website
cp .env.example .env            # edit the port, time zone or update time if you like

# 3. Start it
docker compose up -d --build
```

The site is now at `http://<pi-ip>:8080`.

| Service   | What it does |
|-----------|--------------|
| `web`     | `nginx:alpine`. Serves `./site` read-only on `WEB_PORT` (default 8080). |
| `updater` | `python:3.12-alpine`. Runs `updater/update.py` at startup, then every day at `UPDATE_TIME` (default 05:00 America/Chicago). |

Useful commands:

```bash
docker compose logs -f updater                 # watch the daily refresh
docker compose exec updater python3 /repo/updater/update.py   # refresh right now
docker compose pull && docker compose up -d --build           # update the images
```

### Nginx Proxy Manager

The Pi only needs to expose plain HTTP on your LAN. NPM handles the public domain and SSL.

1. In NPM, go to **Hosts → Proxy Hosts → Add Proxy Host**.
2. **Domain Names:** e.g. `capes.yourdomain.com`
3. **Scheme:** `http` · **Forward Hostname / IP:** your Pi's LAN IP (e.g. `192.168.1.50`) · **Forward Port:** `8080` (or your `WEB_PORT`)
4. Turn on **Block Common Exploits**. Websockets are not needed.
5. On the **SSL** tab, request a Let's Encrypt certificate and turn on **Force SSL** and **HTTP/2**.

Give the Pi a DHCP reservation so the forward IP doesn't change. If NPM's VM and the Pi are on different VLANs, allow TCP 8080 from the NPM VM to the Pi.

### Keeping the cape data current

`site/data/capes.json` is the hand-checked list of capes, dates and steps. To have the Pi pick up edits pushed to GitHub automatically, set `GIT_PULL=1` in `.env`. The updater then runs `git pull --ff-only` before each daily refresh.

- **Public repo:** works as-is.
- **Private repo:** the container has no GitHub credentials. Leave `GIT_PULL=0` and pull from the Pi itself with a cron job (`crontab -e`): `55 4 * * * cd ~/cape-website && git pull --ff-only`. Your normal SSH key or a read-only [deploy key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys) works for this.

When the updater spots a cape that `capes.json` doesn't cover yet, the site shows a **"New cape spotted"** banner that links to the wiki. Add the cape to `capes` (or to `reviewedWikiCapes` if it shouldn't be listed) and the banner clears on the next run.

## Editing `capes.json`

Each cape has:

- `names` and `editions`: the per-edition display name, and which edition tabs it appears on.
- `texture`: the texture hash from `textures.minecraft.net`. The page draws the cape's front face from it.
- `windows`:
  - `role: "earn"` sets when the cape can be earned. ISO UTC timestamps, or `dateOnly` dates read as US Central calendar days.
  - `role: "redeem"` sets the code redemption deadline.
- `locations`: for in-person events. Dates are local to the venue. Use `tba` for cities without dates yet.
- `always: true`: for capes with no end date, such as the Pan Cape.
- `steps`, `alerts`, `notes`, `editionNotes`, `links` (`kind`: `stream` / `redeem` / `tickets` / `info`).
- `sources`: keys into the top-level `sources` map. The page numbers them automatically.

## Local preview without Docker

```bash
python3 updater/update.py                     # optional: fetch news and textures
python3 -m http.server 8080 --directory site
```

## Data sources

[Minecraft.net](https://www.minecraft.net) (preferred), the [Minecraft Help Center](https://help.minecraft.net), [minecraftexperience.com](https://www.minecraftexperience.com), and the [Minecraft Wiki](https://minecraft.wiki/w/Cape). Fan-made; not affiliated with Mojang Studios or Microsoft.
