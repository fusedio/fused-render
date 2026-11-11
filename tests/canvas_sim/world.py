"""Actors, scheduler and invariants for the canvas sync simulator.

One `World` is one canvas: a FakeServer, a clone on disk with the REAL
`_SyncManager` watching it (its watcher and fs threads are no-ops; the
scheduler drives `tick()` one loop pass at a time), and three actors:

  FakeFrame  the embedded workbench (embed v3): holds the canvas as compact
             rows + bodies, posts snapshots / body texts through the real
             routes with gen/seq/rev, pulls deltas and acks them. Posts are
             queued and delivered by a separate step, so a snapshot can be
             built before a delta and land after it (R2).
  Session    a Claude session in the clone: edits source files and
             canvas.toml rows directly, only while its turn is live (which
             drives `agent_run_id`); ending a turn runs the Stop hook.
  Human      the hosted workbench tab writing the server directly with
             If-Match on its baseline revision (reload on 412), and the
             canvas page's Pull.

Every accepted write lands in the `Ledger` as (key, value, observed): the
value the writer saw for that key when it wrote. That is enough to tell a
lost write (a value someone knowingly superseded came back, or a write
vanished under an older value) from a concurrent tie the merge policy had to
break. Tokens (`tk_…`) are unique per write so a final file is attributable.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import tomllib

import fused_render.canvases as canvases_mod
from fused_render import canvas_snapshot

from canvas_sim.server import FakeServer, SimProcs

ENV = "unstable"
GUARD = {"X-Fused": "1"}
ABSENT = "<absent>"

_REAL_RUN = canvases_mod._SyncManager._run
_TOKEN = re.compile(r"tk_[a-z0-9_]+")

INITIAL = {
    # name: (file, x)
    "a": ("a.py", 100),
    "b": ("b.py", 1000),
    "w": ("w.json", 1900),
    "n": ("n.md", 2800),
}


def body_text(file: str, tok: str) -> str:
    if file.endswith(".json"):
        return json.dumps({"type": "widget", "tok": tok})
    if file.endswith(".md"):
        return f"# {tok}\n"
    return f"@fused.udf\ndef udf():\n    return '{tok}'\n"


def token_of(text: str | None) -> str:
    if text is None:
        return ABSENT
    m = _TOKEN.search(text)
    return m.group(0) if m else f"<no token: {text[:40]!r}>"


def norm_text(file: str, text: str) -> str:
    if file.endswith(".json"):
        try:
            return json.dumps(json.loads(text), sort_keys=True)
        except ValueError:
            return text
    return text.replace("\r\n", "\n")


def make_row(name: str, x: int, title: str) -> dict:
    return {"udfName": name, "x": x, "y": 0, "zIndex": 1, "width": 700, "height": 300,
            "title": title}


# -- mtimes: the watcher fingerprints (mtime, size), so two same-size writes
# inside one clock tick must still read as two writes. -----------------------

_MTIME = [0]


def touch(path: str) -> None:
    ns = max(_MTIME[0] + 2_000_000, time.time_ns())
    _MTIME[0] = ns
    try:
        os.utime(path, ns=(ns, ns))
    except OSError:
        pass


def write_file(path: str, text: str) -> None:
    tmp = path + ".simtmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)
    touch(path)


class _OneShot:
    """A stop_event whose first wait() returns False: `_run` makes exactly one
    loop pass (the pre-`_tick` engine)."""

    def __init__(self, real):
        self._real, self._fired = real, False

    def wait(self, timeout=None):
        if self._fired:
            return True
        self._fired = True
        return False

    def is_set(self):
        return self._real.is_set()

    def set(self):
        self._real.set()


def tick(m) -> None:
    if hasattr(type(m), "_tick"):
        m._tick()
        return
    real = m.stop_event
    m.stop_event = _OneShot(real)
    try:
        _REAL_RUN(m)
    finally:
        m.stop_event = real


class FakeAgent:
    """`_agent_module()` stand-in: a run is live in a clone while its
    Session's turn is."""

    def __init__(self):
        self.live: dict[str, str] = {}   # clone dir → run id

    def _live_run_in(self, folder):
        return {"run_id": self.live.get(os.path.normpath(folder), "")}

    def _live_run(self, file, session_id="", limit=None):
        return self._live_run_in(file)


# -- the ledger and invariant (2) ------------------------------------------------


class Ledger:
    def __init__(self):
        self.writes: list[dict] = []
        self.refusals: list[dict] = []
        self.step = 0

    def write(self, actor: str, key: str, value, observed, seen: dict | None = None) -> None:
        """`seen`, on a delete: every key of the node as the deleter saw it,
        so a write to that node the deleter had NOT seen reads as concurrent
        (delete-vs-edit is a tie, not a revert)."""
        self.writes.append({"step": self.step, "actor": actor, "key": key,
                            "value": value, "observed": observed, "seen": seen})

    def refuse(self, actor: str, what: str, reason, *, files=None) -> None:
        self.refusals.append({"step": self.step, "actor": actor, "what": what,
                              "reason": reason, "files": files})

    def _ancestors(self, key: str, value) -> set:
        """Every value `value` knowingly superseded on `key` (the chain of
        `observed` back through the writes)."""
        parents: dict = {}
        for w in self.writes:
            if w["key"] == key:
                parents.setdefault(w["value"], set()).add(w["observed"])
        out: set = set()
        todo = [value]
        while todo:
            for p in parents.get(todo.pop(), ()):
                if p is not None and p not in out:
                    out.add(p)
                    todo.append(p)
        return out

    def _delete_raced(self, key: str, deletes: list[dict]) -> bool:
        """Every delete that superseded `key`'s final value raced a write to
        the same node by another actor that the deleter had not seen: a
        delete-vs-edit tie the merge may break either way. A write the
        deleter's observed state already includes — the value it saw, or
        one that value superseded — is history, not a race: a final value
        from that history means the delete was reverted."""
        node = key.split(":", 1)[1]
        node_writes = [w for w in self.writes if w["key"].split(":", 1)[1] == node
                       and w["actor"] != "init"
                       and not (isinstance(w["value"], str) and w["value"].startswith("del_"))]
        for d in deletes:
            seen = d.get("seen") or {}

            def unseen(w) -> bool:
                if w["key"] not in seen:
                    return True
                saw = seen[w["key"]]
                return w["value"] != saw and w["value"] not in self._ancestors(w["key"], saw)

            if not any(w["actor"] != d["actor"] and unseen(w) for w in node_writes):
                return False
        return True

    def keys(self) -> set[str]:
        return {w["key"] for w in self.writes}

    def lost_writes(self, final: dict) -> tuple[list[str], int]:
        """Invariant (2). For each key the final value must be a HEAD of the
        key's version graph: written by some actor, and not knowingly
        superseded by a later write (no write observed it, directly or
        through a chain). A final value someone wrote OVER is a revert — the
        stale echo of R2/R6 or a rolled-back merge — and a value no actor
        wrote is corruption. Concurrent heads (two actors wrote without
        seeing each other) are a tie the merge policy breaks; the loser is
        counted, not failed. Returns (violations, tie_losses)."""
        problems: list[str] = []
        ties = 0
        by_key: dict[str, list[dict]] = {}
        for w in self.writes:
            by_key.setdefault(w["key"], []).append(w)
        for key, writes in sorted(by_key.items()):
            if key not in final:
                continue  # a row key of a node that ended deleted
            parents = {}
            for w in writes:
                parents.setdefault(w["value"], set()).add(w["observed"])

            def ancestors(v, seen=None):
                seen = set() if seen is None else seen
                for p in parents.get(v, ()):
                    if p is not None and p not in seen:
                        seen.add(p)
                        ancestors(p, seen)
                return seen

            superseded = set()
            for w in writes:
                superseded |= ancestors(w["value"])
            heads = [w["value"] for w in writes if w["value"] not in superseded]
            f = final[key]
            if f == ABSENT:
                if not any(isinstance(h, str) and h.startswith("del_") for h in heads):
                    problems.append(f"{key}: ended absent, but no delete is a head "
                                    f"(heads {heads[-4:]})")
                    continue
            elif f not in parents:
                problems.append(f"{key}: ends {f!r}, a value no actor wrote")
                continue
            elif f in superseded:
                later = [w for w in writes if f in ancestors(w["value"])]
                if all(isinstance(w["value"], str) and w["value"].startswith("del_")
                       for w in later) and self._delete_raced(key, later):
                    ties += 1
                    continue
                problems.append(
                    f"{key}: ends {f!r}, which {later[-1]['actor']} superseded at step "
                    f"{later[-1]['step']} with {later[-1]['value']!r} (a reverted write)")
                continue
            keep = ancestors(f) | {f}
            ties += len([h for h in heads if h != f and h not in keep])
        return problems, ties


# -- the actors -----------------------------------------------------------------


class FakeFrame:
    """The embed v3 frame: local model + the host routes."""

    def __init__(self, world: "World"):
        self.w = world
        self.gen = 1
        self.seq = 0
        self.rev = 0
        self.nodes: dict[str, dict] = {}
        self.bodies: dict[str, str] = {}
        self.files: dict[str, str] = {}
        self.ids: dict[str, str] = {}
        self.hashes: dict[str, str] = {}
        self.base: dict[str, str] = {}
        # embed-snapshot-bridge `baseNodes`: what the frame KNOWS (hydration
        # + every delta since), sent as `base_nodes` so the host can tell a
        # node the frame dropped from one it never saw.
        self.known: set[str] = set()
        self.edges: list[list[str]] = []
        self.outbox: list[dict] = []
        self.deltas: list[dict] = []
        self.inbox: list[dict] = []
        # A bounced snapshot's `touched`, protected until the re-send carries
        # them (the bridge's `rebuild.touched`).
        self.rebuild_touched: set[str] = set()
        self.cut_rev = 0

    # -- disk → frame --------------------------------------------------------

    def cut(self, full: bool = False) -> dict | None:
        """The PAGE's half: GET a delta (it does so the moment the events
        socket shows disk_rev move) and hand it to the frame's inbox. The
        frame applies it in its own step (`apply_inbox`), so a snapshot can
        be serialised between the two — the window `body.rev` exists for."""
        res = self.w.client.get(f"/api/canvases/delta?env={self.w.env}&name={self.w.name}"
                                + ("&full=1" if full else ""))
        self.cut_rev = self.w.manager.disk_rev
        if res.status_code != 200:
            self.w.ledger.refuse("frame", "delta", res.json().get("error"))
            return None
        cut = res.json()
        if "held" in cut or not cut.get("ops"):
            return cut
        self.inbox.append(cut)
        return cut

    def apply_inbox(self) -> int:
        n = 0
        while self.inbox:
            cut = self.inbox.pop(0)
            self.deltas.append(cut)
            if os.environ.get("FUSED_RENDER_SIM_DEBUG"):
                print("   delta", cut["rev"], "full" if cut.get("full") else "", cut.get("gen"),
                      cut.get("seq"), [(o["op"], o.get("name"), (o.get("node") or {}).get("position"))
                                       for o in cut["ops"]])
            skipped = self.apply_ops(cut["ops"], full=bool(cut.get("full")),
                                     guarded=self.protected_for(cut))
            ack = self.w.client.post("/api/canvases/delta/ack", json={
                "env": self.w.env, "name": self.w.name, "rev": cut["rev"], "ok": True,
                "skipped": sorted(skipped)}, headers=GUARD)
            assert ack.status_code == 200, ack.text
            self.rev = max(self.rev, cut["rev"])
            n += 1
        return n

    def sync(self, full: bool = False) -> dict | None:
        """Cut and apply at once (hello, quiescence, a bounced snapshot)."""
        cut = self.cut(full=full)
        self.apply_inbox()
        return cut

    def protected_for(self, cut: dict) -> set[str]:
        """embed-snapshot-bridge `protectedNamesFor`: the `touched` of every
        pending snapshot the host had not applied when it cut this delta
        (past the cut's `seq` in this gen; all of them when the host names no
        seq of this gen), plus a bounced snapshot's names until its re-send."""
        out = set(self.rebuild_touched)
        if "gen" not in cut and "seq" not in cut:
            return out  # an older host: nothing protected
        host_seq = (cut["seq"] if cut.get("gen") == self.gen and isinstance(cut.get("seq"), int)
                    else -1)
        for item in self.outbox:
            if item["kind"] != "snapshot" or (host_seq >= 0 and item["seq"] <= host_seq):
                continue
            out |= item["touched"]
        return out

    def apply_ops(self, ops: list[dict], *, full: bool, guarded=frozenset()) -> set[str]:
        """embed-delta-apply `applyEmbedDelta` with PROTECTED NAMES (application
        #8569): for a name a pending local change touched, an upsert of a
        held node keeps only its code/body/hash/id, an upsert of a node the
        frame no longer holds, a delete and a rename (both ends, guarded for
        the rest of the delta) are skipped, and a full delta keeps a
        protected name it does not list. Returns the skipped names."""
        guarded = set(guarded)
        skipped: set[str] = set()
        if full:
            keep = {o["name"] for o in ops if o["op"] == "upsert"} | guarded
            for d in (self.nodes, self.bodies, self.files, self.ids, self.hashes, self.base):
                for k in list(d):
                    if k not in keep:
                        d.pop(k)
        for op in ops:
            kind = op["op"]
            if kind == "upsert" and op["name"] in guarded:
                name = op["name"]
                if name not in self.nodes:
                    skipped.add(name)  # the user deleted it
                    continue
                if "row" in op or "node" in op:
                    skipped.add(name)
                text = op.get("code", op.get("body"))
                if text is not None:
                    self._body_from_host(name, text, op.get("hash"))
                if "id" in op:
                    self.ids[name] = op["id"]
                continue
            if kind == "delete" and op["name"] in guarded:
                skipped.add(op["name"])
                continue
            if kind == "rename" and {op["from"], op["to"]} & guarded:
                skipped |= {op["from"], op["to"]}
                guarded |= {op["from"], op["to"]}
                continue
            if kind == "upsert":
                name = op["name"]
                self.known.add(name)
                if "file" in op:
                    self.files[name] = op["file"]
                if "row" in op:
                    # mergeNode(node, op.node): the patch carries `position`
                    # only when the host moved the node, so a drag of the
                    # frame's own survives a title/colour upsert.
                    row = dict(op["row"])
                    old = self.nodes.get(name)
                    if old is not None and "position" not in (op.get("node") or {}):
                        for k in ("x", "y"):
                            if k in old:
                                row[k] = old[k]
                    self.nodes[name] = row
                text = op.get("code", op.get("body"))
                if text is not None:
                    self._body_from_host(name, text, op.get("hash"))
                if "id" in op:
                    self.ids[name] = op["id"]
            elif kind == "delete":
                self.known.discard(op["name"])
                for d in (self.nodes, self.bodies, self.files, self.ids, self.hashes,
                          self.base):
                    d.pop(op["name"], None)
            elif kind == "rename":
                frm, to = op["from"], op["to"]
                self.known.discard(frm)
                self.known.add(to)
                for d in (self.nodes, self.bodies, self.ids, self.hashes, self.base):
                    if frm in d:
                        d[to] = d.pop(frm)
                if to in self.nodes:
                    self.nodes[to]["udfName"] = to
                self.files.pop(frm, None)
                if op.get("file"):
                    self.files[to] = op["file"]
            elif kind == "edges":
                new = [list(e) for e in op["set"]
                       if not any(end in guarded and end not in self.nodes for end in e)]
                kept = [e for e in self.edges
                        if any(end in guarded for end in e) and e not in new]
                skipped |= {end for e in op["set"] + kept for end in e if end in guarded}
                self.edges = new + kept
        return skipped

    def _body_from_host(self, name: str, text: str, hash_: str | None) -> None:
        """embed-body-sync.ts `previewBodyFromHost`: the host's text equal to
        the agreed base, or a post of ours in flight → keep what the frame
        holds; else a three-way merge, which for whole-text tokens is: the
        host's text when the frame has no edit of its own since the base."""
        base = self.base.get(name)
        current = self.bodies.get(name)
        if base is not None and text == base:
            return
        if any(i["kind"] == "body" and i["node"] == name for i in self.outbox):
            return
        if current is None or base is None or current == base:
            self.bodies[name] = text
        self.base[name] = text
        self.hashes[name] = hash_ or canvas_snapshot.body_hash(text)

    # -- frame → disk --------------------------------------------------------

    def snapshot_body(self) -> dict:
        nodes = []
        for name, row in self.nodes.items():
            patch = canvas_snapshot.studio_patch_from_row(row)
            node = {"id": f"udf-node-{name}", "type": row.get("type") or "simple-udf", **patch}
            node["data"] = dict(node.get("data") or {}, udfName=name)
            if row.get("type") == canvas_snapshot.FOLDER_TYPE:
                node["data"]["folderName"] = name
            nodes.append(node)
        edges = [{"source": f"udf-node-{a}", "target": f"udf-node-{b}"}
                 for a, b in self.edges if a in self.nodes and b in self.nodes]
        udfs = [{"slug": n, "file": self.files[n], **({"id": self.ids[n]} if n in self.ids else {})}
                for n in self.nodes if n in self.files]
        return {"dashboard_body": {"studioState": {"nodes": nodes, "edges": edges}},
                "udfs": udfs, "body_channel": True, "rev": self.rev,
                "base_nodes": sorted(self.known)}

    def queue_snapshot(self, touched=()) -> int:
        """`touched`: the names this snapshot changed locally — protected
        from any delta the host cuts before applying it."""
        self.seq += 1
        self.outbox.append({"kind": "snapshot", "seq": self.seq, "body": self.snapshot_body(),
                            "touched": set(touched)})
        return self.seq

    def drag(self, name: str, x: int) -> None:
        self.apply_inbox()
        if name not in self.nodes:
            return
        row = self.nodes[name]
        self.w.ledger.write("frame", f"x:{name}", x, row.get("x"))
        row["x"] = x
        self.queue_snapshot(touched=[name])

    def delete(self, name: str, tok: str) -> None:
        self.apply_inbox()
        if name not in self.nodes:
            return
        row = self.nodes.get(name, {})
        self.w.ledger.write("frame", f"body:{name}", tok,
                            token_of(self.bodies.get(name)) if name in self.bodies else None,
                            seen={f"body:{name}": token_of(self.bodies.get(name)),
                                  f"x:{name}": row.get("x"), f"title:{name}": row.get("title")})
        for d in (self.nodes, self.bodies, self.ids, self.hashes, self.base):
            d.pop(name, None)
        self.edges = [e for e in self.edges if name not in e]
        # The node's Udf is gone: its debounced body post goes with it.
        self.outbox = [i for i in self.outbox if not (i["kind"] == "body" and i["node"] == name)]
        self.queue_snapshot(touched=[name])

    def edit_body(self, name: str, tok: str) -> None:
        self.apply_inbox()
        if name not in self.nodes or name not in self.files:
            return
        file = self.files[name]
        text = body_text(file, tok)
        self.w.ledger.write("frame", f"body:{name}", tok, token_of(self.bodies.get(name)))
        base = self.hashes.get(name, canvas_snapshot.body_hash(self.bodies.get(name, "")))
        self.bodies[name] = text
        self.outbox.append({"kind": "body", "node": name, "file": file, "base": base,
                            "text": text})

    def deliver(self) -> bool:
        # The frame handles the page's messages in order: a delta the page
        # already cut is applied before the frame's next post goes out (a
        # snapshot SERIALISED earlier still carries its older rev).
        self.apply_inbox()
        if not self.outbox:
            return False
        item = self.outbox.pop(0)
        c, name = self.w.client, self.w.name
        if item["kind"] == "snapshot":
            res = c.post("/api/canvases/snapshot",
                         json={"env": self.w.env, "name": name, "gen": self.gen, "seq": item["seq"],
                               "body": item["body"]}, headers=GUARD)
            body = res.json()
            if os.environ.get("FUSED_RENDER_SIM_DEBUG"):
                print("   snapshot", item["seq"], "rev", item["body"]["rev"], "->", res.status_code,
                      {k: body.get(k) for k in ("changed", "stale", "rebuild", "upserted", "rev")},
                      [(n["data"]["udfName"], n.get("position")) for n in item["body"]["dashboard_body"]["studioState"]["nodes"]])
            if res.status_code == 409:
                self.w.ledger.refuse("frame", f"snapshot {item['seq']}", body.get("error"))
                self.outbox.insert(0, item)
                return True
            if res.status_code != 200:
                self.w.ledger.refuse("frame", f"snapshot {item['seq']}", body.get("error"))
                return True
            if body.get("rebuild"):
                # Bounced: apply the delta it was built before, then post
                # afresh. A snapshot is the frame's FULL state, so the re-serialised
                # one supersedes every snapshot still queued behind it.
                # Every bounced item's `touched` rides the re-sent entry, and
                # stays protected until it is queued.
                carried = set(item["touched"]) | self.rebuild_touched
                for i in self.outbox:
                    if i["kind"] == "snapshot":
                        carried |= i["touched"]
                self.outbox = [i for i in self.outbox if i["kind"] != "snapshot"]
                self.rebuild_touched = carried
                self.seq += 1
                fresh = self.seq
                # The delta the bounce names reached the frame already
                # (the page cut it on the disk_rev move); only a frame still
                # behind it asks again.
                self.apply_inbox()
                if self.rev < (body.get("rev") or 0):
                    self.sync()
                self.outbox.insert(0, {"kind": "snapshot", "seq": fresh,
                                       "body": self.snapshot_body(), "touched": carried})
                self.rebuild_touched = set()
                return True
            return True
        res = c.post("/api/canvases/body",
                     json={"env": self.w.env, "name": name, "node": item["node"], "file": item["file"],
                           "base_hash": item["base"], "text": item["text"]}, headers=GUARD)
        body = res.json()
        if res.status_code == 409:
            self.w.ledger.refuse("frame", f"body {item['file']}", body.get("error"),
                                 files=[item["file"]])
            self.outbox.insert(0, item)
        elif body.get("ok"):
            self.hashes[item["node"]] = body.get("hash")
            self.base[item["node"]] = item["text"]
        elif body.get("code") == "stale":
            # Rebase: the frame keeps its text over the disk's new base.
            item["base"] = body["disk_hash"]
            self.outbox.insert(0, item)
        elif body.get("code") == "fenced":
            self.outbox.insert(0, item)
        else:
            self.w.ledger.refuse("frame", f"body {item['file']}",
                                 body.get("code") or body.get("error"), files=[item["file"]])
        return True

    def flush(self, limit: int = 20) -> None:
        for _ in range(limit):
            if not self.deliver():
                return


class Session:
    """A Claude session working in the clone."""

    def __init__(self, world: "World"):
        self.w = world
        self.sid = f"sim-session-{world.name}"
        self.contexts: list[str] = []

    @property
    def live(self) -> bool:
        return bool(self.w.agent.live.get(os.path.normpath(self.w.dir)))

    def start_turn(self) -> None:
        self.w.agent.live[os.path.normpath(self.w.dir)] = f"run-{self.w.ledger.step}"

    def end_turn(self) -> None:
        self.w.agent.live.pop(os.path.normpath(self.w.dir), None)
        m = self.w.manager
        if hasattr(m, "turn_end"):
            m.turn_end()  # what the Stop hook route runs (synchronously here)

    def _hook(self, path: str) -> None:
        res = self.w.client.post("/api/canvases/agent/hook", json={
            "event": "post", "session_id": self.sid, "tool_name": "Edit",
            "tool_input": {"file_path": path}, "cwd": self.w.dir}, headers=GUARD)
        ctx = res.json().get("context") if res.status_code == 200 else None
        if ctx:
            self.contexts.append(ctx)

    def _doc(self) -> dict:
        with open(os.path.join(self.w.dir, "canvas.toml"), "rb") as fh:
            return tomllib.load(fh)

    def _write_doc(self, doc: dict) -> None:
        path = os.path.join(self.w.dir, "canvas.toml")
        write_file(path, canvas_snapshot._dump_toml(doc))
        self._hook(path)

    def disk_nodes(self) -> dict[str, str]:
        files = canvas_snapshot.node_files(self.w.dir)
        try:
            rows = canvas_snapshot.rows_of_doc(self._doc())["nodes"]
        except (OSError, tomllib.TOMLDecodeError):
            return {}
        return {n: f for n, f in files.items() if n in rows}

    def edit_body(self, name: str, tok: str) -> None:
        file = self.disk_nodes()[name]
        path = os.path.join(self.w.dir, file)
        with open(path, encoding="utf-8") as fh:
            old = fh.read()
        self.w.ledger.write("session", f"body:{name}", tok, token_of(old))
        write_file(path, body_text(file, tok))
        self._hook(path)

    def edit_row(self, name: str, field: str, value) -> None:
        doc = self._doc()
        for row in doc["canvas"]["nodes"]:
            if canvas_snapshot.row_name(row) == name:
                self.w.ledger.write("session", f"{field}:{name}", value, row.get(field))
                row[field] = value
                self._write_doc(doc)
                return

    def create(self, name: str, ext: str, tok: str, x: int, title: str) -> None:
        file = name + ext
        self.w.ledger.write("session", f"body:{name}", tok, None)
        self.w.ledger.write("session", f"x:{name}", x, None)
        self.w.ledger.write("session", f"title:{name}", title, None)
        write_file(os.path.join(self.w.dir, file), body_text(file, tok))
        doc = self._doc()
        doc["canvas"].setdefault("nodes", []).append(make_row(name, x, title))
        self._write_doc(doc)
        self.w.names.add(name)

    def delete(self, name: str, tok: str) -> None:
        file = self.disk_nodes()[name]
        path = os.path.join(self.w.dir, file)
        with open(path, encoding="utf-8") as fh:
            old = fh.read()
        doc = self._doc()
        row = canvas_snapshot.rows_of_doc(doc)["nodes"].get(name, {})
        self.w.ledger.write("session", f"body:{name}", tok, token_of(old),
                            seen={f"body:{name}": token_of(old), f"x:{name}": row.get("x"),
                                  f"title:{name}": row.get("title")})
        doc["canvas"]["nodes"] = [r for r in doc["canvas"]["nodes"]
                                  if canvas_snapshot.row_name(r) != name]
        doc["canvas"]["edges"] = [e for e in doc["canvas"].get("edges") or [] if name not in e]
        self._write_doc(doc)
        os.remove(path)


class Human:
    """The hosted workbench tab (server writes with If-Match) and the canvas
    page's Pull buttons."""

    def __init__(self, world: "World"):
        self.w = world
        self.baseline: int | None = None
        self.loaded: dict[str, object] = {}
        self.reload()

    @property
    def server(self) -> FakeServer:
        return self.w.server

    def reload(self) -> None:
        s = self.server
        self.baseline = s.revision if s.revision_mode else None
        self.loaded = s_tokens(s)

    def _attempt(self, key: str, value, op, seen_node: str | None = None):
        """Run `op(if_match)`; on 412 reload and redo once (the user reloads
        and makes the edit again). Ledger the write that landed."""
        for _ in range(2):
            status, body = op(self.baseline)
            if status == 200:
                seen = ({k: v for k, v in self.loaded.items() if k.split(":", 1)[1] == seen_node}
                        if seen_node else None)
                self.w.ledger.write("human", key, value, self.loaded.get(key), seen=seen)
                self.baseline = body.get("revision") if self.server.revision_mode else None
                self.loaded = s_tokens(self.server)
                return True
            if status == 412:
                files = sorted(body.get("udfs") or {})
                self.w.ledger.refuse("human", f"hosted {key}",
                                     f"This canvas changed on the server (revision "
                                     f"{body.get('revision')})", files=files)
                self.reload()
                continue
            self.w.ledger.refuse("human", f"hosted {key}", body.get("code"), files=[key])
            return False
        return False

    def edit_body(self, slug: str, tok: str) -> None:
        u = self.server.udfs.get(slug)
        if u is None:
            return
        self._attempt(f"body:{slug}", tok,
                      lambda im: self.server.hosted_save_udf(slug, body_text(u.file, tok), im))

    def edit_row(self, slug: str, field: str, value) -> None:
        self._attempt(f"{field}:{slug}", value,
                      lambda im: self.server.hosted_row(slug, im, **{field: value}))

    def delete(self, slug: str, tok: str) -> None:
        self._attempt(f"body:{slug}", tok, lambda im: self.server.hosted_delete(slug, im),
                      seen_node=slug)

    def add(self, slug: str, ext: str, tok: str, x: int, title: str) -> None:
        file = slug + ext
        ok = self._attempt(f"body:{slug}", tok, lambda im: self.server.hosted_add(
            slug, file, body_text(file, tok), make_row(slug, x, title), im))
        if ok:
            self.w.ledger.write("human", f"x:{slug}", x, None)
            self.w.ledger.write("human", f"title:{slug}", title, None)
            self.w.names.add(slug)

    def pull(self, hard: bool = False) -> dict:
        res = self.w.client.post("/api/canvases/sync/pull",
                                 json={"env": self.w.env, "name": self.w.name, "hard": hard},
                                 headers=GUARD)
        body = res.json()
        if res.status_code != 200 or not body.get("ok", True):
            self.w.ledger.refuse("human", "pull", body.get("error"))
        return body


def s_tokens(s: FakeServer) -> dict:
    out: dict[str, object] = {}
    for slug, u in s.udfs.items():
        out[f"body:{slug}"] = token_of(u.text)
    for name, row in s.rows().items():
        out[f"x:{name}"] = row.get("x")
        out[f"title:{name}"] = row.get("title")
    return out


# -- the world ---------------------------------------------------------------------


class World:
    def __init__(self, harness, procs: SimProcs, agent: FakeAgent, name: str, *,
                 revision_mode: bool = True, rng: random.Random | None = None,
                 initial: dict | None = None, env: str = ENV,
                 server: FakeServer | None = None):
        self.harness, self.client, self.procs, self.agent = harness, harness.client, procs, agent
        self.name = name
        self.env = env
        self.rng = rng or random.Random(0)
        self.ledger = Ledger()
        self.trace: list[str] = []
        self.names: set[str] = set()
        self._n = 0
        self.stale_seen: list[dict] = []
        initial = initial or INITIAL
        shared = server is not None
        self.server = server if shared else procs.add(FakeServer(name, revision_mode=revision_mode))
        rows, files = [], {}
        for node, (file, x) in initial.items():
            tok, title = f"tk_init_{node}", f"tk_init_title_{node}"
            rows.append(make_row(node, x, title))
            files[file] = body_text(file, tok)
            self.ledger.write("init", f"body:{node}", tok, None)
            self.ledger.write("init", f"x:{node}", x, None)
            self.ledger.write("init", f"title:{node}", title, None)
            self.names.add(node)
        edges = [["a", "b"]] if {"a", "b"} <= set(initial) else []
        if not shared:
            self.server.seed(rows, files, edges)
        res = self.client.post("/api/canvases/clone", json={"env": self.env, "name": name},
                               headers=GUARD)
        assert res.status_code == 200, res.text
        self.dir = canvases_mod._canvas_dir(self.env, name)
        self.manager = canvases_mod._sync_manager(self.env, name, create=True)
        canvases_mod._seed_clone_claude_files(self.dir, name)
        self.frame = FakeFrame(self)
        self.session = Session(self)
        self.human = Human(self)
        self.tick(2)  # first look adopts the manifest; the next takes the lease
        self.frame.sync(full=True)

    # -- helpers ---------------------------------------------------------------

    def tok(self, actor: str) -> str:
        self._n += 1
        return f"tk_{actor[0]}{self._n}"

    def uniq(self) -> int:
        self._n += 1
        return 10_000 + self._n

    def note(self, line: str) -> None:
        self.trace.append(f"{self.ledger.step:3d} {line}")

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            tick(self.manager)
            stale = self.manager.server_stale
            if stale is not None and (not self.stale_seen or self.stale_seen[-1] != stale):
                self.stale_seen.append(dict(stale))

    def log_actions(self) -> list[str]:
        return [e["action"] for e in self.client.get(
            f"/api/canvases/sync/log?env={self.env}&name={self.name}").json()["events"]]

    def state(self) -> dict:
        return self.client.get(f"/api/canvases/sync/state?env={self.env}&name={self.name}").json()

    def stop(self) -> None:
        with canvases_mod._SYNC_LOCK:
            m = canvases_mod._syncs.pop((self.env, self.name), None)
        if m is not None:
            m.stop()
        self.agent.live.pop(os.path.normpath(self.dir), None)

    # -- quiescence and the invariants -------------------------------------------

    def _settle_key(self) -> tuple:
        m = self.manager
        return (m.push_seq, m.pull_seq, m.merge_seq, m.merge_rollback_seq, m.disk_rev,
                self.server.revision, self.server.last_updated, m.push_state,
                m.server_stale is None, len(self.frame.outbox), self.frame.rev,
                m._dirty_since is None)

    def quiesce(self, rounds: int = 30) -> bool:
        """Every actor goes idle: the turn ends (Stop hook), the frame delivers
        what it queued and takes the deltas it is owed, the watcher ticks.
        True when the world held still for 3 rounds within `rounds`."""
        if self.session.live:
            self.session.end_turn()
        self.follow()
        prev, still = None, 0
        for _ in range(rounds):
            self.frame.flush()
            self.follow()
            self.frame.sync()
            self.tick()
            self.follow()
            self.tick()
            self.follow()
            key = self._settle_key()
            still = still + 1 if key == prev else 0
            prev = key
            if still >= 3:
                return True
        return False

    def disk_view(self) -> tuple[dict, dict]:
        with open(os.path.join(self.dir, "canvas.toml"), "rb") as fh:
            rows = canvas_snapshot.rows_of_doc(tomllib.load(fh))["nodes"]
        files = {}
        for n, f in canvas_snapshot.node_files(self.dir).items():
            with open(os.path.join(self.dir, f), encoding="utf-8", newline="") as fh:
                files[n] = (f, fh.read())
        return rows, files

    def check_convergence(self) -> list[str]:
        """Invariant (1): disk == server == frame (rows by name on x/title,
        source files by text modulo JSON/newline re-serialisation)."""
        out: list[str] = []
        rows, files = self.disk_view()
        srows = self.server.rows()
        sfiles = {s: (u.file, self.server.export_file(u)) for s, u in self.server.udfs.items()}
        if set(rows) != set(srows):
            out.append(f"rows disk {sorted(rows)} != server {sorted(srows)}")
        if set(files) != set(sfiles):
            out.append(f"files disk {sorted(files)} != server {sorted(sfiles)}")
        for n in set(rows) & set(srows):
            for f in ("x", "title"):
                if rows[n].get(f) != srows[n].get(f):
                    out.append(f"{f}:{n} disk {rows[n].get(f)!r} != server {srows[n].get(f)!r}")
        for n in set(files) & set(sfiles):
            (df, dt), (sf, st) = files[n], sfiles[n]
            if df != sf or norm_text(df, dt) != norm_text(sf, st):
                out.append(f"body:{n} disk {df}:{token_of(dt)} != server {sf}:{token_of(st)}")
        shown = {n for n in rows if n in files or rows[n].get("type") == canvas_snapshot.FOLDER_TYPE}
        fr = self.frame
        if set(fr.nodes) != shown:
            out.append(f"frame nodes {sorted(fr.nodes)} != disk {sorted(shown)}")
        for n in set(fr.nodes) & shown:
            for f in ("x", "title"):
                if fr.nodes[n].get(f) != rows[n].get(f):
                    out.append(f"{f}:{n} frame {fr.nodes[n].get(f)!r} != disk {rows[n].get(f)!r}")
            if n in files and n not in fr.bodies:
                out.append(f"body:{n} the frame shows the node without its source text")
            elif n in fr.bodies and token_of(fr.bodies[n]) != token_of(files[n][1]):
                out.append(f"body:{n} frame {token_of(fr.bodies[n])} != disk {token_of(files[n][1])}")
        if fr.outbox:
            out.append(f"frame still holds {len(fr.outbox)} unsent post(s)")
        return out

    def final_values(self) -> dict:
        rows, files = self.disk_view()
        final: dict[str, object] = {}
        for n in self.names:
            present = n in rows and n in files
            final[f"body:{n}"] = token_of(files[n][1]) if present else ABSENT
            if present:
                final[f"x:{n}"] = rows[n].get("x")
                final[f"title:{n}"] = rows[n].get("title")
        return final

    def check_wedge(self) -> list[str]:
        """Invariant (3): nothing stale, held or errored once all are idle."""
        m, out = self.manager, []
        if m.server_stale is not None:
            out.append(f"server_stale persists: {m.server_stale}")
        if m.push_state != "idle":
            out.append(f"push_state {m.push_state!r} (last_error {m.last_error!r})")
        if m._dirty_since is not None:
            out.append("the clone is still dirty")
        if getattr(m, "_last_pull_error", None):
            out.append(f"pull error persists: {m._last_pull_error}")
        legs = self.state()["legs"]
        for leg, ok in (("disk_to_server", {"idle"}), ("server_to_disk", {"idle", "merged"}),
                        ("disk_to_embed", {"idle"})):
            if legs[leg]["state"] not in ok:
                out.append(f"{leg} {legs[leg]['state']}: {legs[leg].get('reason')}")
        return out

    def check_reasons(self) -> list[str]:
        """Invariant (4): every refusal an actor met, every failed log event,
        and every server-stale chip carried a reason that names something."""
        out = []
        for r in self.ledger.refusals:
            if not r["reason"]:
                out.append(f"refusal without a reason: {r}")
            if r["actor"] == "human" and r["what"].startswith("hosted") and not r["files"]:
                out.append(f"412 without per-file detail: {r}")
        for e in self.client.get(
                f"/api/canvases/sync/log?env={self.env}&name={self.name}").json()["events"]:
            if not e.get("ok", True) and not (e.get("detail") or "").strip():
                out.append(f"failed log event without detail: {e['leg']} {e['action']}")
        for s in self.stale_seen:
            named = s.get("files") if "files" in s else (s.get("udfs") or s.get("layout"))
            if not named:
                out.append(f"server-stale chip names nothing: {s}")
        return out

    def check_all(self) -> list[str]:
        problems = []
        if not self.quiesce():
            problems.append("did not settle within 30 rounds")
        problems += [f"(1) {p}" for p in self.check_convergence()]
        lost, ties = self.ledger.lost_writes(self.final_values())
        self.tie_losses = ties
        problems += [f"(2) {p}" for p in lost]
        problems += [f"(3) {p}" for p in self.check_wedge()]
        problems += [f"(4) {p}" for p in self.check_reasons()]
        return problems

    # -- the random step ------------------------------------------------------------

    def step(self) -> None:
        self.ledger.step += 1
        rng = self.rng
        actions = [("tick", 30), ("frame_sync", 10), ("frame_deliver", 10), ("frame_drag", 6),
                   ("frame_body", 3), ("frame_delete", 1), ("turn", 6), ("s_body", 6),
                   ("s_row", 5), ("s_create", 2), ("s_delete", 1), ("h_body", 4), ("h_row", 3),
                   ("h_delete", 1), ("h_add", 1), ("h_pull", 2), ("k_touch", 1), ("k_meta", 1),
                   ("k_stale_probe", 1)]
        total = sum(w for _a, w in actions)
        pick = rng.uniform(0, total)
        for action, weight in actions:
            pick -= weight
            if pick <= 0:
                break
        getattr(self, "_do_" + action)()
        # The page follows disk_rev over the events socket within
        # milliseconds — far inside one actor step — so it has always cut
        # the delta for a disk move before the next actor acts.
        self.follow()

    def follow(self) -> None:
        """What the fs-event thread (`_watch_fs` → `_note_disk`) and the
        page's events socket do within ~100 ms of a write: disk_rev moves and
        the page cuts the delta. Both are far inside one actor step."""
        self.manager._note_disk()
        if self.manager.disk_rev != self.frame.cut_rev:
            self.frame.cut()

    def _choice(self, names):
        names = sorted(names)
        return self.rng.choice(names) if names else None

    def _do_tick(self):
        self.note("tick")
        self.tick()

    def _do_frame_sync(self):
        n = self.frame.apply_inbox()
        if n:
            self.note(f"frame applies {n} delta(s), rev {self.frame.rev}")

    def _do_frame_deliver(self):
        if self.frame.outbox:
            self.note(f"frame deliver {self.frame.outbox[0]['kind']}")
        self.frame.deliver()

    def _do_frame_drag(self):
        self.frame.apply_inbox()
        n = self._choice(self.frame.nodes)
        if n:
            x = self.uniq()
            self.note(f"frame drag {n} x={x}")
            self.frame.drag(n, x)

    def _do_frame_body(self):
        self.frame.apply_inbox()
        n = self._choice(n for n in self.frame.nodes if n in self.frame.files)
        if n:
            tok = self.tok("frame")
            self.note(f"frame body {n} {tok}")
            self.frame.edit_body(n, tok)

    def _do_frame_delete(self):
        self.frame.apply_inbox()
        n = self._choice(self.frame.nodes)
        if n and len(self.frame.nodes) > 1:
            tok = f"del_{self.tok('frame')}"
            self.note(f"frame delete {n}")
            self.frame.delete(n, tok)

    def _do_turn(self):
        if self.session.live:
            self.note("session turn ends (Stop hook)")
            self.session.end_turn()
        else:
            self.note("session turn starts")
            self.session.start_turn()

    def _session_target(self):
        return self._choice(self.session.disk_nodes()) if self.session.live else None

    def _do_s_body(self):
        n = self._session_target()
        if n:
            tok = self.tok("session")
            self.note(f"session body {n} {tok}")
            self.session.edit_body(n, tok)

    def _do_s_row(self):
        n = self._session_target()
        if n:
            if self.rng.random() < 0.5:
                v = f"tk_s{self.uniq()}"
                self.note(f"session title {n} {v}")
                self.session.edit_row(n, "title", v)
            else:
                x = self.uniq()
                self.note(f"session x {n} {x}")
                self.session.edit_row(n, "x", x)

    def _do_s_create(self):
        if self.session.live:
            name = f"s{self.uniq()}"
            ext = self.rng.choice([".py", ".json", ".md"])
            self.note(f"session create {name}{ext}")
            self.session.create(name, ext, self.tok("session"), self.uniq(), f"tk_st{self.uniq()}")

    def _do_s_delete(self):
        n = self._session_target()
        if n and len(self.session.disk_nodes()) > 1:
            self.note(f"session delete {n}")
            self.session.delete(n, f"del_{self.tok('session')}")

    def _human_reload_maybe(self):
        if self.rng.random() < 0.5:
            self.human.reload()

    def _do_h_body(self):
        self._human_reload_maybe()
        n = self._choice(self.server.udfs)
        if n:
            tok = self.tok("human")
            self.note(f"hosted body {n} {tok}")
            self.human.edit_body(n, tok)

    def _do_h_row(self):
        self._human_reload_maybe()
        n = self._choice(self.server.rows())
        if n:
            x = self.uniq()
            self.note(f"hosted move {n} x={x}")
            self.human.edit_row(n, "x", x)

    def _do_h_delete(self):
        self._human_reload_maybe()
        n = self._choice(self.server.udfs)
        if n and len(self.server.udfs) > 1:
            self.note(f"hosted delete {n}")
            self.human.delete(n, f"del_{self.tok('human')}")

    def _do_h_add(self):
        self._human_reload_maybe()
        name = f"h{self.uniq()}"
        self.note(f"hosted add {name}")
        self.human.add(name, self.rng.choice([".py", ".md"]), self.tok("human"), self.uniq(),
                       f"tk_ht{self.uniq()}")

    def _do_h_pull(self):
        body = self.human.pull()
        self.note(f"page Pull -> ok={body.get('ok')} {body.get('code') or ''}")

    def _do_k_touch(self):
        self.note("server: last_updated bump, no content")
        self.server.touch_stamp()

    def _do_k_meta(self):
        n = self._choice(self.server.udfs)
        if n:
            self.note(f"server: metadata rewrite {n} (hash moves, bytes equal)")
            self.server.rewrite_udf_metadata(n)

    def _do_k_stale_probe(self):
        self.note("server: next probe after an import answers the pre-import state")
        self.server.stale_probe_after_import = True

    def report(self, problems: list[str], tail: int = 60) -> str:
        try:
            events = self.client.get(
                f"/api/canvases/sync/log?env={self.env}&name={self.name}").json()["events"]
        except Exception:  # noqa: BLE001 — a report never fails
            events = []
        log = [f"{e['leg']}: {e['action']}" + (f" — {e['detail'][:140]}" if e.get("detail") else "")
               for e in events[-tail:]]
        return ("\n".join(problems) + "\n-- trace (last steps) --\n"
                + "\n".join(self.trace[-tail:])
                + "\n-- sync log (last events) --\n" + "\n".join(log))
