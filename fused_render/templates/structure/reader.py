"""Reader backing structure/template.html — the internal structure of a Parquet
file: its file-level metadata, per-column schema, and the physical byte layout
down to each row group's column chunks (offsets, sizes, compression, encodings
and statistics). No data pages are read; pyarrow parses only the footer, so
this stays cheap even on a multi-GB file.

Two views are built from one pass:
  • metadata — file summary + schema + per-row-group / per-column-chunk detail.
  • layout   — an ordered list of physical regions (PAR1 header, each row group
               with its column chunks, PAR1 footer) for the box diagram, each
               carrying {start, bytes, end} byte offsets on disk.

A column chunk starts at its dictionary page when present, else its first data
page; its on-disk length is total_compressed_size. The footer is the trailing
FileMetaData thrift (serialized_size) plus a 4-byte length and the 4-byte PAR1
magic, so it ends exactly at end-of-file.

Returns {file, schema, row_groups, layout}. Called by fused.runPython with
{file: "<path>"}.
"""

import datetime
import decimal
import os

import pyarrow.parquet as pq

# Leading + trailing magic, and the 4-byte little-endian footer-length that
# sits between the FileMetaData thrift and the trailing magic.
_MAGIC_LEN = 4
_FOOTER_LEN_FIELD = 4


def _jsonify(value):
    """Coerce a statistics value (min/max may be bytes, Decimal, date/time)
    into something json.dumps can encode."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    return str(value)


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
        out.append(
            {
                "name": c.name,
                "path": c.path,
                "physical_type": c.physical_type,
                "logical_type": None if lt is None or str(lt) == "None" else str(lt),
                "converted_type": c.converted_type,
                "max_def": c.max_definition_level,
                "max_rep": c.max_repetition_level,
            }
        )
    return out


def _row_groups(md):
    groups = []
    for gi in range(md.num_row_groups):
        rg = md.row_group(gi)
        cols = []
        compressed = 0
        for ci in range(rg.num_columns):
            cc = rg.column(ci)
            start = _chunk_start(cc)
            csize = int(cc.total_compressed_size)
            compressed += csize
            cols.append(
                {
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
                }
            )
        groups.append(
            {
                "index": gi,
                "num_rows": rg.num_rows,
                "total_byte_size": int(rg.total_byte_size),
                "compressed_size": compressed,
                "columns": cols,
            }
        )
    return groups


def _layout(row_groups, file_size, footer_start, footer_bytes):
    """Ordered physical regions for the box diagram: PAR1 header, each row group
    (with its column chunks), then the PAR1 footer."""
    regions = [
        {"kind": "header", "label": "PAR1", "start": 0, "bytes": _MAGIC_LEN, "end": _MAGIC_LEN}
    ]
    for rg in row_groups:
        regions.append(
            {
                "kind": "row_group",
                "index": rg["index"],
                "num_rows": rg["num_rows"],
                "bytes": rg["compressed_size"],
                "columns": [
                    {
                        "path": c["path"],
                        "start": c["start"],
                        "bytes": c["compressed_size"],
                        "end": c["end"],
                    }
                    for c in rg["columns"]
                ],
            }
        )
    if file_size is not None:
        regions.append(
            {
                "kind": "footer",
                "label": "PAR1",
                "start": footer_start,
                "bytes": footer_bytes,
                "end": file_size,
            }
        )
    return regions


def main(file: str) -> dict:
    pf = pq.ParquetFile(file)
    md = pf.metadata

    try:
        file_size = os.path.getsize(file)
    except OSError:
        file_size = None

    serialized = int(md.serialized_size)
    footer_bytes = serialized + _FOOTER_LEN_FIELD + _MAGIC_LEN
    footer_start = (file_size - footer_bytes) if file_size is not None else None

    row_groups = _row_groups(md)
    compressions = sorted({c["compression"] for rg in row_groups for c in rg["columns"]})
    total_compressed = sum(rg["compressed_size"] for rg in row_groups)
    total_uncompressed = sum(c["uncompressed_size"] for rg in row_groups for c in rg["columns"])

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
            "compression": compressions,
        },
        "schema": _schema(md),
        "row_groups": row_groups,
        "layout": _layout(row_groups, file_size, footer_start, footer_bytes),
    }
