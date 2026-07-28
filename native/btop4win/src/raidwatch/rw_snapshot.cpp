// rw_snapshot.cpp — pure snapshot->row projection (T6 / T11).
//
// This TU intentionally has NO dependency on btop4win, Windows, or SQLite, so it
// compiles into BOTH the btop4win binary (for the collector) and the rw_tests
// binary (for the mapping assertions). The btop-dependent gather lives in
// rw_collector.cpp; the loop wires this projection to db.insert_metrics().

#include "rw_snapshot.hpp"

namespace raidwatch {

MetricsRow to_metrics_row(const RwSnapshot& s) {
    MetricsRow row;
    row.ts = s.ts;

    // --- direct scalar pass-throughs ---
    row.cpu_total_percent           = s.system.cpu_total_percent;
    row.ram_percent                 = s.system.ram_percent;
    row.ram_used_bytes              = s.system.ram_used_bytes;
    row.swap_percent                = s.system.swap_percent;
    row.pages_per_sec               = s.system.pages_per_sec;
    row.disk_read_bps               = s.system.disk_read_bps;
    row.disk_write_bps              = s.system.disk_write_bps;
    row.disk_queue_length           = s.system.disk_queue_length;
    row.disk_avg_sec_per_transfer   = s.system.disk_avg_sec_per_transfer;
    row.temp_cpu_celsius            = s.system.temp_cpu_celsius;
    row.whea_count_2h               = s.system.whea_count_2h;

    // --- derived scalars ---
    // Game-drive free bytes = first volume's free bytes (legacy _persist).
    if (!s.system.disk_volumes.empty()) {
        row.disk_game_free_bytes = s.system.disk_volumes.front().free_bytes;
    }

    // Net totals aggregated across all NICs (legacy _persist sums per-NIC).
    if (!s.system.net_by_nic.empty()) {
        int64_t sent = 0, recv = 0, errs = 0;
        for (const auto& [_, nic] : s.system.net_by_nic) {
            sent += nic.sent_bps;
            recv += nic.recv_bps;
            errs += nic.errin + nic.errout + nic.dropin + nic.dropout;
        }
        row.net_sent_bps   = sent;
        row.net_recv_bps   = recv;
        row.net_errs_total = errs;
    }

    // --- Fika stub mapping (defaults until step 06) ---
    row.fika_spt_cpu_percent   = s.fika.spt_server.cpu_percent;  // nullopt now
    row.fika_spt_rss_bytes     = s.fika.spt_server.rss_bytes;    // nullopt now
    row.fika_headless_count    = s.fika.headless_count;          // 0 now
    row.fika_headless_cpu_total = s.fika.headless_cpu_total;     // 0 now
    row.fika_headless_rss_total = s.fika.headless_rss_total;     // 0 now

    return row;
}

}  // namespace raidwatch
