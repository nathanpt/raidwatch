# 08 — TUI panels & theming

Status: Backlog

## Goal

Make RaidWatch visible in the TUI: add `gates` and `fika` boxes drawn in
upstream btop4win style, a WHEA badge on the cpu box, the status pill in the
header, options-menu entries, a CSV-export key, and a RaidWatch `.theme` file.
Per T10, upstream's process filter/sort/tree/details/terminate, menus, mouse,
presets, and `.theme` support come free — **verify only, do not port**. No new
collection logic this step; it draws data the collector already produces.

## Requires

- [07 — Gates engine](07-gates-engine.md).

## Tasks

1. **`rw_draw.cpp` / `rw_draw.hpp`** — new boxes in upstream draw style (mirror
   the layout helpers used by `btop_draw.cpp`):
   - **`gates` box** — one row per gate showing: id, severity pill, current
     value vs threshold (with the operator), a sustained-duration progress bar
     (`elapsed / duration_seconds` while crossing, before trigger), and the
     triggered/cleared state. Disabled gates render greyed out.
   - **`fika` box** — three regions: process list (SPT.Server + each headless
     client: pid, cpu%, rss, uptime), the read-only config summary
     (`max_players`, `bot_limits`, `send_rate`, `job_worker_count`,
     `optimized`, `force_ip_set`, `raid_udp_port_open`, `risky_mods`), and the
     recent-events feed (newest ≤ from the 200-event ring). Surface
     `headless_crashed` prominently when set.
2. **WHEA badge** on the cpu box — render `whea_count_2h` (2h count) as a small
   badge; color escalates from neutral → warning when above the
   `stability_whea` threshold.
3. **Header status pill** — render `compute_status_pill`'s `(class, label)` in
   the header. Precedence must match the Python function exactly: stale >
   high-gate > medium-gate > operational (step 07 already computes it; this
   step only draws it).
4. **`shown_boxes` extension** — add `gates` and `fika` to the upstream
   `shown_boxes` config values (in `btop_config.cpp`'s `.conf` mechanism). The
   default layout includes them; the user can hide/reorder like any upstream
   box. Default ordering keeps the upstream cpu/mem/net/proc boxes and appends
   `gates fika`.
5. **Options-menu entries** — add menu items to toggle the gates and fika boxes
   and to trigger CSV export (reusing upstream's options-menu infrastructure).
6. **CSV export** — a TUI key (e.g. the upstream export binding) **and**
   `raidwatch.exe --export-csv <path>`. Output the downsampled history via the
   step-02 `query_history` path, written in the **exact column order** of the
   Python exporter (`main.py::metrics_export` via `csv.DictWriter` with
   `rows[0].keys()`):
   ```
   bucket_ts,cpu_total_percent,ram_percent,ram_used_bytes,swap_percent,
   pages_per_sec,disk_read_bps,disk_write_bps,disk_queue_length,
   disk_avg_sec_per_transfer,disk_game_free_bytes,net_sent_bps,net_recv_bps,
   net_errs_total,temp_cpu_celsius,whea_count_2h,fika_spt_cpu_percent,
   fika_spt_rss_bytes,fika_headless_count,fika_headless_cpu_total,
   fika_headless_rss_total
   ```
   Header row first, one row per downsampled bucket, `text/csv` semantics
   (comma-delimited, newline-terminated). Default window = 24h (1440 min).
7. **`themes/RaidWatch.theme`** — author a RaidWatch theme using btop4win's
   `.theme` format. Ship it in the themes folder so it loads on first run.
8. **Verify upstream features (do not port):** braille graphs (cpu/mem/net
   history rendering), process filter/sort/tree, details, terminate, menus,
   mouse, presets, and `.theme` switching all work unchanged on the forked
   binary. Record a one-line confirmation per feature in the Log.

## Files

**Created:**
- `native/btop4win/src/raidwatch/rw_draw.hpp` / `rw_draw.cpp`.
- `native/btop4win/themes/RaidWatch.theme` (or the upstream themes dir).

**Modified:**
- `native/btop4win/src/btop_config.cpp` — extend `shown_boxes` with `gates fika`
  (T2: minimal, additive edit to an upstream file — record it in the Log).
- `native/btop4win/src/btop_draw.cpp` (or the draw dispatch) — register the new
  boxes + header pill + WHEA badge; add the options-menu entries and the export
  key binding.
- CLI entry — add `--export-csv <path>`.

## Definition of Done

- [ ] The `gates` box renders every gate (enabled + disabled) with current vs
      threshold, the sustained-progress bar, and triggered/cleared state.
- [ ] The `fika` box renders procs, the config summary, and the recent-events
      feed; `headless_crashed` is surfaced when set.
- [ ] The cpu box shows the WHEA 2h-count badge (escalating color above the
      `stability_whea` threshold).
- [ ] The header status pill reflects `compute_status_pill` precedence exactly
      (stale > high > medium > operational) under each condition.
- [ ] `shown_boxes` includes `gates fika`; both boxes can be hidden/reordered
      like upstream boxes.
- [ ] Options-menu entries toggle the new boxes and trigger CSV export.
- [ ] The TUI export key **and** `--export-csv <path>` produce a CSV whose
      header and column order match the Python exporter exactly (the 21 columns
      listed above, in that order).
- [ ] `themes/RaidWatch.theme` loads and recolors the TUI.
- [ ] Upstream features (braille graphs, process filter/sort/tree/details/
      terminate, menus, mouse, presets, theme switching) verified working on
      the fork — one line each in the Log.
- [ ] Visual checklist: a screenshot of the full layout (cpu/mem/net/proc +
      gates + fika + header pill + WHEA badge) matches the btop4win layout
      conventions.

## Verification

On the Windows host:

```powershell
msbuild native\btop4win\btop4win.sln /p:Configuration=Release /p:Platform=x64
.\x64\Release\raidwatch.exe
#   Expected: cpu/mem/net/proc + gates + fika boxes render; header pill shows
#             current status; WHEA badge on cpu box; theme switch recolors.

# CSV column-for-column parity vs the legacy exporter:
.\x64\Release\raidwatch.exe --export-csv out_new.csv
#   (on the Linux box, generate the reference from the legacy app, OR compare
#    headers directly:)
#   Expected header line == "bucket_ts,cpu_total_percent,ram_percent,...,
#                            fika_headless_rss_total" (21 columns, exact order).
```

On the Linux box (legacy reference still present until step 10):

```bash
# Generate the reference CSV from the legacy exporter and diff the header +
# column set against out_new.csv (values will differ — only structure must match).
```

Record the screenshot ref, the CSV header line, and the per-feature upstream
verifications in the Log.

## Log

<!-- On Done: date + screenshot ref, the CSV header line, and the one-line-per-
     feature upstream verifications. Empty until executed. -->
