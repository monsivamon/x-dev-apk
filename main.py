import os
import re
import shutil
from pathlib import Path

import apkmirror
import github
from build_piko import PIKO_REPO, build_piko_patches
from build_variants import (
    build_apks,
    get_all_patch_names,
    get_patches_by_category,
    get_versions_from_mpp_cli,
)
from constants import REPO
from download_bins import download_apkeditor, download_morphe_cli
from utils import download, get_scraper, merge_apk, panic, publish_release

PATCHES_MPP = "bins/patches.mpp"
PATCHES_MPP_PREV = "bins/patches-prev.mpp"
PATCHES_APPLIED_TXT = "patches-applied.txt"
MORPHE_CLI = "bins/morphe-cli.jar"


def log(message: str):
    print(f"[main] {message}", flush=True)


def is_stable_version(version: str) -> bool:
    lower = version.lower()
    return "alpha" not in lower and "beta" not in lower


def is_old_x_version(version: str) -> bool:
    return version.startswith("12.9.1")


def version_sort_key(version: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", version)
    return tuple(int(n) for n in nums) if nums else (0,)


def arch_priority(architecture: str) -> int | None:
    a = (architecture or "").lower().strip()
    if "universal" in a:
        return 0
    if "arm64-v8a" in a and "armeabi-v7a" in a:
        return 1
    if "arm64-v8a" in a:
        return 2
    return None


def android_slug(min_android_text: str) -> str:
    text = (min_android_text or "unknown").strip()
    text = re.sub(r"^Android\s+", "", text, flags=re.IGNORECASE)
    text = text.replace(" ", "-")
    text = text.replace("+", "plus")
    return f"android-{text}"


def pick_best_bundle_variant(variants):
    candidates = []
    for v in variants:
        if not v.is_bundle:
            continue
        prio = arch_priority(v.architecture)
        if prio is None:
            continue
        candidates.append((prio, v.min_android, v))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def find_version_by_slug(supported_version: str):
    slug = "x-" + supported_version.replace(".", "-").replace("_", "-") + "-release"
    url = f"https://www.apkmirror.com/apk/x-corp/twitter/{slug}/"

    log(f"Trying slug URL: {url}")
    try:
        response = get_scraper().get(url)
    except Exception as exc:
        log(f"Slug fetch failed: {exc}")
        return None
    if response.status_code == 200:
        log(f"Slug hit: {url}")
        return apkmirror.Version(version=supported_version, link=url)
    log(f"Slug miss ({response.status_code}): {url}")
    return None


def find_target_version(top_versions, target_versions: list[str]):
    if not target_versions:
        return None, None

    target_set = set(target_versions)
    tried: set[str] = set()

    # Phase 1: check APKMirror top page list (already ordered newest first)
    for version in top_versions:
        if version.version not in target_set:
            continue
        if not is_stable_version(version.version):
            continue
        tried.add(version.version)

        log(f"Checking variants for {version.version}...")
        try:
            variants = apkmirror.get_variants(version)
        except Exception as exc:
            log(f"Failed to get variants for {version.version}: {exc}")
            continue

        best = pick_best_bundle_variant(variants)
        if best is not None:
            log(f"Selected {version.version}: {best.architecture} / {best.min_android_text} / {best.dpi_text}")
            return version, best

    # Phase 2: slug URL fallback sorted newest first
    remaining = [
        v for v in target_versions if v not in tried and is_stable_version(v)
    ]
    for ver_str in sorted(remaining, key=version_sort_key, reverse=True):
        log(f"{ver_str} not on top page, trying slug...")
        version = find_version_by_slug(ver_str)
        if version is None:
            continue

        log(f"Checking variants for {ver_str}...")
        try:
            variants = apkmirror.get_variants(version)
        except Exception as exc:
            log(f"Failed to get variants for {ver_str}: {exc}")
            continue

        best = pick_best_bundle_variant(variants)
        if best is not None:
            log(f"Selected {ver_str}: {best.architecture} / {best.min_android_text} / {best.dpi_text}")
            return version, best

    return None, None


def format_patch_list(patches, previous_patches, statuses=None):
    known = set(previous_patches or [])
    mark_new = previous_patches is not None
    lines = []
    for patch in patches:
        new_mark = "**NEW** " if mark_new and patch not in known else ""
        if statuses is not None:
            status = "Success" if statuses.get(patch, False) else "Failure"
            lines.append(f"- {new_mark}{patch} \u2014 {status}")
        else:
            lines.append(f"- {new_mark}{patch}")
    return "\n".join(lines)


def write_patches_applied(builds, previous_patches):
    sections: list[str] = []
    for build in builds:
        variant = build["variant"]
        label = build["label"]
        ver = build["version"].version
        header = (
            f"# {label} (v{ver}) | {variant.architecture} | {variant.dpi_text or 'unknown dpi'}"
        )
        body = format_patch_list(
            build["patches"], previous_patches, build["statuses"]
        )
        sections.append(f"{header}\n{body}")
    content = "\n\n".join(sections) + "\n"
    Path(PATCHES_APPLIED_TXT).write_text(content, encoding="utf-8")
    log(f"Wrote {len(builds)} build section(s) to {PATCHES_APPLIED_TXT}")


def find_previous_mpp_asset(previous_release):
    return next(
        (a for a in previous_release.assets if a.name.endswith(".mpp")),
        None,
    )


def get_previous_patches_from_release(previous_release):
    if previous_release is None:
        log("No previous release (first build)")
        return None

    mpp_asset = find_previous_mpp_asset(previous_release)
    if mpp_asset is None:
        return None

    log(f"Downloading previous mpp: {mpp_asset.name}")
    if os.path.exists(PATCHES_MPP_PREV):
        os.remove(PATCHES_MPP_PREV)
    download(mpp_asset.browser_download_url, PATCHES_MPP_PREV)

    log("Extracting patch list from previous mpp...")
    patches = get_all_patch_names(MORPHE_CLI, PATCHES_MPP_PREV)
    log(f"Previous patches: {len(patches)}")
    return patches


def get_piko_commits(previous_release, current_commit):
    if previous_release is None:
        return None
    previous_commit = previous_release.tag_name.rsplit("-", maxsplit=1)[-1]
    if not re.fullmatch(r"[0-9a-f]{7,40}", previous_commit):
        return None
    if previous_commit == current_commit[:7]:
        return []
    return github.get_commits_between(PIKO_REPO, previous_commit, current_commit)


def format_commit_list(commits):
    if not commits:
        return ""
    entries = "\n".join(
        f"- [`{c.sha[:7]}`]({c.html_url}) {c.subject}" for c in commits
    )
    return f"Piko commits since previous release:\n{entries}"


def format_variants_info(builds) -> str:
    rows = "\n".join(
        f"| {b['label']} | {b['version'].version} | {b['variant'].architecture} | {b['variant'].min_android_text} | {b['variant'].dpi_text} |"
        for b in builds
    )
    return (
        "### Applied builds\n\n"
        "| Track | Version | Architecture | Minimum Version | Screen DPI |\n"
        "|---|---|---|---|---|\n"
        f"{rows}"
    )


def process(targets: list[dict], piko_build, previous_release=None):
    piko_commit = piko_build.commit[:7]
    primary_ver = targets[0]["version"].version
    release_tag = f"{primary_ver}-{piko_commit}-dual"

    log("Getting previous patch list...")
    previous_patches = get_previous_patches_from_release(previous_release)

    built: list[dict] = []
    for t in targets:
        label = t["label"]
        tag_suffix = t["suffix"]
        version = t["version"]
        variant = t["variant"]
        is_old = t["is_old"]

        slug = android_slug(variant.min_android_text)
        apkm_path = f"big_file-{version.version}{tag_suffix}-{slug}.apkm"

        log(f"[{label}] Downloading v{version.version} ({variant.architecture}) -> {apkm_path}")
        try:
            apkmirror.download_apk(variant, path=apkm_path)
            if not os.path.exists(apkm_path):
                log(f"[{label}] Download missing after fetch: {apkm_path}")
                continue

            with open(apkm_path, "rb") as f:
                magic = f.read(4)
            if magic != b"PK\x03\x04":
                log(f"[{label}] Downloaded file is not a valid ZIP/APKM (magic={magic!r})")
                os.remove(apkm_path)
                continue
        except Exception as exc:
            log(f"[{label}] Download failed: {exc}")
            continue

        # Merge .apkm into single .apk using APKEditor before passing to Morphe CLI
        try:
            merged_apk_path = merge_apk(apkm_path)
            if not os.path.exists(merged_apk_path):
                log(f"[{label}] Merged APK not found: {merged_apk_path}")
                continue
        except Exception as exc:
            log(f"[{label}] APKEditor merge failed: {exc}")
            continue

        log(f"[{label}] Selecting patches (is_old={is_old})...")
        try:
            includes = get_patches_by_category(
                MORPHE_CLI,
                PATCHES_MPP,
                piko_build.patch_categories,
                is_old_x=is_old,
            )
            full_suffix = f"{tag_suffix}-{slug}"
            patches, patch_statuses = build_apks(
                version,
                merged_apk_path,
                piko_build.commit,
                includes=includes,
                suffix=full_suffix,
            )
            apk_name = f"piko-dev-v{version.version}-{piko_commit}{full_suffix}.apk"
            if not os.path.exists(apk_name):
                log(f"[{label}] Patched APK missing: {apk_name}")
                continue

            built.append(
                {
                    "label": label,
                    "version": version,
                    "variant": variant,
                    "apk_name": apk_name,
                    "patches": patches,
                    "statuses": patch_statuses,
                }
            )
            succeeded = sum(1 for v in patch_statuses.values() if v)
            log(f"[{label}] Built {apk_name}: {succeeded}/{len(patches)} patches succeeded")
        except Exception as exc:
            log(f"[{label}] Patching failed: {exc}")

    if not built:
        panic("All target builds failed")

    log(f"Writing {PATCHES_APPLIED_TXT}...")
    write_patches_applied(built, previous_patches)

    commit_list = format_commit_list(
        get_piko_commits(previous_release, piko_build.commit)
    )
    additional_notes = f"\n\n{commit_list}" if commit_list else ""

    variant_info = format_variants_info(built)

    message = f"""{variant_info}

Patch list: see `{PATCHES_APPLIED_TXT}` asset.{additional_notes}

Piko source:
[dev@{piko_commit}](https://github.com/crimera/piko/commit/{piko_build.commit})
"""

    mpp_name = f"patches-{piko_commit}.mpp"
    shutil.copy2(PATCHES_MPP, mpp_name)

    release_assets = [b["apk_name"] for b in built] + [mpp_name, PATCHES_APPLIED_TXT]
    log(f"Publishing release {release_tag} with {len(built)} APK(s)...")
    publish_release(
        release_tag,
        release_assets,
        message,
        release_tag,
    )
    log("Done.")


def main():
    log("Fetching X versions from APKMirror...")
    versions = apkmirror.get_versions(
        "https://www.apkmirror.com/apk/x-corp/twitter/"
    )
    log(f"Found {len(versions)} versions on top page")

    log("Building Piko patches (dev branch)...")
    piko_build = build_piko_patches()
    log(f"Piko build: {piko_build.commit[:7]}")

    log("Downloading APKEditor and morphe-cli.jar...")
    download_apkeditor()
    download_morphe_cli(include_prereleases=True)

    # Extract compatible versions directly from compiled .mpp via Morphe CLI
    cli_old_vers, cli_new_vers = get_versions_from_mpp_cli(
        MORPHE_CLI, PATCHES_MPP, piko_build.patch_categories
    )
    log(f"CLI extracted versions -> Old: {sorted(cli_old_vers)}, NewX: {sorted(cli_new_vers)}")

    old_candidates = sorted(
        (piko_build.old_versions | cli_old_vers | {v for v in piko_build.supported_versions if is_old_x_version(v)}),
        key=version_sort_key,
        reverse=True,
    )
    if not old_candidates:
        old_candidates = ["12.9.1-release.0"]

    new_candidates = sorted(
        (piko_build.new_versions | cli_new_vers | {v for v in piko_build.supported_versions if not is_old_x_version(v)}),
        key=version_sort_key,
        reverse=True,
    )

    log(f"Searching for New X (candidates: {new_candidates})...")
    new_ver, new_var = find_target_version(versions, new_candidates)

    log(f"Searching for Old X 12.9.1 (candidates: {old_candidates})...")
    old_ver, old_var = find_target_version(versions, old_candidates)

    targets: list[dict] = []
    if new_ver and new_var:
        targets.append({
            "label": "New X",
            "suffix": "-new",
            "version": new_ver,
            "variant": new_var,
            "is_old": False,
        })
    if old_ver and old_var:
        targets.append({
            "label": "Old X (12.9.1)",
            "suffix": "-old",
            "version": old_ver,
            "variant": old_var,
            "is_old": True,
        })

    if not targets:
        raise Exception("Neither New X nor Old X (12.9.1) could be resolved on APKMirror")

    primary_ver = targets[0]["version"].version
    release_tag = f"{primary_ver}-{piko_build.commit[:7]}-dual"
    log(f"Checking last release against {release_tag}...")
    last_build_version = github.get_last_build_version(REPO)
    if (
        last_build_version is not None
        and last_build_version.tag_name == release_tag
    ):
        log("No new compatible version/commit found")
        return

    log(f"Starting build for {len(targets)} target(s): {[t['label'] for t in targets]}")
    process(targets, piko_build, last_build_version)


if __name__ == "__main__":
    main()