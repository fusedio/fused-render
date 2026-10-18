"""sysmon/verdict.py: the nine rules, in order, over a dashboard dict."""
from fused_render.sysmon import verdict as V

GB = 1 << 30


def healthy():
    return {
        "net": {"summary": {"in_flight": [{"client": "c", "in_flight": 2, "saturated_s": 0,
                                           "top": None}], "browser_queue_p95_ms": 20}},
        "server": {"pool": {"borrowed": 3, "total": 40},
                   "loop_lag": {"current_ms": 2.0, "max_60s_ms": 30.0}},
        "memory": {"swap_growth_10m_bytes": 0, "pressure": "normal",
                   "biggest": {"pid": 5, "label": "Engine: x", "mem": 100}},
        "claude": {"total": 2, "mem_bytes": 1 * GB},
        "index": {"in_use": 0, "waiting": 0, "abandoned": 0},
        "cpu": {"thermal_limit": 100, "low_power": False},
        "disk": {"free_bytes": 100 * GB},
        "errors": {"last_unexpected_outage_age_s": None},
    }


def keys(d):
    return [r["key"] for r in V.verdict(d)["reasons"]]


def test_nothing_wrong_is_ok():
    out = V.verdict(healthy())
    assert out["level"] == "ok" and out["reasons"] == [] and out["headline"]


def test_rule1_saturated_connections_names_top_source_and_route():
    d = healthy()
    d["net"]["summary"]["in_flight"] = [{"client": "c", "in_flight": 6, "saturated_s": 3.5,
        "top": {"source": "shell /monitor", "route": "/api/fs/events-poll", "count": 6}}]
    out = V.verdict(d)
    r = out["reasons"][0]
    assert r["key"] == "connections" and out["level"] == "bad"
    assert "shell /monitor" in r["text"] and "/api/fs/events-poll" in r["text"]
    assert r["link"].startswith("/monitor?tab=network&source=")
    assert "route=%2Fapi%2Ffs%2Fevents-poll" in r["link"]


def test_rule1_browser_queue_p95():
    d = healthy()
    d["net"]["summary"]["browser_queue_p95_ms"] = 600
    assert keys(d) == ["connections"]


def test_rule2_pool_busy():
    d = healthy()
    d["server"]["pool"] = {"borrowed": 36, "total": 40}
    assert keys(d) == ["threads"]


def test_rule3_loop_lag():
    d = healthy()
    d["server"]["loop_lag"]["current_ms"] = 300
    assert keys(d) == ["loop"]


def test_rule4_swap_growth_and_pressure_name_biggest_process():
    d = healthy()
    d["memory"]["swap_growth_10m_bytes"] = 600 * (1 << 20)
    out = V.verdict(d)
    assert keys(d) == ["memory"] and "Engine: x" in out["reasons"][0]["text"]
    assert out["reasons"][0]["link"] == "/monitor?tab=processes&pid=5"
    d = healthy()
    d["memory"]["pressure"] = "critical"
    assert V.verdict(d)["level"] == "bad"
    d["memory"]["pressure"] = "warn"
    assert V.verdict(d)["level"] == "warn"


def test_rule5_claude_count_or_memory():
    d = healthy()
    d["claude"]["total"] = 9
    assert keys(d) == ["claude"]
    d = healthy()
    d["claude"]["mem_bytes"] = 5 * GB
    assert "5.0 GB" in V.verdict(d)["reasons"][0]["text"]


def test_rule6_index_waiters_or_abandoned():
    d = healthy()
    d["index"]["waiting"] = 2
    assert keys(d) == ["index"]
    d = healthy()
    d["index"]["abandoned"] = 1
    assert keys(d) == ["index"]


def test_rule7_thermal_and_low_power():
    d = healthy()
    d["cpu"]["thermal_limit"] = 70
    assert keys(d) == ["thermal"]
    d = healthy()
    d["cpu"] = {"thermal_limit": None, "low_power": True}
    assert keys(d) == ["thermal"]


def test_rule8_disk_low():
    d = healthy()
    d["disk"]["free_bytes"] = 2 * GB
    assert keys(d) == ["disk"]


def test_rule9_recent_outage():
    d = healthy()
    d["errors"]["last_unexpected_outage_age_s"] = 120
    assert keys(d) == ["outage"]
    d["errors"]["last_unexpected_outage_age_s"] = 3600
    assert keys(d) == []


def test_rule9_recent_crash_warns():
    d = healthy()
    d["errors"]["recent_crash"] = "server-123.log"
    out = V.verdict(d)
    assert [r["key"] for r in out["reasons"]] == ["outage"]
    assert out["reasons"][0]["text"] == "server-123.log crashed"
    assert out["level"] == "warn"


def test_reasons_keep_rule_order_and_level_is_worst():
    d = healthy()
    d["errors"]["last_unexpected_outage_age_s"] = 10
    d["server"]["pool"] = {"borrowed": 40, "total": 40}
    out = V.verdict(d)
    assert [r["key"] for r in out["reasons"]] == ["threads", "outage"]
    assert out["level"] == "bad"
    assert out["headline"] == out["reasons"][0]["text"]


def test_failed_or_missing_sections_are_skipped():
    d = healthy()
    d["server"] = {"error": "boom"}
    del d["disk"]
    d["net"] = None
    assert V.verdict(d)["level"] == "ok"
