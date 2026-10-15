# Web-Based Admin Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a secure, high-performance, real-time Web Admin Dashboard at `/admin` with live metrics, multi-client pool health monitoring, active streams table, and IP ban/unban controls.

**Architecture:** 
1. Create `Thunder/utils/ip_blocker.py` to manage banned caller IPs (in-memory fast lookup + MongoDB persistence), integrated into streaming route guards so banned IPs receive `403 Forbidden`.
2. Implement `Thunder/server/admin_auth.py` for cookie-based session verification with `WEB_ADMIN_PASSWORD` (stored in `Var`).
3. Add `Thunder/server/admin_routes.py` with endpoints for `/admin` (login/dashboard), `/admin/login`, `/admin/logout`, and `/api/admin/*` (`stats`, `ban_ip`, `unban_ip`).
4. Design a dark-mode, responsive web template `Thunder/template/admin_dashboard.html` with real-time stats polling, active stream monitoring, bot workloads, and 1-click IP management.
5. Register routes in `Thunder/server/__init__.py` and add unit test coverage in `tests/test_web_admin_dashboard.py`.

**Tech Stack:** Python 3.13, aiohttp, Jinja2, MongoDB, HTML5/CSS3/Vanilla JS (responsive glassmorphism dark theme), unittest.

**Spec:** User selected "Option 4: Web-Based Admin Dashboard (Live streams, bandwidth stats, IP ban, health check)" from feature proposals.

## Global Constraints

- Must maintain 100% backward compatibility for all existing routes (`/status`, `/f/...`, `/watch/...`, etc.).
- Zero new third-party dependencies: use existing `aiohttp`, `jinja2`, `pymongo`, standard library `hmac`, `secrets`, `hashlib`.
- If `WEB_ADMIN_PASSWORD` is not configured in `config.env`, the `/admin` page displays a setup warning and rejects unauthorized access.
- All dashboard endpoints must strictly require authentication except the login view and login submission.
- Fast-path IP blocking check must be $O(1)$ set lookup to avoid adding latency to high-concurrency streaming.

## Review Focus

1. Banned IP requests to `/f/...` and `/watch/...` must be immediately rejected with `403 Forbidden` without consuming bot pool connection slots.
2. Unauthenticated requests to `/api/admin/*` or `/admin/dashboard` must return `401 Unauthorized` or redirect to `/admin`.
3. Session cookies must be HttpOnly and SameSite=Lax/Strict with secure signature verification.
4. Active streams count and caller list must not leak memory or retain stale entries after stream completion or disconnect.
5. IP unban must immediately restore streaming access without requiring a bot/dyno restart.

---

### Task 1: IP Blocker Utility and Stream Route Integration

**Files:**
- Create: `Thunder/utils/ip_blocker.py`
- Modify: `Thunder/server/stream_routes.py:760-775`
- Test: `tests/test_ip_blocker.py`

**Interfaces:**
- Produces: `is_ip_banned(ip: str) -> bool`
- Produces: `ban_ip(ip: str, reason: str = "") -> None`
- Produces: `unban_ip(ip: str) -> bool`
- Produces: `get_banned_ips() -> list[dict]`
- Consumes: `caller_key(request)` from `Thunder/server/stream_routes.py`

- [ ] **Step 1: Write unit tests for IP blocker**
Create `tests/test_ip_blocker.py` testing ban, unban, idempotent bans, and check functions.

- [ ] **Step 2: Run test to verify it fails**
Run: `python -m unittest tests/test_ip_blocker.py`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement `Thunder/utils/ip_blocker.py`**
Implement in-memory set cache for $O(1)$ checks with MongoDB sync if database is available.

- [ ] **Step 4: Integrate `is_ip_banned` check into `Thunder/server/stream_routes.py`**
Before acquiring `user_streams` or selecting a client, check `is_ip_banned(caller_key(request))` and raise `web.HTTPForbidden(text="Access denied")` if banned.

- [ ] **Step 5: Run test to verify it passes**
Run: `python -m unittest tests/test_ip_blocker.py`
Expected: PASS

- [ ] **Step 6: Commit**
```bash
git add Thunder/utils/ip_blocker.py Thunder/server/stream_routes.py tests/test_ip_blocker.py
git commit -m "feat: add IP blocker utility and integrate with stream routes"
```

---

### Task 2: Admin Authentication and Configuration

**Files:**
- Modify: `Thunder/vars.py`
- Modify: `config_sample.env`
- Create: `Thunder/server/admin_auth.py`
- Test: `tests/test_admin_auth.py`

**Interfaces:**
- Consumes: `Var.WEB_ADMIN_PASSWORD`
- Produces: `create_admin_session() -> str`
- Produces: `verify_admin_session(request: web.Request) -> bool`
- Produces: `verify_admin_password(password: str) -> bool`

- [ ] **Step 1: Write unit tests for session generation and validation**
Create `tests/test_admin_auth.py` testing password check, cookie signing, expiry, and invalid token rejection.

- [ ] **Step 2: Run test to verify it fails**
Run: `python -m unittest tests/test_admin_auth.py`
Expected: FAIL

- [ ] **Step 3: Add `WEB_ADMIN_PASSWORD` to `Thunder/vars.py` and `config_sample.env`**
Add `WEB_ADMIN_PASSWORD: str = os.getenv("WEB_ADMIN_PASSWORD", "")` to `Var`.

- [ ] **Step 4: Implement `Thunder/server/admin_auth.py`**
Use `hmac` and `secrets` to generate and verify timestamped, cryptographically signed tokens.

- [ ] **Step 5: Run test to verify it passes**
Run: `python -m unittest tests/test_admin_auth.py`
Expected: PASS

- [ ] **Step 6: Commit**
```bash
git add Thunder/vars.py config_sample.env Thunder/server/admin_auth.py tests/test_admin_auth.py
git commit -m "feat: add admin authentication and session management"
```

---

### Task 3: Admin Web UI Template (Dashboard & Login)

**Files:**
- Create: `Thunder/template/admin_dashboard.html`
- Create: `Thunder/template/admin_login.html`

**Interfaces:**
- Consumes: Jinja2 template rendering via `Thunder.utils.render_template`
- Produces: HTML pages with CSS/JS for real-time polling to `/api/admin/stats` and ban/unban actions.

- [ ] **Step 1: Create `Thunder/template/admin_login.html`**
Clean dark-mode card with password input, error alert, and submit button.

- [ ] **Step 2: Create `Thunder/template/admin_dashboard.html`**
High-aesthetic glassmorphism dashboard with:
- Top stats banner: Uptime, Active Streams, Bandwidth Served, Total Files, Bot Count.
- System metrics: RAM/CPU usage (if available), errors counter.
- Bot Pool Table: Client ID, DC, Channel Access, Active Load.
- Active Callers Table: IP Address, Active Streams count, "Ban IP" button.
- Banned IPs Table: IP, Timestamp, "Unban" button.
- JavaScript polling engine that updates counters and tables without page reload.

- [ ] **Step 3: Commit**
```bash
git add Thunder/template/admin_login.html Thunder/template/admin_dashboard.html
git commit -m "feat: create modern admin dashboard and login templates"
```

---

### Task 4: Admin HTTP Routes and API Endpoints

**Files:**
- Create: `Thunder/server/admin_routes.py`
- Modify: `Thunder/server/__init__.py`
- Test: `tests/test_web_admin_dashboard.py`

**Interfaces:**
- Produces routes:
  - `GET /admin`
  - `POST /admin/login`
  - `POST /admin/logout`
  - `GET /admin/dashboard`
  - `GET /api/admin/stats`
  - `POST /api/admin/ban_ip`
  - `POST /api/admin/unban_ip`

- [ ] **Step 1: Write integration tests in `tests/test_web_admin_dashboard.py`**
Test login GET/POST, dashboard access guard, stats API response structure, and ban/unban API workflows.

- [ ] **Step 2: Run test to verify it fails**
Run: `python -m unittest tests/test_web_admin_dashboard.py`
Expected: FAIL

- [ ] **Step 3: Implement `Thunder/server/admin_routes.py`**
Implement the handlers with proper auth checks, JSON responses, error handling, and session cookie setting/clearing.

- [ ] **Step 4: Register `admin_routes` in `Thunder/server/__init__.py`**
Import `admin_routes` and attach to `web_app`.

- [ ] **Step 5: Run tests to verify they pass**
Run: `python -m unittest tests/test_web_admin_dashboard.py`
Expected: PASS

- [ ] **Step 6: Run full test suite**
Run: `python -m unittest discover -s tests -t .`
Expected: All 125+ tests PASS

- [ ] **Step 7: Commit**
```bash
git add Thunder/server/admin_routes.py Thunder/server/__init__.py tests/test_web_admin_dashboard.py
git commit -m "feat: implement web admin dashboard endpoints and integration tests"
```
