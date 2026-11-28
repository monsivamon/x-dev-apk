import subprocess

from apkmirror import Version
from utils import patch_apk

# Patches requiring mandatory custom options that fail without user input
EXCLUDED_PATCHES = set()


def log(message: str):
    print(f"[apk] {message}", flush=True)


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


# Extract all compatible com.twitter.android versions grouped by category from Morphe CLI output
def get_versions_from_mpp_cli(
    cli_path: str,
    mpp_path: str,
    patch_categories: dict[str, str | None],
) -> tuple[set[str], set[str]]:
    cli_patches = extract_patches_metadata(cli_path, mpp_path)
    old_vers: set[str] = set()
    new_vers: set[str] = set()

    for p in cli_patches:
        name = p.get("name")
        if not name:
            continue
        cat = patch_categories.get(name)
        for pkg in p.get("compatiblePackages") or []:
            if isinstance(pkg, dict) and pkg.get("name") == "com.twitter.android":
                for v in pkg.get("versions") or []:
                    if cat == "Old patches":
                        old_vers.add(v)
                    elif cat == "NewX patches":
                        new_vers.add(v)
                    else:
                        if v.startswith("12.9.1"):
                            old_vers.add(v)
                        else:
                            new_vers.add(v)

    return old_vers, new_vers


def get_patches_by_category(
    cli: str,
    patches_mpp: str,
    patch_categories: dict[str, str | None],
    is_old_x: bool,
) -> list[str]:
    target_category = "Old patches" if is_old_x else "NewX patches"
    excluded_category = "NewX patches" if is_old_x else "Old patches"

    cli_patches = extract_patches_metadata(cli, patches_mpp)
    selected: list[str] = []

    for p in cli_patches:
        name = p.get("name")
        if not name or name in EXCLUDED_PATCHES:
            continue

        compat = p.get("compatiblePackages") or []
        is_twitter_or_universal = (not compat) or any(
            isinstance(pkg, dict) and pkg.get("name") == "com.twitter.android"
            for pkg in compat
        )
        if not is_twitter_or_universal:
            continue

        cat = patch_categories.get(name)
        if cat == target_category:
            selected.append(name)
        elif cat != excluded_category:
            selected.append(name)

    result = list(dict.fromkeys(selected))
    log(
        f"Selected {len(result)} patches for "
        f"{'Old X (12.9.1)' if is_old_x else 'New X'} "
        f"(category='{target_category}' + common)"
    )
    if not result:
        raise RuntimeError(f"No patches found for category '{target_category}'")
    return result


def get_all_patch_names(cli: str, patches: str) -> list[str]:
    patches_list = extract_patches_metadata(cli, patches)
    names = [p["name"] for p in patches_list if p.get("name")]
    return list(dict.fromkeys(names))


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
        excludes=list(EXCLUDED_PATCHES),
        out=out_apk,
        continue_on_error=True,
    )

    succeeded = sum(1 for v in patch_statuses.values() if v)
    failed = len(patch_statuses) - succeeded
    log(f"Patching complete: {succeeded} succeeded, {failed} failed")

    return includes, patch_statuses