"""The Bots page's "Install Chrome in WSL" button (fused_render/bots/chrome_install.py). Nothing is downloaded
and no apt runs: the download and the root commands are stubbed."""
from _bots_conftest import *  # noqa: F401,F403
import json
import threading
import urllib.request

import pytest

from fused_render.bots import browser, chrome_install as ci

WHICH = {"apt-get": "/usr/bin/apt-get", "wsl.exe": "/mnt/c/WINDOWS/system32/wsl.exe"}
ENV = {"WSL_DISTRO_NAME": "Ubuntu-24.04"}


def _plan(**kw):
    args = {"env": ENV, "machine": "x86_64", "which": WHICH.get, "wsl": True, "euid": 1000, **kw}
    return ci.plan(**args)


def test_plan_is_available_inside_wsl_with_apt_on_x86_64():
    p = _plan()
    assert p == {"available": True, "reason": "", "command": ci.MANUAL}
    assert ci.DEB_URL in p["command"]


@pytest.mark.parametrize("kw, word", [
    ({"wsl": False}, "WSL"),
    ({"machine": "aarch64"}, "aarch64"),
    ({"which": {"wsl.exe": "/x/wsl.exe"}.get}, "apt-get"),
    ({"env": {}}, "WSL_DISTRO_NAME"),
])
def test_plan_says_why_not(kw, word):
    p = _plan(**kw)
    assert p["available"] is False and word in p["reason"]


def test_plan_needs_wsl_exe_unless_already_root(monkeypatch):
    monkeypatch.setattr(ci._wsl, "windows_exe", lambda name, which=None: None)
    assert "wsl.exe" in _plan()["reason"]
    assert _plan(euid=0, env={})["available"] is True


def test_root_goes_through_wsl_exe_without_a_shell():
    argv = ci._root_argv(["apt-get", "update"], env=ENV, euid=1000, which=WHICH.get)
    assert argv == ["/mnt/c/WINDOWS/system32/wsl.exe", "-d", "Ubuntu-24.04", "-u", "root", "-e", "apt-get", "update"]
    assert ci._root_argv(["apt-get", "update"], env=ENV, euid=0) == ["apt-get", "update"]


@pytest.fixture
def stubbed(monkeypatch):
    """A machine where the button works, with the download and the root commands recorded instead of run."""
    ci.reset()
    ran = {"steps": [], "codes": {}, "found": "/usr/bin/google-chrome"}
    monkeypatch.setattr(ci, "plan", lambda: {"available": True, "reason": "", "command": ci.MANUAL})
    monkeypatch.setattr(ci, "_download", lambda url, dest: open(dest, "wb").close())
    monkeypatch.setattr(ci, "_root_argv", lambda argv: list(argv))

    def step(argv, tail):
        ran["steps"].append(argv)
        tail.append(f"ran {argv[0]}")
        return ran["codes"].get("install" if "install" in argv else "update", 0)

    monkeypatch.setattr(ci, "_run_step", step)
    monkeypatch.setattr(browser, "found_chrome", lambda: ran["found"])
    monkeypatch.setattr("fused_render.jobs.upsert", lambda *a, **k: None)
    yield ran
    ci.reset()


def _finish():
    for t in threading.enumerate():
        if t.name == "bots-chrome-install":
            t.join(timeout=10)
    return ci.status()


def test_install_updates_then_installs_the_downloaded_package(stubbed):
    assert ci.start()["state"] == "running"
    st = _finish()
    assert st["state"] == "done" and st["error"] is None and "/usr/bin/google-chrome" in st["detail"]
    update, install = stubbed["steps"]
    assert update == ["apt-get", "update"]
    assert install[:5] == ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y"]
    assert install[5].endswith(ci.DEB_NAME) and " " not in install[5]


def test_a_failed_update_does_not_stop_the_install(stubbed):
    stubbed["codes"]["update"] = 100
    ci.start()
    assert _finish()["state"] == "done" and len(stubbed["steps"]) == 2


def test_a_failed_install_is_an_error_with_apt_s_words(stubbed):
    stubbed["codes"]["install"] = 100
    ci.start()
    st = _finish()
    assert st["state"] == "error" and "code 100" in st["error"] and "ran env" in st["output"]


def test_success_with_no_browser_to_show_for_it_is_an_error(stubbed):
    stubbed["found"] = None
    ci.start()
    assert _finish()["state"] == "error"


def test_a_failed_download_is_an_error_and_runs_nothing_as_root(stubbed, monkeypatch):
    def offline(url, dest):
        raise OSError("Temporary failure in name resolution")

    monkeypatch.setattr(ci, "_download", offline)
    ci.start()
    st = _finish()
    assert st["state"] == "error" and "name resolution" in st["error"] and stubbed["steps"] == []


def test_one_install_at_a_time(stubbed, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(ci, "_download", lambda url, dest: gate.wait(10) and None)
    ci.start()
    with pytest.raises(ci.InstallError, match="already running"):
        ci.start()
    gate.set()
    _finish()


def test_start_refuses_where_the_button_cannot_work(monkeypatch):
    ci.reset()
    monkeypatch.setattr(ci, "plan", lambda: {"available": False, "reason": "only inside WSL", "command": ci.MANUAL})
    with pytest.raises(ci.InstallError, match="only inside WSL"):
        ci.start()
    assert ci.status()["state"] == "idle"


# ---- through the server: GET /api/bots/setup carries the plan; POST /api/bots/setup/chrome starts it ----
def _j(resp):
    status, _headers, body = resp
    return status, json.loads(body or b"{}")


def test_setup_carries_the_plan_only_while_chrome_is_missing(client, monkeypatch):
    plan = {"available": True, "reason": "", "command": ci.MANUAL}
    monkeypatch.setattr(ci, "plan", lambda: plan)
    monkeypatch.setattr(browser, "chrome_candidates", lambda: [])
    st, out = _j(client.get("/api/bots/setup"))
    assert st == 200 and out["chrome"]["found"] is False and out["chrome"]["install"] == plan
    monkeypatch.setattr(browser, "found_chrome", lambda: "/usr/bin/google-chrome")
    st, out = _j(client.get("/api/bots/setup"))
    assert out["chrome"] == {"found": True, "path": "/usr/bin/google-chrome"}


def test_the_install_route_starts_it_and_reports_it(client, stubbed):
    st, out = _j(client.get("/api/bots/setup/chrome"))
    assert st == 200 and out["state"] == "idle"
    st, out = _j(client.post("/api/bots/setup/chrome", {}))
    assert st == 200 and out["state"] == "running"
    _finish()
    st, out = _j(client.get("/api/bots/setup/chrome"))
    assert out["state"] == "done"


def test_the_install_route_needs_the_fused_header_and_says_why_it_cannot_run(client, monkeypatch):
    ci.reset()
    req = urllib.request.Request(client.base + "/api/bots/setup/chrome", data=b"{}", method="POST",  # noqa: S310
                                 headers={"Content-Type": "application/json"})
    assert client._do(req)[0] in (400, 403)
    monkeypatch.setattr(ci, "plan", lambda: {"available": False, "reason": "only inside WSL", "command": ci.MANUAL})
    st, out = _j(client.post("/api/bots/setup/chrome", {}))
    assert st == 409 and "only inside WSL" in out["error"]
    assert ci.status()["state"] == "idle"
