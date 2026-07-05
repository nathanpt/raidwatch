# RaidWatch — Setup Guide

## Quick Install (one command)

```powershell
# 1. Clone the repo to a non-OS drive
cd D:\Tools
git clone https://github.com/nathanpt/raidwatch.git
cd raidwatch

# 2. Run the installer (as Administrator)
.\install.ps1
```

That's it. The installer does everything:
- Checks for Python 3.12+ and .NET runtime
- Creates the virtualenv and installs dependencies
- Generates a strong auth token
- Installs the Windows service (SYSTEM via NSSM)
- Creates the firewall rule (LAN + Tailscale scoped)
- Secures the config file (SYSTEM + Admins only)
- Registers the health watchdog
- Starts the service and prints your login URL + token

When it finishes, open `http://localhost:8080` and log in with the displayed token.

### Firewall scope

The installer will prompt for your LAN subnet (or auto-detect it). It always
includes the Tailscale range (`100.64.0.0/10`). You can also pass it directly:

```powershell
.\install.ps1 -LanSubnet "192.168.1.0/24"
```

---

## Uninstall

```powershell
.\install.ps1 -Uninstall
```

Removes the service, firewall rule, and health watchdog. Your data and config
are preserved (in `data/`).

---

## Post-Install (Optional)

These steps enable the optional features. The dashboard works without them.

### CPU Temperatures (D9)

CPU temp displays from launch, but the `cpu_thermal` gate ships **disabled**
until you validate the sensor. To enable it:

```powershell
python scripts\probe_temps.py
```

This enumerates the LHM sensors on your CPU, shows the Tctl offset, and tells
you exactly what to put in `config.yaml`. After editing:

```powershell
Restart-Service RaidWatch
```

### Fika Process Discovery (D4)

To track SPT.Server and headless clients, confirm the process signature:

```powershell
python scripts\discover_processes.py
```

Then fill the results into `config.yaml` under `processes:` and `server.spt_path`,
and restart the service.

### Tune Gate Thresholds (D10)

Gates ship with **conservative** thresholds. After running a real raid, watch
the gauges and lower thresholds in `config.yaml` to match your actual headroom.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Python 3.12+ not found" | Install from [python.org](https://www.python.org/downloads/) (check "Add to PATH") |
| Temps show "N/A" | Needs .NET Framework 4.8+ (built into Windows 11) + the service running as SYSTEM. Confirm with `python scripts\probe_temps.py`, then set `temps.cpu_sensor_name` in `config.yaml` |
| "Monitoring Degraded" pill | Check `data\raidwatch.log` or `curl http://localhost:8080/health` |
| Can't access from another device | Verify firewall rule: `Get-NetFirewallRule -DisplayName "RaidWatch"` |
| Fika not configured | Set `server.spt_path` in `data\config.yaml`, then `Restart-Service RaidWatch` |
| Forgot token | It's in `data\config.yaml` → `auth.token` (ACL'd to Admins) |

### Logs

NSSM captures all output to `data\raidwatch.log` (auto-rotated at 10MB).

### Manual operations

```powershell
Stop-Service RaidWatch      # stop
Start-Service RaidWatch     # start
Restart-Service RaidWatch   # restart
Get-Service RaidWatch       # check status
```
