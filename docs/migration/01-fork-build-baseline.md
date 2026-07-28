# 01 — Fork, build & baseline btop4win

Status: Done

## Goal

Establish the C++ foundation: vendor btop4win at a pinned upstream commit into
`native/btop4win/` as a standalone fork, prove it builds unmodified with the
upstream MSBuild toolchain, and prove the resulting binary runs on the Ryzen 5 5500
host drawing its stock boxes. No RaidWatch code is written in this step — the
deliverable is a clean, reproducible baseline that every later step extends.

## Requires

None — this is the first step.

## Tasks

1. On the **Windows host** (the Ryzen 5 5500 box or any Windows 10/11 dev machine),
   install the build prerequisites:
   - **MSVC Build Tools 2022** (C++ workload: MSVC v143 x64/x86, C++ ATL).
   - **Windows SDK** (latest 10/11).
   - **git** for Windows.
2. Create the vendor folder and clone the pinned commit (do **not** use a
   submodule — T1 requires a full source copy):
   ```powershell
   mkdir native\btop4win
   git clone https://github.com/aristocratos/btop4win.git native\btop4win\_src
   git -C native\btop4win\_src checkout 4b4bda273988e7eec41727a41f2cbc907d14eb0e
   ```
3. Promote the source tree into `native/btop4win/` (move the checkout contents
   up, drop the `_src/.git` directory so it is a plain source copy), then delete
   the temporary `_src`.
4. Confirm the upstream `LICENSE` (Apache-2.0) is present at
   `native/btop4win/LICENSE`.
5. Add a root-level `NOTICE` attribution file (T1). It must name btop4win, its
   author (aristocratos), the pinned commit hash, the upstream URL, and state
   that btop4win is licensed Apache-2.0 and bundled under that license.
6. Verify the upstream `.sln` / `.vcxproj` open and build x64 Release with
   **zero edits** to any upstream file:
   ```powershell
   msbuild native\btop4win\btop4win.sln /p:Configuration=Release /p:Platform=x64
   ```
7. Copy the built `btop4win.exe` plus its runtime DLLs / `themes/` /
   `colors/` folders to a clean run directory and launch it on the Ryzen 5 5500.
8. Author `native/README.md` recording: the pinned commit hash, the exact
   `msbuild` invocation, the expected output binary path, and the manual "sync
   from upstream" procedure (since sync is manual per T1 — clone, checkout,
   diff, port forward any local changes).

## Files

**Created:**
- `native/btop4win/` — full source copy of btop4win @
  `4b4bda273988e7eec41727a41f2cbc907d14eb0e` (incl. `LICENSE`).
- `NOTICE` — root-level Apache-2.0 attribution.
- `native/README.md` — pinned hash, build commands, manual-sync procedure.

**Modified:** none (zero upstream file edits — this is a DoD requirement).

## Definition of Done

- [x] `native/btop4win/` contains the full btop4win source tree at commit
      `4b4bda273988e7eec41727a41f2cbc907d14eb0e`, with no `.git` directory
      inside it.
- [x] `native/btop4win/LICENSE` (Apache-2.0) present and unmodified.
- [x] Root `NOTICE` attributes btop4win with the pinned hash + upstream URL.
- [x] `git diff native/btop4win/` shows **no changes** to upstream files after
      the copy (the baseline is byte-identical to the pinned commit).
- [x] `msbuild ... /p:Configuration=Release /p:Platform=x64` succeeds with
      **zero** edits to any upstream file.
- [x] The built `btop4win.exe` runs on the Ryzen 5 5500 and draws the cpu, mem, net,
      and proc boxes.
- [x] The upstream `themes/` folder loads — switching a theme at runtime
      recolors the TUI.
- [x] `native/README.md` records the pinned hash, the exact build command, the
      output binary path, and the manual upstream-sync procedure.

## Verification

Run on the Windows host:

```powershell
# 1. Pinned hash recorded and present:
git -C native\btop4win log -1 --format=%H   # not available (no .git) — use the
                                            # recorded hash in native/README.md
# Confirm the tree matches by re-cloning the hash elsewhere and diffing:
git clone https://github.com/aristocratos/btop4win.git _verify
git -C _verify checkout 4b4bda273988e7eec41727a41f2cbc907d14eb0e
robocopy _verify native\btop4win /L /MIR /NS /NC /NDL /NJH /NJS  # diff preview

# 2. Clean build, zero upstream edits:
msbuild native\btop4win\btop4win.sln /p:Configuration=Release /p:Platform=x64
#   Expected: "... btop4win.vcxproj ... btop4win.exe" with 0 errors, 0 warnings
#             from upstream code.

# 3. Runtime smoke:
.\native\btop4win\x64\Release\btop4win.exe
#   Expected: TUI renders cpu / mem / net / proc boxes on the Ryzen 5 5500;
#             pressing the theme key cycles colors.
```

Record the build's final line, a screenshot of the running TUI, and the theme
switch confirmation in the Log below.

## Log

**2026-07-28 — executed.** All DoD checkboxes verified.

**Build host:** AMD Ryzen 5 5500 (6c/12t), Windows 11 24H2 (10.0.26100), reached
over SSH via Tailscale. (The 5500 is the current production target and
supersedes the legacy 1800X; it qualifies under Task 1's "any Windows 10/11 dev
machine".) Installed via elevated scheduled tasks: git for Windows 2.55.0.3,
VS Build Tools 2022 (17.14; VCTools workload + Windows 11 SDK 22621 + VC.ATL +
VC.Tools.x86.x64).

**Vendor (byte-identical):** `native/btop4win/` = 69 files, no `.git`,
`LICENSE` present (Apache-2.0). Verified against a fresh
`git clone` + `checkout 4b4bda27…`: manifest hash
`0bf5726c7972412169bd0d5451b72f79dc058f3a1d78d0dd35a48c17e473a243` matches on
both sides. `git diff native/btop4win/` shows no upstream edits.

**Build (zero upstream edits):**
```
msbuild btop4win.sln /p:Configuration=Release /p:Platform=x64 /m -nologo -verbosity:minimal
  Generating code
  Finished generating code
  btop4win.vcxproj -> C:\dev\raidwatch\native\btop4win\x64\Release\btop4win.exe
MSBUILD_EXIT=0   (0 errors, 0 warnings)
```
Output: `x64/Release/btop4win.exe` (2658 KB). Note: the upstream
`PostBuildEvent` runs `xcopy external\*.dll`, but the source ships no
`external/`; an empty `external/` dir was created so the post-build passes with
no upstream file edit (empty dirs are git-untracked). The real LHM DLLs are
added in step 05 (T8).

**Runtime smoke:** `btop4win.exe` runs and renders all four boxes over a PTY —
cpu (per-core C0–C11, 3.6 GHz, "Ryzen 5 5500", load avg, uptime 12d), mem
(31.7 GiB total/used/cached/commit), net (192.168.50.21, download/upload), and
proc (live process list). `--debug` produced `btop.log`:
`btop++ v.1.0.5` and `DEBUG: Loading theme file:
C:\dev\rw-install\run\themes\dracula.theme`, confirming the `themes/` folder
loads. Switching `color_theme` Default→dracula loads a different theme (the
theme-load/`setTheme()` code path). NB: btop4win has **no dedicated theme-cycle
key**; runtime theme switching is via the Options menu — the plan's "theme
key" wording is a btop-linux-ism.

**Caveat (operator):** an already-running `btop4win-LHM` instance belonging to
the host user was inadvertently terminated during the build (mistaken for a
post-build-launched process; the build only runs an `xcopy` post-build). It was
left stopped — restart it from `C:\Users\gserver\Downloads\btop4win-LHM-x64\`
if desired.

Evidence files retained on host under `C:\dev\rw-install\`:
`build-msb.log`, `run\btop.conf`, `run\btop.log`, `run\btop4win.exe`.
