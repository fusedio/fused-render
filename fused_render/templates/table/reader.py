# /// script
# dependencies = ["pyarrow"]
# ///
"""Reader backing table/template.html. Returns a JSON-safe page of rows."""
import datetime
import decimal

import fused
import pyarrow.parquet as pq


def _jsonify(value):
    """Stringify scalars pyarrow may hand back that json.dumps can't encode."""
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    return value


@fused.udf
def main(file: str, offset: int = 0, limit: int = 100) -> dict:
    table = pq.read_table(file)
    total_rows = table.num_rows
    rows = table.slice(offset, limit).to_pylist()
    rows = [{k: _jsonify(v) for k, v in row.items()} for row in rows]
    return {"columns": table.schema.names, "rows": rows, "total_rows": total_rows}
