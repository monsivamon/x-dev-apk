# 🛠️ X DEV APK ✨

Builds patched Twitter/X APKs using [Piko](https://github.com/crimera/piko) patches compiled directly from the `dev` branch.

**⏰ Runs automatically every day at 17:00 JST.**

📦 [Latest Build Download](https://github.com/monsivamon/x-dev-apk/releases/latest)

## 🚀 Features

- 📱 **Dual-Track Release (New X & Old X)**: Automatically builds both the latest **New X** track (`NewX patches`) and the legacy UI **Old X** track (`Old patches`) in a single release (`-dual`).
- 🧩 **Category-Aware Full Patching**: Dynamically extracts patch categories (`NewX patches` / `Old patches` + common patches) from the compiled Piko bundle and force-applies all matching patches.
- 🔧 **Clean Bundle Merging via APKEditor**: Merges split APK bundles (`.apkm`) with `APKEditor` prior to patching with Morphe CLI for maximum patch compatibility.
- 🛟 **Per-Track Fallback & Continue-on-Error**: A failed download or patch on one track does not block the other, and individual patch failures are bypassed via `--continue-on-error`.
- 📊 **Detailed Patch Report**: Writes per-track patch results (`Success` / `Failure`) to `patches-applied.txt` and marks newly added patches with `**NEW**`.
- 🛡️ **Multi-Stage Cloudflare Bypass & Slug Fallback**: Resolves APKMirror downloads via `curl_cffi`, `cloudscraper`, Google Translate relay / R2 direct CDN resolution, and headless Chromium fallback, plus direct slug URL probing.
- 🚫 **Stable Base APKs Only**: Skips `alpha` / `beta` X versions and targets stable `prod` / `release` builds.

## 📦 Release Assets

Every release includes:

| Asset | Description |
|---|---|
| `piko-dev-v{version}-{commit}-new-{slug}.apk` | **New X** track — latest supported X version patched with `NewX patches` + common patches. |
| `piko-dev-v{version}-{commit}-old-{slug}.apk` | **Old X** track — legacy UI X version patched with `Old patches` + common patches. |
| `patches-{commit}.mpp` | Compiled Piko patch bundle used for the build. |
| `patches-applied.txt` | Per-track sections listing applied patches with `Success` / `Failure`; new patches marked with `**NEW**`. |

### Which APK should I install?

| File identifier | Target |
|---|---|
| `-new-` | **New X** — Built on the latest X release (`12.45.x+`) with Piko's `NewX` patch set. |
| `-old-` | **Old X** — Built on the legacy UI X release (`12.19.x` / `12.9.x`) with Piko's classic `Old` patch set. |

Both APKs are pre-merged universal / arm64 builds. Pick `-new-` for the newest X features or `-old-` if you prefer the classic X layout and full legacy patch compatibility.

## ⚠️ Disclaimer

All patches included in this build are from the `dev` branch and are under active development.  
Stable operation is not guaranteed.  
Unexpected issues may occur. Please use at your own risk.

## 🙏 Credits

- 🛠️ [MorpheApp](https://github.com/MorpheApp) - Morphe CLI patcher
- 🛠️ [crimera/piko](https://github.com/crimera/piko) - Piko patches (`dev` branch)
- 🔧 [REAndroid/APKEditor](https://github.com/REAndroid/APKEditor) - Split APK bundle merger
- 💡 [j-hc](https://github.com/j-hc) - Inspired by j-hc's revanced builder template