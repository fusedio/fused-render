"""Regenerate the DXF test fixtures + their ground-truth sidecars.

One-time dev tool (NOT collected by pytest — leading underscore). The ezdxf
fixtures need `ezdxf`, which is not a template dependency, so run out-of-band:

    uv run --with ezdxf python _gen_fixtures.py

For every fixture it writes `<name>.dxf` (or `.dwg`) and a `<name>.expected.json`
sidecar listing the facts a metadata reader should recover. `test_reader.py`
runs the stdlib `reader.py` and asserts each key present in a sidecar matches —
so ezdxf is the source of truth and `reader.py` must independently reproduce it.

Note: ezdxf resets $EXTMIN/$EXTMAX to the uninitialized sentinel (1e20) on save
(a real CAD app writes them after zoom-extents), so the ezdxf fixtures exercise
the "extents not set" path. `crafted.dxf` is hand-written to carry real extents
and an off-layer (negative color).
"""
import json
import os

import ezdxf
from ezdxf import bbox

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
os.makedirs(FIX, exist_ok=True)

SENTINEL = 1e19  # |coord| >= this => uninitialized $EXTMIN/$EXTMAX

# --- shared spec: reader.py MUST use identical maps ---
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


def version_name(code):
    return ACAD_VERSIONS.get(code, code)


def units_name(code):
    return INSUNITS.get(int(code), "Unknown")


def facts_from_doc(doc):
    msp = doc.modelspace()
    counts = {}
    for e in msp:
        t = e.dxftype()
        counts[t] = counts.get(t, 0) + 1
    layers = sorted(
        ({"name": lay.dxf.name, "color": abs(int(lay.dxf.color))} for lay in doc.layers),
        key=lambda x: x["name"].lower(),
    )

    def vec(name):
        try:
            v = doc.header[name]
            coords = [round(float(v[0]), 3), round(float(v[1]), 3), round(float(v[2]), 3)]
        except Exception:
            return None
        return None if any(abs(c) >= SENTINEL for c in coords) else coords

    acad = doc.header.get("$ACADVER")
    units = int(doc.header.get("$INSUNITS", 0))
    return {
        "format": "dxf",
        "supported": True,
        "acadver": acad,
        "version_name": version_name(acad),
        "insunits": units,
        "units_name": units_name(units),
        "extmin": vec("$EXTMIN"),
        "extmax": vec("$EXTMAX"),
        "layers": layers,
        "layer_count": len(layers),
        "entity_counts": counts,
        "entity_total": sum(counts.values()),
    }


def write_sidecar(name, facts):
    with open(os.path.join(FIX, name + ".expected.json"), "w", encoding="utf-8") as f:
        json.dump(facts, f, indent=2)


def save(doc, name):
    doc.saveas(os.path.join(FIX, name + ".dxf"))
    facts = facts_from_doc(doc)
    write_sidecar(name, facts)
    print(f"wrote {name}.dxf  ({facts['entity_total']} entities, {facts['layer_count']} layers)")


# ---- floorplan.dxf : rich, R2018, mm, colored layers, mixed entities ----
doc = ezdxf.new("R2018", setup=True)
doc.header["$INSUNITS"] = 4  # millimeters
for lname, color in [
    ("WALLS", 4), ("DOORS", 3), ("WINDOWS", 5),
    ("DIMENSIONS", 2), ("FURNITURE", 6), ("TEXT", 7), ("ELECTRICAL", 1),
]:
    doc.layers.add(lname, color=color)
msp = doc.modelspace()
msp.add_lwpolyline([(1000, 1000), (11000, 1000), (11000, 7000), (1000, 7000)],
                   close=True, dxfattribs={"layer": "WALLS"})
msp.add_line((6500, 1000), (6500, 5000), dxfattribs={"layer": "WALLS"})
msp.add_line((6500, 4000), (11000, 4000), dxfattribs={"layer": "WALLS"})
msp.add_line((1000, 5000), (6500, 5000), dxfattribs={"layer": "WALLS"})
msp.add_line((4600, 1000), (5600, 1000), dxfattribs={"layer": "WINDOWS"})
msp.add_line((9000, 1000), (10200, 1000), dxfattribs={"layer": "WINDOWS"})
msp.add_arc((3000, 1000), 900, 0, 90, dxfattribs={"layer": "DOORS"})
msp.add_arc((6500, 2600), 900, 90, 180, dxfattribs={"layer": "DOORS"})
msp.add_line((3000, 1000), (3900, 1000), dxfattribs={"layer": "DOORS"})
msp.add_line((6500, 2600), (6500, 3500), dxfattribs={"layer": "DOORS"})
msp.add_lwpolyline([(1500, 1500), (3700, 1500), (3700, 2400), (1500, 2400)],
                   close=True, dxfattribs={"layer": "FURNITURE"})
msp.add_lwpolyline([(7200, 1400), (9400, 1400), (9400, 2600), (7200, 2600)],
                   close=True, dxfattribs={"layer": "FURNITURE"})
msp.add_circle((5400, 6000), 700, dxfattribs={"layer": "FURNITURE"})
for x, y in [(1100, 2000), (6400, 2000), (10900, 2400)]:
    msp.add_circle((x, y), 110, dxfattribs={"layer": "ELECTRICAL"})
msp.add_text("LIVING", height=420, dxfattribs={"layer": "TEXT"}).set_placement((3200, 3300))
msp.add_text("BEDROOM", height=420, dxfattribs={"layer": "TEXT"}).set_placement((8600, 2600))
msp.add_text("KITCHEN", height=420, dxfattribs={"layer": "TEXT"}).set_placement((3600, 6000))
dim = msp.add_linear_dim(base=(1000, 500), p1=(1000, 1000), p2=(11000, 1000),
                         dimstyle="Standard", dxfattribs={"layer": "DIMENSIONS"})
dim.render()
_ = bbox.extents(msp)  # ezdxf clears header extents on save regardless; kept for parity
save(doc, "floorplan")

# ---- minimal.dxf : one line, default version ----
doc = ezdxf.new()
doc.modelspace().add_line((0, 0), (100, 100))
save(doc, "minimal")

# ---- units_inch.dxf : INSUNITS=1 (inches), a circle ----
doc = ezdxf.new("R2010")
doc.header["$INSUNITS"] = 1
doc.modelspace().add_circle((5, 5), 2.5)
save(doc, "units_inch")

# ---- empty.dxf : valid, no entities, meters ----
doc = ezdxf.new("R2013")
doc.header["$INSUNITS"] = 6
save(doc, "empty")

# ---- crafted.dxf : hand-written, real extents + off-layer (negative color) ----
crafted = "\r\n".join([
    "0", "SECTION", "2", "HEADER",
    "9", "$ACADVER", "1", "AC1024",
    "9", "$INSUNITS", "70", "1",
    "9", "$EXTMIN", "10", "0.0", "20", "0.0", "30", "0.0",
    "9", "$EXTMAX", "10", "250.5", "20", "120.0", "30", "0.0",
    "0", "ENDSEC",
    "0", "SECTION", "2", "TABLES",
    "0", "TABLE", "2", "LAYER",
    "0", "LAYER", "2", "0", "70", "0", "62", "7",
    "0", "LAYER", "2", "HIDDEN", "70", "0", "62", "-3",   # negative => layer off
    "0", "LAYER", "2", "GRID", "70", "0", "62", "8",
    "0", "ENDTAB", "0", "ENDSEC",
    "0", "SECTION", "2", "ENTITIES",
    "0", "LINE", "8", "0", "10", "0", "20", "0", "11", "250", "21", "120",
    "0", "CIRCLE", "8", "GRID", "10", "50", "20", "50", "40", "10",
    "0", "LWPOLYLINE", "8", "HIDDEN", "90", "2", "70", "1",
    "10", "0", "20", "0", "10", "100", "20", "0",
    "0", "ENDSEC", "0", "EOF", "",
])
with open(os.path.join(FIX, "crafted.dxf"), "w", encoding="utf-8", newline="") as f:
    f.write(crafted)
write_sidecar("crafted", {
    "format": "dxf", "supported": True,
    "acadver": "AC1024", "version_name": "AutoCAD 2010",
    "insunits": 1, "units_name": "Inches",
    "extmin": [0.0, 0.0, 0.0], "extmax": [250.5, 120.0, 0.0],
    "layers": [
        {"name": "0", "color": 7},
        {"name": "GRID", "color": 8},
        {"name": "HIDDEN", "color": 3},
    ],
    "layer_count": 3,
    "entity_counts": {"LINE": 1, "CIRCLE": 1, "LWPOLYLINE": 1},
    "entity_total": 3,
})
print("wrote crafted.dxf (real extents + off-layer)")

# ---- fake.dwg : NOT a real drawing, just a DWG version header for detection ----
with open(os.path.join(FIX, "fake.dwg"), "wb") as f:
    f.write(b"AC1032" + b"\x00" * 122)
write_sidecar("fake.dwg", {
    "format": "dwg", "supported": False,
    "acadver": "AC1032", "version_name": "AutoCAD 2018",
})
print("wrote fake.dwg (detection fixture)")

# ---- binary.dxf : binary-DXF sentinel header for detection ----
with open(os.path.join(FIX, "binary.dxf"), "wb") as f:
    f.write(b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00" * 32)
write_sidecar("binary.dxf", {"format": "binary-dxf", "supported": False})
print("wrote binary.dxf (detection fixture)")

print("done.")
