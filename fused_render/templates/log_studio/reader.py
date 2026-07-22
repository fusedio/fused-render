# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
# drain3 is deliberately NOT a hard dependency: it powers only pattern mining,
# which _patterns() degrades gracefully without (an install-hint fallback).
# Declaring it here would let a failed/blocked install of that one extra break
# every op the reader serves, so it stays optional (shipped via the bundled
# extra / present in the app venv).

import math
import os
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone

_LEVELS = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL", "OTHER")
_LEVEL_MAP = {
    "TRACE": "TRACE",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "NOTICE": "INFO",
    "WARN": "WARN",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "ERR": "ERROR",
    "SEVERE": "ERROR",
    "FATAL": "FATAL",
    "CRITICAL": "FATAL",
    "CRIT": "FATAL",
    "ALERT": "FATAL",
    "EMERG": "FATAL",
    "EMERGENCY": "FATAL",
    "PANIC": "FATAL",
}
_LEVEL_RE = re.compile(r"\b(" + "|".join(_LEVEL_MAP) + r")\b", re.IGNORECASE)
_ISO_RE = re.compile(
    r"(?<!\d)(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)"
)
_APACHE_RE = re.compile(r"\[(\d{1,2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})\]")
_SYSLOG_RE = re.compile(r"^(?:<\d+>)?\s*([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\b")
_YMD_RE = re.compile(
    r"(?<!\d)(\d{4}[/-]\d{1,2}[/-]\d{1,2}[ T]\d{1,2}:\d{2}:\d{2}"
    r"(?:[.,]\d{1,6})?)(?!\d)"
)
_MDY_RE = re.compile(
    r"(?<!\d)(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}[ T]\d{1,2}:\d{2}:\d{2}"
    r"(?:[.,]\d{1,6})?)(?!\d)"
)
_TEXT_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2}[- ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[- ]\d{4}[ T]\d{1,2}:\d{2}:\d{2})(?!\d)",
    re.IGNORECASE,
)
_SCAN_SECONDS = 18.0
_SCAN_LINES = 1_000_000
_MAX_LINE_BYTES = 64 * 1024


def _text(raw):
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    return raw.decode("utf-8", errors="replace")


def _readline(source, deadline):
    raw = source.readline(_MAX_LINE_BYTES + 1)
    if not raw:
        return b"", False
    clipped = len(raw) > _MAX_LINE_BYTES
    head = raw[:_MAX_LINE_BYTES] if clipped else raw
    # Consume the rest of an over-long line so the next read starts at a real
    # line boundary; leaving the cursor mid-line would misalign every offset,
    # level, and timestamp that follows. If the scan deadline expires while
    # skipping, jump to EOF so the caller stops cleanly rather than mid-line.
    while len(raw) > _MAX_LINE_BYTES and not raw.endswith(b"\n"):
        if time.monotonic() >= deadline:
            source.seek(0, os.SEEK_END)
            break
        raw = source.readline(_MAX_LINE_BYTES + 1)
    return head, clipped


def _from_iso(display):
    value = display.replace(",", ".")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)  # +0530 -> +05:30
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _from_apache(display):
    return datetime.strptime(display, "%d/%b/%Y:%H:%M:%S %z").timestamp()


def _from_syslog(display):
    now = datetime.now(timezone.utc)
    parsed = datetime.strptime(f"{now.year} {display}", "%Y %b %d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    if parsed.timestamp() > now.timestamp() + 86400:  # no year in a syslog stamp
        parsed = parsed.replace(year=now.year - 1)
    return parsed.timestamp()


def _strptime(*formats):
    def parse(display):
        value = re.sub(r"(?<=\d)T(?=\d)", " ", display.replace(",", "."))
        for fmt in formats:
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
        return None

    return parse


# Ordered timestamp strategies: the first regex to match a line wins, and its
# parser turns the match into epoch seconds (UTC), or raises / returns None so
# the next strategy is tried. Same-shaped stamps share the "datetime" label.
_PARSERS = (
    ("iso", _ISO_RE, _from_iso),
    ("apache", _APACHE_RE, _from_apache),
    ("syslog", _SYSLOG_RE, _from_syslog),
    (
        "datetime",
        _YMD_RE,
        _strptime(
            "%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"
        ),
    ),
    (
        "datetime",
        _MDY_RE,
        _strptime(
            "%m/%d/%Y %H:%M:%S.%f",
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%y %H:%M:%S.%f",
            "%m/%d/%y %H:%M:%S",
            "%d/%m/%Y %H:%M:%S.%f",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%y %H:%M:%S.%f",
            "%d/%m/%y %H:%M:%S",
            "%m-%d-%Y %H:%M:%S.%f",
            "%m-%d-%Y %H:%M:%S",
            "%m-%d-%y %H:%M:%S.%f",
            "%m-%d-%y %H:%M:%S",
            "%d-%m-%Y %H:%M:%S.%f",
            "%d-%m-%Y %H:%M:%S",
            "%d-%m-%y %H:%M:%S.%f",
            "%d-%m-%y %H:%M:%S",
        ),
    ),
    ("datetime", _TEXT_DATE_RE, _strptime("%d-%b-%Y %H:%M:%S", "%d %b %Y %H:%M:%S")),
)


def _timestamp(text):
    for name, regex, parse in _PARSERS:
        match = regex.search(text)
        if not match:
            continue
        display = match.group(1)
        try:
            epoch = parse(display)
        except ValueError:
            epoch = None
        if epoch is not None:
            return epoch, display, name
    return None


def _level(text):
    match = _LEVEL_RE.search(text)
    return _LEVEL_MAP[match.group(1).upper()] if match else None


def _query_matcher(q):
    q = q.strip()
    if q.startswith("re:"):
        pattern = re.compile(q[3:])
        return lambda text: pattern.search(text) is not None
    if len(q) > 2 and q.startswith("/") and q.endswith("/"):
        pattern = re.compile(q[1:-1])
        return lambda text: pattern.search(text) is not None
    needle = q.casefold()
    return lambda text: not needle or needle in text.casefold()


def _filters(q, levels, from_epoch, to_epoch):
    matches_query = _query_matcher(q)
    wanted = {
        _LEVEL_MAP.get(value.strip().upper(), value.strip().upper())
        for value in levels.split(",")
        if value.strip()
    }

    def matches(text, effective):
        # `effective` is the line's (epoch, level): its own values, or the
        # ones inherited from the record it continues (stack traces and other
        # continuation lines), so level facets and time brushes keep the
        # context attached to a matching event instead of dropping it.
        epoch, level = effective
        if not matches_query(text):
            return False
        # A continuation line whose parent is unknown — the first lines of a
        # scan, or a record split across a tail chunk boundary — is not
        # excluded by level or range: hiding the stack trace of a matching
        # event is worse than occasionally keeping a boundary line.
        if epoch is None and level is None:
            return True
        if wanted and (level or "OTHER") not in wanted:
            return False
        if from_epoch and (epoch is None or epoch < from_epoch):
            return False
        if to_epoch and (epoch is None or epoch > to_epoch):
            return False
        return True

    return matches


def _effective(stamp, level, last):
    """A line's effective (epoch, level): an untimestamped line inherits the
    enclosing record's epoch, and a continuation line (no timestamp, no level)
    inherits its level too."""
    if stamp:
        return stamp[0], level
    if level:
        return last[0], level
    return last


def _counts():
    return {level: 0 for level in _LEVELS}


def _row(offset, text, stamp, level, effective, truncated=False):
    return {
        "offset": offset,
        "text": text,
        "timestamp": stamp[1] if stamp else None,
        "epoch": stamp[0] if stamp else None,
        # the badge wears the effective level: a continuation line shows the
        # level it inherited (and was filtered under), not OTHER
        "level": effective[1] or "OTHER",
        "continuation": stamp is None and level is None,
        "truncated": truncated,
    }


def _seek_line(file_obj, offset, size):
    offset = max(0, min(int(offset), size))
    if offset:
        file_obj.seek(offset - 1)
        previous = file_obj.read(1)
        file_obj.seek(offset)
        if previous != b"\n":
            _readline(file_obj, time.monotonic() + _SCAN_SECONDS)
    return file_obj.tell()


def _overview(file):
    size = os.path.getsize(file)
    path = os.path.abspath(file)
    deadline = time.monotonic() + _SCAN_SECONDS
    level_counts = _counts()
    format_counts = {}
    line_count = 0
    minimum = None
    maximum = None
    clipped_lines = 0
    last = (None, None)
    with open(file, "rb") as source:
        while line_count < _SCAN_LINES and time.monotonic() < deadline:
            raw, clipped = _readline(source, deadline)
            if not raw:
                break
            line_count += 1
            clipped_lines += int(clipped)
            text = _text(raw)
            stamp = _timestamp(text)
            level = _level(text)
            effective = _effective(stamp, level, last)
            if stamp or level:
                last = effective
            level_counts[effective[1] or "OTHER"] += 1
            if stamp:
                format_counts[stamp[2]] = format_counts.get(stamp[2], 0) + 1
                if minimum is None or stamp[0] < minimum[0]:
                    minimum = stamp
                if maximum is None or stamp[0] > maximum[0]:
                    maximum = stamp
        truncated = source.tell() < size
    detected = max(format_counts, key=format_counts.get) if format_counts else None
    return {
        "line_count": line_count,
        "path": path,
        "mtime": os.path.getmtime(file),
        "byte_size": size,
        "size": size,
        "timestamp_format": detected,
        "min_epoch": minimum[0] if minimum else None,
        "min_timestamp": minimum[1] if minimum else None,
        "max_epoch": maximum[0] if maximum else None,
        "max_timestamp": maximum[1] if maximum else None,
        "from": minimum[0] if minimum else None,
        "to": maximum[0] if maximum else None,
        "levels": level_counts,
        "clipped_lines": clipped_lines,
        "truncated": truncated,
    }


def _clean_path(path):
    return (path or "").strip().strip("\"'").strip()


# --- mount-safe directory listing ------------------------------------------
# A kernel listing (os.scandir/os.listdir/os.walk) on a path under a remote
# rclone NFS mount forces rclone to enumerate the ENTIRE parent S3 prefix and
# can DROP the mount, wedging the server. This template stays mount-AGNOSTIC:
# it never imports shell.mounts and never matches mount paths. Instead the UI
# passes `src` (server origin + /api/fs/raw?path=) and we ask the server whether
# a path is remote (/api/fs/stat); if so we list it via the mount-routed,
# paginated /api/fs/list — never through the kernel. _server_url + _stat are
# copied verbatim from pyramid/overview_pyramid.py.
import json as _json
import urllib.error as _urlerr
import urllib.parse as _urlparse
import urllib.request as _urlreq


def _server_url(src, endpoint, path):
    u = _urlparse.urlsplit(src)
    return f"{u.scheme}://{u.netloc}{endpoint}?path=" + _urlparse.quote(path)


def _stat(src, path):
    url = _server_url(src, "/api/fs/stat", path)
    try:
        with _urlreq.urlopen(url, timeout=10) as r:
            return ("ok", _json.load(r))
    except _urlerr.HTTPError as e:
        if e.code == 404:
            return ("missing", None)
        return ("unreachable", None)
    except Exception:  # noqa: BLE001 — any network error -> fall back to local
        return ("unreachable", None)


def _remote_dir(src, path):
    """True iff the server says `path` is a remote (mount-backed) directory.
    No src / unreachable / missing -> False (presume local, kernel listing OK)."""
    if not src or not path:
        return False
    status, meta = _stat(src, path)
    return status == "ok" and bool(meta.get("remote"))


def _list_remote(src, path, cap=5000):
    """List `path` via the server's mount-routed, paginated /api/fs/list — never
    the kernel. Follows the cursor up to `cap` entries so a huge S3 prefix
    returns a bounded page set instead of tripping the NFS deadman."""
    entries, cursor, truncated = [], "", False
    while True:
        url = _server_url(src, "/api/fs/list", path)
        if cursor:
            url += "&cursor=" + _urlparse.quote(cursor)
        with _urlreq.urlopen(url, timeout=30) as r:
            payload = _json.load(r)
        entries.extend(payload.get("entries") or [])
        truncated = bool(payload.get("truncated"))
        cursor = payload.get("cursor") or ""
        if len(entries) >= cap or not truncated or not cursor:
            break
    return entries, truncated


def _listdir(file, path, src=""):
    path = _clean_path(path)
    if path:
        directory = os.path.abspath(os.path.expanduser(path))
    elif file:
        directory = os.path.dirname(os.path.abspath(file))
    else:
        directory = os.path.expanduser("~")
    if _remote_dir(src, directory):
        # Mount-backed dir: list via /api/fs/list, never a kernel scan.
        entries = []
        try:
            ents, _ = _list_remote(src, directory, cap=1000)
        except Exception:  # noqa: BLE001
            ents = []
        for ent in ents:
            if ent["name"].startswith("."):
                continue
            is_dir = bool(ent.get("is_dir"))
            entries.append(
                {
                    "name": ent["name"],
                    "path": os.path.join(directory, ent["name"]).replace(os.sep, "/"),
                    "is_dir": is_dir,
                    "size": None if is_dir else (ent.get("size") or 0),
                }
            )
            if len(entries) >= 1000:
                break
        entries.sort(key=lambda item: (not item["is_dir"], item["name"].casefold()))
        parent = os.path.dirname(directory)
        return {
            "path": directory.replace(os.sep, "/"),
            "parent": parent.replace(os.sep, "/") if parent and parent != directory else None,
            "entries": entries,
            "truncated": len(entries) >= 1000,
        }
    if not os.path.isdir(directory):
        directory = os.path.dirname(directory) or directory
    entries = []
    with os.scandir(directory) as scanned:
        for entry in scanned:
            if entry.name.startswith("."):
                continue
            is_dir = entry.is_dir()
            if not is_dir and not entry.is_file():
                continue
            try:
                size = None if is_dir else entry.stat().st_size
            except OSError:
                continue
            entries.append(
                {
                    "name": entry.name,
                    "path": entry.path.replace(os.sep, "/"),
                    "is_dir": is_dir,
                    "size": size,
                }
            )
            if len(entries) >= 1000:
                break
    entries.sort(key=lambda item: (not item["is_dir"], item["name"].casefold()))
    parent = os.path.dirname(directory)
    return {
        "path": directory.replace(os.sep, "/"),
        "parent": parent.replace(os.sep, "/") if parent and parent != directory else None,
        "entries": entries,
        "truncated": len(entries) >= 1000,
    }


def _resolve(path):
    target = os.path.abspath(os.path.expanduser(_clean_path(path)))
    exists = os.path.exists(target)
    return {
        "path": target.replace(os.sep, "/"),
        "exists": exists,
        "is_dir": exists and os.path.isdir(target),
    }


def _tail_page(file, limit, q, levels, from_epoch, to_epoch):
    size = os.path.getsize(file)
    deadline = time.monotonic() + _SCAN_SECONDS
    matches = _filters(q, levels, from_epoch, to_epoch)
    rows = []
    position = size
    carry = b""
    scanned = 0
    has_more = False
    truncated = False
    clipped_lines = 0
    with open(file, "rb") as source:
        while position and scanned < _SCAN_LINES and time.monotonic() < deadline:
            chunk_start = max(0, position - 65536)
            source.seek(chunk_start)
            data = source.read(position - chunk_start) + carry
            parts = data.split(b"\n")
            offsets = []
            offset = chunk_start
            for part in parts:
                offsets.append(offset)
                offset += len(part) + 1
            first = 0 if chunk_start == 0 else 1
            # Forward pass (file order) over the chunk so a continuation line can
            # inherit the timestamp and level of the record above it; the collection
            # sweep below runs backward, which can't see a line's parent on its own.
            texts, stamps, line_levels, effs = (
                [None] * len(parts),
                [None] * len(parts),
                [None] * len(parts),
                [None] * len(parts),
            )
            chunk_last = (None, None)
            for i in range(first, len(parts)):
                t = _text(parts[i][:_MAX_LINE_BYTES])
                s = _timestamp(t)
                lv = _level(t)
                texts[i], stamps[i], line_levels[i] = t, s, lv
                effs[i] = _effective(s, lv, chunk_last)
                if s or lv:
                    chunk_last = effs[i]
            for index in range(len(parts) - 1, first - 1, -1):
                if offsets[index] == size and not parts[index]:
                    continue
                scanned += 1
                clipped = len(parts[index]) > _MAX_LINE_BYTES
                clipped_lines += int(clipped)
                text, stamp, level = texts[index], stamps[index], line_levels[index]
                if matches(text, effs[index]):
                    if len(rows) == limit:
                        has_more = True
                        break
                    rows.append(_row(offsets[index], text, stamp, level, effs[index], clipped))
                if scanned >= _SCAN_LINES or time.monotonic() >= deadline:
                    break
            if has_more or scanned >= _SCAN_LINES or time.monotonic() >= deadline:
                break
            carry = parts[0] if chunk_start else b""
            if len(carry) > 8 * 1024 * 1024:
                truncated = True
                break
            position = chunk_start
        if position and not has_more:
            truncated = True
    rows.reverse()
    return {
        "rows": rows,
        "next_offset": size,
        "has_more": has_more or truncated,
        "scanned_lines": scanned,
        "clipped_lines": clipped_lines,
        "truncated": truncated,
    }


def _page(file, page, limit, q, levels, from_epoch, to_epoch, tail):
    size = os.path.getsize(file)
    limit = max(1, min(int(limit), 1000))
    if tail:
        return _tail_page(file, limit, q, levels, from_epoch, to_epoch)
    deadline = time.monotonic() + _SCAN_SECONDS
    matches = _filters(q, levels, from_epoch, to_epoch)
    rows = []
    scanned = 0
    has_more = False
    last_end = None
    clipped_lines = 0
    last = (None, None)
    with open(file, "rb") as source:
        _seek_line(source, page, size)
        while scanned < _SCAN_LINES and time.monotonic() < deadline:
            offset = source.tell()
            raw, clipped = _readline(source, deadline)
            if not raw:
                break
            scanned += 1
            clipped_lines += int(clipped)
            text = _text(raw)
            stamp = _timestamp(text)
            level = _level(text)
            effective = _effective(stamp, level, last)
            if stamp or level:
                last = effective
            if not matches(text, effective):
                continue
            row = _row(offset, text, stamp, level, effective, clipped)
            end = source.tell()
            if len(rows) < limit:
                rows.append(row)
                last_end = end
            else:
                has_more = True
                break
        stopped_at = source.tell()
        truncated = stopped_at < size and not has_more

    return {
        "rows": rows,
        "next_offset": last_end if last_end is not None else stopped_at,
        "has_more": has_more or truncated,
        "scanned_lines": scanned,
        "clipped_lines": clipped_lines,
        "truncated": truncated,
    }


def _merge_buckets(buckets):
    merged = {}
    for index, bucket in buckets.items():
        target = merged.setdefault(index // 2, {"count": 0, "levels": _counts()})
        target["count"] += bucket["count"]
        for level, count in bucket["levels"].items():
            target["levels"][level] += count
    return merged


def _histogram(file, bins, q, levels, from_epoch, to_epoch):
    size = os.path.getsize(file)
    target_bins = max(10, min(int(bins), 200))
    deadline = time.monotonic() + _SCAN_SECONDS
    matches = _filters(q, levels, from_epoch, to_epoch)
    buckets = {}
    width = 1.0
    matching_count = 0
    level_counts = _counts()
    scanned = 0
    clipped_lines = 0
    last = (None, None)
    with open(file, "rb") as source:
        while scanned < _SCAN_LINES and time.monotonic() < deadline:
            raw, clipped = _readline(source, deadline)
            if not raw:
                break
            scanned += 1
            clipped_lines += int(clipped)
            text = _text(raw)
            stamp = _timestamp(text)
            level = _level(text)
            effective = _effective(stamp, level, last)
            if stamp or level:
                last = effective
            if not matches(text, effective):
                continue
            matching_count += 1
            normalized_level = effective[1] or "OTHER"
            level_counts[normalized_level] += 1
            if effective[0] is None:
                continue
            index = math.floor(effective[0] / width)
            bucket = buckets.setdefault(index, {"count": 0, "levels": _counts()})
            bucket["count"] += 1
            bucket["levels"][normalized_level] += 1
            while buckets and max(buckets) - min(buckets) + 1 > target_bins:
                buckets = _merge_buckets(buckets)
                width *= 2.0

        truncated = source.tell() < size
    output = []
    bucket_range = range(min(buckets), max(buckets) + 1) if buckets else ()
    for index in bucket_range:
        bucket = buckets.get(index, {"count": 0, "levels": _counts()})
        output.append(
            {
                "start": index * width,
                "end": (index + 1) * width,
                "count": bucket["count"],
                "levels": bucket["levels"],
            }
        )
    return {
        "bins": output,
        "bucket_seconds": width,
        "matching_count": matching_count,
        "levels": level_counts,
        "scanned_lines": scanned,
        "clipped_lines": clipped_lines,
        "truncated": truncated,
    }


def _pattern_query(template):
    return "re:^" + ".*".join(re.escape(part) for part in template.split("<*>")) + "$"


def _patterns(file, limit, q, levels, from_epoch, to_epoch):
    try:
        from drain3 import TemplateMiner
        from drain3.template_miner_config import TemplateMinerConfig
    except ImportError:
        return {
            "available": False,
            "message": "Install drain3>=0.9.11 to enable pattern mining.",
            "patterns": [],
            "truncated": False,
        }

    config = TemplateMinerConfig()
    config.drain_max_clusters = 1000
    config.profiling_enabled = False
    miner = TemplateMiner(config=config)
    matches = _filters(q, levels, from_epoch, to_epoch)
    representatives = OrderedDict()
    size = os.path.getsize(file)
    deadline = time.monotonic() + _SCAN_SECONDS
    scanned = 0
    matching_count = 0
    clipped_lines = 0
    last = (None, None)
    with open(file, "rb") as source:
        while scanned < 250_000 and time.monotonic() < deadline:
            raw, clipped = _readline(source, deadline)
            if not raw:
                break
            scanned += 1
            clipped_lines += int(clipped)
            text = _text(raw)
            stamp = _timestamp(text)
            level = _level(text)
            effective = _effective(stamp, level, last)
            if stamp or level:
                last = effective
            if not matches(text, effective):
                continue
            matching_count += 1
            result = miner.add_log_message(text)
            cluster_id = result["cluster_id"]
            if cluster_id not in representatives:
                representatives[cluster_id] = text
                if len(representatives) > 2000:
                    representatives.popitem(last=False)
        truncated = source.tell() < size

    patterns = []
    for cluster in miner.drain.clusters:
        template = cluster.get_template()
        patterns.append(
            {
                "count": cluster.size,
                "template": template,
                "representative": representatives.get(cluster.cluster_id, ""),
                "sample": representatives.get(cluster.cluster_id, ""),
                "query": _pattern_query(template),
            }
        )
    patterns.sort(key=lambda item: (-item["count"], item["template"]))
    return {
        "available": True,
        "query": q,
        "patterns": patterns[: max(1, min(int(limit), 500))],
        "matching_count": matching_count,
        "total_matches": matching_count,
        "scanned_lines": scanned,
        "clipped_lines": clipped_lines,
        "truncated": truncated,
    }


def _context(file, page, context):
    size = os.path.getsize(file)
    target = max(0, min(int(page), size))
    count = max(0, min(int(context), 200))
    deadline = time.monotonic() + _SCAN_SECONDS
    newlines = []
    position = target
    searched = 0
    search_limit = 8 * 1024 * 1024
    with open(file, "rb") as source:
        while position and len(newlines) <= count and searched < search_limit:
            chunk_start = max(0, position - 65536)
            source.seek(chunk_start)
            chunk = source.read(position - chunk_start)
            searched += len(chunk)
            end = len(chunk)
            while len(newlines) <= count:
                index = chunk.rfind(b"\n", 0, end)
                if index < 0:
                    break
                newlines.append(chunk_start + index)
                end = index
            position = chunk_start
            if time.monotonic() >= deadline:
                break
        start = newlines[count] + 1 if len(newlines) > count else position
        truncated = position > 0 and len(newlines) <= count
        source.seek(start)
        lines = []
        found_target = False
        after = 0
        # Bound the window: count lines before the target, the target, count
        # after. Without this cap a stale/rotated target offset that never
        # matches any line would read all the way to EOF (a big payload on an
        # actively written log).
        max_lines = 2 * count + 2
        while time.monotonic() < deadline and len(lines) < max_lines:
            offset = source.tell()
            raw, clipped = _readline(source, deadline)
            if not raw:
                break
            end = source.tell()
            is_target = offset <= target < end
            if found_target:
                if after >= count:
                    break
                after += 1
            lines.append(
                {"offset": offset, "text": _text(raw), "target": is_target, "truncated": clipped}
            )
            if is_target:
                found_target = True
        if not found_target:
            truncated = True
        if time.monotonic() >= deadline and source.tell() < size:
            truncated = True
    return {"offset": target, "lines": lines, "truncated": truncated}


def main(
    file: str,
    op: str = "overview",
    page: int = 0,
    limit: int = 200,
    q: str = "",
    level: str = "",
    from_epoch: float = 0.0,
    to_epoch: float = 0.0,
    tail: bool = False,
    bins: int = 100,
    context: int = 5,
    path: str = "",
    src: str = "",
    **params: str,
) -> dict:
    if op == "list":
        return _listdir(file, path, src)
    if op == "resolve":
        return _resolve(path)
    from_epoch = float(params.get("from") or from_epoch)
    to_epoch = float(params.get("to") or to_epoch)
    if op == "overview":
        return _overview(file)
    if op == "page":
        return _page(file, page, limit, q, level, from_epoch, to_epoch, tail)
    if op == "histogram":
        return _histogram(file, bins, q, level, from_epoch, to_epoch)
    if op == "patterns":
        return _patterns(file, limit, q, level, from_epoch, to_epoch)
    if op == "context":
        return _context(file, page, context)
    return {
        "error": f"Unknown operation: {op}",
        "operations": ["overview", "list", "resolve", "page", "histogram", "patterns", "context"],
    }
