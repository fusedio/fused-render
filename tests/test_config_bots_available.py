"""/api/config says whether the Bots feature exists on this machine, so the
shell can hide it where the server never runs it (Windows: server/app.py)."""
from fused_render import _platform
from fused_render.server.routers.config import api_config


def _config():
    return api_config(start_dir="/tmp", token=None)


def test_bots_are_available_off_windows(monkeypatch):
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    assert _config()["bots_available"] is True


def test_bots_are_unavailable_on_windows(monkeypatch):
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    assert _config()["bots_available"] is False


def test_onboarding_reports_chrome_not_applicable_on_windows(monkeypatch):
    # The Chrome step only matters for bots; with none on Windows it leaves the
    # meter (`n/a`, which the wizard does not count) instead of nagging.
    from fused_render.shell import onboarding

    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    stages = {}
    onboarding._observe(stages)
    assert stages["chrome"]["status"] == "n/a"


def test_onboarding_still_walks_chrome_candidates_elsewhere(monkeypatch):
    from fused_render.bots import browser
    from fused_render.shell import onboarding

    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    monkeypatch.setattr(browser, "CHROME_CANDIDATES", [])
    stages = {}
    onboarding._observe(stages)
    assert stages["chrome"]["status"] == "pending"
