"""Metadata reader for the AutoCAD (DXF) viewer template.

The drawing itself is parsed and rendered client-side by the vendored
`dxf-viewer` (WebGL) library. This backend does the cheap, complementary work:
a stdlib, section-aware scan of an *ASCII* DXF that recovers the facts a viewer
wants up front — format, CAD version, drawing units, header extents, the layer
table (name + ACI color), and an entity-type histogram — without pulling in a
heavy DXF library. DWG and binary-DXF are detected and reported as unsupported
(a DWG mode can slot in later); everything is returned JSON-native.

No third-party imports — this runs against whatever interpreter serves the
template, so it stays stdlib-only.
"""
import os

# --- shared spec: _gen_fixtures.py uses identical maps for its ground truth ---
ACAD_VERSIONS = {
    "AC1006": "AutoCAD R10", "AC1009": "AutoCAD R12", "AC1012": "AutoCAD R13",
    "AC1014": "AutoCAD R14", "AC1015": "AutoCAD 2000", "AC1018": "AutoCAD 2004",
    "AC1021": "AutoCAD 2007", "AC1024": "AutoCAD 2010", "AC1027": "AutoCAD 2013",
    "AC1032": "AutoCAD 2018",
}
INSUNITS = {
    0: "Unitless", 1: "Inches", 2: "Feet", 3: "Miles", 4: "Millimeters",
    5: "Centimeters", 6: "Meters", 7: "Kilometers", 8: "Microinches", 9: "Mils",
    10: "Yards", 11: "Angstroms", 12: "Nanometers", 13: "Microns",
    14: "Decimeters", 15: "Decameters", 16: "Hectometers", 17: "Gigameters",
    18: "Astronomical units", 19: "Light years", 20: "Parsecs",
    21: "US Survey Feet", 22: "US Survey Inch", 23: "US Survey Yard",
    24: "US Survey Mile",
}
SENTINEL = 1e19  # |coord| >= this => uninitialized $EXTMIN/$EXTMAX
# sub-entities that belong to a parent (POLYLINE/INSERT), not standalone entities
_SUBENTITIES = {"VERTEX", "SEQEND", "ATTRIB"}


def _version_name(code):
    return ACAD_VERSIONS.get(code, code)


def _units_name(code):
    return INSUNITS.get(int(code), "Unknown")


def _detect(path):
    """Return (format, acadver) from the file's first bytes."""
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head.startswith(b"AutoCAD Binary DXF"):
        return "binary-dxf", None
    if head[:4] == b"AC10":
        return "dwg", head[:6].decode("ascii", "replace")
    return "dxf", None


def _iter_pairs(path):
    """Yield (code:int, value:str) group-code pairs, streaming (bounded memory)."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        while True:
            code_line = fh.readline()
            if code_line == "":
                return
            val_line = fh.readline()
            if val_line == "":
                return
            try:
                code = int(code_line.strip())
            except ValueError:
                continue
            yield code, val_line.strip()


def _scan_ascii_dxf(path):
    acadver = None
    insunits = 0
    ext = {"$EXTMIN": {}, "$EXTMAX": {}}
    layers = []
    cur_layer = None
    counts = {}

    section = None
    cur_table = None
    expect_section_name = False
    expect_table_name = False
    header_var = None

    def flush_layer():
        nonlocal cur_layer
        if cur_layer and cur_layer.get("name") is not None:
            layers.append(cur_layer)
        cur_layer = None

    for code, value in _iter_pairs(path):
        if code == 0:
            header_var = None
            if value == "SECTION":
                expect_section_name = True
                section = None
            elif value == "ENDSEC":
                flush_layer()
                section = None
                cur_table = None
            elif value == "TABLE":
                flush_layer()
                cur_table = None
                expect_table_name = True
            elif value == "ENDTAB":
                flush_layer()
                cur_table = None
            elif value == "LAYER" and section == "TABLES" and cur_table == "LAYER":
                flush_layer()
                cur_layer = {"name": None, "color": 7}
            elif section == "ENTITIES" and value not in _SUBENTITIES:
                counts[value] = counts.get(value, 0) + 1
            continue

        if expect_section_name and code == 2:
            section = value
            expect_section_name = False
            if section == "TABLES":
                expect_table_name = False
            continue

        if section == "TABLES":
            if expect_table_name and code == 2:
                cur_table = value
                expect_table_name = False
            elif cur_table == "LAYER" and cur_layer is not None:
                if code == 2:
                    cur_layer["name"] = value
                elif code == 62:
                    try:
                        cur_layer["color"] = abs(int(float(value)))
                    except ValueError:
                        pass
            continue

        if section == "HEADER":
            if code == 9:
                header_var = value
            elif header_var == "$ACADVER" and code == 1:
                acadver = value
            elif header_var == "$INSUNITS" and code == 70:
                try:
                    insunits = int(float(value))
                except ValueError:
                    pass
            elif header_var in ("$EXTMIN", "$EXTMAX") and code in (10, 20, 30):
                try:
                    ext[header_var][code] = round(float(value), 3)
                except ValueError:
                    pass

    flush_layer()

    def assemble(name):
        d = ext[name]
        if not all(k in d for k in (10, 20, 30)):
            return None
        coords = [d[10], d[20], d[30]]
        return None if any(abs(c) >= SENTINEL for c in coords) else coords

    layers.sort(key=lambda x: x["name"].lower())
    return {
        "acadver": acadver,
        "version_name": _version_name(acadver) if acadver else None,
        "insunits": insunits,
        "units_name": _units_name(insunits),
        "extmin": assemble("$EXTMIN"),
        "extmax": assemble("$EXTMAX"),
        "layers": layers,
        "layer_count": len(layers),
        "entity_counts": counts,
        "entity_total": sum(counts.values()),
    }


def main(file: str) -> dict:
    """Return DXF metadata for `file`. Never raises for a bad/missing file —
    reports it via `format="unknown"` + `error` so the template can show a
    friendly state."""
    try:
        size = os.path.getsize(file)
        fmt, acadver = _detect(file)
    except OSError as e:
        return {"format": "unknown", "supported": False, "error": str(e)}

    if fmt == "dwg":
        return {
            "format": "dwg",
            "supported": False,
            "acadver": acadver,
            "version_name": _version_name(acadver) if acadver else None,
            "size": size,
        }
    if fmt == "binary-dxf":
        return {"format": "binary-dxf", "supported": False, "size": size}

    info = _scan_ascii_dxf(file)
    info.update({"format": "dxf", "supported": True, "size": size})
    return info
