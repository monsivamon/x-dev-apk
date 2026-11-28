import html as html_lib
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.parse

import requests

_scraper = None

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def github_api_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _is_challenge_page(res) -> bool:
    if res is None or getattr(res, "status_code", None) != 200:
        return False
    ctype = (res.headers.get("Content-Type") or "").lower()
    if ctype and "html" not in ctype:
        return False
    try:
        text = (res.text or "")[:5000]
    except Exception:
        return False
    markers = (
        "Just a moment",
        "cf-chl-",
        "__cf_chl_",
        "Checking your browser",
        "Attention Required",
        "challenge-platform",
        "cf_chl_opt",
    )
    return any(m in text for m in markers)


def _get_proxy():
    proxy = (os.environ.get("APKMIRROR_PROXY") or "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


_TRANSLATE_HOST = "www-apkmirror-com.translate.goog"
_TRANSLATE_PARAMS = {"_x_tr_sl": "auto", "_x_tr_tl": "en", "_x_tr_hl": "en"}


def _to_translate(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query += list(_TRANSLATE_PARAMS.items())
    return urllib.parse.urlunsplit(
        ("https", _TRANSLATE_HOST, parsed.path, urllib.parse.urlencode(query), "")
    )


def _from_translate(html: str) -> str:
    pattern = re.compile(r"https?://www-apkmirror-com\.translate\.goog(?P<rest>/[^\"'\s<>]*)")

    def repl(match: "re.Match[str]") -> str:
        url = html_lib.unescape(match.group("rest"))
        parts = urllib.parse.urlsplit(url)
        query = [
            (k, v)
            for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if not k.startswith("_x_tr_")
        ]
        return urllib.parse.urlunsplit(("", "", parts.path, urllib.parse.urlencode(query), ""))

    return pattern.sub(repl, html)


_relay_lock = threading.Lock()
_relay_last = 0.0
_RELAY_MIN_INTERVAL = 2.5


def _relay_get(url: str, **kwargs):
    global _relay_last
    with _relay_lock:
        wait = _RELAY_MIN_INTERVAL - (time.time() - _relay_last)
        if wait > 0:
            time.sleep(wait)
        try:
            return requests.get(url, headers={"User-Agent": CHROME_UA}, **kwargs)
        finally:
            _relay_last = time.time()


def _fetch_via_translate(url: str) -> str | None:
    relay_url = _to_translate(url)
    last_status = None
    for attempt in range(3):
        if attempt:
            time.sleep(5 * attempt)
        try:
            res = _relay_get(relay_url, timeout=90)
        except Exception as e:
            print(f"[scraper] translate relay error: {type(e).__name__}: {e}", flush=True)
            continue
        last_status = res.status_code
        if res.status_code == 429:
            print("[scraper] translate relay rate-limited (429)", flush=True)
            time.sleep(10 * (attempt + 1))
            continue
        if res.status_code != 200:
            print(f"[scraper] translate relay returned {res.status_code}", flush=True)
            continue
        html = _from_translate(res.text)
        if "apkmirror" not in html.lower():
            print("[scraper] translate relay did not return an APKMirror page", flush=True)
            continue
        print("[scraper] fetched via Google Translate relay", flush=True)
        return html
    if last_status is not None:
        print(f"[scraper] translate relay gave up (last status {last_status})", flush=True)
    return None


def _untranslate_url(location: str) -> str | None:
    parts = urllib.parse.urlsplit(location)
    host = parts.netloc
    if not host.endswith(".translate.goog"):
        return None
    host = host[: -len(".translate.goog")]
    host = host.replace("-r2-cloudflarestorage-com", ".r2.cloudflarestorage.com")
    if host.startswith("www-apkmirror-com"):
        host = host.replace("www-apkmirror-com", "www.apkmirror.com", 1)
    if host == "apkmirror.com" or host.endswith(".apkmirror.com"):
        return None
    query = "&".join(part for part in parts.query.split("&") if not part.startswith("_x_tr_"))
    return urllib.parse.urlunsplit((parts.scheme or "https", host, parts.path, query, ""))


def resolve_apk_via_translate(download_php_url: str) -> str | None:
    relay_url = _to_translate(download_php_url)
    for attempt in range(3):
        if attempt:
            time.sleep(5 * attempt)
        try:
            res = _relay_get(relay_url, timeout=90, allow_redirects=False)
        except Exception as e:
            print(f"[scraper] translate redirect error: {type(e).__name__}: {e}", flush=True)
            continue
        if res.status_code == 429:
            print("[scraper] translate redirect rate-limited (429)", flush=True)
            time.sleep(10 * (attempt + 1))
            continue
        location = res.headers.get("Location") or res.headers.get("location")
        if res.status_code in (301, 302, 303, 307, 308) and location:
            target = _untranslate_url(location)
            if target:
                print("[scraper] resolved CDN URL via Google Translate relay", flush=True)
                return target
            print(f"[scraper] unexpected translate redirect target: {location[:80]}", flush=True)
        elif res.status_code == 200:
            match = re.search(
                r"https?://[^\"'<>\s]*(?:r2\.cloudflarestorage\.com|"
                r"-r2-cloudflarestorage-com\.translate\.goog)[^\"'<>\s]*",
                html_lib.unescape(res.text or ""),
            )
            if match:
                target = _untranslate_url(match.group(0))
                if target:
                    print("[scraper] resolved CDN URL via Google Translate relay", flush=True)
                    return target
            print("[scraper] translate redirect returned 200 without a CDN URL", flush=True)
        else:
            print(f"[scraper] translate redirect returned {res.status_code}", flush=True)
    return None


def _force_relay() -> bool:
    return (os.environ.get("APKMIRROR_FORCE_RELAY") or "").strip().lower() in ("1", "true", "yes")


class _SimpleResponse:
    def __init__(self, html: str) -> None:
        self.status_code = 200
        self.content = html.encode("utf-8")
        self.text = html
        self.headers = {"Content-Type": "text/html; charset=UTF-8"}

    def close(self) -> None:
        pass


def _describe_block(res) -> str:
    try:
        text = res.text or ""
    except Exception:
        return f"status={getattr(res, 'status_code', '?')}"
    match = re.search(r"<title>(.*?)</title>", text[:20000], re.S)
    title = match.group(1).strip() if match else text[:100].replace("\n", " ")
    return f"status={res.status_code} title={title!r}"


class _HybridSession:
    def __init__(self) -> None:
        self._proxy = _get_proxy()
        self._primary = None
        self._fallback = None
        self.headers = {"User-Agent": CHROME_UA}
        via = "via proxy" if self._proxy else "direct"
        print(f"[scraper] primary: curl_cffi (current Chrome fingerprint, {via})", flush=True)

    def _get_primary(self):
        if self._primary is None:
            from curl_cffi import requests as curl_requests
            last_error = None
            for impersonate in ("chrome", "chrome136", "chrome131", "safari17_0"):
                try:
                    self._primary = curl_requests.Session(
                        impersonate=impersonate, proxies=self._proxy
                    )
                    print(f"[scraper] curl_cffi impersonate={impersonate}", flush=True)
                    break
                except Exception as e:
                    last_error = e
            if self._primary is None:
                raise last_error
        return self._primary

    def _get_fallback(self):
        if self._fallback is None:
            import cloudscraper
            try:
                session = cloudscraper.create_scraper(enable_stealth=True)
            except TypeError:
                session = cloudscraper.create_scraper()
            session.headers.update({"User-Agent": CHROME_UA})
            if self._proxy:
                session.proxies.update(self._proxy)
            self._fallback = session
            print("[scraper] fallback: cloudscraper (stealth + Chrome UA)", flush=True)
        return self._fallback

    def _send(self, client, url, *args, **kwargs):
        headers = kwargs.pop("headers", None)
        if headers:
            kwargs["headers"] = headers
        return client.get(url, *args, **kwargs)

    def get(self, url, *args, **kwargs):
        stream = bool(kwargs.get("stream"))

        if not stream and _force_relay():
            html = _fetch_via_translate(url)
            if html is not None:
                return _SimpleResponse(html)

        try:
            r = self._send(self._get_primary(), url, *args, **kwargs)
        except Exception as e:
            print(f"[scraper] primary error: {type(e).__name__}: {e}", flush=True)
            r = None

        blocked = (
            r is None
            or r.status_code == 403
            or (not stream and _is_challenge_page(r))
        )
        if not blocked:
            return r

        reason = "error" if r is None else ("403" if r.status_code == 403 else "challenge page")
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
        print(f"[scraper] primary returned {reason}, retrying with cloudscraper...", flush=True)
        fallback = self._send(self._get_fallback(), url, *args, **kwargs)

        if fallback.status_code != 403 and (stream or not _is_challenge_page(fallback)):
            return fallback

        print(f"[scraper] cloudscraper also blocked ({_describe_block(fallback)})", flush=True)

        if not stream:
            html = _fetch_via_translate(url)
            if html is not None:
                return _SimpleResponse(html)

            try:
                from browser import get_browser
                html = get_browser().get_html(url)
                print(f"[scraper] fetched via headless browser: {url}", flush=True)
                return _SimpleResponse(html)
            except Exception as e:
                print(f"[scraper] browser fallback failed: {type(e).__name__}: {e}", flush=True)

        return fallback


def get_scraper():
    global _scraper
    if _scraper is None:
        _scraper = _HybridSession()
    return _scraper


def reset_scraper():
    global _scraper
    _scraper = None


def panic(message: str):
    print(message, file=sys.stderr)
    exit(1)


def download(link, out, headers=None, use_scraper=False):
    dir_name = os.path.dirname(out)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    if os.path.exists(out):
        print(f"{out} already exists skipping download")
        return

    print(f"Downloading: {link}", flush=True)

    if headers is None:
        headers = {}
    if "Referer" not in headers and "apkmirror.com" in link:
        headers["Referer"] = "https://www.apkmirror.com/"

    if use_scraper:
        r = get_scraper().get(link, stream=True, headers=headers)
    else:
        time.sleep(random.uniform(1.0, 2.0))
        r = requests.get(link, stream=True, headers=headers, timeout=120)

    try:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP Error {r.status_code} for URL: {link}")

        chunks = r.iter_content(chunk_size=8192)
        try:
            first_chunk = next(chunks)
        except StopIteration:
            first_chunk = b""

        if first_chunk:
            lower_chunk = first_chunk.lower()
            if b"<html" in lower_chunk or b"<!doctype" in lower_chunk:
                raise RuntimeError("Downloaded file is HTML (challenge page), not an APK/APKM")

        with open(out, "wb") as f:
            if first_chunk:
                f.write(first_chunk)
            for chunk in chunks:
                if chunk:
                    f.write(chunk)
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def run_command(command: list[str]):
    cmd = subprocess.run(command, capture_output=True, shell=True)

    try:
        cmd.check_returncode()
    except subprocess.CalledProcessError:
        print(cmd.stdout)
        print(cmd.stderr)
        exit(1)



def merge_apk(path: str) -> str:
    print(f"Merging split APK bundle with APKEditor: {path}", flush=True)
    subprocess.run(
        ["java", "-jar", "./bins/apkeditor.jar", "m", "-extractNativeLibs", "true", "-i", path],
        check=True,
    )
    merged_path = path.rsplit(".", 1)[0] + "_merged.apk"
    return merged_path

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


def publish_release(tag: str, files: list[str], message: str, title=""):
    key = os.environ.get("GITHUB_TOKEN")
    if key is None:
        raise Exception("GITHUB_TOKEN is not set")

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