"""Google Sheets Tabs — cell and sheet styling. Called as main(action=..., ...) by
index.html (the Styling card) and by bots (see SKILL.md). Every action is one
spreadsheets.batchUpdate call (repeatCell, updateBorders, mergeCells,
updateDimensionProperties, autoResizeDimensions, updateSheetProperties,
addConditionalFormatRule, setBasicFilter) made with the same service-account key as sheets.py.

`doc` is a Sheets URL, id or saved name; `tab` a worksheet title or gid; `range`
an A1 range on that tab ("A1:D10", "B:C", "2:5", "C3", empty = whole tab).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sheets  # noqa: E402 — shared auth, library, tab lookup (also un-shadows google.*)

ACTIONS = ("format_range", "set_borders", "merge_cells", "resize", "freeze",
           "conditional_format", "style_header", "clear_format")

# ---------------------------------------------------------------- colors

NAMED_COLORS = {
    "black": "#000000", "white": "#ffffff", "red": "#ea4335", "green": "#34a853", "blue": "#4285f4",
    "yellow": "#fbbc04", "orange": "#ff6d01", "purple": "#9334e6", "pink": "#ff63b8", "gray": "#9aa0a6",
    "grey": "#9aa0a6", "lightgray": "#e8eaed", "lightgrey": "#e8eaed", "darkgray": "#5f6368",
    "darkgrey": "#5f6368", "cyan": "#46bdc6", "teal": "#00897b", "magenta": "#e91e63", "brown": "#795548",
    "navy": "#1a237e", "darkblue": "#1a237e", "lightblue": "#cfe2f3", "darkgreen": "#137333",
    "lightgreen": "#d9ead3", "lightyellow": "#fff2cc", "lightred": "#f4cccc", "lightorange": "#fce5cd",
    "lightpurple": "#d9d2e9", "darkred": "#a50e0e", "gold": "#f1c232", "silver": "#c0c0c0",
    "lime": "#00ff00", "maroon": "#800000", "olive": "#808000", "indigo": "#3f51b5", "violet": "#8e24aa",
}
NO_COLOR = ("none", "transparent", "clear", "no", "off")


def parse_color(text):
    """'#f00', '#ff0000', 'ff0000', 'red', 'rgb(255,0,0)' -> Sheets Color dict; NO_COLOR -> None.
    Raises ValueError on anything else."""
    t = (text or "").strip().lower().replace(" ", "")
    if t in NO_COLOR:
        return None
    t = NAMED_COLORS.get(t, t)
    m = re.fullmatch(r"rgba?\((\d+),(\d+),(\d+)(?:,[\d.]+)?\)", t)
    if m:
        r, g, b = (min(255, int(x)) for x in m.groups())
    else:
        h = t.lstrip("#")
        if re.fullmatch(r"[0-9a-f]{3}", h):
            h = "".join(c * 2 for c in h)
        if not re.fullmatch(r"[0-9a-f]{6}", h):
            raise ValueError("'%s' is not a color. Use hex like #ff0000 or a name like red, lightblue, "
                             "darkgreen (or 'none' to remove)." % text)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return {"red": r / 255.0, "green": g / 255.0, "blue": b / 255.0}


def tri(value, name):
    """Tri-state flag: '' -> None (leave as is); true/false words -> bool."""
    t = str(value if value is not None else "").strip().lower()
    if t in ("", "none", "keep", "unchanged"):
        return None
    if t in ("1", "true", "yes", "on", "y"):
        return True
    if t in ("0", "false", "no", "off", "n"):
        return False
    raise ValueError("%s must be true or false (or empty to leave it unchanged), not '%s'." % (name, value))


# ---------------------------------------------------------------- A1 -> GridRange

def col_index(letters):
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def split_range(tab, rng):
    """'Sheet 1'!A1:B2 -> (tab, 'A1:B2'). The tab in the range wins only when `tab` is empty."""
    rng = (rng or "").strip()
    if "!" in rng:
        sheet, rng = rng.rsplit("!", 1)
        sheet = sheet.strip()
        if sheet.startswith("'") and sheet.endswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
        if not (tab or "").strip():
            tab = sheet
    return tab, rng.strip()


def grid_range(sheet_id, rng):
    """A1 range (no sheet name) -> GridRange. 0-based, end exclusive; missing bounds stay open."""
    gr = {"sheetId": int(sheet_id)}
    rng = (rng or "").replace("$", "").replace(" ", "")
    if not rng:
        return gr
    parts = rng.split(":")
    if len(parts) > 2:
        raise ValueError("'%s' is not an A1 range. Use e.g. A1:D10, B:C, 2:5 or C3." % rng)
    cells = []
    for p in parts:
        m = re.fullmatch(r"([A-Za-z]*)(\d*)", p)
        if not m or not (m.group(1) or m.group(2)):
            raise ValueError("'%s' is not an A1 range. Use e.g. A1:D10, B:C, 2:5 or C3." % rng)
        cells.append((col_index(m.group(1)) if m.group(1) else None, int(m.group(2)) if m.group(2) else None))
    (c1, r1), (c2, r2) = cells[0], cells[-1]
    if len(parts) == 1:          # single cell, column or row: span exactly that
        c2, r2 = c1, r1
    if c1 is not None and c2 is not None and c2 < c1:
        c1, c2 = c2, c1
    if r1 is not None and r2 is not None and r2 < r1:
        r1, r2 = r2, r1
    if r1 is not None:
        if r1 < 1:
            raise ValueError("Row numbers start at 1.")
        gr["startRowIndex"] = r1 - 1
    if r2 is not None:
        gr["endRowIndex"] = r2
    if c1 is not None:
        gr["startColumnIndex"] = c1 - 1
    if c2 is not None:
        gr["endColumnIndex"] = c2
    return gr


def range_label(tab, rng):
    return sheets.quote_title(tab["title"]) + ("!" + rng if rng else "")


# ---------------------------------------------------------------- request builders

H_ALIGN = {"left": "LEFT", "center": "CENTER", "centre": "CENTER", "middle": "CENTER", "right": "RIGHT"}
V_ALIGN = {"top": "TOP", "middle": "MIDDLE", "center": "MIDDLE", "centre": "MIDDLE", "bottom": "BOTTOM"}
WRAP = {"wrap": "WRAP", "true": "WRAP", "yes": "WRAP", "on": "WRAP", "1": "WRAP",
        "overflow": "OVERFLOW_CELL", "false": "OVERFLOW_CELL", "no": "OVERFLOW_CELL", "off": "OVERFLOW_CELL",
        "0": "OVERFLOW_CELL", "clip": "CLIP"}
NUMBER_PRESETS = {
    "number": ("NUMBER", "#,##0.00"), "integer": ("NUMBER", "#,##0"), "percent": ("PERCENT", "0.00%"),
    "currency": ("CURRENCY", "$#,##0.00"), "date": ("DATE", "yyyy-mm-dd"), "time": ("TIME", "hh:mm:ss"),
    "datetime": ("DATE_TIME", "yyyy-mm-dd hh:mm:ss"), "scientific": ("SCIENTIFIC", "0.00E+00"),
    "text": ("TEXT", "@"),
}


def number_format_of(text):
    """Preset name or a Sheets pattern -> NumberFormat; 'automatic'/'none' -> None (clears it)."""
    t = (text or "").strip()
    if t.lower() in ("automatic", "auto", "none", "clear", "plain"):
        return None
    if t.lower() in NUMBER_PRESETS:
        kind, pattern = NUMBER_PRESETS[t.lower()]
        return {"type": kind, "pattern": pattern}
    low = t.lower()
    if "y" in low or "d" in low:
        kind = "DATE_TIME" if "h" in low else "DATE"
    elif "h" in low or ("s" in low and ":" in low):
        kind = "TIME"
    elif "%" in t:
        kind = "PERCENT"
    elif "e+" in low:
        kind = "SCIENTIFIC"
    elif "@" in t:
        kind = "TEXT"
    else:
        kind = "NUMBER"
    return {"type": kind, "pattern": t}


def cell_format(bg_color="", text_color="", bold="", italic="", underline="", strikethrough="",
                font_size=0, font_family="", h_align="", v_align="", wrap="", number_format=""):
    """-> (userEnteredFormat dict, [field paths]) for repeatCell. Only given args are touched."""
    fmt, text, fields, applied = {}, {}, [], []
    if (bg_color or "").strip():
        c = parse_color(bg_color)
        if c is not None:
            fmt["backgroundColor"] = c
        fields.append("userEnteredFormat.backgroundColor")
        applied.append("background")
    if (text_color or "").strip():
        c = parse_color(text_color)
        text["foregroundColor"] = c if c is not None else {"red": 0, "green": 0, "blue": 0}
        fields.append("userEnteredFormat.textFormat.foregroundColor")
        applied.append("text color")
    for key, val in (("bold", bold), ("italic", italic), ("underline", underline),
                     ("strikethrough", strikethrough)):
        flag = tri(val, key)
        if flag is not None:
            text[key] = flag
            fields.append("userEnteredFormat.textFormat." + key)
            applied.append(key if flag else "not " + key)
    size = int(font_size or 0)
    if size:
        if not 1 <= size <= 400:
            raise ValueError("font_size must be between 1 and 400.")
        text["fontSize"] = size
        fields.append("userEnteredFormat.textFormat.fontSize")
        applied.append("size %d" % size)
    if (font_family or "").strip():
        text["fontFamily"] = font_family.strip()
        fields.append("userEnteredFormat.textFormat.fontFamily")
        applied.append(font_family.strip())
    if text:
        fmt["textFormat"] = text
    for arg, table, key, label in ((h_align, H_ALIGN, "horizontalAlignment", "h_align"),
                                   (v_align, V_ALIGN, "verticalAlignment", "v_align")):
        t = (arg or "").strip().lower()
        if t:
            if t not in table:
                raise ValueError("%s must be one of %s." % (label, ", ".join(sorted(set(table)))))
            fmt[key] = table[t]
            fields.append("userEnteredFormat." + key)
            applied.append(t)
    t = str(wrap or "").strip().lower()
    if t:
        if t not in WRAP:
            raise ValueError("wrap must be wrap, overflow or clip (or true/false).")
        fmt["wrapStrategy"] = WRAP[t]
        fields.append("userEnteredFormat.wrapStrategy")
        applied.append(WRAP[t].lower().replace("_cell", ""))
    if (number_format or "").strip():
        nf = number_format_of(number_format)
        if nf is not None:
            fmt["numberFormat"] = nf
        fields.append("userEnteredFormat.numberFormat")
        applied.append("format " + (nf["pattern"] if nf else "automatic"))
    return fmt, fields, applied


def repeat_cell(gr, fmt, fields):
    return {"repeatCell": {"range": gr, "cell": {"userEnteredFormat": fmt}, "fields": ",".join(fields)}}


BORDER_STYLES = {"solid": "SOLID", "thin": "SOLID", "medium": "SOLID_MEDIUM", "solid_medium": "SOLID_MEDIUM",
                 "thick": "SOLID_THICK", "solid_thick": "SOLID_THICK", "dashed": "DASHED", "dotted": "DOTTED",
                 "double": "DOUBLE", "none": "NONE"}
SIDE_NAMES = {"top": "top", "bottom": "bottom", "left": "left", "right": "right",
              "inner_horizontal": "innerHorizontal", "innerhorizontal": "innerHorizontal",
              "inner_vertical": "innerVertical", "innervertical": "innerVertical"}
SIDE_GROUPS = {
    "all": ["top", "bottom", "left", "right", "innerHorizontal", "innerVertical"],
    "outer": ["top", "bottom", "left", "right"], "box": ["top", "bottom", "left", "right"],
    "inner": ["innerHorizontal", "innerVertical"],
    "horizontal": ["top", "bottom", "innerHorizontal"], "vertical": ["left", "right", "innerVertical"],
}


def border_sides(sides):
    out = []
    for word in re.split(r"[,\s]+", (sides or "all").strip().lower()):
        if not word:
            continue
        if word in SIDE_GROUPS:
            out += SIDE_GROUPS[word]
        elif word in SIDE_NAMES:
            out.append(SIDE_NAMES[word])
        else:
            raise ValueError("Unknown border side '%s'. Use all, outer, inner, horizontal, vertical, or any of "
                             "top, bottom, left, right, inner_horizontal, inner_vertical." % word)
    return list(dict.fromkeys(out)) or SIDE_GROUPS["all"]


CONDITIONS = {
    # friendly name: (BooleanCondition type, number of values)
    "greater_than": ("NUMBER_GREATER", 1), ">": ("NUMBER_GREATER", 1), "gt": ("NUMBER_GREATER", 1),
    "greater_or_equal": ("NUMBER_GREATER_THAN_EQ", 1), ">=": ("NUMBER_GREATER_THAN_EQ", 1),
    "gte": ("NUMBER_GREATER_THAN_EQ", 1),
    "less_than": ("NUMBER_LESS", 1), "<": ("NUMBER_LESS", 1), "lt": ("NUMBER_LESS", 1),
    "less_or_equal": ("NUMBER_LESS_THAN_EQ", 1), "<=": ("NUMBER_LESS_THAN_EQ", 1),
    "lte": ("NUMBER_LESS_THAN_EQ", 1),
    "equals": ("EQ", 1), "=": ("EQ", 1), "eq": ("EQ", 1),
    "not_equals": ("NEQ", 1), "!=": ("NEQ", 1), "neq": ("NEQ", 1),
    "between": ("NUMBER_BETWEEN", 2), "not_between": ("NUMBER_NOT_BETWEEN", 2),
    "contains": ("TEXT_CONTAINS", 1), "text_contains": ("TEXT_CONTAINS", 1),
    "not_contains": ("TEXT_NOT_CONTAINS", 1), "text_not_contains": ("TEXT_NOT_CONTAINS", 1),
    "starts_with": ("TEXT_STARTS_WITH", 1), "ends_with": ("TEXT_ENDS_WITH", 1),
    "text_equals": ("TEXT_EQ", 1), "is_email": ("TEXT_IS_EMAIL", 0), "is_url": ("TEXT_IS_URL", 0),
    "empty": ("BLANK", 0), "blank": ("BLANK", 0), "not_empty": ("NOT_BLANK", 0), "not_blank": ("NOT_BLANK", 0),
    "date_before": ("DATE_BEFORE", 1), "date_after": ("DATE_AFTER", 1), "date_equals": ("DATE_EQ", 1),
    "formula": ("CUSTOM_FORMULA", 1), "custom_formula": ("CUSTOM_FORMULA", 1),
}


def is_number(s):
    try:
        float(str(s).replace(",", ""))
        return True
    except ValueError:
        return False


def boolean_condition(condition, value, gr=None):
    key = (condition or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not key:
        raise ValueError("Give a condition, e.g. greater_than, less_than, equals, between, contains, "
                         "empty, not_empty, date_before or formula.")
    kind, nvals = CONDITIONS.get(key, (key.upper(), 1))
    value = "" if value is None else str(value)
    if kind == "EQ":             # numbers compare as numbers, anything else as text
        kind = "NUMBER_EQ" if is_number(value) else "TEXT_EQ"
    if kind == "NEQ":
        if is_number(value):
            kind = "NUMBER_NOT_EQ"
        else:                    # grid rules have no text "not equal"; a formula on the top-left cell does it
            gr = gr or {}
            ref = "%s%d" % (sheets.col_letter(gr.get("startColumnIndex", 0) + 1), gr.get("startRowIndex", 0) + 1)
            return {"type": "CUSTOM_FORMULA",
                    "values": [{"userEnteredValue": '=%s<>"%s"' % (ref, value.replace('"', '""'))}]}
    if kind in ("DATE_BEFORE", "DATE_AFTER", "DATE_EQ"):
        rel = value.strip().upper().replace(" ", "_")
        if rel in ("TODAY", "TOMORROW", "YESTERDAY", "PAST_WEEK", "PAST_MONTH", "PAST_YEAR"):
            return {"type": kind, "values": [{"relativeDate": rel}]}
    if nvals == 0:
        return {"type": kind}
    vals = [v.strip() for v in value.split(",")] if nvals == 2 else [value.strip()]
    if nvals == 2 and len(vals) != 2:
        raise ValueError("%s needs two values in `value`, e.g. \"10,20\"." % condition)
    if any(v == "" for v in vals):
        raise ValueError("Condition '%s' needs a `value`." % condition)
    if kind == "CUSTOM_FORMULA" and not vals[0].startswith("="):
        vals[0] = "=" + vals[0]
    return {"type": kind, "values": [{"userEnteredValue": v} for v in vals]}


def auto_resize(sheet_id, dimension, start=None, end=None):
    dr = {"sheetId": int(sheet_id), "dimension": dimension}
    if start is not None:
        dr["startIndex"] = start
    if end is not None:
        dr["endIndex"] = end
    return {"autoResizeDimensions": {"dimensions": dr}}


# ---------------------------------------------------------------- main

def main(action: str = "format_range", doc: str = "", tab: str = "", range: str = "",
         bg_color: str = "", text_color: str = "", bold: str = "", italic: str = "",
         underline: str = "", strikethrough: str = "", font_size: int = 0, font_family: str = "",
         h_align: str = "", v_align: str = "", wrap: str = "", number_format: str = "",
         style: str = "solid", color: str = "#000000", sides: str = "all",
         unmerge: bool = False, merge_type: str = "all",
         pixel_size: int = 0, auto_fit: bool = False, dimension: str = "",
         rows: int = -1, cols: int = -1, condition: str = "", value: str = "",
         basic_filter: bool = False):
    args = dict(locals())
    try:
        return run(**args)
    except Exception as e:  # noqa: BLE001 — surface as data for the page/bot
        return {"error": sheets.explain_error(e, action), "action": action}


def run(action, doc, tab, range, **a):
    if action not in ACTIONS:
        raise ValueError("Unknown action: %s. Actions: %s" % (action, ", ".join(ACTIONS)))
    tab, rng = split_range(tab, range)
    sheet_id = sheets.doc_id_of(doc)
    svc = sheets.get_service()
    meta = sheets.fetch_meta(svc, sheet_id)
    tabs = sheets.flatten_tabs(meta)
    sheets.upsert_doc(sheet_id, meta.get("properties", {}).get("title", ""))
    t = sheets.find_tab(tabs, tab)
    if t["type"] != "GRID":
        raise ValueError("'%s' is a %s, not a grid of cells; it cannot be styled." % (t["title"], t["type"].lower()))
    gid = t["id"]
    gr = grid_range(gid, rng)
    requests, applied = [], []

    if action == "format_range":
        fmt, fields, applied = cell_format(
            a["bg_color"], a["text_color"], a["bold"], a["italic"], a["underline"], a["strikethrough"],
            a["font_size"], a["font_family"], a["h_align"], a["v_align"], a["wrap"], a["number_format"])
        if not fields:
            raise ValueError("Nothing to change. Give at least one of bg_color, text_color, bold, italic, "
                             "underline, strikethrough, font_size, font_family, h_align, v_align, wrap, "
                             "number_format.")
        requests.append(repeat_cell(gr, fmt, fields))

    elif action == "set_borders":
        st = (a["style"] or "solid").strip().lower()
        if st not in BORDER_STYLES:
            raise ValueError("style must be one of %s." % ", ".join(sorted(BORDER_STYLES)))
        border = {"style": BORDER_STYLES[st]}
        if border["style"] != "NONE":
            border["color"] = sheets_color_or_black(a["color"])
        req = {"range": gr}
        sides = border_sides(a["sides"])
        for side in sides:
            req[side] = border
        requests.append({"updateBorders": req})
        applied = ["%s %s border" % (st, s) for s in sides]

    elif action == "merge_cells":
        if a["unmerge"]:
            requests.append({"unmergeCells": {"range": gr}})
            applied = ["unmerged"]
        else:
            mt = {"all": "MERGE_ALL", "columns": "MERGE_COLUMNS", "rows": "MERGE_ROWS"}.get(
                (a["merge_type"] or "all").strip().lower())
            if not mt:
                raise ValueError("merge_type must be all, columns or rows.")
            if not rng:
                raise ValueError("Give the range of cells to merge, e.g. A1:D1.")
            requests.append({"mergeCells": {"range": gr, "mergeType": mt}})
            applied = ["merged (%s)" % mt.split("_")[1].lower()]

    elif action == "resize":
        dim = (a["dimension"] or "").strip().lower()
        bare = rng.replace("$", "").replace(" ", "")
        if not dim:
            if re.fullmatch(r"\d+(:\d+)?", bare):
                dim = "rows"
            else:
                dim = "columns"
        if dim in ("row", "rows"):
            api_dim, s, e = "ROWS", gr.get("startRowIndex"), gr.get("endRowIndex")
        elif dim in ("col", "cols", "column", "columns"):
            api_dim, s, e = "COLUMNS", gr.get("startColumnIndex"), gr.get("endColumnIndex")
        else:
            raise ValueError("dimension must be columns or rows.")
        size = int(a["pixel_size"] or 0)
        if not a["auto_fit"] and not size:
            raise ValueError("Give pixel_size (e.g. 120) or auto_fit=true.")
        if a["auto_fit"]:
            requests.append(auto_resize(gid, api_dim, s, e))
            applied = ["auto-fit %s" % api_dim.lower()]
        else:
            if not 1 <= size <= 10000:
                raise ValueError("pixel_size must be between 1 and 10000.")
            dr = {"sheetId": int(gid), "dimension": api_dim}
            if s is not None:
                dr["startIndex"] = s
            if e is not None:
                dr["endIndex"] = e
            requests.append({"updateDimensionProperties": {
                "range": dr, "properties": {"pixelSize": size}, "fields": "pixelSize"}})
            applied = ["%s set to %dpx" % (api_dim.lower(), size)]

    elif action == "freeze":
        gp, fields = {}, []
        if int(a["rows"]) >= 0:
            gp["frozenRowCount"] = int(a["rows"])
            fields.append("gridProperties.frozenRowCount")
            applied.append("%d frozen rows" % int(a["rows"]))
        if int(a["cols"]) >= 0:
            gp["frozenColumnCount"] = int(a["cols"])
            fields.append("gridProperties.frozenColumnCount")
            applied.append("%d frozen columns" % int(a["cols"]))
        if not fields:
            raise ValueError("Give rows and/or cols (0 unfreezes).")
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": int(gid), "gridProperties": gp}, "fields": ",".join(fields)}})

    elif action == "conditional_format":
        fmt = {}
        if (a["bg_color"] or "").strip():
            c = parse_color(a["bg_color"])
            if c is not None:
                fmt["backgroundColor"] = c
        if (a["text_color"] or "").strip():
            c = parse_color(a["text_color"])
            if c is not None:
                fmt["textFormat"] = {"foregroundColor": c}
        flag = tri(a["bold"], "bold")
        if flag is not None:
            fmt.setdefault("textFormat", {})["bold"] = flag
        if not fmt:
            raise ValueError("Give bg_color and/or text_color (or bold) for the matching cells.")
        cond = boolean_condition(a["condition"], a["value"], gr)
        requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [gr], "booleanRule": {"condition": cond, "format": fmt}}}})
        applied = ["rule %s%s" % (cond["type"], (" " + ", ".join(
            str(v.get("userEnteredValue", v.get("relativeDate", ""))) for v in cond.get("values", []))))]

    elif action == "style_header":
        # default: row 1 across the whole tab; a range picks the header cells (its rows get frozen)
        if not rng:
            gr = {"sheetId": int(gid), "startRowIndex": 0, "endRowIndex": 1}
        gr.setdefault("startRowIndex", 0)
        gr.setdefault("endRowIndex", gr["startRowIndex"] + 1)
        fmt, fields, _ = cell_format(bg_color=a["bg_color"] or "#188038", text_color=a["text_color"] or "#ffffff",
                                     bold="true", v_align="middle")
        requests.append(repeat_cell(gr, fmt, fields))
        freeze = gr["endRowIndex"]
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": int(gid), "gridProperties": {"frozenRowCount": freeze}},
            "fields": "gridProperties.frozenRowCount"}})
        requests.append(auto_resize(gid, "COLUMNS", gr.get("startColumnIndex"), gr.get("endColumnIndex")))
        applied = ["bold", "background", "text color", "%d frozen row%s" % (freeze, "" if freeze == 1 else "s"),
                   "auto-fit columns"]
        if a["basic_filter"]:    # filter over the header row and everything below it
            fr = {k: v for k, v in gr.items() if k != "endRowIndex"}
            fr["startRowIndex"] = gr["endRowIndex"] - 1
            requests.append({"setBasicFilter": {"filter": {"range": fr}}})
            applied.append("filter")

    elif action == "clear_format":
        requests.append({"repeatCell": {"range": gr, "cell": {}, "fields": "userEnteredFormat"}})
        applied = ["formatting cleared"]

    svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()
    return {"ok": True, "action": action, "tab": {"id": gid, "title": t["title"]},
            "range": range_label(t, rng), "applied": applied, "requests": len(requests),
            "url": sheets.doc_url(sheet_id, gid)}


def sheets_color_or_black(text):
    c = parse_color(text or "#000000")
    return c if c is not None else {"red": 0, "green": 0, "blue": 0}
