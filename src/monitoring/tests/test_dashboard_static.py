"""Static checks of the dashboard front end (static/app.js).

Device names come from mDNS announcements that any host on the LAN can make,
so they must never be interpolated into innerHTML unescaped. If Node.js is
available, escapeHtml() itself is also executed.
"""
import os
import re
import shutil
import subprocess

import pytest

APP_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard", "static", "app.js")


def _source():
    with open(APP_JS, encoding="utf-8") as f:
        return f.read()


def test_hostnames_are_never_interpolated_raw():
    src = _source()
    # every ${...} that mentions a hostname must go through escapeHtml(...)
    for expr in re.findall(r"\$\{([^}]*hostname[^}]*)\}", src):
        assert expr.strip().startswith("escapeHtml("), f"unescaped hostname interpolation: ${{{expr}}}"


def test_api_posts_send_the_token_header():
    src = _source()
    for path in ("/api/upload_video", "/api/detect_tags", "/api/zero_tags", "/api/clear_zero"):
        calls = re.findall(r"fetch\('" + re.escape(path) + r"'[^)]*\)", src)
        assert calls, path
        assert all("authHeaders()" in c for c in calls), path


def test_lux_renamed_in_front_end():
    src = _source()
    assert "msg.lux_raw" in src
    assert "h.lux." not in src and "h.lux)" not in src


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_escape_html_executes():
    src = _source()
    fn = re.search(r"function escapeHtml\(value\) \{.*?\n\}", src, re.S).group(0)
    script = fn + "\nprocess.stdout.write(escapeHtml(`<img src=x onerror=\"alert('1')\">&`));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30).stdout
    assert out == "&lt;img src=x onerror=&quot;alert(&#39;1&#39;)&quot;&gt;&amp;"
