# native/ — vendored btop4win baseline

This directory holds the **btop4win** source that RaidWatch forks (migration
step 01). It is a byte-identical, full source copy of a single pinned upstream
commit — no submodule, no edits to any upstream file. New RaidWatch code is
added in *separate* translation units under `native/btop4win/src/raidwatch/`
in later steps, leaving upstream files untouched.

See the root `NOTICE` for attribution and licensing (Apache-2.0).

## Pinned commit

```
upstream : https://github.com/aristocratos/btop4win
commit   : 4b4bda273988e7eec41727a41f2cbc907d14eb0e
tag/ref  : master, 2025-10-12, btop4win v1.0.5
```

There is no `.git` directory inside `native/btop4win/` (by design — T1 requires
a plain source copy). The pinned hash above is the source of truth; verify a
copy against it with the procedure under [Verifying the
baseline](#verifying-the-baseline) below.

## Build prerequisites (Windows host only)

Per T2, the C++ build runs **only on Windows**. The Linux dev box cannot build
or run it. Required on the build host:

- **MSVC Build Tools 2022** — `VCTools` workload (MSVC v143, C++ ATL), plus
  `Microsoft.VisualStudio.Component.Windows11SDK.22621` (Windows 11 SDK) and
  `Microsoft.VisualStudio.Component.VC.Tools.x86.x64`.
- **git for Windows** (only needed to clone/sync; not required to build an
  already-checked-out tree).

Silent install (elevated), for reference:

```powershell
.\vs_BuildTools.exe --quiet --wait --norestart `
  --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended `
  --add Microsoft.VisualStudio.Component.Windows11SDK.22621 `
  --add Microsoft.VisualStudio.Component.VC.ATL `
  --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64
```

## Build (zero upstream edits)

From the `native/btop4win/` directory:

```powershell
msbuild btop4win.sln /p:Configuration=Release /p:Platform=x64
```

Locate MSBuild via `vswhere` (do not hard-code the path):

```powershell
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
& $vswhere -latest -products * -requires Microsoft.Component.MSBuild -find MSBuild\**\Bin\MSBuild.exe
```

`/m -nologo -verbosity:minimal` are optional for cleaner output. Expected
result on a clean tree: **0 errors, 0 warnings**.

**Output binary:** `native/btop4win/x64/Release/btop4win.exe` (~2.6 MB).

### Post-build note (external/ DLLs)

The upstream `.vcxproj` has a `PostBuildEvent` that runs
`xcopy /y /d "$(ProjectDir)external\*.dll" "$(OutDir)"` to copy the
LibreHardwareMonitor DLLs next to the exe. The pinned source **ships no
`external/` directory**, so on a fresh tree that xcopy has no source and the
post-build step errors (`MSB3073`), even though the exe links cleanly.

To get a clean `MSBUILD_EXIT=0` **without editing any upstream file**, create
an empty `external/` folder next to the project before building:

```powershell
New-Item -ItemType Directory -Force -Path native\btop4win\external
```

An empty directory is not tracked by git, so this does not alter the vendored
tree. The actual LHM DLLs are vendored separately in step 05 (T8) — do **not**
mix them with the repo's pythonnet LHM 0.9.6 set.

## Running the binary

btop4win is a full-screen terminal UI. Drop the built exe next to its runtime
data and launch it in a real console / RDP session (it renders via the Windows
console / ConPTY):

```powershell
# run dir with the exe + the themes/ folder beside it
mkdir run; copy x64\Release\btop4win.exe run\; copy themes run\themes\ -Recurse
.\run\btop4win.exe
```

The TUI draws the cpu / mem / net / proc boxes. Themes live in `themes/` (26
`.theme` files); the active theme is `color_theme` in the auto-generated
`btop.conf` next to the exe. **btop4win has no dedicated theme-cycle key** —
switching a theme at runtime is done through the Options menu
(`m` → Options → `color_theme`), which writes the config and regenerates the
theme live. Run with `--debug` to emit `btop.log` (records, e.g.,
`Loading theme file: <run>\themes\<name>.theme`).

## Manual sync from upstream (T1)

Upstream is near-dormant; syncing is manual. **Do not rebase the vendored copy
blindly** — later migration steps add new files under
`native/btop4win/src/raidwatch/`, which must be preserved across a sync.

```bash
# 1. Fetch the target upstream commit into a scratch checkout:
git clone https://github.com/aristocratos/btop4win.git /tmp/btop_new
git -C /tmp/btop_new checkout <NEW_UPSTREAM_COMMIT>

# 2. Diff against the vendored tree, ignoring upstream-internal noise:
diff -qr --exclude=.git /tmp/btop_new native/btop4win

# 3. Port upstream changes forward by hand:
#    - Replace changed UPSTREAM files (src/*.cpp, *.hpp, *.sln, *.vcxproj, …).
#    - PRESERVE any native/btop4win/src/raidwatch/* additions (separate TUs).
#    - Re-record the new pinned commit hash here and in the root NOTICE.
# 4. Rebuild + re-run the smoke (see above) before committing.
```

Update the pinned hash in **this file** and in the root **NOTICE** whenever the
sync lands a new commit.

## Verifying the baseline

Confirm the vendored tree is byte-identical to the pinned commit (no edits):

```bash
git clone https://github.com/aristocratos/btop4win.git /tmp/btop_verify
git -C /tmp/btop_verify checkout 4b4bda273988e7eec41727a41f2cbc907d14eb0e
# manifest hash must match (excludes .git):
( cd /tmp/btop_verify && find . -path ./.git -prune -o -type f -print | LC_ALL=C sort \
    | xargs -d '\n' sha256sum | sha256sum )
( cd native/btop4win && find . -type f -print | LC_ALL=C sort \
    | xargs -d '\n' sha256sum | sha256sum )
```

Both commands must print the same hash (recorded at step-01 completion:
`0bf5726c7972412169bd0d5451b72f79dc058f3a1d78d0dd35a48c17e473a243`, 69 files).
```
