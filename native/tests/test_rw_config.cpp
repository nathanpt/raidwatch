// test_rw_config.cpp — ports tests/test_config_validation.py to catch2 (T11).
// Covers the T4 DoD: auto-generate from example, warn-and-ignore unknown keys,
// hard-fail on a bad gate operator, tctl_offset default, and the real example.

#include "catch.hpp"
#include "rw_config.hpp"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>

namespace fs = std::filesystem;

namespace {
fs::path scratch_dir() {
    auto d = fs::temp_directory_path() / ("rwcfg_" + std::to_string(std::rand()));
    fs::remove_all(d);
    fs::create_directories(d);
    return d;
}
void write_file(const fs::path& p, const std::string& body) {
    fs::create_directories(p.parent_path());
    std::ofstream(p) << body;
}
// Path to the repo's real example, resolved from this source file's location
// (native/tests/ -> repo root).
fs::path real_example() {
    return fs::path(__FILE__).parent_path() / ".." / ".." / "config.yaml.example";
}
}  // namespace

TEST_CASE("First run auto-generates data/config.yaml from the example (D23)", "[config]") {
    auto d = scratch_dir();
    write_file(d / "example.yaml", "processes:\n  spt_server_process_name: \"X.exe\"\n"
                                   "fika:\n  spt_path: \"D:\\\\SP\"\n  raid_udp_port: 25565\n");
    auto target = d / "data" / "config.yaml";
    REQUIRE_FALSE(fs::exists(target));

    auto cfg = raidwatch::load_config(target.string(), (d / "example.yaml").string());
    REQUIRE(fs::exists(target));  // auto-generated
    REQUIRE(cfg.processes.spt_server_process_name == "X.exe");
    REQUIRE(cfg.fika.spt_path == "D:\\SP");
}

TEST_CASE("Missing example falls back to built-in defaults", "[config]") {
    auto d = scratch_dir();
    auto target = d / "data" / "config.yaml";
    auto cfg = raidwatch::load_config(target.string(), (d / "nope.yaml").string());
    REQUIRE(fs::exists(target));
    // Built-in defaults match config.py.
    REQUIRE(cfg.temps.tctl_offset == 20.0);
    REQUIRE(cfg.collection.interval_seconds == 5.0);
    REQUIRE(cfg.processes.spt_server_process_name == "SPT.Server.exe");
}

TEST_CASE("tctl_offset defaults to 20.0 when omitted", "[config]") {
    auto d = scratch_dir();
    auto p = d / "c.yaml";
    write_file(p, "collection:\n  interval_seconds: 5\n");
    auto cfg = raidwatch::load_config(p.string(), (d / "nope.yaml").string());
    REQUIRE(cfg.temps.tctl_offset == 20.0);
}

TEST_CASE("Unknown keys warn and are ignored (no failure)", "[config]") {
    auto d = scratch_dir();
    auto p = d / "c.yaml";
    write_file(p,
        "processes:\n  spt_server_process_name: \"A.exe\"\n"
        "  bogus_field: 123\n"            // unknown under a section
        "collection:\n  interval_seconds: 5\n"
        "totally_unknown_section: hi\n"); // unknown top-level
    std::vector<std::string> unk;
    auto cfg = raidwatch::load_config(p.string(), (d / "nope.yaml").string(), &unk);
    REQUIRE(cfg.processes.spt_server_process_name == "A.exe");
    REQUIRE(unk.size() == 2);  // one section-level + one top-level unknown key
}

TEST_CASE("Dropped HTTP sections (server/auth) are tolerated, not fatal", "[config]") {
    auto d = scratch_dir();
    auto p = d / "c.yaml";
    write_file(p, "server:\n  port: 8080\nauth:\n  token: x\ncollection:\n  interval_seconds: 5\n");
    std::vector<std::string> unk;
    auto cfg = raidwatch::load_config(p.string(), (d / "nope.yaml").string(), &unk);
    REQUIRE(cfg.collection.interval_seconds == 5.0);
    // server/auth are reported as unknown top-level keys, never fatal.
    REQUIRE_FALSE(unk.empty());
}

TEST_CASE("Invalid gate operator is a hard error (D4)", "[config]") {
    auto d = scratch_dir();
    auto p = d / "c.yaml";
    write_file(p, "gates:\n  ram_high:\n    threshold: 90\n    operator: \"=>\"\n");
    REQUIRE_THROWS_AS(
        raidwatch::load_config(p.string(), (d / "nope.yaml").string()),
        raidwatch::RwConfigError);
}

TEST_CASE("Invalid gate severity is a hard error", "[config]") {
    auto d = scratch_dir();
    auto p = d / "c.yaml";
    write_file(p, "gates:\n  ram_high:\n    threshold: 90\n    severity: critical\n");
    REQUIRE_THROWS_AS(
        raidwatch::load_config(p.string(), (d / "nope.yaml").string()),
        raidwatch::RwConfigError);
}

TEST_CASE("Negative gate duration is a hard error", "[config]") {
    auto d = scratch_dir();
    auto p = d / "c.yaml";
    write_file(p, "gates:\n  ram_high:\n    threshold: 90\n    duration_seconds: -5\n");
    REQUIRE_THROWS_AS(
        raidwatch::load_config(p.string(), (d / "nope.yaml").string()),
        raidwatch::RwConfigError);
}

TEST_CASE("Gate severity is normalized to lowercase", "[config]") {
    auto d = scratch_dir();
    auto p = d / "c.yaml";
    write_file(p, "gates:\n  ram_high:\n    threshold: 90\n    severity: HIGH\n");
    auto cfg = raidwatch::load_config(p.string(), (d / "nope.yaml").string());
    REQUIRE(cfg.gates.at("ram_high").severity == "high");
}

TEST_CASE("All six comparison operators are accepted", "[config]") {
    auto d = scratch_dir();
    const char* ops[] = {">", "<", ">=", "<=", "==", "!="};
    for (size_t i = 0; i < 6; ++i) {
        // Filename must avoid the operator chars ('>', '<', etc. are illegal on Windows).
        auto p = d / (std::string("op") + std::to_string(i) + ".yaml");
        write_file(p, std::string("gates:\n  g:\n    threshold: 1\n    operator: \"") + ops[i] + "\"\n");
        auto cfg = raidwatch::load_config(p.string(), (d / "nope.yaml").string());
        REQUIRE(cfg.gates.at("g").op == ops[i]);
    }
}

TEST_CASE("The shipped config.yaml.example parses cleanly (6 gates)", "[config]") {
    auto ex = real_example();
    if (!fs::exists(ex)) return;  // skip if the example isn't alongside the build
    auto d = scratch_dir();
    // Load by auto-generating a copy from the real example, then parse it.
    auto target = d / "data" / "config.yaml";
    auto cfg = raidwatch::load_config(target.string(), ex.string());
    REQUIRE(cfg.gates.size() == 6);
    for (const auto& [id, g] : cfg.gates) REQUIRE_FALSE(g.recommendation.empty());
    // storage_space uses the '<' operator (gate fires when free space is BELOW).
    REQUIRE(cfg.gates.at("storage_space").op == "<");
    // Fika fields relocated to the top-level fika: section (T4).
    REQUIRE(cfg.fika.raid_udp_port == 25565);
    REQUIRE_FALSE(cfg.fika.spt_path.empty());
    REQUIRE(cfg.fika.log_paths.count("server") == 1);
}
