// RaidWatch client — SSE live updates + REST pre-fill (D5/D25/D28).
// Vanilla JS + Chart.js; no build step required (D29).

"use strict";

// --- State ---
const MAX_POINTS = 720; // 1h @ 5s
const cpuHistory = [];
const ramHistory = [];
const diskReadHistory = [];
const diskWriteHistory = [];
const netSentHistory = [];
const netRecvHistory = [];
let cpuChart = null;
let ramChart = null;
let diskChart = null;
let netChart = null;
let eventSource = null;
let startedAt = null;
let lastHealth = null;     // most recent /health result (staleness source for D22 pill)
let activeGates = [];      // most recent /api/gates active (triggered) list

// --- Init ---
document.addEventListener("DOMContentLoaded", () => {
    initCharts();
    loadHistory();
    connectSSE();
    initKeyboard();
    initTheme();
    fetchHealth();
    fetchGates();
    document.getElementById("footer-version").textContent = "v" + (window.RW_VERSION || "0.1.0");
});

// --- Charts ---
function makeChart(canvasId, datasets, yMax = 100) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return null;
    return new Chart(canvas, {
        type: "line",
        data: { labels: [], datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            scales: {
                x: { display: false },
                y: {
                    beginAtZero: true,
                    max: yMax,
                    ticks: { color: "#94a3b8", font: { size: 10 } },
                    grid: { color: "#1e293b" },
                },
            },
            plugins: {
                legend: { display: datasets.length > 1, labels: { color: "#94a3b8", font: { size: 10 } } },
                tooltip: { backgroundColor: "#1e293b", titleColor: "#e2e8f0", bodyColor: "#94a3b8" },
            },
        },
    });
}

function initCharts() {
    cpuChart = makeChart("chart-cpu", [
        { label: "CPU %", data: [], borderColor: "#22c55e", borderWidth: 2, pointRadius: 0, tension: 0.3, fill: true, backgroundColor: "rgba(34,197,94,0.1)" },
    ]);
    ramChart = makeChart("chart-ram", [
        { label: "RAM %", data: [], borderColor: "#3b82f6", borderWidth: 2, pointRadius: 0, tension: 0.3, fill: true, backgroundColor: "rgba(59,130,246,0.1)" },
    ]);
    diskChart = makeChart("chart-disk", [
        { label: "Read KB/s", data: [], borderColor: "#22c55e", borderWidth: 1.5, pointRadius: 0, tension: 0.3 },
        { label: "Write KB/s", data: [], borderColor: "#f59e0b", borderWidth: 1.5, pointRadius: 0, tension: 0.3 },
    ], null);
    netChart = makeChart("chart-net", [
        { label: "Sent KB/s", data: [], borderColor: "#8b5cf6", borderWidth: 1.5, pointRadius: 0, tension: 0.3 },
        { label: "Recv KB/s", data: [], borderColor: "#06b6d4", borderWidth: 1.5, pointRadius: 0, tension: 0.3 },
    ], null);
}

function pushHistory(arr, val) {
    arr.push(val);
    if (arr.length > MAX_POINTS) arr.shift();
}

function updateChart(chart, ...datasets) {
    if (!chart) return;
    datasets.forEach((data, i) => { if (chart.data.datasets[i]) chart.data.datasets[i].data = [...data]; });
    chart.data.labels = datasets[0].map((_, i) => i);
    chart.update("none");
}

// --- REST pre-fill (D25) ---
async function loadHistory() {
    const minutes = parseInt(document.getElementById("range-select")?.value || "60");
    try {
        const resp = await fetch(`/api/metrics/history?minutes=${minutes}`);
        const json = await resp.json();
        if (!json.ok || !json.data?.length) return;
        const rows = json.data;
        cpuChart && (cpuChart.data.datasets[0].data = rows.map(r => r.cpu_total_percent || 0));
        cpuChart && (cpuChart.data.labels = rows.map(() => ""));
        cpuChart && cpuChart.update("none");
        ramChart && (ramChart.data.datasets[0].data = rows.map(r => r.ram_percent || 0));
        ramChart && (ramChart.data.labels = rows.map(() => ""));
        ramChart && ramChart.update("none");
    } catch (e) {
        console.warn("History pre-fill failed:", e);
    }
}

// --- SSE (D5/D25/D28) ---
function connectSSE() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource("/api/stream", { withCredentials: true });

    eventSource.onopen = () => {
        console.log("SSE connected");
        fetchHealth();
        fetchGates();
    };
    eventSource.onerror = () => {
        console.log("SSE error — EventSource auto-reconnects");
        setStatusPill("degraded", "Reconnecting…");
    };
    eventSource.addEventListener("snapshot", (event) => {
        updateUI(JSON.parse(event.data));
    });
}

// --- UI update on each snapshot ---
function updateUI(snap) {
    document.getElementById("loading")?.classList.add("hidden");
    document.getElementById("dashboard")?.classList.remove("hidden");

    const sys = snap.system || {};
    const self = snap.self || {};

    // Status pill
    updateStatusPill();

    // CPU
    if (sys.cpu_total_percent != null) {
        document.getElementById("card-cpu").textContent = sys.cpu_total_percent.toFixed(1) + "%";
        const cores = sys.cpu_per_core_percent || [];
        document.getElementById("card-cpu-cores").textContent = cores.length + " cores";
        setGauge("gauge-cpu", sys.cpu_total_percent, 100);
        pushHistory(cpuHistory, sys.cpu_total_percent);
        updateChart(cpuChart, cpuHistory);
    }

    // RAM
    if (sys.ram_percent != null) {
        document.getElementById("card-ram").textContent = sys.ram_percent.toFixed(1) + "%";
        document.getElementById("card-ram-detail").textContent =
            formatBytes(sys.ram_used_bytes) + " / " + formatBytes(sys.ram_total_bytes);
        setGauge("gauge-ram", sys.ram_percent, 100);
        pushHistory(ramHistory, sys.ram_percent);
        updateChart(ramChart, ramHistory);
    }

    // Temp (D9: warning if unvalidated)
    if (sys.temp_cpu_celsius != null) {
        const tempEl = document.getElementById("card-temp");
        tempEl.textContent = sys.temp_cpu_celsius.toFixed(1) + "°C";
        tempEl.className = "text-xl font-bold " + (sys.temp_cpu_celsius > 88 ? "text-red-400" : sys.temp_cpu_celsius > 80 ? "text-amber-400" : "text-green-400");
        document.getElementById("card-temp-detail").textContent = "Sensor active";
    } else {
        document.getElementById("card-temp").textContent = "N/A";
        document.getElementById("card-temp-detail").textContent = "Run probe_temps.py (D9)";
    }

    // Storage
    const volumes = sys.disk_volumes || [];
    if (volumes.length > 0) {
        const v = volumes[0];
        const freePct = v.total_bytes > 0 ? (v.free_bytes / v.total_bytes * 100) : 0;
        document.getElementById("card-storage-free").textContent = freePct.toFixed(0) + "% free";
        document.getElementById("card-storage-free").className = "text-xl font-bold " +
            (freePct < 10 ? "text-red-400" : freePct < 20 ? "text-amber-400" : "text-green-400");
    }
    if (sys.disk_read_bps != null || sys.disk_write_bps != null) {
        const totalIO = (sys.disk_read_bps || 0) + (sys.disk_write_bps || 0);
        document.getElementById("card-disk-io").textContent = "I/O: " + formatBytes(totalIO) + "/s";
        pushHistory(diskReadHistory, (sys.disk_read_bps || 0) / 1024);
        pushHistory(diskWriteHistory, (sys.disk_write_bps || 0) / 1024);
        updateChart(diskChart, diskReadHistory, diskWriteHistory);
    }
    if (sys.disk_queue_length != null) {
        document.getElementById("card-disk-queue").textContent = "Q: " + sys.disk_queue_length.toFixed(1);
    }

    // WHEA
    if (sys.whea_count_2h != null) {
        const el = document.getElementById("card-health");
        // WHEA shown in events or health card
    }

    // Network
    const nics = sys.net_by_nic || {};
    let totalSent = 0, totalRecv = 0;
    for (const nic in nics) { totalSent += nics[nic].sent_bps || 0; totalRecv += nics[nic].recv_bps || 0; }
    pushHistory(netSentHistory, totalSent / 1024);
    pushHistory(netRecvHistory, totalRecv / 1024);
    updateChart(netChart, netSentHistory, netRecvHistory);

    // Top processes
    const tbody = document.getElementById("process-table-body");
    if (tbody && snap.process?.top?.length) {
        tbody.innerHTML = snap.process.top.map(p =>
            `<tr class="border-b border-slate-700/50 hover:bg-slate-700/30">
                <td class="py-1.5 pr-4 text-slate-400">${p.pid}</td>
                <td class="pr-4 text-slate-200">${p.name}</td>
                <td class="text-right pr-4 text-slate-300">${p.cpu_percent.toFixed(1)}</td>
                <td class="text-right text-slate-400">${formatBytes(p.rss_bytes)}</td>
            </tr>`
        ).join("");
    }

    // Fika events (decorative; D3)
    const feed = document.getElementById("events-feed");
    if (feed && snap.fika?.events_recent?.length) {
        feed.innerHTML = snap.fika.events_recent.map(e => {
            const color = e.severity === "error" ? "text-red-400" : e.severity === "warn" ? "text-amber-400" : "text-slate-300";
            const time = new Date(e.ts).toLocaleTimeString();
            return `<div class="text-sm ${color}"><span class="text-slate-500">${time}</span> [${e.source}] ${e.message}</div>`;
        }).join("");
    }
}

// --- Gauges (D10: current-vs-threshold headroom) ---
function setGauge(id, value, max) {
    const el = document.getElementById(id);
    if (!el) return;
    const pct = Math.min(value / max * 100, 100);
    el.setAttribute("stroke-dashoffset", 100 - pct);
    // Color: green < 70%, amber < 88%, red ≥ 88%
    const ratio = value / max;
    const color = ratio >= 0.88 ? "#ef4444" : ratio >= 0.70 ? "#f59e0b" : "#22c55e";
    el.setAttribute("stroke", color);
}

// --- Status pill (D22: stale-core > High gate > Medium gate > Operational) ---
// Mirrors raidwatch/gates.py compute_status_pill(). Pill state derives from the
// cached /health (staleness) and /api/gates (triggered severity) — one source of truth.
function setStatusPill(statusClass, label) {
    const pill = document.getElementById("status-pill");
    if (!pill) return;
    const colors = {
        critical:    ["bg-red-900/50",   "text-red-300",   "border-red-700"],
        degraded:    ["bg-amber-900/50", "text-amber-300", "border-amber-700"],
        operational: ["bg-green-900/50", "text-green-300", "border-green-700"],
    };
    const tokens = colors[statusClass] || colors.operational;
    pill.className = "px-3 py-1 rounded-full text-xs md:text-sm font-medium border " + tokens.join(" ");
    pill.textContent = label;
}

function updateStatusPill() {
    // Wait for the first /health before driving the pill (preserves the initial
    // "Connecting…" state until real staleness data is available).
    if (lastHealth == null) return;

    const c = lastHealth.collector || {};
    const stale = lastHealth.status === "critical" ||
        (c.last_tick_age_seconds != null && c.last_tick_age_seconds > 15);
    if (stale) {
        setStatusPill("critical", "Monitoring Degraded");
        return;
    }
    const high = activeGates.find(g => g.severity === "high");
    if (high) {
        setStatusPill("critical", "Critical: " + high.gate_id);
        return;
    }
    const medium = activeGates.find(g => g.severity === "medium");
    if (medium) {
        setStatusPill("degraded", "Degraded: " + medium.gate_id);
        return;
    }
    setStatusPill("operational", "Operational");
}

async function fetchHealth() {
    try {
        const resp = await fetch("/health");
        const h = await resp.json();
        lastHealth = h;                       // staleness source for the D22 pill
        window.RW_VERSION = h.version;
        startedAt = h.started_at;
        if (startedAt) {
            const uptime = Math.floor((Date.now() - startedAt) / 1000);
            const h2 = Math.floor(uptime / 3600), m = Math.floor((uptime % 3600) / 60);
            document.getElementById("uptime-hint").textContent = `↑ ${h2}h ${m}m`;
        }
        updateStatusPill();
    } catch (e) { /* server may be starting */ }
}

// --- Gates (D22: triggered severity feeds the status pill) ---
async function fetchGates() {
    try {
        const resp = await fetch("/api/gates", { credentials: "same-origin" });
        // 401/403: auth required — the page may redirect to login; stay silent.
        if (resp.status === 401 || resp.status === 403) return;
        if (!resp.ok) return;
        const data = await resp.json();
        activeGates = Array.isArray(data.active) ? data.active : [];
        updateStatusPill();
    } catch (e) { /* network error — keep last known gates */ }
}

// --- Actions ---
function exportCSV() {
    window.open("/api/metrics/export.csv?minutes=1440", "_blank");
    toast("Exporting CSV (last 24h)…", "info");
}

function refreshNow() {
    loadHistory();
    fetchHealth();
    fetchGates();
    toast("Refreshed", "success");
}

// --- Keyboard shortcuts ---
function initKeyboard() {
    document.addEventListener("keydown", (e) => {
        if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
        switch (e.key) {
            case "?": document.getElementById("help-modal").classList.remove("hidden"); break;
            case "r": case "R": refreshNow(); break;
            case "Escape": document.getElementById("help-modal").classList.add("hidden"); break;
        }
    });
}

// --- Theme toggle (persisted) ---
function initTheme() {
    const saved = localStorage.getItem("rw-theme") || "dark";
    applyTheme(saved);
}

function toggleTheme() {
    const current = document.documentElement.classList.contains("dark") ? "dark" : "light";
    applyTheme(current === "dark" ? "light" : "dark");
}

function applyTheme(theme) {
    localStorage.setItem("rw-theme", theme);
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.body.classList.toggle("bg-slate-900", theme === "dark");
    document.body.classList.toggle("text-slate-100", theme === "dark");
    if (theme === "light") {
        document.body.classList.add("bg-slate-100", "text-slate-900");
    } else {
        document.body.classList.remove("bg-slate-100", "text-slate-900");
    }
}

// --- Toasts ---
function toast(message, type = "info") {
    const container = document.getElementById("toast-container");
    const colors = { info: "bg-blue-600", success: "bg-green-600", warning: "bg-amber-600", error: "bg-red-600" };
    const el = document.createElement("div");
    el.className = `${colors[type] || colors.info} text-white text-sm px-4 py-2 rounded-lg shadow-lg transition-opacity duration-300`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, 4000);
}

// --- Helpers ---
function formatBytes(bytes) {
    if (bytes == null || bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(Math.abs(bytes)) / Math.log(k));
    return parseFloat(bytes / Math.pow(k, i)).toFixed(1) + " " + sizes[i];
}
