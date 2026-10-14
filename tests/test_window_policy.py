"""The native windows' navigation/download policy (fused_render/window_policy.py).

`mac_window.py` is AppKit and never imports in CI; every decision it acts on
lives in `window_policy.py` so it can be pinned here on any platform.
"""

import pytest

from fused_render import window_policy as wp

PORT = 1777
APP = f"http://127.0.0.1:{PORT}"


@pytest.mark.parametrize(
    "url, kind",
    [
        (f"{APP}/", "app"),
        (f"{APP}/apps/Users/me/Fused/local/foo?_tab=files", "app"),
        (f"http://localhost:{PORT}/render", "app"),
        (f"http://127.0.0.1:{PORT + 1}/", "external"),  # some other local server
        ("http://127.0.0.1/", "external"),  # port 80 is not our server
        ("https://huggingface.co/models", "external"),
        ("about:blank", "other"),
        (f"blob:{APP}/abc", "other"),
        ("data:text/plain,hi", "other"),
        ("javascript:void(0)", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_classify(url, kind):
    assert wp.classify(url, PORT) == kind


@pytest.mark.parametrize(
    "host, port, own",
    [
        ("127.0.0.1", PORT, True),
        ("localhost", PORT, True),
        ("LOCALHOST", PORT, True),
        ("[::1]", PORT, True),
        ("127.0.0.1", PORT + 1, False),  # some other local server
        ("127.0.0.1", 0, False),          # WKSecurityOrigin of an opaque origin
        ("127.0.0.1", None, False),
        ("huggingface.co", PORT, False),  # a foreign host on "our" port number
        ("", PORT, False),
        (None, PORT, False),
    ],
)
def test_is_own_origin(host, port, own):
    assert wp.is_own_origin(host, port, PORT) is own


def _nav(url, **kw):
    base = dict(is_main_frame=True, has_target_frame=True,
                wants_download=False, new_window_modifier=False)
    base.update(kw)
    return wp.navigation_action(url, PORT, **base)


def test_plain_in_app_navigation_is_allowed():
    assert _nav(f"{APP}/apps/Users/me/Fused/local/x") == "allow"


def test_target_blank_to_the_app_opens_a_new_window():
    # window.open / target=_blank arrive with no target frame.
    assert _nav(f"{APP}/explorer/embed/Users/me/y.fused", has_target_frame=False) == "new_window"


def test_cmd_click_on_an_app_link_opens_a_new_window():
    assert _nav(f"{APP}/", new_window_modifier=True) == "new_window"


def test_cmd_click_inside_a_subframe_stays_put():
    # An app's own iframe navigating itself.
    assert _nav(f"{APP}/render?x=1", is_main_frame=False,
                new_window_modifier=True) == "allow"


def test_external_main_frame_and_popups_go_to_the_default_browser():
    assert _nav("https://huggingface.co/x") == "open_external"
    assert _nav("https://github.com/", has_target_frame=False) == "open_external"


def test_external_subframe_loads_stay_inside_the_page():
    assert _nav("https://tiles.example.com/1/2/3.png", is_main_frame=False) == "allow"


def test_download_attribute_wins_over_everything():
    assert _nav(f"{APP}/api/fs/raw?path=a.parquet", wants_download=True) == "download"
    assert _nav(f"blob:{APP}/abc", has_target_frame=False, wants_download=True) == "download"


def test_other_schemes_are_left_to_webkit():
    assert _nav("about:blank", has_target_frame=False) == "allow"
    assert _nav(f"blob:{APP}/x") == "allow"
    assert _nav("data:text/plain,hi") == "allow"
    assert _nav("javascript:void(0)") == "allow"


@pytest.mark.parametrize("url", [
    "fused-render://relaunch",            # the update dialog's Restart
    "fused-render://relaunch?reason=fda",  # FdaStrip / FdaStep / IndexFdaCta
    "fused-render://launch",              # the down card
    "mailto:someone@example.com",
])
def test_deep_links_go_out_through_launch_services(url):
    # WKWebView fails these in-view ("unsupported URL") instead of asking
    # LaunchServices, so a native window must bounce them out itself —
    # otherwise the Restart button does nothing and the page waits out its cap.
    assert wp.needs_launch_services(url) is True
    assert _nav(url) == "open_external"
    # A sub-frame has no business launching apps.
    assert _nav(url, is_main_frame=False) == "allow"


def _resp(**kw):
    base = dict(is_main_frame=True, can_show_mime=True, content_disposition=None)
    base.update(kw)
    return wp.response_action(**base)


def test_attachment_disposition_downloads():
    assert _resp(content_disposition='attachment; filename="a.fused"') == "download"
    assert _resp(content_disposition="ATTACHMENT") == "download"
    assert _resp(content_disposition='inline; filename="a.png"') == "allow"


def test_unshowable_mime_downloads():
    assert _resp(can_show_mime=False) == "download"


def test_subframe_responses_are_never_downloads():
    assert _resp(is_main_frame=False, can_show_mime=False,
                 content_disposition="attachment") == "allow"


def test_download_destination_dedupes_like_finder():
    taken = {"/dl/a.parquet", "/dl/a 2.parquet"}
    assert wp.download_destination("/dl", "a.parquet", exists=taken.__contains__) == "/dl/a 3.parquet"
    assert wp.download_destination("/dl", "b.zip", exists=taken.__contains__) == "/dl/b.zip"


def test_download_destination_sanitises_the_name():
    never = lambda _p: False  # noqa: E731
    assert wp.download_destination("/dl", "../../etc/passwd", exists=never) == "/dl/.._.._etc_passwd"
    assert wp.download_destination("/dl", "", exists=never) == "/dl/download"
    assert wp.download_destination("/dl", "..", exists=never) == "/dl/download"
    assert wp.download_destination("/dl", ".bashrc", exists=lambda p: p == "/dl/.bashrc") == "/dl/.bashrc 2"


def test_frame_autosave_name():
    assert wp.frame_autosave_name(None) == "FusedRenderWindow"
    assert wp.frame_autosave_name("") == "FusedRenderWindow"
    p1 = wp.frame_autosave_name("/Users/me/Fused/local/a")
    assert p1 == wp.frame_autosave_name("/Users/me/Fused/local/./a")
    assert p1 != wp.frame_autosave_name("/Users/me/Fused/local/b")
    assert p1.startswith("FusedRenderWindow:")


@pytest.mark.parametrize(
    "url, key",
    [
        (f"{APP}/apps/Users/me/Fused/local/foo", "/Users/me/Fused/local/foo"),
        (f"{APP}/apps/Users/me/Fused/local/foo?_tab=files", "/Users/me/Fused/local/foo"),
        (f"{APP}/apps/Users/me/my%20app/", "/Users/me/my app"),
        (f"{APP}/explorer/view/Users/me/a.parquet", "/Users/me/a.parquet"),
        (f"{APP}/explorer/embed/Users/me/a.fused", "/Users/me/a.fused"),
        (f"{APP}/", None),
        (f"{APP}/apps", None),
        (f"{APP}/apps/", None),
        (f"{APP}/apps/..%2Fetc", None),
        (f"{APP}/tasks", None),
        (f"{APP}/explorer", None),
        ("about:blank", None),
        (None, None),
    ],
)
def test_window_key_of(url, key):
    assert wp.window_key_of(url) == key


# ---- Shell Tabs: the host URL's `_tabs` mirror ------------------------------

HOST3 = f"{APP}/apps/Users/me/map?_tabs=%2Fhome,.,%2Ftasks"


def test_tab_urls_reads_every_tab_in_strip_order():
    assert wp.tab_urls(HOST3) == ["/home", "/apps/Users/me/map", "/tasks"]


def test_tab_urls_without_the_param_is_the_url_itself():
    assert wp.tab_urls(f"{APP}/apps/Users/me/map?_tab=files") == ["/apps/Users/me/map?_tab=files"]
    assert wp.tab_urls(f"{APP}/") == ["/"]
    assert wp.tab_urls(None) == []
    assert wp.tab_urls("") == []


def test_tab_urls_keeps_each_items_own_query_and_commas():
    other = "/explorer/view/Users/me/a,b.csv?x=1&y=2"
    url = f"{APP}/tasks?q=1&_tabs=.,{wp.tab_url_of(other)}"
    assert wp.tab_urls(url) == ["/tasks?q=1", other]


def test_tab_urls_counts_the_active_tab_even_without_its_dot():
    assert wp.tab_urls(f"{APP}/tasks?_tabs=%2Fhome") == ["/tasks", "/home"]


def test_bang_slot_is_an_active_tab_the_host_cannot_sit_on():
    render = "/render?path=/Users/me/a.py"
    url = f"{APP}/home?_tabs=%2Ftasks,!{wp.tab_url_of(render)}"
    assert wp.tab_urls(url) == ["/tasks", render]  # /home itself is no tab
    assert wp.tab_urls(f"{APP}/home?_tabs=!{wp.tab_url_of(render)}") == [render]
    assert wp.active_tab_url(url) == APP + render
    assert wp.tab_path(url) == render
    assert wp.tab_keys(url) == set() and wp.tab_identities(url) == set()
    view = "/explorer/embed/Users/me/b.html"
    mixed = f"{APP}/home?_tabs={wp.tab_url_of(view)},!{wp.tab_url_of(render)}"
    assert wp.tab_identities(mixed) == {("/Users/me/b.html", "view")}
    assert wp.active_tab_url(HOST3) == f"{APP}/apps/Users/me/map"


def test_strip_tabs_keeps_the_rest_of_the_url():
    assert wp.strip_tabs(HOST3) == f"{APP}/apps/Users/me/map"
    assert wp.strip_tabs(f"{APP}/tasks?a=1&_tabs=.,%2Fhome&b=2#f") == f"{APP}/tasks?a=1&b=2#f"
    assert wp.strip_tabs(f"{APP}/tasks?a=1") == f"{APP}/tasks?a=1"
    assert wp.strip_tabs(None) == ""


def test_tab_path_is_path_and_query_without_tabs():
    assert wp.tab_path(HOST3) == "/apps/Users/me/map"
    assert wp.tab_path(f"{APP}/tasks?a=1&_tabs=.,%2Fhome#sec") == "/tasks?a=1#sec"
    assert wp.tab_path(f"{APP}/tasks?a=1&_tabs=.,%2Fhome") == "/tasks?a=1"
    assert wp.tab_path(APP) == "/"


def test_has_tabs_only_for_multi_tab_or_bang_hosts():
    assert wp.has_tabs(HOST3) is True
    assert wp.has_tabs(f"{APP}/home?_tabs=!%2Frender") is True
    assert wp.has_tabs(f"{APP}/apps/Users/me/map?_tab=files") is False
    assert wp.has_tabs(None) is False


def test_same_tab_page_follows_the_hosts_rule():
    assert wp.same_tab_page("/", "/home")
    assert wp.same_tab_page("/explorer/view/a/b.html", "/explorer/embed/a/b.html?x=1")
    assert wp.same_tab_page("/tasks?a=1", "/tasks")
    assert not wp.same_tab_page("/bots", "/bots/x")


def test_tab_edit_target_treats_a_view_tab_as_running():
    assert wp.tab_edit_target("view", "/nope/a.html") == "/explorer/view/nope/a.html"
    assert wp.tab_edit_target("embed", "/nope/a.html") == "/explorer/view/nope/a.html"
    assert wp.tab_edit_target("view", None) is None
    assert wp.tab_edit_target(None, "/nope/a.html") is None


def test_window_key_and_view_ignore_the_tabs_param():
    # The host's own identity is its ACTIVE tab's.
    assert wp.window_key_of(HOST3) == "/Users/me/map"
    assert wp.window_view_of(HOST3) == "app"


def test_tab_keys_and_identities_cover_every_keyed_tab():
    embed = "/explorer/embed/Users/me/app/index.html"
    url = f"{APP}/apps/Users/me/map?_tabs=%2Fhome,.,{wp.tab_url_of(embed)},%2Ftasks"
    assert wp.tab_keys(url) == {"/Users/me/map", "/Users/me/app/index.html"}
    # An embed tab and an explorer-view tab are one page inside a tab.
    assert wp.tab_identities(url) == {("/Users/me/map", "app"),
                                      ("/Users/me/app/index.html", "view")}
    assert wp.tab_view("embed") == "view" and wp.tab_view("app") == "app"
    assert wp.tab_keys(f"{APP}/home") == set()
    assert wp.tab_keys(None) == set()


def test_bot_home_selects_any_bots_tab_without_navigating():
    js = wp.tabs_bots_home_js()
    assert "p.indexOf('/bots/')===0" in js and "t.select(l[i].id)" in js
    assert "navigateActive" not in js
    assert 't.open("/bots",{focusExisting:true})' in js
    # The bot-chat idiom is the same scan plus the route.
    assert 't.navigateActive("/bots/x")' in wp.tabs_bot_js("/bots/x")
    for path in ("/", "/home", "/bots"):
        assert wp.tabs_open_or_home_js(path, True) == js
    assert wp.tabs_open_or_home_js("/bots/x", True) == wp.tabs_open_js("/bots/x")
    assert wp.tabs_open_or_home_js("/home", False) == wp.tabs_open_js("/home")


def test_context_menu_says_new_tab_only_in_tab_mode():
    ident = "WKMenuItemIdentifierOpenLinkInNewWindow"
    link = f"{APP}/apps/Users/me/map"
    assert wp.context_menu_item(ident, PORT, link_url=link) == ("keep", None)
    assert wp.context_menu_item(ident, PORT, link_url=link, tab_mode=True) == (
        "retitle", "Open Link in New Tab")
    # An external link still goes to the browser, tabs or not.
    assert wp.context_menu_item(ident, PORT, link_url="https://x.org/", tab_mode=True) == (
        "retitle", "Open Link in Browser")
