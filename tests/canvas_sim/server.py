"""The fake Fused control plane and the in-process fused CLI / shims.

`FakeServer` is one canvas (collection) as the application server holds it
after the revision program (contract §1): `revision` per collection and per
UDF, If-Match on every write (412 with the contract's `detail`), a lease, and
an old-server mode that has none of those keys. Adversarial knobs model the
ledger incidents: a widget body re-serialised on read, a `last_updated` bump
with no content change, a UDF metadata rewrite that moves its hash with
identical exported bytes (HLS_Tiff), and a manifest probe that answers the
pre-import state once after an import (read-after-write lag).

`SimProcs` replaces the `subprocess` NAME inside fused_render.canvases (see
`install`): every `subprocess.run` / `Popen` the module makes — the manifest,
zip and lease shims, `canvas push|pull|validate` — is answered here, in
process, from the FakeServer. The real exception classes are kept so the
module's `except subprocess.TimeoutExpired` clauses still match.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess as _real_subprocess
import time
import tomllib
import zipfile

from fused_render import canvas_snapshot

FOLDER = canvas_snapshot.FOLDER_TYPE


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class Udf:
    __slots__ = ("slug", "file", "text", "revision", "id", "salt", "stamp")

    def __init__(self, slug, file, text, revision, sid, stamp):
        self.slug, self.file, self.text = slug, file, text
        self.revision, self.id, self.salt, self.stamp = revision, sid, 0, stamp

    @property
    def hash(self) -> str:
        # The server's udf_body_hash is over the serialised udf_body (code
        # plus metadata), never the exported file's bytes: `salt` stands in
        # for the metadata, so a metadata-only rewrite moves the hash while
        # the exported file stays byte-identical (the HLS_Tiff incident).
        return _md5(f"{self.salt}:{self.text}".encode())


class FakeServer:
    """One canvas on the fake control plane. All writes go through `_commit`,
    which applies the contract's bump rule."""

    def __init__(self, name: str, *, revision_mode: bool = True):
        self.name = name
        self.id = f"c-{name}"
        self.revision_mode = revision_mode
        self.revision = 1
        self._stamp_n = 0
        self.last_updated = self._next_stamp()
        self.doc: dict = {"type": "canvas", "canvas": {"nodes": [], "edges": []}}
        self.udfs: dict[str, Udf] = {}
        self._ids = 0
        self.lease_owner: str | None = None
        self.lease_until: float | None = None
        self.clock_offset = 0.0
        # -- adversarial knobs --------------------------------------------
        self.reserialize_widgets = False      # .json exported re-dumped
        self.stale_probe_after_import = False  # one pre-import manifest
        self._stale_manifest: dict | None = None
        # -- observability ---------------------------------------------------
        self.writes: list[dict] = []          # every accepted server write
        self.refusals: list[dict] = []        # every 412 / 409 answered
        self.imports = 0
        self.import_log: list[dict] = []

    # -- clock / stamps ------------------------------------------------------

    def now(self) -> float:
        return time.time() + self.clock_offset

    def _next_stamp(self) -> str:
        self._stamp_n += 1
        return f"2026-11-11T00:{self._stamp_n // 60:02d}:{self._stamp_n % 60:02d}.{self._stamp_n:06d}"

    def _new_id(self, slug: str) -> str:
        self._ids += 1
        return f"udf-{slug}-{self._ids}"

    # -- seeding -------------------------------------------------------------

    def seed(self, rows: list[dict], files: dict[str, str], edges=()) -> None:
        self.doc = {"type": "canvas", "canvas": {"nodes": [dict(r) for r in rows],
                                                  "edges": [list(e) for e in edges]}}
        for file, text in files.items():
            slug = os.path.splitext(file)[0]
            self.udfs[slug] = Udf(slug, file, text, 1, self._new_id(slug), self.last_updated)

    # -- reads -----------------------------------------------------------------

    def rows(self) -> dict[str, dict]:
        return canvas_snapshot.rows_of_doc(self.doc)["nodes"]

    def manifest(self, *, consume_stale: bool = True) -> dict:
        if consume_stale and self._stale_manifest is not None:
            stale, self._stale_manifest = self._stale_manifest, None
            return stale
        udfs = {}
        for slug, u in self.udfs.items():
            info = {"hash": u.hash, "last_updated": u.stamp, "id": u.id}
            if self.revision_mode:
                info["revision"] = u.revision
            udfs[slug] = info
        out = {"id": self.id, "last_updated": self.last_updated, "udfs": udfs}
        if self.revision_mode:
            out["revision"] = self.revision
            out["lease_owner"] = self.lease_owner
            out["lease_until"] = (None if self.lease_until is None
                                  else _iso(self.lease_until))
        return out

    def export_file(self, u: Udf) -> str:
        if self.reserialize_widgets and u.file.endswith(".json"):
            try:
                return json.dumps(json.loads(u.text), indent=2, sort_keys=True) + "\n"
            except ValueError:
                return u.text
        return u.text

    def export(self) -> dict[str, bytes]:
        """What `fused canvas pull` / the zip shim hand out. canvas.toml is
        re-serialised from the stored rows (the real server rebuilds it from
        its database, never the bytes pushed)."""
        out = {"canvas.toml": canvas_snapshot._dump_toml(self.doc).encode("utf-8")}
        for u in self.udfs.values():
            out[u.file] = self.export_file(u).encode("utf-8")
        return out

    def detail(self) -> dict:
        return {"code": "revision_mismatch", "revision": self.revision,
                "last_updated": self.last_updated,
                "udfs": {s: {"id": u.id, "revision": u.revision, "udf_body_hash": u.hash}
                         for s, u in self.udfs.items()}}

    # -- the bump rule ---------------------------------------------------------

    def _cas(self, if_match, who: str) -> dict | None:
        """None when the write may go ahead; the 412 detail otherwise."""
        if not self.revision_mode or if_match is None:
            return None  # missing header (or an old server): unconditional
        if _if_match(if_match) == self.revision:
            return None
        detail = self.detail()
        self.refusals.append({"who": who, "status": 412, "detail": detail})
        return detail

    def _commit(self, who: str, what: str, *, udfs_changed=(), content=True) -> None:
        stamp = self._next_stamp()
        self.last_updated = stamp
        for slug in udfs_changed:
            u = self.udfs.get(slug)
            if u is not None:
                u.revision += 1
                u.stamp = stamp
        if content:
            self.revision += 1
        self.writes.append({"who": who, "what": what, "revision": self.revision,
                            "udfs": list(udfs_changed)})

    # -- hosted-tab writes (the Human actor) ----------------------------------

    def _row(self, slug: str) -> dict | None:
        for r in self.doc["canvas"]["nodes"]:
            if canvas_snapshot.row_name(r) == slug:
                return r
        return None

    def hosted_save_udf(self, slug: str, text: str, if_match=None):
        if (d := self._cas(if_match, "hosted")) is not None:
            return 412, d
        u = self.udfs.get(slug)
        if u is None:
            return 404, {"code": "not_found", "slug": slug}
        if u.text == text:
            # A byte-identical re-save: last_updated moves, no revision bumps.
            self.touch_stamp()
            return 200, {"revision": self.revision}
        u.text = text
        self._commit("hosted", f"save {slug}", udfs_changed=[slug])
        return 200, {"revision": self.revision}

    def hosted_row(self, slug: str, if_match=None, **fields):
        if (d := self._cas(if_match, "hosted")) is not None:
            return 412, d
        row = self._row(slug)
        if row is None:
            return 404, {"code": "not_found", "slug": slug}
        row.update(fields)
        self._commit("hosted", f"row {slug} {fields}")
        return 200, {"revision": self.revision}

    def hosted_folder(self, name: str, children: list[str], if_match=None):
        if (d := self._cas(if_match, "hosted")) is not None:
            return 412, d
        self.doc["canvas"]["nodes"].append({
            "udfName": name, "folderName": name, "type": FOLDER, "x": 0, "y": -900,
            "zIndex": 0, "width": 2400, "height": 800, "title": name,
            "childUdfOrder": list(children)})
        self._commit("hosted", f"folder {name}")
        return 200, {"revision": self.revision}

    def hosted_delete(self, slug: str, if_match=None):
        if (d := self._cas(if_match, "hosted")) is not None:
            return 412, d
        if slug not in self.udfs:
            return 404, {"code": "not_found", "slug": slug}
        del self.udfs[slug]
        canvas = self.doc["canvas"]
        canvas["nodes"] = [r for r in canvas["nodes"] if canvas_snapshot.row_name(r) != slug]
        canvas["edges"] = [e for e in canvas.get("edges") or [] if slug not in e]
        self._commit("hosted", f"delete {slug}")
        return 200, {"revision": self.revision}

    def hosted_add(self, slug: str, file: str, text: str, row: dict, if_match=None):
        if (d := self._cas(if_match, "hosted")) is not None:
            return 412, d
        self.udfs[slug] = Udf(slug, file, text, 1, self._new_id(slug), self.last_updated)
        self.doc["canvas"]["nodes"].append(dict(row, udfName=slug))
        self._commit("hosted", f"add {slug}")
        return 200, {"revision": self.revision}

    # -- adversarial knobs -----------------------------------------------------

    def recreate(self) -> None:
        """The canvas was deleted and created again under the same name: a
        NEW collection id, revision back at 1, every UDF a new row (new ids,
        revision 1) with the same content. The engine keys its base by id +
        base_revision, so it must not read revision 1 < base as "older"."""
        self.id = f"c-{self.name}-r{self._next_stamp()[-6:]}"
        self.revision = 1
        self.lease_owner = self.lease_until = None
        stamp = self._next_stamp()
        self.last_updated = stamp
        for slug, u in self.udfs.items():
            u.id, u.revision, u.stamp = self._new_id(slug), 1, stamp
        self.writes.append({"who": "server", "what": "recreate", "revision": 1, "udfs": []})

    def touch_stamp(self) -> None:
        """A write that changes no content (share settings, a preview image
        re-sign): `last_updated` moves, the revision does not."""
        self.last_updated = self._next_stamp()
        self.writes.append({"who": "server", "what": "touch", "revision": self.revision,
                            "udfs": []})

    def rewrite_udf_metadata(self, slug: str) -> None:
        """HLS_Tiff: the server rewrote a UDF's metadata — `udf_body_hash`
        moves, the exported file is byte-identical. A udf-row write, so both
        revisions bump in revision mode."""
        u = self.udfs[slug]
        u.salt += 1
        self._commit("server", f"metadata {slug}", udfs_changed=[slug])

    # -- the CLI push (import-toml) --------------------------------------------

    def validate_dir(self, top: str, skip=()) -> list[str]:
        try:
            with open(os.path.join(top, "canvas.toml"), "rb") as fh:
                doc = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return [f"error: canvas.toml: {exc}"]
        files = canvas_snapshot.node_files(top)
        rows = canvas_snapshot.rows_of_doc(doc)
        errors = []
        for name, row in rows["nodes"].items():
            if row.get("type") != FOLDER and name not in files and "node_files" not in skip:
                errors.append(f"error: node '{name}' has no source file")
        for a, b in rows["edges"]:
            if a not in rows["nodes"] or b not in rows["nodes"]:
                errors.append(f"error: edge {a} -> {b} names a missing node")
        return errors

    def import_dir(self, top: str, if_match=None, validate=True) -> tuple[int, str, str]:
        if self.revision_mode and if_match is not None and _if_match(if_match) != self.revision:
            detail = self._cas(if_match, "cli")
            return 3, "", "Error: the canvas changed on the server\n" + json.dumps(detail) + "\n"
        if validate:
            errors = self.validate_dir(top)
            if errors:
                return 1, "", "\n".join(errors + [
                    f"Error: fused canvas push failed with {len(errors)} error(s)"]) + "\n"
        with open(os.path.join(top, "canvas.toml"), "rb") as fh:
            doc = tomllib.load(fh)
        files = canvas_snapshot.node_files(top)
        self.import_log.append({"rows": sorted(canvas_snapshot.rows_of_doc(doc)["nodes"]),
                                "files": sorted(files), "if_match": if_match})
        pre = self.manifest(consume_stale=False)
        changed: list[str] = []
        new_udfs: dict[str, Udf] = {}
        for slug, file in files.items():
            with open(os.path.join(top, file), encoding="utf-8", newline="") as fh:
                text = fh.read()
            u = self.udfs.get(slug)
            if u is not None and u.file == file and (u.text == text or self.export_file(u) == text):
                new_udfs[slug] = u
                continue
            if u is None or u.file != file:
                u = Udf(slug, file, text, 0, self._new_id(slug), self.last_updated)
            else:
                u.text = text
            new_udfs[slug] = u
            changed.append(slug)
        removed = sorted(set(self.udfs) - set(new_udfs))
        doc_changed = (canvas_snapshot.rows_of_doc(doc) != canvas_snapshot.rows_of_doc(self.doc))
        self.udfs = new_udfs
        self.doc = doc
        self.imports += 1
        content = bool(changed or removed or doc_changed)
        self._commit("cli", "import" + (f" {changed}" if changed else "")
                     + (f" -{removed}" if removed else ""), udfs_changed=changed,
                     content=content)
        if self.stale_probe_after_import:
            self._stale_manifest = pre
        return 0, f"Pushed.\nRevision: {self.revision}\n", ""

    # -- lease -----------------------------------------------------------------

    def lease(self, action: str, owner: str, ttl: float) -> dict:
        if not self.revision_mode:
            return {"ok": False, "code": "unsupported"}
        now = self.now()
        if action == "release":
            if self.lease_owner == owner:
                self.lease_owner = self.lease_until = None
            return {"ok": True}
        if (self.lease_owner is None or self.lease_owner == owner
                or (self.lease_until or 0) < now):
            self.lease_owner = owner
            self.lease_until = now + min(float(ttl), 600.0)
            return {"ok": True, "owner": owner, "lease_until": _iso(self.lease_until)}
        out = {"ok": False, "code": "leased", "owner": self.lease_owner,
               "lease_until": _iso(self.lease_until)}
        self.refusals.append({"who": "lease", "status": 409, "detail": out})
        return out


def _if_match(value) -> int:
    """`42`, `"42"` and `W/"42"` all name revision 42 (coordinator, final
    server semantics)."""
    text = str(value).strip()
    if text.startswith("W/"):
        text = text[2:]
    return int(text.strip('"'))


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + f".{int(epoch % 1 * 1e6):06d}+00:00"


# -- the subprocess stand-in ---------------------------------------------------


class _FakePopen:
    """Popen for the push CLI: the work happens in `communicate`, the way
    `_run_push_cli` reads it."""

    pid = 0

    def __init__(self, procs: "SimProcs", argv: list[str], kw: dict):
        self._procs, self.args, self._kw = procs, list(argv), kw
        self.returncode: int | None = None

    def communicate(self, input=None, timeout=None):
        res = self._procs.run(self.args, text=True)
        self.returncode = res.returncode
        return res.stdout, res.stderr

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class SimProcs:
    """Stands in for the `subprocess` module inside fused_render.canvases."""

    TimeoutExpired = _real_subprocess.TimeoutExpired
    CompletedProcess = _real_subprocess.CompletedProcess
    CalledProcessError = _real_subprocess.CalledProcessError
    SubprocessError = _real_subprocess.SubprocessError
    PIPE = _real_subprocess.PIPE
    DEVNULL = _real_subprocess.DEVNULL
    STDOUT = _real_subprocess.STDOUT

    def __init__(self):
        self.servers: dict[str, FakeServer] = {}
        self.calls: list[tuple[str, list[str], int]] = []
        # Callables run inside a push, after the CLI started and before the
        # import lands (a hosted save racing the push → its CAS 412).
        self.during_push: list = []

    def add(self, server: FakeServer) -> FakeServer:
        self.servers[server.name] = server
        return server

    def _by_id(self, cid: str) -> FakeServer | None:
        for s in self.servers.values():
            if s.id == cid or s.name == cid:
                return s
        return None

    def Popen(self, argv, **kw):  # noqa: N802 — the module's own name
        return _FakePopen(self, argv, kw)

    def run(self, argv, **kw):
        argv = [str(a) for a in argv]
        text = bool(kw.get("text") or kw.get("encoding") or kw.get("universal_newlines"))
        kind, code, out, err = self._dispatch(argv)
        self.calls.append((kind, argv, code))
        if not text:
            out = out.encode("utf-8") if isinstance(out, str) else out
            err = err.encode("utf-8") if isinstance(err, str) else err
        elif isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return _real_subprocess.CompletedProcess(argv, code, out, err)

    def kinds(self, kind: str) -> list[list[str]]:
        return [a for k, a, _c in self.calls if k == kind]

    # -- routing ---------------------------------------------------------------

    def _dispatch(self, argv: list[str]):
        base = [os.path.basename(a) for a in argv]
        for shim, handler in (("_fused_canvas_manifest.py", self._manifest),
                              ("_fused_canvas_zip.py", self._zip),
                              ("_fused_canvas_lease.py", self._lease)):
            if shim in base:
                return (shim[len("_fused_canvas_"):-3],) + handler(argv[base.index(shim) + 1:])
        if "canvas" in argv:
            i = argv.index("canvas")
            verb = argv[i + 1] if i + 1 < len(argv) else ""
            rest = argv[i + 2:]
            handler = {"push": self._push, "pull": self._pull, "validate": self._validate,
                       "list": self._list, "create": self._noop,
                       "delete": self._noop}.get(verb)
            if handler is not None:
                return (verb,) + handler(rest)
        if "whoami" in argv:
            return ("whoami", 0, json.dumps({"handle": "tester"}), "")
        return ("unknown", 2, "", f"Error: unknown sim command {argv!r}\n")

    @staticmethod
    def _flag(args: list[str], name: str) -> str | None:
        if name in args:
            i = args.index(name)
            return args[i + 1] if i + 1 < len(args) else None
        return None

    def _manifest(self, args):
        s = self.servers.get(args[0]) if args else None
        if s is None:
            return 1, "", "Error: no such canvas\n"
        return 0, json.dumps(s.manifest()), ""

    def _zip(self, args):
        s = self._by_id(args[0]) if args else None
        if s is None:
            return 1, b"", b"no such collection\n"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for rel, data in s.export().items():
                zf.writestr(rel, data)
        return 0, buf.getvalue(), b""

    def _lease(self, args):
        # [acquire|release, <collection_id>, <owner>, <ttl_s>]
        if len(args) < 3:
            return 2, "", "usage\n"
        action, cid, owner = args[0], args[1], args[2]
        ttl = float(args[3]) if len(args) > 3 else 90.0
        s = self._by_id(cid)
        if s is None:
            return 1, "", "no such collection\n"
        return 0, json.dumps(s.lease(action, owner, ttl)) + "\n", ""

    def _push(self, args):
        top = args[0]
        name = self._flag(args, "--canvas")
        s = self.servers.get(name)
        if s is None:
            return 1, "", f"Error: no canvas {name!r}\n"
        for hook in list(self.during_push):
            hook()
        return s.import_dir(top, if_match=self._flag(args, "--if-match"),
                            validate="--no-validate" not in args)

    def _pull(self, args):
        name, out = args[0], self._flag(args, "-o")
        s = self.servers.get(name)
        if s is None or out is None:
            return 1, "", "Error: pull failed\n"
        bundle = s.export()
        if "--dry-run" in args:
            diff = False
            for rel, data in bundle.items():
                try:
                    with open(os.path.join(out, rel), "rb") as fh:
                        if fh.read() != data:
                            diff = True
                except OSError:
                    diff = True
            for root, dirs, names in os.walk(out):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for n in names:
                    rel = os.path.relpath(os.path.join(root, n), out).replace(os.sep, "/")
                    if n != "_shared.fused" and rel not in bundle:
                        diff = True
            return 0, ("Would write files (local differs from the canvas)." if diff else
                       "Nothing to do: already up to date (local files match the canvas)."), ""
        os.makedirs(out, exist_ok=True)
        if "--force" in args:
            for root, dirs, names in os.walk(out):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for n in names:
                    rel = os.path.relpath(os.path.join(root, n), out).replace(os.sep, "/")
                    if n != "_shared.fused" and rel not in bundle:
                        os.remove(os.path.join(root, n))
        for rel, data in bundle.items():
            with open(os.path.join(out, rel), "wb") as fh:
                fh.write(data)
        return 0, "", ""

    def _validate(self, args):
        skip = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--skip"]
        errors = FakeServer("x").validate_dir(args[0], skip=skip)
        return (1 if errors else 0), "", "\n".join(errors) + ("\n" if errors else "")

    def _list(self, args):
        return 0, json.dumps(sorted(self.servers)), ""

    def _noop(self, args):
        return 0, "", ""

