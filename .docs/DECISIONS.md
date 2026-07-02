# DECISIONS.md — Architecture Decision Record

**Origin:** Grilling session on `.docs/DESIGN.md`, 2026-07-01.
**Status:** These decisions are the agreed v1 baseline and **supersede** the conflicting "either/or" hedges in `DESIGN.md`. Where `DESIGN.md` is silent or ambiguous, the decision below governs.

Decisions are grouped by area and tagged with the `DESIGN.md` section each one resolves.

---

## Scope & Identity

### D1 — Bespoke tool with thin generic seams (not a product)
- **Decision:** v1 is a bespoke personal tool for the homelab (1800X AM4 host / mini-PC), built with *thin* generalization seams. It is **not** a general-purpose distributable product.
- **Rationale:** The richest, most concrete content in `DESIGN.md` is 100% host/Fika-specific (DOCP/FCLK/XMP, DDR4 marketplace notes, thresholds for the specific 32GB/1800X box). "Extensible to Minecraft/ARK" is speculative scope with zero users. Generic-first is the classic trap that ships nothing; a working bespoke tool can be generalized later.
- **Supersedes:** §1 Philosophy tension ("generic game/server monitoring" vs. "tailored to your SPT+Fika"); §7 multi-host/multi-game future work (deferred).

### D2 — Tight MVP cut line
- **Decision — IN v1:** hardware collector (CPU/RAM/disk I/O + free space/net via psutil; disk queue; WHEA); Fika process monitoring + config read + best-effort log events; 5 hardware-derived gates; SQLite history + in-memory deque; `GET current` + `GET history` + one live transport; single Overview page; service install + SETUP_GUIDE + firewall rule.
- **Decision — OUT (deferred to v1.x):** deep-dive metrics tab, Fika/raids tab, logs viewer, settings/**config-editor UI** (edit YAML by hand), Discord webhooks, LHM per-core/GPU/drive thermal richness beyond a single validated CPU reading, `fika_instability`/`network_errors`/`ram_paging` gates, the weighted 40/30/15/15 "Overall Health Score" (ship simple "any gate active → degraded" instead), WS/SSE/polling duality.
- **Decision — DROPPED permanently:** "Kill process" button (dangerous, near-zero value on a monitor).
- **Supersedes:** §1 Primary Goals/Non-Goals breadth; §8 Implementation Order (this defines the actual v1 boundary).

---

## Fika

### D3 — Reliable core + decorative logs
- **Decision:** Fika v1 = process monitoring (SPT.Server + headless) + read-only config display as first-class, reliable sources. Log parsing is **best-effort "Recent Events" context only** and **never** feeds gates or the health status.
- **Rationale:** Fika has no public rich API; all Fika-specific intelligence depends on reverse-engineered log formats that change every SPT/Fika mod update. The gates that drive upgrade decisions (RAM/CPU/storage/temps/WHEA) are 100% hardware-derived and version-independent. Risk inversion: the killer feature (gates) is solid; the differentiator (Fika intelligence) is fragile. Keep the fragile part decorative so log-format churn never corrupts decisions.
- **Supersedes:** §3.2 Fika/SPT metrics depth; the `fika_instability` gate feeding health.

### D4 — Config-driven process discovery + helper
- **Decision:** Process identification is config-driven: `spt_server_process_name`, `headless_process_name`, `headless_cmdline_pattern` (regex). Ship `scripts/discover_processes.py` that dumps candidate processes (name + PID + full cmdline) so the real headless signature is filled from the actual WATCHDOG/Fika setup. No hardcoded signature guess.
- **Rationale:** Headless clients are `EscapeFromTarkov.exe` instances distinguished only by a Fika-version-specific cmdline arg; hardcoding a guess breaks silently on Fika changes and can't separate headless from a player client. Process-level data yields count + per-PID CPU/RAM/uptime; "hosting a raid?" comes from logs (decorative), not the process.
- **Supersedes:** §3.2 "Detect multiple instances via process cmdline or window title / args" vagueness.

---

## Architecture

### D5 — SSE + vanilla JS + Chart.js (no WebSocket, no HTMX)
- **Decision:** Single live transport = Server-Sent Events (`EventSource`) pushing JSON; vanilla JS updates cards + Chart.js. WebSocket and HTMX are **not** included in v1.
- **Rationale:** Data flow is one-way (server pushes; client never streams back in v1), so WebSocket's bidirectionality is wasted. Chart.js needs JSON, not HTML partial swaps, so HTMX doesn't fit and earns no keep in v1 (no partial-swap interactions exist). `EventSource` is native, auto-reconnects, and transits Tailscale/proxies cleanly. Collection is every 5s, so no transport yields sub-5s true freshness regardless. HTMX may return in v1.x when partial-swap UI (config editor / logs viewer) lands.
- **Supersedes:** §2 "WebSocket /ws/live ... fallback HTMX ... SSE as alternative"; §4 "No build toolchain ... HTMX".

### D6 — Dict-merge module contract under namespaces (no framework)
- **Decision:** Each module is a file returning a metrics dict; the collector merges them under namespaced keys (`system.*`, `fika.*`). Fika's config-view and log-sources live in their own file. **No** plugin registry, **no** dynamic discovery, **no** schema-declaration framework; the set of modules is hardcoded. Generalize to a real framework only when game #2 is imminent (Rule of Three).
- **Rationale:** Defining a module abstraction before a second game exists is designing in a vacuum. Two modules (system core + Fika) don't justify framework tax; namespaced dict-merge is the cheapest real seam that still leaves a clean generalization path.
- **Supersedes:** §1 "Clear module system"; §5 file-structure implications of a plugin system.

### D7 — pywin32 in-process for Windows metrics (no PowerShell hot path)
- **Decision:** Disk queue length via `pywin32` `win32pdh` (in-process, every 5s); WHEA errors via `pywin32` `win32evtlog` (in-process, every ~60s). **No `powershell.exe` subprocess in the collection hot path.** PowerShell is reserved for one-shot setup scripts (firewall rule, service install).
- **Rationale:** Spawning `powershell.exe` twice every 5s is ~50–150ms + RAM bump per invocation, 24/7 — directly violating the "<2% CPU / lightweight" success metric — and adds execution-policy fragility. WHEA is a rare-event metric feeding a "last 2h" gate, so 5s polling is pure waste. `pywin32` is already a dependency and exposes both APIs in-process.
- **Supersedes:** §2/§3.1 "PowerShell Get-Counter / Get-WinEvent" subprocess strategy and its "cache results if expensive" workaround.

---

## Reliability

### D8 — Per-module isolation + no-overlap scheduling + backoff
- **Decision:** Each metric source is collected in its own try/except; failure returns `None` for that key only + increments a per-module error counter (UI shows "X unavailable"). The loop schedules the next cycle 5s after **completion** (cycles never overlap, slight drift acceptable). After N consecutive failures, a module backs off (e.g., poll every ~60s) to avoid log spam / wasted cycles.
- **Rationale:** Temps (LHM) will be the least-tested, most-likely-to-throw path in v1 and is debugged in production. Concurrent collector runs corrupt the gate state machine, the deque, and SQLite writes, so overlap must be impossible. Isolation guarantees a temps crash degrades to "temps unavailable" without blanking CPU/RAM or corrupting state.
- **Supersedes:** §5 "Handle exceptions gracefully" (made concrete and structural).

### D9 — Temps: LHM in v1, SYSTEM service, display ON / gate disabled until validated
- **Decision:** Ship LibreHardwareMonitor via `pythonnet` in v1; service runs as **SYSTEM**. Temps **display** from launch. The `cpu_thermal` gate ships **DISABLED by default** and is armed only after running `scripts/probe_temps.py` on the 1800X, confirming the sensor identity (Tdie vs Tctl vs CPU Package) and accounting for the Zen1 **Tctl +20°C offset**, then setting a correct threshold.
- **Rationale:** `DESIGN.md`'s 4-tier temps fallback is mostly fiction — `WinTmp` is just a thin wrapper over LibreHardwareMonitor and *also* requires admin (tier-1 == tier-2); `MSAcpi_ThermalZoneTemperature` returns motherboard thermal-zone temps, not CPU package, and is unreliable on Ryzen (tier-3 useless on AM4). So the real menu is "embed LHM (needs SYSTEM/admin)" vs "no temps." Temps is the only thing forcing SYSTEM; everything else (log reads, `Get-WinEvent`, PerfMon) works least-privilege. Because temps cannot be tested on the non-Ryzen dev desktop, decouple display (safe) from gating (can false-trigger) so an unvalidated sensor/offset can't spam alerts on day one.
- **Supersedes:** §2/§5 "Temps Strategy (priority order)"; §3.3 `cpu_thermal` gate default state.

### D10 — Gates armed from launch with conservative defaults + baselining
- **Decision:** The 5 hardware gates (`ram_high`, `cpu_sustained`, `storage_io`, `storage_space`, `stability_whea`) ship **enabled** with **conservative** thresholds higher than `DESIGN.md`'s examples (e.g., RAM >90%, CPU >88% sustained) to minimize false triggers during baselining. The UI shows **current-value-vs-threshold headroom gauges** from day one. After observing a real raid, thresholds are lowered to the host's real headroom via config (no code change). (`cpu_thermal` remains disabled per D9.)
- **Rationale:** Gate thresholds in `DESIGN.md` are openly guesses. The host's real baselines are unknown until observed in prod — especially RAM, which may legitimately idle at 75–85% with SPT.Server + headless clients. Unlike temps, the *data* (psutil) is reliable; the risk is only bad threshold *values*, so arming is acceptable. Conservative defaults + visible headroom let the killer feature go live on day one without a flood of false alerts, with an explicit post-raid calibration expectation.
- **Supersedes:** §3.3 gate example thresholds (now conservative defaults to be tuned, not authoritative).

---

## Security

### D11 — Bind 0.0.0.0, LAN+Tailscale-scoped firewall, token required
- **Decision:** Bind `0.0.0.0:8080`; firewall rule scoped to the specific LAN subnet + Tailscale range `100.64.0.0/10`, **excluding guest/IoT VLANs** where the OPNsense setup segments them. A bearer token is **required** (not optional) for access.
- **Rationale:** Service runs as SYSTEM (D9), so any RCE = full host compromise. The user accepts LAN reachability (devices not all on Tailscale); the token becomes the primary auth gate, so it must be mandatory and strong.
- **Supersedes:** §2 "bound to Tailscale IP or 0.0.0.0 ... firewall scoped"; §1 "No complex auth ... optional simple token" (token is now required).

### D12 — HttpOnly cookie auth for SSE
- **Decision:** Authenticate the live SSE stream via an `HttpOnly` cookie set by a first authenticated request (`EventSource` sends it automatically).
- **Rationale:** Browser `EventSource` **cannot send an `Authorization` header** — it carries only cookies or URL query params. Query-param tokens leak into server access logs / browser history / proxy logs (unacceptable for the sole auth gate of a SYSTEM service). Cookie auth keeps SSE's auto-reconnect and keeps the token out of logs.
- **Supersedes:** §2/§5 implied header-token or query-token auth for the live stream.

### D13 — Cleartext HTTP for v1, strong token; TLS deferred
- **Decision:** No TLS in v1 (cleartext HTTP). Token is a ≥32-byte random secret generated at install (never a default/weak value), stored in an owner-only-ACL config file, compared in constant time. Defer TLS to v1.x via `mkcert` local-CA or Caddy reverse proxy if the LAN-trust assumption weakens (e.g., untrusted/guest devices sharing the host's subnet).
- **Rationale:** The cookie (token) travels cleartext over HTTP on the LAN, so anyone sniffing the LAN captures it → SYSTEM. This exposure is **consistent with the chosen threat model** (SYSTEM + `0.0.0.0` + token-only already accept LAN-trust + plaintext). Tight firewall scoping + a strong random token are the cheap mitigations. Adding Caddy contradicts the "self-contained Python" philosophy; `mkcert` is the lighter v1.x option. The trigger to revisit: any untrusted device on the host's LAN subnet.
- **Supersedes:** §1 "No complex auth/RBAC or HTTPS (local network assumed)" — now an explicit, accepted tradeoff rather than an assumption.

---

## Round 2 — Implementation Decisions (D14–D26)

Deeper forks surfaced in a continued grilling pass. These resolve concrete implementation ambiguities `DESIGN.md` left open or hedged, several of which hide production traps (GIL hangs, DST corruption, `database is locked`, restart loops, stale-data false-reassurance).

### Data Layer

#### D14 — Fixed wide table + separate event tables (no EAV)
- **Decision:** `metrics_history` is a fixed wide table — one row per collection cycle, one column per metric (system columns + Fika per-cycle process metrics). Log events go in a separate append-only `fika_events` table; gate transitions in a separate `gate_events` table. Revisit the schema only when generalizing per the Rule-of-Three (same principle as D6).
- **Rationale:** D6's namespaced dict-merge makes a fixed wide table *look* awkward, but v1 has exactly two modules whose per-cycle output is a known, small set of numeric metrics. Key-value (EAV) and JSON-blob hybrids are the same premature-flexibility trap D6 rejected — they trade simple queries + compactness for flexibility nobody needs yet. Config (display-only) and log events (append-only) are not per-cycle, so they get their own tables.
- **Supersedes:** §2 `metrics_history` column list (now authoritative shape); §1 "Clear module system" schema implications.

#### D15 — On-the-fly SQL time-bucket aggregation for history ranges
- **Decision:** History queries downsample at query time via `GROUP BY (ts / bucket)` with aggregates, bucket-sized to cap output ~720 points per range (24h→2-min, 6h→30s, 1h/15m→raw 5s). Use `max()` for peak-relevant metrics (CPU/RAM — don't average away spikes) and `avg()` for throughput rates (disk/network). No pre-computed rollup tables.
- **Rationale:** At 5s cadence over 48h the whole table is ~34,560 rows — SQL bucket-aggregates that in single-digit ms. Rollup tables exist to tame millions of rows and would double the write path + add sync/backfill complexity for a scale never reached here. Shipping raw 5s points for a 24h chart (17,280 points) would stall Chart.js and bloat payloads.
- **Supersedes:** §2 "keep 24h high-res + daily aggregates" (aggregates are computed, not stored) and the history-endpoint downsampling ambiguity.

#### D21 — Single shared aiosqlite connection (serialized)
- **Decision:** One `aiosqlite` connection opened at FastAPI lifespan startup and reused by the collector (writes), API handlers (reads), and pruning — all serialized on the event loop. Default rollback journal (WAL unnecessary with one connection). `busy_timeout` set defensively.
- **Rationale:** SQLite is fundamentally single-writer; a single shared connection makes concurrent access *impossible by construction*, so there is no `database is locked`/`SQLITE_BUSY` to hit. Connection-per-request (a Postgres-world instinct) collides with the collector's 5s writes and forces WAL + retries — complexity that exists only to patch a bad connection model. Critical to nail because `database is locked` errors on a headless host are hard to debug remotely.
- **Supersedes:** §5 `database.py` ("aiosqlite init") — implies the single-connection model explicitly.

### Windows / Collection Specifics

#### D16 — Windowed re-query + RecordNumber dedup for WHEA
- **Decision:** Every ~60s, fresh-query the System log for WHEA-Logger events with `TimeGenerated >= now − 2h` and count them (the count naturally decays as events age out — correct sliding window). Dedup the persisted "recent WHEA events" UI list via a `RecordNumber` unique constraint so re-inserts across polls are no-ops. Count all WHEA events uniformly (19/20/41); surface raw event text/ID for human severity judgment rather than weighting per Event ID.
- **Rationale:** The gate is a sliding 2h window, so a naïve "count new events since last poll and accumulate" never decays and sticks forever. A stateless windowed re-query is both simpler and correct; watermark+accumulate+decay re-derives the same answer with more state and restart edge cases. Per-Event-ID severity weighting is threshold-guessing theater for a rare metric (consistent with D10's stance).
- **Supersedes:** §3.1/§3.3 WHEA collection + the `stability_whea` gate counting semantics.

#### D17 — Periodic tail for logs; drop the watchdog dependency
- **Decision:** Each collector cycle (5s), seek to the last byte offset per log file, read new lines, regex-classify into the decorative Recent Events feed. Per-file byte offsets held in memory only; on restart, resume from end (skip historical backlog — live feed, not archive); rotation-safe (offset > file size → reset to end). `watchdog` is **not** a dependency.
- **Rationale:** `DESIGN.md`'s only justification for `watchdog` was efficiency, which only mattered when logs were gate-feeding. Per D3 logs are now decorative context whose latency is irrelevant, so the efficiency argument collapses — leaving `watchdog`'s costs (a dependency, a background observer thread, Windows file-handle/rotation edge cases) with no benefit. Periodic tail is simpler, failure-isolated (a wedged log just yields no new lines that cycle), and removes a line from `requirements.txt`.
- **Supersedes:** §3.2/§5 "watchdog lib for FS events (efficient) or periodic tail" hedge.

#### D20 — Targeted 5s game processes + ~15s sampled top-others
- **Decision:** Known game/dashboard processes (SPT.Server, headless instances, the dashboard itself — PIDs already in hand from D4) are collected every 5s. The full `psutil.process_iter()` for "top 5 others" runs on a separate ~15s timer under D8 isolation. Per-process `cpu_percent()` over the 15s delta is also more meaningful than a 5s one.
- **Rationale:** Full `process_iter()` over every PID every 5s is hundreds of syscalls/cycle on Windows — the same class of overhead that made the PowerShell plan (D7) untenable, directly threatening the <2% CPU goal. The "top others" view doesn't need 5s freshness; 15s is plenty. The process-table collector is its own isolated task so a slow enumeration never delays core metrics.
- **Supersedes:** §4 "Top Processes Table" collection strategy and §5 `utils.py` psutil helpers.

### Deployment / Ops

#### D18 — NSSM (vendored exe) as the service wrapper
- **Decision:** The dashboard runs as a Windows service via NSSM, with the single NSSM `.exe` vendored into `scripts/` (self-contained — no install, no network fetch at deploy). NSSM runs the venv `python.exe main.py`; SCM recovery set to restart on 1st/2nd/3rd failure with delays.
- **Rationale:** pythonnet + LibreHardwareMonitor (D9) is native interop that can wedge the GIL — a native call that blocks indefinitely is *not* catchable by D8's try/except and freezes the whole interpreter. That is precisely the failure mode NSSM was built to handle (it `taskkill`s and restarts a wedged process) and precisely where `win32serviceutil` fails (a GIL-wedged service can't honor the SCM stop signal → forced-kill-after-timeout on a headless host you can't easily reach). The "self-contained Python" objection to NSSM is weak: it's a ~300KB static exe committed to the repo.
- **Supersedes:** §6 "Service: NSSM or pure Python win32serviceutil" hedge.

#### D19 — UTC everywhere for timestamps; monotonic clock for durations
- **Decision:** Persist and query all timestamps as UTC epoch milliseconds; the browser renders local time via `Date()`. OS-supplied timestamps (WHEA `TimeGenerated`, etc.) are normalized to UTC on ingest. Gate sustained-duration logic uses `time.monotonic()` deltas exclusively — wall-clock only labels, never measures elapsed time.
- **Rationale:** For a 24/7 service, naive local time corrupts twice a year: DST spring-forward creates timestamp gaps (a 5s cycle "takes an hour"), fall-back creates duplicates that break ordering and sliding-window queries. UTC sidesteps both. Gate durations measured by wall-clock subtraction are vulnerable to NTP steps / DST jumps falsely triggering or resetting "sustained for N minutes"; `time.monotonic()` is immune. `DESIGN.md` is entirely silent on timezone.
- **Supersedes:** (silence in) §2/§3/§5 on timestamp representation and duration measurement.

#### D26 — Logging to stdout, captured + rotated by NSSM
- **Decision:** Python `logging` (leveled, default INFO, configurable via `config.yaml`) writes to stdout/stderr; NSSM's `AppStdout`/`AppStderr` + `AppRotateOnline` capture it to one file and own rotation (catches native-interop crash tracebacks that bypass the logging framework too). Windows Event Log as a *secondary* sink for critical events (startup, collector-failed, High-gate transitions) is deferred to v1.x.
- **Rationale:** Letting NSSM own the file + rotation means one log source to inspect and zero rotation logic in Python; NSSM-captured stdout also catches crashes a `RotatingFileHandler` would miss. A Python-owned rotating file would split one incident across two files. Windows Event Log is a strong secondary sink (visible next to the WHEA logs already read there) but a weak primary for detailed collector logs.
- **Supersedes:** §5/§6 "Dashboard self-logs to file + Windows Event if configured" hedge.

### UX / Frontend

#### D22 — Layered status pill: stale-data > High gate > Medium gate > Operational
- **Decision:** The top-nav status pill is computed with strict precedence: (1) core collector hasn't produced a successful cycle in >3 intervals (~15s, via D8's per-cycle tracking) → red "Monitoring degraded — stale data"; (2) any High-severity armed gate triggered → red "Critical: {gate}"; (3) any Medium armed gate triggered → yellow "Degraded: {gate}"; (4) otherwise green "Operational". Optional-module failures (temps/logs in backoff) color only their own card, never the pill.
- **Rationale:** The deferred weighted health score (D2) left the pill undefined. The worst outcome on a 24/7 monitor is green-with-stale-data — trusting numbers that froze at 02:00 — so collector liveness must *outrank* gates. "Degraded" means the *core* metrics failed, not an optional module in backoff (consistent with D8): a backed-off temps module shows "unavailable" on its card while the pill reflects the still-healthy core.
- **Supersedes:** §4 Top Nav "Status pill ... computed from active gates + overall score" (now the layered precedence, no score).

#### D23 — Auto-generate config on first run + degrade gracefully
- **Decision:** On first run / missing `config.yaml`, auto-generate one from the example with safe defaults and start the collector. System metrics need zero config (psutil/pywin32 auto-discover), so the dashboard is immediately useful; the Fika module starts **disabled** with a "Fika not configured — set paths in config.yaml" banner and activates after the file is edited + the service restarted. All paths are validated on startup; an invalid path disables only its module with a logged warning (D8 isolation), never blocks startup. No interactive wizard (no stdin under NSSM).
- **Rationale:** Fail-fast-on-missing-config restart-loops under NSSM and buries the message in its stdout log — hostile on a headless host. An interactive wizard is unusable (services have no stdin). Because the core needs no config, degrade-gracefully gives instant hardware-monitoring value and a smooth path to enabling Fika, and never restart-loops. Uniform with D8: a bad path is just module-level degradation.
- **Supersedes:** §6 deployment step 5 ("copy config.yaml.example, edit") — now auto-generated + degraded, not manual-copy-required.

#### D24 — Login form + long-lived cookie for initial auth
- **Decision:** `GET /login` renders a single password field; `POST` the token → constant-time validate → set an `HttpOnly`, `SameSite=Lax` cookie with a ~90-day expiry → redirect to the dashboard. The token is generated at install (D13) and written to an owner-only file surfaced in the install-script output. No URL-token. Re-login only on cookie expiry.
- **Rationale:** The cookie auth chosen in D12 needs an initial minting flow the doc never specifies. A login form is standard, hygienic, and keeps the token out of URLs/history (unlike `/?token=XXX`). A session-only cookie would force constant re-login on a glance-at-it tool, pushing toward insecure shortcuts like bookmarking the token; a ~90-day cookie fits daily homelab use. The long-lived-over-cleartext caveat is consistent with D13's accepted threat model.
- **Supersedes:** (silence in) §1/§5 on the auth/login flow.

#### D25 — Full-snapshot SSE + REST pre-fill on load
- **Decision:** On page load, REST `GET history` pre-fills charts for the selected range and REST `GET current` populates cards; then open the SSE stream. SSE pushes a FULL metrics snapshot every 5s — no delta/diff logic. On any (re)connect, the server's first push is a full snapshot, guaranteeing resync after `EventSource`'s silent auto-reconnect with zero client-side merge state.
- **Rationale:** A few KB/snapshot every 5s on LAN/Tailscale is trivial, so delta optimization gains nothing and reintroduces the reconnect-resync problem it claims to solve (missed deltas during `EventSource`'s silent reconnect would need a full-snapshot handshake anyway). REST pre-fill avoids empty cards waiting on the first SSE push and lets charts show history ranges the stream can't carry.
- **Supersedes:** §2 "push compact JSON deltas" (now full snapshots) and the initial-load data flow.

## Round 3 — Robustness, Professionalism & Elegance (D27–D34)

Decisions from a critical design-review pass focused on elegance, robustness, and professionalism. Several close genuine 24/7-survival bugs and one security finding (WinRing0 in a SYSTEM service) that the earlier grilling rounds missed.

### Process Reliability & Self-Healing

#### D27 — Collector supervision + loop-body wrapping + `/health` liveness + external watchdog
- **Decision:** The collector loop body is wrapped **end-to-end** in try/except-log-continue (not just per-module per D8), so the task cannot die from any raised exception. A **supervisor task** awaits the collector task and restarts it on unexpected exit. `/health` exposes `last_tick_age_seconds` (machine-readable; D35). For the irreducible case of a native-interop GIL hang (pythonnet/LHM) that stalls the event loop in a way Python can't catch, an **external Windows Scheduled Task** curls `/health` on a short timeout and restarts the service on failure. Lifespan `shutdown` cancels the collector, closes SSE subscribers, and closes the DB cleanly.
- **Rationale:** A dead asyncio collector task doesn't crash the FastAPI process — the app keeps serving the last cached snapshot over a live SSE connection, so NSSM (which monitors process liveness, not application liveness) never restarts, and the dashboard serves stale data **silently and indefinitely** on a 24/7 box. This is the highest-severity gap in the prior design. Loop-body wrapping prevents death-by-exception; the supervisor handles unexpected task return; `/health` makes staleness detectable; the external watchdog is the only backstop for a hung-but-alive process (NSSM alone can't see it).
- **Supersedes/extends:** D8 (per-module isolation now covers the loop orchestration too); closes the silent-staleness hole the grilling missed.

#### D28 — Broadcast broker with bounded per-subscriber queues
- **Decision:** A `broker.py` sits between the collector and SSE subscribers. The collector publishes one snapshot per cycle via a **non-blocking** call; the broker fans out into one bounded `asyncio.Queue(maxlen=K)` per subscriber (drop-oldest on overflow). Each SSE handler drains its own queue. Concurrent subscribers are capped (e.g., 20). The collector never awaits a client.
- **Rationale:** Without a broker, a broadcast that does `await client.send(...)` per client in the collector loop lets a single slow/stuck SSE client (a flaky Tailscale mobile tab) block the entire 5s cycle. The broker decouples collector from clients; bounded queues give backpressure that drops data for a slow client rather than stalling fast ones or the collector. This also naturally trims the `collector.py` god-module (broadcast moves out).
- **Supersedes:** §5 collector `broadcast()` (now `publish()` → broker) and the implicit direct-fan-out model.

### Asset & Packaging Robustness

#### D29 — Vendored static frontend assets (no CDN, no Play CDN)
- **Decision:** Frontend assets are **vendored** as static files: `static/vendor/tailwind.css` (compiled once via the standalone Tailwind CLI **at authoring time**, committed) and `static/vendor/chart.umd.min.js`. The Tailwind **Play CDN is not used.**
- **Rationale:** Tailwind's own documentation states the Play CDN is **not for production** (~500KB in-browser JIT, FOUC, an extra network hop) — a professionalism smell for a tool meant to run forever. Worse, CDN dependencies break if the viewing device has no internet (a LAN/Tailscale-only phone checking the dashboard). Vendoring keeps the zero-deploy-build philosophy (the Tailwind CLI runs only when the design changes, at authoring time) while being production-grade and offline-resilient.
- **Supersedes/refines:** D5's "Chart.js CDN + Tailwind Play CDN" — assets are now vendored, not CDN-served.

#### D30 — LHM DLL set vendored from the official release; .NET runtime documented
- **Decision:** `LibreHardwareMonitorLib.dll` and its dependencies are **vendored into `vendor/lhm/`** from the official LibreHardwareMonitor GitHub release, with the LHM `LICENSE` (MPL-2.0) attributed. The path is configurable (`temps.lhm_dll_path`). The **.NET runtime** is documented as a host prerequisite and noted in the SETUP_GUIDE.
- **Rationale:** The prior design said "load `LibreHardwareMonitorLib.dll`" without specifying acquisition — an implementer would stall (the DLL isn't on PyPI; it ships in the LHM release zip alongside `HidSharp.dll` and the WinRing0 driver). Vendoring from the official release is the reproducible, license-clean, offline-friendly answer and pins the exact LHM version tested against pythonnet.
- **Supersedes/refines:** D9's temps strategy (adds the concrete acquisition/packaging path).

#### D31 — WinRing0-in-SYSTEM risk acknowledged; official-release-only
- **Decision:** Accept and document that LHM reads sensors via the **WinRing0 kernel driver**, which carries **CVE-2020-14979 (local privilege escalation)** and is AV-flagged, and that we load it into a **SYSTEM**-privileged, network-reachable process. Mitigations: the driver/DLL come **only from the official LHM GitHub release** (vendored, D30); network reach is limited by the firewall scope (D11). Explicitly noted that **temps is the only feature forcing SYSTEM + WinRing0**, so deferring temps to v1.1 (running least-privilege instead) remains the risk-reducing alternative if the threat environment changes.
- **Rationale:** Research during the review surfaced this CVE, which materially changes the D9 cost/benefit: SYSTEM service + ring-0 LPE driver + network exposure is a real attack-surface step-up versus the rest of the stack. An honest spec names this rather than burying it, and frames temps-deferral as the available risk dial.
- **Supersedes/qualifies:** D9 — the SYSTEM decision is now made with eyes open about WinRing0/CVE.

### Professionalism

#### D32 — Automated tests + quality bars (typing, ruff, pinned deps, schema migrations)
- **Decision:** A `tests/` dir with **pytest** covers the pure-logic modules: gate sustained-duration timing, WHEA sliding-window decay (D16), downsampling bucket math (D15), log-line regex classification, config validation (incl. regex compile), and broker overflow behavior. The project uses **full type hints**, **ruff** (lint + format), and **pinned dependencies via `uv`** (committed lockfile). SQLite schema uses **`PRAGMA user_version` + idempotent migrations** run at startup.
- **Rationale:** The trickiest logic (gate timing, WHEA window, downsampling) is exactly what bites silently and is trivially unit-testable as pure functions — locking it down is high-value precisely because temps will be debugged in prod (D9), so the *logic* must be trustworthy even where the *hardware* is uncertain. Type hints + ruff + pinned deps are standard professional bars; pinning matters especially here because pythonnet ↔ LHM ↔ .NET version compat is fragile and the server is updated rarely. Schema migrations future-proof the wide table (D14) for column adds without hand-SQL.
- **Supersedes/extends:** §6 Testing (now includes automated coverage); D14 (now versioned/migrated).

#### D33 — Token hygiene: ACL config to SYSTEM+Administrators; token never logged
- **Decision:** The install script **ACLs `config.yaml` to `SYSTEM` + `Administrators` only** (so a low-priv LAN user can't read the token), and the auth token is **redacted from all log records** via a logging filter.
- **Rationale:** D13 said "owner-only file" generically, but the service runs as **SYSTEM** — the file's ACL must be tied to that identity to mean anything, else any local user reading the file gets the single auth gate to a SYSTEM service. "Never logged" closes the trivial leak where the token that D24 puts in a cookie also appears in request/error logs.
- **Supersedes/refines:** D13's "owner-only file" + D26 logging (adds token redaction).

#### D34 — Product name is RaidWatch (align with repo)
- **Decision:** The product is named **RaidWatch** throughout: service name `RaidWatch`, database `data/raidwatch.db`, UI title "RaidWatch". The earlier `FikaMonitor`/`fika_monitor.db` naming is retired.
- **Rationale:** The repository is `raidwatch`; the design previously disagreed (`FikaMonitor`). "RaidWatch" is more accurate — the tool is a host monitor with a Fika module, not the reverse — and matches the existing directory name. Coherence/consistency win.
- **Supersedes:** All prior `FikaMonitor`/`fika_monitor.db` references.

#### D35 — `/health` machine-readable contract
- **Decision:** A `/health` endpoint returns a canonical JSON contract: `{status (operational|degraded|critical), version, started_at, collector:{last_tick_ts, last_tick_age_seconds, last_cycle_ms, consecutive_failures}, modules:{<name>:{state (ok|degraded|backoff|error)}}, sse_subscribers, db_size_mb}`. It is the server-side backbone of the D22 status pill and the input consumed by the D27 external watchdog Scheduled Task.
- **Rationale:** The prior design mentioned `/health` without defining it, yet it's load-bearing: it's what makes collector staleness *machine-detectable* server-side (the UI pill is human-facing only) and what the external watchdog curls to decide whether to restart the service. A fixed contract means the watchdog, the UI, and future tooling all agree on the meaning of "degraded."
- **Supersedes:** §5 "/health endpoint" mention (now a defined contract).

### Document-style changes (no ADR entry — presentation, not decisions)

- **E2 — Register shift in §2 "Stack":** trimmed from re-arguing choices (ADR-register voice) to one purpose-line per technology, deferring the *why* to `DECISIONS.md`. The living spec states what *is*; the ADR records *why*.
- **E3 — `collector.py` trimmed:** broadcast logic moved to `broker.py` (falls out of D28); persistence stays in `database.py`. The god-module concern resolves as a side effect of the broker decision, so no separate decision is needed.

---

## Decided by default (low-stakes, single obvious answer — not grilled)

- **Live data buffer:** `collections.deque(maxlen=720)` (1h @ 5s) for charts; no DB hit on the hot path.
- **History store shape & downsampling:** see D14 (fixed wide table + event tables) and D15 (on-the-fly SQL bucketing, no stored aggregates); prune `metrics_history` rows `>48h`. Connection model per D21.
- **Gate state persistence:** in SQLite (survives restarts); durations measured via `time.monotonic()` per D19.
- **Frontend structure:** single `index.html` + `static/app.js` + Chart.js CDN + Tailwind Play CDN; Jinja only if a server-rendered initial state is wanted.
- **Config changes:** applied by restarting the service (no live reload in v1; config-editor UI is deferred per D2).
- **Process-level metrics:** psutil `cpu_percent`, `virtual_memory`, `swap_memory`, `disk_io_counters`, `net_io_counters`, per-process info for SPT.Server + headless.

---

## Open follow-ups requiring the user's input (not blocking design)

- Confirm the **exact headless-client launch arg** from the WATCHDOG/Fika setup (populates D4's `headless_cmdline_pattern`).
- Run `probe_temps.py` on the 1800X in prod to capture the **real LHM sensor name(s)** and observe the Tctl offset behavior (unblocks arming `cpu_thermal` per D9).
- Baseline a real raid to tune gate thresholds from the conservative defaults to actual headroom (per D10).
