"""
AST-based test suite integrity gate.

Enforces Test Integrity invariants across all non-E2E pytest files:
1. No tautological assertions (`assert True`, `assert ... or True`).
2. No permissive boolean-OR assertions (`assert a or b`).
3. No permissive status-code membership sets (`assert res.status_code in [200, 400]`).
4. No vacuous test functions lacking assertions (`assert`, `pytest.raises`, `.assert_*`).
5. No mocking of local cryptographic or SSRF validators (`stripe.Webhook.construct_event`, `_is_private_ip`).
"""
import ast
from pathlib import Path
import pytest

TESTS_DIR = Path(__file__).resolve().parent

FORBIDDEN_MOCK_TARGETS = (
    'stripe.Webhook.construct_event',
    'Webhook.construct_event',
    '_is_private_ip',
)


def _find_integrity_violations(source: str, filename: str = '<memory>') -> list[str]:
    """Parse Python test source into an AST and return a list of integrity violations."""
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []

    for node in ast.walk(tree):
        # 1-3. Inspect `assert` statements
        if isinstance(node, ast.Assert):
            test = node.test
            if isinstance(test, ast.Constant) and test.value is True:
                violations.append(f'{filename}:{node.lineno}: tautological `assert True`')
            elif isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
                violations.append(
                    f'{filename}:{node.lineno}: permissive `or` assertion `{ast.unparse(test)}`'
                )
            elif isinstance(test, ast.Compare):
                left_src = ast.unparse(test.left)
                if 'status_code' in left_src and any(isinstance(op, ast.In) for op in test.ops):
                    violations.append(
                        f'{filename}:{node.lineno}: permissive status_code set `{ast.unparse(test)}`'
                    )

        # 4. Inspect `test_*` functions for at least one assertion or exception check
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
            has_verification = False
            for child in ast.walk(node):
                if isinstance(child, ast.Assert):
                    has_verification = True
                    break
                if isinstance(child, ast.Call):
                    func_src = ast.unparse(child.func)
                    if any(tok in func_src for tok in ('raises', 'fail', 'warns', 'assert_')):
                        has_verification = True
                        break
            if not has_verification:
                violations.append(
                    f'{filename}:{node.lineno}: vacuous test `{node.name}` has no assert or pytest.raises'
                )

        # 5. Inspect `patch(...)` / `patch.object(...)` calls for forbidden local crypto/SSRF mocks
        elif isinstance(node, ast.Call):
            func_src = ast.unparse(node.func)
            if 'patch' in func_src and node.args:
                args_src = ' '.join(
                    arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                    else ast.unparse(arg)
                    for arg in node.args[:2]
                )
                for forbidden in FORBIDDEN_MOCK_TARGETS:
                    if forbidden in args_src:
                        violations.append(
                            f'{filename}:{node.lineno}: forbidden mock of local validator `{forbidden}`'
                        )
                        break

    return violations


def test_entire_suite_has_zero_ast_integrity_violations():
    """Every unit/integration test file in tests/ must pass the AST integrity audit."""
    test_files = sorted(
        p for p in TESTS_DIR.glob('test_*.py')
        if p.name != Path(__file__).name
    )
    assert len(test_files) >= 15

    all_violations: list[str] = []
    for path in test_files:
        source = path.read_text(encoding='utf-8')
        all_violations.extend(_find_integrity_violations(source, filename=path.name))

    assert all_violations == [], 'Test integrity violations found:\n' + '\n'.join(all_violations)


@pytest.mark.parametrize(
    'bad_code,expected_fragment',
    [
        ('def test_x():\n    assert True\n', 'tautological `assert True`'),
        ('def test_x():\n    assert "a" in d or "b" in d\n', 'permissive `or` assertion'),
        ('def test_x():\n    assert res.status_code in [200, 400]\n', 'permissive status_code set'),
        ('def test_x():\n    x = 1 + 1\n', 'vacuous test `test_x`'),
        (
            'def test_x():\n    with patch("stripe.Webhook.construct_event"):\n        assert 1 == 1\n',
            'forbidden mock of local validator `stripe.Webhook.construct_event`',
        ),
    ],
)
def test_ast_auditor_catches_each_prohibited_pattern(bad_code, expected_fragment):
    """Verify the AST auditor rejects every blacklisted test anti-pattern."""
    violations = _find_integrity_violations(bad_code, filename='synthetic_test.py')
    assert len(violations) == 1
    assert expected_fragment in violations[0]

