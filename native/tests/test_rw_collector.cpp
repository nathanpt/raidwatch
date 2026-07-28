// test_rw_collector.cpp — asserts the RwSnapshot -> metrics_history row mapping
// (T6 / T11). Covers every column of the step-03 mapping table: direct scalar
// pass-throughs, the derived disk_game_free_bytes / net totals, and the
// "NULL until step N" / "0 until step N" rules for the not-yet-wired fields.
//
// This test links ONLY rw_snapshot.cpp (the pure projection) — it has no
// dependency on btop4win, Windows, or SQLite, so it builds and runs identically
// on the host and (for prototyping) under g++ on the Linux box. The
// cadence_seconds() backoff helper from rw_collector.hpp is header-inline, so
// its case also needs no btop/Windows link.

#include "catch.hpp"
#include "rw_collector.hpp"
#include "rw_snapshot.hpp"

#include <cstdint>

namespace {

bool has(const std::optional<double>& o) { return o.has_value(); }
bool has(const std::optional<int64_t>& o) { return o.has_value(); }

// Build a snapshot with every step-03-populated field set to a distinct value.
raidwatch::RwSnapshot make_full_snapshot() {
    raidwatch::RwSnapshot s;
    s.ts = 1'700'000'000'000LL;

    s.system.cpu_total_percent = 42.5;
    s.system.cpu_per_core_percent = {10.0, 20.0, 30.0};

    s.system.ram_total_bytes     = 34'359'738'368LL;  // 32 GiB
    s.system.ram_used_bytes      = 16'000'000'000LL;
    s.system.ram_available_bytes = 18'359'738'368LL;
    s.system.ram_percent         = 46.6;

    s.system.swap_total_bytes = 8'000'000'000LL;
    s.system.swap_used_bytes  = 1'000'000'000LL;
    s.system.swap_percent     = 12.5;

    s.system.disk_read_bps  = 5'000'000LL;
    s.system.disk_write_bps = 3'000'000LL;

    s.system.disk_volumes = {
        {"C:\\", 500'000'000'000LL, 123'000'000'000LL},  // game drive (index 0)
        {"D:\\", 1'000'000'000'000LL, 800'000'000'000LL},
    };

    raidwatch::RwNetNicStats eth{};
    eth.sent_bps = 1'000;
    eth.recv_bps = 2'000;
    eth.errin = 1;
    eth.errout = 2;
    eth.dropin = 3;
    eth.dropout = 4;
    raidwatch::RwNetNicStats wifi{};
    wifi.sent_bps = 100;
    wifi.recv_bps = 200;
    wifi.errin = 10;
    wifi.errout = 20;
    wifi.dropin = 30;
    wifi.dropout = 40;
    s.system.net_by_nic.emplace("eth0", eth);
    s.system.net_by_nic.emplace("wlan0", wifi);

    // Step-04/05 fields left unset on purpose (asserted separately).
    return s;
}

}  // namespace

TEST_CASE("to_metrics_row projects every step-03 scalar per the mapping table",
          "[collector]") {
    const auto row = raidwatch::to_metrics_row(make_full_snapshot());

    SECTION("ts and direct pass-throughs") {
        REQUIRE(row.ts == 1'700'000'000'000LL);
        REQUIRE(row.cpu_total_percent.value() == 42.5);
        REQUIRE(row.ram_percent.value() == 46.6);
        REQUIRE(row.ram_used_bytes.value() == 16'000'000'000LL);
        REQUIRE(row.swap_percent.value() == 12.5);
        REQUIRE(row.disk_read_bps.value() == 5'000'000LL);
        REQUIRE(row.disk_write_bps.value() == 3'000'000LL);
    }

    SECTION("disk_game_free_bytes = first volume's free bytes") {
        REQUIRE(row.disk_game_free_bytes.value() == 123'000'000'000LL);
    }

    SECTION("net totals sum across all NICs") {
        // sent = 1000 + 100, recv = 2000 + 200
        REQUIRE(row.net_sent_bps.value() == 1'100LL);
        REQUIRE(row.net_recv_bps.value() == 2'200LL);
        // errs = (1+2+3+4) + (10+20+30+40) = 10 + 100 = 110
        REQUIRE(row.net_errs_total.value() == 110LL);
    }

    SECTION("not-yet-wired fields are NULL this step") {
        REQUIRE_FALSE(has(row.pages_per_sec));             // step 04 (PDH)
        REQUIRE_FALSE(has(row.disk_queue_length));         // step 04 (PDH)
        REQUIRE_FALSE(has(row.disk_avg_sec_per_transfer)); // step 04 (PDH)
        REQUIRE_FALSE(has(row.temp_cpu_celsius));          // step 05 (LHM)
        REQUIRE_FALSE(has(row.whea_count_2h));             // step 04 (WHEA)
    }

    SECTION("fika stub: spt fields NULL, headless fields 0") {
        REQUIRE_FALSE(has(row.fika_spt_cpu_percent));      // NULL until step 06
        REQUIRE_FALSE(has(row.fika_spt_rss_bytes));        // NULL until step 06
        REQUIRE(row.fika_headless_count.value() == 0);     // 0 until step 06
        REQUIRE(row.fika_headless_cpu_total.value() == 0.0);
        REQUIRE(row.fika_headless_rss_total.value() == 0);
    }
}

TEST_CASE("A minimal snapshot (only ts) yields NULL/0 everywhere else",
          "[collector]") {
    raidwatch::RwSnapshot s;
    s.ts = 99;

    const auto row = raidwatch::to_metrics_row(s);

    REQUIRE(row.ts == 99);
    REQUIRE_FALSE(has(row.cpu_total_percent));
    REQUIRE_FALSE(has(row.ram_percent));
    REQUIRE_FALSE(has(row.ram_used_bytes));
    REQUIRE_FALSE(has(row.swap_percent));
    REQUIRE_FALSE(has(row.disk_read_bps));
    REQUIRE_FALSE(has(row.disk_write_bps));
    REQUIRE_FALSE(has(row.disk_game_free_bytes));   // no volumes -> NULL
    REQUIRE_FALSE(has(row.net_sent_bps));            // no NICs -> NULL
    REQUIRE_FALSE(has(row.net_recv_bps));
    REQUIRE_FALSE(has(row.net_errs_total));
    // Fika headless fields are the "0 until step 06" rule, not NULL.
    REQUIRE(row.fika_headless_count.value() == 0);
    REQUIRE(row.fika_headless_cpu_total.value() == 0.0);
    REQUIRE(row.fika_headless_rss_total.value() == 0);
}

TEST_CASE("disk_game_free_bytes always uses index 0 regardless of size",
          "[collector]") {
    raidwatch::RwSnapshot s;
    s.ts = 1;
    s.system.disk_volumes = {
        {"Z:\\", 100LL, 11LL},   // index 0 even though smallest
        {"C:\\", 999LL, 999LL},
    };
    const auto row = raidwatch::to_metrics_row(s);
    REQUIRE(row.disk_game_free_bytes.value() == 11LL);
}

TEST_CASE("A single NIC with no errors yields zero errs_total", "[collector]") {
    raidwatch::RwSnapshot s;
    s.ts = 1;
    raidwatch::RwNetNicStats n{};
    n.sent_bps = 500;
    n.recv_bps = 900;
    s.system.net_by_nic.emplace("eth0", n);

    const auto row = raidwatch::to_metrics_row(s);
    REQUIRE(row.net_sent_bps.value() == 500LL);
    REQUIRE(row.net_recv_bps.value() == 900LL);
    REQUIRE(row.net_errs_total.value() == 0LL);
}
TEST_CASE("cadence_seconds backs off after 5 consecutive failures (D8)",
          "[collector]") {
    using C = raidwatch::RwCollector;
    const double interval = 5.0;

    // Clean + below threshold: normal cadence.
    REQUIRE(raidwatch::cadence_seconds(0, interval) == interval);
    REQUIRE(raidwatch::cadence_seconds(4, interval) == interval);

    // At/above threshold: back off to ~60s.
    REQUIRE(raidwatch::cadence_seconds(5, interval) == C::kBackoffSeconds);
    REQUIRE(raidwatch::cadence_seconds(99, interval) == C::kBackoffSeconds);

    // Recovery: resetting the failure count restores the normal interval.
    REQUIRE(raidwatch::cadence_seconds(0, interval) == interval);
    REQUIRE(C::kBackoffThreshold == 5);
    REQUIRE(C::kBackoffSeconds == 60.0);
}
