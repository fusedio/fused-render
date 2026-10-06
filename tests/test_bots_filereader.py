"""fused_render/bots/filereader.py: the `readfile` tool's readers. PDF and
image cases need macOS (sips, PDFKit); they skip elsewhere."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import os
import subprocess
import sys

import pytest

from fused_render.bots import filereader as fr

mac = pytest.mark.skipif(sys.platform != "darwin", reason="sips / PDFKit are macOS")


def test_kind_by_extension_and_sniff(tmp_path):
    (tmp_path / "a.md").write_text("# hi")
    (tmp_path / "noext").write_bytes(b"%PDF-1.4 ...")
    (tmp_path / "png").write_bytes(b"\x89PNG\r\n\x1a\n...")
    (tmp_path / "bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / "plain").write_bytes(b"just words")
    assert fr.kind(str(tmp_path / "a.md")) == "text"
    assert fr.kind(str(tmp_path / "noext")) == "pdf"
    assert fr.kind(str(tmp_path / "png")) == "image"
    assert fr.kind(str(tmp_path / "bin")) == "binary"
    assert fr.kind(str(tmp_path / "plain")) == "text"


def test_text_is_paged(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("a" * fr.PAGE_CHARS + "b" * 10)
    t, img = fr.read_file(str(p))
    assert img is None and "part 1 of 2" in t and t.endswith("a" * 50)
    t, _ = fr.read_file(str(p), 2)
    assert "part 2 of 2" in t and t.endswith("b" * 10)
    t, _ = fr.read_file(str(p), 99)  # clamped
    assert "part 2 of 2" in t
    t, _ = fr.read_file(str(p), "x")  # garbage page -> 1
    assert "part 1 of 2" in t


def test_binary_and_missing(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00" * 10)
    t, img = fr.read_file(str(p))
    assert t.startswith("error:") and "binary" in t and img is None
    t, _ = fr.read_file(str(tmp_path / "nope"))
    assert t.startswith("error: not a file")


@mac
def test_rich_text_via_textutil(tmp_path):
    p = tmp_path / "a.rtf"
    p.write_text(r"{\rtf1\ansi Hello RTF world}")
    t, img = fr.read_file(str(p))
    assert img is None and "Hello RTF world" in t and "converted to text" in t


def _pdf(path, pages, text=True):
    import Quartz
    url = Quartz.CFURLCreateWithFileSystemPath(None, path, Quartz.kCFURLPOSIXPathStyle, False)
    ctx = Quartz.CGPDFContextCreateWithURL(url, Quartz.CGRectMake(0, 0, 300, 300), None)
    for i in range(pages):
        Quartz.CGPDFContextBeginPage(ctx, None)
        if text:
            Quartz.CGContextSelectFont(ctx, b"Helvetica", 12, Quartz.kCGEncodingMacRoman)
            Quartz.CGContextSetTextDrawingMode(ctx, Quartz.kCGTextFill)
            s = f"Page {i + 1}: enough words here to count as a real text layer for the test."
            Quartz.CGContextShowTextAtPoint(ctx, 20, 150, s.encode(), len(s))
        else:
            Quartz.CGContextSetRGBFillColor(ctx, 1, 0, 0, 1)
            Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(20, 20, 100, 100))
        Quartz.CGPDFContextEndPage(ctx)
    Quartz.CGPDFContextClose(ctx)


@mac
def test_pdf_text_pages(tmp_path):
    pytest.importorskip("Quartz")
    p = str(tmp_path / "doc.pdf")
    _pdf(p, 2)
    t, img = fr.read_file(p)
    assert img is None and "page 1 of 2" in t and "Page 1:" in t
    t, _ = fr.read_file(p, 2)
    assert "page 2 of 2" in t and "Page 2:" in t


@mac
def test_pdf_scan_is_an_image(tmp_path):
    pytest.importorskip("Quartz")
    p = str(tmp_path / "scan.pdf")
    _pdf(p, 1, text=False)
    t, img = fr.read_file(p)
    assert "no text layer" in t and img and img[:3] == b"\xff\xd8\xff"


@mac
def test_image_is_jpeg(tmp_path):
    p = str(tmp_path / "scan.pdf")
    pytest.importorskip("Quartz")
    _pdf(p, 1, text=False)
    png = str(tmp_path / "a.png")
    subprocess.run(["sips", "-s", "format", "png", p, "--out", png], capture_output=True)
    assert os.path.exists(png)
    t, img = fr.read_file(png)
    assert t.startswith("IMAGE a.png") and img[:3] == b"\xff\xd8\xff"
