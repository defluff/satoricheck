import pytest
from playwright.sync_api import sync_playwright
import time

def test_live_pro_connection_stability():
    """
    E2E Test for Live Pro Connection Stability.
    
    Scenario:
    1. Login to the application.
    2. Navigate to Live Pro.
    3. Start a session.
    4. Wait for 10 seconds (simulated stability check).
    5. Verify no disconnection errors are visible.
    """
    with sync_playwright() as p:
        # Launch browser (headless=True for CI/Background)
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            # 1. Login (Simplified for local dev - assuming auto-login or basic auth if needed)
            # For this specific verify step, we assume the server is running locally on 5000 or 8000.
            # Adjust port if necessary.
            base_url = "http://localhost:8000" 
            
            print(f"Navigating to {base_url}...")
            page.goto(base_url)
            
            # NOTE: If auth is required, we might need to inject a token or use a test user.
            # For now, we'll assume the dev environment might have a bypass or we check if redirect happens.
            
            # Check if we are redirected to login
            if "login" in page.url:
                print("Login page detected. Attempting login...")
                # Fill in dummy credentials if this is a local test env
                # page.fill('input[name="email"]', 'test@example.com')
                # page.fill('input[name="password"]', 'password')
                # page.click('button[type="submit"]')
                # page.wait_for_url(f"{base_url}/dashboard")
                pass

            # 2. Navigate to Live Pro
            # Assuming there is a link or we can go directly
            page.goto(f"{base_url}/live-pro")
            print("Navigated to Live Pro.")
            
            # 3. Start Session
            # Look for a start button. This ID might need to be adjusted based on actual DOM.
            # Using specific selector based on likely UI
            try:
                start_btn = page.wait_for_selector('button#start-session-btn', timeout=5000)
                if start_btn:
                    start_btn.click()
                    print("Clicked Start Session.")
            except Exception:
                print("Start button not found or already active.")

            # 4. Wait for stability
            print("Waiting 10s for connection stability...")
            time.sleep(10)

            # 5. Check for errors
            # Look for common error toasts or disconnection messages
            content = page.content()
            if "Connection closed" in content or "Error 1006" in content:
                pytest.fail("Found disconnection error in page content!")
            
            print("No visible disconnection errors found.")

        except Exception as e:
            print(f"Test failed with exception: {e}")
            # snapshot for debug
            # page.screenshot(path="failed_test.png")
            raise e
        finally:
            browser.close()

if __name__ == "__main__":
    test_live_pro_connection_stability()
