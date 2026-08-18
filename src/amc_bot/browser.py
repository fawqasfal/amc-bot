"""Selenium lifecycle and low-level, non-evasive browser helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from stat import S_IMODE
from typing import Iterable

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.selenium_manager import SeleniumManager
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .errors import HumanActionRequired, RateLimited, SiteChanged


HUMAN_GATE_MARKERS = (
    "botdeflector human verification",
    "verification in progress",
    "verify you are human",
    "complete the security check",
    "captcha",
)

RATE_LIMIT_MARKERS = (
    "too many requests",
    "rate limit",
    "temporarily blocked",
    "access denied",
)


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    driver_path: Path | None = None
    chrome_binary: Path | None = None
    profile_directory: Path | None = None
    headless: bool = False
    detach: bool = True
    page_load_timeout: float = 30.0
    element_timeout: float = 12.0


def create_chrome(config: BrowserConfig) -> WebDriver:
    """Create an ordinary Chrome session.

    No stealth flags, CAPTCHA solvers, or access-control workarounds are used.
    A normal profile directory can be supplied so a human-completed challenge
    survives browser restarts.
    """

    options = Options()
    if config.chrome_binary:
        options.binary_location = str(config.chrome_binary)
    if config.profile_directory:
        config.profile_directory.mkdir(parents=True, exist_ok=True)
        config.profile_directory.chmod(0o700)
        if S_IMODE(config.profile_directory.stat().st_mode) & 0o077:
            raise PermissionError("Chrome profile directory must not be accessible by group or others")
        options.add_argument(f"--user-data-dir={config.profile_directory}")
    if config.headless:
        options.add_argument("--headless=new")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-extensions")
    options.add_experimental_option(
        "prefs",
        {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        },
    )
    options.add_experimental_option("detach", config.detach)

    if config.driver_path:
        service = Service(executable_path=str(config.driver_path))
    else:
        manager_args = ["--browser", "chrome", "--skip-driver-in-path"]
        if config.chrome_binary:
            manager_args.extend(("--browser-path", str(config.chrome_binary)))
        resolved = SeleniumManager().binary_paths(manager_args)
        service = Service(executable_path=resolved["driver_path"])
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(config.page_load_timeout)
    return driver


class BrowserPage:
    """Small wrapper that tries selector candidates in a deterministic order."""

    def __init__(self, driver: WebDriver, timeout: float = 12.0) -> None:
        self.driver = driver
        self.timeout = timeout

    def body_text(self) -> str:
        for _attempt in range(3):
            try:
                return self.driver.find_element(By.TAG_NAME, "body").text.casefold()
            except StaleElementReferenceException:
                continue
            except NoSuchElementException:
                break
        return self.driver.page_source.casefold()

    def ensure_not_blocked(self) -> None:
        text = self.body_text()
        marker = next((value for value in HUMAN_GATE_MARKERS if value in text), None)
        if marker:
            raise HumanActionRequired(
                "AMC is showing a human-verification step; complete it in the open browser"
            )
        if any(value in text for value in RATE_LIMIT_MARKERS):
            raise RateLimited()

    def first(
        self,
        selectors: Iterable[str],
        *,
        clickable: bool = False,
        timeout: float | None = None,
    ) -> WebElement:
        wait = WebDriverWait(self.driver, timeout or self.timeout)
        for selector in selectors:
            try:
                condition = (
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    if clickable
                    else EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
                return wait.until(condition)
            except TimeoutException:
                continue
        self.ensure_not_blocked()
        raise SiteChanged(f"None of the expected page controls were found: {tuple(selectors)!r}")

    def all(self, selectors: Iterable[str]) -> list[WebElement]:
        for selector in selectors:
            matches = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if matches:
                return matches
        self.ensure_not_blocked()
        return []

    def click_text(self, labels: Iterable[str], timeout: float | None = None) -> WebElement:
        """Prefer accessible button/link text before CSS fallbacks."""

        wait = WebDriverWait(self.driver, timeout or self.timeout)
        for label in labels:
            xpath = (
                "//*[self::button or self::a]"
                f"[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                f"'abcdefghijklmnopqrstuvwxyz'), {label.casefold()!r})]"
            )
            try:
                element = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
                element.click()
                return element
            except TimeoutException:
                continue
        self.ensure_not_blocked()
        raise SiteChanged(f"No clickable control matched labels {tuple(labels)!r}")
