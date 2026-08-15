import re
import subprocess

from apkmirror import Version
from utils import patch_apk

# Morphe CLIからパッチメタデータを抽出する
def extract_patches_metadata(cli_path: str, mpp_path: str) -> list:
    print(f"  -> Extracting patch list dynamically from {mpp_path} via CLI...")

    cmd = [
        "java", "-jar", cli_path,
        "list-patches",
        f"--patches={mpp_path}",
        "-p", "-v"
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

        if s_line.startswith('Index:'):
            current_patch = {"name": "", "compatiblePackages": []}
            patches.append(current_patch)
            current_package = None
            in_versions = False

        elif s_line.startswith('Name:') and current_patch is not None:
            current_patch["name"] = s_line[5:].strip()

        elif s_line.startswith('Package name:'):
            pkg_name = s_line.split('Package name:', 1)[1].strip()
            current_package = {"name": pkg_name, "versions": []}
            if current_patch is not None:
                current_patch["compatiblePackages"].append(current_package)
            in_versions = False

        elif s_line.startswith('Compatible versions:'):
            in_versions = True

        elif in_versions and current_package is not None:
            if s_line[0].isdigit():
                current_package["versions"].append(s_line)
            else:
                in_versions = False

    if not patches:
        raise RuntimeError("Could not parse any patch data from CLI output")

    return patches

# 指定バージョンに互換性のあるパッチ名を取得する
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

# Twitter互換の全パッチリストを動的に抽出する（devブランチ用）
def get_dev_patches(cli: str, patches: str, target_version: str) -> list[str]:
    patches_list = extract_patches_metadata(cli, patches)

    includes = get_patches_for_version(
        patches_list,
        "com.twitter.android",
        target_version,
    )

    print("===== Extracted Twitter patches =====")
    print(includes)

    if not includes:
        raise RuntimeError("Morphe returned no dev patches")

    return list(dict.fromkeys(includes))

# APKに対して全パッチを適用し、パッチリストと成否辞書を返す
def build_apks(
    latest_version: Version,
    apk: str,
    piko_commit: str,
) -> tuple[list[str], dict[str, bool]]:
    patches = "bins/patches.mpp"
    cli = "bins/morphe-cli.jar"

    includes = get_dev_patches(cli, patches, latest_version.version)

    patch_statuses = patch_apk(
        cli,
        patches,
        apk,
        includes=includes,
        excludes=[],
        out=f"piko-dev-v{latest_version.version}-{piko_commit[:7]}.apk",
        continue_on_error=True,
    )

    return includes, patch_statuses