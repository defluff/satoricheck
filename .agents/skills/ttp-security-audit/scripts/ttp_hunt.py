#!/usr/bin/env python3
"""Lightweight TTP vulnerability hunter for Authenix (Python 3.12+ stdlib only)."""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

USER_OWNED_MODELS = {"FactCheck", "MediaCheck", "TokenBalance", "Transaction", "Streak"}
UNBOUNDED_EXPORT_MODELS = {"FactCheck", "MediaCheck", "Transaction"}
HTTP_CALL_METHODS = {"get", "post", "put", "patch", "delete", "head", "request"}
EXEMPT_ROUTE_NAMES = {
    "health",
    "health_legacy",
    "serve_index",
    "serve_static",
    "stripe_webhook",
    "cleanup_expired_checks",
    "wizard_monthly_refill",
    "logout",
    "get_current_user",
    "get_extension_token",
    "google_login",
    "google_callback",
    "get_packages",
    "payment_success",
    "get_fact_check_history",
    "get_token_history",
}
EXEMPT_IDOR_FUNCS = {"stripe_webhook", "_fulfill_purchase", "cleanup_expired_checks", "wizard_monthly_refill"}

_RE_DANGEROUS_JS = re.compile(r"\b(?:eval|new\s+Function|document\.writeln?)\s*\(")
_RE_INNERHTML_ASSIGN = re.compile(r"\.innerHTML\s*=\s*")
_RE_SAFE_INTERP = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*\s*\?\s*(['\"]).*?\1\s*:\s*(['\"]).*?\2$|"
    r"^(?:this\.)?(?:currentSlide|index|seconds|remaining|cost|count|total|amountPrefix|modeBadgeLabel)$|"
    r"^(?:escaped|safe)[A-Za-z0-9_]*$|"
    r"^new\s+Date\([^)]*\)\.to[A-Za-z]+\([^)]*\)$"
)


def resolve_repo_root() -> Path:
    """Resolve git top-level so the script runs cleanly from repo root or subdirs."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return Path(out).resolve()
    except Exception:
        pass
    return Path(__file__).resolve().parents[4]


def _in_python_scope(rel: Path) -> bool:
    parts = rel.parts
    if len(parts) < 3 or parts[:2] != ("satoricheck", "backend") or rel.suffix != ".py":
        return False
    if any(p in {"__pycache__", "tests", "migrations"} for p in parts):
        return False
    return rel.name != "migrate_to_postgres.py"


def _in_js_scope(rel: Path) -> bool:
    parts = rel.parts
    if rel.suffix != ".js" or any(p in {"vendor", "__pycache__", "node_modules"} for p in parts):
        return False
    return parts[:3] == ("satoricheck", "frontend", "js") or parts[:2] == ("satoricheck", "extension")


def collect_files(repo_root: Path, diff_only: bool) -> tuple[list[Path], list[Path]]:
    """Return (python_files, js_files) to scan."""
    py_files: list[Path] = []
    js_files: list[Path] = []
    if diff_only:
        changed: set[str] = set()
        try:
            diff_out = subprocess.check_output(
                ["git", "diff", "--name-only", "--diff-filter=ACMRTUXB", "HEAD"],
                cwd=repo_root,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            changed.update(line.strip() for line in diff_out.splitlines() if line.strip())
        except subprocess.CalledProcessError:
            for cmd in (["git", "diff", "--name-only"], ["git", "diff", "--cached", "--name-only"]):
                out = subprocess.check_output(cmd, cwd=repo_root, stderr=subprocess.DEVNULL, text=True)
                changed.update(line.strip() for line in out.splitlines() if line.strip())
        untracked = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        changed.update(line.strip() for line in untracked.splitlines() if line.strip())
        for rel_str in sorted(changed):
            path = (repo_root / rel_str).resolve()
            if not path.is_file():
                continue
            rel = path.relative_to(repo_root)
            if _in_python_scope(rel):
                py_files.append(path)
            elif _in_js_scope(rel):
                js_files.append(path)
        return py_files, js_files

    backend_dir = repo_root / "satoricheck" / "backend"
    for path in sorted(backend_dir.rglob("*.py")):
        if _in_python_scope(path.relative_to(repo_root)):
            py_files.append(path)

    for js_root in (repo_root / "satoricheck" / "frontend" / "js", repo_root / "satoricheck" / "extension"):
        if js_root.exists():
            for path in sorted(js_root.rglob("*.js")):
                if _in_js_scope(path.relative_to(repo_root)):
                    js_files.append(path)
    return py_files, js_files


def _decorator_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return [ast.unparse(d) for d in func.decorator_list]


def _is_route_handler(decs: list[str]) -> bool:
    return any(".route(" in d for d in decs)


def _route_methods(decs: list[str]) -> set[str]:
    methods: set[str] = set()
    for d in decs:
        if ".route(" in d:
            for m in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                if f"'{m}'" in d or f'"{m}"' in d:
                    methods.add(m)
    return methods or {"GET"}


def scan_python_file(path: Path, rel: str) -> list[tuple[str, str, int, str]]:
    """Run AST-based TTP checks on a single backend Python file."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [("T1552", rel, exc.lineno or 1, f"Python syntax error during scan: {exc.msg}")]

    leads: list[tuple[str, str, int, str]] = []
    is_route_file = rel.startswith("satoricheck/backend/routes/")
    is_ssrf_exempt = rel in {
        "satoricheck/backend/services/safe_http.py",
        "satoricheck/backend/services/grok_service.py",
    }
    has_csv_escape = "_CSV_FORMULA_PREFIXES" in source or "_sanitize_csv_cell" in source

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and not is_ssrf_exempt:
            func_src = ast.unparse(node.func)
            if (
                any(func_src == f"requests.{m}" for m in HTTP_CALL_METHODS)
                or func_src == "urllib.request.urlopen"
                or func_src == "urlopen"
            ):
                leads.append(("T1210", rel, node.lineno, f"Direct outbound HTTP call `{func_src}` bypasses safe_http"))

        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        decs = _decorator_names(node)
        fn_src = ast.get_source_segment(source, node) or ""
        is_route = is_route_file and _is_route_handler(decs)
        has_scheduler = any("scheduler_secret_required" in d for d in decs)

        if is_route and not has_scheduler and node.name not in EXEMPT_ROUTE_NAMES:
            has_limiter = any(".limit(" in d for d in decs)
            methods = _route_methods(decs)
            expensive = any(tok in fn_src for tok in ("gemini", "stripe", "grok", "csv.writer", "safe_http"))
            if not has_limiter and (methods - {"GET"} or expensive):
                leads.append(("T1499", rel, node.lineno, f"Route `{node.name}` lacks explicit `@limiter.limit(...)`"))

        if is_route and not has_scheduler:
            b64_calls = [
                sub for sub in ast.walk(node)
                if isinstance(sub, ast.Call) and ast.unparse(sub.func).endswith("b64decode")
            ]
            for call in b64_calls:
                has_len_before = any(
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "len"
                    and sub.lineno < call.lineno
                    for sub in ast.walk(node)
                )
                if not has_len_before:
                    leads.append(("T1499", rel, call.lineno, f"`b64decode` in `{node.name}` lacks prior `len(...)` check"))

            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    targets = [ast.unparse(t) for t in sub.targets]
                    if "limit" in targets and isinstance(sub.value, ast.Call) and ast.unparse(sub.value.func) == "min":
                        if "max(" not in ast.unparse(sub.value):
                            leads.append(("T1499", rel, sub.lineno, f"`limit = min(...)` in `{node.name}` lacks `max(1, ...)` lower clamp"))

                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "all":
                    chain_src = ast.unparse(sub)
                    uses_export_model = any(
                        m in chain_src or f"query({m})" in fn_src for m in UNBOUNDED_EXPORT_MODELS
                    )
                    if uses_export_model and ".limit(" not in chain_src:
                        leads.append(("T1499", rel, sub.lineno, f"Unbounded `.all()` query in route `{node.name}` lacks `.limit(...)`"))

            if "csv.writer(" in fn_src and not has_csv_escape:
                leads.append(("T1204.002", rel, node.lineno, f"`csv.writer` in `{node.name}` lacks formula-prefix escaping"))

            has_cd_guard = any(guard in fn_src for guard in ("fullmatch", "secure_filename", "ALLOWED_"))
            if not has_cd_guard:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Assign) and any(
                        isinstance(t, ast.Subscript) and "Content-Disposition" in ast.unparse(t)
                        for t in sub.targets
                    ):
                        val_src = ast.unparse(sub.value)
                        if any(isinstance(n, ast.JoinedStr) for n in ast.walk(sub.value)) or ".format(" in val_src:
                            leads.append(("T1204.002", rel, sub.lineno, f"`Content-Disposition` in `{node.name}` interpolates unvalidated value"))
                    elif isinstance(sub, ast.Dict):
                        for k, v in zip(sub.keys, sub.values):
                            if isinstance(k, ast.Constant) and k.value == "Content-Disposition" and v is not None:
                                val_src = ast.unparse(v)
                                if any(isinstance(n, ast.JoinedStr) for n in ast.walk(v)) or ".format(" in val_src:
                                    leads.append(("T1204.002", rel, v.lineno, f"`Content-Disposition` in `{node.name}` interpolates unvalidated value"))

            exc_names = {h.name for h in ast.walk(node) if isinstance(h, ast.ExceptHandler) and h.name}
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and ast.unparse(sub.func) in {"APIError", "jsonify"}:
                    call_src = ast.unparse(sub)
                    leaks_str = any(f"str({ename})" in call_src for ename in exc_names)
                    leaks_fstr = any(
                        isinstance(fv, ast.FormattedValue)
                        and isinstance(fv.value, ast.Name)
                        and fv.value.id in exc_names
                        for fv in ast.walk(sub)
                    )
                    if leaks_str or leaks_fstr:
                        leads.append(("T1552", rel, sub.lineno, f"`{ast.unparse(sub.func)}` returns raw `str(exc)` in `{node.name}`"))

        if not has_scheduler and node.name not in EXEMPT_IDOR_FUNCS:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and ast.unparse(sub.func) == "db_session.query":
                    if sub.args and ast.unparse(sub.args[0]) in USER_OWNED_MODELS:
                        if "user_id" not in fn_src and "user.id" not in fn_src:
                            model_name = ast.unparse(sub.args[0])
                            leads.append(("T1068", rel, sub.lineno, f"`db_session.query({model_name})` in `{node.name}` lacks `user_id` scope"))

    return leads


def _extract_js_rhs(text: str, start_idx: int) -> str:
    """Extract RHS expression from `.innerHTML =` across single/multi-line statements."""
    i = start_idx
    n = len(text)
    in_quote: str | None = None
    escaped = False
    paren_depth = 0
    brace_depth = 0
    while i < n:
        ch = text[i]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif in_quote:
            if ch == in_quote:
                in_quote = None
        else:
            if ch in {"'", '"', "`"}:
                in_quote = ch
            elif ch == "(":
                paren_depth += 1
            elif ch == ")" and paren_depth > 0:
                paren_depth -= 1
            elif ch == "{":
                brace_depth += 1
            elif ch == "}" and brace_depth > 0:
                brace_depth -= 1
            elif ch == ";" and paren_depth == 0 and brace_depth == 0:
                break
            elif ch == "\n" and paren_depth == 0 and brace_depth == 0:
                rest = text[i + 1 :].lstrip()
                if rest and rest[0] not in {"+", "?", ":"}:
                    break
        i += 1
    return text[start_idx:i].strip()


def _extract_template_interps(expr: str) -> list[str]:
    """Extract balanced `${...}` expressions from a JS template literal or block."""
    results: list[str] = []
    i = 0
    n = len(expr)
    while i < n - 1:
        if expr[i] == "$" and expr[i + 1] == "{":
            depth = 1
            start = i + 2
            j = start
            in_q: str | None = None
            esc = False
            while j < n and depth > 0:
                c = expr[j]
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif in_q:
                    if c == in_q:
                        in_q = None
                else:
                    if c in {"'", '"', "`"}:
                        in_q = c
                    elif c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                j += 1
            if depth == 0:
                results.append(expr[start : j - 1].strip())
                i = j
                continue
        i += 1
    return results


def _is_single_call(expr: str, prefixes: tuple[str, ...]) -> bool:
    """Return True iff expr is a single balanced function call starting with one of prefixes."""
    matched_prefix = next((p for p in prefixes if expr.startswith(p)), None)
    if not matched_prefix:
        return False
    depth = 1
    i = len(matched_prefix)
    n = len(expr)
    in_q: str | None = None
    esc = False
    while i < n and depth > 0:
        ch = expr[i]
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif in_q:
            if ch == in_q:
                in_q = None
        else:
            if ch in {"'", '"', "`"}:
                in_q = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        i += 1
    return depth == 0 and i == n


def _is_safe_innerhtml_rhs(rhs: str, preceding_window: str) -> bool:
    if rhs in {"''", '""', "``", "originalText", "originalHtml"}:
        return True
    if _is_single_call(rhs, ("DOMPurify.sanitize(",)):
        return True
    ternary_match = re.fullmatch(r"[^?]+\?\s*(.+?)\s*:\s*(.+)", rhs, flags=re.DOTALL)
    if ternary_match:
        b1, b2 = ternary_match.group(1).strip(), ternary_match.group(2).strip()
        if all(b in {"''", '""'} or _is_single_call(b, ("DOMPurify.sanitize(",)) for b in (b1, b2)):
            return True
    if rhs.startswith(("'", '"')) and rhs.endswith(("'", '"')) and "+" not in rhs:
        return True
    if re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\.join\(['\"]['\"]\)", rhs):
        var_name = rhs.split(".", 1)[0]
        if f"{var_name}.push(" in preceding_window and (
            "DOMPurify.sanitize(" in preceding_window or "escapeHtml(" in preceding_window
        ):
            return True
    if rhs.startswith("`") and rhs.endswith("`"):
        interps = _extract_template_interps(rhs)
        if not interps:
            return True
        return all(_is_safe_interp_expr(expr, preceding_window) for expr in interps)
    if ".map(" in rhs and ".join(" in rhs and "`" in rhs:
        interps = _extract_template_interps(rhs)
        if interps and all(_is_safe_interp_expr(expr, rhs + "\n" + preceding_window) for expr in interps):
            return True
    return False


def _is_safe_interp_expr(expr: str, context: str) -> bool:
    if _is_single_call(
        expr,
        ("escapeHtml(", "_escapeHtml(", "this.escapeHtml(", "this._escapeHtml(", "DOMPurify.sanitize("),
    ):
        return True
    if _RE_SAFE_INTERP.search(expr):
        return True
    # Nested ternary whose branches only contain HTML tags + safe interpolations
    if "?" in expr and ":" in expr:
        nested = _extract_template_interps(expr)
        if nested and all(_is_safe_interp_expr(sub, context) for sub in nested):
            return True
    # Identifier built via escapeHtml or numeric coercion in surrounding lines
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", expr):
        base = expr.split(".")[-1]
        if base in {"date", "amount", "prob", "probLabel"}:
            return True
        if re.search(rf"\b{re.escape(base)}\b[^\n;]*?(?:escapeHtml\(|DOMPurify\.sanitize\()", context):
            return True
    return False


def scan_js_file(path: Path, rel: str) -> list[tuple[str, str, int, str]]:
    """Scan frontend/extension JS for dangerous sinks and unescaped innerHTML."""
    text = path.read_text(encoding="utf-8")
    leads: list[tuple[str, str, int, str]] = []
    for match in _RE_DANGEROUS_JS.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        leads.append(("T1059.007", rel, line, f"Dangerous JS sink `{match.group(0).strip()}`"))

    for match in _RE_INNERHTML_ASSIGN.finditer(text):
        rhs = _extract_js_rhs(text, match.end())
        window_start = max(0, match.start() - 1200)
        preceding_window = text[window_start : match.start()]
        if not _is_safe_innerhtml_rhs(rhs, preceding_window):
            line = text.count("\n", 0, match.start()) + 1
            leads.append(("T1059.007", rel, line, "Dynamic `innerHTML` assignment without `DOMPurify.sanitize` or `escapeHtml`"))
    return leads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Authenix lightweight TTP security lead scanner.")
    parser.add_argument("--diff", action="store_true", help="Scan only modified and untracked files in scope.")
    parser.add_argument("--strict", action="store_true", help="Exit with code 1 if any leads are found.")
    args = parser.parse_args(argv)

    repo_root = resolve_repo_root()
    py_files, js_files = collect_files(repo_root, args.diff)
    if args.diff and not py_files and not js_files:
        print("No changed backend/JS files in scope.")
        return 0

    leads: list[tuple[str, str, int, str]] = []
    for path in py_files:
        leads.extend(scan_python_file(path, path.relative_to(repo_root).as_posix()))
    for path in js_files:
        leads.extend(scan_js_file(path, path.relative_to(repo_root).as_posix()))

    for ttp_id, rel, line, reason in sorted(leads, key=lambda item: (item[1], item[2], item[0])):
        print(f"[{ttp_id}] {rel}:{line} — {reason}")

    total_files = len(py_files) + len(js_files)
    if leads:
        print(f"Found {len(leads)} lead(s) across {total_files} file(s).")
        return 1 if args.strict else 0
    print(f"No TTP leads found across {total_files} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
