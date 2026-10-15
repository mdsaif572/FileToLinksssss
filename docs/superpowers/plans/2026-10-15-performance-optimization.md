# Codebase Performance and Speed Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Optimize FileToLink bot and streaming server for higher throughput, lower latency TTFB, reduced Telegram FloodWait risks, and faster batch processing.

**Architecture:** 
1. Add an in-memory message metadata LRU cache to `ByteStreamer` so rapid HTTP range requests for the same media don't re-query Telegram's `get_messages` API.
2. Upgrade Aiohttp response handling in `stream_routes.py` from buffered `web.Response(body=generator)` to zero-copy `web.StreamResponse` with direct `prepare()` and `write()`.
3. Unlock parallel URL shortening concurrency via `asyncio.Semaphore` instead of a global mutex.
4. Accelerate batch link processing in `stream.py` with bounded concurrent workers.
5. Optimize in-memory cache pruning in `canonical_files.py` from $O(N)$ full-scan to $O(k)$ head-check.
6. Modernize Python 3.13 event loop invocation in `__main__.py` and make client stream concurrency limits configurable.

**Tech Stack:** Python 3.13, Pyrogram/wzgram, aiohttp, pymongo, unittest

**Spec:** In-session requirements from user: "isko optimize karo thoda code aur speed jada aane ke lea jo need ho wo implement plan banao then changes karo test karo".

## Global Constraints

- Must maintain 100% backward compatibility for all Telegram commands and HTTP endpoints (`/f/...`, `/watch/...`, `/{hash}{id}/...`).
- Zero new third-party dependencies: use Python standard library and existing libraries (`aiohttp`, `pyrogram`/`wzgram`).
- All I/O must remain async-safe and gracefully handle client disconnects (`ConnectionResetError`).
- Test suite must run cleanly via `python -m unittest`.

## Review Focus

1. Client early disconnection during `StreamResponse.write()` must not crash the server or corrupt workload counters.
2. Message caching in `ByteStreamer` must have bounded capacity and TTL to avoid unbounded memory leaks.
3. Concurrent URL shortening must respect timeout and error handling without hanging batch requests.
4. Batch link generation concurrency must retain in-order link output matching original user message order.
5. Cache pruning in `canonical_files.py` must correctly evict expired items without KeyError or broken ordering.

---

### Task 1: ByteStreamer In-Memory Message Caching

**Files:**
- Modify: `Thunder/utils/custom_dl.py`
- Test: `tests/test_custom_dl.py`

**Interfaces:**
- Consumes: `Client.get_messages(chat_id, message_id)`
- Produces: `ByteStreamer.get_message(message_id: int) -> Message` (cached with 5-minute TTL)

- [ ] **Step 1: Write unit test for ByteStreamer message caching**
- [ ] **Step 2: Run test to verify it fails/passes**
- [ ] **Step 3: Implement LRU message caching in `Thunder/utils/custom_dl.py`**
- [ ] **Step 4: Run test to verify it passes**

---

### Task 2: High-Performance Web Streaming with `web.StreamResponse`

**Files:**
- Modify: `Thunder/server/stream_routes.py`
- Modify: `Thunder/vars.py`
- Test: `tests/test_stream_routes.py`

**Interfaces:**
- Consumes: `streamer.stream_file(...)`
- Produces: `_serve_media_response(request, ...) -> web.StreamResponse`

- [ ] **Step 1: Write unit test for range request parsing and StreamResponse headers**
- [ ] **Step 2: Run test to verify failure/baseline**
- [ ] **Step 3: Refactor `_serve_media_response` to use `web.StreamResponse` with `await response.prepare()` and `await response.write()`**
- [ ] **Step 4: Make `MAX_CONCURRENT_PER_CLIENT` configurable in `Var` (default 16)**
- [ ] **Step 5: Run tests and ensure all pass**

---

### Task 3: URL Shortener Concurrency Optimization

**Files:**
- Modify: `Thunder/utils/shortener.py`
- Test: `tests/test_shortener.py`

**Interfaces:**
- Consumes: `ShortenerPlugin.shorten(...)`
- Produces: `ShortenerSystem.short_url(url: str) -> str` with concurrency semaphore instead of mutex lock

- [ ] **Step 1: Write unit test testing concurrent short_url calls**
- [ ] **Step 2: Run test to verify behavior**
- [ ] **Step 3: Replace `asyncio.Lock()` with `asyncio.Semaphore(10)` in `Thunder/utils/shortener.py`**
- [ ] **Step 4: Run test to verify high throughput concurrent execution**

---

### Task 4: Controlled Concurrency for Batch Link Generation

**Files:**
- Modify: `Thunder/bot/plugins/stream.py`
- Test: `tests/test_stream_batch.py`

**Interfaces:**
- Consumes: `process_single(...)`
- Produces: `process_batch(...)` processing chunk items concurrently with order preservation

- [ ] **Step 1: Write test for batch item processing**
- [ ] **Step 2: Update `process_batch` in `Thunder/bot/plugins/stream.py` to process chunk items with bounded concurrency (`asyncio.gather`) while maintaining original sequence**
- [ ] **Step 3: Run tests to verify correctness and order preservation**

---

### Task 5: $O(k)$ LRU Cache Pruning in Canonical Files

**Files:**
- Modify: `Thunder/utils/canonical_files.py`
- Test: `tests/test_canonical_cache.py`

**Interfaces:**
- Consumes: `_cache_by_unique_id`, `_cache_by_hash`, `_cache_by_message_id`
- Produces: `_prune_cache(cache)` stopping as soon as first unexpired key is reached

- [ ] **Step 1: Write unit test for cache eviction correctness and TTL expiry**
- [ ] **Step 2: Optimize `_prune_cache` in `Thunder/utils/canonical_files.py`**
- [ ] **Step 3: Run test to verify all expired items evicted and active items kept**

---

### Task 6: Event Loop Modernization & Full Verification

**Files:**
- Modify: `Thunder/__main__.py`
- Test: Run full test suite `python -m unittest discover tests` and syntax verification

- [ ] **Step 1: Update `Thunder/__main__.py` to use `asyncio.run(start_services())`**
- [ ] **Step 2: Execute full regression test suite `python -m unittest discover tests`**
- [ ] **Step 3: Run python compilation verification across all modules**
