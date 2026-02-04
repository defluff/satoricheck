"""
Playwright E2E Tests for Pitchdeck PDF Upload.

Tests the client-side PDF upload functionality:
- Click-to-upload (file picker)
- Drag-and-drop upload
- File type validation (PDF only)

Privacy-first: Files are handled client-side only, never uploaded to server.
"""
import pytest
from playwright.sync_api import Page, expect
import os

# Base URL for tests
BASE_URL = os.getenv('E2E_BASE_URL', 'http://localhost:8000')

# Test PDF file path (create a minimal valid PDF for testing)
TEST_PDF_PATH = os.path.join(os.path.dirname(__file__), 'fixtures', 'test_deck.pdf')
TEST_INVALID_FILE_PATH = os.path.join(os.path.dirname(__file__), 'fixtures', 'test_invalid.txt')


@pytest.fixture(scope="module", autouse=True)
def setup_test_fixtures():
    """Create test fixture files if they don't exist."""
    fixtures_dir = os.path.join(os.path.dirname(__file__), 'fixtures')
    os.makedirs(fixtures_dir, exist_ok=True)
    
    # Create minimal valid PDF (PDF 1.4 header + empty page)
    pdf_path = os.path.join(fixtures_dir, 'test_deck.pdf')
    if not os.path.exists(pdf_path):
        # Minimal valid PDF structure
        pdf_content = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj
xref
0 4
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
trailer << /Size 4 /Root 1 0 R >>
startxref
196
%%EOF"""
        with open(pdf_path, 'wb') as f:
            f.write(pdf_content)
    
    # Create invalid text file
    txt_path = os.path.join(fixtures_dir, 'test_invalid.txt')
    if not os.path.exists(txt_path):
        with open(txt_path, 'w') as f:
            f.write("This is not a PDF file.")
    
    yield
    # Cleanup handled by pytest-playwright


def navigate_to_pitchdeck(page: Page):
    """Helper: Navigate to the Pitchdeck view."""
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")
    
    # Click the Pitch Deck nav button
    pitchdeck_btn = page.locator("#nav-pitchdeck-btn")
    pitchdeck_btn.click()
    
    # Wait for pitchdeck view to be visible
    page.wait_for_selector("#pitchdeck-view:not(.hidden)")


class TestPitchdeckUploadClickToUpload:
    """Test click-to-upload functionality."""

    def test_click_upload_zone_opens_file_picker(self, page: Page):
        """
        Given: User is on the Pitchdeck view
        When: User clicks the upload zone
        Then: A file picker dialog should be triggered
        """
        navigate_to_pitchdeck(page)
        
        # The upload zone should exist
        upload_zone = page.locator("#pd-upload-zone")
        expect(upload_zone).to_be_visible()
        
        # There should be a hidden file input
        file_input = page.locator("#pd-file-input")
        expect(file_input).to_be_attached()
        
        # Start waiting for file chooser before clicking
        with page.expect_file_chooser() as fc_info:
            upload_zone.click()
        
        # File chooser should have been triggered
        file_chooser = fc_info.value
        assert file_chooser is not None

    def test_click_upload_valid_pdf_success(self, page: Page):
        """
        Given: User is on the Pitchdeck view
        When: User clicks upload and selects a valid PDF
        Then: The file should be loaded and UI should update
        """
        navigate_to_pitchdeck(page)
        
        file_input = page.locator("#pd-file-input")
        
        # Upload the test PDF
        file_input.set_input_files(TEST_PDF_PATH)
        
        # Wait for success state
        page.wait_for_selector(".pd-upload-zone.uploaded", timeout=5000)
        
        # Upload zone should show success state
        upload_zone = page.locator("#pd-upload-zone")
        expect(upload_zone).to_have_class("pd-upload-zone uploaded")
        
        # Filename should be displayed
        filename_display = page.locator(".pd-uploaded-filename")
        expect(filename_display).to_contain_text("test_deck.pdf")
        
        # Deep Dive button should be enabled
        deep_dive_btn = page.locator("#pd-deep-dive-btn")
        expect(deep_dive_btn).not_to_be_disabled()


class TestPitchdeckUploadDragAndDrop:
    """Test drag-and-drop upload functionality."""

    def test_drag_drop_valid_pdf_success(self, page: Page):
        """
        Given: User is on the Pitchdeck view  
        When: User drags and drops a valid PDF onto the upload zone
        Then: The file should be loaded and UI should update
        """
        navigate_to_pitchdeck(page)
        
        upload_zone = page.locator("#pd-upload-zone")
        
        # Create a DataTransfer with the file
        # Note: Playwright doesn't have native drag-drop file support,
        # so we simulate by dispatching events with file data
        
        # Read the test file content
        with open(TEST_PDF_PATH, 'rb') as f:
            file_content = f.read()
        
        # Dispatch drop event with file
        page.evaluate("""
            (fileContent) => {
                const uploadZone = document.getElementById('pd-upload-zone');
                const file = new File([new Uint8Array(fileContent)], 'test_deck.pdf', { type: 'application/pdf' });
                const dataTransfer = new DataTransfer();
                dataTransfer.items.add(file);
                
                const dropEvent = new DragEvent('drop', {
                    bubbles: true,
                    cancelable: true,
                    dataTransfer: dataTransfer
                });
                
                uploadZone.dispatchEvent(dropEvent);
            }
        """, list(file_content))
        
        # Wait for success state
        page.wait_for_selector(".pd-upload-zone.uploaded", timeout=5000)
        
        # Verify success
        expect(upload_zone).to_have_class("pd-upload-zone uploaded")

    def test_drag_over_shows_visual_feedback(self, page: Page):
        """
        Given: User is on the Pitchdeck view
        When: User drags a file over the upload zone
        Then: The zone should show visual feedback (dragover class)
        """
        navigate_to_pitchdeck(page)
        
        upload_zone = page.locator("#pd-upload-zone")
        
        # Dispatch dragover event
        page.evaluate("""
            () => {
                const uploadZone = document.getElementById('pd-upload-zone');
                const dragOverEvent = new DragEvent('dragover', {
                    bubbles: true,
                    cancelable: true,
                    dataTransfer: new DataTransfer()
                });
                uploadZone.dispatchEvent(dragOverEvent);
            }
        """)
        
        # Zone should have dragover class
        expect(upload_zone).to_have_class("pd-upload-zone dragover")


class TestPitchdeckUploadValidation:
    """Test file validation."""

    def test_invalid_file_type_rejected(self, page: Page):
        """
        Given: User is on the Pitchdeck view
        When: User tries to upload a non-PDF file
        Then: The upload should be rejected with an error message
        """
        navigate_to_pitchdeck(page)
        
        file_input = page.locator("#pd-file-input")
        
        # Try to upload a text file
        file_input.set_input_files(TEST_INVALID_FILE_PATH)
        
        # Error toast should appear
        toast = page.locator(".toast.error")
        expect(toast).to_be_visible(timeout=3000)
        expect(toast).to_contain_text("PDF")
        
        # Upload zone should NOT have success state
        upload_zone = page.locator("#pd-upload-zone")
        expect(upload_zone).not_to_have_class("uploaded")

    def test_empty_file_rejected(self, page: Page):
        """
        Given: User is on the Pitchdeck view
        When: User tries to upload an empty file
        Then: The upload should be rejected
        """
        navigate_to_pitchdeck(page)
        
        # Create empty file in fixtures
        empty_path = os.path.join(os.path.dirname(__file__), 'fixtures', 'empty.pdf')
        with open(empty_path, 'wb') as f:
            f.write(b'')
        
        file_input = page.locator("#pd-file-input")
        file_input.set_input_files(empty_path)
        
        # Error toast should appear
        toast = page.locator(".toast.error")
        expect(toast).to_be_visible(timeout=3000)
        
        # Cleanup
        os.remove(empty_path)
