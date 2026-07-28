// rw_database.hpp — SQLite persistence layer (T5). Ports raidwatch/database.py.
//
// Single shared sqlite3* connection serialized on the collector thread (D21
// analog): no mutex — all access is single-threaded by construction. WAL is
// OFF (the legacy Python service used WAL because the async API had concurrent
// readers; the C++ collector has a single writer/reader, so the default rollback
// journal is simpler and correct — T5). Opens set a busy_timeout as a defensive
// belt-and-suspenders.
//
// Schema is the exact v1 DDL (5 tables + 4 indexes) migrated via idempotent
// `IF NOT EXISTS` statements and `PRAGMA user_version`. Timestamps are UTC
// epoch ms everywhere (D19).

#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

struct sqlite3;

namespace raidwatch {

// Current schema version. Bump when adding a migration (D32).
inline constexpr int kSchemaVersion = 1;

// One cycle's scalar metrics — the wide metrics_history row (D14). `ts` is the
// primary key; every other column is optional (absent => SQL NULL), matching the
// legacy dict-based insert where missing keys bound NULL.
struct MetricsRow {
    int64_t ts = 0;
    std::optional<double> cpu_total_percent;
    std::optional<double> ram_percent;
    std::optional<int64_t> ram_used_bytes;
    std::optional<double> swap_percent;
    std::optional<double> pages_per_sec;
    std::optional<int64_t> disk_read_bps;
    std::optional<int64_t> disk_write_bps;
    std::optional<double> disk_queue_length;
    std::optional<double> disk_avg_sec_per_transfer;
    std::optional<int64_t> disk_game_free_bytes;
    std::optional<int64_t> net_sent_bps;
    std::optional<int64_t> net_recv_bps;
    std::optional<int64_t> net_errs_total;
    std::optional<double> temp_cpu_celsius;
    std::optional<int64_t> whea_count_2h;
    std::optional<double> fika_spt_cpu_percent;
    std::optional<int64_t> fika_spt_rss_bytes;
    std::optional<int64_t> fika_headless_count;
    std::optional<double> fika_headless_cpu_total;
    std::optional<int64_t> fika_headless_rss_total;
};

class RwDatabaseError : public std::runtime_error {
public:
    explicit RwDatabaseError(const std::string& msg) : std::runtime_error(msg) {}
};

class RwDatabase {
public:
    explicit RwDatabase(std::string db_path);
    ~RwDatabase();

    RwDatabase(const RwDatabase&) = delete;
    RwDatabase& operator=(const RwDatabase&) = delete;

    // Open the connection, apply busy_timeout / journal pragmas, run migrations
    // up to kSchemaVersion. Idempotent (safe to call twice).
    void open();

    // Close the connection (also done by the destructor).
    void close();

    // UTC epoch milliseconds (D19).
    static int64_t now_ms();

    // Current PRAGMA user_version (kSchemaVersion after open()).
    int user_version();

    bool table_exists(const std::string& name);
    bool index_exists(const std::string& name);

    // INSERT OR REPLACE one metrics row (ts is the PK).
    void insert_metrics(const MetricsRow& row);

    // On-the-fly time-bucketed downsampling (D15). Returns one row per bucket
    // with `bucket_ts` plus the requested metric columns aggregated per the
    // fixed aggregate map (MAX/AVG/MIN). `metrics == nullptr` => all wide-table
    // columns. Requested column names are validated against the allow-list
    // (SQL-injection guard); an unknown name throws RwDatabaseError. NULL
    // aggregates are returned as NaN.
    std::vector<std::map<std::string, double>> query_history(
        int minutes = 60, const std::vector<std::string>* metrics = nullptr);

    // Bucket selector (ms) — caps output near ~720 points (D15).
    //   minutes >= 720 -> 120000, >= 360 -> 30000, else 5000.
    static int bucket_for_minutes(int minutes);

    // Column -> aggregate function. Public so tests can assert the exact map.
    static const std::map<std::string, std::string>& aggregate_map();

private:
    void run_migrations();
    void exec(const char* sql);

    std::string db_path_;
    sqlite3* db_ = nullptr;
};

}  // namespace raidwatch
