import json
import os
from pathlib import Path

import apkmirror
import github
from apkmirror import Variant, Version
from build_piko import PIKO_REPO, PikoBuild, build_piko_patches
from build_variants import build_apks
from constants import REPO
from download_bins import download_morphe_cli
from utils import get_scraper, panic, publish_release

PATCHES_LIST_ASSET = "patches-list.json"
PATCHES_MPP = "bins/patches.mpp"

# サポート対象の最新バージョンを取得する（トップページの一覧から）
def get_latest_version(
    versions: list[Version], supported_versions: frozenset[str] | None = None
) -> Version | None:
    for version in versions:
        if supported_versions is None or version.version in supported_versions:
            return version

# Pikoが要求するバージョン文字列からAPKMirrorのURLスラッグを組み立てて直接アクセスする
def find_version_by_slug(supported_version: str) -> Version | None:
    # "12.19.1-release.0" -> "x-12-19-1-release-0-release"
    slug = "x-" + supported_version.replace(".", "-").replace("_", "-") + "-release"
    url = f"https://www.apkmirror.com/apk/x-corp/twitter/{slug}/"

    print(f"[debug] trying slug URL: {url}")

    response = get_scraper().get(url)
    if response.status_code == 200:
        print(f"[debug] slug URL hit: {url}")
        return Version(version=supported_version, link=url)

    print(f"[debug] slug URL miss ({response.status_code}): {url}")
    return None

# トップページで見つからなかった場合、スラッグURLを順に試す
def resolve_latest_version_by_slug(
    supported_versions: frozenset[str],
) -> Version | None:
    # 新しいバージョンから順に試す
    for supported in sorted(supported_versions, reverse=True):
        version = find_version_by_slug(supported)
        if version is not None:
            return version
    return None

# APKバンドルを選択する
def get_bundle_variant(variants: list[Variant]) -> Variant | None:
    universal_bundle = next(
        (
            variant
            for variant in variants
            if variant.is_bundle and variant.architecture == "universal"
        ),
        None,
    )
    if universal_bundle is not None:
        return universal_bundle

    return next((variant for variant in variants if variant.is_bundle), None)

# パッチ一覧をSuccess/False付きMarkdown形式に整形する
def format_patch_list(
    patches: list[str],
    previous_patches: list[str] | None,
    statuses: dict[str, bool] | None = None,
) -> str:
    known_patches = set(previous_patches or [])
    mark_new_patches = previous_patches is not None

    lines = []
    for patch in patches:
        new_mark = "**NEW** " if mark_new_patches and patch not in known_patches else ""

        if statuses is not None:
            status = "Success" if statuses.get(patch, False) else "False"
            lines.append(f"- {new_mark}{patch} — {status}")
        else:
            lines.append(f"- {new_mark}{patch}")

    return "\n".join(lines)

# パッチ一覧をJSONファイルに書き出す
def write_patches_list(patches: list[str]) -> None:
    Path(PATCHES_LIST_ASSET).write_text(
        json.dumps(patches, indent=2) + "\n",
        encoding="utf-8",
    )

# 前回リリース以降のPikoコミット一覧を取得する
def get_piko_commits(
    previous_release: github.GithubRelease | None, current_commit: str
) -> list[github.GithubCommit] | None:
    if previous_release is None:
        return None

    previous_commit = previous_release.tag_name.rsplit("-", maxsplit=1)[-1]
    if previous_commit == current_commit[:7]:
        return []

    return github.get_commits_between(PIKO_REPO, previous_commit, current_commit)

# コミットリストをMarkdown形式に整形する
def format_commit_list(commits: list[github.GithubCommit] | None) -> str:
    if not commits:
        return ""

    entries = "\n".join(
        f"- [`{commit.sha[:7]}`]({commit.html_url}) {commit.subject}"
        for commit in commits
    )
    return f"Piko commits since previous release:\n{entries}"

# APKのダウンロードからパッチ適用、リリース発行までを実行する
def process(
    latest_version: Version,
    piko_build: PikoBuild,
    previous_release: github.GithubRelease | None = None,
):
    print("===== STEP 4: Download APK variants =====")
    variants: list[Variant] = apkmirror.get_variants(latest_version)
    print(f"[debug] variants: {variants}")

    download_link = get_bundle_variant(variants)
    if download_link is None:
        raise Exception("APK bundle not found")
    print(f"[debug] selected bundle variant: {download_link}")

    print("===== STEP 5: Download APK =====")
    apk_path = f"big_file-{latest_version.version}.apkm"
    apkmirror.download_apk(download_link, path=apk_path)
    if not os.path.exists(apk_path):
        panic("Failed to download apkm")
    print(f"[debug] apk downloaded: {apk_path}")

    print("===== STEP 6: Download Morphe CLI =====")
    download_morphe_cli(include_prereleases=True)

    piko_commit = piko_build.commit[:7]
    release_tag = f"{latest_version.version}-{piko_commit}"
    apk_name = f"piko-dev-v{latest_version.version}-{piko_commit}.apk"

    print(f"Using Piko dev@{piko_commit}")

    print("===== STEP 7: Apply patches =====")
    patches, patch_statuses = build_apks(latest_version, apk_path, piko_build.commit)
    print(f"[debug] patches applied: {len(patches)}")

    write_patches_list(patches)

    previous_patches = (
        github.get_release_asset_json(previous_release, PATCHES_LIST_ASSET)
        if previous_release is not None
        else None
    )

    patch_list = format_patch_list(patches, previous_patches, patch_statuses)

    commit_list = format_commit_list(
        get_piko_commits(previous_release, piko_build.commit)
    )
    additional_notes = commit_list
    additional_notes = f"\n\n{additional_notes}" if additional_notes else ""

    message = f"""Patches applied:
{patch_list}{additional_notes}

Piko source:
[dev@{piko_commit}](https://github.com/crimera/piko/commit/{piko_build.commit})
"""

    print("===== STEP 8: Publish release =====")
    publish_release(
        release_tag,
        [apk_name, PATCHES_MPP, PATCHES_LIST_ASSET],
        message,
        release_tag,
    )

# 最新のXバージョンを確認し、必要ならビルドを開始する
def main():
    print("===== STEP 1: Build Piko patches (.mpp) =====")
    piko_build = build_piko_patches()
    print(f"[debug] piko commit: {piko_build.commit}")
    print(f"[debug] piko supported versions: {sorted(piko_build.supported_versions)}")

    print("===== STEP 2: Search X versions on APKMirror top page =====")
    versions = apkmirror.get_versions(
        "https://www.apkmirror.com/apk/x-corp/twitter/"
    )
    print(f"[debug] APKMirror versions ({len(versions)}):")
    print([v.version for v in versions])

    print("===== STEP 3: Determine latest compatible version =====")
    latest_version = get_latest_version(versions, piko_build.supported_versions)

    # トップページで見つからなかった場合、スラッグURLを組み立てて直接試す
    if latest_version is None:
        print("[debug] no match on top page, trying slug URL fallback...")
        latest_version = resolve_latest_version_by_slug(
            piko_build.supported_versions
        )

    if latest_version is None:
        raise Exception("No X version is supported by the Piko dev patches")
    print(f"[debug] latest compatible version: {latest_version.version}")
    print(f"[debug] detail page link: {latest_version.link}")

    release_tag = f"{latest_version.version}-{piko_build.commit[:7]}"
    print(f"[debug] candidate release tag: {release_tag}")

    print("===== STEP 3.5: Check if already released =====")
    last_build_version: github.GithubRelease | None = github.get_last_build_version(REPO)
    if last_build_version is not None:
        print(f"[debug] last build release tag: {last_build_version.tag_name}")
    else:
        print("[debug] no previous release found")

    if (
        last_build_version is not None
        and last_build_version.tag_name == release_tag
    ):
        print("No new compatible version found")
        return

    print(f"New compatible version found: {latest_version.version}")
    process(latest_version, piko_build, last_build_version)


if __name__ == "__main__":
    main()