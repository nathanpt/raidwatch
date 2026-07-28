// rw_collector.cpp — the 5s collection loop (T6). See rw_collector.hpp.
//
// Reads btop4win's already-collected shared state. Concurrency model: btop's
// Runner thread mutates the Cpu/Mem/Net/Proc caches; this collector is a peer
// thread that reads them. Before each source read it waits for
// `Runner::active == false` (the same guard btop's own draw loop uses) and then
// calls the collect() functions with no_update=true, which early-return the
// cached structs without re-querying PDH/WMI/Psapi and without mutating them.
// The residual race window (btop starting a fresh run mid-read) is bounded and
// matches the "read already-collected state" contract in T6; hardening it would
// require a shared mutex in upstream btop_collect.cpp, which T2 forbids.

#include "rw_collector.hpp"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <exception>
#include <memory>
#include <utility>

// btop4win shared state + tools (cached reads only).
#define _WIN32_DCOM
#define _WIN32_WINNT 0x0600
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#define VC_EXTRALEAN
#include <windows.h>

#include <btop_shared.hpp>
#include <btop_config.hpp>
#include <btop_tools.hpp>

namespace raidwatch {
namespace {

using clock = std::chrono::steady_clock;

double elapsed_ms(clock::time_point start) {
    return std::chrono::duration<double, std::milli>(clock::now() - start).count();
}

// Sleep `seconds`, but wake every 100ms to observe the stop flag so quit is
// prompt (btop exits via quick_exit; we must not block the join).
void sleep_with_stop(std::atomic<bool>& stop, double seconds) {
    const auto end = clock::now() + std::chrono::duration<double>(seconds);
    while (clock::now() < end) {
        if (stop.load(std::memory_order_relaxed)) return;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
}

// Lookup a uint64_t stat by key, returning 0 if absent (defensive; btop always
// populates the well-known keys after Shared::init).
uint64_t stat_or0(const robin_hood::unordered_flat_map<std::string, uint64_t>& m,
                  const char* key) {
    auto it = m.find(key);
    return it == m.end() ? 0 : it->second;
}

}  // namespace

// --------------------------------------------------------------- lifecycle --

RwCollector::RwCollector(RwConfig config, RwDatabase& db)
    : config_(std::move(config)), db_(db) {}

RwCollector::~RwCollector() { stop(); }

void RwCollector::start() {
    if (thread_.joinable()) return;  // idempotent
    stop_.store(false);
    thread_ = std::thread([this] { run_loop(); });
}

void RwCollector::stop() {
    if (!thread_.joinable()) return;  // idempotent
    stop_.store(true);
    try { thread_.join(); } catch (...) { /* thread already gone */ }
}

// ---------------------------------------------------------------- main loop --

void RwCollector::run_loop() {
    const double interval = config_.collection.interval_seconds;
    Logger::info("RwCollector loop started (interval=" + std::to_string(interval) + "s)");

    while (!stop_.load(std::memory_order_relaxed)) {
        const auto cycle_start = clock::now();
        int failures = consecutive_failures_.load();

        try {
            // Whole body wrapped (D27 analog): gather -> project -> persist.
            RwSnapshot snap = gather_snapshot();
            MetricsRow row = to_metrics_row(snap);

            const double cycle_ms = elapsed_ms(cycle_start);
            snap.self.last_cycle_ms = cycle_ms;
            snap.self.consecutive_failures = failures;

            db_.insert_metrics(row);

            // Clean cycle: reset counters + publish self-metrics.
            consecutive_failures_.store(0);
            last_tick_ts_.store(row.ts);
            last_cycle_ms_.store(cycle_ms);
        } catch (const std::exception& e) {
            consecutive_failures_.store(failures + 1);
            last_cycle_ms_.store(elapsed_ms(cycle_start));
            // Log once per failure (D27 analog): the loop cannot die from this.
            Logger::warning("RwCollector cycle failed (consecutive="
                            + std::to_string(failures + 1) + "): " + e.what());
        } catch (...) {
            consecutive_failures_.store(failures + 1);
            last_cycle_ms_.store(elapsed_ms(cycle_start));
            Logger::warning("RwCollector cycle failed (non-std exception)");
        }

        // Schedule next cycle `cadence` after completion (no overlap; D8).
        const double cadence =
            (consecutive_failures_.load() >= kBackoffThreshold) ? kBackoffSeconds : interval;
        const double wait_s = cadence - elapsed_ms(cycle_start) / 1000.0;
        sleep_with_stop(stop_, wait_s > 0.1 ? wait_s : 0.1);
    }

    Logger::info("RwCollector loop stopped.");
}

// ------------------------------------------------------------------ gather --

RwSnapshot RwCollector::gather_snapshot() {
    RwSnapshot snap;
    snap.ts = RwDatabase::now_ms();

    // Wait for btop's Runner to finish any in-flight collect so we read a
    // consistent cached snapshot (btop's draw loop uses this same guard).
    Tools::atomic_wait_for(Runner::active, true, 5000);

    int cpu_fails = 0, mem_fails = 0, net_fails = 0, proc_fails = 0;

    // --- CPU (cached; no_update=true returns current_cpu without re-query) ---
    try {
        const auto& cpu = Cpu::collect(true);
        const auto& total = cpu.cpu_percent.at("total");
        if (!total.empty()) snap.system.cpu_total_percent = static_cast<double>(total.back());

        snap.system.cpu_per_core_percent.reserve(cpu.core_percent.size());
        for (const auto& core : cpu.core_percent) {
            snap.system.cpu_per_core_percent.push_back(core.empty() ? 0.0
                                                                    : static_cast<double>(core.back()));
        }
    } catch (const std::exception& e) {
        ++cpu_fails;
        Logger::warning("RwCollector: CPU source failed (consecutive="
                        + std::to_string(cpu_fails) + "): " + e.what());
        // fields stay std::nullopt / empty
    }

    // --- Mem + disks (cached; no_update=true returns current_mem) ---
    try {
        const auto& mem = Mem::collect(true);

        snap.system.ram_total_bytes     = static_cast<int64_t>(stat_or0(mem.stats, "total"));
        snap.system.ram_used_bytes      = static_cast<int64_t>(stat_or0(mem.stats, "used"));
        snap.system.ram_available_bytes = static_cast<int64_t>(stat_or0(mem.stats, "available"));
        const auto& used_pct = mem.percent.at("used");
        if (!used_pct.empty()) snap.system.ram_percent = static_cast<double>(used_pct.back());

        // Swap = Windows page file (btop "page_*"); nullopt when there is none.
        const int64_t page_total = static_cast<int64_t>(stat_or0(mem.stats, "page_total"));
        const int64_t page_used  = static_cast<int64_t>(stat_or0(mem.stats, "page_used"));
        if (page_total > 0) {
            snap.system.swap_total_bytes = page_total;
            snap.system.swap_used_bytes  = page_used;
            const auto& page_pct = mem.percent.at("page_used");
            if (!page_pct.empty()) snap.system.swap_percent = static_cast<double>(page_pct.back());
        }

        // Disk volumes (game drive = disks_order[0] per legacy _persist).
        for (const auto& letter : mem.disks_order) {
            auto it = mem.disks.find(letter);
            if (it == mem.disks.end()) continue;
            snap.system.disk_volumes.push_back({it->first, it->second.total, it->second.free});
        }

        // Disk read/write Bps: btop stores per-cycle byte deltas (over update_ms);
        // convert to bytes/sec by dividing by the btop collection interval.
        const double interval_s = std::max(1, Config::getI("update_ms")) / 1000.0;
        int64_t read_delta = 0, write_delta = 0;
        bool have_io = false;
        for (const auto& [letter, d] : mem.disks) {
            (void)letter;
            if (!d.io_read.empty())  { read_delta  += d.io_read.back();  have_io = true; }
            if (!d.io_write.empty()) { write_delta += d.io_write.back(); have_io = true; }
        }
        if (have_io) {
            snap.system.disk_read_bps  = static_cast<int64_t>(read_delta  / interval_s);
            snap.system.disk_write_bps = static_cast<int64_t>(write_delta / interval_s);
        }
        // disk_queue_length / disk_avg_sec_per_transfer stay nullopt (step 04 PDH).
    } catch (const std::exception& e) {
        ++mem_fails;
        Logger::warning("RwCollector: Mem/Disks source failed (consecutive="
                        + std::to_string(mem_fails) + "): " + e.what());
    }

    // --- Net (read Net::current_net directly; avoids collect()'s selected-iface
    //     side-effects and aggregates every NIC). ---
    try {
        for (const auto& [iface, info] : Net::current_net) {
            RwNetNicStats n;
            auto sit = info.stat.find("upload");
            if (sit != info.stat.end()) n.sent_bps = static_cast<int64_t>(sit->second.speed);
            sit = info.stat.find("download");
            if (sit != info.stat.end()) n.recv_bps = static_cast<int64_t>(sit->second.speed);
            // btop exposes no NIC error/drop counters — errin/out/dropin/out stay 0.
            snap.system.net_by_nic.emplace(iface, n);
        }
    } catch (const std::exception& e) {
        ++net_fails;
        Logger::warning("RwCollector: Net source failed (consecutive="
                        + std::to_string(net_fails) + "): " + e.what());
    }

    // --- Proc (cached; mutex-protected via WMImutex, early-returns once btop
    //     has populated current_procs). Top-5 by CPU. ---
    try {
        auto& procs = Proc::collect(true);
        std::vector<const Proc::proc_info*> ranked;
        ranked.reserve(procs.size());
        for (const auto& p : procs) ranked.push_back(&p);
        const size_t top_n = std::min<size_t>(5, ranked.size());
        std::partial_sort(ranked.begin(), ranked.begin() + top_n, ranked.end(),
                          [](const Proc::proc_info* a, const Proc::proc_info* b) {
                              return a->cpu_p > b->cpu_p;
                          });
        for (size_t i = 0; i < top_n; ++i) {
            const auto* p = ranked[i];
            snap.process.top.push_back({static_cast<int64_t>(p->pid), p->name, p->cpu_p,
                                        static_cast<int64_t>(p->mem)});
        }
    } catch (const std::exception& e) {
        ++proc_fails;
        Logger::warning("RwCollector: Proc source failed (consecutive="
                        + std::to_string(proc_fails) + "): " + e.what());
    }

    // pages_per_sec, temp_cpu_celsius, whea_count_2h stay nullopt (steps 04/05).
    // Fika stays at its stub defaults (step 06).

    return snap;
}

// --------------------------------------------------- process-wide lifecycle --

namespace {
struct PersistenceState {
    std::unique_ptr<RwDatabase> db;
    std::unique_ptr<RwCollector> collector;
};
std::unique_ptr<PersistenceState> g_state;
}  // namespace

bool start_persistence(const std::string& config_path, const std::string& example_path,
                       const std::string& db_path) {
    try {
        auto state = std::make_unique<PersistenceState>();

        std::vector<std::string> unknown;
        RwConfig cfg = load_config(config_path, example_path, &unknown);
        for (const auto& k : unknown) Logger::warning("RwConfig: unknown key ignored: " + k);

        state->db = std::make_unique<RwDatabase>(db_path);
        state->db->open();

        state->collector = std::make_unique<RwCollector>(std::move(cfg), *state->db);
        state->collector->start();

        g_state = std::move(state);
        Logger::info("RaidWatch persistence started (db=" + db_path + ")");
        return true;
    } catch (const std::exception& e) {
        // Graceful degradation: never take down the TUI over a persistence fault.
        Logger::error("RaidWatch persistence disabled: " + std::string(e.what()));
        g_state.reset();
        return false;
    }
}

void stop_persistence() {
    if (g_state) {
        if (g_state->collector) g_state->collector->stop();  // joins the loop thread
        g_state.reset();  // closes the DB via RwDatabase destructor
    }
}

}  // namespace raidwatch
