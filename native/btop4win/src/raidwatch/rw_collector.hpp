// rw_collector.hpp — the 5-second collection loop (T6).
//
// RwCollector reads btop4win's ALREADY-collected shared state (Cpu/Mem/Net/Proc),
// shapes it into an RwSnapshot, projects it to a metrics_history row via
// to_metrics_row(), and persists one row per cycle. Per T6 it does NOT re-query
// PDH/WMI/Psapi for the Cpu/Mem/Net/Disks/Proc equivalents — btop4win's own
// btop_collect.cpp already gathers those on its Runner thread.
//
// Reliability properties ported from raidwatch/collector.py:
//   - Whole body wrapped (D27 analog): the loop cannot die from a throw.
//   - Per-source isolation (D8 analog): each btop source read is in its own
//     try/catch; a throw blanks only that source's fields + bumps its own
//     error counter; the rest of the snapshot is still gathered and persisted.
//   - No-overlap scheduling: the next cycle runs `interval` after completion.
//   - Backoff: after kBackoffThreshold consecutive whole-body failures the
//     loop sleeps ~kBackoffSeconds; a clean cycle resets to the normal cadence.
//   - All durations from std::chrono::steady_clock (D19 analog); only the
//     persisted `ts` is wall-clock UTC epoch ms.

#pragma once

#include "rw_config.hpp"
#include "rw_database.hpp"
#include "rw_snapshot.hpp"

#include <atomic>
#include <string>
#include <thread>

namespace raidwatch {

class RwCollector {
public:
    // `db` must outlive the collector (the btop entry point owns it via
    // start_persistence/stop_persistence).
    RwCollector(RwConfig config, RwDatabase& db);
    ~RwCollector();

    RwCollector(const RwCollector&) = delete;
    RwCollector& operator=(const RwCollector&) = delete;

    // Start the background loop thread (idempotent; no-op if already running).
    void start();

    // Signal stop and join the thread (idempotent; safe if never started).
    void stop();

    // --- self-metrics (for the --health-check staleness probe, T12) ---
    int64_t last_tick_ts() const noexcept { return last_tick_ts_.load(); }
    double last_cycle_ms() const noexcept { return last_cycle_ms_.load(); }
    int consecutive_failures() const noexcept { return consecutive_failures_.load(); }

    // Backoff tuning (D8 analog). Public so tests/future steps can reference.
    static constexpr int kBackoffThreshold = 5;
    static constexpr double kBackoffSeconds = 60.0;

private:
    void run_loop();
    RwSnapshot gather_snapshot();  // per-source isolated reads of btop state

    RwConfig config_;
    RwDatabase& db_;

    std::atomic<bool> stop_{false};
    std::thread thread_;

    std::atomic<int64_t> last_tick_ts_{0};
    std::atomic<double> last_cycle_ms_{0.0};
    std::atomic<int> consecutive_failures_{0};
};
// Pure: cadence (seconds) for the next cycle given the current consecutive
// whole-body failure count and the configured interval. After kBackoffThreshold
// consecutive failures the loop backs off to kBackoffSeconds; a clean cycle
// (caller resets the count to 0) restores the normal interval. Header-inline so
// it is unit-testable without linking the btop/Windows-only collector TU.
inline double cadence_seconds(int consecutive_failures, double interval_seconds) {
    return (consecutive_failures >= RwCollector::kBackoffThreshold)
               ? RwCollector::kBackoffSeconds
               : interval_seconds;
}

// --- process-wide lifecycle (called from the btop4win entry point) ----------
//
// start_persistence loads config.yaml, opens data/raidwatch.db, and launches the
// collector thread. It never throws: a failure is logged and the TUI keeps
// running without persistence (graceful degradation — RaidWatch must not take
// down the host monitor). Returns true on success.
//
// stop_persistence signals the collector to stop, joins it, and closes the DB.
// Safe to call when not started.
//
// Both default paths match rw_config/rw_database ("data/config.yaml",
// "config.yaml.example", "data/raidwatch.db") so the TUI run-from-repo layout
// works without arguments.
bool start_persistence(
    const std::string& config_path = "data/config.yaml",
    const std::string& example_path = "config.yaml.example",
    const std::string& db_path = "data/raidwatch.db");

void stop_persistence();

}  // namespace raidwatch
