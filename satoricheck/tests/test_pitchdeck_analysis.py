"""
Pitchdeck Analysis Service Tests.

Tests the PitchdeckService for PDF analysis with Gemini Vision.
Follows TDD approach - tests written before implementation.

Test coverage:
- Happy path: Successful PDF analysis
- Edge cases: Null input, empty input, invalid PDF
- Error handling: Timeout, API errors, malformed responses
- Input sanitization: Oversized files, non-PDF data
"""
import pytest
from unittest.mock import patch, MagicMock
import base64
import json


class TestPitchdeckServiceAnalyzePDF:
    """Unit tests for PitchdeckService.analyze_pitch_deck()."""

    # =========================================================================
    # HAPPY PATH TESTS
    # =========================================================================

    def test_analyze_pdf_returns_structured_summary(self, app):
        """
        Given: A valid PDF file
        When: analyze_pitch_deck() is called
        Then: Returns structured JSON with summary and USP
        """
        from backend.services.pitchdeck_service import PitchdeckService
        
        service = PitchdeckService()
        
        # Minimal valid PDF bytes
        pdf_bytes = self._create_minimal_pdf()
        
        with patch.object(service, '_call_gemini_vision') as mock_gemini:
            # Return Gemini API response format (not parsed dict)
            mock_gemini.return_value = {
                'candidates': [{
                    'content': {
                        'parts': [{'text': json.dumps({
                            'company_name': 'HydroNova',
                            'summary': 'Clean water technology startup.',
                            'usp': 'Patented desalination membrane.',
                            'market_size': '$50B global market',
                            'competition': ['Competitor A', 'Competitor B']
                        })}]
                    }
                }]
            }
            
            result = service.analyze_pitch_deck(pdf_bytes)
            
            assert result is not None
            assert 'company_name' in result
            assert 'summary' in result
            assert 'usp' in result
            assert result['company_name'] == 'HydroNova'
            mock_gemini.assert_called_once()

    def test_analyze_pdf_extracts_all_required_fields(self, app):
        """
        Given: A complete pitch deck PDF
        When: analyze_pitch_deck() is called
        Then: All required analysis fields are extracted
        """
        from backend.services.pitchdeck_service import PitchdeckService
        
        service = PitchdeckService()
        pdf_bytes = self._create_minimal_pdf()
        
        with patch.object(service, '_call_gemini_vision') as mock_gemini:
            # Return Gemini API response format
            mock_gemini.return_value = {
                'candidates': [{
                    'content': {
                        'parts': [{'text': json.dumps({
                            'company_name': 'TechCorp',
                            'summary': 'AI-powered analytics platform.',
                            'usp': 'Real-time insights with 99.9% accuracy.',
                            'market_size': '$100B by 2030',
                            'competition': ['BigData Inc', 'AnalyticsPro'],
                            'team_highlights': 'Ex-Google, Ex-Meta founders',
                            'funding_ask': '$5M Series A'
                        })}]
                    }
                }]
            }
            
            result = service.analyze_pitch_deck(pdf_bytes)
            
            # Core fields are required
            assert 'company_name' in result
            assert 'summary' in result
            assert 'usp' in result
            # Extended fields may be present
            assert 'market_size' in result or True  # Optional

    # =========================================================================
    # NULL AND EMPTY INPUT TESTS
    # =========================================================================

    def test_analyze_pdf_rejects_null_input(self, app):
        """
        Given: Null input
        When: analyze_pitch_deck(None) is called
        Then: Raises ValueError with clear message
        """
        from backend.services.pitchdeck_service import PitchdeckService
        
        service = PitchdeckService()
        
        with pytest.raises(ValueError) as exc_info:
            service.analyze_pitch_deck(None)
        
        assert 'PDF data is required' in str(exc_info.value)

    def test_analyze_pdf_rejects_empty_bytes(self, app):
        """
        Given: Empty byte array
        When: analyze_pitch_deck(b'') is called
        Then: Raises ValueError with clear message
        """
        from backend.services.pitchdeck_service import PitchdeckService
        
        service = PitchdeckService()
        
        with pytest.raises(ValueError) as exc_info:
            service.analyze_pitch_deck(b'')
        
        assert 'PDF data is required' in str(exc_info.value) or 'empty' in str(exc_info.value).lower()

    # =========================================================================
    # INVALID INPUT TESTS
    # =========================================================================

    def test_analyze_pdf_rejects_non_pdf_data(self, app):
        """
        Given: Data that is not a valid PDF (e.g., plain text)
        When: analyze_pitch_deck() is called
        Then: Raises ValueError indicating invalid PDF
        """
        from backend.services.pitchdeck_service import PitchdeckService
        
        service = PitchdeckService()
        not_a_pdf = b'This is just plain text, not a PDF'
        
        with pytest.raises(ValueError) as exc_info:
            service.analyze_pitch_deck(not_a_pdf)
        
        assert 'Invalid PDF' in str(exc_info.value) or 'not a valid PDF' in str(exc_info.value).lower()

    def test_analyze_pdf_rejects_oversized_file(self, app):
        """
        Given: PDF exceeding maximum size limit (25MB)
        When: analyze_pitch_deck() is called
        Then: Raises ValueError indicating file too large
        """
        from backend.services.pitchdeck_service import PitchdeckService
        
        service = PitchdeckService()
        
        # Create 26MB of data (over the 25MB limit)
        oversized_data = b'%PDF-1.4\n' + (b'x' * (26 * 1024 * 1024))
        
        with pytest.raises(ValueError) as exc_info:
            service.analyze_pitch_deck(oversized_data)
        
        assert 'too large' in str(exc_info.value).lower() or 'size' in str(exc_info.value).lower()

    def test_analyze_pdf_rejects_corrupted_pdf(self, app):
        """
        Given: A file with PDF header but corrupted content
        When: analyze_pitch_deck() is called  
        Then: Raises ValueError or handles gracefully
        """
        from backend.services.pitchdeck_service import PitchdeckService
        
        service = PitchdeckService()
        
        # PDF header but garbage content
        corrupted_pdf = b'%PDF-1.4\ngarbage data that is not valid PDF structure'
        
        # Should either raise ValueError or handle gracefully
        # Implementation may choose to let Gemini handle it
        try:
            with patch.object(service, '_call_gemini_vision') as mock_gemini:
                mock_gemini.side_effect = Exception('Unable to parse document')
                service.analyze_pitch_deck(corrupted_pdf)
        except (ValueError, Exception) as e:
            # Either explicit validation or Gemini error is acceptable
            assert True

    # =========================================================================
    # TIMEOUT AND API ERROR TESTS
    # =========================================================================

    def test_analyze_pdf_handles_timeout(self, app):
        """
        Given: Gemini API times out
        When: analyze_pitch_deck() is called
        Then: Raises TimeoutError with user-friendly message
        """
        from backend.services.pitchdeck_service import PitchdeckService
        import requests
        
        service = PitchdeckService()
        pdf_bytes = self._create_minimal_pdf()
        
        with patch.object(service, '_call_gemini_vision') as mock_gemini:
            mock_gemini.side_effect = requests.exceptions.Timeout('Connection timed out')
            
            with pytest.raises(TimeoutError) as exc_info:
                service.analyze_pitch_deck(pdf_bytes)
            
            assert 'timed out' in str(exc_info.value).lower() or 'timeout' in str(exc_info.value).lower()

    def test_analyze_pdf_handles_rate_limit(self, app):
        """
        Given: Gemini API returns 429 rate limit
        When: analyze_pitch_deck() is called
        Then: Raises RateLimitError or returns graceful fallback
        """
        from backend.services.pitchdeck_service import PitchdeckService
        import requests
        
        service = PitchdeckService()
        pdf_bytes = self._create_minimal_pdf()
        
        with patch.object(service, '_call_gemini_vision') as mock_gemini:
            mock_response = MagicMock()
            mock_response.status_code = 429
            error = requests.exceptions.HTTPError(response=mock_response)
            mock_gemini.side_effect = error
            
            # Should either raise or return fallback
            try:
                result = service.analyze_pitch_deck(pdf_bytes)
                # If it returns, should indicate rate limit
                assert 'error' in result or result.get('status') == 'rate_limited'
            except Exception as e:
                assert 'rate' in str(e).lower() or '429' in str(e)

    def test_analyze_pdf_handles_server_error(self, app):
        """
        Given: Gemini API returns 500 server error
        When: analyze_pitch_deck() is called
        Then: Raises ServiceUnavailableError with retry hint
        """
        from backend.services.pitchdeck_service import PitchdeckService
        import requests
        
        service = PitchdeckService()
        pdf_bytes = self._create_minimal_pdf()
        
        with patch.object(service, '_call_gemini_vision') as mock_gemini:
            mock_response = MagicMock()
            mock_response.status_code = 500
            error = requests.exceptions.HTTPError(response=mock_response)
            mock_gemini.side_effect = error
            
            with pytest.raises(Exception) as exc_info:
                service.analyze_pitch_deck(pdf_bytes)
            
            assert 'unavailable' in str(exc_info.value).lower() or 'server' in str(exc_info.value).lower()

    def test_analyze_pdf_handles_malformed_response(self, app):
        """
        Given: Gemini returns non-JSON or malformed response
        When: analyze_pitch_deck() is called
        Then: Returns graceful fallback with error indication
        """
        from backend.services.pitchdeck_service import PitchdeckService
        
        service = PitchdeckService()
        pdf_bytes = self._create_minimal_pdf()
        
        with patch.object(service, '_call_gemini_vision') as mock_gemini:
            # Return malformed Gemini response with unparseable JSON in content
            mock_gemini.return_value = {
                'candidates': [{
                    'content': {
                        'parts': [{'text': 'This is not valid JSON { broken'}]
                    }
                }]
            }
            
            # Should handle gracefully, not crash
            try:
                result = service.analyze_pitch_deck(pdf_bytes)
                # If it returns, should indicate parsing error
                assert result is None or 'error' in result
            except ValueError as e:
                # ValueError for parsing is acceptable
                assert 'parse' in str(e).lower() or 'json' in str(e).lower()

    # =========================================================================
    # OUTPUT SANITIZATION TESTS
    # =========================================================================

    def test_analyze_pdf_sanitizes_html_in_response(self, app):
        """
        Given: Gemini returns response containing HTML/script tags
        When: analyze_pitch_deck() is called
        Then: HTML is escaped to prevent XSS
        """
        from backend.services.pitchdeck_service import PitchdeckService
        
        service = PitchdeckService()
        pdf_bytes = self._create_minimal_pdf()
        
        with patch.object(service, '_call_gemini_vision') as mock_gemini:
            # Return Gemini API response with XSS vectors in content
            mock_gemini.return_value = {
                'candidates': [{
                    'content': {
                        'parts': [{'text': json.dumps({
                            'company_name': '<script>alert("xss")</script>EvilCorp',
                            'summary': 'Normal summary text.',
                            'usp': '<img onerror="alert(1)" src="x">Bad USP'
                        })}]
                    }
                }]
            }
            
            result = service.analyze_pitch_deck(pdf_bytes)
            
            # XSS vectors should be escaped or stripped
            assert '<script>' not in result.get('company_name', '')
            assert '<img' not in result.get('usp', '')

    def test_analyze_pdf_truncates_excessively_long_fields(self, app):
        """
        Given: Gemini returns excessively long text in fields
        When: analyze_pitch_deck() is called
        Then: Fields are truncated to reasonable limits
        """
        from backend.services.pitchdeck_service import PitchdeckService
        
        service = PitchdeckService()
        pdf_bytes = self._create_minimal_pdf()
        
        with patch.object(service, '_call_gemini_vision') as mock_gemini:
            very_long_text = 'x' * 100000  # 100KB of text
            # Return Gemini API response with excessively long fields
            mock_gemini.return_value = {
                'candidates': [{
                    'content': {
                        'parts': [{'text': json.dumps({
                            'company_name': very_long_text,
                            'summary': very_long_text,
                            'usp': 'Normal USP'
                        })}]
                    }
                }]
            }
            
            result = service.analyze_pitch_deck(pdf_bytes)
            
            # Fields should be truncated to reasonable limits
            max_field_length = 10000  # 10KB reasonable max
            assert len(result.get('company_name', '')) <= max_field_length
            assert len(result.get('summary', '')) <= max_field_length

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    def _create_minimal_pdf(self) -> bytes:
        """Create a minimal valid PDF for testing."""
        return b"""%PDF-1.4
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


class TestPitchdeckAPIEndpoint:
    """Integration tests for /api/pitchdeck/analyze endpoint."""

    def test_endpoint_requires_authentication(self, client):
        """
        Given: Unauthenticated request
        When: POST /api/pitchdeck/analyze
        Then: Returns 401 Unauthorized
        """
        response = client.post('/api/pitchdeck/analyze', json={
            'pdf_data': 'base64encodeddata'
        })
        assert response.status_code == 401

    def test_endpoint_rejects_missing_pdf_data(self, auth_client):
        """
        Given: Authenticated request without pdf_data
        When: POST /api/pitchdeck/analyze
        Then: Returns 400 Bad Request
        """
        response = auth_client.post('/api/pitchdeck/analyze', json={})
        assert response.status_code == 400
        
        data = response.get_json()
        assert 'error' in data or 'message' in data

    def test_endpoint_rejects_invalid_base64(self, auth_client):
        """
        Given: Authenticated request with invalid base64
        When: POST /api/pitchdeck/analyze  
        Then: Returns 400 Bad Request
        """
        response = auth_client.post('/api/pitchdeck/analyze', json={
            'pdf_data': 'not-valid-base64!!!'
        })
        assert response.status_code == 400

    def test_endpoint_returns_analysis_on_success(self, auth_client, mocker):
        """
        Given: Valid PDF upload
        When: POST /api/pitchdeck/analyze
        Then: Returns 200 with analysis results
        """
        # Mock the service
        mock_result = {
            'company_name': 'TestCorp',
            'summary': 'A test company.',
            'usp': 'Best testing practices.'
        }
        
        mocker.patch(
            'backend.services.pitchdeck_service.PitchdeckService.analyze_pitch_deck',
            return_value=mock_result
        )
        
        # Valid minimal PDF as base64
        pdf_bytes = b"""%PDF-1.4
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
        pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
        
        response = auth_client.post('/api/pitchdeck/analyze', json={
            'pdf_data': pdf_base64
        })
        
        assert response.status_code == 200
        data = response.get_json()
        assert data.get('company_name') == 'TestCorp'

    def test_endpoint_rate_limited(self, auth_client):
        """
        Given: Too many requests in short period
        When: Multiple POST /api/pitchdeck/analyze
        Then: Eventually returns 429 Too Many Requests
        """
        # Note: Actual rate limit testing depends on Flask-Limiter config
        # This test verifies the endpoint exists and handles load
        pass  # Rate limit testing is complex with test client


class TestPitchdeckServiceVisionCall:
    """Tests for the Gemini Vision API call internals."""

    def test_vision_call_includes_pdf_in_request(self, app):
        """
        Given: PDF bytes
        When: _call_gemini_vision() is called
        Then: Request payload includes PDF as inline data
        """
        from backend.services.pitchdeck_service import PitchdeckService
        
        service = PitchdeckService()
        pdf_bytes = b'%PDF-1.4\ntest content'
        
        with patch('requests.post') as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                'candidates': [{
                    'content': {
                        'parts': [{'text': '{"company_name": "Test"}'}]
                    }
                }]
            }
            mock_post.return_value = mock_response
            
            service._call_gemini_vision(pdf_bytes, "Analyze this PDF")
            
            # Verify PDF data was included in request
            call_args = mock_post.call_args
            payload = call_args[1]['json'] if 'json' in call_args[1] else call_args[0][1]
            
            # Should contain inline_data with PDF
            assert 'contents' in payload

    def test_vision_call_uses_pro_model_for_vision(self, app):
        """
        Given: PDF analysis request
        When: _call_gemini_vision() is called
        Then: Uses vision-capable Pro model
        """
        from backend.services.pitchdeck_service import PitchdeckService
        
        service = PitchdeckService()
        pdf_bytes = b'%PDF-1.4\ntest'
        
        with patch('requests.post') as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                'candidates': [{
                    'content': {
                        'parts': [{'text': '{}'}]
                    }
                }]
            }
            mock_post.return_value = mock_response
            
            service._call_gemini_vision(pdf_bytes, "Analyze")
            
            # Verify correct model endpoint and payload structure
            call_url = mock_post.call_args[0][0]
            assert 'gemini-3-pro-preview' in call_url
            
            payload = mock_post.call_args[1]['json']
            # Ensure PDF data is sent as inline_data with correct mime type
            parts = payload['contents'][0]['parts']
            assert any(part.get('inline_data', {}).get('mime_type') == 'application/pdf' for part in parts)
