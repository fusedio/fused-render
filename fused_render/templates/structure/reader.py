"""Reader backing structure/template.html — the internal structure of a Parquet
file: its file-level metadata, per-column schema, and the physical byte layout
down to each row group's column chunks (offsets, sizes, compression, encodings
and statistics). No data pages are read; pyarrow parses only the footer, so
this stays cheap even on a multi-GB file.

Two calls, because a heavy file has O(row_groups × columns) column chunks
(1000 row groups × 30 columns = 30 000) and one object per chunk is megabytes
of JSON the page can't render:

  • main(file) — the overview. File summary + schema + one entry per row group
    carrying *numeric arrays* (chunk_bytes / chunk_starts, indexed by schema
    leaf order) instead of per-chunk objects, plus `layout`: the ordered list
    of physical regions (PAR1 header, each row group, PAR1 footer) for the box
    diagram, each with {start, bytes, end} byte offsets on disk. The layout's
    row groups carry no per-column entries — the template derives column boxes
    from the row group's arrays plus schema[j].path, so the offsets are shipped
    once, not twice.

  • main(file, row_group=i) — the detail for ONE row group: today's full
    per-column-chunk objects (compression, encodings, num_values, byte range,
    statistics), fetched lazily when that row group is expanded.

A column chunk starts at its dictionary page when present, else its first data
page; its on-disk length is total_compressed_size. The footer is the trailing
FileMetaData thrift (serialized_size) plus a 4-byte length and the 4-byte PAR1
magic, so it ends exactly at end-of-file.

Called by fused.runPython with {file: "<path>"} and, for the detail,
{file: "<path>", row_group: <int>}.
"""
import datetime
import decimal
import os

import pyarrow.parquet as pq

# Leading + trailing magic, and the 4-byte little-endian footer-length that
# sits between the FileMetaData thrift and the trailing magic.
_MAGIC_LEN = 4
_FOOTER_LEN_FIELD = 4

# Cap on any string this module emits. min/max statistics are arbitrary column
# values — a non-pyarrow writer can put a whole blob in them, and 30 000 chunks
# of untruncated min/max dwarf everything else in the detail payload. The
# diagram only ever shows a short preview anyway.
_MAX_STR = 64


def _clip(text: str) -> str:
    """Bound a produced string to _MAX_STR chars, marking truncation."""
    return text if len(text) <= _MAX_STR else text[:_MAX_STR] + "…"


def _jsonify(value):
    """Coerce a statistics value (min/max may be bytes, Decimal, date/time)
    into something json.dumps can encode, bounded in length."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, (bytes, bytearray)):
        return _clip(value.hex())
    if isinstance(value, decimal.Decimal):
        return _clip(str(value))
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return _clip(value.isoformat())
    return _clip(str(value))


def _chunk_start(cc) -> int:
    """First on-disk byte of a column chunk: its dictionary page if it has one,
    otherwise its first data page. (file_offset is 0 for arrow-written files,
    so it can't be trusted.)"""
    if cc.has_dictionary_page and cc.dictionary_page_offset is not None:
        return int(cc.dictionary_page_offset)
    return int(cc.data_page_offset)


def _stats(cc):
    st = cc.statistics
    if st is None:
        return None
    return {
        "min": _jsonify(st.min) if st.has_min_max else None,
        "max": _jsonify(st.max) if st.has_min_max else None,
        "nulls": st.null_count,
        "distinct": st.distinct_count,
    }


def _schema(md):
    """Per-column parquet schema (leaf columns), with the physical and logical
    typing that the metadata view labels each column by."""
    out = []
    for i in range(md.num_columns):
        c = md.schema.column(i)
        lt = c.logical_type
        out.append({
            "name": c.name,
            "path": c.path,
            "physical_type": c.physical_type,
            "logical_type": None if lt is None or str(lt) == "None" else str(lt),
            "converted_type": c.converted_type,
            "max_def": c.max_definition_level,
            "max_rep": c.max_repetition_level,
        })
    return out


def _row_group_summary(rg, gi):
    """Compact per-row-group entry: the two numbers per column chunk the
    diagram actually needs (on-disk length and first byte), as parallel arrays
    in row-group column order — which is schema leaf order in parquet, so the
    template can index them by leaf index. Also returns the aggregates the file
    summary rolls up (uncompressed total, codec names), computed in this same
    pass so the footer is only walked once."""
    sizes = []
    starts = []
    uncompressed = 0
    codecs = set()
    for ci in range(rg.num_columns):
        cc = rg.column(ci)
        sizes.append(int(cc.total_compressed_size))
        starts.append(_chunk_start(cc))
        uncompressed += int(cc.total_uncompressed_size)
        codecs.add(cc.compression)
    return {
        "index": gi,
        "num_rows": rg.num_rows,
        "total_byte_size": int(rg.total_byte_size),
        "compressed_size": sum(sizes),
        "chunk_bytes": sizes,
        "chunk_starts": starts,
    }, uncompressed, codecs


def _row_group_detail(rg, gi):
    """Full per-column-chunk detail for one row group — the lazily fetched
    view behind an expanded row group."""
    cols = []
    for ci in range(rg.num_columns):
        cc = rg.column(ci)
        start = _chunk_start(cc)
        csize = int(cc.total_compressed_size)
        cols.append({
            "path": cc.path_in_schema,
            "physical_type": cc.physical_type,
            "compression": cc.compression,
            "encodings": list(cc.encodings),
            "num_values": cc.num_values,
            "has_dictionary": bool(cc.has_dictionary_page),
            "start": start,
            "compressed_size": csize,
            "uncompressed_size": int(cc.total_uncompressed_size),
            "end": start + csize,
            "stats": _stats(cc),
        })
    return {
        "index": gi,
        "num_rows": rg.num_rows,
        "total_byte_size": int(rg.total_byte_size),
        "compressed_size": sum(c["compressed_size"] for c in cols),
        "columns": cols,
    }


def _layout(row_groups, file_size, footer_start, footer_bytes):
    """Ordered physical regions for the box diagram: PAR1 header, each row
    group, then the PAR1 footer. Column chunks are deliberately NOT repeated
    here — they're already in each row group's chunk_starts/chunk_bytes."""
    regions = [{"kind": "header", "label": "PAR1",
                "start": 0, "bytes": _MAGIC_LEN, "end": _MAGIC_LEN}]
    for rg in row_groups:
        regions.append({
            "kind": "row_group",
            "index": rg["index"],
            "num_rows": rg["num_rows"],
            "bytes": rg["compressed_size"],
        })
    if file_size is not None:
        regions.append({"kind": "footer", "label": "PAR1",
                        "start": footer_start, "bytes": footer_bytes,
                        "end": file_size})
    return regions


def _row_group_index(value, num_row_groups) -> int:
    """Parse the requested row group. It arrives from JS as a number, but a
    param that round-tripped through the URL is a string, so accept both."""
    try:
        gi = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"row_group must be an integer, got {value!r}") from None
    if gi < 0 or gi >= num_row_groups:
        raise ValueError(
            f"row_group {gi} out of range: file has {num_row_groups} row groups"
        )
    return gi


def main(file: str, row_group=None) -> dict:
    # row_group is intentionally unannotated: bind_params only coerces int/
    # float/str annotations, and an annotation of `int` would blow up on a
    # JS-sent null. Absent from the params dict it keeps this default, so the
    # overview call stays `main(file)`.
    pf = pq.ParquetFile(file)
    md = pf.metadata

    if row_group is not None and row_group != "":
        gi = _row_group_index(row_group, md.num_row_groups)
        return _row_group_detail(md.row_group(gi), gi)

    try:
        file_size = os.path.getsize(file)
    except OSError:
        file_size = None

    serialized = int(md.serialized_size)
    footer_bytes = serialized + _FOOTER_LEN_FIELD + _MAGIC_LEN
    footer_start = (file_size - footer_bytes) if file_size is not None else None

    row_groups = []
    total_uncompressed = 0
    codecs = set()
    for gi in range(md.num_row_groups):
        summary, uncompressed, rg_codecs = _row_group_summary(md.row_group(gi), gi)
        row_groups.append(summary)
        total_uncompressed += uncompressed
        codecs |= rg_codecs
    total_compressed = sum(rg["compressed_size"] for rg in row_groups)

    return {
        "file": {
            "path": file,
            "size": file_size,
            "num_rows": md.num_rows,
            "num_row_groups": md.num_row_groups,
            "num_columns": md.num_columns,
            "format_version": str(md.format_version),
            "created_by": md.created_by,
            "serialized_size": serialized,
            "footer_start": footer_start,
            "footer_bytes": footer_bytes,
            "total_compressed": total_compressed,
            "total_uncompressed": total_uncompressed,
            "compression": sorted(codecs),
        },
        "schema": _schema(md),
        "row_groups": row_groups,
        "layout": _layout(row_groups, file_size, footer_start, footer_bytes),
    }
