import re
import subprocess

from apkmirror import Version
from utils import patch_apk


# Print a prefixed progress message for the APK stage
def log(message: str):
    print(f"[apk] {message}", flush=True)


# Extract patch metadata from a .mpp via the Morphe CLI
def extract_patches_metadata(cli_path: str, mpp_path: str) -> list:
    log(f"Extracting patch list from {mpp_path} via CLI...")

    cmd = [
        "java", "-jar", cli_path,
        "list-patches",
        f"--patches={mpp_path}",
        "-p", "-v",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        out = result.stdout
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to extract patches from CLI: {e.stderr}")
    except Exception as e:
        raise RuntimeError(f"Failed to execute CLI command: {e}")

    patches = []
    current_patch = None
    current_package = None
    in_versions = False

    for line in out.splitlines():
        s_line = line.strip()
        if not s_line:
            continue

        if s_line.startswith("Index:"):
            current_patch = {"name": "", "compatiblePackages": []}
            patches.append(current_patch)
            current_package = None
            in_versions = False

        elif s_line.startswith("Name:") and current_patch is not None:
            current_patch["name"] = s_line[5:].strip()

        elif s_line.startswith("Package name:"):
            pkg_name = s_line.split("Package name:", 1)[1].strip()
            current_package = {"name": pkg_name, "versions": []}
            if current_patch is not None:
                current_patch["compatiblePackages"].append(current_package)
            in_versions = False

        elif s_line.startswith("Compatible versions:"):
            in_versions = True

        elif in_versions and current_package is not None:
            if s_line[0].isdigit():
                current_package["versions"].append(s_line)
            else:
                in_versions = False

    if not patches:
        raise RuntimeError("Could not parse any patch data from CLI output")

    return patches


# Return patch names compatible with the given version
def get_patches_for_version(patches_list: list, package_name: str, target_version: str) -> list:
    patches = []
    for patch in patches_list:
        patch_name = patch.get("name")
        compat = patch.get("compatiblePackages")

        supports_version = False
        if not compat:
            supports_version = True
        elif isinstance(compat, dict) and package_name in compat:
            versions = compat[package_name]
            if not versions or target_version in versions:
                supports_version = True
        elif isinstance(compat, list):
            for pkg in compat:
                if isinstance(pkg, dict) and pkg.get("name") == package_name:
                    versions = pkg.get("versions", [])
                    if not versions or target_version in versions:
                        supports_version = True
                    break

        if supports_version and patch_name:
            patches.append(patch_name)

    return patches


# Extract the full Twitter patch list for the target version (dev branch)
def get_dev_patches(cli: str, patches: str, target_version: str) -> list[str]:
    patches_list = extract_patches_metadata(cli, patches)
    includes = get_patches_for_version(
        patches_list, "com.twitter.android", target_version
    )
    log(f"Found {len(includes)} dev patches for {target_version}")
    if not includes:
        raise RuntimeError("Morphe returned no dev patches for the target version")
    return list(dict.fromkeys(includes))


# Extract every patch name from a .mpp (no version filter).
# Used for NEW marking against the previous release.
def get_all_patch_names(cli: str, patches: str) -> list[str]:
    patches_list = extract_patches_metadata(cli, patches)
    names = [p["name"] for p in patches_list if p.get("name")]
    return list(dict.fromkeys(names))


# Apply patches to the APK and return patch names and statuses
def build_apks(
    latest_version: Version,
    apk: str,
    piko_commit: str,
    includes: list[str],
    suffix: str = "",
) -> tuple[list[str], dict[str, bool]]:
    patches = "bins/patches.mpp"
    cli = "bins/morphe-cli.jar"

    out_apk = f"piko-dev-v{latest_version.version}-{piko_commit[:7]}{suffix}.apk"
    log(f"Patching {apk} -> {out_apk} (may take 5-10 minutes)...")

    patch_statuses = patch_apk(
        cli,
        patches,
        apk,
        includes=includes,
        excludes=[],
        out=out_apk,
        continue_on_error=True,
    )

    succeeded = sum(1 for v in patch_statuses.values() if v)
    failed = len(patch_statuses) - succeeded
    log(f"Patching complete: {succeeded} succeeded, {failed} failed")

    return includes, patch_statuses