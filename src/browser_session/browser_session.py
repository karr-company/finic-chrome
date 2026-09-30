import os
import subprocess
import time
from glob import glob
from typing import Optional

import requests

SESSION_PATH = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "chrome_context"
)

# Hardened flag set: no --disable-web-security / site-per-process bypass.
# Keep the anti-automation flag so portals don't fingerprint the driver.
BASE_FLAGS = [
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-accelerated-2d-canvas",
    "--no-first-run",
    "--no-zygote",
    "--use-gl=egl",
    "--disable-blink-features=AutomationControlled",
    "--disable-background-networking",
    "--enable-features=NetworkService,NetworkServiceInProcess",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-breakpad",
    "--disable-client-side-phishing-detection",
    "--disable-component-extensions-with-background-pages",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-features=Translate",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--disable-sync",
    "--force-color-profile=srgb",
    "--metrics-recording-only",
    "--password-store=basic",
    "--use-mock-keychain",
    "--hide-scrollbars",
    "--mute-audio",
]

HEADFUL_FLAGS = [
    "--window-size=1300,570",
    "--window-position=000,000",
]

HEADLESS_FLAGS = [
    "--headless",
    "--disable-gpu",
    "--enable-logging=stderr",
]

START_TIMEOUT_SECS = 30


def _chromium_bin() -> str:
    """Locate the Playwright-bundled Chromium executable.

    The arch suffix differs by platform: chrome-linux (x64) vs
    chrome-linux-arm64 (arm64).
    """
    matches = sorted(glob("/ms-playwright/chromium-*/chrome-linux*/chrome"))
    if not matches:
        raise RuntimeError("No Playwright Chromium found under /ms-playwright")
    return matches[0]


class BrowserSession:
    """A per-session chromedriver process fronting the Playwright Chromium."""

    def __init__(self, port: int):
        self.port = port
        self.data_dir = os.path.join(SESSION_PATH, str(port))
        self.process: Optional[subprocess.Popen] = None

        browser_mode = os.getenv("BROWSER_MODE", "headless").lower()
        self.is_headless = browser_mode == "headless"

    def capabilities(self) -> dict:
        """W3C capabilities for session creation on this session's driver."""
        flags = HEADLESS_FLAGS if self.is_headless else HEADFUL_FLAGS
        return {
            "capabilities": {
                "alwaysMatch": {
                    "browserName": "chrome",
                    "goog:chromeOptions": {
                        "binary": _chromium_bin(),
                        "args": BASE_FLAGS
                        + flags
                        + [f"--user-data-dir={self.data_dir}"],
                    },
                }
            }
        }

    def start(self):
        os.makedirs(self.data_dir, exist_ok=True)
        self.process = subprocess.Popen(
            [
                "chromedriver",
                f"--port={self.port}",
                "--allowed-ips=",
                "--allowed-origins=*",
            ]
        )
        deadline = time.time() + START_TIMEOUT_SECS
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"chromedriver exited early with code {self.process.returncode}"
                )
            try:
                resp = requests.get(
                    f"http://127.0.0.1:{self.port}/status", timeout=1
                )
                if resp.status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.2)
        self.stop()
        raise RuntimeError(
            f"chromedriver on port {self.port} did not become ready in time"
        )

    def stop(self):
        """Terminate the driver process. The data dir is intentionally kept
        so cookies/storage persist across sessions on this port."""
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
