// test_rw_database.cpp — ports tests/test_downsampling.py to catch2 (T11).
// Covers the T5 DoD: fresh-DB user_version=1, all 5 tables + 4 indexes, the
// insert->query roundtrip with correct aggregates, and bucket sizes.

#include "catch.hpp"
#include "rw_database.hpp"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <vector>

namespace fs = std::filesystem;

namespace {
fs::path scratch_db() {
    auto p = fs::temp_directory_path() / ("rwdb_" + std::to_string(std::rand()) + ".db");
    fs::remove(p);
    return p;
}
bool eq(double a, double b) { return std::fabs(a - b) < 1e-6; }
}  // namespace

TEST_CASE("Fresh DB reports user_version == 1", "[database]") {
    raidwatch::RwDatabase db(scratch_db().string());
    db.open();
    REQUIRE(db.user_version() == raidwatch::kSchemaVersion);
    REQUIRE(db.user_version() == 1);
}

TEST_CASE("All five tables exist on a fresh DB", "[database]") {
    raidwatch::RwDatabase db(scratch_db().string());
    db.open();
    for (const char* t :
         {"metrics_history", "fika_events", "gate_events", "whea_events", "gate_state"})
        REQUIRE(db.table_exists(t));
}

TEST_CASE("All four indexes exist on a fresh DB", "[database]") {
    raidwatch::RwDatabase db(scratch_db().string());
    db.open();
    for (const char* i :
         {"idx_metrics_ts", "idx_fika_events_ts", "idx_gate_events_ts", "idx_whea_ts"})
        REQUIRE(db.index_exists(i));
}

TEST_CASE("open() is idempotent and migrations are safe to re-run", "[database]") {
    raidwatch::RwDatabase db(scratch_db().string());
    db.open();
    db.open();  // second open must not throw or duplicate
    REQUIRE(db.user_version() == 1);
}

TEST_CASE("bucket_for_minutes caps output near ~720 points (D15)", "[database]") {
    using D = raidwatch::RwDatabase;
    REQUIRE(D::bucket_for_minutes(15) == 5000);    // 15m -> raw 5s
    REQUIRE(D::bucket_for_minutes(60) == 5000);    // 1h -> raw 5s
    REQUIRE(D::bucket_for_minutes(360) == 30000);  // 6h -> 30s
    REQUIRE(D::bucket_for_minutes(720) == 120000); // 24h -> 2m
    REQUIRE(D::bucket_for_minutes(1440) == 120000);
}

TEST_CASE("aggregate map selects MAX/AVG/MIN per column (D15)", "[database]") {
    const auto& m = raidwatch::RwDatabase::aggregate_map();
    REQUIRE(m.at("cpu_total_percent") == "MAX");
    REQUIRE(m.at("ram_percent") == "MAX");
    REQUIRE(m.at("pages_per_sec") == "AVG");
    REQUIRE(m.at("disk_read_bps") == "AVG");
    REQUIRE(m.at("disk_game_free_bytes") == "MIN");
    REQUIRE(m.at("net_errs_total") == "MAX");
    REQUIRE(m.size() == 20);
}

TEST_CASE("insert then query_history(60) roundtrips with correct aggregates", "[database]") {
    raidwatch::RwDatabase db(scratch_db().string());
    db.open();

    raidwatch::MetricsRow r;
    r.ts = raidwatch::RwDatabase::now_ms();
    r.cpu_total_percent = 42.5;
    r.ram_percent = 70.0;
    r.ram_used_bytes = 16'000'000'000LL;
    db.insert_metrics(r);

    auto rows = db.query_history(60);
    REQUIRE(rows.size() == 1);
    const auto& row = rows[0];
    REQUIRE(row.count("bucket_ts") == 1);
    REQUIRE(eq(row.at("cpu_total_percent"), 42.5));    // MAX
    REQUIRE(eq(row.at("ram_percent"), 70.0));          // MAX
    REQUIRE(eq(row.at("ram_used_bytes"), 16.0e9));     // MAX
}

TEST_CASE("Downsampling groups 5s rows into one 5s bucket for a 60m window", "[database]") {
    // Three rows 5s apart, all within the 60m window. With bucket=5000ms each
    // lands in its own bucket (ts/5000 distinct) — but the per-bucket aggregate
    // of a single row equals the row's value (no double-count).
    raidwatch::RwDatabase db(scratch_db().string());
    db.open();
    int64_t base = raidwatch::RwDatabase::now_ms() - 10 * 1000;
    double cpus[3] = {40.0, 60.0, 80.0};
    for (int i = 0; i < 3; ++i) {
        raidwatch::MetricsRow r;
        r.ts = base + i * 5000;
        r.cpu_total_percent = cpus[i];
        r.pages_per_sec = 100.0;  // AVG
        db.insert_metrics(r);
    }
    auto rows = db.query_history(60);
    REQUIRE(rows.size() == 3);  // three distinct 5000ms buckets
    // Each bucket's MAX equals the single inserted value.
    std::vector<double> got;
    for (const auto& row : rows) got.push_back(row.at("cpu_total_percent"));
    std::sort(got.begin(), got.end());
    REQUIRE(eq(got[0], 40.0));
    REQUIRE(eq(got[1], 60.0));
    REQUIRE(eq(got[2], 80.0));
}

TEST_CASE("MAX vs AVG across a single bucket", "[database]") {
    // Force two rows into the SAME bucket by giving them the same ts (PK forces
    // a replace). Instead use ts that share ts/5000 but differ: pick base so
    // base and base+1 land in the same 5000 bucket.
    raidwatch::RwDatabase db(scratch_db().string());
    db.open();
    int64_t base = raidwatch::RwDatabase::now_ms() - 1000;  // well inside 60m
    double vals[2] = {10.0, 30.0};
    for (int i = 0; i < 2; ++i) {
        raidwatch::MetricsRow r;
        r.ts = base + i;  // base and base+1 -> same ts/5000 bucket
        r.cpu_total_percent = vals[i];  // MAX -> 30
        r.pages_per_sec = vals[i];      // AVG -> 20
        db.insert_metrics(r);
    }
    auto rows = db.query_history(60);
    REQUIRE(rows.size() == 1);
    REQUIRE(eq(rows[0].at("cpu_total_percent"), 30.0));  // MAX
    REQUIRE(eq(rows[0].at("pages_per_sec"), 20.0));      // AVG
}

TEST_CASE("Unknown metric column is rejected (SQL-injection guard)", "[database]") {
    raidwatch::RwDatabase db(scratch_db().string());
    db.open();
    std::vector<std::string> bad = {"evil; DROP TABLE metrics_history--", "ts", "not_a_column"};
    for (const auto& c : bad) {
        std::vector<std::string> cols = {c};
        REQUIRE_THROWS_AS(db.query_history(60, &cols), raidwatch::RwDatabaseError);
    }
}

TEST_CASE("Column filter returns only requested metrics", "[database]") {
    raidwatch::RwDatabase db(scratch_db().string());
    db.open();
    raidwatch::MetricsRow r;
    r.ts = raidwatch::RwDatabase::now_ms();
    r.cpu_total_percent = 12.0;
    r.ram_percent = 34.0;
    db.insert_metrics(r);
    std::vector<std::string> cols = {"cpu_total_percent"};
    auto rows = db.query_history(60, &cols);
    REQUIRE(rows.size() == 1);
    REQUIRE(rows[0].count("cpu_total_percent") == 1);
    REQUIRE(rows[0].count("ram_percent") == 0);
}
