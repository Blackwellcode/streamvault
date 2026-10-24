#!/usr/bin/env python3
"""Automated Screenshot Capture for NachtSilo Hub Showcase.

Captures high-resolution, neutral retina screenshots of all key views in Demo Mode
for GitHub documentation and project presentation.
"""

import os
import sys
import time
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("TARGET_URL", "http://127.0.0.1:5055")
DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "screenshots")


def capture_all():
    os.makedirs(DOCS_DIR, exist_ok=True)
    print(f"Connecting to {BASE_URL}...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        
        # 1. Desktop Context (1600x950, Retina 2x)
        desktop = browser.new_context(
            viewport={"width": 1600, "height": 950},
            device_scale_factor=2,
            color_scheme="dark",
        )
        page = desktop.new_page()

        # Login
        print("1. Logging in as demo admin...")
        page.goto(f"{BASE_URL}/login")
        page.fill("#username", "admin")
        page.fill("#password", "admin")
        page.click("#login-submit")
        page.wait_for_url(f"{BASE_URL}/")
        time.sleep(1.5)

        # 2. Dashboard
        print("2. Capturing Dashboard...")
        page.goto(f"{BASE_URL}/")
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)
        page.screenshot(path=os.path.join(DOCS_DIR, "dashboard.png"))

        # 3. Streamer Archive View
        print("3. Capturing Streamer Hub...")
        page.goto(f"{BASE_URL}/streamer/nexusprime")
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)
        page.screenshot(path=os.path.join(DOCS_DIR, "streamer_hub.png"))

        # 4. VOD Player with Chat
        print("4. Capturing VOD Player...")
        page.goto(f"{BASE_URL}/vod/nexusprime/nexusprime_tw_2026-10-24_10-00-00.mp4")
        page.wait_for_load_state("networkidle")
        time.sleep(2.0)
        page.screenshot(path=os.path.join(DOCS_DIR, "vod_player.png"))

        # 5. Admin Dashboard
        print("5. Capturing Admin Panel...")
        page.goto(f"{BASE_URL}/admin")
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)
        page.screenshot(path=os.path.join(DOCS_DIR, "admin_panel.png"))

        # 6. User Management
        print("6. Capturing User Management...")
        page.goto(f"{BASE_URL}/admin/users")
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)
        page.screenshot(path=os.path.join(DOCS_DIR, "admin_users.png"))

        # Save cookies for mobile context
        cookies = desktop.cookies()
        desktop.close()

        # 7. Mobile View (iPhone 14 / Safari)
        print("7. Capturing Mobile View...")
        mobile = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            color_scheme="dark",
        )
        mobile.add_cookies(cookies)
        mobile_page = mobile.new_page()
        mobile_page.goto(f"{BASE_URL}/")
        mobile_page.wait_for_load_state("networkidle")
        time.sleep(1.5)
        mobile_page.screenshot(path=os.path.join(DOCS_DIR, "mobile_view.png"))
        mobile.close()

        browser.close()
    print("All screenshots captured successfully in:", DOCS_DIR)


if __name__ == "__main__":
    capture_all()
