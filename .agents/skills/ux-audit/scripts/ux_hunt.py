#!/usr/bin/env python3
"""Lightweight UX & Google Best Practices scanner for Authenix frontend & extension."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCAN_ROOTS = [
    REPO_ROOT / "satoricheck" / "frontend",
    REPO_ROOT / "satoricheck" / "extension",
]
VALID_EXTS = {".html", ".css", ".js"}
SKIP_DIRS = {"vendor", "node_modules", ".git", "assets"}


def get_changed_files() -> set[Path]:
    """Return modified/untracked files from git status."""
    res = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    changed: set[Path] = set()
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        rel = line[3:].strip().split(" -> ")[-1]
        changed.add((REPO_ROOT / rel).resolve())
    return changed


def iter_files(diff_only: bool) -> list[Path]:
    """Collect target frontend/extension files."""
    changed = get_changed_files() if diff_only else set()
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in VALID_EXTS:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if diff_only and path.resolve() not in changed:
                continue
            files.append(path)
    return sorted(files)


def scan_html(path: Path, lines: list[str]) -> list[tuple[int, str, str]]:
    """Scan HTML for WCAG, CLS, and Core Web Vitals leads."""
    leads: list[tuple[int, str, str]] = []
    in_head = False
    for idx, line in enumerate(lines, 1):
        low = line.lower()
        if "<head" in low:
            in_head = True
        elif "</head>" in low:
            in_head = False

        if in_head and "<script" in low and "src=" in low:
            if not any(attr in low for attr in ("defer", "async", 'type="module"', "type='module'")):
                leads.append((idx, "CWV/LCP", "Render-blocking <script> in <head> without defer/async/module"))

        if "<img" in low and "alt=" not in low:
            leads.append((idx, "WCAG/A11y", "<img> missing alt attribute"))

        if re.search(r"<(div|span)\b[^>]*\bonclick=", low) and "role=" not in low:
            leads.append((idx, "WCAG/A11y", "Clickable <div>/<span> without role='button' or semantic <button>"))

        if re.search(r"<(input|select|textarea)\b", low) and 'type="hidden"' not in low and "type='hidden'" not in low:
            if "aria-label" not in low and "aria-labelledby" not in low and "id=" not in low:
                leads.append((idx, "WCAG/A11y", "Form control missing id (for <label>) or aria-label"))
    return leads


def scan_css(path: Path, lines: list[str]) -> list[tuple[int, str, str]]:
    """Scan CSS for focus-ring removal, layout-thrashing animations, viewport bugs, and token hygiene."""
    leads: list[tuple[int, str, str]] = []
    rel = path.relative_to(REPO_ROOT).as_posix()
    is_variables = rel.endswith("_variables.css")
    is_helpers = rel.endswith("_helpers.css")
    in_keyframes = False

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("/*") or stripped.startswith("*"):
            continue
        low = stripped.lower()

        if "@keyframes" in low:
            in_keyframes = True
        elif in_keyframes and stripped == "}":
            in_keyframes = False

        if re.search(r"\boutline\s*:\s*(none|0)\b", low):
            leads.append((idx, "WCAG/Focus", "outline: none/0 — verify :focus-visible replacement exists"))

        if in_keyframes and re.search(r"\b(top|left|right|bottom|width|height|margin|padding)\s*:", low):
            leads.append((idx, "CWV/CLS", "Animating layout property in @keyframes; prefer transform/opacity"))

        if "100vh" in low and "100dvh" not in low and "/frontend/" in rel:
            leads.append((idx, "Mobile-UX", "100vh without 100dvh may clip on mobile browser address bars"))

        if not is_helpers and "!important" in low:
            leads.append((idx, "CSS-Hygiene", "!important used outside _helpers.css"))

        if not is_variables and not stripped.startswith("--") and re.search(r"#[0-9a-fA-F]{3,8}\b", stripped):
            # Ignore URL fragments like #icon
            if not re.search(r"url\([^)]*#", stripped):
                leads.append((idx, "Design-Tokens", "Hardcoded hex color; prefer var(--...) from _variables.css"))

    return leads


def scan_js(path: Path, lines: list[str]) -> list[tuple[int, str, str]]:
    """Scan JS for blocking native dialogs and unguarded UX anti-patterns."""
    leads: list[tuple[int, str, str]] = []
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        if re.search(r"\b(alert|confirm|prompt)\s*\(", stripped):
            leads.append((idx, "Material/UX", "Blocking native dialog (alert/confirm/prompt); use in-app modal/toast"))
    return leads


def main() -> int:
    parser = argparse.ArgumentParser(description="UX & Google Best Practices lead hunter")
    parser.add_argument("--diff", action="store_true", help="Scan only git-modified/untracked files")
    parser.add_argument("--strict", action="store_true", help="Exit 1 if any leads are found")
    args = parser.parse_args()

    files = iter_files(args.diff)
    total_leads = 0

    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        if path.suffix == ".html":
            leads = scan_html(path, lines)
        elif path.suffix == ".css":
            leads = scan_css(path, lines)
        elif path.suffix == ".js":
            leads = scan_js(path, lines)
        else:
            leads = []

        if leads:
            rel = path.relative_to(REPO_ROOT).as_posix()
            for line_no, category, msg in leads:
                print(f"[{category}] {rel}:{line_no} — {msg}")
                total_leads += 1

    print(f"\nUX Hunt complete: {len(files)} file(s) scanned, {total_leads} lead(s) surfaced.")
    if args.strict and total_leads > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
