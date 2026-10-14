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
                  Left to WebKit; never bounced out of the process — EXCEPT a
                  main-frame navigation to a scheme WebKit cannot load itself
                  (``fused-render://``, ``mailto:``, ...). WKWebView on macOS
                  does not hand those to LaunchServices the way Safari does:
                  the load fails in-view with "unsupported URL" and nothing
                  else happens. That is how the update dialog's Restart
                  (``fused-render://relaunch``), the FDA strip's relaunch and
                  the down card's launch link all did nothing inside a native
                  window — the page sat at "Quitting…" until its two-minute
                  cap. Those go out through NSWorkspace like an external link,
                  which is what delivers them to `application:openURLs:`.

Ported from Render App (fused-render-lite `window_policy.py`), where the
same decisions ran a year of `.fused` windows.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import quote, unquote, urlsplit

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


def app_home_path(fs_path: str) -> str:
    """The URL PATH an app opens at INSIDE the Home window (the
    ``apps_open_in_home`` preference): the explorer view of its entry page,
    or of the folder when it has none — what the browser shell navigates to
    on an app click, NOT the chrome-free embed of `app_window_path`."""
    fs_path = os.path.abspath(fs_path)
    target = fs_path
    if os.path.isdir(fs_path):
        try:
            from fused_render.app_listing import app_entry

            target = app_entry(fs_path) or fs_path
        except OSError:
            pass
    return explorer_view_path(target)


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


# ---- Shell Tabs: a host window's URL names every tab -----------------------
#
# With `shell_tabs_enabled` on, a native window's page is a TAB HOST: one
# shell drawing a tab strip, each surface an iframe. The host mirrors its
# state into its own URL so the window code — which only ever sees a URL,
# read live (KVO on macOS, `get_uri` on Linux) — can tell what is open
# without asking the page: the path+query is the ACTIVE tab's (its embed
# marker stripped), and one extra query param lists every tab in strip
# order, comma-joined, each non-active one ``encodeURIComponent``-ed and the
# active one's slot the literal ``.``. One tab = no param at all, so a
# non-host window and a single-tab host read the same. A relaunch snapshot
# records that URL as it is and reopens the whole strip from it.
#
#   /apps/map?_tabs=%2Fhome,.,%2Ftasks   — [Home, the map app (active), Tasks]
#
# One exception: an active tab that is not a page of the shell SPA (a
# ``/render?path=…`` runtime page) cannot be the host's own location — a
# reload there would boot the bare runtime, no strip — so the host sits on
# ``/home`` and the active slot is ``!`` + the encoded url instead of ``.``,
# written even for a single tab (``/home?_tabs=!%2Frender%3F…``). The host
# location is then no tab at all.

#: The host URL's tab-list query param (see above).
TABS_PARAM = "_tabs"
_TABS_PREFIX = TABS_PARAM + "="


def _split_tabs(query: str) -> tuple[list[str], str | None]:
    """``query`` without its ``_tabs`` items, and the raw value of the last
    one (None when absent). Raw, NOT `parse_qs`: an item's own commas are
    ``%2C`` and must survive the split on ``,`` before anything decodes."""
    kept, raw = [], None
    for part in query.split("&") if query else ():
        if part == TABS_PARAM or part.startswith(_TABS_PREFIX):
            raw = part[len(_TABS_PREFIX):] if part != TABS_PARAM else ""
        elif part:
            kept.append(part)
    return kept, raw


def strip_tabs(url: str | None) -> str:
    """``url`` without the ``_tabs`` param, the rest of it (scheme, host,
    other params in their order, fragment) untouched — what Open in Browser
    and Copy URL hand out: the active tab, not a strip a browser cannot draw."""
    if not url:
        return url or ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    kept, raw = _split_tabs(parts.query)
    if raw is None:
        return url
    return parts._replace(query="&".join(kept)).geturl()


def tab_path(url: str | None) -> str:
    """The same-origin PATH+QUERY of ``url``, ``_tabs`` dropped — the form
    `window._fusedTabs.open` takes (`/apps/x`, `/tasks?a=1`), with its
    ``#fragment`` kept when it has one. For a host URL that is its ACTIVE
    tab's, a ``!`` slot included (`active_tab_url`). "/" for an empty or
    unparsable URL."""
    try:
        parts = urlsplit(active_tab_url(url))
    except ValueError:
        return "/"
    path = parts.path or "/"
    return (path + ("?" + parts.query if parts.query else "")
            + ("#" + parts.fragment if parts.fragment else ""))


def has_tabs(url: str | None) -> bool:
    """Does ``url`` carry a ``_tabs`` list — a host showing more than one
    tab, or a runtime-page tab (``!``)? False for every window that never
    hosted, which is what keeps the pref-off paths byte-for-byte."""
    if not url:
        return False
    try:
        return _split_tabs(urlsplit(url).query)[1] is not None
    except ValueError:
        return False


def same_tab_page(a: str, b: str) -> bool:
    """Two tab paths name the same PAGE, the host's `samePage` rule: path
    only (a tab that changed its sort is still that tab), ``/`` is
    ``/home``, and an explorer view is its embed (the frame's spelling)."""
    def norm(u: str) -> str:
        path = urlsplit(u).path.rstrip("/") or "/home"
        if path.startswith("/explorer/view/"):
            path = "/explorer/embed/" + path[len("/explorer/view/"):]
        return path
    try:
        return norm(a) == norm(b)
    except ValueError:
        return False


def tab_edit_target(view: str | None, key: str | None) -> str | None:
    """`edit_target` for a TAB HOST: the explorer URL PATH Edit opens. The
    host mirrors an embed tab under ``/explorer/view/``, so a ``view`` key
    is an app or file RUNNING in a tab (a tab frame never shows the
    explorer itself) and edits like an embed; a folder key edits its entry
    page. None for a keyless tab (Home, /tasks)."""
    if not key or view not in ("embed", "app", "view"):
        return None
    target = key
    if os.path.isdir(key):
        try:
            from fused_render.app_listing import app_entry

            target = app_entry(key) or key
        except OSError:
            pass
    return explorer_view_path(target)


def tab_urls(url: str | None) -> list[str]:
    """Every tab of a host window, in strip order, each as a PATH+QUERY
    (`tab_path`'s form, the origin dropped — they are all the host's own).
    The ``.`` slot is the host URL itself minus ``_tabs``; a ``!``-prefixed
    slot is an active tab the host could not sit on (the host location is
    then no tab); a URL with no ``_tabs`` is one tab, itself. Empty items
    are skipped, and a list with neither marker still counts the host
    location as the active tab, first. Empty for no URL."""
    if not url:
        return []
    try:
        query = urlsplit(url).query
    except ValueError:
        return []
    active = tab_path(url)
    _kept, raw = _split_tabs(query)
    if raw is None:
        return [active]
    out, seen_active = [], False
    for item in raw.split(","):
        if item == ".":
            if not seen_active:
                out.append(active)
                seen_active = True
        elif item.startswith("!"):
            if len(item) > 1:
                out.append(unquote(item[1:]))
            seen_active = True
        elif item:
            out.append(unquote(item))
    if not seen_active:
        out.insert(0, active)
    return out


def active_tab_url(url: str | None) -> str:
    """The full URL of a host's ACTIVE tab: `strip_tabs` of the host URL,
    or — with a ``!`` slot (see above) — that tab's path+query on the host's
    origin. What Open in Browser and Copy URL hand out."""
    stripped = strip_tabs(url)
    if not url:
        return stripped
    try:
        parts = urlsplit(url)
    except ValueError:
        return stripped
    _kept, raw = _split_tabs(parts.query)
    for item in (raw or "").split(","):
        if item.startswith("!") and len(item) > 1:
            path, _, query = unquote(item[1:]).partition("?")
            return parts._replace(path=path or "/", query=query, fragment="").geturl()
    return stripped


def tab_view(view: str | None) -> str | None:
    """A `window_view_of` view in a TAB's terms. Inside a tab the explorer
    view and the embed of one file are the same page — the frame always
    shows the embed, and the host mirrors it under ``/explorer/view/`` (a
    host reloaded on an embed URL would boot chrome-free, no strip) — so
    both read ``"view"``. Only for matching against tabs: between WINDOWS
    an app's run window and its explorer window stay two things."""
    return "view" if view == "embed" else view


def tab_identities(url: str | None) -> set[tuple[str, str | None]]:
    """(`window_key_of`, `tab_view`) of every keyed tab of ``url``: what a
    host window SHOWS, tab by tab, for focus-or-open to match on."""
    out = set()
    for u in tab_urls(url):
        key = window_key_of(u)
        if key is not None:
            out.add((key, tab_view(window_view_of(u))))
    return out


def tab_keys(url: str | None) -> set[str]:
    """`window_key_of` of every tab of ``url`` (keyless tabs — Home, /tasks
    — contribute nothing): the launcher's running dot for a host window."""
    return {key for key, _view in tab_identities(url)}


def tab_url_of(path: str) -> str:
    """One ``_tabs`` item for a non-active tab: ``encodeURIComponent`` of its
    path+query (JS's safe set, so the host decodes exactly what it wrote).
    The inverse of the split in `tab_urls`; tests build host URLs with it."""
    return quote(path, safe="-_.!~*'()")


# ---- Shell Tabs: what the window code says to a host page -------------------
#
# The host page owns `window._fusedTabs` (the frontend); the window code only
# EVALUATES these strings in a window's top document — `evaluateJavaScript`
# on macOS, `run_javascript` on Linux — and acts on the answer. Every one is
# synchronous and answers a plain value, so "no host here" (the flag off when
# that page booted, a popup, a page still loading) is just a ``false`` /
# ``'win'`` the caller turns into today's window behaviour. One copy, here,
# so both platforms say exactly the same thing to the same API.


def tabs_js(expr: str) -> str:
    """Run ``expr`` (JS, with the host API bound to ``t``) if this page is a
    tab host: evaluates to ``true`` when it ran, ``false`` when there is no
    host."""
    return ("(function(){var t=window._fusedTabs; if(!t) return false; "
            f"{expr}; return true;}})()")


def tabs_open_js(path: str) -> str:
    """Open ``path`` (`tab_path`'s form) as a tab, or select the tab already
    showing it."""
    return tabs_js(f"t.open({json.dumps(path)},{{focusExisting:true}})")


def tabs_navigate_js(path: str) -> str:
    """Point the ACTIVE tab at ``path`` (Edit: the run tab becomes its explorer)."""
    return tabs_js(f"t.navigateActive({json.dumps(path)})")


def tabs_nth_js(n: int) -> str:
    """Select the ``n``-th tab (1-based; ⌘1…⌘9)."""
    return tabs_js(f"t.nth({int(n)})")


def tabs_bot_js(view: str) -> str:
    """Show bot chat ``view`` (``/bots/<id>``) in the host's Bots tab: the
    first tab anywhere on ``/bots`` is selected and pointed at it — a second
    Bots tab per bot would be the per-window pile-up tabs exist to end —
    else a new tab opens on it. The Bots store reads the URL on mount and
    follows it after, so the route alone selects the chat."""
    v = json.dumps(view)
    return _tabs_bots_scan(f"t.navigateActive({v}); ", view)


def tabs_bots_home_js() -> str:
    """Home under Fused Bot, whose front door is the Bots page: the first
    tab anywhere on ``/bots`` (``/bots/<id>`` included) is SELECTED, left
    on whatever chat it shows — `focusExisting` matches by path, so
    `tabs_open_js("/bots")` would open a second Bots tab beside one on a
    chat — else a new tab opens on ``/bots``."""
    return _tabs_bots_scan("", "/bots")


def _tabs_bots_scan(then: str, fallback: str) -> str:
    # One loop for `tabs_bot_js` / `tabs_bots_home_js`: select the first tab
    # on /bots or /bots/*, run ``then``; none → open ``fallback``.
    f = json.dumps(fallback)
    return tabs_js(
        "var l=t.list(), i, p; for(i=0;i<l.length;i++){"
        "p=String(l[i].url||'').split('?')[0];"
        "if(p==='/bots'||p.indexOf('/bots/')===0){"
        f"t.select(l[i].id); {then}return true;}}}} "
        f"t.open({f},{{focusExisting:true}})")


def tabs_open_or_home_js(path: str, is_bot: bool) -> str:
    """`tabs_open_js`, except Home under Fused Bot (``/``, ``/home`` or
    ``/bots`` itself) goes through `tabs_bots_home_js`."""
    if is_bot and path.split("?", 1)[0].rstrip("/") in ("", "/home", "/bots"):
        return tabs_bots_home_js()
    return tabs_open_js(path)


#: ⌘W / Ctrl+W: ``'tab'`` when a tab closed, ``'win'`` when the WINDOW should
#: close instead — no host, or this is its last tab (closing the last tab of
#: a host would only make it open a fresh Home, which is not what ⌘W means).
TABS_CLOSE_JS = ("(function(){var t=window._fusedTabs; if(!t) return 'win'; "
                 "if(t.list().length<=1) return 'win'; t.close(); return 'tab';})()")


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

    Only the PATH is read, so a tab host's ``_tabs`` query (`TABS_PARAM`)
    never leaks into the key: a host window's URL is its ACTIVE tab's, and
    this is that tab's key. The other tabs' keys are `tab_keys`.
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
    if is_main_frame and needs_launch_services(url):
        return "open_external"
    return "allow"


#: Schemes WebKit renders or resolves on its own. Anything else with a scheme
#: is some other application's URL (a deep link, mailto:, tel:) and WKWebView
#: will not open it — see the module docstring.
WEBKIT_SCHEMES = frozenset({"http", "https", "about", "blob", "data",
                            "javascript", "file"})


def needs_launch_services(url: str | None) -> bool:
    """True for a URL whose scheme only another app can handle."""
    if not url:
        return False
    try:
        scheme = (urlsplit(url).scheme or "").lower()
    except ValueError:
        return False
    return bool(scheme) and scheme not in WEBKIT_SCHEMES


#: Right-click menu items WebKit proposes that would open a URL "in a new
#: window" (`WKMenuItemIdentifiersPrivate.h`), keyed by which URL of the
#: element they act on. The dictionary is what `context_menu_item` knows.
_OPEN_IN_NEW_WINDOW_ITEMS = {
    "WKMenuItemIdentifierOpenImageInNewWindow": ("image", "Open Image in Browser"),
    "WKMenuItemIdentifierOpenLinkInNewWindow": ("link", "Open Link in Browser"),
    "WKMenuItemIdentifierOpenMediaInNewWindow": ("media", "Open Media in Browser"),
}


#: The same items' labels under Shell Tabs, for a URL of ours (`context_menu_item`).
_OPEN_IN_NEW_TAB_TITLES = {
    "image": "Open Image in New Tab",
    "link": "Open Link in New Tab",
    "media": "Open Media in New Tab",
}


def context_menu_item(
    identifier: str | None,
    port: int,
    *,
    image_url: str | None = None,
    link_url: str | None = None,
    media_url: str | None = None,
    tab_mode: bool = False,
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

    ``tab_mode`` (Shell Tabs, `shell_tabs_enabled`): a page of ours opens as
    a TAB of the window it was clicked in, not a window, so that label says
    "in New Tab" instead — same item, same delegate path.
    """
    entry = _OPEN_IN_NEW_WINDOW_ITEMS.get(identifier or "")
    if entry is None:
        return ("keep", None)
    which, browser_title = entry
    url = {"image": image_url, "link": link_url, "media": media_url}[which]
    kind = classify(url, port)
    if kind == "app":
        if tab_mode:
            return ("retitle", _OPEN_IN_NEW_TAB_TITLES[which])
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
