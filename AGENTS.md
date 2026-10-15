# AGENTS.md — Cloud Station File-to-Link Bot

Python 3.13+ Telegram bot converting files to direct HTTP links. Uses Pyrofork, aiohttp, MongoDB, uvloop.

## Run

```bash
python -m Thunder          # Primary entry point
bash thunder.sh            # Runs python3 update.py && python3 -m Thunder
```

## Dependencies

```bash
pip install -r requirements.txt
# aiohttp, cloudscraper, Jinja2, wzgram, pymongo, psutil, python-dotenv, speedtest-cli, tgcrypto, uvloop==0.21.0
```

## Project Structure

```text
Thunder/
├── __init__.py              # __version__, StartTime
├── __main__.py              # Entry: start_services() via asyncio
├── vars.py                  # Configuration from env vars
├── bot/
│   ├── __init__.py          # StreamBot client, multi_clients, work_loads
│   ├── clients.py           # Multi-client management
│   └── plugins/
│       ├── admin.py         # Owner commands: /users /broadcast /status /stats /restart /log /authorize /deauthorize /ban /unban /shell /speedtest
│       ├── callbacks.py     # Inline keyboard handlers
│       ├── common.py        # User commands: /start /help /about /dc /ping
│       └── stream.py        # /link (groups), private/channel media handlers
├── server/
│   ├── __init__.py          # web_server() — creates aiohttp app with routes
│   ├── stream_routes.py     # HTTP streaming endpoints
│   └── exceptions.py        # Custom HTTP exceptions
├── utils/                   # 20 modules — see imports below
└── template/                # dl.html, req.html (Jinja2)
```

## Key Imports

```python
from Thunder.utils.logger import logger          # Async-safe QueueHandler logger, writes to Thunder/logs/bot.txt
from Thunder.utils.database import db             # AsyncMongoClient singleton
from Thunder.utils.rate_limiter import rate_limiter, request_executor, handle_rate_limited_request
from Thunder.utils.bot_utils import is_admin      # async def is_admin(cli, chat_id_val) -> bool — checks bot membership, NOT a decorator
from Thunder.utils.decorators import owner_only   # async guard function, not a decorator
from Thunder.vars import Var                      # All env config
```

## Code Conventions

- PEP 8, 4-space indent, 120-char lines
- Imports: stdlib → third-party → local
- All I/O is async; use `asyncio.sleep()` not `time.sleep()`
- Catch `FloodWait` from Telegram API with `await asyncio.sleep(e.value)`
- Log with `logger.error(..., exc_info=True)` for exceptions
- Admin access: `filters.user(Var.OWNER_ID)` on Pyrogram handlers (not `is_admin()`)
- Naming: PascalCase classes, snake_case functions/vars, UPPER_SNAKE_CASE constants

## Rate Limiting

Two-tier deque system in `rate_limiter.py`:
- Owners bypass queue entirely
- Authorized users → `priority_queue` (drained first)
- Regular users → `request_queue`
- `QueueFullError` raised on overflow

## Configuration

Copy `config_sample.env` → `config.env`. Required vars: `API_ID`, `API_HASH`, `BOT_TOKEN`, `BIN_CHANNEL`, `DATABASE_URL`.

## Streaming health, links and jobs (added after the streaming fixes)

Runtime counters + alerting (`Thunder/utils/metrics.py`, dependency-free so the
routes, streamer and plugins can all import it):

- `/status` (HTTP) returns a `metrics` block; the bot's `/status` prints a
  "Streaming Metrics" report including per-client error attribution.
- `FailureAlerter` DMs `OWNER_ID` when severe stream errors cross
  `STREAM_FAILURE_ALERT_THRESHOLD` inside `STREAM_FAILURE_ALERT_WINDOW_MINUTES`
  (cooldown 15 min). Disable with `ALERT_ON_STREAM_FAILURES="False"`.

Per-caller fairness: `MAX_CONCURRENT_PER_USER` (0 disables, default 8) caps
simultaneous streams per **caller address**, resolved by `caller_key()` from the
last `X-Forwarded-For` entry (the one the proxy added) and falling back to
`request.remote`. Never key this on the uploader id: one popular file would then
share a single allowance across all of its viewers. Over-limit callers get `429`
+ `Retry-After`; 4xx states are logged at debug so they do not read as failures.

Links: records carry `expires_at` (from `LINK_TTL_HOURS`, 0 = never) and
`revoked`. Both `/f/<hash>/<name>` and `/watch/f/<hash>/<name>` answer
`410 Gone` for revoked/expired links. Owner commands: `/revoke <hash|link>`,
`/unrevoke <hash|link>`. `/thumb/<hash>` serves the stored thumbnail, and the
watch/download pages emit OpenGraph tags so links unfurl with a preview.

Uploads: a private message that is exactly one http(s) URL is fetched by
Telegram (`send_document(url)`), stored in BIN_CHANNEL, registered canonically
and returned as a normal link; toggle with `URL_UPLOAD_ENABLED`. `/mystats`
reports links created, downloads served and bytes stored per uploader.

Two production symptoms from the router log, both handled in code:

- a stalled MTProto read used to leave the response silent until the gateway
  dropped it (`H15 Idle connection`, client stuck on a half-delivered stream).
  `CHUNK_READ_TIMEOUT` (20s, `Thunder/utils/custom_dl.py`) turns a silent read
  into a retryable `asyncio.TimeoutError`, so the same chunk is re-read instead.
- `cleanup_clients()` stops the whole pool with `asyncio.gather` inside a 20s
  budget. Stopping 44 clients one by one exceeded the platform's 30s SIGTERM
  grace period and produced `R12 Exit timeout` + SIGKILL (status 137).

Client selection prefers a pool client in the file's own data centre
(`dc_from_file_id` + `select_optimal_client(preferred_dc=...)`) while still
respecting the per-client capacity gate.

CI: `.github/workflows/tests.yml` runs `python -m unittest discover -s tests -t .`
on every push; `dockerize.yml` publishes to GHCR (`ghcr.io/<owner>/<repo>`), no
Docker Hub secrets required.

## Multiple storage channels

Telegram caps channel administrators at 50, and a bot can only be added to a
channel as an administrator, so a bot pool larger than that needs more than one
storage channel:

- `BIN_CHANNEL` is the primary; `BIN_CHANNEL_2`, `BIN_CHANNEL_3`, ... are extras.
  They parse into `Var.BIN_CHANNELS` (primary first, duplicates dropped) and each
  is verified per client at boot.
- Every record stores `bin_channel_id`. `record_bin_channel()` treats records
  written before this feature as living in the primary, so existing links keep
  working.
- A new file goes to `choose_bin_channel(file_unique_id)`: a stable hash, so the
  same file always lands in the same channel and a re-upload reuses its record.
- Each pool client's readable channels are kept in `client_channels`;
  `select_optimal_client(required_channel=...)` only considers clients that
  administer the file's channel (DC preference and the capacity gate still
  apply). When nobody is capable the request fails fast with `503` and the event
  is counted as `no_capable_client` (a severe kind, so it also feeds the owner
  alert): spending the retry budget on a client that was never able to read the
  channel only turns a configuration mistake into a slow timeout.
- Per-file capacity is bounded by the bots of *its* channel (~50 admins per
  channel), not by the size of the whole pool: 100 bots over two channels give
  each file the ~50bots of one channel. More channels spread load, they do not
  give one file a hundred clients.
- Links that do not name a channel (`/{id}?hash=`, `/watch/<hash><id>`) resolve
  by trying every configured channel, primary first.
- Finding the id of a new channel: add the primary bot as an administrator, then
  `python scripts/list_channels.py` (via the bot's `/shell` or a one-off dyno)
  prints every channel the bot can see with its id and whether it is already
  configured. `python scripts/check_pool.py` reports the per-token channel access.
- Sizing facts come from the boot log: it prints the pool start rate, process RSS
  with per-client cost, and the extrapolated boot time for 100/200/400 clients.
  On Heroku the boot budget is 60s (R10) and a standard dyno holds ~1 GB.
- Operationally: first ~50 admins in `BIN_CHANNEL` (counting the owner and the
  primary bot), the rest in `BIN_CHANNEL_2`.
  Only a bot that administers a file's channel can stream that file, and the
  boot log prints the per-channel client counts.

## File references (streaming)

Telegram file references are bound to the session that fetched the message, and
they expire. Two rules keep `/f/...` and `/{id}?hash=...` streaming alive:

- Read the BIN_CHANNEL message with the client that will stream it
  (`ByteStreamer.get_message`) and fall back to `StreamBot` only if that read
  fails. The message cache in `Thunder/utils/custom_dl.py` is keyed by
  `(id(client), message_id)` and is written under the client that fetched — a
  message fetched by one client must never be streamed by another, or Telegram
  answers `400 FILE_REFERENCE_EXPIRED`.
- On a `FILE_REFERENCE_*` error, drop the cached message and re-read it with the
  streaming client, then retry the same chunk (`stream_file`). Never treat it as
  a permanent failure on the first attempt.
- Whoever fetched the reference streams it (`get_message_with_owner` →
  `stream_client`). Every client in `MULTI_TOKEN*` must still be a member of
  `BIN_CHANNEL`, otherwise its own read fails, the primary bot's message is used,
  and the file ends up streaming through the primary bot instead of the pool.

## Debugging

- Logs: `Thunder/logs/bot.txt`
- Health check: admin `/status` command
- No linting/formatting tools configured — follow conventions manually
- Tests: `python -m unittest discover -s tests -t .` (currently covers streaming
  route byte-range serving and `work_loads` accounting — see `tests/`)

## Upstream sync (update.py)

`Procfile` runs `python update.py; python -m Thunder` (semicolon, deliberately:
a failed sync must never stop the dyno from booting).

- The sync must use `git checkout -f -B <branch> origin/<branch>`. Without `-f`
  it aborts on a freshly deployed tree (Heroku slug / Docker image) with
  "untracked working tree files would be overwritten by checkout", because the
  app files exist on disk but have no git history yet. `-f` overwrites only files
  present in the upstream commit, so untracked `config.env` survives.
- `update.py` must exit 0 even when the sync fails, and must always write the
  `.upstream_synced` marker. The marker suppresses the auto-update re-exec in
  `Thunder/__main__.py`; without it on the failure path the boot loops.
- A failed git sync falls back to the GitHub archive download (`update_via_archive`).