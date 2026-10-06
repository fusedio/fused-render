"""The Linux native-window host: one GTK3 + WebKitGTK process that shows the
app's windows, the counterpart of macOS's `mac_window.WindowManager`.

Run as ``python -I -m fused_render.supervisor._linux.window_host --port N
--socket PATH --state DIR`` by the supervisor (`windows.py`), after the server
is ready. It listens on a unix socket (`window_host_ipc`) for ``open`` /
``set_enabled`` / ``quit``; the supervisor's own open path and the server's
``POST /api/windows/open`` / prefs hook all go through it. Its enabled state at
startup comes from `native_windows_enabled` in prefs.json, read once the
socket is listening (see `main`'s `on_listening`) rather than passed on the
command line, so a preference PUT that races the host's startup is never
lost.

Why a separate process: GTK wants the main thread of a process that owns a
display connection, the supervisor's main thread is its event loop, and a
WebKit web-process abort (see "Known gap" in docs/LINUX_DESKTOP_SPEC.md) or a
GTK crash must never take the supervisor and the server down with it. If this
process cannot start (no PyGObject, no typelib, no display) it exits with
`EXIT_UNAVAILABLE` and a one-line reason on stderr; the supervisor logs it once
and falls back to ``xdg-open`` browser tabs for the rest of the session.

WebKitGTK and GTK come from the system, never the bundle. Layout:

* the top half of this file is pure logic (`Host`, `map_*`, `FrameStore`) with
  no GTK import, unit-tested on every OS against a fake `Backend`;
* `load_toolkit` and `GtkBackend` are the only code that imports ``gi``, always
  lazily and only reached from `main()`.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from fused_render import window_host_ipc, window_policy

EXIT_UNAVAILABLE = 3  # supervisor reads this as "fall back to the browser"

_DEFAULT_SIZE = (1200, 800)
_MIN_DIM, _MAX_DIM = 100, 20000


class ToolkitUnavailable(RuntimeError):
    """GTK/WebKitGTK cannot be used here (missing package, typelib or display)."""


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------

# Navigation types that are the *main frame* going somewhere, for our own
# origin and for routing a scheme WebKit cannot load itself (OTHER, see
# below) to launch services. WebKit2 4.1's NavigationAction has no
# is_main_frame, so a sub-frame load (a map's tile iframe, an embed) of one
# of these types is indistinguishable from the main frame going there — a
# false positive here is harmless: our own origin allows either way, and
# OTHER only matters for schemes WebKit cannot route itself. A same-frame
# `location.assign` (the update dialog's Restart) also arrives as OTHER, so
# OTHER counts as main-frame too when the URL needs launch services
# (`fused-render://relaunch`, `mailto:`) — the one case where leaving it to
# the page hangs the window instead of just doing nothing.
_MAIN_FRAME_NAV = {"LINK_CLICKED", "FORM_SUBMITTED", "FORM_RESUBMITTED",
                   "BACK_FORWARD", "RELOAD"}

# For an EXTERNAL target the same false positive is not harmless: calling a
# sub-frame's BACK_FORWARD/RELOAD "main frame" would yank the whole window to
# the browser over an iframe restoring its own history, or a map tile
# reloading. With no main-frame signal to fall back on, only a user-gesture
# link click or form submission is confident enough to hand off to the
# browser; anything else (BACK_FORWARD, RELOAD, a scripted navigation) is
# left to the page, so a foreign iframe keeps loading in place. The residual
# gap: a genuine user click on a link to an external site *inside* an
# external iframe still gets handed to the browser, same as a top-level
# click — WebKit2 4.1 gives us no way to tell those apart.
_EXTERNAL_HANDOFF_NAV = {"LINK_CLICKED", "FORM_SUBMITTED"}


def map_navigation(url: str | None, port: int, nav_type: str, *,
                   button: int = 0, ctrl: bool = False,
                   user_gesture: bool = True) -> str:
    """allow | new_window | open_external for a navigation inside a window.
    ``nav_type`` is WebKitNavigationType's nick upper-cased (``LINK_CLICKED``).
    ``user_gesture`` is WebKitNavigationAction's ``is_user_gesture()``."""
    if window_policy.classify(url, port) == "external":
        is_main_frame = user_gesture and nav_type in _EXTERNAL_HANDOFF_NAV
    else:
        is_main_frame = (nav_type in _MAIN_FRAME_NAV
                         or window_policy.needs_launch_services(url))
    return window_policy.navigation_action(
        url, port,
        is_main_frame=is_main_frame,
        has_target_frame=True,
        wants_download=False,
        new_window_modifier=ctrl or button == 2,
    )


def map_new_window(url: str | None, port: int) -> str:
    """new_window | open_external | ignore for `window.open` / ``target=_blank``.
    Blank / ``about:`` targets are ignored: they must neither open nor focus a
    window. Otherwise classified like a main-frame navigation with no target
    frame (as on macOS); ``"allow"`` has no window to load into, so it is
    ignored."""
    if _is_blank(url):
        return "ignore"
    verdict = window_policy.navigation_action(
        url, port, is_main_frame=True, has_target_frame=False,
        wants_download=False, new_window_modifier=False)
    return "ignore" if verdict == "allow" else verdict


def map_response(*, can_show: bool, content_disposition: str | None) -> str:
    return window_policy.response_action(
        is_main_frame=True, can_show_mime=can_show, content_disposition=content_disposition)


class FrameStore:
    """Remembers each window's size (and position, where the display server
    honours one) per `window_policy.frame_autosave_name`. Best effort: a corrupt
    or unwritable file means default placement, never an error."""

    def __init__(self, path) -> None:
        self._path = Path(path)
        self._data: dict = {}
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._data = loaded
        except (OSError, ValueError):
            pass

    def get(self, name: str) -> dict | None:
        entry = self._data.get(name)
        return entry if isinstance(entry, dict) and self._sane(entry.get("w"), entry.get("h")) else None

    @staticmethod
    def _sane(w, h) -> bool:
        return (isinstance(w, int) and isinstance(h, int)
                and _MIN_DIM <= w <= _MAX_DIM and _MIN_DIM <= h <= _MAX_DIM)

    def put(self, name: str, w: int, h: int, x, y) -> None:
        if not self._sane(w, h):
            return
        self._data[name] = {"w": w, "h": h,
                            "x": x if isinstance(x, int) else None,
                            "y": y if isinstance(y, int) else None}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(self._data), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass


class Host:
    """Command dispatch plus the window book. Everything that touches a window
    runs through ``backend.run_on_main`` so GTK only ever sees its own thread;
    the book itself is only touched from there."""

    def __init__(self, port: int, backend, *, enabled: bool = True) -> None:
        self.port = port
        self.backend = backend
        self.enabled = enabled
        self._windows: list = []  # handles, in creation order

    # -- IPC entry point (runs on the ipc thread) --
    def dispatch(self, command: dict) -> dict:
        cmd = command.get("cmd")
        if cmd == "ping":
            return {"ok": True}
        if cmd == "open":
            url = command.get("url")
            if not isinstance(url, str):
                return {"ok": False, "reason": "'url' must be a string"}
            token = command.get("activation_token")
            if token is not None and not isinstance(token, str):
                return {"ok": False, "reason": "'activation_token' must be a string"}
            return self.backend.run_on_main(lambda: self.open_url(url, token))
        if cmd == "set_enabled":
            on = command.get("on")
            if not isinstance(on, bool):
                return {"ok": False, "reason": "'on' must be a boolean"}
            self.backend.run_on_main(lambda: self.set_enabled(on))
            return {"ok": True}
        if cmd == "quit":
            self.backend.run_on_main(self.quit)
            return {"ok": True}
        return {"ok": False, "reason": f"unknown command {cmd!r}"}

    # -- main thread --
    def open_url(self, url: str, activation_token: str | None = None) -> dict:
        kind = window_policy.classify(url, self.port)
        if kind == "other":
            return {"ok": False, "reason": "unsupported url"}
        if not self.enabled:
            return {"ok": False, "reason": "disabled"}
        if kind == "external":
            self.backend.open_external(url)
            return {"ok": True}
        self.focus_or_open(url, activation_token)
        return {"ok": True}

    def focus_or_open(self, url: str, activation_token: str | None = None):
        existing = self._find(url)
        if existing is not None:
            self.backend.present(existing, activation_token)
            return existing
        key, view = window_policy.window_key_of(url), window_policy.window_view_of(url)
        handle = self.backend.new_window(url, window_policy.frame_autosave_name(key, view))
        self._windows.append(handle)
        self.backend.present(handle, activation_token)
        return handle

    def _find(self, url: str):
        key, view = window_policy.window_key_of(url), window_policy.window_view_of(url)
        for handle in self._windows:
            live = self.backend.current_url(handle)
            if key is None:
                # Only Home reuses a keyless window; other keyless pages
                # (/tasks, /preferences) always open fresh, as on macOS.
                if (_is_home(url) and window_policy.window_key_of(live) is None
                        and _is_home(live)):
                    return handle
            elif (window_policy.window_key_of(live) == key
                  and window_policy.window_view_of(live) == view):
                return handle
        return None

    def window_closed(self, handle) -> None:
        if handle in self._windows:
            self._windows.remove(handle)

    def quit(self) -> None:
        """Every open window's frame, saved before the process actually
        exits. Both ways the supervisor stops the host — the `quit` command
        (dispatch, above) and `main`'s SIGTERM handler — land here, since a
        bare `Gtk.main_quit()` (or killpg's SIGTERM with no handler) drops
        whatever a user-close or set_enabled(False) hasn't already saved."""
        for handle in list(self._windows):
            self.backend.save_frame(handle)
        self.backend.quit()

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        if not on:
            for handle in list(self._windows):
                self.backend.close(handle)
            self._windows.clear()

    def popup(self, url: str, kind: str) -> None:
        """A `new_window` / `open_external` decision from a window's own policy
        (`map_navigation` / `map_new_window`)."""
        if _is_blank(url):
            return
        if kind == "open_external":
            self.backend.open_external(url)
        elif kind == "new_window":
            self.focus_or_open(url)


def _is_blank(url: str | None) -> bool:
    return not url or url.strip().lower().startswith("about:")


def _is_home(url: str | None) -> bool:
    if not url:
        return False
    try:
        # The shell rewrites "/" to "/home" via replaceState right after first paint.
        return urlsplit(url).path in ("", "/", "/home")
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# GTK / WebKitGTK (Linux desktop only; never imported at module level)
# ---------------------------------------------------------------------------

def pick_unix_signal_add(glib, glib_unix_loader):
    """The `(priority, signum, callback)` function that arms a unix signal on
    the GLib main loop, picked without needing a real `gi`: `glib_unix_loader`
    takes no arguments and returns the `GLibUnix` module, raising
    `ValueError`/`ImportError` the same way `gi.require_version` plus
    `from gi.repository import GLibUnix` would when that namespace is not
    installed. `GLibUnix.signal_add` is preferred when it loads, since on a
    recent GLib (unix_signal_add moved out of `GLib` itself) it is the only
    one that exists; `glib.unix_signal_add` is the fallback for a GLib old
    enough to still carry it directly. Returns `None` when neither exists, so
    the caller can skip installing a SIGTERM handler instead of crashing."""
    try:
        glib_unix = glib_unix_loader()
    except (ValueError, ImportError):
        return getattr(glib, "unix_signal_add", None)
    return glib_unix.signal_add


def load_toolkit() -> SimpleNamespace:
    """Import GTK3 + WebKit2 4.1 through PyGObject or say exactly why not."""
    try:
        import gi
    except ImportError as error:
        raise ToolkitUnavailable(
            "PyGObject (gi) is not installed (pip install 'fused-render[linux-desktop]')"
        ) from error
    try:
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("WebKit2", "4.1")
        from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: PLC0415
    except (ValueError, ImportError) as error:
        raise ToolkitUnavailable(
            f"GTK 3 / WebKit2 4.1 typelib not found ({error}); install the system package "
            "gir1.2-webkit2-4.1 (Debian/Ubuntu) or webkit2gtk-4.1 (Arch, Fedora)"
        ) from error
    result = Gtk.init_check(None)  # (bool, argv) in PyGObject's GTK3 overrides
    ok = result[0] if isinstance(result, tuple) else bool(result)
    if not ok or Gdk.Display.get_default() is None:
        raise ToolkitUnavailable("no graphical display available (DISPLAY/WAYLAND_DISPLAY unset?)")

    def _load_glib_unix():
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix  # noqa: PLC0415
        return GLibUnix

    unix_signal_add = pick_unix_signal_add(GLib, _load_glib_unix)
    return SimpleNamespace(Gtk=Gtk, Gdk=Gdk, GLib=GLib, WebKit2=WebKit2,
                           unix_signal_add=unix_signal_add)


class _Win:
    """One window: the opaque handle `Host` keeps."""

    def __init__(self, window, view, frame_name: str) -> None:
        self.window, self.view, self.frame_name = window, view, frame_name


# Strictly below window_host_ipc.CALLER_TIMEOUT_S, so when the host gives up
# the caller is still waiting and hears the refusal before it falls back.
_MAIN_DEADLINE_S = 3.0


class GtkBackend:
    def __init__(self, tk, host_getter, state_dir: Path, log) -> None:
        from fused_render import __version__

        self.tk, self._host, self._log = tk, host_getter, log
        self._frames = FrameStore(state_dir / "window-frames.json")
        data_dir = str(state_dir / "window-host")
        wk = tk.WebKit2
        manager = wk.WebsiteDataManager(base_data_directory=data_dir,
                                        base_cache_directory=data_dir + "/cache")
        self._context = wk.WebContext.new_with_website_data_manager(manager)
        self._context.connect("download-started", self._on_download_started)
        self._version = __version__

    # -- threading --
    def run_on_main(self, fn):
        if threading.current_thread() is threading.main_thread():
            return fn()
        done, box = threading.Event(), {}
        lock = threading.Lock()
        state = {"cancelled": False}

        def run():
            with lock:
                if state["cancelled"]:
                    return False  # the caller already gave up and fell back
            try:
                box["value"] = fn()
            except BaseException as error:  # noqa: BLE001 - re-raised on the caller
                box["error"] = error
            done.set()
            return False  # GLib.SOURCE_REMOVE

        self.tk.GLib.idle_add(run)
        # Bounded start-to-finish, not just start: a fn already running on the
        # main thread when the deadline passes must not make the caller (and
        # so its CALLER_TIMEOUT_S) wait for it. `run` still finishes on the
        # main thread when it gets there — cancelled only stops it starting —
        # and box/done are just left unread; no crash, no second reply.
        if not done.wait(_MAIN_DEADLINE_S):
            with lock:
                state["cancelled"] = True
            raise TimeoutError("GTK main loop did not respond")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def quit(self) -> None:
        self.tk.Gtk.main_quit()

    # -- windows --
    def new_window(self, url: str, frame_name: str) -> _Win:
        Gtk, wk = self.tk.Gtk, self.tk.WebKit2
        view = wk.WebView.new_with_context(self._context)
        settings = view.get_settings()
        settings.set_user_agent_with_application_details("FusedRender", self._version)
        settings.set_enable_developer_extras(True)
        # getUserMedia/getDisplayMedia need both on; hasattr guards an older
        # system WebKitGTK that predates one of them. `_on_permission` is the
        # gate that actually allows the resulting prompt, app-origin only.
        if hasattr(settings, "set_enable_media_stream"):
            settings.set_enable_media_stream(True)
        if hasattr(settings, "set_enable_webrtc"):
            settings.set_enable_webrtc(True)
        window = Gtk.Window()
        window.set_title("FusedRender")
        size = self._frames.get(frame_name)
        window.set_default_size(*( (size["w"], size["h"]) if size else _DEFAULT_SIZE))
        if size and size.get("x") is not None and size.get("y") is not None:
            window.move(size["x"], size["y"])  # honoured on X11; Wayland ignores it
        window.add(view)
        win = _Win(window, view, frame_name)
        view.connect("decide-policy", self._on_decide_policy)
        view.connect("create", self._on_create)
        view.connect("close", lambda _v: window.destroy())
        view.connect("notify::title", lambda v, _p: window.set_title(v.get_title() or "FusedRender"))
        view.connect("permission-request", self._on_permission)
        view.connect("web-process-terminated", self._on_web_process_terminated)
        window.connect("delete-event", lambda _w, _e: self.save_frame(win) and False)
        window.connect("destroy", lambda _w: self._host().window_closed(win))
        window.connect("key-press-event", lambda _w, e: self._on_key(win, e))
        view.load_uri(url)
        return win

    def present(self, win: _Win, activation_token: str | None = None) -> None:
        win.window.show_all()
        if activation_token:
            # GTK3's Wayland backend treats a startup id that looks like an
            # xdg-activation token as one; present() then raises using it.
            win.window.set_startup_id(activation_token)
            win.window.present()
        else:
            win.window.present_with_time(self.tk.Gdk.CURRENT_TIME)

    def close(self, win: _Win) -> None:
        self.save_frame(win)
        win.window.destroy()

    def current_url(self, win: _Win):
        return win.view.get_uri()

    def open_external(self, url: str) -> None:
        # Called on the GTK thread; `ui.open_url` can block up to 5s and raise
        # OSError, so run it on a worker and log failures.
        from fused_render.supervisor._linux import ui

        def worker() -> None:
            try:
                ui.open_url(url)
            except OSError as error:
                self._log(f"could not open external link ({error}): {url}")

        threading.Thread(target=worker, daemon=True,
                         name="fused-render-window-host-open-external").start()

    def save_frame(self, win: _Win) -> bool:
        """Called from a window's own close (`delete-event`) and from
        `Host.quit` for every window still open when the process quits."""
        try:
            w, h = win.window.get_size()
            x, y = win.window.get_position()
            self._frames.put(win.frame_name, w, h, x, y)
        except Exception:  # noqa: BLE001 - a lost frame is cosmetic
            pass
        return True

    # -- signals --
    def _on_key(self, win: _Win, event) -> bool:
        Gdk = self.tk.Gdk
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(event.state & Gdk.ModifierType.MOD1_MASK)
        key = event.keyval
        if key == Gdk.KEY_F5 or (ctrl and key == Gdk.KEY_r):
            win.view.reload()
        elif alt and key == Gdk.KEY_Left:
            win.view.go_back()
        elif alt and key == Gdk.KEY_Right:
            win.view.go_forward()
        elif ctrl and key == Gdk.KEY_w:
            win.window.close()
        else:
            return False
        return True

    def _on_decide_policy(self, view, decision, decision_type) -> bool:
        wk = self.tk.WebKit2
        host = self._host()
        T = wk.PolicyDecisionType
        if decision_type in (T.NAVIGATION_ACTION, T.NEW_WINDOW_ACTION):
            action = decision.get_navigation_action()
            url = action.get_request().get_uri()
            if decision_type == T.NEW_WINDOW_ACTION:
                verdict = map_new_window(url, host.port)
            else:
                nick = action.get_navigation_type().value_nick.upper().replace("-", "_")
                ctrl = bool(action.get_modifiers() & self.tk.Gdk.ModifierType.CONTROL_MASK)
                verdict = map_navigation(url, host.port, nick,
                                         button=action.get_mouse_button(), ctrl=ctrl,
                                         user_gesture=action.is_user_gesture())
            if verdict == "allow":
                decision.use()
            else:
                decision.ignore()
                host.popup(url, verdict)
            return True
        if decision_type == T.RESPONSE:
            if not decision.is_main_frame_main_resource():
                decision.use()
                return True
            headers = decision.get_response().get_http_headers()
            disposition = headers.get_one("Content-Disposition") if headers else None
            if map_response(can_show=decision.is_mime_type_supported(),
                            content_disposition=disposition) == "download":
                decision.download()
            else:
                decision.use()
            return True
        return False

    def _on_create(self, view, navigation_action):
        # window.open() / target=_blank that did not pass through decide-policy:
        # route it by the same rule, never hand WebKit a second web view.
        url = navigation_action.get_request().get_uri()
        host = self._host()
        verdict = map_new_window(url, host.port)
        if verdict != "ignore":
            host.popup(url, verdict)
        return None

    def _on_permission(self, view, request) -> bool:
        # One gate for every WebKitPermissionRequest kind WebKit asks about —
        # camera/mic (UserMediaPermissionRequest, also display capture),
        # geolocation, notifications: app-origin pages get the answer a
        # browser gives after the user's already clicked Allow once; nothing
        # else (a third-party iframe) does.
        parts = urlsplit(view.get_uri() or "")
        try:
            port = parts.port
        except ValueError:
            port = None
        if window_policy.is_own_origin(parts.hostname, port, self._host().port):
            request.allow()
        else:
            request.deny()
        return True

    def _on_web_process_terminated(self, view, reason) -> None:
        self._log(f"web process terminated ({reason.value_nick}) at {view.get_uri()}")
        url = view.get_uri() or ""
        media = ("<p>If this page plays audio or video, this system's WebKitGTK may "
                 "lack GStreamer plugins (see the FusedRender Linux notes).</p>"
                 if reason.value_nick == "crashed" else "")
        safe = url.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
        view.load_html(
            f'<body style="font:15px sans-serif;margin:3em"><h3>This page stopped unexpectedly</h3>'
            f'{media}<p><a href="{safe}">Reload</a></p></body>', url or None)

    # -- downloads --
    def _on_download_started(self, _context, download) -> None:
        download.connect("decide-destination", self._on_decide_destination)

    def _on_decide_destination(self, download, suggested) -> bool:
        GLib = self.tk.GLib
        folder = (GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
                  or os.path.expanduser("~/Downloads"))
        os.makedirs(folder, exist_ok=True)
        download.set_destination(
            GLib.filename_to_uri(window_policy.download_destination(folder, suggested), None))
        return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="window_host")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--state", required=True)
    args = parser.parse_args(argv)

    def log(message: str) -> None:
        print(f"window-host: {message}", file=sys.stderr, flush=True)

    try:
        tk = load_toolkit()
    except ToolkitUnavailable as error:
        log(str(error))
        return EXIT_UNAVAILABLE

    # App identity, before the first window: "fused-render" matches the
    # .desktop basename (integration.py's _DESKTOP_NAME / StartupWMClass) and
    # the icon name it installs under the hicolor theme (its _ICON_STEM), so
    # the compositor/taskbar group and icon this process under the same
    # identity the installed .desktop entry advertises.
    tk.GLib.set_prgname("fused-render")
    tk.GLib.set_application_name("FusedRender")
    tk.Gtk.Window.set_default_icon_name("fused-render")

    holder: dict = {}
    backend = GtkBackend(tk, lambda: holder["host"], Path(args.state), log)
    state_dir = Path(args.state)

    def on_listening() -> None:
        # Reads `native_windows_enabled` from prefs.json only once the socket
        # is listening: a PUT that landed earlier is already on disk (prefs.py
        # writes before it calls `apply`), and the backlog holds any `open` /
        # `set_enabled` that arrives while this read is in flight, so nothing
        # is dispatched against a Host that does not exist yet.
        from fused_render.supervisor._linux.windows import preference_enabled

        holder["host"] = Host(args.port, backend, enabled=preference_enabled(state_dir))

    stop = threading.Event()
    window_host_ipc.serve(args.socket, lambda command: holder["host"].dispatch(command),
                          stop, log, on_listening=on_listening)
    # SIGTERM is how the supervisor's Job.close() stops this process after
    # (or instead of, if the `quit` IPC command never got through) asking
    # nicely; without a handler it would drop whatever `quit` didn't already
    # save. `tk.unix_signal_add` (resolved in `load_toolkit`, from whichever
    # of `GLibUnix.signal_add` / `GLib.unix_signal_add` this system's GLib
    # actually has) runs the callback on the GLib main loop itself (a plain
    # `signal.signal` handler would starve behind the C poll()), so
    # `host.quit()` needs no run_on_main marshalling here. Neither existing
    # is only a nicety lost: the supervisor still stops this process over the
    # `quit` IPC command, or failing that a bare SIGTERM with no handler.
    if tk.unix_signal_add is not None:
        tk.unix_signal_add(tk.GLib.PRIORITY_DEFAULT, signal.SIGTERM,
                           lambda: _on_sigterm(holder["host"]))
    else:
        log("no unix_signal_add available on this GLib; SIGTERM will not save open windows first")
    try:
        tk.Gtk.main()
    finally:
        stop.set()
    return 0


def _on_sigterm(host: Host) -> bool:
    host.quit()
    return False  # GLib.SOURCE_REMOVE: a quitting process needs it once


if __name__ == "__main__":
    sys.exit(main())
