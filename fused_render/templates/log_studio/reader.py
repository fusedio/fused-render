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
_APACHE_RE = re.compile(
    r"\[(\d{1,2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})\]"
)
_SYSLOG_RE = re.compile(
    r"^(?:<\d+>)?\s*([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\b"
)
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


def _epoch(value, formats):
    # The date regexes accept a "T" between date and time but the strptime
    # formats are space-separated; digit-T-digit keeps month names intact.
    normalized = re.sub(r"(?<=\d)T(?=\d)", " ", value.replace(",", "."))
    for fmt in formats:
        try:
            parsed = datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            pass
    return None


def _timestamp(text):
    match = _ISO_RE.search(text)
    if match:
        display = match.group(1)
        value = display.replace(",", ".")
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp(), display, "iso"
        except ValueError:
            pass

    match = _APACHE_RE.search(text)
    if match:
        display = match.group(1)
        try:
            return datetime.strptime(display, "%d/%b/%Y:%H:%M:%S %z").timestamp(), display, "apache"
        except ValueError:
            pass

    match = _SYSLOG_RE.search(text)
    if match:
        display = match.group(1)
        try:
            now = datetime.now(timezone.utc)
            parsed = datetime.strptime(f"{now.year} {display}", "%Y %b %d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            if parsed.timestamp() > now.timestamp() + 86400:
                parsed = parsed.replace(year=now.year - 1)
            return parsed.timestamp(), display, "syslog"
        except ValueError:
            pass

    match = _YMD_RE.search(text)
    if match:
        display = match.group(1)
        value = _epoch(
            display,
            (
                "%Y/%m/%d %H:%M:%S.%f",
                "%Y/%m/%d %H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S",
            ),
        )
        if value is not None:
            return value, display, "datetime"

    match = _MDY_RE.search(text)
    if match:
        display = match.group(1)
        value = _epoch(
            display,
            (
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
        )
        if value is not None:
            return value, display, "datetime"

    match = _TEXT_DATE_RE.search(text)
    if match:
        display = match.group(1)
        value = _epoch(display, ("%d-%b-%Y %H:%M:%S", "%d %b %Y %H:%M:%S"))
        if value is not None:
            return value, display, "datetime"
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

    def matches(text, level, epoch):
        # `epoch` is the line's effective time: its own timestamp, or the one
        # inherited from the record it continues (stack traces and other
        # untimestamped continuation lines), so brushing a range keeps the
        # context attached to an in-range event instead of dropping it.
        if not matches_query(text) or (wanted and (level or "OTHER") not in wanted):
            return False
        # A continuation line whose parent timestamp is unknown — the first
        # lines of a scan, or a record split across a tail chunk boundary —
        # isn't excluded by the range: hiding the stack trace of an in-range
        # event is worse than occasionally keeping a boundary line.
        if epoch is None and level is None:
            return True
        if from_epoch and (epoch is None or epoch < from_epoch):
            return False
        if to_epoch and (epoch is None or epoch > to_epoch):
            return False
        return True

    return matches


def _effective_epoch(stamp, last_epoch):
    """A line's own epoch, or the previous timestamped line's for an
    untimestamped continuation line."""
    return stamp[0] if stamp else last_epoch


def _counts():
    return {level: 0 for level in _LEVELS}


def _row(offset, text, stamp, level, truncated=False):
    return {
        "offset": offset,
        "text": text,
        "timestamp": stamp[1] if stamp else None,
        "epoch": stamp[0] if stamp else None,
        "level": level or "OTHER",
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
    with open(file, "rb") as source:
        while line_count < _SCAN_LINES and time.monotonic() < deadline:
            raw, clipped = _readline(source, deadline)
            if not raw:
                break
            line_count += 1
            clipped_lines += int(clipped)
            text = _text(raw)
            level = _level(text)
            level_counts[level or "OTHER"] += 1
            stamp = _timestamp(text)
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


def _listdir(file, path):
    path = _clean_path(path)
    if path:
        directory = os.path.abspath(os.path.expanduser(path))
    elif file:
        directory = os.path.dirname(os.path.abspath(file))
    else:
        directory = os.path.expanduser("~")
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
            # inherit the timestamp of the record above it; the collection sweep
            # below runs backward, which can't see a line's parent on its own.
            texts, stamps, line_levels, effs = [None] * len(parts), [None] * len(parts), [None] * len(parts), [None] * len(parts)
            chunk_epoch = None
            for i in range(first, len(parts)):
                t = _text(parts[i][:_MAX_LINE_BYTES])
                s = _timestamp(t)
                texts[i], stamps[i], line_levels[i] = t, s, _level(t)
                if s:
                    chunk_epoch = s[0]
                effs[i] = _effective_epoch(s, chunk_epoch)
            for index in range(len(parts) - 1, first - 1, -1):
                if offsets[index] == size and not parts[index]:
                    continue
                scanned += 1
                clipped = len(parts[index]) > _MAX_LINE_BYTES
                clipped_lines += int(clipped)
                text, stamp, level = texts[index], stamps[index], line_levels[index]
                if matches(text, level, effs[index]):
                    if len(rows) == limit:
                        has_more = True
                        break
                    rows.append(_row(offsets[index], text, stamp, level, clipped))
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
    last_epoch = None
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
            if stamp:
                last_epoch = stamp[0]
            if not matches(text, level, _effective_epoch(stamp, last_epoch)):
                continue
            row = _row(offset, text, stamp, level, clipped)
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
    timestamped_count = 0
    level_counts = _counts()
    scanned = 0
    clipped_lines = 0
    last_epoch = None
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
            if stamp:
                last_epoch = stamp[0]
            if not matches(text, level, _effective_epoch(stamp, last_epoch)):
                continue
            matching_count += 1
            normalized_level = level or "OTHER"
            level_counts[normalized_level] += 1
            if stamp is None:
                continue
            timestamped_count += 1
            index = math.floor(stamp[0] / width)
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
        "timestamped_count": timestamped_count,
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
    last_epoch = None
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
            if stamp:
                last_epoch = stamp[0]
            if not matches(text, level, _effective_epoch(stamp, last_epoch)):
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
            lines.append({"offset": offset, "text": _text(raw), "target": is_target, "truncated": clipped})
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
    **params: str,
) -> dict:
    if op == "list":
        return _listdir(file, path)
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
    return {"error": f"Unknown operation: {op}", "operations": ["overview", "list", "resolve", "page", "histogram", "patterns", "context"]}
