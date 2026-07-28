// rw_database.cpp — SQLite persistence layer (T5). See rw_database.hpp.

#include "rw_database.hpp"

#include "../../../third_party/sqlite3.h"

#include <chrono>
#include <cmath>
#include <cstring>
#include <sstream>
#include <type_traits>

namespace raidwatch {
namespace {

// ----------------------------------------------------------- v1 schema DDL --
// Ported verbatim from raidwatch/database.py::_MIGRATIONS (idempotent).
const char* kMigrations[] = {
    "CREATE TABLE IF NOT EXISTS metrics_history ("
    "  ts                          INTEGER PRIMARY KEY,"
    "  cpu_total_percent           REAL,"
    "  ram_percent                 REAL,"
    "  ram_used_bytes              INTEGER,"
    "  swap_percent                REAL,"
    "  pages_per_sec               REAL,"
    "  disk_read_bps               INTEGER,"
    "  disk_write_bps              INTEGER,"
    "  disk_queue_length           REAL,"
    "  disk_avg_sec_per_transfer   REAL,"
    "  disk_game_free_bytes        INTEGER,"
    "  net_sent_bps                INTEGER,"
    "  net_recv_bps                INTEGER,"
    "  net_errs_total              INTEGER,"
    "  temp_cpu_celsius            REAL,"
    "  whea_count_2h               INTEGER,"
    "  fika_spt_cpu_percent        REAL,"
    "  fika_spt_rss_bytes          INTEGER,"
    "  fika_headless_count         INTEGER,"
    "  fika_headless_cpu_total     REAL,"
    "  fika_headless_rss_total     INTEGER"
    ")",
    "CREATE TABLE IF NOT EXISTS fika_events ("
    "  id          INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  ts          INTEGER NOT NULL,"
    "  source      TEXT NOT NULL,"
    "  severity    TEXT NOT NULL,"
    "  message     TEXT NOT NULL,"
    "  raw_line    TEXT"
    ")",
    "CREATE TABLE IF NOT EXISTS gate_events ("
    "  id          INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  ts          INTEGER NOT NULL,"
    "  gate_id     TEXT NOT NULL,"
    "  action      TEXT NOT NULL,"
    "  value       REAL,"
    "  severity    TEXT"
    ")",
    "CREATE TABLE IF NOT EXISTS whea_events ("
    "  record_number   INTEGER PRIMARY KEY,"
    "  ts_generated    INTEGER NOT NULL,"
    "  event_id        INTEGER,"
    "  message         TEXT"
    ")",
    "CREATE TABLE IF NOT EXISTS gate_state ("
    "  gate_id                TEXT PRIMARY KEY,"
    "  last_crossed_monotonic REAL,"
    "  currently_triggered    INTEGER DEFAULT 0,"
    "  last_triggered_ts      INTEGER,"
    "  trigger_count          INTEGER DEFAULT 0"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_metrics_ts    ON metrics_history(ts)",
    "CREATE INDEX IF NOT EXISTS idx_fika_events_ts ON fika_events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_gate_events_ts ON gate_events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_whea_ts        ON whea_events(ts_generated)",
};

// Column → aggregate (D15). MAX for peaks, AVG for rates, MIN for free space.
const std::map<std::string, std::string>& agg_map_storage() {
    static const std::map<std::string, std::string> m = {
        {"cpu_total_percent", "MAX"}, {"ram_percent", "MAX"},
        {"ram_used_bytes", "MAX"}, {"swap_percent", "MAX"},
        {"pages_per_sec", "AVG"}, {"disk_read_bps", "AVG"},
        {"disk_write_bps", "AVG"}, {"disk_queue_length", "MAX"},
        {"disk_avg_sec_per_transfer", "MAX"}, {"disk_game_free_bytes", "MIN"},
        {"net_sent_bps", "AVG"}, {"net_recv_bps", "AVG"},
        {"net_errs_total", "MAX"}, {"temp_cpu_celsius", "MAX"},
        {"whea_count_2h", "MAX"}, {"fika_spt_cpu_percent", "AVG"},
        {"fika_spt_rss_bytes", "MAX"}, {"fika_headless_count", "MAX"},
        {"fika_headless_cpu_total", "AVG"}, {"fika_headless_rss_total", "MAX"},
    };
    return m;
}

[[nodiscard]] std::string sqlite_err(sqlite3* db, const std::string& ctx) {
    return ctx + ": " + (db ? sqlite3_errmsg(db) : "no handle");
}

void check(int rc, sqlite3* db, const std::string& ctx) {
    if (rc != SQLITE_OK && rc != SQLITE_DONE && rc != SQLITE_ROW)
        throw RwDatabaseError(sqlite_err(db, ctx));
}

template <class T>
void bind_opt(sqlite3_stmt* st, int idx, const std::optional<T>& v) {
    if (!v) { sqlite3_bind_null(st, idx); return; }
    if constexpr (std::is_integral_v<T>) sqlite3_bind_int64(st, idx, static_cast<int64_t>(*v));
    else sqlite3_bind_double(st, idx, *v);
}

}  // namespace

// --------------------------------------------------------------- lifecycle --

RwDatabase::RwDatabase(std::string db_path) : db_path_(std::move(db_path)) {}

RwDatabase::~RwDatabase() { close(); }

void RwDatabase::open() {
    if (db_) return;
    int rc = sqlite3_open_v2(db_path_.c_str(), &db_,
                             SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nullptr);
    if (rc != SQLITE_OK)
        throw RwDatabaseError(sqlite_err(db_, "sqlite3_open"));
    // Single-writer defensive settings (T5: WAL off — rollback journal).
    sqlite3_busy_timeout(db_, 5000);
    exec("PRAGMA journal_mode = DELETE");
    exec("PRAGMA synchronous = NORMAL");
    run_migrations();
}

void RwDatabase::close() {
    if (db_) { sqlite3_close_v2(db_); db_ = nullptr; }
}

void RwDatabase::exec(const char* sql) {
    char* err = nullptr;
    int rc = sqlite3_exec(db_, sql, nullptr, nullptr, &err);
    if (rc != SQLITE_OK) {
        std::string msg = err ? err : "unknown";
        sqlite3_free(err);
        throw RwDatabaseError(std::string(sql).substr(0, 60) + ": " + msg);
    }
}

void RwDatabase::run_migrations() {
    if (user_version() < kSchemaVersion) {
        for (const char* stmt : kMigrations) exec(stmt);
        std::string set = "PRAGMA user_version = " + std::to_string(kSchemaVersion);
        exec(set.c_str());
    }
}

int64_t RwDatabase::now_ms() {
    using namespace std::chrono;
    return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

// ----------------------------------------------------------------- queries --

int RwDatabase::user_version() {
    sqlite3_stmt* st = nullptr;
    check(sqlite3_prepare_v2(db_, "PRAGMA user_version", -1, &st, nullptr), db_, "user_version prepare");
    int v = 0;
    if (sqlite3_step(st) == SQLITE_ROW) v = sqlite3_column_int(st, 0);
    sqlite3_finalize(st);
    return v;
}

static bool object_exists(sqlite3* db, const std::string& type, const std::string& name) {
    sqlite3_stmt* st = nullptr;
    const char* sql = "SELECT 1 FROM sqlite_master WHERE type=? AND name=?";
    check(sqlite3_prepare_v2(db, sql, -1, &st, nullptr), db, "object_exists prepare");
    sqlite3_bind_text(st, 1, type.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(st, 2, name.c_str(), -1, SQLITE_TRANSIENT);
    bool found = sqlite3_step(st) == SQLITE_ROW;
    sqlite3_finalize(st);
    return found;
}

bool RwDatabase::table_exists(const std::string& name) { return object_exists(db_, "table", name); }
bool RwDatabase::index_exists(const std::string& name) { return object_exists(db_, "index", name); }

int RwDatabase::bucket_for_minutes(int minutes) {
    if (minutes >= 720) return 120000;
    if (minutes >= 360) return 30000;
    return 5000;
}

const std::map<std::string, std::string>& RwDatabase::aggregate_map() {
    return agg_map_storage();
}

// ------------------------------------------------------------------ writes --

void RwDatabase::insert_metrics(const MetricsRow& r) {
    if (!db_) throw RwDatabaseError("insert_metrics: database not open");
    const char* sql =
        "INSERT OR REPLACE INTO metrics_history ("
        " ts, cpu_total_percent, ram_percent, ram_used_bytes, swap_percent,"
        " pages_per_sec, disk_read_bps, disk_write_bps, disk_queue_length,"
        " disk_avg_sec_per_transfer, disk_game_free_bytes, net_sent_bps,"
        " net_recv_bps, net_errs_total, temp_cpu_celsius, whea_count_2h,"
        " fika_spt_cpu_percent, fika_spt_rss_bytes, fika_headless_count,"
        " fika_headless_cpu_total, fika_headless_rss_total"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)";
    sqlite3_stmt* st = nullptr;
    check(sqlite3_prepare_v2(db_, sql, -1, &st, nullptr), db_, "insert_metrics prepare");
    sqlite3_bind_int64(st, 1, r.ts);
    bind_opt(st, 2, r.cpu_total_percent);
    bind_opt(st, 3, r.ram_percent);
    bind_opt(st, 4, r.ram_used_bytes);
    bind_opt(st, 5, r.swap_percent);
    bind_opt(st, 6, r.pages_per_sec);
    bind_opt(st, 7, r.disk_read_bps);
    bind_opt(st, 8, r.disk_write_bps);
    bind_opt(st, 9, r.disk_queue_length);
    bind_opt(st, 10, r.disk_avg_sec_per_transfer);
    bind_opt(st, 11, r.disk_game_free_bytes);
    bind_opt(st, 12, r.net_sent_bps);
    bind_opt(st, 13, r.net_recv_bps);
    bind_opt(st, 14, r.net_errs_total);
    bind_opt(st, 15, r.temp_cpu_celsius);
    bind_opt(st, 16, r.whea_count_2h);
    bind_opt(st, 17, r.fika_spt_cpu_percent);
    bind_opt(st, 18, r.fika_spt_rss_bytes);
    bind_opt(st, 19, r.fika_headless_count);
    bind_opt(st, 20, r.fika_headless_cpu_total);
    bind_opt(st, 21, r.fika_headless_rss_total);
    int rc = sqlite3_step(st);
    sqlite3_finalize(st);
    check(rc, db_, "insert_metrics step");
}

std::vector<std::map<std::string, double>> RwDatabase::query_history(
    int minutes, const std::vector<std::string>* metrics) {
    if (!db_) throw RwDatabaseError("query_history: database not open");
    const auto& am = agg_map_storage();

    std::vector<std::string> cols;
    if (metrics) cols = *metrics;
    else for (const auto& [c, _] : am) cols.push_back(c);

    for (const auto& c : cols)
        if (am.find(c) == am.end())
            throw RwDatabaseError("Unknown metric column: " + c);

    std::ostringstream sql;
    sql << "SELECT (ts / ?) * ? AS bucket_ts";
    for (const auto& c : cols) sql << ", " << am.at(c) << "(" << c << ") AS " << c;
    sql << " FROM metrics_history WHERE ts >= ? GROUP BY bucket_ts ORDER BY bucket_ts";

    sqlite3_stmt* st = nullptr;
    check(sqlite3_prepare_v2(db_, sql.str().c_str(), -1, &st, nullptr), db_, "query_history prepare");
    int bucket = bucket_for_minutes(minutes);
    int64_t cutoff = now_ms() - static_cast<int64_t>(minutes) * 60000;
    sqlite3_bind_int(st, 1, bucket);
    sqlite3_bind_int(st, 2, bucket);
    sqlite3_bind_int64(st, 3, cutoff);

    std::vector<std::map<std::string, double>> out;
    while (sqlite3_step(st) == SQLITE_ROW) {
        std::map<std::string, double> row;
        row["bucket_ts"] = static_cast<double>(sqlite3_column_int64(st, 0));
        for (size_t i = 0; i < cols.size(); ++i) {
            int col = static_cast<int>(i + 1);
            if (sqlite3_column_type(st, col) == SQLITE_NULL)
                row[cols[i]] = std::nan("");
            else
                row[cols[i]] = sqlite3_column_double(st, col);
        }
        out.push_back(std::move(row));
    }
    sqlite3_finalize(st);
    return out;
}

}  // namespace raidwatch
