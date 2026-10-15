<p align="center">
  <img src="https://raw.githubusercontent.com/mdsaif572/FileToLinksssss/main/Thunder/template/logo.png" alt="Cloud Station Logo" width="120" onerror="this.src='https://cdn.jsdelivr.net/gh/fyaz05/Resources@main/FileToLink/Thunder.jpg'">
  <h1 align="center">☁️ Cloud Station File-to-Link</h1>
</p>

<p align="center">
  <b>High-Performance Telegram File-to-Link Bot for High-Speed Direct Downloads & Media Streaming</b>
</p>

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.13%2B-blue?style=for-the-badge&logo=python" alt="Python Version"></a>
  <a href="https://github.com/rjriajul/wzgram"><img src="https://img.shields.io/badge/WZGram-3.1.5-blue?style=for-the-badge" alt="WZGram"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-green?style=for-the-badge" alt="License"></a>
  <a href="https://github.com/mdsaif572/FileToLinksssss"><img src="https://img.shields.io/badge/Maintained-Yes-success?style=for-the-badge" alt="Maintenance"></a>
</p>

<hr>

## 📑 Table of Contents

- [About The Project](#about-the-project)
- [Key Features](#key-features)
- [How It Works](#how-it-works)
- [Configuration Guide](#configuration-guide)
  - [Essential Configuration](#essential-configuration)
  - [Multiple Storage Channels](#multiple-storage-channels)
  - [Multi-Client Pool Configuration](#multi-client-pool-configuration)
  - [Optional Configuration Variables](#optional-configuration-variables)
- [Usage & Commands Reference](#usage--commands-reference)
  - [User Commands](#user-commands)
  - [Admin / Owner Commands](#admin--owner-commands)
  - [BotFather Menu Setup](#botfather-menu-setup)
- [Streaming Architecture & Optimization](#streaming-architecture--optimization)
  - [Zero H15 Idle Timeouts & Chunk Pre-fetching](#zero-h15-idle-timeouts--chunk-pre-fetching)
  - [Auto-Sync on Dyno Restart](#auto-sync-on-dyno-restart)
  - [Fair Per-Caller Rate Limiting](#fair-per-caller-rate-limiting)
- [Web Admin Dashboard](#web-admin-dashboard)
- [Deployment Guide](#deployment-guide)
  - [Heroku Quick Deploy](#heroku-quick-deploy)
  - [Docker / VPS Deployment](#docker--vps-deployment)
- [Troubleshooting & FAQ](#troubleshooting--faq)
- [License](#license)

<hr>

## About The Project

**Cloud Station** is an enterprise-grade Telegram bot designed to convert files, documents, and media of any size into fast, direct HTTP(S) links. It allows users to stream videos directly in any browser or media player and download files at maximum line speed without opening Telegram.

Built with Python 3.13, **WZGram 3.1.5**, `aiohttp`, and MongoDB, Cloud Station includes multi-client load balancing, multiple storage channels, failure alerting, fair rate limiting, and an auto-healing upstream sync engine.

---

## Key Features

- ⚡ **Direct Link Streaming & Downloading**: Stream videos in-browser or download with IDM/Aria2/Wget at full connection speed.
- 🎨 **Vibrant Button Styles**: Interactive Telegram menus styled with native colors (Blue Primary, Green Success, Red Danger).
- 📦 **Multiple Storage Channels (`BIN_CHANNEL_2`, `BIN_CHANNEL_3`, ...)**:
  - Bypasses Telegram's 50-administrator limit per channel.
  - Automatically load-balances files across channels using deterministic content hashes.
- 🤖 **Multi-Client Pool (`MULTI_TOKEN1`, `MULTI_TOKEN2`, ...)**:
  - Distributes streaming loads across dozens of bot accounts.
  - Automatic DC-matching (prefers a client in the file's Telegram Data Center to minimize TTFB).
- 🔗 **Direct URL Uploads (`URL_UPLOAD_ENABLED`)**: Send any direct HTTP(S) URL in private chat; Telegram downloads it directly into your storage channel and returns a link.
- 🔒 **Link Revocation & Expiration (`LINK_TTL_HOURS`)**:
  - Set links to auto-expire after $N$ hours.
  - Instantly revoke or restore access with `/revoke <hash>` and `/unrevoke <hash>`.
- 📊 **Streaming Metrics & Owner Failure Alerts**:
  - `/status` HTTP endpoint with live Prometheus-compatible runtime metrics.
  - Automatic Telegram DM notifications to `OWNER_ID` when errors exceed thresholds.
- 🔄 **Zero-Downtime Dyno Auto-Sync**: Automatically syncs code on dyno boot via GitHub Archive (`.tar.gz`) even when `git` binary is unavailable on container/PaaS environments.
- 🛡️ **Per-Caller Rate Limiting (`MAX_CONCURRENT_PER_USER`)**: Caps simultaneous streams per client IP using reverse-proxy forwarded IP headers, ensuring single users cannot monopolize pool capacity.
- 📱 **Maximalist Responsive Watch & Download Page**: Modern glassmorphic web player supporting range requests, scrubbing, and picture-in-picture.

---

## How It Works

```
User sends media / URL
          │
          ▼
   Primary Bot / Pool
          │
          ▼
   Stable Hash Channel Router
  ┌───────┴───────┐
  ▼               ▼
BIN_CHANNEL   BIN_CHANNEL_2 ...
  └───────┬───────┘
          ▼
   Direct Link Generated
   (/f/<hash>/<name> & /watch/f/<hash>/<name>)
          │
          ▼
   Streaming Server (aiohttp)
   [DC Client Router + Pre-fetch Engine]
          │
          ▼
   Fast Browser Playback / Multi-threaded Download
```

---

## Configuration Guide

Create `config.env` in the root directory (or configure Environment Variables in your hosting dashboard):

### Essential Configuration

| Variable | Required | Description | Example |
| :--- | :---: | :--- | :--- |
| `API_ID` | **Yes** | Telegram API ID from [my.telegram.org](https://my.telegram.org) | `1234567` |
| `API_HASH` | **Yes** | Telegram API Hash from [my.telegram.org](https://my.telegram.org) | `abc123def456...` |
| `BOT_TOKEN` | **Yes** | Primary bot token from [@BotFather](https://t.me/BotFather) | `123456:ABC-DEF...` |
| `BIN_CHANNEL` | **Yes** | Primary storage channel ID (bot must be admin) | `-1001234567890` |
| `OWNER_ID` | **Yes** | Telegram User ID of the bot owner | `123456789` |
| `DATABASE_URL` | **Yes** | MongoDB connection string (Atlas or self-hosted) | `mongodb+srv://...` |
| `FQDN` | **Yes** | Fully Qualified Domain Name (or platform app domain) | `mybot.herokuapp.com` |
| `HAS_SSL` | No | Set `True` if your domain uses HTTPS (default `True`) | `True` |
| `PORT` | No | Web server port (Heroku sets this automatically) | `8080` |
| `NO_PORT` | No | Hide port in generated URLs (`True` for 80/443) | `True` |

---

### Multiple Storage Channels

Telegram caps channel administrators at 50. If your bot pool has more than ~45 bots, add additional channels:

```env
# Primary Channel (Default)
BIN_CHANNEL=-1001234567890

# Secondary Channels (Add as many as needed)
BIN_CHANNEL_2=-1009876543210
BIN_CHANNEL_3=-1005555555555
```

> **Rules for Multiple Channels:**
> 1. The primary bot (`BOT_TOKEN`) must be an admin in **all** storage channels.
> 2. The first ~45 pool bots should be admins in `BIN_CHANNEL`.
> 3. Additional pool bots (`MULTI_TOKEN*`) should be admins in `BIN_CHANNEL_2`, `BIN_CHANNEL_3`, etc.
> 4. Files are automatically hashed and routed to the proper channel on upload.

---

### Multi-Client Pool Configuration

Add additional bots to multiply download speed and handle concurrent streaming:

```env
MULTI_TOKEN1="bot_token_here"
MULTI_TOKEN2="bot_token_here"
MULTI_TOKEN3="bot_token_here"
# Add as many as needed: MULTI_TOKEN4, MULTI_TOKEN5, ...
```

---

### Optional Configuration Variables

| Variable | Default | Description |
| :--- | :---: | :--- |
| `UPSTREAM_REPO` | `""` | GitHub repository URL for auto-updating on restart. |
| `UPSTREAM_BRANCH` | `"main"` | Branch to pull updates from (`main`). |
| `MAX_CONCURRENT_PER_USER` | `8` | Maximum simultaneous streams per viewer IP address. |
| `LINK_TTL_HOURS` | `0` | Hours before links expire (`0` = never expires). |
| `URL_UPLOAD_ENABLED` | `"True"` | Enable users to upload files by sending direct URLs. |
| `WEB_ADMIN_PASSWORD` | `""` | Password for the `/admin` dashboard. Empty = dashboard disabled. |
| `ALERT_ON_STREAM_FAILURES` | `"True"` | Send Telegram DM to owner when stream errors spike. |
| `STREAM_FAILURE_ALERT_THRESHOLD`| `5` | Error count required to trigger an alert. |
| `STREAM_FAILURE_ALERT_WINDOW_MINUTES`| `5` | Time window (minutes) for error threshold count. |
| `FORCE_CHANNEL_ID` | `""` | Require users to join this channel before using the bot. |
| `TOKEN_ENABLED` | `"False"` | Require access token verification. |
| `SHORTEN_MEDIA_LINKS` | `"False"` | Shorten generated media links using URL shortener. |
| `MAX_BATCH_FILES` | `50` | Maximum files allowed per batch forward. |

---

## Usage & Commands Reference

### User Commands

| Command | Description |
| :--- | :--- |
| `/start` | Start the bot, check welcome message and token activation. |
| `/help` | Show user manual, supported features, and link guides. |
| `/about` | Bot version, engine information, and developer credits. |
| `/ping` | Test bot response time and API latency. |
| `/dc` | Check Telegram Data Center for yourself or reply to a media file. |
| `/mystats` | View your personal uploaded files count, storage used, and downloads served. |
| `/link [count]` | Generate direct links for media (reply to a file or forward multiple files). |

### Admin / Owner Commands

| Command | Description |
| :--- | :--- |
| `/status` | View live multi-client pool health, stream workloads, and memory usage. |
| `/stats` | View global database statistics (total users, links, storage). |
| `/revoke <hash>` | Instantly revoke access to a file link (answers `410 Gone`). |
| `/unrevoke <hash>` | Restore a previously revoked file link. |
| `/broadcast` | Broadcast text, media, or button messages to all bot users. |
| `/ban <user_id>` | Ban a user from using the bot. |
| `/unban <user_id>` | Unban a user. |
| `/users` | Get total registered bot users count. |
| `/speedtest` | Run network speed test on the hosting server. |
| `/log` | Download latest bot execution log file. |
| `/restart` | Restart bot services. |
| `/shell <cmd>` | Execute a terminal command directly from Telegram. |

### BotFather Menu Setup

Copy and paste this list to [@BotFather](https://t.me/BotFather) ➔ `/setcommands`:

```text
start - Start the bot & get help
link - Generate direct link for media
dc - Check file or user data center
ping - Check bot response speed
about - Bot specifications & info
help - How to use the bot
mystats - View your personal usage stats
status - [Admin] View streaming & pool status
stats - [Admin] Global database analytics
revoke - [Admin] Revoke a public link
unrevoke - [Admin] Unrevoke a public link
broadcast - [Admin] Send announcement to all users
ban - [Admin] Ban a user
unban - [Admin] Unban a user
users - [Admin] Total user count
log - [Admin] Get bot runtime logs
restart - [Admin] Restart bot
```

---

## Streaming Architecture & Optimization

### Zero H15 Idle Timeouts & Chunk Pre-fetching
Standard Telegram stream bridges suffer from Heroku `H15 Idle connection` errors when Telegram takes longer than 55 seconds to dispatch the first chunk after HTTP headers are committed. Cloud Station eliminates this:
- The streaming router pre-fetches the initial byte chunk **before** sending `206 Partial Content` headers.
- If upstream experiences transient MTProto packet stalls, `custom_dl.py` automatically retries without dropping the client connection.

### Auto-Sync on Dyno Restart
The `Procfile` is configured to launch via:
```text
web: python update.py && python -m Thunder
```
On boot or restart (`heroku restart`):
1. `update.py` connects to `UPSTREAM_REPO` on branch `UPSTREAM_BRANCH`.
2. Even in containerized environments where `git` is not installed, it fetches the latest commit via the GitHub Archive `.tar.gz` stream.
3. It updates all repository files while strictly preserving your local `config.env`.
4. It boots the latest codebase immediately.

### Fair Per-Caller Rate Limiting
Instead of throttling by user account (which would unfairly penalize viral files viewed by hundreds of separate people), `MAX_CONCURRENT_PER_USER` tracks concurrent connections per **caller IP address** using trusted proxy headers (`X-Forwarded-For`). Single callers cannot consume all pool threads.

---

## Web Admin Dashboard

Set `WEB_ADMIN_PASSWORD` and open `https://<your-app>/admin` for a live view of the instance. Without the password the page stays disabled and every request to it is refused.

**What it shows** (auto-refreshes every 3 seconds):

- **Streams** — active / served / failed, bytes served, message-cache hit rate.
- **Bot pool** — every client with its id, Telegram data centre, per-channel access (`✓` / `✗`) and current load; a client that silently lost channel access is visible here immediately.
- **Active callers** — which IPs hold how many streams, with **one-click ban**. Bans are stored in MongoDB, take effect instantly (no restart), survive a restart, and remain in force even if the database is temporarily unreachable. Blocked requests are refused with `403` before a pool slot is taken.
- **Banned callers** — unban in one click; access returns immediately.
- **Last error** — kind, age and detail of the most recent failure.

Sessions are signed cookies valid for 12 hours (`HttpOnly`, `SameSite=Lax`, `Secure` when `HAS_SSL`); the signing key is regenerated per process, so a restart or a password change logs everyone out. Failed logins are throttled per IP.

`GET /healthz` is a public liveness probe (`status`, `version`, client count, active streams) that exposes no configuration — useful for uptime monitors. To verify the dashboard without Telegram credentials, run `python scripts/dashboard_check.py`.

---

## Deployment Guide

### Heroku Quick Deploy

1. Fork this repository to your GitHub account.
2. In Heroku Dashboard, create a new App.
3. Under **Settings ➔ Config Vars**, add all required keys (`API_ID`, `API_HASH`, `BOT_TOKEN`, `BIN_CHANNEL`, `DATABASE_URL`, `OWNER_ID`, `FQDN`).
4. Set `UPSTREAM_REPO` to your fork URL and `UPSTREAM_BRANCH` to `main`.
5. Under **Deploy**, connect your GitHub repo and deploy the `main` branch.
6. Under **Resources**, ensure the `web` dyno is switched ON.

### Docker / VPS Deployment

```bash
# 1. Clone repository
git clone https://github.com/mdsaif572/FileToLinksssss.git
cd FileToLinksssss

# 2. Setup virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp config_sample.env config.env
nano config.env

# 5. Start with launcher script
bash thunder.sh
```

---

## Troubleshooting & FAQ

#### Q: How do I test if the streaming server is working?
Visit `https://<YOUR_DOMAIN>/status` in your browser. It will return a JSON document showing server uptime, active clients, and streaming metrics.

#### Q: Why do I get "400 CHANNEL_INVALID" on startup?
Ensure your primary bot and all multi-client bots have been added to your storage channel (`BIN_CHANNEL`) as **Administrators** with full permissions (Post Messages, Delete Messages, Edit Messages).

#### Q: Why do video files play without sound in Chrome?
Telegram files encoded with AC3/EAC3 or DTS audio codecs cannot be decoded by standard browser engines. Use external players like **VLC**, **MX Player**, or **MPV** by pasting the stream URL directly.

---

## License

Licensed under the [Apache License 2.0](LICENSE). Built for high reliability, maximum speed, and production media delivery.
