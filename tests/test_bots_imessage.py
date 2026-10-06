"""fused_render.bots.imessage: the pure helpers (no chat.db, no osascript).
The poll loop and delivery policy are tested in test_bots_channels.py."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import os

from fused_render.bots import imessage


def test_paths_resolve_lazily_under_app_home(app_home, tmp_path, monkeypatch):
    data = os.path.join(str(app_home), "bots", "data")
    assert imessage.CURSOR == os.path.join(data, "imessage.json")
    assert imessage.LOCK == os.path.join(data, "imessage.lock")
    assert imessage.STATE == os.path.join(data, "imessage-state.json")
    assert imessage.DATA == data and imessage.BOTS == os.path.join(data, "bots")
    other = tmp_path / "elsewhere"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(other))
    assert imessage.cursor_path() == os.path.join(str(other), "bots", "data", "imessage.json")


def test_norm_handle():
    assert imessage.norm_handle("+1 (555) 123-4567") == "+15551234567"
    assert imessage.norm_handle("555 123 4567") == "+15551234567"
    assert imessage.norm_handle("44 20 7946 0958") == "+442079460958"
    assert imessage.norm_handle(" Mom@iCloud.COM ") == "mom@icloud.com"
    assert imessage.norm_handle("") == "" and imessage.norm_handle(None) == ""


def test_parse_contacts_strips_bidi_and_dedupes():
    text = ("Ali ⁦+1 (555) 123-4567⁩\n"
            "mom@icloud.com, Bob: 555.987.6543; nobody here\n"
            "Ali again +15551234567\n"
            "‎Dana <+44 20 7946 0958>")
    out = imessage.parse_contacts(text, owner="+1 555 000 1111")
    assert out == [("the user", "+15550001111"), ("Ali", "+15551234567"), ("mom@icloud.com", "mom@icloud.com"),
                   ("Bob", "+15559876543"), ("Dana", "+442079460958")]
    assert imessage.parse_contacts("") == []
    assert imessage.parse_contacts("", owner="x@y.co") == [("the user", "x@y.co")]
    # the owner is not listed twice when also typed as a contact
    assert imessage.parse_contacts("Me +15550001111", owner="+15550001111") == [("the user", "+15550001111")]


def test_resolve_contact():
    contacts = [("the user", "+15550001111"), ("Ali Rahimi", "+15551234567"), ("mom@icloud.com", "mom@icloud.com")]
    assert imessage.resolve_contact("ali rahimi", contacts) == ("Ali Rahimi", "+15551234567")
    assert imessage.resolve_contact("(555) 123-4567", contacts) == ("Ali Rahimi", "+15551234567")
    assert imessage.resolve_contact("Ali", contacts) == ("Ali Rahimi", "+15551234567")  # substring of the label
    assert imessage.resolve_contact("MOM@icloud.com", contacts) == ("mom@icloud.com", "mom@icloud.com")
    assert imessage.resolve_contact("the user", contacts) == ("the user", "+15550001111")
    assert imessage.resolve_contact("Zed", contacts) is None
    assert imessage.resolve_contact("+19998887777", contacts) is None
    assert imessage.resolve_contact("", contacts) is None


def test_chunks_split_at_line_breaks():
    assert list(imessage.chunks("  hi  ")) == ["hi"]
    assert list(imessage.chunks("   ")) == []
    para = ("a" * 2000) + "\n" + ("b" * 2000)
    assert list(imessage.chunks(para)) == ["a" * 2000, "b" * 2000]
    solid = "c" * 7000  # no usable break: hard cut at MAX_TEXT
    parts = list(imessage.chunks(solid))
    assert [len(p) for p in parts] == [3000, 3000, 1000] and "".join(parts) == solid
    early = "x\n" + "d" * 5000  # a break before MAX_TEXT/2 is ignored
    assert [len(p) for p in imessage.chunks(early)] == [3000, 2002]


def _blob(text, prefix=b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84"):
    body = text.encode()
    n = len(body)
    if n < 0x80:
        ln = bytes([n])
    elif n < 0x10000:
        ln = b"\x81" + n.to_bytes(2, "little")
    else:
        ln = b"\x82" + n.to_bytes(4, "little")
    return prefix + b"NSString\x01\x94\x84\x01+" + ln + body + b"\x86\x84\x02iI\x01"


def test_decode_attributed_body():
    assert imessage.decode_attributed_body(_blob("hello there")) == "hello there"
    long = "é" * 150  # 300 bytes: two-byte length
    assert imessage.decode_attributed_body(_blob(long)) == long
    big = "z" * 70000  # four-byte length
    assert imessage.decode_attributed_body(_blob(big)) == big
    assert imessage.decode_attributed_body(b"") == ""
    assert imessage.decode_attributed_body(None) == ""
    assert imessage.decode_attributed_body(b"no marker at all") == ""
    assert imessage.decode_attributed_body(b"NSString" + b"\x00" * 20 + b"+\x03abc") == ""  # '+' too far away


def test_handles_to_bots():
    from fused_render.bots import store

    store.write_meta("a", {"id": "a", "imessage": "+1 555 123 4567"})
    store.write_meta("b", {"id": "b", "imessage": "+15551234567"})  # same handle: both bots, id order (the router picks)
    store.write_meta("c", {"id": "c", "imessage": ""})
    store.write_meta("d", {"id": "d", "imessage": "Me@Mail.com"})
    assert imessage.handles_to_bots() == {"+15551234567": ["a", "b"], "me@mail.com": ["d"]}
    assert imessage.bots_with_handles() == {"+15551234567": "a", "me@mail.com": "d"}  # the old first-wins shape


def test_cursor_round_trip():
    assert imessage.load_cursor() == {"rowid": None, "sent": {}}
    imessage.save_cursor({"rowid": 7, "line": {"a": 3}, "sent": {"hi": 1.0}, "seq": {"old": 1}})
    assert imessage.load_cursor() == {"rowid": 7, "sent": {"hi": 1.0}}  # the older cursors' "seq" and "line" dropped


def test_format_texts():
    assert imessage.format_texts("Ali", []) == "no texts with Ali yet"
    s = imessage.format_texts("Ali", [{"rowid": 1, "ts": 0, "me": True, "text": "hi"},
                                      {"rowid": 2, "ts": 0, "me": False, "text": "x" * 400}])
    assert s.startswith("TEXTS with Ali (oldest first):\n")
    assert "] me: hi" in s and "] Ali: " + "x" * 300 + "\n" not in s and s.endswith("x" * 300)
