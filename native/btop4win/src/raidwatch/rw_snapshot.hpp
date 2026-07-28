// rw_snapshot.hpp — the RwSnapshot struct tree (T6).
//
// Mirrors raidwatch/models.py::MetricsSnapshot field-for-field. Every nullable
// scalar is std::optional (the Python models used `float | None`); vectors and
// maps are always present (possibly empty). The tree is the in-memory shape the
// collector assembles each cycle; only the scalar subset crosses into
// metrics_history via to_metrics_row() (see rw_snapshot.cpp).
//
// This header has NO dependency on btop4win, Windows, or SQLite — it is pure
// C++ so the snapshot->row mapping is unit-testable in isolation (T11).

#pragma once

#include "rw_database.hpp"  // MetricsRow (the wide metrics_history row)

#include <cstdint>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace raidwatch {

// --- Sub-structs -----------------------------------------------------------

struct RwDiskVolume {
    std::string mount;        // drive letter / mount point (e.g. "C:\\")
    int64_t total_bytes = 0;
    int64_t free_bytes = 0;
};

struct RwNetNicStats {
    int64_t sent_bps = 0;     // upload bytes/sec
    int64_t recv_bps = 0;     // download bytes/sec
    // btop4win does not enumerate NIC error/drop counters; stay 0 this port
    // (kept for struct parity with the legacy NetNicStats + the errs_total sum).
    int64_t errin = 0;
    int64_t errout = 0;
    int64_t dropin = 0;
    int64_t dropout = 0;
};

struct RwTopProcess {
    int64_t pid = 0;
    std::string name;
    double cpu_percent = 0.0;
    int64_t rss_bytes = 0;
};

// One SPT.Server / headless-instance entry (populated in step 06).
struct RwProcessInfo {
    int64_t pid = 0;
    std::string name;
    std::optional<double> cpu_percent;
    std::optional<int64_t> rss_bytes;
};

// --- Metric groups ---------------------------------------------------------

// Mirrors models.py::SystemMetrics. Temps/WHEA/PDH-extras are declared here but
// left std::nullopt until their steps (05/04) populate them.
struct RwSystemMetrics {
    std::optional<double> cpu_total_percent;
    std::vector<double> cpu_per_core_percent;

    std::optional<int64_t> ram_total_bytes;
    std::optional<int64_t> ram_used_bytes;
    std::optional<int64_t> ram_available_bytes;
    std::optional<double> ram_percent;

    std::optional<int64_t> swap_total_bytes;
    std::optional<int64_t> swap_used_bytes;
    std::optional<double> swap_percent;

    std::optional<double> pages_per_sec;                 // step 04 (PDH)
    std::optional<int64_t> disk_read_bps;
    std::optional<int64_t> disk_write_bps;
    std::optional<double> disk_queue_length;             // step 04 (PDH)
    std::optional<double> disk_avg_sec_per_transfer;     // step 04 (PDH)

    std::vector<RwDiskVolume> disk_volumes;
    std::unordered_map<std::string, RwNetNicStats> net_by_nic;

    std::optional<double> temp_cpu_celsius;              // step 05 (LHM)
    std::optional<int64_t> whea_count_2h;                // step 04 (WHEA)
};

// Fika module — declared as a stub here, populated in step 06. The scalar
// fields the wide table references are present so to_metrics_row compiles; they
// stay at their defaults (spt_server optionals = nullopt; headless_* = 0) until
// the Fika gather is wired.
struct RwFikaMetrics {
    RwProcessInfo spt_server;
    int64_t headless_count = 0;        // 0 until step 06 (not NULL — legacy default)
    double headless_cpu_total = 0.0;   // 0 until step 06
    int64_t headless_rss_total = 0;    // 0 until step 06
};

struct RwProcessMetrics {
    std::vector<RwTopProcess> top;     // top-N by CPU (D20)
};

struct RwSelfMetrics {
    double last_cycle_ms = 0.0;
    int consecutive_failures = 0;
    int subscribers = 0;               // 0 outside SSE; kept for struct parity
};

// --- Top-level snapshot (§3.4 contract) ------------------------------------

struct RwSnapshot {
    int64_t ts = 0;                    // UTC epoch ms (D19)
    RwSystemMetrics system;
    RwFikaMetrics fika;
    RwProcessMetrics process;
    RwSelfMetrics self;
};

// --- Snapshot -> metrics_history row ---------------------------------------

// Project the scalar subset of a snapshot onto one wide metrics_history row
// (D14). Pure, allocation-light, no I/O — the exact mapping asserted by
// test_rw_collector. Field-by-field per the step-03 mapping table:
//
//   ts                       snapshot.ts
//   cpu_total_percent        system.cpu_total_percent
//   ram_percent              system.ram_percent
//   ram_used_bytes           system.ram_used_bytes
//   swap_percent             system.swap_percent
//   pages_per_sec            system.pages_per_sec            (NULL until step 04)
//   disk_read_bps            system.disk_read_bps
//   disk_write_bps           system.disk_write_bps
//   disk_queue_length        system.disk_queue_length        (NULL until step 04)
//   disk_avg_sec_per_transfer system.disk_avg_sec_per_transfer (NULL until step 04)
//   disk_game_free_bytes     system.disk_volumes[0].free_bytes
//   net_sent_bps             sum net_by_nic[*].sent_bps
//   net_recv_bps             sum net_by_nic[*].recv_bps
//   net_errs_total           sum net_by_nic[*] (errin+errout+dropin+dropout)
//   temp_cpu_celsius         system.temp_cpu_celsius         (NULL until step 05)
//   whea_count_2h            system.whea_count_2h            (NULL until step 04)
//   fika_spt_cpu_percent     fika.spt_server.cpu_percent     (NULL until step 06)
//   fika_spt_rss_bytes       fika.spt_server.rss_bytes       (NULL until step 06)
//   fika_headless_count      fika.headless_count             (0 until step 06)
//   fika_headless_cpu_total  fika.headless_cpu_total         (0 until step 06)
//   fika_headless_rss_total  fika.headless_rss_total         (0 until step 06)
MetricsRow to_metrics_row(const RwSnapshot& snapshot);

}  // namespace raidwatch
