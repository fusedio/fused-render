"""geotiff streaming (D1484): the TIFF decoder reading an `s3://` object by HTTP
ranges instead of an mmap of a downloaded copy.

* `_tiff_core` reads any SLICEABLE buffer (`_unpack_at`, `len(buf)`), and
  parses BigTIFF as well as classic TIFF — the big rasters in the bucket are
  BigTIFF, and the parity tests decode the same pixels from both layouts,
  written by the tiny writer below (no tifffile/GDAL in the test env).
* `remote_range.RemoteBuffer` (shared/) against a local HTTP server that speaks Range,
  ETag and If-Match: bytes-slice semantics, the header block cache, coalesced
  `read_many`, a changed object (412) and a re-sign after a 403.
* the decoder end to end on a streamed address, and the stream budget that
  refuses a read which would really be a download.
* which templates stream a bucket object (`stream-s3` marker) and the stat
  field the explorer reads.
"""
import os
import random
import struct
import sys
import zlib

import numpy as np
import pytest

import fused_render

GEOTIFF = os.path.join(os.path.dirname(fused_render.__file__), "templates", "geotiff")
if GEOTIFF not in sys.path:
    sys.path.insert(0, GEOTIFF)

import _tiff_core as T  # noqa: E402 - path seeded above, as the daemon does

RF = T.remote_range()  # shared/remote_range.py, loaded the way the template loads it


# --------------------------------------------------------------- TIFF writer


def _tiff(images, *, big, tiled, compress=True, chunk=16):
    """A little-endian TIFF/BigTIFF of `images` (2-D uint16 arrays; the first
    is the full image, the rest reduced-resolution IFDs), tiled `chunk`×`chunk`
    or in `chunk`-row strips, deflate or uncompressed."""
    off_t = 16 if big else 4          # LONG8 / LONG for offsets and counts
    entry = 20 if big else 12
    inline = 8 if big else 4
    out = bytearray(16 if big else 8)
    blobs = []
    for a in images:
        H, W = a.shape
        chunks = []
        if tiled:
            for ty in range(0, H, chunk):
                for tx in range(0, W, chunk):
                    t = np.zeros((chunk, chunk), "<u2")
                    part = a[ty:ty + chunk, tx:tx + chunk]
                    t[:part.shape[0], :part.shape[1]] = part
                    chunks.append(t.tobytes())
        else:
            for y in range(0, H, chunk):
                chunks.append(np.ascontiguousarray(a[y:y + chunk], "<u2").tobytes())
        offs, counts = [], []
        for c in chunks:
            c = zlib.compress(c) if compress else c
            offs.append(len(out))
            counts.append(len(c))
            out += c
        blobs.append((W, H, offs, counts))

    fmt = {3: "H", 4: "I", 16: "Q"}
    size = {3: 2, 4: 4, 16: 8}
    ifd_offsets = []
    for i, (W, H, offs, counts) in enumerate(blobs):
        tags = [(254, 4, [1 if i else 0]), (256, 4, [W]), (257, 4, [H]), (258, 3, [16]),
                (259, 3, [8 if compress else 1]), (262, 3, [1]), (277, 3, [1]),
                (284, 3, [1]), (339, 3, [1])]
        if tiled:
            tags += [(322, 3, [chunk]), (323, 3, [chunk]),
                     (324, off_t, offs), (325, off_t, counts)]
        else:
            tags += [(273, off_t, offs), (278, 4, [chunk]), (279, off_t, counts)]
        tags.sort()
        ifd_off = len(out)
        ifd_offsets.append(ifd_off)
        n = len(tags)
        head = 8 if big else 2
        extra_at = ifd_off + head + n * entry + (8 if big else 4)
        ifd = bytearray(struct.pack("<Q" if big else "<H", n))
        extra = bytearray()
        for tag, typ, vals in tags:
            raw = struct.pack("<" + fmt[typ] * len(vals), *vals)
            ifd += struct.pack("<HHQ" if big else "<HHI", tag, typ, len(vals))
            if len(raw) <= inline:
                ifd += raw.ljust(inline, b"\0")
            else:
                ifd += struct.pack("<Q" if big else "<I", extra_at + len(extra))
                extra += raw
                if len(extra) % 2:
                    extra += b"\0"
        next_at = len(ifd)
        ifd += b"\0" * (8 if big else 4)  # next-IFD pointer, patched below
        out += ifd + extra
        ifd_offsets[-1] = (ifd_off, ifd_off + next_at)
    for (_, ptr), (nxt, _) in zip(ifd_offsets, ifd_offsets[1:]):
        out[ptr:ptr + (8 if big else 4)] = struct.pack("<Q" if big else "<I", nxt)
    first = ifd_offsets[0][0]
    if big:
        out[0:16] = b"II" + struct.pack("<HHHQ", 43, 8, 0, first)
    else:
        out[0:8] = b"II" + struct.pack("<HI", 42, first)
    return bytes(out)


def _images(w=70, h=45):
    rng = np.random.default_rng(7)
    full = rng.integers(0, 60000, size=(h, w), dtype=np.uint16)
    return [full, full[::2, ::2].copy(), full[::4, ::4].copy()]


@pytest.mark.parametrize("tiled", [True, False])
@pytest.mark.parametrize("compress", [True, False])
def test_bigtiff_decodes_the_same_pixels_as_classic(tmp_path, tiled, compress):
    imgs = _images()
    paths = {}
    for big in (False, True):
        p = tmp_path / f"{'big' if big else 'classic'}.tif"
        p.write_bytes(_tiff(imgs, big=big, tiled=tiled, compress=compress))
        paths[big] = str(p)
    for big, path in paths.items():
        bands, meta, _ = T.read_tiff(path)
        assert np.array_equal(bands[0], imgs[0]), big
        assert meta["tiff"]["overviews"] == [[35, 23], [18, 12]]
        # an overview read picks the reduced IFD, same as classic
        small, _, _ = T.read_tiff(path, "", 200)
        assert np.array_equal(small[0], imgs[2])
    _, en, _, _ = T.parse_header(paths[True])
    assert en == "<" and en.big
    _, en, _, _ = T.parse_header(paths[False])
    assert en == "<" and not en.big


def test_a_corrupt_bigtiff_ifd_count_is_refused(tmp_path):
    data = bytearray(_tiff(_images(), big=True, tiled=True))
    first = struct.unpack_from("<Q", data, 8)[0]
    data[first:first + 8] = struct.pack("<Q", 10 ** 9)
    p = tmp_path / "bad.tif"
    p.write_bytes(bytes(data))
    with pytest.raises(T.Unsupported, match="corrupt TIFF header"):
        T.parse_header(str(p))


def test_unpack_at_matches_unpack_from_and_fails_the_same_way():
    buf = bytes(range(32))
    assert T._unpack_at("<HI", buf, 3) == struct.unpack_from("<HI", buf, 3)
    with pytest.raises(struct.error):
        T._unpack_at("<Q", buf, 28)


# ------------------------------------------------------ a local range server

from _range_bucket import bucket  # noqa: E402,F401 - the fixture (tests/_range_bucket.py)


def test_remote_buffer_slices_like_bytes(bucket):
    data = os.urandom(300_000)
    bucket.objects["/obj.bin"] = data
    buf = RF.RemoteBuffer("s3://fused-asset/obj.bin")
    assert len(buf) == len(data)
    rng = random.Random(3)
    for _ in range(60):
        a = rng.randrange(-len(data) - 5, len(data) + 5)
        b = rng.randrange(-len(data) - 5, len(data) + 5)
        assert buf[a:b] == data[a:b], (a, b)
    assert buf[5] == data[5] and buf[-1] == data[-1]
    assert buf[:0] == b"" and buf[len(data):] == b""
    with pytest.raises(IndexError):
        buf[len(data)]
    with pytest.raises(ValueError):
        buf[::2]


def test_header_reads_come_from_the_first_block(bucket):
    bucket.objects["/obj.bin"] = os.urandom(500_000)
    buf = RF.RemoteBuffer("s3://fused-asset/obj.bin")
    assert len(bucket.log) == 1  # one request learned size, ETag and the head
    for off in range(0, RF.HEAD_BYTES - 16, 997):
        buf[off:off + 12]
    assert len(bucket.log) == 1
    buf[RF.HEAD_BYTES + 10:RF.HEAD_BYTES + 20]  # a new block: one request, then cached
    buf[RF.HEAD_BYTES + 30:RF.HEAD_BYTES + 40]
    assert len(bucket.log) == 2
    # later requests are pinned to the version first read
    assert all(im for _, _, im in bucket.log[1:])


def test_read_many_coalesces_adjacent_ranges(bucket):
    data = os.urandom(2_000_000)
    bucket.objects["/obj.bin"] = data
    buf = RF.RemoteBuffer("s3://fused-asset/obj.bin")
    before = len(bucket.log)
    # Two runs of adjacent "tiles", far apart: two requests, not twenty.
    ranges = [(400_000 + i * 20_000, 400_000 + (i + 1) * 20_000) for i in range(10)]
    ranges += [(1_500_000 + i * 20_000, 1_500_000 + (i + 1) * 20_000) for i in range(10)]
    random.Random(1).shuffle(ranges)
    got = buf.read_many(ranges)
    assert got == [data[a:b] for a, b in ranges]
    assert len(bucket.log) - before == 2


def test_a_changed_object_fails_instead_of_mixing_versions(bucket):
    bucket.objects["/obj.bin"] = os.urandom(300_000)
    buf = RF.RemoteBuffer("s3://fused-asset/obj.bin")
    bucket.objects["/obj.bin"] = os.urandom(300_000)
    with pytest.raises(RF.RemoteChanged):
        buf[200_000:200_010]
    assert buf.changed


def test_a_403_re_signs_once(bucket):
    bucket.objects["/obj.bin"] = os.urandom(300_000)
    buf = RF.RemoteBuffer("s3://fused-asset/obj.bin")
    assert len(bucket.signs) == 1
    bucket.deny_next = 1
    assert buf[200_000:200_004] == bucket.objects["/obj.bin"][200_000:200_004]
    assert len(bucket.signs) == 2
    bucket.deny_next = 2
    with pytest.raises(RF.RemoteError):
        buf[290_000:290_004]  # a block not yet cached


def test_a_server_ignoring_range_is_not_a_download(bucket):
    bucket.objects["/obj.bin"] = os.urandom(300_000)
    buf = RF.RemoteBuffer("s3://fused-asset/obj.bin")
    bucket.ignore_range = True
    with pytest.raises(RF.RemoteError, match="ignored the byte range"):
        buf[200_000:200_010]


def test_expired_urls_are_re_signed(bucket, monkeypatch):
    bucket.objects["/obj.bin"] = os.urandom(300_000)
    stamps = iter(["2000-01-01T00:00:00Z", None, None])

    def sign(address):
        bucket.signs.append(address)
        return {"url": bucket.url("obj.bin"), "expires_at": next(stamps)}

    monkeypatch.setattr(RF.fused_s3, "sign", sign)
    buf = RF.RemoteBuffer("s3://fused-asset/obj.bin")
    buf[200_000:200_004]
    assert len(bucket.signs) == 2  # the first URL had already expired


def test_only_s3_addresses():
    with pytest.raises(ValueError):
        RF.RemoteBuffer("/tmp/x.tif")


# ------------------------------------------------- the decoder on a stream


def test_read_tiff_streams_the_same_pixels(bucket, tmp_path, monkeypatch):
    imgs = _images(200, 150)
    data = _tiff(imgs, big=True, tiled=True)
    bucket.objects["/r.tif"] = data
    local = tmp_path / "r.tif"
    local.write_bytes(data)
    monkeypatch.setattr(T, "_REMOTE", {})
    remote, rmeta, _ = T.read_tiff("s3://fused-asset/r.tif")
    plain, lmeta, _ = T.read_tiff(str(local))
    assert np.array_equal(remote, plain)
    assert rmeta["file_size"] == lmeta["file_size"] == len(data)
    # a window reads only its tiles, in one coalesced request
    T._REMOTE.clear()
    n = len(bucket.log)
    win, _, wsel = T.read_tiff("s3://fused-asset/r.tif", "0.5,0.5,0.7,0.7")
    assert wsel
    assert len(bucket.log) - n <= 3  # head block, IFD tail block, the window's tiles


def test_a_read_over_the_budget_is_refused_before_fetching(bucket, monkeypatch):
    bucket.objects["/big.tif"] = _tiff(_images(200, 150), big=False, tiled=False,
                                       compress=False)
    monkeypatch.setattr(T, "_REMOTE", {})
    monkeypatch.setattr(T, "STREAM_BUDGET", 10_000)
    n = len(bucket.log)
    with pytest.raises(T.TooLargeToStream, match="Too large to stream"):
        T.read_tiff("s3://fused-asset/big.tif")
    fetched = sum(1 for _, rng, _ in bucket.log[n:] if rng)
    assert fetched <= 2  # the header, never the pixels
    assert issubclass(T.TooLargeToStream, T.Unsupported)


def test_stream_refusal_threshold():
    assert T.stream_refusal(T.STREAM_BUDGET) is None
    assert "3.0 GB" in T.stream_refusal(3_000_000_000)
    assert T.level_bytes([1, 2, 3]) == 6 and T.level_bytes(7) == 7


def test_gdal_source_signs_for_s3_and_passes_local_paths(bucket, tmp_path):
    p = tmp_path / "a.tif"
    p.write_bytes(b"II*\0")
    assert T.gdal_source(str(p)) == (str(p), 4)
    name, size = T.gdal_source("s3://fused-asset/a.tif")
    assert name == "/vsicurl/https://bucket.test/a.tif"
    env = T.gdal_env({})
    assert env["GDAL_DISABLE_READDIR_ON_OPEN"] == "EMPTY_DIR"


# -------------------------------------------------- which templates stream


def test_which_templates_stream_which_files():
    from fused_render.server import templates as server_templates

    tif = server_templates.remote_stream_templates("scene.tif")
    assert [e["mode"] for e in tif] == ["geotiff"]
    assert tif[0]["path"].endswith(os.path.join("geotiff", "template.html"))
    assert [e["mode"] for e in server_templates.remote_stream_templates("x.TIFF")] == ["geotiff"]
    # duckdb is bound to CSV/JSON/.duckdb too, but its marker lists parquet only
    assert [e["mode"] for e in server_templates.remote_stream_templates("t.parquet")] == ["duckdb"]
    for name in ("t.csv", "t.json", "t.duckdb", "Makefile"):  # no text sniff either
        assert server_templates.remote_stream_templates(name) == [], name


def test_a_marker_lists_extensions_or_streams_everything(tmp_path):
    from fused_render.server import templates as server_templates

    m = tmp_path / "stream-s3"
    m.write_text(".parquet .GeoParquet\nprose. not an extension line\n")
    assert server_templates._streamed_extensions(str(m)) == (".parquet", ".geoparquet")
    m.write_text("no extension lines at all\n")
    assert server_templates._streamed_extensions(str(m)) is None
    assert server_templates._streamed_extensions(str(tmp_path / "missing")) == ()


def test_remote_stat_reports_stream_templates(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from fused_render import remote_access, remote_fetch
    from fused_render.server.routers import remote as remote_router
    from fused_render.shell import prefs

    monkeypatch.setattr(prefs, "canvases_enabled", lambda: True)
    monkeypatch.setattr(remote_router, "_index_answer", lambda name, arg: None)
    monkeypatch.setattr(remote_access, "head", lambda b, k: remote_access.ObjectInfo(
        size=123, mtime=None, etag="e"))
    monkeypatch.setattr(remote_fetch, "cached_path", lambda b, k, info: None)
    app = FastAPI()
    app.include_router(remote_router.router)
    c = TestClient(app)
    tif = c.get("/api/remote/stat", params={"url": "s3://fused-asset/a/b.tif"}).json()
    assert [e["mode"] for e in tif["stream_templates"]] == ["geotiff"]
    csv = c.get("/api/remote/stat", params={"url": "s3://fused-asset/a/b.csv"}).json()
    assert csv["stream_templates"] == []


# --------------------------------------------- review fixes (PR 2301 Bugbot)


@pytest.mark.parametrize("big", [True, False])
def test_a_corrupt_tag_count_is_unsupported_not_an_allocation(tmp_path, big):
    """A tag whose count claims more values than the file holds (uint64 in
    BigTIFF) fails as a corrupt header — before any format string or slice
    is sized by it — so callers fall back instead of the daemon dying."""
    data = bytearray(_tiff(_images(), big=big, tiled=True))
    first = struct.unpack_from("<Q" if big else "<I", data, 8 if big else 4)[0]
    entry = first + (8 if big else 2)       # the first entry (tag 254)
    # Rewrite its type to LONG8/LONG and its count to something absurd.
    if big:
        data[entry + 2:entry + 12] = struct.pack("<HQ", 16, 2 ** 61)
    else:
        data[entry + 2:entry + 8] = struct.pack("<HI", 4, 2 ** 31)
    p = tmp_path / "bad-count.tif"
    p.write_bytes(bytes(data))
    with pytest.raises(T.Unsupported, match="corrupt TIFF header"):
        T.parse_header(str(p))


def test_read_values_refuses_values_past_the_end():
    buf = b"\0" * 64
    with pytest.raises(struct.error):
        T._read_values(buf, "<", 4, 100, 0)
    assert T._read_values(buf, "<", 3, 4, 0) == [0, 0, 0, 0]


def test_a_server_ignoring_range_on_the_first_read_is_refused_unread(bucket):
    """The first read is at offset 0: a 200 carrying a whole object bigger
    than the window is refused from Content-Length, not buffered."""
    bucket.objects["/big.bin"] = os.urandom(RF.HEAD_BYTES * 4)
    bucket.ignore_range = True
    with pytest.raises(RF.RemoteError, match="ignored the byte range"):
        RF.RemoteBuffer("s3://fused-asset/big.bin")
    # an object no bigger than the window is fine either way
    bucket.objects["/small.bin"] = os.urandom(1000)
    buf = RF.RemoteBuffer("s3://fused-asset/small.bin")
    assert buf[:] == bucket.objects["/small.bin"]


def test_merged_runs_are_capped(bucket, monkeypatch):
    data = os.urandom(3_000_000)
    bucket.objects["/obj.bin"] = data
    buf = RF.RemoteBuffer("s3://fused-asset/obj.bin")
    monkeypatch.setattr(RF, "MAX_RUN", 500_000)
    before = len(bucket.log)
    ranges = [(100_000 + i * 100_000, 200_000 + i * 100_000) for i in range(20)]
    assert buf.read_many(ranges) == [data[a:b] for a, b in ranges]
    sizes = []
    for _, rng, _ in bucket.log[before:]:
        a, _, b = rng[6:].partition("-")
        sizes.append(int(b) - int(a) + 1)
    assert len(sizes) >= 4 and max(sizes) <= 500_000


# ---------------------------------------- review fixes (PR 2301 CodeRabbit)


def test_read_many_takes_slice_semantics(bucket):
    data = os.urandom(300_000)
    bucket.objects["/obj.bin"] = data
    buf = RF.RemoteBuffer("s3://fused-asset/obj.bin")
    ranges = [(-4, 300_000), (-100_000, -90_000), (299_990, 400_000), (10, 5)]
    assert buf.read_many(ranges) == [data[a:b] for a, b in ranges]


def test_merging_never_doubles_the_transfer(bucket):
    """Tiny ranges far apart (each gap just under GAP) are not merged into
    one request that is mostly gap bytes."""
    data = os.urandom(4_000_000)
    bucket.objects["/obj.bin"] = data
    buf = RF.RemoteBuffer("s3://fused-asset/obj.bin")
    before = len(bucket.log)
    ranges = [(200_000 + i * (RF.GAP + 1024), 200_000 + i * (RF.GAP + 1024) + 1024)
              for i in range(40)]
    assert buf.read_many(ranges) == [data[a:b] for a, b in ranges]
    moved = 0
    for _, rng, _ in bucket.log[before:]:
        a, _, b = rng[6:].partition("-")
        moved += int(b) - int(a) + 1
    assert moved <= 2 * sum(b - a for a, b in ranges) + RF.BLOCK * 4


def test_max_transfer_falls_back_to_unmerged(bucket):
    data = os.urandom(2_000_000)
    bucket.objects["/obj.bin"] = data
    buf = RF.RemoteBuffer("s3://fused-asset/obj.bin")
    before = len(bucket.log)
    ranges = [(300_000 + i * 60_000, 300_000 + i * 60_000 + 30_000) for i in range(10)]
    assert buf.read_many(ranges, max_transfer=350_000) == [data[a:b] for a, b in ranges]
    assert len(bucket.log) - before == 10  # merging would have moved 570 KB


def test_a_streamed_read_over_the_decode_budget_is_refused(bucket, monkeypatch):
    bucket.objects["/r.tif"] = _tiff(_images(200, 150), big=False, tiled=True)
    monkeypatch.setattr(T, "_REMOTE", {})
    monkeypatch.setattr(T, "DECODE_BUDGET", 10_000)
    with pytest.raises(T.TooLargeToStream, match="decodes to about"):
        T.read_tiff("s3://fused-asset/r.tif")


def _one_strip_tiff():
    """A 200x150 TIFF stored as ONE uncompressed strip (60 KB)."""
    return _tiff([_images(200, 150)[0]], big=False, tiled=False, compress=False, chunk=150)


@pytest.mark.parametrize("frac", [
    [0.5, 0.5, 0.5, 0.5],      # zero area: the worker reads the whole image
    [0.75, 0.75, 1.0, 1.0],    # a quarter, but inside the one strip
])
def test_rasterio_path_budget_counts_what_gdal_reads(bucket, monkeypatch, frac):
    import tiff_reader as R
    bucket.objects["/s.tif"] = _one_strip_tiff()
    monkeypatch.setattr(T, "_REMOTE", {})
    monkeypatch.setattr(T, "STREAM_BUDGET", 40_000)  # < the 60 KB strip
    with pytest.raises(T.TooLargeToStream):
        R._check_stream_budget("s3://fused-asset/s.tif", frac, 10 ** 9)


def test_window_chunks_counts_blocks_per_plane():
    t = {256: (4, [100]), 257: (4, [100]), 277: (3, [2]), 284: (3, [2]),
         322: (3, [16]), 323: (3, [16]), 324: (4, list(range(98))), 325: (4, [1] * 98)}
    need, _, _ = T.window_chunks(t, 0, 0, 20, 10)
    across, per_plane = 7, 49
    assert need == [0, 1, per_plane, per_plane + 1]
    assert T.window_chunks(t, 5, 5, 5, 9)[0] == []  # empty window
