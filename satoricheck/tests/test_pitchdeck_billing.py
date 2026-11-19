import base64
import json
from unittest.mock import MagicMock, patch
import pytest


def _make_pdf_with_pages(page_count: int) -> str:
    pdf_content = (
        f"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        f"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count {page_count} >>\nendobj\n"
        f"3 0 obj\n<< /Type /Page >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF"
    ).encode('utf-8')
    return base64.b64encode(pdf_content).decode('utf-8')


@pytest.mark.parametrize(
    'page_count,expected_cost',
    [
        (1, 2),
        (10, 2),
        (12, 4),
        (20, 4),
        (60, 12),
        (75, 12),
    ],
)
def test_pitchdeck_billing_deduction_tiers(auth_client, db_session_fixture, test_user, page_count, expected_cost):
    """Pitch deck analysis bills 2 CP per 10 slides (min 2, max 12) and calls Argon vision + Flash cache."""
    from backend.models import TokenBalance
    from backend.services import get_gemini_service, get_pitchdeck_service

    initial_balance = test_user.token_balance.balance
    pitchdeck_svc = get_pitchdeck_service()
    gemini_svc = get_gemini_service()

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = json.dumps({
        'company_name': 'Test Co',
        'summary': 'Startups.',
        'usp': 'Strong moat.',
    })
    mock_client.models.generate_content.return_value = mock_resp
    mock_cache = MagicMock()
    mock_cache.name = 'cachedContents/deck-tier-test'
    mock_client.caches.create.return_value = mock_cache

    with patch.object(pitchdeck_svc, 'client', mock_client), \
         patch.object(gemini_svc, 'client', mock_client):
        response = auth_client.post('/api/pitchdeck/analyze', json={
            'pdf_data': _make_pdf_with_pages(page_count)
        })

    assert response.status_code == 200
    data = response.get_json()
    assert data['success'] is True
    assert data['cost_incurred'] == expected_cost
    assert mock_client.models.generate_content.call_args.kwargs['model'] == pitchdeck_svc.MODEL_MEDIA
    assert mock_client.caches.create.call_args.kwargs['model'] == pitchdeck_svc.MODEL

    db_session_fixture.expire_all()
    bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
    assert bal.balance == initial_balance - expected_cost


def test_pitchdeck_rejects_insufficient_balance_for_min_2_cp(auth_client, db_session_fixture, test_user):
    """User with 1 CP cannot analyze a 1-slide pitch deck (requires 2 CP minimum)."""
    from backend.models import TokenBalance
    from backend.services import get_pitchdeck_service

    bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
    bal.balance = 1
    db_session_fixture.commit()

    pitchdeck_svc = get_pitchdeck_service()
    mock_client = MagicMock()

    with patch.object(pitchdeck_svc, 'client', mock_client):
        response = auth_client.post('/api/pitchdeck/analyze', json={
            'pdf_data': _make_pdf_with_pages(1)
        })

    assert response.status_code == 403
    assert 'requires 2 CP' in response.get_json()['error']
    mock_client.models.generate_content.assert_not_called()

    db_session_fixture.expire_all()
    refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
    assert refreshed.balance == 1


def test_pitchdeck_refunds_multi_cp_on_vision_failure(auth_client, db_session_fixture, test_user):
    """If Argon vision analysis fails after reserving 4 CP (12 slides), all 4 CP are refunded."""
    from backend.models import TokenBalance
    from backend.services import get_pitchdeck_service

    initial_balance = test_user.token_balance.balance
    pitchdeck_svc = get_pitchdeck_service()
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError('Upstream Gemini failure')

    with patch.object(pitchdeck_svc, 'client', mock_client), \
         patch('requests.post') as mock_rest_post:
        response = auth_client.post('/api/pitchdeck/analyze', json={
            'pdf_data': _make_pdf_with_pages(12)
        })

    assert response.status_code == 500
    mock_rest_post.assert_not_called()
    db_session_fixture.expire_all()
    bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
    assert bal.balance == initial_balance
