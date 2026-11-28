import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

PIKO_REPO = "crimera/piko"
PIKO_REPOSITORY = f"https://github.com/{PIKO_REPO}.git"
PIKO_BRANCH = "dev"
XDEV_CONSTANTS = (
    "patches/src/main/kotlin/app/crimera/patches/twitter/utils/Constants.kt"
)
TWITTER_PATCHES_DIR = "patches/src/main/kotlin/app/crimera/patches/twitter"

GRADLE_JVMARGS = "-Xmx4g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8"


@dataclass(frozen=True)
class PikoBuild:
    commit: str
    supported_versions: frozenset[str]
    old_versions: frozenset[str]
    new_versions: frozenset[str]
    patch_categories: dict[str, str | None]


def log(message: str):
    print(f"[piko] {message}", flush=True)


def get_supported_versions(constants: str) -> frozenset[str]:
    versions = frozenset(re.findall(r'version\s*=\s*"([^"]+)"', constants))
    return versions


def configure_gradle_memory(piko_directory: Path) -> None:
    gradle_properties = piko_directory / "gradle.properties"
    existing = (
        gradle_properties.read_text(encoding="utf-8")
        if gradle_properties.exists()
        else ""
    )

    kept_lines: list[str] = [
        line
        for line in existing.splitlines()
        if not line.strip().startswith("org.gradle.jvmargs")
    ]

    kept_lines.append(f"org.gradle.jvmargs={GRADLE_JVMARGS}")
    gradle_properties.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")

    log(f"Set org.gradle.jvmargs={GRADLE_JVMARGS}")


def _parse_patches_json_data(data) -> tuple[dict[str, str | None], set[str], set[str]]:
    categories: dict[str, str | None] = {}
    old_vers: set[str] = set()
    new_vers: set[str] = set()

    items = data.get("patches") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return categories, old_vers, new_vers

    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        compat = item.get("compatiblePackages") or []
        twitter_pkgs = [
            pkg
            for pkg in compat
            if isinstance(pkg, dict)
            and (pkg.get("packageName") == "com.twitter.android" or pkg.get("name") == "com.twitter.android")
        ]
        if compat and not twitter_pkgs:
            continue

        cat = item.get("category")
        if isinstance(cat, dict):
            cat = cat.get("name")
        categories[name] = cat

        for pkg in twitter_pkgs:
            targets = pkg.get("targets") or pkg.get("versions") or []
            for t in targets:
                v_str = t.get("version") if isinstance(t, dict) else (t if isinstance(t, str) else None)
                if not v_str:
                    continue
                if cat == "Old patches":
                    old_vers.add(v_str)
                elif cat == "NewX patches":
                    new_vers.add(v_str)

    return categories, old_vers, new_vers


def _extract_from_mpp(mpp_path: Path) -> tuple[dict[str, str | None], set[str], set[str]]:
    categories: dict[str, str | None] = {}
    old_vers: set[str] = set()
    new_vers: set[str] = set()
    try:
        with zipfile.ZipFile(mpp_path, "r") as zf:
            for member in zf.namelist():
                if not member.lower().endswith(".json"):
                    continue
                try:
                    data = json.loads(zf.read(member).decode("utf-8"))
                except Exception:
                    continue
                c, ov, nv = _parse_patches_json_data(data)
                categories.update(c)
                old_vers.update(ov)
                new_vers.update(nv)
    except Exception as exc:
        log(f"Could not inspect .mpp zip metadata: {exc}")
    return categories, old_vers, new_vers


def _extract_categories_from_sources(piko_directory: Path) -> dict[str, str | None]:
    categories: dict[str, str | None] = {}
    src_root = piko_directory / TWITTER_PATCHES_DIR
    if not src_root.exists():
        return categories

    category_map = {
        "OLD_PATCHES": "Old patches",
        "NEWX_PATCHES": "NewX patches",
        "NewX": "NewX patches",
        "Old": "Old patches",
    }

    for kt_file in src_root.rglob("*.kt"):
        text = kt_file.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'\w*Patch\s*\((?P<args>.*?)\)\s*\{', text, re.DOTALL):
            args = m.group("args")
            nm = re.search(r'(?:name\s*=\s*)?"([^"]+)"', args)
            if not nm:
                continue
            patch_name = nm.group(1)
            cm = re.search(r'category\s*=\s*(?:Category\.)?([A-Za-z0-9_]+|"[^"]+")', args)
            if cm:
                raw_cat = cm.group(1).strip('"')
                categories[patch_name] = category_map.get(raw_cat, raw_cat)
            else:
                categories.setdefault(patch_name, None)

    return categories


def build_piko_patches(output: str = "bins/patches.mpp") -> PikoBuild:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    log(f"Output path: {output_path}")

    with tempfile.TemporaryDirectory(prefix="piko-") as temporary_directory:
        piko_directory = Path(temporary_directory) / "piko"

        log(f"Cloning {PIKO_REPO} ({PIKO_BRANCH})...")
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                PIKO_BRANCH,
                PIKO_REPOSITORY,
                str(piko_directory),
            ],
            check=True,
        )
        log("Clone complete")

        const_path = piko_directory / XDEV_CONSTANTS
        const_versions = get_supported_versions(const_path.read_text(encoding="utf-8")) if const_path.exists() else frozenset()
        log(f"Constants.kt versions ({len(const_versions)}): {sorted(const_versions)}")

        log("Configuring Gradle JVM memory...")
        configure_gradle_memory(piko_directory)

        log("Running gradle clean buildAndroid (may take several minutes)...")
        subprocess.run(
            ["./gradlew", "clean", "buildAndroid"],
            cwd=piko_directory,
            env=os.environ.copy(),
            check=True,
        )
        log("Gradle build complete")

        log("Locating .mpp artifact...")
        artifacts = sorted(
            (piko_directory / "patches" / "build" / "libs").glob("patches-*.mpp")
        )
        if not artifacts:
            raise FileNotFoundError("Piko did not produce a patches .mpp artifact")
        log(f"Found: {artifacts[-1].name}")

        shutil.copy2(artifacts[-1], output_path)
        log(f"Copied to {output_path}")

        patch_categories, old_vers, new_vers = _extract_from_mpp(output_path)
        if patch_categories:
            log(f"Extracted {len(patch_categories)} patch categories from built .mpp")
        else:
            local_json = piko_directory / "patches-list.json"
            if local_json.exists():
                data = json.loads(local_json.read_text(encoding="utf-8"))
                patch_categories, old_vers, new_vers = _parse_patches_json_data(data)
                log(f"Extracted {len(patch_categories)} patch categories from cloned patches-list.json")
            else:
                patch_categories = _extract_categories_from_sources(piko_directory)
                log(f"Extracted {len(patch_categories)} patch categories from Kotlin sources")

        all_versions = const_versions | old_vers | new_vers
        old_cnt = sum(1 for v in patch_categories.values() if v == "Old patches")
        new_cnt = sum(1 for v in patch_categories.values() if v == "NewX patches")
        log(f"Category breakdown: Old patches={old_cnt}, NewX patches={new_cnt}, total={len(patch_categories)}")
        log(f"JSON target versions -> Old: {sorted(old_vers)}, NewX: {sorted(new_vers)}")

        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=piko_directory,
            check=True,
            capture_output=True,
            text=True,
        )

    sha = commit.stdout.strip()
    log(f"Piko build done: {sha[:7]}")
    return PikoBuild(
        commit=sha,
        supported_versions=frozenset(all_versions),
        old_versions=frozenset(old_vers),
        new_versions=frozenset(new_vers),
        patch_categories=patch_categories,
    )