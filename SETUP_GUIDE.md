# RaidWatch — Setup Guide

Step-by-step deployment for Windows 11 IoT LTSC (spec §6). Follow in order.

## Prerequisites

1. **Windows 11 IoT LTSC** (debloated, AM4/LAN/AIO drivers, updates applied).
2. Static IP or Tailscale configured.
3. **.NET runtime** installed (required by pythonnet/LibreHardwareMonitor; D30).
   - Install the .NET Desktop Runtime 8.x from Microsoft.
4. **Python 3.12+** installed (added to PATH).

## Installation

### Step 1 — Copy project + create venv

```powershell
# Copy project to a non-OS drive
mkdir D:\Tools\RaidWatch
# Copy all project files here (including vendor/lhm/, nssm.exe)

cd D:\Tools\RaidWatch
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Step 2 — First run (foreground test)

On first run with no `config.yaml`, it **auto-generates** with safe defaults (D23).
System metrics work immediately; Fika starts disabled.

```powershell
python main.py
```

Open `http://localhost:8080` in a browser:
- Login page appears → enter the token from `data/config.yaml` (`auth.token`).
- Dashboard loads with live CPU/RAM/disk metrics.
- Fika/temps/WHEA show "unavailable" or "N/A" until configured.

Verify `/health` returns `"status": "operational"`:
```powershell
curl http://localhost:8080/health
```

Press `Ctrl+C` to stop.

### Step 3 — Install as a service (D18)

Run the install script as **Administrator**:

```powershell
# Edit $LanSubnets in the script to match your network first!
powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1
```

This will:
- Install RaidWatch as a **SYSTEM** service via NSSM (needed for LHM; D9/D31)
- Set SCM restart on 1st/2nd/3rd failure (D18)
- **Generate a strong auth token** (replacing the CHANGE_ME placeholder; D13)
- **ACL `config.yaml`** to SYSTEM + Administrators only (D33)
- Create a firewall rule scoped to LAN + Tailscale (D11)
- Register an external `/health` watchdog (D27)

Save the generated token — you need it to log in.

### Step 4 — Firewall verification (D11)

The install script creates a firewall rule. Verify it scopes reachability:

```powershell
Get-NetFirewallRule -DisplayName "RaidWatch" | Get-NetFirewallAddressFilter
```

**Edit `$LanSubnets`** in `install_service.ps1` to match your network. Guest/IoT
VLANs should be excluded.

### Step 5 — Access from another device

From your gaming PC:
- LAN: `http://<host-ip>:8080`
- Tailscale: `http://<tailscale-ip>:8080`

Log in once with the token (cookie persists ~90 days; D24).

## Post-Install Configuration

### Step 6 — Temperature validation (D9)

**CPU temp displays from launch but the `cpu_thermal` gate is DISABLED.**
To arm it:

```powershell
python scripts\probe_temps.py
```

This enumerates LHM sensors on the 1800X, dumps names/values, and observes the
Zen1 Tctl +20°C offset. Fill the results into `config.yaml`:

```yaml
temps:
  cpu_sensor_name: "Tctl"        # from probe output
  tctl_offset: 20                # Zen1 (1800X)
gates:
  cpu_thermal:
    enabled: true                # arm after validation
```

Restart the service: `Restart-Service RaidWatch`

### Step 7 — Process discovery (D4)

Identify the headless client launch arg from your WATCHDOG/Fika setup:

```powershell
python scripts\discover_processes.py
```

Fill the result into `config.yaml`:

```yaml
processes:
  headless_cmdline_pattern: "--fika-headless"  # confirm from output
```

### Step 8 — Configure Fika paths (D23)

Edit `config.yaml`:

```yaml
server:
  spt_path: "D:\\SPTarkov"
  log_paths:
    server: "D:\\SPTarkov\\BepInEx\\LogOutput.log"
    fika: "D:\\SPTarkov\\user\\mods\\fika-server\\logs\\fika.log"
```

Restart the service to activate the Fika module.

### Step 9 — Baseline + tune gates (D10)

Gates ship with **conservative** thresholds. After observing a real raid:

1. Launch SPT + Fika, join a raid with bots.
2. Watch CPU/RAM/storage gauges during bot spawns.
3. Confirm conservative gates don't false-positive.
4. Lower thresholds in `config.yaml` to your real headroom.

Example tuning:
```yaml
gates:
  ram_high:
    threshold: 82         # lower from 90 after baselining
  cpu_sustained:
    threshold: 75         # lower from 88 after baselining
```

## Maintenance

### Update

```powershell
Stop-Service RaidWatch
git pull  # or replace files
pip install -U -r requirements.txt
Start-Service RaidWatch
```

### Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall_service.ps1
```

### Troubleshooting

- **Logs:** NSSM captures stdout/stderr to `data/raidwatch.log` (D26).
- **Health:** `curl http://localhost:8080/health` — check `collector.last_tick_age_seconds`.
- **"Monitoring Degraded" pill:** collector is stale — check `/health` and service log.
- **Temps N/A:** Run `probe_temps.py` as SYSTEM; verify .NET runtime + DLL path.
- **Fika not configured:** Set `server.spt_path` in `config.yaml` and restart.
