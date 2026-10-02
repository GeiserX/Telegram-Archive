"""The sidebar's status line, executed: Live stays Live while the daily full pass runs.

Since 9.2.0 the listener is on by default and the full pass runs once a day.
The real ``sidebarStatusNow`` and ``backupHealthOf`` are lifted out of the
template and run under node with stubbed refs, so these pin what the line says
for each state rather than the source text.
"""

import json
import shutil
import subprocess

import pytest
from test_frontend_audit_fixes import INDEX_HTML, _extract_const_arrow_function

LAST = "2026-01-15T03:00:05Z"


def _status_line(*, listener: bool, in_progress: bool, stats_at: str | None, role: str = "master") -> dict:
    html = INDEX_HTML.read_text(encoding="utf-8")
    helpers = "\n".join(
        _extract_const_arrow_function(html, name, asynchronous=False) for name in ("backupHealthOf", "sidebarStatusNow")
    )
    script = f"""
"use strict";
const moment = {{ utc: iso => ({{ isBefore: other => Date.parse(iso) < other.t, t: Date.parse(iso) }}) }};
const ref = value => ({{ value }});
const computed = getter => ({{ get value() {{ return getter(); }} }});
const loadingStats = ref(false);
const lastBackupTime = ref({json.dumps(LAST)});
const listenerActive = ref({json.dumps(listener)});
const userRole = ref({json.dumps(role)});
const statsData = ref({{ backup_in_progress: {json.dumps(in_progress)}, stats_calculated_at: {json.dumps(stats_at)} }});
const formatStamp = () => 'STAMP';
const formatAgo = () => 'AGO';
{helpers}
const backupHealth = computed(() => backupHealthOf(
    lastBackupTime.value, statsData.value?.backup_in_progress, statsData.value?.stats_calculated_at));
console.log(JSON.stringify(sidebarStatusNow()));
"""
    return json.loads(_run_node_output(script))


def _run_node_output(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable is not installed")
    # SECURITY-REVIEW: The executable path is resolved locally and untrusted input is never passed to a shell.
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout


FINISHED = "2026-01-15T03:40:00Z"
UNFINISHED = "2026-01-14T03:40:00Z"


def test_live_when_the_listener_is_up():
    line = _status_line(listener=True, in_progress=False, stats_at=FINISHED)

    assert line == {
        "kind": "live",
        "text": "Live",
        "title": "New messages arrive as they are sent. Last full pass: STAMP.",
    }


def test_live_outranks_a_running_full_pass():
    """The daily pass can run for hours; messages still arrive live meanwhile."""
    line = _status_line(listener=True, in_progress=True, stats_at=UNFINISHED)

    assert line["kind"] == "live"
    assert line["text"] == "Live"
    assert "A full pass is running." in line["title"]
    assert "The one before ran on STAMP." in line["title"]


def test_an_unfinished_backup_still_wins_for_the_owner():
    line = _status_line(listener=True, in_progress=False, stats_at=UNFINISHED)

    assert line["kind"] == "unfinished"
    assert line["text"] == "Last backup did not finish"


def test_a_viewer_sees_live_over_an_unfinished_backup():
    line = _status_line(listener=True, in_progress=False, stats_at=UNFINISHED, role="viewer")

    assert line["kind"] == "live"


def test_without_the_listener_a_running_pass_reads_backing_up():
    line = _status_line(listener=False, in_progress=True, stats_at=UNFINISHED)

    assert line["kind"] == "running"
    assert line["text"] == "Backing up…"


def test_without_the_listener_an_idle_archive_reads_when_it_backed_up():
    line = _status_line(listener=False, in_progress=False, stats_at=FINISHED)

    assert line == {"kind": "idle", "text": "Backed up AGO", "title": "Last backup: STAMP"}
