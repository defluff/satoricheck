"""
Route authorization coverage gate.

Every registered route must belong to exactly one explicit category below. Anything not
declared PUBLIC / SCHEDULER / WEBHOOK is treated as a user-protected route and must reject
unauthenticated callers. A new route therefore fails closed: forgetting @login_required
makes this suite fail, and forgetting to categorise a deliberately public route does too.

This replaces the copy-pasted inline scripts previously embedded in the security workflows.
"""
import pytest

from backend.server import app as flask_app

# Routes that are intentionally reachable without a user session (alphabetical).
PUBLIC_ENDPOINTS = {
    'auth.google_callback',
    'auth.google_login',
    'auth.login',
    'auth.signup',
    'billing.get_packages',
    'billing.payment_success',
    'health',
    'health_legacy',
    'serve_index',
    'serve_static',
    'static',
}

# Cloud Scheduler cron endpoints: authenticated by X-Scheduler-Secret, not by user session.
SCHEDULER_ENDPOINTS = {
    'billing.wizard_monthly_refill',
    'factcheck.cleanup_expired_checks',
}

# Third-party callbacks authenticated by a signature (Stripe), not by user session.
WEBHOOK_ENDPOINTS = {
    'billing.stripe_webhook',
}

NON_USER_PROTECTED = PUBLIC_ENDPOINTS | SCHEDULER_ENDPOINTS | WEBHOOK_ENDPOINTS


def _routes(endpoints=None, exclude=frozenset()):
    """Return (rule, method, endpoint) triples for pytest parametrization."""
    triples = []
    for rule in flask_app.url_map.iter_rules():
        if endpoints is not None and rule.endpoint not in endpoints:
            continue
        if rule.endpoint in exclude:
            continue
        for method in sorted(rule.methods - {'HEAD', 'OPTIONS'}):
            triples.append((rule.rule, method, rule.endpoint))
    return triples


def _ids(triples):
    return [f'{method} {rule}' for rule, method, _ in triples]


USER_PROTECTED = _routes(exclude=NON_USER_PROTECTED)
SCHEDULER_ROUTES = _routes(endpoints=SCHEDULER_ENDPOINTS)
WEBHOOK_ROUTES = _routes(endpoints=WEBHOOK_ENDPOINTS)


def test_every_declared_endpoint_exists():
    """Stale allowlist entries would silently widen the exemption if a route is renamed."""
    registered = {rule.endpoint for rule in flask_app.url_map.iter_rules()}
    stale = NON_USER_PROTECTED - registered
    assert not stale, f'Allowlisted endpoints no longer registered: {sorted(stale)}'


def test_categories_do_not_overlap():
    """An endpoint in two categories makes the intended contract ambiguous."""
    assert not (PUBLIC_ENDPOINTS & SCHEDULER_ENDPOINTS)
    assert not (PUBLIC_ENDPOINTS & WEBHOOK_ENDPOINTS)
    assert not (SCHEDULER_ENDPOINTS & WEBHOOK_ENDPOINTS)


def test_user_protected_routes_were_discovered():
    """Guards against the parametrized suites silently collecting zero routes."""
    assert len(USER_PROTECTED) >= 20


@pytest.mark.parametrize('rule,method,endpoint', USER_PROTECTED, ids=_ids(USER_PROTECTED))
def test_user_protected_route_rejects_unauthenticated_request(client, rule, method, endpoint):
    response = client.open(rule, method=method, json={})
    assert response.status_code in (401, 403), (
        f'{method} {rule} ({endpoint}) answered {response.status_code} to an unauthenticated '
        'request. Add @login_required, or declare the route in an allowlist category.'
    )


@pytest.mark.parametrize('rule,method,endpoint', SCHEDULER_ROUTES, ids=_ids(SCHEDULER_ROUTES))
def test_scheduler_route_rejects_missing_secret(client, rule, method, endpoint):
    response = client.open(rule, method=method)
    assert response.status_code == 401, f'{endpoint} must require X-Scheduler-Secret'


@pytest.mark.parametrize('rule,method,endpoint', WEBHOOK_ROUTES, ids=_ids(WEBHOOK_ROUTES))
def test_webhook_route_rejects_unsigned_request(client, rule, method, endpoint):
    response = client.open(rule, method=method, json={'type': 'checkout.session.completed'})
    assert response.status_code in (400, 401, 403), (
        f'{endpoint} accepted an unsigned payload with {response.status_code}'
    )
