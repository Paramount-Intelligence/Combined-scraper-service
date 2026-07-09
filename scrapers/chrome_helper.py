"""
chrome_helper.py
================
Shared Selenium Chrome/Chromium driver factory for all scrapers.

Reads CHROME_BIN and CHROMEDRIVER_PATH from the environment (set by Docker),
applies the required Chrome options for headless containerised operation,
and creates the webdriver.Chrome instance.

Usage in any scraper:
    from scrapers.chrome_helper import build_driver   # when run from /app root
    # — or, when run from scrapers/<name>/ sub-directory —
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from chrome_helper import build_driver

    driver = build_driver()
"""
import os
import shutil

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------

def _find_binary(env_var: str, candidates: list[str]) -> str:
    """Return the first resolvable path for *env_var* / *candidates*."""
    val = os.getenv(env_var, "").strip()
    if val and os.path.exists(val):
        return val
    for path in candidates:
        if os.path.exists(path):
            return path
    # Last-resort: search PATH
    binary_name = candidates[-1].rsplit("/", 1)[-1]
    found = shutil.which(binary_name)
    return found or ""


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def build_driver(extra_options: list[str] | None = None) -> webdriver.Chrome:
    """
    Build and return a Selenium Chrome WebDriver.

    Reads CHROME_BIN / CHROMEDRIVER_PATH from env.  Falls back to common
    system paths and (if unavailable) webdriver-manager.

    :param extra_options: Additional Chrome CLI arguments to pass, if any.
    :return: A running webdriver.Chrome instance.
    :raises RuntimeError: If neither system ChromeDriver nor webdriver-manager
                          can supply a driver path.
    """
    headless = os.getenv("HEADLESS", "True").lower() == "true"

    chrome_bin = _find_binary("CHROME_BIN", [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ])

    chromedriver_path = _find_binary("CHROMEDRIVER_PATH", [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    ])

    options = Options()

    # --- Headless & display ---
    if headless:
        options.add_argument("--headless=new")

    # --- Required for containerised/Docker/Railway operation ---
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")

    # --- Anti-bot hardening ---
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    # --- Binary location ---
    if chrome_bin:
        options.binary_location = chrome_bin

    # --- Extra caller-provided options ---
    for arg in (extra_options or []):
        options.add_argument(arg)

    # --- Service (ChromeDriver) ---
    if chromedriver_path:
        service = Service(chromedriver_path)
    else:
        # Fallback: webdriver-manager (dev / local usage)
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            from webdriver_manager.core.os_manager import ChromeType
            is_chromium = "chromium" in (chrome_bin or "").lower()
            mgr = ChromeDriverManager(
                chrome_type=ChromeType.CHROMIUM if is_chromium else ChromeType.GOOGLE
            )
            driver_path = mgr.install()
            service = Service(driver_path)
        except Exception as e:
            raise RuntimeError(
                f"No ChromeDriver found at system paths and webdriver-manager failed: {e}"
            ) from e

    driver = webdriver.Chrome(service=service, options=options)

    # Suppress navigator.webdriver flag
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {
        "userAgent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    })

    return driver
