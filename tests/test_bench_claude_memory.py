"""scripts/bench_claude_memory.py: classify a `ps` snapshot into per-session trees.

The script is loaded by path (it is not a package module). Only the pure
functions are exercised; `--spawn` launches real claude processes and is never
run from a test.
"""
import importlib.util
import os

import pytest

SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "bench_claude_memory.py",
)


@pytest.fixture()
def bench():
    spec = importlib.util.spec_from_file_location("bench_claude_memory", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOME = "/Users/me/Work/fused-render"
PS = f"""\
  100     1   66000 /usr/bin/python3 {HOME}/fused_render/claude_agent/session_host.py /runs/a
  101   100  380000 /Users/me/.local/bin/claude -p --input-format stream-json --output-format stream-json
  102   101   27000 /usr/bin/python3 {HOME}/fused_render/claude_agent/permission_server.py
  103   101   42000 node /opt/mcp/one.js
  104   103   43000 node /opt/mcp/one-child.js
  200     1   65000 /usr/bin/python3 /tmp/pytest-of-me/pytest-1/x/fused_render/claude_agent/session_host.py /runs/b
  201   200  300000 claude -p --input-format stream-json
  202   201   17000 /usr/bin/python3 /tmp/pytest-of-me/pytest-1/x/fused_render/claude_agent/permission_server.py
  300   299   99000 /usr/bin/python3 -m something_else
  this line is malformed
  400     1  310000 claude -p --system-prompt-file /tmp/fused_render_ai_sp123 --output-format stream-json
  401   400   11000 node /opt/mcp/ai-child.js
  500     1    9000 /sbin/launchd
"""


def test_parse_ps_skips_malformed_and_keeps_command_whole(bench):
    procs = bench.parse_ps(PS)
    assert len(procs) == 12
    p = {x.pid: x for x in procs}
    assert p[101].ppid == 100 and p[101].rss_kb == 380000
    assert p[101].cmd.startswith("/Users/me/.local/bin/claude -p --input-format")


def test_classify_order(bench):
    procs = bench.parse_ps(PS)
    by = {p.pid: bench.classify(p, procs) for p in procs}
    assert by[100] == "host"
    assert by[101] == "claude"
    assert by[102] == "fused-permission-mcp"
    assert by[103] == "other-mcp"
    assert by[104] == "other-mcp"
    assert by[201] == "claude"
    assert by[300] == "other"
    assert by[400] == "other"  # fused.ai warm process is not a session claude
    assert by[500] == "other"


def test_session_trees_rooted_at_hosts_sorted_by_pid(bench):
    trees = bench.session_trees(bench.parse_ps(PS))
    assert [t[0][0].pid for t in trees] == [100, 200]  # ppid=1 host still a root
    first = {p.pid: c for p, c in trees[0]}
    assert first == {
        100: "host", 101: "claude", 102: "fused-permission-mcp",
        103: "other-mcp", 104: "other-mcp",
    }


def test_warm_ai_process_is_not_attached_to_any_host(bench):
    trees = bench.session_trees(bench.parse_ps(PS))
    pids = {p.pid for t in trees for p, _ in t}
    assert 400 not in pids and 401 not in pids


def test_only_under_filters_pytest_leaked_hosts(bench):
    procs = bench.parse_ps(PS)
    trees = bench.session_trees(procs, only_under=HOME)
    assert [t[0][0].pid for t in trees] == [100]


def test_summarize_totals(bench):
    trees = bench.session_trees(bench.parse_ps(PS))
    s = bench.summarize(trees)
    assert s["sessions"] == 2
    first = s["per_session"][0]
    assert first["host"] == pytest.approx(66000 / 1024)
    assert first["claude"] == pytest.approx(380000 / 1024)
    assert first["other-mcp"] == pytest.approx(85000 / 1024)
    assert first["total"] == pytest.approx((66000 + 380000 + 27000 + 85000) / 1024)
    assert s["total_mb"] == pytest.approx(
        (66000 + 380000 + 27000 + 85000 + 65000 + 300000 + 17000) / 1024
    )
    assert s["mean_mb"] == pytest.approx(s["total_mb"] / 2)


def test_format_report_has_footer_and_totals(bench):
    trees = bench.session_trees(bench.parse_ps(PS))
    out = bench.format_report(bench.summarize(trees))
    assert "double-counts shared pages" in out
    assert "total" in out.lower()


def test_empty_snapshot(bench):
    s = bench.summarize(bench.session_trees([]))
    assert s["sessions"] == 0 and s["total_mb"] == 0 and s["mean_mb"] == 0
    assert "0 sessions" in bench.format_report(s)
