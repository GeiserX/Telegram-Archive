"""Self-contained frontend: every asset the viewer page loads ships with it.

Code that runs with the archive's session must come from this server —
a CDN edge (or an npm publish the tag floats to) must never be able to ship
script into an authenticated viewer. This also keeps the UI working on
air-gapped deployments and stops the per-pageview IP/UA leak to third
parties.
"""

import hashlib
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "src" / "web"
STATIC = (WEB / "static").resolve()
VENDOR = STATIC / "vendor"
# Any absolute network location: explicit http(s) scheme or protocol-relative //host.
EXTERNAL = re.compile(r"(?:https?:)?//", re.IGNORECASE)


def test_index_html_references_no_external_origin():
    html = (WEB / "templates" / "index.html").read_text()
    tags = re.findall(r"<(?:script|link)\b[^>]*>", html)
    urls = [u for tag in tags for u in re.findall(r'(?:src|href)="([^"]+)"', tag)]
    assert urls, "expected script/link references in index.html"
    for url in urls:
        if url.startswith("data:"):
            continue
        assert not EXTERNAL.search(url), f"external asset origin: {url[:100]}"


def test_every_referenced_static_asset_exists():
    html = (WEB / "templates" / "index.html").read_text()
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert refs, "expected /static/ asset references"
    for ref in refs:
        candidate = (WEB / ref.split("?")[0].lstrip("/")).resolve()
        assert candidate.is_relative_to(STATIC), f"{ref} escapes /static/"
        assert candidate.is_file(), f"{ref} referenced but missing on disk"


def test_vendored_css_targets_exist_locally():
    """Every url(...) a vendored stylesheet fetches must resolve inside /static."""
    checked = 0
    for css in VENDOR.rglob("*.css"):
        for raw in re.findall(r"url\(([^)]+)\)", css.read_text()):
            target = raw.strip().strip("'\"")
            if target.startswith("data:"):
                continue
            assert not EXTERNAL.search(target), f"{css.name} fetches remotely: {target[:80]}"
            path = target.split("?")[0].split("#")[0]
            if path.startswith("/static/"):
                resolved = (WEB / path.lstrip("/")).resolve()
            else:
                resolved = (css.parent / path).resolve()
            assert resolved.is_relative_to(STATIC), f"{css.name}: {target} escapes /static"
            assert resolved.is_file(), f"{css.name}: {target} missing on disk"
            checked += 1
    assert checked, "expected url() targets in vendored css"


def test_csp_allows_only_local_sources():
    """Parse the policy directive-by-directive: keyword/data/blob sources only.

    Rejects hosts, bare schemes (http:, ws:, wss:) and protocol-relative
    sources in one pass, so a future 'just one origin' edit goes red here.
    """
    main_src = (WEB / "main.py").read_text()
    start = main_src.index('"Content-Security-Policy"')
    block = main_src[start : main_src.index(")", start)]
    csp = "".join(re.findall(r'"([^"]*)"', block)[1:])
    allowed = {"'self'", "'unsafe-inline'", "'unsafe-eval'", "data:", "blob:"}
    directives = [d.strip() for d in csp.split(";") if d.strip()]
    assert len(directives) >= 6, f"CSP lost directives: {csp!r}"
    for directive in directives:
        name, *sources = directive.split()
        assert sources, f"CSP directive {name} has no sources"
        for source in sources:
            assert source in allowed, f"CSP {name} allows non-local source: {source}"


def test_service_worker_fetches_nothing_remote():
    sw = (WEB / "static" / "sw.js").read_text()
    literals = re.findall(r"""["'`]([^"'`\n]*)["'`]""", sw)
    assert literals, "expected string literals in sw.js"
    for lit in literals:
        if lit.startswith("data:"):
            continue
        assert not EXTERNAL.search(lit), f"sw.js references remote location: {lit[:80]}"


def test_vendor_manifest_records_every_file_with_matching_hash():
    """Exact two-way path match plus recomputed sha256 per vendored file."""
    record = re.compile(r"^(\S+) -> ([0-9a-f]{64})\s+(\S+)(?:\s+\(.*\))?$")
    recorded = {}
    for line in (VENDOR / "VENDOR-MANIFEST.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = record.match(line)
        assert match, f"malformed manifest line: {line[:100]}"
        _origin, digest, rel = match.groups()
        assert rel not in recorded, f"duplicate manifest entry: {rel}"
        recorded[rel] = digest
    on_disk = {
        f.relative_to(VENDOR).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in VENDOR.rglob("*")
        if f.is_file() and f.name != "VENDOR-MANIFEST.txt"
    }
    assert recorded.keys() == on_disk.keys(), (
        f"manifest/disk drift: unrecorded={sorted(on_disk.keys() - recorded.keys())} "
        f"stale={sorted(recorded.keys() - on_disk.keys())}"
    )
    for rel, digest in recorded.items():
        assert on_disk[rel] == digest, f"{rel}: recorded sha256 does not match file bytes"
