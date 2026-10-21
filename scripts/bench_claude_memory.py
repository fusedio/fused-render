#!/usr/bin/env python3
"""Measure what each Claude chat/task session costs in resident memory.

Default: one `ps` snapshot, grouped into one tree per session host
(`claude_agent/session_host.py`), with per-class MB: host, claude, the fused
permission MCP, and any other MCP servers claude launched.

`--spawn N` launches N throwaway `claude -p` stream-json sessions (default or
`--mode strict`, which adds `--strict-mcp-config` with an empty server list),
samples their tree RSS and prints the peak. It makes real model calls, so it is
a manual tool and no test exercises it.

RSS is KB from macOS `ps`; it double-counts pages shared between processes.
Stdlib only.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import namedtuple

Proc = namedtuple("Proc", "pid ppid rss_kb cmd")

CLASSES = ("host", "claude", "fused-permission-mcp", "other-mcp")
_AI_WARM_MARK = "fused_render_ai_sp"


def parse_ps(text):
    """Parse `ps -axo pid=,ppid=,rss=,command=` output; malformed lines are skipped."""
    procs = []
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            procs.append(Proc(int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
        except ValueError:
            continue
    return procs


def children_map(procs):
    out = {}
    for p in procs:
        out.setdefault(p.ppid, []).append(p)
    return out


def _is_claude(cmd):
    argv0 = cmd.split(None, 1)[0] if cmd.split() else ""
    return os.path.basename(argv0) == "claude" or " -p --input-format stream-json" in cmd


def _own_class(proc):
    cmd = proc.cmd
    if _AI_WARM_MARK in cmd:
        return "other"  # fused.ai warm process: reported apart, never a session
    if "claude_agent/session_host.py" in cmd:
        return "host"
    if "claude_agent/permission_server.py" in cmd:
        return "fused-permission-mcp"
    if _is_claude(cmd):
        return "claude"
    return None


def classify(proc, procs=()):
    own = _own_class(proc)
    if own:
        return own
    by_pid = {p.pid: p for p in procs}
    seen = set()
    cur = by_pid.get(proc.ppid)
    while cur is not None and cur.pid not in seen:
        seen.add(cur.pid)
        if _own_class(cur) == "claude":
            return "other-mcp"
        cur = by_pid.get(cur.ppid)
    return "other"


def session_trees(procs, only_under=None):
    """One tree per host, sorted by pid: a list of [(Proc, class), ...] with the host first."""
    kids = children_map(procs)
    trees = []
    for host in sorted((p for p in procs if _own_class(p) == "host"), key=lambda p: p.pid):
        if only_under and only_under not in host.cmd:
            continue
        tree, stack, seen = [], [host], set()
        while stack:
            cur = stack.pop()
            if cur.pid in seen:
                continue
            seen.add(cur.pid)
            if cur is not host and _AI_WARM_MARK in cur.cmd:
                continue
            tree.append((cur, classify(cur, procs)))
            stack.extend(kids.get(cur.pid, []))
        tree.sort(key=lambda pc: pc[0].pid)
        trees.append(tree)
    return trees


def summarize(trees):
    per_session = []
    for tree in trees:
        row = {c: 0.0 for c in CLASSES}
        row["other"] = 0.0
        for proc, cls in tree:
            row[cls] = row.get(cls, 0.0) + proc.rss_kb / 1024
        row["pid"] = tree[0][0].pid
        row["total"] = sum(v for k, v in row.items() if k not in ("pid", "total"))
        per_session.append(row)
    total = sum(r["total"] for r in per_session)
    n = len(per_session)
    return {
        "sessions": n,
        "per_session": per_session,
        "total_mb": total,
        "mean_mb": total / n if n else 0,
    }


def format_report(summary):
    cols = list(CLASSES) + ["other", "total"]
    lines = [f"{summary['sessions']} sessions"]
    lines.append("  host pid  " + "".join(f"{c:>22}" for c in cols))
    for r in summary["per_session"]:
        lines.append(f"  {r['pid']:<9} " + "".join(f"{r[c]:>20.1f}MB" for c in cols))
    lines.append(f"total: {summary['total_mb']:.1f} MB  mean per session: {summary['mean_mb']:.1f} MB")
    lines.append("RSS is KB from macOS ps; it double-counts shared pages.")
    return "\n".join(lines)


def _ps():
    out = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss=,command="], capture_output=True, text=True, check=True
    ).stdout
    return parse_ps(out)


def _tree_rss_mb(root_pid):
    procs = _ps()
    kids = children_map(procs)
    by_pid = {p.pid: p for p in procs}
    total, stack, seen = 0, [root_pid], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if pid in by_pid:
            total += by_pid[pid].rss_kb
        stack.extend(k.pid for k in kids.get(pid, []))
    return total / 1024


def spawn_bench(n, mode, prompt, hold_seconds):
    binary = os.environ.get("FUSED_RENDER_CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        sys.exit("claude binary not found (set FUSED_RENDER_CLAUDE_BIN)")
    argv = [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    if mode == "strict":
        argv += ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}) + "\n"
    procs = []
    peaks = {}
    try:
        for _ in range(n):
            p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            procs.append(p)
            p.stdin.write(msg.encode())
            p.stdin.flush()
            peaks[p.pid] = 0.0
        t0 = time.time()
        settled = None
        while time.time() - t0 < 60:
            for p in procs:
                peaks[p.pid] = max(peaks[p.pid], _tree_rss_mb(p.pid))
            if settled is None and time.time() - t0 >= hold_seconds:
                settled = time.time()
            if settled is not None and time.time() - settled >= hold_seconds:
                break
            time.sleep(0.5)
    finally:
        for p in procs:
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except Exception:
                pass
    total = sum(peaks.values())
    print(f"mode={mode} sessions={n} mean peak per session: {total / max(n, 1):.1f} MB  total peak: {total:.1f} MB")
    print("RSS is KB from macOS ps; it double-counts shared pages.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only-under", help="only hosts whose command contains this path")
    ap.add_argument("--spawn", type=int, metavar="N")
    ap.add_argument("--mode", choices=("default", "strict"), default="default")
    ap.add_argument("--prompt", default="say ok")
    ap.add_argument("--hold-seconds", type=float, default=5)
    a = ap.parse_args(argv)
    if a.spawn:
        spawn_bench(a.spawn, a.mode, a.prompt, a.hold_seconds)
        return 0
    print(format_report(summarize(session_trees(_ps(), only_under=a.only_under))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
