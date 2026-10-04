"""Navigation and download policy for the app's native windows (macOS).

Pure Python, no AppKit: `mac_window.py` asks these functions what to do with
a navigation WebKit is about to perform and enacts the answer. Keeping the
decisions here means they run under pytest on every platform, while the
AppKit half is only ever imported inside the packaged macOS app.

A URL is one of three kinds relative to the server this process owns:

- ``"app"``       loopback, our port — the shell, an app page, an embed, a
                  raw-file URL. Loads inside a window.
- ``"external"``  any other http(s) — the default browser's job.
- ``"other"``     anything else (about:blank, data:, blob:, javascript:).
                  Left to WebKit; never bounced out of the process.

Ported from Render App (fused-render-lite `window_policy.py`), where the
same decisions ran a year of `.fused` windows.
"""
from __future__ import annotations

import os
import re
from urllib.parse import unquote, urlsplit

from fused_render._view_url_codec import (
    app_page_path,
    embed_url_path,
    explorer_view_path,
    view_url_path,
)

#: Filled by app.py on macOS once the run loop is up: ``apply(enabled)`` builds
#: or drops the window manager when the ``native_windows_enabled`` preference
#: flips (shell/prefs.py); ``open_app(path)`` focuses-or-opens an app's own
#: window (POST /api/windows/open — the shell's app clicks inside a native
#: window). Empty under ``fused-render serve`` and on every other platform —
#: the preference still stores, nothing changes hands.
native_hooks: dict = {}


def shell_path_for(fs_path: str) -> str:
    """The shell URL PATH a filesystem path opens at: an app folder (one with
    a tagged entry page) on its app page, anything else on its explorer view /
    embed. One rule for a native window and for a browser tab, so the
    launcher lands on the same address whichever the preference picks."""
    fs_path = os.path.abspath(fs_path)
    if os.path.isdir(fs_path):
        try:
            from fused_render.app_listing import app_entry

            if app_entry(fs_path):
                return app_page_path(fs_path)
        except OSError:
            pass
    return view_url_path(fs_path)

def app_window_path(fs_path: str) -> str:
    """The URL PATH an app opens at in its OWN native window: the app's entry
    page as a chrome-free embed — the app alone, not its source. A `.fused`
    is already an embed; anything with no entry page (a plain folder, a lone
    file) falls back to `shell_path_for`, since there is no app to run. The
    Edit button (`edit_target`) is the way from here into the explorer."""
    fs_path = os.path.abspath(fs_path)
    if os.path.isdir(fs_path):
        try:
            from fused_render.app_listing import app_entry

            entry = app_entry(fs_path)
        except OSError:
            entry = None
        if entry:
            return embed_url_path(entry)
    return shell_path_for(fs_path)


def edit_target(view: str | None, key: str | None) -> str | None:
    """The explorer URL PATH the Edit button opens for a window showing
    ``key`` in ``view`` (`window_view_of`), or None when there is nothing to
    edit from there: Home, /tasks, or a window already in the explorer.

    An embed edits the file it runs; an app page (``/apps/<folder>``) edits
    its entry page, or the folder itself when it has none."""
    if not key or view not in ("embed", "app"):
        return None
    target = key
    if view == "app":
        try:
            from fused_render.app_listing import app_entry

            target = app_entry(key) or key
        except OSError:
            pass
    return explorer_view_path(target)


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}

HOME_FRAME_NAME = "FusedRenderWindow"


def frame_autosave_name(key: str | None, view: str | None = None) -> str:
    """The NSWindow frame-autosave name for a window opened on ``key``.

    ``key`` is what the window was opened to show — an app folder (an
    `/apps/<folder>` page) or a file (an explorer view/embed) — so each app
    reopens where the user last left *that* app, not where the last window
    of any app was. None (the shell home) gets the plain name.

    An EMBED gets a name of its own: an app's run window and an explorer window
    (the explorer view of the same entry file) share ``key``, and each must
    keep its own size and place.
    """
    if not key:
        return HOME_FRAME_NAME
    if view == "embed":
        return f"{HOME_FRAME_NAME}:embed:{os.path.abspath(key)}"
    return f"{HOME_FRAME_NAME}:{os.path.abspath(key)}"


_VIEW_PREFIXES = (("/apps/", "app"), ("/explorer/view/", "view"),
                  ("/explorer/embed/", "embed"))


def window_view_of(url: str | None) -> str | None:
    """How a shell URL shows its `window_key_of` key: ``"app"`` (an app
    page), ``"view"`` (the explorer) or ``"embed"`` (the page alone), or None
    for a URL with no key. The other half of a window's identity: an app's
    run window and an explorer window on it share a key and differ here."""
    if window_key_of(url) is None:
        return None
    path = urlsplit(url).path
    for prefix, view in _VIEW_PREFIXES:
        if path.startswith(prefix):
            return view
    return None


def window_key_of(url: str | None) -> str | None:
    """The filesystem path a shell URL is showing, or None.

    ``/apps/<segments>`` names an app folder; ``/explorer/view/<segments>``
    and ``/explorer/embed/<segments>`` name a file or folder. Everything else
    (home, /tasks, /preferences, the launcher) has no key. Segments are
    percent-decoded one by one, the way the shell's router encodes them.
    This is a window's identity for focus-or-open and the launcher's
    "running" dot; it is read live from the web view, since the shell is an
    SPA whose `pushState` navigations never pass through the policy delegate.
    """
    if not url:
        return None
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    for prefix, _view in _VIEW_PREFIXES:
        if path.startswith(prefix):
            rest = path[len(prefix):]
            segs = [unquote(s) for s in rest.split("/") if s]
            if not segs or any(s in (".", "..") or "/" in s for s in segs):
                return None
            return "/" + "/".join(segs)
    return None


def is_own_origin(host: str | None, port: int | None, app_port: int) -> bool:
    """Is a security origin (``host``, ``port``) this process's own server?

    The gate behind every "may this page …" question WebKit puts to the host:
    camera/mic, geolocation. Our own pages (loopback, our port) get the
    answer the browser would have given after the user clicked Allow once;
    anything else — a third-party iframe inside an app — does not. Same
    host rule as `classify` so the two never disagree about what "ours" is.
    """
    if not host or port is None:
        return False
    host = host.lower()
    if host not in _LOOPBACK_HOSTS and not host.startswith("127."):
        return False
    try:
        return int(port) == app_port
    except (TypeError, ValueError):
        return False


def classify(url: str | None, port: int) -> str:
    """Kind of ``url`` relative to the server on ``port`` (module docstring)."""
    if not url:
        return "other"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "other"
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return "other"
    host = (parts.hostname or "").lower()
    if host in _LOOPBACK_HOSTS or host.startswith("127."):
        try:
            url_port = parts.port
        except ValueError:
            return "external"
        if url_port is None:
            url_port = 443 if scheme == "https" else 80
        return "app" if url_port == port else "external"
    return "external"


def navigation_action(
    url: str | None,
    port: int,
    *,
    is_main_frame: bool,
    has_target_frame: bool,
    wants_download: bool,
    new_window_modifier: bool,
) -> str:
    """What to do with a navigation WebKit asks about, before any response.

    Returns ``"allow"``, ``"download"``, ``"new_window"`` or
    ``"open_external"``.

    Order matters: a ``download`` attribute wins over everything (an app's
    ``<a download href=fused.rawUrl(...)>`` must save, never navigate); then
    where the URL points; then how the click asked to open it. A navigation
    with no target frame is ``target=_blank`` / `window.open` — a new tab in a
    browser, so a new window here. ⌘-click and middle-click mean the same.
    Sub-frame navigations to foreign hosts are the page's own iframe business
    (tile servers, embeds) and stay allowed.
    """
    if wants_download:
        return "download"
    kind = classify(url, port)
    if kind == "app":
        if not has_target_frame or (is_main_frame and new_window_modifier):
            return "new_window"
        return "allow"
    if kind == "external":
        if is_main_frame or not has_target_frame:
            return "open_external"
        return "allow"
    return "allow"


#: Right-click menu items WebKit proposes that would open a URL "in a new
#: window" (`WKMenuItemIdentifiersPrivate.h`), keyed by which URL of the
#: element they act on. The dictionary is what `context_menu_item` knows.
_OPEN_IN_NEW_WINDOW_ITEMS = {
    "WKMenuItemIdentifierOpenImageInNewWindow": ("image", "Open Image in Browser"),
    "WKMenuItemIdentifierOpenLinkInNewWindow": ("link", "Open Link in Browser"),
    "WKMenuItemIdentifierOpenMediaInNewWindow": ("media", "Open Media in Browser"),
}


def context_menu_item(
    identifier: str | None,
    port: int,
    *,
    image_url: str | None = None,
    link_url: str | None = None,
    media_url: str | None = None,
) -> tuple[str, str | None]:
    """What to do with one item of the right-click menu WebKit proposes:
    ``("keep", None)``, ``("drop", None)`` or ``("retitle", "<title>")``.

    WebKit's default menu is a browser's. Its "Open … in New Window" items
    all go through the popup delegate, which does for the URL what it does
    for `window.open`: a page of ours gets a window, so the label is true;
    an external http(s) URL goes to the default browser, so the label is a
    lie — say "in Browser"; anything else (a generated image as a `blob:`
    or `data:` URL) has nowhere to go and the item would do nothing — drop
    it, Copy Image and Download Image are still there. Every other item
    stays: Copy, Copy Image, Look Up, Share, Inspect Element and the
    downloads, which `mac_window` adopts the way it adopts link downloads.
    """
    entry = _OPEN_IN_NEW_WINDOW_ITEMS.get(identifier or "")
    if entry is None:
        return ("keep", None)
    which, browser_title = entry
    url = {"image": image_url, "link": link_url, "media": media_url}[which]
    kind = classify(url, port)
    if kind == "app":
        return ("keep", None)
    if kind == "external":
        return ("retitle", browser_title)
    return ("drop", None)


def response_action(
    *,
    is_main_frame: bool,
    can_show_mime: bool,
    content_disposition: str | None,
) -> str:
    """``"allow"`` or ``"download"`` for a response WebKit is about to render.

    Anything the engine cannot show inline (a parquet, a zip, a .fused) and
    anything the server marks ``attachment`` becomes a download instead of a
    blank page or a "cannot show" sheet.
    """
    if not is_main_frame:
        return "allow"
    if _is_attachment(content_disposition):
        return "download"
    if not can_show_mime:
        return "download"
    return "allow"


def _is_attachment(disposition: str | None) -> bool:
    if not disposition:
        return False
    return disposition.split(";", 1)[0].strip().lower() == "attachment"


_UNSAFE_NAME = re.compile(r"[\x00-\x1f/\\:]")


def download_destination(downloads_dir: str, suggested: str | None,
                         exists=os.path.exists) -> str:
    """A path under ``downloads_dir`` that does not exist yet.

    WebKit refuses to write a download over an existing file, so the Finder
    convention applies: ``name``, ``name 2``, ``name 3`` … The suggested name
    is reduced to one safe path component first; an empty or unusable one
    becomes ``download``.
    """
    name = _UNSAFE_NAME.sub("_", (suggested or "").strip()) or "download"
    if name in (".", ".."):
        name = "download"
    stem, ext = os.path.splitext(name)
    if not stem:  # ".bashrc"-style names: keep the whole thing as the stem
        stem, ext = name, ""
    candidate = os.path.join(downloads_dir, name)
    counter = 2
    while exists(candidate):
        candidate = os.path.join(downloads_dir, f"{stem} {counter}{ext}")
        counter += 1
    return candidate
