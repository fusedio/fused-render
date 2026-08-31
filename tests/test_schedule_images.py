"""Files attached to scheduled tasks (schedule.shots_dir + /api/schedule/shot).

The shape: the New task form uploads each file once (POST /api/schedule/shot,
MULTIPART, bytes under ~/.fused-render/task-shots), schedules with the returned
PATHS, and the fired run gets the paths in its message plus a pre-allowed Read
of the dir (claude_spawn extra_read_dirs -> agent._start). FUSED_RENDER_HOME is
redirected to a tmp dir so nothing touches the real home.

ANY FILE, NO CAPS (D618): the count cap, the byte cap and the image-only MIME
gate are gone. What is left of the type question is `kind` — and the transcode
that can CHANGE it, for a picture no browser draws.
"""
import base64
import io
import json
import os
import re

import pytest

from fastapi.testclient import TestClient

from fused_render import claude_spawn, schedule, tasks_store
from fused_render.server import create_app
from fused_render.server import image_convert


FUSED = {"X-Fused": "1"}  # D3 guard header required on writes

#: A real 1x1 PNG, so the endpoint's happy path stores actual image bytes.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBg"
    "AAAABQABh6FO1AAAAABJRU5ErkJggg=="
)


def _client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    app = create_app(start_dir=str(tmp_path))
    return TestClient(app), home


def _post(client, raw: bytes = PNG, name: str = "a.png",
          mime: str = "image/png", **kw):
    """One multipart upload. `mime=None` sends no content type at all."""
    files = {"file": (name, io.BytesIO(raw), mime) if mime
             else (name, io.BytesIO(raw))}
    return client.post("/api/schedule/shot", files=files, **kw)


# ---- the upload endpoint -------------------------------------------------------


def test_upload_requires_the_fused_header(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    assert _post(client).status_code == 403


def test_upload_refuses_a_body_that_is_not_multipart(tmp_path, monkeypatch):
    """And the GUARD still answers first, which is why `file` is optional.

    A required `File(...)` would make a JSON body a 422 raised before any of our
    code runs — an unguarded reply to a cross-origin POST.
    """
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.post("/api/schedule/shot", json={"data": "data:image/png;base64,x"},
                       headers=FUSED)
    assert resp.status_code == 400
    assert "multipart" in resp.json()["error"]
    assert client.post("/api/schedule/shot", json={}).status_code == 403


def test_upload_refuses_an_empty_file(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = _post(client, raw=b"", headers=FUSED)
    assert resp.status_code == 400
    assert "empty" in resp.json()["error"]


def test_upload_stores_under_task_shots_and_returns_the_path(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    resp = _post(client, headers=FUSED)
    assert resp.status_code == 200
    body = resp.json()
    path = body["path"]
    assert path.startswith(os.path.join(str(home), "task-shots").replace("\\", "/"))
    assert path.endswith(".png")
    assert body["kind"] == "image"
    assert (body["width"], body["height"]) == (1, 1)
    with open(path, "rb") as fh:
        assert fh.read() == PNG


def test_upload_takes_ANY_file_type_and_ANY_size(tmp_path, monkeypatch):
    """The image-only MIME gate and the 4 MB cap are both gone (D618)."""
    client, _ = _client(tmp_path, monkeypatch)
    big = b"col\n" + b"x,y\n" * 1_100_000        # ~4.4 MB of csv: past the old cap
    body = _post(client, raw=big, name="rows.csv", mime="text/csv",
                 headers=FUSED).json()
    assert body["kind"] == "file"
    assert body["path"].endswith(".csv")
    assert "width" not in body
    with open(body["path"], "rb") as fh:
        assert fh.read() == big          # byte for byte, whole
    # An extension nobody can work out is NO extension, not a guessed one: the
    # path still stores and still reads, it just has no template of its own.
    for name, mime, ext in (("dump.sql", "application/sql", ".sql"),
                            ("notes", "", ""),
                            ("a.parquet", None, ".parquet"),
                            ("x.tar.gz", "application/gzip", ".gz")):
        got = _post(client, raw=b"bytes", name=name, mime=mime, headers=FUSED).json()
        assert got["path"].endswith(ext), (name, got)
        assert got["kind"] == "file"


def test_upload_mints_the_name_and_only_borrows_a_SANITISED_extension(tmp_path, monkeypatch):
    """The filename is the one field a page chooses freely, so none of it is
    used except the extension — lowercased, alphanumerics only, length-capped."""
    client, _ = _client(tmp_path, monkeypatch)
    for name, ext in (("SHOT.PNG", ".png"), ("../../etc/passwd", ""),
                      ("weird.C S/V", ""), ("no-dot", "")):
        # No content type, so `ext_for` has only the filename to go on.
        body = _post(client, raw=b"bytes", name=name, mime=None, headers=FUSED).json()
        got = os.path.basename(body["path"])
        assert got.endswith(ext), (name, got)
        assert "passwd" not in got and ".." not in got and " " not in got


def test_upload_transcodes_a_picture_no_browser_can_draw(tmp_path, monkeypatch):
    """A TIFF goes up and a PNG comes back — at the CONVERTED path, so the chip
    that draws `path` draws pixels rather than an empty box (D614's answer, in
    the shared module now)."""
    pytest.importorskip("PIL")
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2000, 1000), (10, 20, 30)).save(buf, format="TIFF")
    client, _ = _client(tmp_path, monkeypatch)
    body = _post(client, raw=buf.getvalue(), name="scan.tif", mime="image/tiff",
                 headers=FUSED).json()
    assert body["kind"] == "image"
    assert body["path"].endswith("-view.png")
    # Capped at the shared edge, and the ORIGINAL is still there beside it.
    assert (body["width"], body["height"]) == (image_convert.PNG_EDGE, 800)
    assert os.path.isfile(body["path"])
    assert os.path.isfile(body["path"].replace("-view.png", ".tif"))


def test_an_oversize_but_drawable_picture_is_downscaled_server_side(tmp_path, monkeypatch):
    """4 MB is a DOWNSCALE TRIGGER, never a refusal (D615's rule, kept).

    The budget is monkeypatched rather than met with a real 4 MB image: the
    branch under test is "over the number", not the number.
    """
    pytest.importorskip("PIL")
    from PIL import Image
    monkeypatch.setattr(image_convert, "PNG_MAX_BYTES", 64)
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (200, 100, 50)).save(buf, format="PNG")
    client, _ = _client(tmp_path, monkeypatch)
    body = _post(client, raw=buf.getvalue(), name="big.png", headers=FUSED).json()
    assert body["kind"] == "image"
    # A DIFFERENT name, so a .png being re-encoded is not asked to overwrite
    # itself; the JPEG ladder is what a PNG over the budget falls to.
    assert body["path"].endswith("-view.jpg")


def test_a_failed_transcode_costs_the_conversion_and_never_the_file(tmp_path, monkeypatch):
    """Junk bytes claiming to be a TIFF: the attachment is still stored and
    still returned, because losing it to report a problem with it would be the
    worse bug."""
    client, _ = _client(tmp_path, monkeypatch)
    body = _post(client, raw=b"not a tiff at all", name="x.tif",
                 mime="image/tiff", headers=FUSED).json()
    assert body["kind"] == "image"
    assert body["path"].endswith(".tif")
    with open(body["path"], "rb") as fh:
        assert fh.read() == b"not a tiff at all"


def test_the_shared_module_never_raises(tmp_path):
    bad = tmp_path / "junk.tif"
    bad.write_bytes(b"\x00\x01")
    assert "error" in image_convert.transcode(str(bad), str(tmp_path / "out"))
    assert "error" in image_convert.transcode(str(tmp_path / "nope.tif"),
                                              str(tmp_path / "out"))
    assert image_convert.dimensions(str(bad)) is None


# ---- scheduling with images ----------------------------------------------------


def _upload(client) -> str:
    return _post(client, headers=FUSED).json()["path"]


def test_create_stores_validated_paths_and_serves_them_back(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    path = _upload(client)
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "look at this",
        "delay_seconds": 3600, "images": [path]})
    assert resp.status_code == 200
    entry = resp.json()["entry"]
    assert entry["images"] == [path]
    listed = client.get("/api/schedule").json()["entries"]
    assert [e for e in listed if e["id"] == entry["id"]][0]["images"] == [path]


def test_create_refuses_paths_outside_the_task_shots_dir(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    outside = tmp_path / "not-a-shot.png"
    outside.write_bytes(PNG)
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m",
        "delay_seconds": 3600, "images": [str(outside)]})
    assert resp.status_code == 400
    assert "not a task attachment" in resp.json()["error"]


def test_create_refuses_a_symlink_smuggled_into_the_dir(tmp_path, monkeypatch):
    # Realpath membership, not string prefix: a link under the dir pointing out
    # of it must not turn `images` into a way to read arbitrary files.
    client, home = _client(tmp_path, monkeypatch)
    _upload(client)  # ensures the dir exists
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    link = os.path.join(str(home), "task-shots", "link.png")
    os.symlink(str(secret), link)
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m",
        "delay_seconds": 3600, "images": [link]})
    assert resp.status_code == 400


def test_create_does_NOT_cap_the_attachment_count(tmp_path, monkeypatch):
    """IMAGES_MAX (4) is gone (D618), asserted as the absence it is."""
    assert not hasattr(schedule, "IMAGES_MAX")
    client, _ = _client(tmp_path, monkeypatch)
    paths = [_upload(client) for _ in range(7)]
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m",
        "delay_seconds": 3600, "images": paths})
    assert resp.status_code == 200, resp.json()
    assert resp.json()["entry"]["images"] == paths


# ---- what the fired run is handed ----------------------------------------------


def test_attachments_block_is_the_chats_own_pane_shot_block(tmp_path, monkeypatch):
    """D619. THE BUG, from Akshil's own screenshot: opening a fired task's chat
    showed the user's turn ending in a raw list of `/Users/…/task-shots/…pdf`
    paths, where the SAME chat renders its own attachments as receipt rows. The
    tail is gone; the block the page writes is the block the scheduler writes.
    """
    entry = {"attachments": [
        {"path": "/x/task-shots/a1.pdf", "name": "Q3 report.pdf", "kind": "file"},
        {"path": "/x/task-shots/b2.png", "name": "chart.png", "kind": "image"}]}
    block = schedule._attachments_block(entry)
    assert block.startswith("<pane-shot>\n")
    assert block.endswith("\n</pane-shot>")
    # The tail that made the bug, asserted as the absence it now is.
    assert "read them with the Read tool" not in block
    # The payload is the LAST line inside the block, which is where `paneShotIn`
    # looks for it — everything above is prose for the model.
    payload = json.loads(block.splitlines()[-2])
    assert payload == [
        {"kind": "file", "view": "/x/task-shots/a1.pdf",
         "name": "Q3 report.pdf", "viewNote": ""},
        {"kind": "image", "view": "/x/task-shots/b2.png",
         "name": "chart.png", "viewNote": ""}]
    # ALWAYS AN ARRAY, even for one — the shape `paneShotBlock` settled on, so no
    # reader has to branch on the length of the list it is drawing.
    one = schedule._attachments_block({"attachments": [
        {"path": "/x/task-shots/only.csv", "name": "rows.csv", "kind": "file"}]})
    assert json.loads(one.splitlines()[-2]) == [
        {"kind": "file", "view": "/x/task-shots/only.csv",
         "name": "rows.csv", "viewNote": ""}]
    assert schedule._attachments_block({}) == ""
    assert schedule._attachments_block({"images": [], "attachments": []}) == ""


def test_the_blocks_noun_never_calls_a_spreadsheet_a_picture(tmp_path, monkeypatch):
    """`paneShotBlock`'s own three-way choice, mirrored: a list that is nothing
    but files must not be announced as pictures, and a mixed one has no honest
    singular noun at all."""
    def noun(kinds):
        return schedule._attachments_block({"attachments": [
            {"path": f"/x/task-shots/{i}", "name": str(i), "kind": k}
            for i, k in enumerate(kinds)]}).splitlines()[1]

    assert "attached a file to this task" in noun(["file"])
    assert "attached 2 files to this task" in noun(["file", "file"])
    assert "attached a picture to this task" in noun(["image"])
    assert "attached 2 pictures to this task" in noun(["image", "image"])
    assert "attached 2 attachments to this task" in noun(["image", "file"])


def test_the_block_claims_no_screen_and_no_pane(tmp_path, monkeypatch):
    """The two kinds a scheduled run cannot have. "pane" and "overview" are
    pictures of a screen taken at send time (`paneShotBlock` describes both),
    and there was no screen — describing them would tell the model to look for
    a badge nobody burned in."""
    block = schedule._attachments_block(
        {"attachments": [{"path": "/x/task-shots/a.png", "name": "a.png",
                          "kind": "image"}]})
    assert '"overview"' not in block
    assert '"pane"' not in block
    assert "nobody is at the screen" in block
    # …and it says WHEN, because the files were chosen days before this run.
    assert "when they scheduled it" in block


def test_the_block_rides_between_the_state_block_and_the_words(tmp_path, monkeypatch):
    """`composeOutgoing`'s order, which is not cosmetic: `tasks_store` and
    `agent.py` only peel a LEADING machinery block, so a block sitting after the
    message would be read as something the user typed and would title the row
    with itself."""
    file = tmp_path / "page.html"
    file.write_text("<p>x</p>", encoding="utf-8")
    entry = {"target": str(file), "message": "run the report",
             "attachments": [{"path": "/x/task-shots/a.png", "name": "a.png",
                              "kind": "image"}]}
    out = schedule._composed(entry)
    assert out.index("<live-app-state>") < out.index("<pane-shot>")
    assert out.index("</pane-shot>") < out.index("run the report")
    assert out.endswith("run the report")
    # And with no attachments it is byte-identical to what it always was.
    plain = dict(entry, attachments=[], images=[])
    assert schedule._composed(plain) == schedule._outgoing(plain)


def test_a_legacy_entry_with_only_paths_still_gets_a_block(tmp_path, monkeypatch):
    """Every entry stored before D619 has `images` and nothing else. The name is
    the basename and the kind is the extension's answer — worse than the
    browser's, and the only one available."""
    block = schedule._attachments_block(
        {"images": ["/x/task-shots/20260101-aaaa.png",
                    "/x/task-shots/20260101-bbbb.csv",
                    "/x/task-shots/20260101-cccc.tif"]})
    assert json.loads(block.splitlines()[-2]) == [
        {"kind": "image", "view": "/x/task-shots/20260101-aaaa.png",
         "name": "20260101-aaaa.png", "viewNote": ""},
        {"kind": "file", "view": "/x/task-shots/20260101-bbbb.csv",
         "name": "20260101-bbbb.csv", "viewNote": ""},
        # A `.tif` is a picture no browser draws, so the receipt row must not put
        # an <img> on it — the same call `DRAWABLE_EXTS` makes in the card.
        {"kind": "file", "view": "/x/task-shots/20260101-cccc.tif",
         "name": "20260101-cccc.tif", "viewNote": ""}]


def test_a_stored_entry_is_never_re_validated(tmp_path, monkeypatch):
    """A scheduled task can fire days after it was written and its files can be
    moved out from under it. `_attachments` (the REQUEST path) refuses a file
    that has gone; `_stored_attachments` must not, or one deleted attachment
    would raise inside the tick and take every other task down with it."""
    entry = {"attachments": [{"path": "/nowhere/at/all.png", "name": "gone.png",
                              "kind": "image"}]}
    assert schedule._stored_attachments(entry)[0]["path"] == "/nowhere/at/all.png"
    assert "gone.png" in schedule._attachments_block(entry)


def test_a_stored_entry_with_junk_in_the_field_falls_back(tmp_path, monkeypatch):
    """Forgiving in the same direction the page's reader is: a transcript that
    renders without one name is a small loss, a tick that throws is not."""
    entry = {"images": ["/x/task-shots/a.png"],
             "attachments": ["not an object", {"name": "no path"},
                             {"path": "/x/task-shots/a.png", "name": "",
                              "kind": "nonsense"}]}
    assert schedule._stored_attachments(entry) == [
        {"path": "/x/task-shots/a.png", "name": "a.png", "kind": "image"}]


# ---- the attachments field on the wire -----------------------------------------


def test_create_stores_the_names_and_kinds_the_browser_knew(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    path = _upload(client)
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m", "delay_seconds": 3600,
        "images": [path],
        "attachments": [{"path": path, "name": "Q3 report.pdf", "kind": "file"}]})
    assert resp.status_code == 200, resp.json()
    entry = resp.json()["entry"]
    assert entry["attachments"] == [
        {"path": path, "name": "Q3 report.pdf", "kind": "file"}]
    # …and it survives the round trip through the store, which is what an EDIT
    # (cancel + re-create) reads the chips back from.
    listed = client.get("/api/schedule").json()["entries"]
    assert [e for e in listed if e["id"] == entry["id"]][0]["attachments"] \
        == entry["attachments"]


def test_either_field_alone_is_enough(tmp_path, monkeypatch):
    """`images` is every client written before today; `attachments` is every one
    written after. Neither may be the only way to attach a file."""
    client, _ = _client(tmp_path, monkeypatch)
    path = _upload(client)
    only_images = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m", "delay_seconds": 3600,
        "images": [path]}).json()["entry"]
    assert only_images["attachments"] == [
        {"path": path, "name": os.path.basename(path), "kind": "image"}]
    only_rich = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m", "delay_seconds": 3600,
        "attachments": [{"path": path, "name": "shot.png", "kind": "image"}]}).json()["entry"]
    # `images` DERIVED, so `_send`'s Read grant and every existing reader of the
    # entry keep working with no knowledge of the new field.
    assert only_rich["images"] == [path]
    assert only_rich["attachments"] == [
        {"path": path, "name": "shot.png", "kind": "image"}]


def test_attachments_are_confined_to_the_task_shots_dir(tmp_path, monkeypatch):
    """The SAME containment `images` has (`_shot_path`, one function now): the
    new field must not become the way to point a scheduled prompt at any file."""
    client, home = _client(tmp_path, monkeypatch)
    _upload(client)
    outside = tmp_path / "secret.txt"
    outside.write_text("s", encoding="utf-8")
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m", "delay_seconds": 3600,
        "attachments": [{"path": str(outside), "name": "s.txt", "kind": "file"}]})
    assert resp.status_code == 400
    assert "not a task attachment" in resp.json()["error"]
    # …and a symlink under the dir cannot smuggle one out either.
    link = os.path.join(str(home), "task-shots", "link.txt")
    os.symlink(str(outside), link)
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m", "delay_seconds": 3600,
        "attachments": [{"path": link, "name": "l.txt", "kind": "file"}]})
    assert resp.status_code == 400


@pytest.mark.parametrize("kind", ["pane", "overview", "", None, "IMAGE", 1])
def test_only_the_two_headless_kinds_are_accepted(tmp_path, monkeypatch, kind):
    """A scheduled run has no screen, so "pane" and "overview" are not things it
    can carry — and an unknown kind would reach the block as a word no reader
    of it has a sentence for."""
    client, _ = _client(tmp_path, monkeypatch)
    path = _upload(client)
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m", "delay_seconds": 3600,
        "attachments": [{"path": path, "name": "a.png", "kind": kind}]})
    assert resp.status_code == 400
    assert "kind must be one of" in resp.json()["error"]


def test_a_name_is_displayed_and_therefore_never_read_as_a_path(tmp_path, monkeypatch):
    """The name lands in a prompt and on a chip. A client that sent a path here
    must not have it read as one, and a newline in it would break the
    one-line-per-block-payload reading `paneShotIn` does."""
    client, _ = _client(tmp_path, monkeypatch)
    path = _upload(client)

    def stored(name):
        resp = client.post("/api/schedule", headers=FUSED, json={
            "target": str(tmp_path), "message": "m", "delay_seconds": 3600,
            "attachments": [{"path": path, "name": name, "kind": "image"}]})
        assert resp.status_code == 200, resp.json()
        return resp.json()["entry"]["attachments"][0]["name"]

    assert stored("/etc/passwd") == "passwd"
    assert stored("a\nb.png") == "a b.png"
    assert stored("  spaced.png  ") == "spaced.png"
    # An empty one falls back to the stored file's own name rather than refusing:
    # a nameless chip is a small loss, a refused schedule is not.
    assert stored("") == os.path.basename(path)
    assert stored(None) == os.path.basename(path)
    # …but a paragraph is refused, because this is a filename.
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m", "delay_seconds": 3600,
        "attachments": [{"path": path, "name": "x" * 300, "kind": "image"}]})
    assert resp.status_code == 400
    assert "longer than" in resp.json()["error"]


def test_a_recurring_occurrence_carries_the_attachments_and_their_names(tmp_path, monkeypatch):
    """The attachments travel with every run for the same reason the words do —
    an occurrence IS that template's run — and so must the names, or the first
    run of a repeat shows receipt rows and the second shows timestamps."""
    client, _ = _client(tmp_path, monkeypatch)
    path = _upload(client)
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m", "repeats": "0 9 * * *",
        "attachments": [{"path": path, "name": "daily.png", "kind": "image"}]})
    assert resp.status_code == 200, resp.json()
    entries = client.get("/api/schedule").json()["entries"]
    occurrence = [e for e in entries if e.get("template_id")]
    assert occurrence
    # both fields, since `_send`'s Read grant reads `images`
    assert occurrence[0]["images"] == [path]
    assert occurrence[0]["attachments"] == [
        {"path": path, "name": "daily.png", "kind": "image"}]


# ---- the duplicated wire constants --------------------------------------------


def _template_html() -> str:
    return open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "fused_render", "templates", "claude",
        "template.html"), encoding="utf-8").read()


def test_the_tag_matches_the_page_that_reads_it():
    """D146 / PY-15. `schedule.py` may not import a template and a template may
    not import `fused_render`, so the tag is spelled twice. This is the test the
    comment is not: rename `PANE_SHOT_TAG` in the page and the scheduler starts
    writing a block nothing renders — silently, because every reader answers an
    unparseable block with an empty list rather than a throw."""
    page = _template_html()
    written = [line.strip() for line in page.splitlines()
               if line.strip().startswith('const PANE_SHOT_TAG = "')]
    assert len(written) == 1, "one writer of the tag in the page, or this is stale"
    tag = written[0].split('"')[1]
    assert schedule._PANE_SHOT_TAG == tag
    # And the tag is in the strip lists, which is what keeps the block out of a
    # row title (`sessionTitle`, tasks_store, agent.py).
    assert tag in tasks_store._MACHINERY_STRIP


def test_the_kind_guess_matches_the_cards_own_drawable_list():
    """`_derived_attachment` guesses a legacy path's kind from its extension, and
    the New task card guesses a restored chip's the same way. The two answers
    disagreeing means a chip drawn with a thumbnail whose receipt row shows a 📄
    (or worse, the reverse: an <img> pointed at a `.csv`)."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "frontend", "src", "shell",
        "NewJobModal.tsx"), encoding="utf-8").read()
    head = src.index("const DRAWABLE_EXTS")
    body = src[head:src.index("]);", head)]
    exts = set(re.findall(r'"(\.[a-z0-9]+)"', body))
    assert exts, "the card's list moved — this test is reading nothing"
    assert exts == set(schedule._DRAWABLE_EXTS)


def test_spawn_helper_ships_extra_read_dirs_to_the_agent(monkeypatch):
    seen = {}

    class _Res:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(cmd, *, input, **kw):
        seen.update(json.loads(input))
        return _Res()

    monkeypatch.setattr(claude_spawn.subprocess, "run", fake_run)
    claude_spawn.spawn_helper("/tmp/t", "hi", "auto",
                              extra_read_dirs=["/x/task-shots"])
    assert seen["extra_read_dirs"] == ["/x/task-shots"]
    claude_spawn.spawn_helper("/tmp/t", "hi", "auto")
    assert seen["extra_read_dirs"] == []


def test_a_send_without_images_keeps_the_old_call_shape(tmp_path, monkeypatch):
    """No images, no `extra_read_dirs` kwarg — not even as None.

    Regression: passing it unconditionally broke every test double in the repo
    (`fake_spawn(target, prompt, permission_mode, session_id="")`), and because
    `_send` catches a bad spawn into `_fail`, the symptom was a `failed` event
    on a task that had nothing wrong with it. A run with nothing to read there
    should also not carry a directory rule.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    seen = {}

    def spy(target, prompt, permission_mode, session_id="", **kw):
        seen["kw"] = kw
        return {"run_id": "r-1"}

    monkeypatch.setattr(schedule.claude_spawn, "spawn_helper", spy)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    monkeypatch.setattr(schedule, "_report", lambda *a, **k: None)
    monkeypatch.setattr(schedule, "_watching", lambda *a, **k: None)
    monkeypatch.setattr(schedule, "_update", lambda *a, **k: None)
    schedule._send({"id": "x", "target": str(tmp_path), "message": "plain",
                    "session_id": "", "permission_mode": "auto"})
    assert "extra_read_dirs" not in seen["kw"]


def test_a_send_with_images_pre_allows_the_task_shots_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    seen = {}

    def spy(target, prompt, permission_mode, session_id="", **kw):
        seen["kw"] = kw
        seen["prompt"] = prompt
        return {"run_id": "r-1"}

    monkeypatch.setattr(schedule.claude_spawn, "spawn_helper", spy)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    monkeypatch.setattr(schedule, "_report", lambda *a, **k: None)
    monkeypatch.setattr(schedule, "_watching", lambda *a, **k: None)
    monkeypatch.setattr(schedule, "_update", lambda *a, **k: None)
    shot = os.path.join(schedule.shots_dir(), "a.png")
    schedule._send({"id": "x", "target": str(tmp_path), "message": "look",
                    "session_id": "", "permission_mode": "auto",
                    "images": [shot]})
    assert seen["kw"]["extra_read_dirs"] == [schedule.shots_dir()]
    # The wire spells the path with forward slashes on every platform (the
    # template's reader and the Read rule both do), so a Windows join is
    # compared in that spelling too.
    assert shot.replace("\\", "/") in seen["prompt"]


@pytest.mark.skipif(os.name == "nt",
                    reason="os.symlink needs elevation on Windows")
def test_the_pre_allowed_dir_and_the_stored_paths_have_ONE_spelling(tmp_path, monkeypatch):
    """The Read rule matches TEXT, so both sides must resolve identically.

    A symlink anywhere on the path — a symlinked home, macOS' own
    /tmp -> /private/tmp — used to leave `shots_dir()` unresolved while
    `_images` stored realpaths, and the headless run was handed paths its rule
    did not cover (Bugbot, PR #865).
    """
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "linked-home"
    os.symlink(str(real), str(link))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(link))
    assert schedule.shots_dir() == os.path.realpath(schedule.shots_dir())

    client = TestClient(create_app(start_dir=str(tmp_path)))
    path = _post(client, headers=FUSED).json()["path"]
    # The upload's own answer, the validator's, and the pre-allowed dir all
    # agree — which is the whole property.
    assert path.startswith(schedule.shots_dir())
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m",
        "delay_seconds": 3600, "images": [path]})
    assert resp.status_code == 200, resp.json()
    assert resp.json()["entry"]["images"] == [path]


def test_agent_start_turns_extra_dirs_into_read_rules():
    # A source pin, because _start launches a real process: the helper's
    # request field must land in the run's --allowed-tools as a Read rule,
    # exactly the SHOTS mechanism (agent._read_rule).
    src = open(os.path.join(os.path.dirname(__file__), "..", "fused_render",
                            "templates", "claude", "agent.py"),
                encoding="utf-8").read()
    assert "extra_read_dirs: list | None = None" in src
    assert "[_read_rule(d) for d in (extra_read_dirs or [])]" in src
