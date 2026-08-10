"""macOS FSEvents fast path: replay the OS's persistent per-volume change
journal so an incremental rescan visits only the directories that actually
changed, instead of stat-ing every directory in the tree.

Entirely best-effort. Every entry point returns None off darwin, and ANY doubt
— dropped events, purged history, a different volume, a ctypes failure, a
timeout, or an implausibly large change set — also returns None, at which
point the caller falls back to the full incremental walk. That is why the
ctypes surface is wrapped so defensively: a broken/renamed CoreServices symbol
must degrade the scan's speed, never its correctness.

Ported from OpenIndex's `runner.py`; see specs/scan-incremental.md §3.
"""
import json
import os
import sys
import time

_FSE_MUST_SCAN_SUBDIRS = 0x01
# user dropped | kernel dropped | id wrapped | root changed
_FSE_BAD_FLAGS = 0x02 | 0x04 | 0x08 | 0x20
_FSE_HISTORY_DONE = 0x10


def _libs():
    if sys.platform != "darwin":
        return None
    import ctypes
    import ctypes.util
    try:
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        cs = ctypes.CDLL(
            "/System/Library/Frameworks/CoreServices.framework/CoreServices")
        return ctypes, cf, cs
    except Exception:
        return None


def current_id():
    libs = _libs()
    if not libs:
        return None
    ctypes, _cf, cs = libs
    try:
        cs.FSEventsGetCurrentEventId.restype = ctypes.c_uint64
        return int(cs.FSEventsGetCurrentEventId())
    except Exception:
        return None


def device_uuid(root):
    libs = _libs()
    if not libs:
        return None
    ctypes, cf, cs = libs
    try:
        dev = os.stat(root).st_dev
        cs.FSEventsCopyUUIDForDevice.restype = ctypes.c_void_p
        cs.FSEventsCopyUUIDForDevice.argtypes = [ctypes.c_int32]
        u = cs.FSEventsCopyUUIDForDevice(dev)
        if not u:
            return None
        cf.CFUUIDCreateString.restype = ctypes.c_void_p
        cf.CFUUIDCreateString.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        s = cf.CFUUIDCreateString(None, u)
        buf = ctypes.create_string_buffer(64)
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                          ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString(s, buf, 64, 0x08000100)
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease(s)
        cf.CFRelease(u)
        return buf.value.decode("ascii", "replace") or None
    except Exception:
        return None


def _replay(root, since_id, timeout=20.0, max_events=200_000):
    """Deliver (path, flags) for every dir changed under root since event id.
    Returns None if history is unusable or the change set is huge."""
    libs = _libs()
    if not libs:
        return None
    ctypes, cf, cs = libs
    try:
        c_void_p, c_char_p = ctypes.c_void_p, ctypes.c_char_p
        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p,
                                                 ctypes.c_uint32]
        cf.CFArrayCreate.restype = c_void_p
        cf.CFArrayCreate.argtypes = [c_void_p, ctypes.POINTER(c_void_p),
                                     ctypes.c_long, c_void_p]
        cf.CFRunLoopGetCurrent.restype = c_void_p
        cf.CFRunLoopRunInMode.restype = ctypes.c_int32
        cf.CFRunLoopRunInMode.argtypes = [c_void_p, ctypes.c_double,
                                          ctypes.c_ubyte]
        cf.CFRelease.argtypes = [c_void_p]

        CB = ctypes.CFUNCTYPE(None, c_void_p, c_void_p, ctypes.c_size_t,
                              c_void_p, ctypes.POINTER(ctypes.c_uint32),
                              ctypes.POINTER(ctypes.c_uint64))
        cs.FSEventStreamCreate.restype = c_void_p
        cs.FSEventStreamCreate.argtypes = [c_void_p, CB, c_void_p, c_void_p,
                                           ctypes.c_uint64, ctypes.c_double,
                                           ctypes.c_uint32]
        cs.FSEventStreamScheduleWithRunLoop.argtypes = [c_void_p, c_void_p,
                                                        c_void_p]
        cs.FSEventStreamStart.restype = ctypes.c_ubyte
        cs.FSEventStreamStart.argtypes = [c_void_p]
        cs.FSEventStreamStop.argtypes = [c_void_p]
        cs.FSEventStreamInvalidate.argtypes = [c_void_p]
        cs.FSEventStreamRelease.argtypes = [c_void_p]

        out = []
        state = {"done": False, "bad": False}

        def _cb(stream, info, num, paths, flags, ids):
            try:
                pp = ctypes.cast(paths, ctypes.POINTER(c_char_p))
                for i in range(num):
                    fl = flags[i]
                    if fl & _FSE_HISTORY_DONE:
                        state["done"] = True
                        continue
                    if fl & _FSE_BAD_FLAGS:
                        state["bad"] = True
                        continue
                    out.append((pp[i].decode("utf-8", "replace"), fl))
            except Exception:
                state["bad"] = True

        cb = CB(_cb)
        path_cf = cf.CFStringCreateWithCString(None, root.encode("utf-8"),
                                               0x08000100)
        vals = (c_void_p * 1)(path_cf)
        arr = cf.CFArrayCreate(None, vals, 1, None)
        stream = cs.FSEventStreamCreate(None, cb, None, arr,
                                        ctypes.c_uint64(since_id), 0.0, 0)
        if not stream:
            return None
        mode = c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
        cs.FSEventStreamScheduleWithRunLoop(stream, cf.CFRunLoopGetCurrent(),
                                            mode)
        started = bool(cs.FSEventStreamStart(stream))
        deadline = time.time() + timeout
        while (started and not state["done"] and not state["bad"]
               and len(out) < max_events and time.time() < deadline):
            cf.CFRunLoopRunInMode(mode, 0.2, 1)
        if started:
            cs.FSEventStreamStop(stream)
        cs.FSEventStreamInvalidate(stream)
        cs.FSEventStreamRelease(stream)
        cf.CFRelease(arr)
        cf.CFRelease(path_cf)
        if (not started or state["bad"] or not state["done"]
                or len(out) >= max_events):
            return None
        return out
    except Exception:
        return None


def load_states(cfg):
    """{root: {event_id, uuid, devs, updated}} — tolerates the legacy
    single-root file shape and a missing/corrupt file."""
    try:
        with open(cfg.fsevents_json) as f:
            st = json.load(f)
    except Exception:
        return {}
    if not isinstance(st, dict):
        return {}
    if isinstance(st.get("roots"), dict):
        return st["roots"]
    if st.get("root"):  # legacy single-root shape
        return {st["root"]: st}
    return {}


def save_state(cfg, root, event_id, uuid, devs):
    roots = load_states(cfg)
    roots[root] = {"event_id": event_id, "uuid": uuid,
                   "devs": sorted(devs), "updated": time.time()}
    os.makedirs(cfg.dir, exist_ok=True)
    with open(cfg.fsevents_json + ".new", "w") as f:
        json.dump({"roots": roots}, f)
    os.replace(cfg.fsevents_json + ".new", cfg.fsevents_json)


def hint(cfg, root):
    """(forced_dirs set, walk_subtrees list) of paths changed since the last
    successful scan of this same root, or None to fall back to a walk."""
    st = load_states(cfg).get(root)
    if not st or not st.get("event_id"):
        return None
    if len(st.get("devs") or []) != 1:
        return None  # multi-volume root (e.g. "/"): per-device ids don't apply
    uuid = device_uuid(root)
    if not uuid or uuid != st.get("uuid"):
        return None
    events = _replay(root, int(st["event_id"]))
    if events is None:
        return None
    rootp = root.rstrip("/") or "/"
    forced, subtrees = set(), []
    for p, fl in events:
        p = p.rstrip("/") or "/"
        if p != rootp and not p.startswith(rootp + "/"):
            continue
        if fl & _FSE_MUST_SCAN_SUBDIRS:
            subtrees.append(p)
        else:
            forced.add(p)
    return forced, subtrees
