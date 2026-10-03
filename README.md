# Minecraft Cape Tracker

A clean, single-page site that lists every Minecraft cape you can **still earn**.

- **Java first, then Bedrock:** tabs for each edition, with each cape's edition-specific name and how to equip it.
- **Event deep-dives:** step-by-step requirements, key dates, live countdowns, open and upcoming venue cities, and links to streams, tickets and code redemption.
- **All times in US Central:** shown as CST or CDT, whichever is in effect.
- **Auto-filtering:** a cape moves to *Recently closed* when it ends. It disappears 2 months after closing. Status is calculated from the dates in the data each time the page loads, so it is always current.
- **Sources:** every cape card has numbered citations to a source list. Minecraft.net wins when sources give different times.
- **Daily refresh:** a cloud routine researches capes and pushes updates to GitHub every day. The Pi pulls them automatically, checks Minecraft.net news for cape mentions, flags new capes listed on the Minecraft Wiki, and caches cape textures locally.

## Run it on a Raspberry Pi (Docker Compose)

Works on any 64-bit or 32-bit Raspberry Pi OS with Docker installed. Both images are multi-arch.

```bash
# 1. Install Docker (skip if you already have it)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out and back in afterwards

# 2. Get the code
git clone https://github.com/ethanocurtis/Cape-Website.git
cd Cape-Website
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

### How it stays up to date

1. **Research (cloud, daily ~3:47 AM Central):** a scheduled Claude routine checks Minecraft.net, the Minecraft Help Center, minecraftexperience.com and the Minecraft Wiki for new capes and changed dates. It updates `site/data/capes.json` and pushes the changes straight to `main`. Your computer does not need to be on.
2. **Deploy (Pi, daily at `UPDATE_TIME`, default 5:00 AM Central):** the updater container fast-forwards the checkout to GitHub `main` over HTTPS. Because the repo is public, no keys are needed. Changes to `site/` go live immediately because nginx serves the folder directly. If `updater/update.py` itself changed, the updater restarts with the new version.
3. **Checks (Pi, same run):** it reads Minecraft.net news and the wiki's cape list. A cape that `capes.json` doesn't cover yet gets a **"New cape spotted"** banner until the next research run adds it.

Notes:
- The Pi only fast-forwards. If you edit files on the Pi and commit them there, the pull is skipped and the error appears in `docker compose logs updater`. Make edits on GitHub instead, or run `git reset --hard origin/main` on the Pi.
- Changes to `docker-compose.yml` or `docker/` need a manual `docker compose up -d --build` on the Pi.
- Set `GIT_PULL=0` in `.env` to turn off automatic pulls.

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
