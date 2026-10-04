"""Platform gating of the native-window dependencies (macOS WKWebView, Linux
WebKitGTK).

Every requirement that only one OS can use must carry a marker for that OS, and
none of them may sit in core `dependencies`: `pip install fused-render` with no
extras has to resolve on every platform without compiling PyGObject or pulling
pyobjc. The Linux half is also the one place the PyGObject version range is
pinned, so the girepository ABI choice is asserted here rather than only in a
comment.
"""
import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _project() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


def _reqs(items) -> list[Requirement]:
    return [Requirement(i) for i in items]


def _named(reqs, *names) -> list[Requirement]:
    wanted = {n.lower() for n in names}
    return [r for r in reqs if r.name.lower() in wanted]


def _marker_pins(req: Requirement, platform: str) -> bool:
    """True when the marker is true for ``platform`` and false for the other
    two, i.e. it genuinely gates to that one OS."""
    assert req.marker is not None, f"{req} has no marker"
    envs = {"darwin": "darwin", "linux": "linux", "win32": "win32"}
    for name, value in envs.items():
        got = req.marker.evaluate({"sys_platform": value, "platform_machine": "x86_64",
                                   "python_version": "3.12"})
        if got != (name == platform):
            return False
    return True


def test_app_extra_is_darwin_only():
    app = _reqs(_project()["optional-dependencies"]["app"])
    gated = _named(app, "rumps", "pyobjc-framework-WebKit")
    assert {r.name.lower() for r in gated} == {"rumps", "pyobjc-framework-webkit"}
    for req in gated:
        assert _marker_pins(req, "darwin"), f"{req} must be darwin-only"


def test_linux_desktop_extra_gates_pygobject_to_linux():
    extra = _reqs(_project()["optional-dependencies"]["linux-desktop"])
    gi = _named(extra, "PyGObject")
    assert len(gi) == 1, "linux-desktop must declare PyGObject exactly once"
    assert _marker_pins(gi[0], "linux")


def test_pygobject_range_stays_on_girepository_1():
    # PyGObject >= 3.51 links libgirepository-2.0 (glib >= 2.80), which stock
    # Ubuntu 22.04 lacks and no 24.04 desktop package guarantees; < 3.51 links
    # libgirepository-1.0, which every GNOME desktop with python3-gi has.
    gi = _named(_reqs(_project()["optional-dependencies"]["linux-desktop"]), "PyGObject")[0]
    spec = gi.specifier
    assert not spec.contains("3.51.0") and not spec.contains("3.54.0")
    assert spec.contains("3.50.2")
    assert spec.contains("3.48.2")


def test_nothing_gtk_or_webkit_is_core():
    core = _reqs(_project()["dependencies"])
    for req in core:
        n = req.name.lower()
        assert n not in {"pygobject", "pycairo", "rumps"}, f"{req} must not be core"
        if n == "pyobjc-framework-webkit":
            raise AssertionError("pyobjc-framework-WebKit must stay in [app]")


def test_every_darwin_pyobjc_in_core_still_has_its_marker():
    core = _reqs(_project()["dependencies"])
    pyobjc = [r for r in core if r.name.lower().startswith("pyobjc")]
    assert pyobjc, "the core pyobjc entries are deliberate (see pyproject comments)"
    for req in pyobjc:
        assert _marker_pins(req, "darwin")


def test_gi_is_never_imported_at_module_level():
    # A guard placed after an eager import is dead: the import itself must sit
    # inside the Linux-only path. Scan every source file for a top-level
    # (column-0) import of gi / pyobjc-WebKit style names.
    root = PYPROJECT.parent / "fused_render"
    offenders = []
    pat = re.compile(r"^(import gi\b|from gi\b|from gi\.repository)", re.M)
    for path in root.rglob("*.py"):
        if pat.search(path.read_text(encoding="utf-8", errors="replace")):
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"top-level gi import in: {offenders}"
