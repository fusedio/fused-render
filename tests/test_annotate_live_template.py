"""Tests for the annotate-live template
(fused_render/templates/annotate-live/).

Two things under test:

1. Recorder PARITY — annotate_live.py is a byte-for-byte copy of annotate.py's
   record/tombstone contract (only the docstring differs), so the sidecar log
   must behave identically: comments upsert by `id`, a dropped comment is never
   deleted (last-seen state persists), only `deleted_ids` tombstones (same
   atomic write, permanent), and unowned keys survive the read-merge-write.
   Same load-via-importlib pattern as test_annotate_comments.py — the module is
   a stdlib runPython target, not a package import.

2. Template REGISTRY placement + shape — annotate-live is bound immediately
   after every `annotate` (never first, never a file type's default), ships its
   own template.html + annotate_live.py + icon.svg, injects no runtime script
   tag, and carries the live layer (LiveMap sync, LWW merge, echo guard,
   transport=off fallback).
"""
import importlib.util
import json
import os

from fused_render import server


TEMPLATE_DIR = os.path.join("fused_render", "templates", "annotate-live")


def _load(name, rel):
    path = os.path.join(TEMPLATE_DIR, rel)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_annotate_live():
    return _load("annotate_live_target", "annotate_live.py")


def _sidecar(tmp_path):
    return tmp_path / "sample.html.json"


def _target(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    return f


# ======================================================= recorder parity

def test_record_creates_comments_key(tmp_path):
    ann = _load_annotate_live()
    f = _target(tmp_path)
    resp = ann._record(str(f), [
        {"id": "c1", "content": "hi", "author": "ada",
         "createdAt": 1720000000000, "updatedAt": 1720000000000, "view": "_render"},
    ], [])
    assert resp == {"recorded": True, "count": 1, "deleted": 0}

    data = json.loads(_sidecar(tmp_path).read_text())
    assert data["claudeSessions"] == []  # backfilled so a claude turn round-trips
    log = data["comments"]
    assert len(log) == 1
    e = log[0]
    assert e["id"] == "c1"
    assert e["content"] == "hi"
    assert e["author"] == "ada"  # live-layer field rides through untouched
    assert e["createdAt"] == 1720000000000  # comment's own ms epoch, untouched
    assert e["recorded_at"] == e["updated_at"]  # server seconds, equal on first write


def test_second_record_same_id_updates_in_place(tmp_path):
    ann = _load_annotate_live()
    f = _target(tmp_path)
    ann._record(str(f), [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    first = json.loads(_sidecar(tmp_path).read_text())["comments"][0]

    ann._record(str(f), [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    log = json.loads(_sidecar(tmp_path).read_text())["comments"]
    assert len(log) == 1  # not duplicated
    e = log[0]
    assert e["recorded_at"] == first["recorded_at"]  # first-seen time is stable
    assert e["updated_at"] >= first["updated_at"]     # bumped on every record


def test_dropped_comment_stays_in_sidecar(tmp_path):
    ann = _load_annotate_live()
    f = _target(tmp_path)
    ann._record(str(f), [
        {"id": "A", "content": "keep", "createdAt": 1},
        {"id": "B", "content": "gone-from-url", "createdAt": 2},
    ], [])
    ann._record(str(f), [{"id": "A", "content": "keep", "createdAt": 1}], [])

    log = json.loads(_sidecar(tmp_path).read_text())["comments"]
    ids = sorted(e["id"] for e in log)
    assert ids == ["A", "B"]
    b = next(e for e in log if e["id"] == "B")
    assert b["content"] == "gone-from-url"  # untouched, no deleted_at
    assert "deleted_at" not in b


def test_preserves_unowned_keys(tmp_path):
    ann = _load_annotate_live()
    f = _target(tmp_path)
    sess = [{"id": "s1", "preview": "hi", "created_at": 1, "last_used": 1, "cwd": "/x"}]
    hist = [{"id": "bk-1", "search": "a=1", "recorded_at": 1.0, "updated_at": 1.0}]
    _sidecar(tmp_path).write_text(json.dumps({
        "claudeSessions": sess,
        "bookmarkHistory": hist,
        "lastSession": "s1",
    }))

    ann._record(str(f), [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    data = json.loads(_sidecar(tmp_path).read_text())
    assert data["claudeSessions"] == sess
    assert data["bookmarkHistory"] == hist
    assert data["lastSession"] == "s1"
    assert len(data["comments"]) == 1


def test_empty_array_is_no_op(tmp_path):
    ann = _load_annotate_live()
    f = _target(tmp_path)
    resp = ann._record(str(f), [], [])
    assert resp == {"recorded": True, "count": 0, "deleted": 0}
    assert not _sidecar(tmp_path).exists()  # nothing to record never touches disk


def test_deleted_ids_tombstone_in_same_write(tmp_path):
    ann = _load_annotate_live()
    f = _target(tmp_path)
    ann._record(str(f), [
        {"id": "A", "content": "keep", "createdAt": 1},
        {"id": "B", "content": "bye", "createdAt": 2},
    ], [])
    resp = ann._record(str(f), [{"id": "A", "content": "keep", "createdAt": 1}], ["B"])
    assert resp == {"recorded": True, "count": 1, "deleted": 1}

    log = json.loads(_sidecar(tmp_path).read_text())["comments"]
    b = next(e for e in log if e["id"] == "B")
    assert b["deleted_at"] == b["updated_at"]  # stamped, seconds
    a = next(e for e in log if e["id"] == "A")
    assert "deleted_at" not in a


def test_rerecording_keeps_tombstone(tmp_path):
    ann = _load_annotate_live()
    f = _target(tmp_path)
    ann._record(str(f), [{"id": "A", "content": "hi", "createdAt": 1}], [])
    ann._record(str(f), [], ["A"])
    ann._record(str(f), [{"id": "A", "content": "hi again", "createdAt": 1}], [])

    log = json.loads(_sidecar(tmp_path).read_text())["comments"]
    assert log[0]["deleted_at"]  # permanent — survives the re-record
    assert log[0]["content"] == "hi again"


def test_main_dispatch_and_status(tmp_path):
    ann = _load_annotate_live()
    f = _target(tmp_path)
    assert ann.main(action="record", file=str(f),
                    comments=[{"id": "c1", "content": "x", "createdAt": 1}]) == \
        {"recorded": True, "count": 1, "deleted": 0}
    assert "error" in ann.main(action="record", file="")
    assert "error" in ann.main(action="bogus", file=str(f))
    assert ann.main(action="status", file=str(f)) == {"writable": True}


def test_record_output_matches_annotate_byte_for_byte(tmp_path):
    """The whole point of the copy: annotate_live.py and annotate.py must write
    identical sidecar content for the same input (server timestamps aside)."""
    live = _load_annotate_live()
    base = _load("annotate_base_target",
                 os.path.join("..", "annotate", "annotate.py"))
    payload = [{"id": "c1", "content": "hi", "author": "x", "createdAt": 5,
                "updatedAt": 7, "resolved": True}]

    fa = tmp_path / "a.html"; fa.write_text("<html></html>")
    fb = tmp_path / "b.html"; fb.write_text("<html></html>")
    live._record(str(fa), payload, ["gone"])
    base._record(str(fb), payload, ["gone"])

    def _strip(d):
        for c in d.get("comments", []):
            for k in ("recorded_at", "updated_at", "deleted_at"):
                c.pop(k, None)
        return d
    a = _strip(json.loads((tmp_path / "a.html.json").read_text()))
    b = _strip(json.loads((tmp_path / "b.html.json").read_text()))
    assert a == b


# ======================================================= registry placement

def test_annotate_live_bound_right_after_annotate_in_html():
    entries, error = server._templates_for("/x/page.html", False)
    assert error is None
    modes = [e["mode"] for e in entries]
    assert modes == ["_render", "code", "claude", "annotate", "annotate-live", "history"]
    # never the default: the file type still opens on its first (sentinel) mode.
    assert modes[0] != "annotate-live"


def test_annotate_live_bound_after_annotate_in_parquet():
    entries, error = server._templates_for("/x/data.parquet", False)
    assert error is None
    modes = [e["mode"] for e in entries]
    assert modes == ["duckdb", "structure", "h3", "claude", "annotate", "annotate-live", "history"]


def test_registry_annotate_live_always_follows_annotate_never_first():
    with open(server.BUILTIN_REGISTRY, encoding="utf-8") as f:
        reg = json.load(f)
    seen = False
    for key, names in reg.items():
        assert names[0] != "annotate-live", key  # never a default
        for i, name in enumerate(names):
            if name == "annotate-live":
                seen = True
                assert names[i - 1] == "annotate", key  # immediately after annotate
        # and every annotate is paired with a following annotate-live
        for i, name in enumerate(names):
            if name == "annotate":
                assert names[i + 1] == "annotate-live", key
    assert seen  # the binding actually landed


def test_annotate_live_resolves_with_icon():
    path, err = server._resolve_name("annotate-live")
    assert path is not None, err
    assert path.endswith(os.path.join("annotate-live", "template.html"))
    assert server._icon_for(path) is not None  # ships icon.svg


# ======================================================= template shape

def _template_html():
    with open(os.path.join(TEMPLATE_DIR, "template.html"), encoding="utf-8") as f:
        return f.read()


def test_ships_all_three_files():
    for name in ("template.html", "annotate_live.py", "icon.svg"):
        assert os.path.isfile(os.path.join(TEMPLATE_DIR, name)), name


def test_no_runtime_script_tag():
    # The runtime is injected by the server, never script-tagged in a template.
    html = _template_html()
    assert "runtime.js\"" not in html and "src=\"/runtime" not in html


def test_runpython_targets_the_copy_not_annotate():
    html = _template_html()
    assert "./annotate_live.py" in html
    assert "\"./annotate.py\"" not in html  # never calls the sibling's target


def test_live_layer_present_and_wired():
    html = _template_html()
    # dynamic CDN import (only when enabled) + the shared public key
    assert "cdn.jsdelivr.net/npm/@liveblocks/client" in html
    assert "pk_prod_uztICbT3vBg7TSMBX_u-57MkCG87KtkiB6xJyBcz-vX2XhEnNKxVcNhzYUIeHSAa" in html
    # id-keyed LiveMap shared truth + roster read
    assert "new mod.LiveMap()" in html
    assert "getOthers()" in html
    # merge rule: tombstone wins, else higher updatedAt; echo guard + off switch
    assert "deleted === true" in html
    assert "updatedAt" in html
    assert "lastSynced" in html
    assert 'get("transport") !== "off"' in html
    # identity persisted under the spec's localStorage key
    assert "fused.annotateLive.author" in html


def test_claude_hook_button_present():
    html = _template_html()
    assert 'data-a="claude"' in html
    assert 'c.assigned = "claude"' in html
    assert "assigned to Claude" in html
