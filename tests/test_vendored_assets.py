"""Self-contained frontend: every asset the viewer page loads ships with it.

Code that runs with the archive's session must come from this server —
a CDN edge (or an npm publish the tag floats to) must never be able to ship
script into an authenticated viewer. This also keeps the UI working on
air-gapped deployments and stops the per-pageview IP/UA leak to third
parties.
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "src" / "web"
EXTERNAL = re.compile(r"https?://", re.IGNORECASE)


def test_index_html_references_no_external_origin():
    html = (WEB / "templates" / "index.html").read_text()
    for line in html.splitlines():
        stripped = line.strip()
        if stripped.startswith(("<script", "<link")):
            assert not EXTERNAL.search(stripped), f"external asset origin: {stripped[:100]}"


def test_every_referenced_static_asset_exists():
    html = (WEB / "templates" / "index.html").read_text()
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert refs, "expected /static/ asset references"
    for ref in refs:
        assert (WEB / ref.lstrip("/").removeprefix("static/../")).parent  # path shape sanity
        local = WEB / "static" / ref.removeprefix("/static/")
        assert local.is_file(), f"{ref} referenced but missing on disk"


def test_vendored_css_pulls_nothing_remote():
    """Only url(...) fetches count — license URLs in comments are fine."""
    vendor = WEB / "static" / "vendor"
    for css in vendor.rglob("*.css"):
        for target in re.findall(r"url\(([^)]+)\)", css.read_text()):
            assert not EXTERNAL.search(target), f"{css.name} fetches remotely: {target[:80]}"


def test_csp_names_no_remote_host():
    main_src = (WEB / "main.py").read_text()
    start = main_src.index('"script-src')
    end = main_src.index('"font-src', start)
    csp_block = main_src[start : end + 200]
    assert "https://" not in csp_block, "CSP still whitelists a remote host"


def test_service_worker_fetches_nothing_remote():
    sw = (WEB / "static" / "sw.js").read_text()
    assert not EXTERNAL.search(sw)


def test_vendor_manifest_covers_every_vendored_file():
    vendor = WEB / "static" / "vendor"
    manifest = (vendor / "VENDOR-MANIFEST.txt").read_text()
    for f in vendor.rglob("*"):
        if f.is_file() and f.name != "VENDOR-MANIFEST.txt":
            rel = f.relative_to(vendor).as_posix()
            assert rel in manifest, f"{rel} vendored but not recorded in VENDOR-MANIFEST.txt"
