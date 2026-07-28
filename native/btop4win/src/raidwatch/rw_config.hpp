// rw_config.hpp — RaidWatch settings loader (T4).
//
// Ports raidwatch/config.py (minus the dropped `server:` / `auth:` HTTP-only
// sections) to C++. Parses config.yaml with the vendored rapidyaml single
// header, validates the gate fields that are real config mistakes (D4), and
// auto-generates a first-run config from config.yaml.example (D23).
//
// Semantics ported 1:1 from the legacy pydantic model:
//   - Unknown keys WARN and are ignored (replaces pydantic `extra="forbid"` — a
//     hard failure is wrong for a local TUI whose config ships in-repo).
//   - Invalid gate operator/severity and negative durations are HARD errors
//     (real config mistakes); surfaced as RwConfigError.
//
// Timestamp / clock decisions live in rw_database; this layer is pure settings.

#pragma once

#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace raidwatch {

// Valid comparison operators for gate conditions (config.py::VALID_OPERATORS).
inline constexpr const char* kValidOperators[] = {">", "<", ">=", "<=", "==", "!="};

// Valid gate severities.
inline constexpr const char* kValidSeverities[] = {"low", "medium", "high"};

// Thrown on a real config error: bad gate operator/severity, negative
// duration, non-positive collection interval, malformed YAML, type mismatch.
class RwConfigError : public std::runtime_error {
public:
    explicit RwConfigError(const std::string& msg) : std::runtime_error(msg) {}
};

struct ProcessesCfg {
    std::string spt_server_process_name = "SPT.Server.exe";
    std::string headless_process_name = "EscapeFromTarkov.exe";
    // Regex (compiled lazily by the collector in a later step; here stored raw).
    std::string headless_cmdline_pattern = "--fika-headless";
};

struct CollectionCfg {
    double interval_seconds = 5.0;
    int history_retention_hours = 48;
    double whea_poll_seconds = 60.0;
    double top_others_poll_seconds = 15.0;
};

struct TempsCfg {
    std::string lhm_dll_path = "vendor/lhm/LibreHardwareMonitorLib.dll";
    std::string cpu_sensor_name;          // empty ⇒ gate stays disarmed
    double tctl_offset = 0.0;             // Zen3 (Ryzen 5 5500) reports true die temp; re-derive per chip
};

struct FikaCfg {
    std::string spt_path;                 // empty ⇒ Fika module disabled
    std::map<std::string, std::string> log_paths;  // source → path
    std::string headless_path;            // empty ⇒ headless health disabled
    int raid_udp_port = 25565;
    std::vector<std::string> risky_mod_names;
};

struct GateCfg {
    bool enabled = false;
    double threshold = 0.0;
    std::string op = ">";                 // operator (renamed — `operator` is a keyword)
    double duration_seconds = 0.0;
    std::string severity = "medium";
    std::string recommendation;
    std::string metric;                   // e.g. "system.ram_percent"
};

struct RwConfig {
    ProcessesCfg processes;
    CollectionCfg collection;
    TempsCfg temps;
    std::map<std::string, GateCfg> gates;
    FikaCfg fika;
};

// Load and validate configuration.
//
// Auto-generation (D23): if `config_path` does not exist, it is created from
// `example_path` (copied verbatim) when that exists, otherwise from the
// built-in default tree. The newly generated file is then parsed like any other.
//
// Unknown keys are warned to stderr and (optionally) collected in
// `unknown_keys_out` for testability; they never abort the load. Invalid gate
// operator/severity, negative duration, and non-positive interval throw
// RwConfigError.
//
// Paths default relative to the current working directory, matching the legacy
// layout (repo root = CWD): "data/config.yaml" + "config.yaml.example".
RwConfig load_config(const std::string& config_path = "data/config.yaml",
                     const std::string& example_path = "config.yaml.example",
                     std::vector<std::string>* unknown_keys_out = nullptr);

// Serialize a config tree back to YAML (used for the no-example auto-generate
// fallback and by tests). Block style, stable key order.
std::string dump_config_yaml(const RwConfig& cfg);

}  // namespace raidwatch
