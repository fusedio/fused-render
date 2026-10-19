from __future__ import annotations

import mmap
import struct
import threading
import zlib
from functools import lru_cache

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

TILE = 512
GRID = 2048
MAX_CLASSES = 32
SAMPLE_FORMATS = {"u": 1, "i": 2, "f": 3}
TYPES = {3: "H", 12: "d", 16: "Q"}


def _ceil(a: int, b: int) -> int:
    return -(-a // b)


def _lzw_head(data: bytes, n: int) -> bytes:
    out, added, prev, pos = b"", {}, b"", 0
    while len(out) < n and pos + 9 <= 8 * len(data) - 16:
        code = (int.from_bytes(data[pos >> 3:(pos >> 3) + 3], "big") >> (15 - (pos & 7))) & 0x1FF
        pos += 9
        if code == 256:
            added, prev = {}, b""
            continue
        if code == 257:
            break
        entry = bytes([code]) if code < 256 else added.get(code, prev + prev[:1])
        if prev:
            added[258 + len(added)] = prev + entry[:1]
        out, prev = out + entry, entry
    return out[:n]


def _mode(data: np.ndarray, k: int, nodata) -> np.ndarray:
    _, h, w = data.shape
    blocks = data.reshape(h // k, k, w // k, k).transpose(0, 2, 1, 3).reshape(h // k, w // k, k * k)
    values = [v for v in np.unique(blocks) if v != nodata]
    best, top = blocks[..., 0].copy(), np.zeros(blocks.shape[:2], np.uint8)
    for value in values if len(values) <= MAX_CLASSES else []:
        count = (blocks == value).sum(-1, dtype=np.uint8)
        best[count > top] = value
        top = np.maximum(top, count)
    return best[None]


def _sampleable(dataset) -> bool:
    image, bits = dataset.tags(ns="IMAGE_STRUCTURE"), _bits(dataset)
    return (dataset.driver == "GTiff" and dataset.profile.get("tiled") and not dataset.overviews(1)
            and dataset.count * bits <= 512
            and image.get("COMPRESSION", "NONE") in ("NONE", "LZW", "DEFLATE") and image.get("PREDICTOR", "1") in ("1", "2")
            and (dataset.count == 1 or image.get("INTERLEAVE") == "PIXEL") and not (bits % 8 and (bits > 8 or dataset.count > 1)))


def _bits(dataset) -> int:
    return int(dataset.tags(1, ns="IMAGE_STRUCTURE").get("NBITS", np.dtype(dataset.dtypes[0]).itemsize * 8))


def _corners(dataset):
    count, dtype, bits = dataset.count, np.dtype(dataset.dtypes[0]), _bits(dataset)
    compression = dataset.tags(ns="IMAGE_STRUCTURE").get("COMPRESSION", "NONE")
    tile = dataset.block_shapes[0]
    grid = (_ceil(dataset.height, tile[0]), _ceil(dataset.width, tile[1]))
    block = lambda kind, x, y: int(dataset.get_tag_item(f"BLOCK_{kind}_{x}_{y}", "TIFF", bidx=1) or 0)
    n = max(1, count * bits // 8)
    with open(dataset.name, "rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as buf:
        order = ">" if buf[:2] == b"MM" else "<"
        heads, empty = [], []
        for y in range(grid[0]):
            for x in range(grid[1]):
                offset, size = block("OFFSET", x, y), block("SIZE", x, y)
                empty.append(size == 0)
                raw = buf[offset:offset + min(size, 4096)]
                head = (raw[:n] if compression == "NONE" else _lzw_head(raw + b"\0\0", n) if compression == "LZW"
                        else zlib.decompressobj().decompress(raw, n))
                heads.append(head.ljust(n, b"\0"))
    raw = np.frombuffer(b"".join(heads), np.uint8)
    values = (raw >> (8 - bits)).astype(dtype) if bits < 8 else raw.view(dtype.newbyteorder(order)).astype(dtype)
    values = values.reshape(*grid, count)
    if dataset.nodata is not None:
        values[np.reshape(empty, grid)] = dataset.nodata
    return values


class VirtualCOG:
    def __init__(self, path: str):
        self.path, self.local = path, threading.local()
        with rasterio.open(path) as dataset:
            epsg = dataset.crs.to_epsg() if dataset.crs else None
            self.warp = epsg is None or epsg > 65535 or dataset.transform.b or dataset.transform.d
            self.colormap = dataset.colormap(1) if dataset.count == 1 and dataset.dtypes[0] == "uint8" \
                and dataset.colorinterp[0].name == "palette" else None
            self.sample = not self.warp and _sampleable(dataset)
            self.cell = dataset.block_shapes[0] if self.sample else None
            overviews = bool(dataset.overviews(1))
        source = self.dataset()
        self.width, self.height, self.count = source.width, source.height, source.count
        self.dtype, self.nodata, self.transform = np.dtype(source.dtypes[0]), source.nodata, source.transform
        self.epsg, self.geographic = source.crs.to_epsg(), source.crs.is_geographic
        self.factors = [1]
        while max(self.width, self.height) > TILE * self.factors[-1]:
            self.factors.append(self.factors[-1] * 2)
        if not (self.sample or overviews):
            step = 1
            while max(self.width, self.height) > GRID * step:
                step *= 2
            self.cell = (step, step)
        self.coarse_from = self.cell and (max(self.cell) // 2 if self.sample else self.cell[0])
        self.lock, self.grid = threading.Lock(), None
        self.levels = [(f, _ceil(self.width, f), _ceil(self.height, f)) for f in self.factors]
        self.grids = [(_ceil(w, TILE), _ceil(h, TILE)) for _, w, h in self.levels]
        self.tile_bytes = TILE * TILE * self.count * self.dtype.itemsize
        self.header = self._layout()
        self.firsts = np.cumsum([0] + [gx * gy for gx, gy in self.grids])
        self.size = len(self.header) + int(self.firsts[-1]) * self.tile_bytes
        self.tile = lru_cache(maxsize=8)(self._tile)

    def dataset(self):
        if not hasattr(self.local, "dataset"):
            dataset = rasterio.open(self.path)
            self.local.dataset = WarpedVRT(dataset, crs="EPSG:4326") if self.warp else dataset
        return self.local.dataset

    def _tags(self, level: int, width: int, height: int, tiles: int) -> list:
        kind = SAMPLE_FORMATS[self.dtype.kind]
        tags = [(254, 16, [int(level > 0)]), (256, 16, [width]), (257, 16, [height]),
                (258, 3, [self.dtype.itemsize * 8] * self.count), (259, 3, [1]),
                (262, 3, [3 if self.colormap else 1]), (277, 3, [self.count]), (284, 3, [1]),
                (322, 3, [TILE]), (323, 3, [TILE]), (324, 16, [0] * tiles),
                (325, 16, [self.tile_bytes] * tiles), (339, 3, [kind] * self.count)]
        if level == 0:
            a, _, c, _, e, f = self.transform[:6]
            key = (2048, 2) if self.geographic else (3072, 1)
            tags += [(33550, 12, [a, -e, 0]), (33922, 12, [0, 0, 0, c, f, 0]),
                     (34735, 3, [1, 1, 0, 3, 1024, 0, 1, key[1], 1025, 0, 1, 1, key[0], 0, 1, self.epsg])]
            if self.nodata is not None:
                tags.append((42113, 2, f"{self.nodata!r}\0".encode()))
            if self.colormap:
                table = np.array([self.colormap.get(i, (0, 0, 0, 0))[:3] for i in range(256)]) * 257
                tags.append((320, 3, table.T.ravel()))
        return sorted(tags)

    def _layout(self) -> bytes:
        ifds = [self._tags(level, w, h, gx * gy) for level, ((_, w, h), (gx, gy)) in enumerate(zip(self.levels, self.grids))]
        encode = lambda kind, values: values if kind == 2 else np.asarray(values, "<" + TYPES[kind]).tobytes()
        payloads = [[encode(kind, values) for _, kind, values in tags] for tags in ifds]
        extra = 16 + sum(16 + 20 * len(tags) for tags in ifds)
        data = _ceil(extra + sum(_ceil(len(p), 8) * 8 for ps in payloads for p in ps if len(p) > 8), 32768) * 32768
        base = data
        for tags, ps, (gx, gy) in zip(ifds, payloads, self.grids):
            ps[[tag for tag, _, _ in tags].index(324)] = encode(16, base + np.arange(gx * gy) * self.tile_bytes)
            base += gx * gy * self.tile_bytes
        head, tail = bytearray(b"II" + struct.pack("<HHHQ", 43, 8, 0, 16)), bytearray()
        for index, (tags, ps) in enumerate(zip(ifds, payloads)):
            head += struct.pack("<Q", len(tags))
            for (tag, kind, values), payload in zip(tags, ps):
                if len(payload) <= 8:
                    head += struct.pack("<HHQ", tag, kind, len(values)) + payload.ljust(8, b"\0")
                else:
                    head += struct.pack("<HHQQ", tag, kind, len(values), extra + len(tail))
                    tail += payload.ljust(_ceil(len(payload), 8) * 8, b"\0")
            head += struct.pack("<Q", len(head) + 8 if index < len(ifds) - 1 else 0)
        return bytes(head + tail).ljust(data, b"\0")

    def _tile(self, index: int) -> bytes:
        level = int(np.searchsorted(self.firsts, index, side="right")) - 1
        (f, w, h), (gx, _) = self.levels[level], self.grids[level]
        ty, tx = divmod(index - int(self.firsts[level]), gx)
        x0, y0 = tx * TILE, ty * TILE
        width, height = min(TILE, w - x0), min(TILE, h - y0)
        out = np.full((TILE, TILE, self.count), self.nodata or 0, self.dtype)
        k = min(4, f) if self.count == 1 and self.dtype.itemsize == 1 else 1
        if self.coarse_from and f >= self.coarse_from:
            grid = self._coarse()
            rows = np.minimum(np.arange(y0 * k, (y0 + height) * k) * f // k // self.cell[0], grid.shape[0] - 1)
            cols = np.minimum(np.arange(x0 * k, (x0 + width) * k) * f // k // self.cell[1], grid.shape[1] - 1)
            data = np.moveaxis(grid[rows[:, None], cols[None, :]], -1, 0)
        else:
            window = Window(x0 * f, y0 * f, min(width * f, self.width - x0 * f), min(height * f, self.height - y0 * f))
            data = self.dataset().read(window=window, out_shape=(self.count, height * k, width * k), resampling=Resampling.nearest)
        out[:height, :width] = np.moveaxis(_mode(data, k, self.nodata) if k > 1 else data, 0, -1)
        return out.tobytes()

    def _coarse(self) -> np.ndarray:
        with self.lock:
            if self.grid is None and self.sample:
                with rasterio.open(self.path) as dataset:
                    self.grid = _corners(dataset)
            elif self.grid is None:
                shape = (self.count, _ceil(self.height, self.cell[0]), _ceil(self.width, self.cell[1]))
                self.grid = np.moveaxis(self.dataset().read(out_shape=shape, resampling=Resampling.nearest), 0, -1)
        return self.grid

    def read(self, start: int, end: int) -> bytes:
        parts, pos = [self.header[start:end]], max(start, len(self.header))
        while pos < end:
            index, inner = divmod(pos - len(self.header), self.tile_bytes)
            chunk = self.tile(index)[inner:inner + end - pos]
            parts.append(chunk)
            pos += len(chunk)
        return b"".join(parts)
