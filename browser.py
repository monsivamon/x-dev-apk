"""Headless-browser fallback for Cloudflare-protected APKMirror pages."""

from __future__ import annotations

import threading
from urllib.parse import urlparse

from utils import CHROME_UA

CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "checking your browser",
    "please wait",
)

CHALLENGE_MARKERS = (
    "Just a moment",
    "cf-chl-",
    "__cf_chl_",
    "challenge-platform",
    "cf_chl_opt",
)


class BrowserUnavailable(Exception):
    pass


def _is_apkmirror(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "apkmirror.com" or host.endswith(".apkmirror.com")


class BrowserFetcher:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._playwright = None
        self._browser = None
        self._context = None

    def _ensure(self) -> None:
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            raise BrowserUnavailable(f"playwright is not available: {e}") from e

        self._playwright = sync_playwright().start()
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ]
        last_error = None
        for launch_kwargs in ({"channel": "chrome"}, {}):
            try:
                self._browser = self._playwright.chromium.launch(
                    headless=True, args=args, **launch_kwargs
                )
                break
            except Exception as e:
                last_error = e
        if self._browser is None:
            raise BrowserUnavailable(f"failed to launch Chromium: {last_error}")

        self._context = self._browser.new_context(
            user_agent=CHROME_UA,
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1366, "height": 768},
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        print("[browser] headless Chromium ready", flush=True)

    def close(self) -> None:
        for obj, method in (
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass
        self._context = self._browser = self._playwright = None

    def _open(self):
        self._ensure()
        return self._context.new_page()

    def _wait_for_content(self, page) -> str:
        for _ in range(45):
            title = (page.title() or "").lower()
            if not any(marker in title for marker in CHALLENGE_TITLES):
                break
            page.wait_for_timeout(1000)

        html = page.content()
        head = html[:8000]
        if any(marker in head for marker in CHALLENGE_MARKERS):
            raise BrowserUnavailable("Cloudflare challenge was not solved")
        return html

    def get_html(self, url: str) -> str:
        with self._lock:
            page = self._open()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                return self._wait_for_content(page)
            finally:
                page.close()

    def resolve_redirect(self, url: str) -> str | None:
        """Follow a download URL in the browser and capture the CDN target."""
        with self._lock:
            page = self._open()
            captured: dict[str, str] = {}

            def handle_download(download):
                captured.setdefault("url", download.url)
                try:
                    download.cancel()
                except Exception:
                    pass

            def handle_request(request):
                host = (urlparse(request.url).hostname or "").lower()
                if "cloudflarestorage.com" in host:
                    captured.setdefault("url", request.url)

            page.on("download", handle_download)
            page.on("request", handle_request)
            try:
                page.goto(url, wait_until="commit", timeout=90000)
            except Exception:
                pass

            for _ in range(10):
                if captured.get("url"):
                    break
                page.wait_for_timeout(500)

            final_url = page.url
            page.close()

            if captured.get("url"):
                return captured["url"]
            if final_url and final_url != "about:blank" and not _is_apkmirror(final_url):
                return final_url
            return None


_browser: BrowserFetcher | None = None


def get_browser() -> BrowserFetcher:
    global _browser
    if _browser is None:
        _browser = BrowserFetcher()
    return _browser