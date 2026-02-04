import pytest
import base64
from unittest.mock import MagicMock, patch

@pytest.fixture
def mock_pitchdeck_service():
    with patch('backend.routes.pitchdeck.PitchdeckService') as mock:
        yield mock

def test_pitchdeck_billing_deduction(auth_client, db_session_fixture, mock_pitchdeck_service, test_user):
    """
    Verify that analyzing a pitchdeck deducts tokens correctly.
    Min cost is 1 CP.
    """
    # 1. Setup User Balance
    # test_user already has 100 CP from fixture
    initial_balance = test_user.token_balance.balance
    
    # 2. Mock Service
    service_instance = mock_pitchdeck_service.return_value
    service_instance.analyze_pitch_deck.return_value = {
        "company_name": "Test Co",
        "summary": "Startups."
    }
    
    # 3. Create Dummy PDF (base64)
    # Minimal PDF
    pdf_content = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n3 0 obj\n<< /Type /Page >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF"
    pdf_b64 = base64.b64encode(pdf_content).decode('utf-8')
    
    # 4. Request Analysis
    response = auth_client.post('/api/pitchdeck/analyze', json={
        'pdf_data': pdf_b64
    })
    
    assert response.status_code == 200
    data = response.json
    assert data['success'] is True
    assert data['cost_incurred'] >= 1
    
    # 5. Verify Deduction
    db_session_fixture.refresh(test_user.token_balance)
    assert test_user.token_balance.balance == initial_balance - data['cost_incurred']
    print(f"\nInitial: {initial_balance}, Cost: {data['cost_incurred']}, Final: {test_user.token_balance.balance}")
