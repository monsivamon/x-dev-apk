import os
import re
import subprocess
import sys

import requests

_scraper = None


# Build headers for GitHub API requests, adding auth if available
def github_api_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# Cloudflare のチャレンジページ（HTTP 200 で返ってくる）かどうかを判定する
def _is_challenge_page(res) -> bool:
    if res is None or getattr(res, "status_code", None) != 200:
        return False
    try:
        text = (res.text or "")[:5000]
    except Exception:
        return False
    markers = (
        "Just a moment", "cf-chl-", "__cf_chl_", "Checking your browser",
        "Attention Required", "challenge-platform", "cf_chl_opt",
    )
    return any(m in text for m in markers)

# CloudflareのBot検知を回避するためのスクレイパーを取得する
class _HybridSession:
    def __init__(self) -> None:
        import cloudscraper
        try:
            self._primary = cloudscraper.create_scraper(enable_stealth=True)
        except TypeError:
            self._primary = cloudscraper.create_scraper()
        self._primary.headers.update({"User-Agent": "APKUpdater-v3.0.3"})
        self._fallback = None
        self.headers = self._primary.headers
        print("[scraper] primary: cloudscraper (stealth + APKUpdater UA)", flush=True)

    def _get_fallback(self):
        if self._fallback is None:
            from curl_cffi import requests as curl_requests
            self._fallback = curl_requests.Session(impersonate="chrome124")
            print("[scraper] fallback: curl_cffi (Chrome 124 full impersonation)", flush=True)
        return self._fallback

    def get(self, *args, **kwargs):
        r = self._primary.get(*args, **kwargs)
        # 403 または チャレンジページが返ってきた場合は fallback に切り替え
        if r.status_code == 403 or _is_challenge_page(r):
            reason = "403" if r.status_code == 403 else "challenge page"
            try: r.close()
            except Exception: pass
            print(f"[scraper] primary returned {reason}, retrying with curl_cffi...", flush=True)
            r = self._get_fallback().get(*args, **kwargs)
        return r

# Return the singleton hybrid scraper instance
def get_scraper():
    global _scraper
    if _scraper is None:
        _scraper = _HybridSession()
    return _scraper


# Print an error message to stderr and exit with code 1
def panic(message: str):
    print(message, file=sys.stderr)
    exit(1)


# Download a file from link to out, optionally via cloudscraper
def download(link, out, headers=None, use_scraper=False):
    dir_name = os.path.dirname(out)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    if os.path.exists(out):
        print(f"{out} already exists skipping download")
        return

    if use_scraper:
        print(f"Downloading with scraper: {link}", flush=True)

    session = get_scraper() if use_scraper else requests

    with session.get(link, stream=True, headers=headers, timeout=30) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)


# Run a shell command and exit on failure
def run_command(command: list[str]):
    cmd = subprocess.run(command, capture_output=True, shell=True)

    try:
        cmd.check_returncode()
    except subprocess.CalledProcessError:
        print(cmd.stdout)
        print(cmd.stderr)
        exit(1)


# Apply patches via Morphe CLI and return per-patch success status
def patch_apk(
    cli: str,
    patches: str,
    apk: str,
    includes: list[str] | None = None,
    excludes: list[str] | None = None,
    out: str | None = None,
    minimum_patches: int | None = None,
    continue_on_error: bool = False,
) -> dict[str, bool]:
    command = [
        "java",
        "-jar",
        cli,
        "patch",
        "-p",
        patches,
        "--keystore",
        "ks.keystore",
        "--keystore-entry-password",
        "123456789",
        "--keystore-password",
        "123456789",
        "--signer",
        "jhc",
        "--keystore-entry-alias",
        "jhc",
        "--force",
        "--exclusive",
    ]

    if continue_on_error:
        command.append("--continue-on-error")

    if includes is not None:
        for i in includes:
            command.append("-e")
            command.append(i)

    if excludes is not None:
        for e in excludes:
            command.append("-d")
            command.append(e)

    if out is not None:
        command.extend(["--out", out])

    command.append(os.path.abspath(apk))

    result = subprocess.run(command, text=True, capture_output=True)
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", file=sys.stderr, flush=True)

    if result.returncode != 0:
        result.check_returncode()

    output = result.stdout + result.stderr
    statuses: dict[str, bool] = {}

    if includes:
        failed = set(
            re.findall(
                r"FAILED:\s*(.+?)\s*$",
                output,
                re.MULTILINE | re.IGNORECASE,
            )
        )
        for patch_name in includes:
            statuses[patch_name] = patch_name not in failed

    if out is not None and not os.path.exists(out):
        raise FileNotFoundError(f"Morphe did not create the expected output: {out}")

    return statuses


# Create a GitHub release via gh CLI and upload assets
def publish_release(tag: str, files: list[str], message: str, title=""):
    key = os.environ.get("GITHUB_TOKEN")
    if key is None:
        raise Exception("GITHUB_TOKEN is not set")

    # Anything that is not the default branch becomes a prerelease.
    ref = os.environ.get("GITHUB_REF", "")
    is_release = ref in ("", "refs/heads/master", "refs/heads/main")

    command = ["gh", "release", "create", tag, "--notes", message, "--title", title]
    if is_release:
        command.append("--latest")
    else:
        command.append("--prerelease")
        print(f"Non-default ref ({ref}) -> creating prerelease")

    if len(files) == 0:
        raise Exception("Files should have atleast one item")

    for file in files:
        command.append(file)

    subprocess.run(command, env=os.environ.copy()).check_returncode()