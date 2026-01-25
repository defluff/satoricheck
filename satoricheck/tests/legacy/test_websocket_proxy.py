import pytest
from unittest.mock import MagicMock, patch
import sys

# Mock config to avoid loading env vars or real scheduling
sys.modules['backend.config'] = MagicMock()
sys.modules['backend.database'] = MagicMock()
sys.modules['backend.models'] = MagicMock()

# Import the module under test
from backend.services.websocket_proxy import init_websocket_proxy, ws_proxy

def test_deepgram_sdk_import():
    """Verify that we can import the necessary Deepgram classes."""
    try:
        from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents
        print("Successfully imported Deepgram classes")
    except ImportError as e:
        pytest.fail(f"Failed to import Deepgram SDK components: {e}")

def test_init_websocket_proxy():
    """Verify initialization of the proxy."""
    mock_app = MagicMock()
    # Mock the 'sock' global in the module to avoid actual Flask extension logic if needed,
    # but the real sock.init_app call should be fine with a mock app if packages are installed.
    # However to be safe, we can trust the function call checks.
    
    with patch('backend.services.websocket_proxy.sock') as mock_sock:
        init_websocket_proxy(mock_app)
        mock_sock.init_app.assert_called_once_with(mock_app)
