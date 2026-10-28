import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.browser


def test_setup_dashboard_controls_mobile_and_logout(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    env = {**os.environ, "TM_DATA_DIR": str(tmp_path / "data"), "TM_MOCK": "true",
           "TM_SECURE_COOKIES": "false", "TM_PORT": str(port), "TM_PUBLIC_ORIGIN": origin}
    log = (tmp_path / "server.log").open("w")
    process = subprocess.Popen([sys.executable, "-m", "tempmonitor.web"], env=env, stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(origin + "/healthz", timeout=1).close()
                break
            except (urllib.error.URLError, TimeoutError):
                if process.poll() is not None:
                    pytest.fail((tmp_path / "server.log").read_text())
                time.sleep(.2)
        else:
            pytest.fail("Mock server did not start")
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin)
            page.get_by_label("Setup token").fill((tmp_path / "data/setup-token").read_text())
            page.get_by_label("Administrator name").fill("admin")
            page.get_by_label("Password (12 characters minimum)").fill("browser-only-password-123")
            page.get_by_role("button", name="Create administrator").click()
            page.get_by_label("Username").fill("admin")
            page.get_by_label("Password", exact=True).fill("browser-only-password-123")
            page.get_by_role("button", name="Sign in", exact=True).click()
            playwright.expect(page.get_by_text("Homeserver temperatures", exact=True)).to_be_visible()
            playwright.expect(page.get_by_text("FAN 01", exact=True)).to_be_visible()
            playwright.expect(page.get_by_text("FAN 04", exact=True)).to_be_visible()
            page.get_by_role("button", name="Edit curve", exact=True).first.click()
            page.get_by_label("Temperature", exact=True).nth(1).fill("46")
            page.get_by_role("button", name="Save curve", exact=True).click()
            playwright.expect(page.get_by_text("Fan 1 curve confirmed", exact=True)).to_be_visible()
            fan = page.locator(".fan-card").first
            fan.get_by_role("button", name="Apply", exact=True).click()
            playwright.expect(page.get_by_text("Fan 1 override confirmed", exact=True)).to_be_visible()
            fan.get_by_role("button", name="Auto", exact=True).click()
            playwright.expect(page.get_by_text("Fan 1 returned to automatic", exact=True)).to_be_visible()
            playwright.expect(page.get_by_text("Fan 1 returned to automatic", exact=True)).to_be_hidden(timeout=10000)
            Path("test-results").mkdir(exist_ok=True)
            page.evaluate("scrollTo(0, 0)")
            page.screenshot(path="test-results/dashboard-desktop.png", full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_function("document.documentElement.scrollWidth <= innerWidth + 1")
            page.screenshot(path="test-results/dashboard-mobile.png", full_page=True)
            assert not errors, errors
            page.get_by_role("button", name="Sign out", exact=True).click()
            playwright.expect(page.get_by_role("button", name="Sign in", exact=True)).to_be_visible()
            assert page.request.get(origin + "/api/status").status == 401
            page.set_viewport_size({"width": 1440, "height": 1100})
            page.goto(origin)
            page.get_by_label("Viewing password").fill("fanclub")
            page.get_by_role("button", name="View dashboard", exact=True).click()
            playwright.expect(page.get_by_text("Homeserver temperatures", exact=True)).to_be_visible()
            playwright.expect(page.get_by_role("link", name="Log in as admin")).to_be_visible()
            assert page.get_by_role("button", name="Edit curve", exact=True).count() == 0
            assert page.get_by_role("button", name="Apply", exact=True).count() == 0
            page.get_by_text("Last hour", exact=True).click()
            page.get_by_role("option", name="Last 6 hours", exact=True).click()
            page.wait_for_timeout(2500)
            assert page.request.get(origin + "/api/history?hours=6").status == 200
            page.screenshot(path="test-results/viewer-desktop.png", full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_function("document.documentElement.scrollWidth <= innerWidth + 1")
            page.screenshot(path="test-results/viewer-mobile.png", full_page=True)
            page.get_by_role("link", name="Log in as admin").click()
            playwright.expect(page.get_by_label("Username")).to_be_visible()
            assert not errors, errors
            browser.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()
