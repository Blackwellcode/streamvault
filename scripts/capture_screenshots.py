#!/usr/bin/env python3
"""Automated Screenshot Capture for NachtSilo Hub Showcase.

Captures high-resolution, neutral retina screenshots of all key views in Demo Mode
for GitHub documentation and project presentation with strict assertions.
"""

import os
import subprocess
import sys
import time
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("TARGET_URL", "http://127.0.0.1:5057")
DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "screenshots")


def start_server_if_needed():
    import urllib.request
    try:
        with urllib.request.urlopen(f"{BASE_URL}/login", timeout=1) as resp:
            if resp.status == 200:
                print(f"Server is already running at {BASE_URL}")
                return None
    except Exception:
        pass

    print(f"Starting background demo server on {BASE_URL}...")
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{root_dir};{root_dir}/WEBUI"
    env["DEMO_MODE"] = "1"
    env["VOD_PORT"] = "5057"
    
    python_exe = sys.executable
    proc = subprocess.Popen(
        [python_exe, os.path.join(root_dir, "WEBUI", "webui.py"), "--demo", "--port", "5057"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        time.sleep(0.5)
        try:
            import urllib.request
            with urllib.request.urlopen(f"{BASE_URL}/login", timeout=1) as resp:
                if resp.status == 200:
                    print("Connected to demo server!")
                    return proc
        except Exception:
            pass
    print("Warning: server did not respond within 15 seconds")
    return proc


def capture_all():
    os.makedirs(DOCS_DIR, exist_ok=True)
    server_proc = start_server_if_needed()
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            
            # Desktop Context (1600x950, Retina 2x)
            desktop = browser.new_context(
                viewport={"width": 1600, "height": 950},
                device_scale_factor=2,
                color_scheme="dark",
            )
            page = desktop.new_page()

            # 1. Login
            print("1. Logging in as demo admin...")
            page.goto(f"{BASE_URL}/login")
            page.fill("#username", "admin")
            page.fill("#password", "admin")
            page.click("#login-submit")
            page.wait_for_url(f"{BASE_URL}/")
            time.sleep(1)

            # 2. Dashboard
            print("2. Capturing Dashboard...")
            res = page.goto(f"{BASE_URL}/")
            assert res.status == 200, f"Dashboard returned status {res.status}"
            page.wait_for_load_state("networkidle")
            time.sleep(1.5)
            content = page.content()
            assert "Fehler" not in content[:500], "Dashboard error!"
            page.screenshot(path=os.path.join(DOCS_DIR, "dashboard.png"))

            # 3. Streamer Archive View
            print("3. Capturing Streamer Hub...")
            res = page.goto(f"{BASE_URL}/streamer/nexusprime")
            assert res.status == 200, f"Streamer hub returned status {res.status}"
            page.wait_for_load_state("networkidle")
            time.sleep(1.5)
            content = page.content()
            assert "Fehler" not in content[:500], "Streamer hub error!"
            page.screenshot(path=os.path.join(DOCS_DIR, "streamer_hub.png"))

            # 4. VOD Player with Chat
            print("4. Capturing VOD Player...")
            res = page.goto(f"{BASE_URL}/vod/nexusprime/nexusprime_tw_2026-10-24_10-00-00.mp4")
            assert res.status == 200, f"VOD player returned status {res.status}"
            page.wait_for_load_state("networkidle")
            time.sleep(2.0)
            content = page.content()
            assert "Not Found" not in content, "VOD returned Not Found!"
            assert "Fehler" not in content[:500], "VOD player error!"
            page.screenshot(path=os.path.join(DOCS_DIR, "vod_player.png"))

            # 5. Admin Dashboard
            print("5. Capturing Admin Panel...")
            res = page.goto(f"{BASE_URL}/admin")
            assert res.status == 200, f"Admin panel returned status {res.status}"
            page.wait_for_load_state("networkidle")
            time.sleep(1.5)
            content = page.content()
            assert "Fehler beim Laden" not in content, "Admin panel rendered error message!"
            page.screenshot(path=os.path.join(DOCS_DIR, "admin_panel.png"))

            # 6. User Management
            print("6. Capturing User Management...")
            res = page.goto(f"{BASE_URL}/admin/users")
            assert res.status == 200, f"Admin users returned status {res.status}"
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
            res = mobile_page.goto(f"{BASE_URL}/")
            assert res.status == 200, f"Mobile view returned status {res.status}"
            mobile_page.wait_for_load_state("networkidle")
            time.sleep(1.5)
            mobile_page.screenshot(path=os.path.join(DOCS_DIR, "mobile_view.png"))
            mobile.close()

            browser.close()
        print("All screenshots captured successfully in:", DOCS_DIR)
    finally:
        if server_proc:
            server_proc.terminate()
            server_proc.wait()


if __name__ == "__main__":
    capture_all()
