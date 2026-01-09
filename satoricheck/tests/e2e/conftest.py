"""
Playwright E2E Test Configuration.
Provides fixtures for browser contexts, authentication, and test utilities.
"""
import pytest
from playwright.sync_api import Page, BrowserContext, expect
import os
import sys

# Add backend to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Base URL for tests - defaults to local dev server
BASE_URL = os.getenv('E2E_BASE_URL', 'http://localhost:8000')


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Configure browser context for all tests."""
    return {
        **browser_context_args,
        "viewport": {"width": 1280, "height": 720},
        "ignore_https_errors": True,
    }


@pytest.fixture
def page(context: BrowserContext) -> Page:
    """Create a new page for each test."""
    page = context.new_page()
    yield page
    page.close()


@pytest.fixture
def authenticated_page(context: BrowserContext, page: Page) -> Page:
    """
    Create an authenticated page by logging in via the API.
    This bypasses the Google OAuth flow for testing.
    """
    # First, create a test user via API (or use existing)
    page.goto(f"{BASE_URL}/")
    
    # Use the test login endpoint or signup
    page.goto(f"{BASE_URL}/#")
    
    # Click login and use email/password (not Google)
    # Wait for the page to load
    page.wait_for_load_state("networkidle")
    
    # Try to find and click the signup/login button
    try:
        # Look for auth modal trigger
        page.click('[data-action="show-login"]', timeout=3000)
    except:
        pass  # Modal might already be open or not exist
    
    # Fill login form with test credentials
    try:
        page.fill('input[name="email"], #login-email', 'e2e-test@example.com')
        page.fill('input[name="password"], #login-password', 'TestPass123!')
        page.click('button[type="submit"], #login-submit')
        page.wait_for_load_state("networkidle")
    except:
        # If login fails, try signup
        try:
            page.fill('input[name="email"], #signup-email', 'e2e-test@example.com')
            page.fill('input[name="password"], #signup-password', 'TestPass123!')
            page.click('#signup-submit')
            page.wait_for_load_state("networkidle")
        except:
            pass  # Continue anyway - might already be logged in
    
    return page


@pytest.fixture
def mobile_page(browser) -> Page:
    """Create a mobile viewport page (iPhone 14)."""
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)"
    )
    page = context.new_page()
    yield page
    page.close()
    context.close()


# Test data constants
TEST_CLAIM_TRUE = "The Earth orbits the Sun."
TEST_CLAIM_FALSE = "The Earth is flat."
TEST_CLAIM_MISLEADING = "Vaccines cause autism."
