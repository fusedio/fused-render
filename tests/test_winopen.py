from fused_render import winopen


def test_progid():
    assert winopen._progid(".csv") == "FusedRender.csv"


def test_type_name():
    assert winopen._type_name(".csv") == "CSV File (fused-render)"


def test_extensions_are_dotted_and_exclude_sentinels():
    ext_list = winopen.extensions()
    assert ext_list  # registry.json ships with real entries
    assert all(ext.startswith(".") and "/" not in ext and "*" not in ext for ext in ext_list)
    assert not (winopen._NOT_EXTENSIONS & set(ext_list))
    assert ext_list == sorted(ext_list)


def test_extensions_excludes_glob_patterns():
    # ".*.json" is a template wildcard key, not a real registerable extension
    assert ".*.json" not in winopen.extensions()


def test_view_url_no_path():
    assert winopen._view_url(1777, None) == "http://127.0.0.1:1777/"


def test_view_url_encodes_each_segment(monkeypatch):
    # abspath is OS-dependent (backslashes aren't separators on POSIX);
    # stub it so the segment-splitting/encoding logic is tested in isolation
    monkeypatch.setattr(winopen.os.path, "abspath", lambda p: p)
    url = winopen._view_url(1777, r"C:\data\sales.csv")
    assert url == "http://127.0.0.1:1777/view/C%3A/data/sales.csv"
