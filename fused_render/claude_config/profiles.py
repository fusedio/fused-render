"""Profiles feature endpoints (profiles.md).

A profile is a git branch over CLAUDE_DIR. The branch primitives live in lib.py
(the git layer's owner); this module composes them into the create/switch/delete
actions and enforces the guards.

main(action=...):
  list   -> {profiles: [{name, current, isDefault}], current}   (profiles.md §2)
  create -> {ok, name} | {ok:false, error}                      (profiles.md §3)
           params: name, from?
  switch -> {ok, current} | {ok:false, dirty, files} | {ok:false, error}
           params: name, message?                               (profiles.md §4)
  delete -> {ok, name} | {ok:false, error}                      (profiles.md §5)
           params: name
  export -> {ok, filename, b64} | {ok:false, error}             (profiles.md §6)
           params: name
  inspect -> {ok, entries:[{path,isDir,size}]} | {ok:false, error}   (profiles.md §7)
           params: b64
  import -> {ok, branch, imported} | {ok:false, dirty, files} | {ok:false, error}
           params: b64, paths (JSON list), branch, message?    (profiles.md §7)
"""
import base64
import io
import json
import re
import zipfile

from . import lib

_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _exists(name):
    return any(b["name"] == name for b in lib.branches())


def main(action: str = "list", name: str = "", **kwargs) -> dict:
    frm = kwargs.get("from") or kwargs.get("frm") or ""
    message = kwargs.get("message") or ""

    if action == "list":
        return {"profiles": lib.branches(), "current": lib.current_profile()}

    if action == "create":
        # profiles.md §3: validate ref name, reject flags and collisions.
        if not name or not _NAME_RE.match(name) or name.startswith("-"):
            return {"ok": False, "error": "invalid profile name"}
        if _exists(name):
            return {"ok": False, "error": "profile already exists"}
        if frm and not _exists(frm):
            return {"ok": False, "error": "unknown source profile"}
        try:
            with lib.config_lock():
                lib.create_branch(name, frm or None)
        except RuntimeError as e:
            # The regex still admits a few strings git rejects (a..b, .lock, HEAD).
            return {"ok": False, "error": f"invalid profile name: {e}"}
        return {"ok": True, "name": name}

    if action == "switch":
        # profiles.md §4: dirty-guarded checkout.
        if not _exists(name):
            return {"ok": False, "error": "unknown profile"}
        with lib.config_lock():
            st = lib.status()
            if st["dirty"]:
                if not message:
                    return {"ok": False, "dirty": True, "files": st["files"]}
                lib.commit(message)
            lib.switch_branch(name)
        return {"ok": True, "current": lib.current_profile()}

    if action == "delete":
        # profiles.md §5: safe delete, with user-actionable refusals.
        if name == lib.current_profile():
            return {"ok": False, "error": "cannot delete the current profile; switch away first"}
        prof = next((b for b in lib.branches() if b["name"] == name), None)
        if not prof:
            return {"ok": False, "error": "unknown profile"}
        if prof["isDefault"]:
            return {"ok": False, "error": "cannot delete the default profile"}
        try:
            with lib.config_lock():
                lib.delete_branch(name)
        except RuntimeError as e:
            if "not fully merged" in str(e):
                return {"ok": False,
                        "error": "profile has changes not present in another profile; cannot delete"}
            return {"ok": False, "error": str(e)}
        return {"ok": True, "name": name}

    if action == "export":
        # profiles.md §6: read-only .zip of the branch's tracked files, base64'd
        # over the JSON return. No lock, no commit. Filename stem only — the page
        # stamps the date (main() has no wall clock) and appends `.zip`.
        if not _exists(name):
            return {"ok": False, "error": "unknown profile"}
        try:
            data = lib.archive_zip(name)
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "filename": "claude-" + name.replace("/", "-"),
            "b64": base64.b64encode(data).decode("ascii"),
        }

    if action == "inspect":
        # profiles.md §7: list a zip's members so the page can render a picker.
        b64 = kwargs.get("b64") or ""
        try:
            zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(b64)))
        except (ValueError, zipfile.BadZipFile):
            return {"ok": False, "error": "not a valid .zip file"}
        entries = []
        for info in zf.infolist():
            entries.append({
                "path": info.filename,
                "isDir": info.filename.endswith("/"),
                "size": info.file_size,
            })
        return {"ok": True, "entries": entries}

    if action == "import":
        # profiles.md §7: overlay selected members onto a NEW branch off current.
        b64 = kwargs.get("b64") or ""
        branch = kwargs.get("branch") or ""
        try:
            paths = json.loads(kwargs.get("paths") or "[]")
        except ValueError:
            return {"ok": False, "error": "invalid paths"}
        if not branch or not _NAME_RE.match(branch) or branch.startswith("-"):
            return {"ok": False, "error": "invalid branch name"}
        if _exists(branch):
            return {"ok": False, "error": "profile already exists"}
        if not isinstance(paths, list) or not paths:
            return {"ok": False, "error": "no files selected"}
        try:
            zip_bytes = base64.b64decode(b64)
            zipfile.ZipFile(io.BytesIO(zip_bytes))  # validate before mutating
        except (ValueError, zipfile.BadZipFile):
            return {"ok": False, "error": "not a valid .zip file"}
        with lib.config_lock():
            st = lib.status()
            if st["dirty"]:
                message = kwargs.get("message") or ""
                if not message:
                    return {"ok": False, "dirty": True, "files": st["files"]}
                lib.commit(message)
            try:
                lib.create_branch(branch)
            except RuntimeError as e:
                return {"ok": False, "error": f"invalid branch name: {e}"}
            lib.switch_branch(branch)
            imported = lib.import_archive(zip_bytes, paths)
            lib.commit(f"Import into {branch}")
        return {"ok": True, "branch": branch, "imported": imported}

    return {"error": f"unknown action: {action}"}
