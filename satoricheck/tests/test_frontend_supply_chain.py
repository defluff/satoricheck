"""
Frontend supply-chain tests.

1. Third-party <script> tags must be integrity-pinned (SRI) and fetched anonymously.
2. No frontend JS may pull scripts/workers from a CDN at runtime: dynamic loads bypass the
   SRI check on index.html (this is how an un-pinned PDF.js worker previously slipped by).
3. Vendored libraries must match the hashes recorded in their README (detects tampering or
   accidental edits).
"""
import base64
import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

FRONTEND_DIR = Path(__file__).resolve().parent.parent / 'frontend'
CDN_HOSTS = ('cdn.jsdelivr.net', 'cdnjs.cloudflare.com', 'unpkg.com', 'cdn.skypack.dev')


class _ScriptCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.scripts.append(dict(attrs))


def _external_scripts(html_path):
    collector = _ScriptCollector()
    collector.feed(html_path.read_text(encoding='utf-8'))
    return [
        attrs for attrs in collector.scripts
        if (attrs.get('src') or '').startswith(('http://', 'https://', '//'))
    ]


def _sha384_b64(path):
    return base64.b64encode(hashlib.sha384(path.read_bytes()).digest()).decode()


EXTERNAL_SCRIPTS = _external_scripts(FRONTEND_DIR / 'index.html')
JS_FILES = sorted((FRONTEND_DIR / 'js').rglob('*.js'))


def test_external_scripts_were_discovered():
    assert EXTERNAL_SCRIPTS, 'Parser found no external scripts; the SRI check would be vacuous'


@pytest.mark.parametrize('attrs', EXTERNAL_SCRIPTS, ids=[a['src'] for a in EXTERNAL_SCRIPTS])
def test_external_script_has_sri_and_anonymous_crossorigin(attrs):
    assert attrs['src'].startswith('https://'), 'External scripts must use HTTPS'
    assert re.match(r'^sha(384|512)-[A-Za-z0-9+/=]+', attrs.get('integrity', '')), (
        f"{attrs['src']} is missing a sha384/sha512 integrity attribute"
    )
    assert attrs.get('crossorigin') == 'anonymous'


@pytest.mark.parametrize('js_file', JS_FILES, ids=[p.name for p in JS_FILES])
def test_js_does_not_load_code_from_cdn_at_runtime(js_file):
    source = js_file.read_text(encoding='utf-8')
    offenders = [host for host in CDN_HOSTS if host in source]
    assert not offenders, (
        f'{js_file.name} references {offenders}; self-host the asset or pin it with SRI '
        'in index.html (workers and dynamic imports cannot be SRI-checked)'
    )


def test_vendored_files_match_recorded_hashes():
    checked = 0
    for readme in FRONTEND_DIR.glob('vendor/*/README.md'):
        rows = re.findall(r'\|\s*`([^`]+\.js)`\s*\|\s*`([^`]+)`\s*\|', readme.read_text())
        for filename, expected in rows:
            assert _sha384_b64(readme.parent / filename) == expected, (
                f'{readme.parent.name}/{filename} does not match its recorded SHA-384'
            )
            checked += 1
    assert checked >= 2, 'No vendored hashes were verified'
