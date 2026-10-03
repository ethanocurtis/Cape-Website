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
| `notifier` | `python:3.12-alpine`. Serves the alert sign-up API at `/api/` (through `web`) and sends Discord and email alerts. See [Alerts](#alerts-discord-and-email). |

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

1. **Research (cloud, daily ~3:47 AM Central):** a scheduled Claude routine checks Minecraft.net, the Minecraft Help Center, minecraftexperience.com and the Minecraft Wiki for new capes and changed dates. It updates `site/data/capes.json`, removes capes that closed more than 2 months ago, and pushes the changes straight to `main`. Your computer does not need to be on.
2. **Deploy (Pi, daily at `UPDATE_TIME`, default 5:00 AM Central):** the updater container fast-forwards the checkout to GitHub `main` over HTTPS. Because the repo is public, no keys are needed. Changes to `site/` go live immediately because nginx serves the folder directly. If `updater/update.py` itself changed, the updater restarts with the new version.
3. **Checks (Pi, same run):** it reads Minecraft.net news and the wiki's cape list. A cape that `capes.json` doesn't cover yet gets a **"New cape spotted"** banner until the next research run adds it.

Notes:
- The Pi only fast-forwards. If you edit files on the Pi and commit them there, the pull is skipped and the error appears in `docker compose logs updater`. Make edits on GitHub instead, or run `git reset --hard origin/main` on the Pi.
- Changes to `docker-compose.yml` or `docker/` need a manual `docker compose up -d --build` on the Pi.
- Set `GIT_PULL=0` in `.env` to turn off automatic pulls.

## Alerts (Discord and email)

Visitors click **Get alerts**, then either paste a Discord channel webhook URL or enter an email address, and choose what they want:

| Alert | When it's sent |
|-------|----------------|
| New cape | A cape that can be earned now or soon is added to `capes.json`. |
| Cape opens | An earn window starts, or a Minecraft Experience city opens. |
| Ending soon | About 24 hours before a cape can no longer be earned, or before a code-redemption deadline. |
| Dates changed | A date moves, a venue city is added, or a new warning (e.g. "codes ran out") is posted. |

They can also pick Java, Bedrock or both.

- **Discord:** on subscribe, the notifier posts a test message, so a wrong URL fails right away. Submitting the same URL again changes the options, and **Unsubscribe** removes it. If someone deletes the webhook in Discord, it is removed automatically on the next alert.
- **Email:** double opt-in. Signing up sends a confirmation link (valid 48 hours), and nothing else is sent until it's clicked. Signing up again with new choices sends a new link that saves them. Every alert email has an unsubscribe link and one-click unsubscribe headers (`List-Unsubscribe`), which Gmail and Yahoo require. Alerts from the same check are combined into one email. Times are shown in US Central.

- The notifier re-reads `capes.json` every 5 minutes, so alerts go out shortly after the Pi pulls the daily update. On its first start it only records the current state and sends nothing.
- Discord timestamps show in each reader's own time zone.
- Set `SITE_URL` in `.env` to your public address so alerts link to the cape on the site.
- Subscriptions are stored in the `notifier-data` Docker volume, not in the repo or under `site/`, because webhook URLs let anyone post to that channel and email addresses are personal data.
- Limits: 10 API requests a minute per visitor (nginx), 5 new sign-ups a day per IP, one confirmation email per address every 10 minutes, 2,000 webhooks (`MAX_SUBSCRIPTIONS`) and 5,000 email subscribers (`MAX_EMAIL_SUBSCRIBERS`).
- Logs: `docker compose logs -f notifier`. Changes to `notifier/notifier.py` pulled from GitHub are picked up automatically within 5 minutes.

### Setting up email

Email is off until `SMTP_HOST`, `SMTP_FROM` and `SITE_URL` are set in `.env`; until then the form only offers Discord. A home internet connection can't deliver email reliably on its own, so use an SMTP relay:

| Provider | `SMTP_HOST` | Port / `SMTP_SECURITY` | Notes |
|----------|-------------|------------------------|-------|
| Brevo | `smtp-relay.brevo.com` | 587 / `starttls` | Free tier: 300 emails a day. |
| Mailgun | `smtp.mailgun.org` | 587 / `starttls` | |
| Amazon SES | `email-smtp.<region>.amazonaws.com` | 587 / `starttls` | Cheapest at volume. |
| Gmail / Google Workspace | `smtp.gmail.com` | 587 / `starttls` | Needs an app password. About 500 emails a day. |

Then:

1. Verify your sending domain with the provider and add the SPF, DKIM and DMARC DNS records it gives you. Without them, alerts go to spam.
2. Fill in the `SMTP_*` lines in `.env`, e.g. `SMTP_FROM=Cape Tracker <alerts@yourdomain.com>`.
3. Run `docker compose up -d`. The notifier log says `email on` when it's ready.

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
