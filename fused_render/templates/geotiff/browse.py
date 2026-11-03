"""Directory listing for the nc_preview file explorer.

Returns the sub-directories and files of `dir` so the HTML can render a
navigable picker — no need to type paths by hand. Stdlib only.
"""


def _crumbs(dir):
    """Breadcrumb segments [{"label", "path"}, ...] from root to `dir`.

    `dir` is forward-slash canonical (see main()), so the TAIL is a plain
    split — but the ROOT is not a segment like the others and cannot be built
    by joining:

      * a Windows drive root is "C:/", never "C:". A bare "C:" is
        DRIVE-RELATIVE — it names the current directory on that drive rather
        than its top — so crumbing the root as "C:" made a click on it land
        wherever the serving process last happened to be on C:.
      * a UNC root is "//server/share". The leading "//" is eaten by the
        empty-segment filter, and a bare "//server" is not a path at all, so
        the share belongs to the root crumb instead of being crumbed alone.
      * a POSIX root contributes no crumb of its own: "/a/b" crumbs as
        "a" -> "/a", "b" -> "/a/b".

    A module-level helper rather than inline in main() so the two roots above
    can be tested directly — neither is constructible on a POSIX test host,
    where `dir` always comes back from os.path.abspath as "/...".
    """
    parts, acc, rest = [], "", dir
    if len(dir) > 1 and dir[1] == ":" and dir[0].isalpha():
        acc = dir[:2] + "/"               # "C:/", the real root of the drive
        parts.append({"label": dir[:2], "path": acc})
        rest = dir[2:]
    elif dir.startswith("//"):
        unc = [s for s in dir[2:].split("/") if s]
        acc = "//" + "/".join(unc[:2])    # server AND share, one root crumb
        parts.append({"label": acc, "path": acc})
        rest = "/".join(unc[2:])
    for seg in [s for s in rest.split("/") if s]:
        acc = (acc.rstrip("/") + "/" + seg) if acc else "/" + seg
        parts.append({"label": seg, "path": acc})
    return parts


def main(dir: str = "~", exts: str = ".nc", show_all: bool = False):
    import os

    # Canonicalized to forward slashes right away: os.path.abspath backslashes
    # EVERY separator on Windows, even a path that was already forward-slashed
    # (ntpath.normpath doesn't leave "/" alone), and every step after this one
    # — the "/"-only breadcrumb split below, and
    # each entry's joined path — has to agree on one separator or the
    # breadcrumbs collapse into a single garbled segment. Forward slash is the
    # form used everywhere else in the app (_view_url_codec.canonical_fs_path)
    # and Windows accepts it in every real filesystem call, so there is no
    # reason for this helper to be the one place still native-separator.
    dir = os.path.abspath(os.path.expanduser(dir or "~")).replace(os.sep, "/")

    allow = tuple(e.strip().lower() for e in exts.split(",") if e.strip())

    # Gather (name, is_dir, size) triples.
    if not os.path.isdir(dir):
        dir = os.path.dirname(dir) or "/"
    try:
        names = os.listdir(dir)
    except OSError as e:
        return {"error": f"cannot list {dir}: {e}", "dir": dir,
                "parent": os.path.dirname(dir)}
    triples = []
    for name in names:
        if name.startswith("."):            # hide dotfiles
            continue
        full = os.path.join(dir, name)
        try:
            is_dir = os.path.isdir(full)
            size = None if is_dir else os.path.getsize(full)
        except OSError:
            continue
        triples.append((name, is_dir, size))

    dirs, files = [], []
    for name, is_dir, size in triples:
        # os.path.join re-inserts a native (backslash) separator on Windows
        # even though `dir` is already forward-slash — re-canonicalize.
        full = os.path.join(dir, name).replace(os.sep, "/")
        if is_dir:
            dirs.append({"name": name, "path": full, "is_dir": True})
        else:
            ext = os.path.splitext(name)[1].lower()
            loadable = ext in allow
            if loadable or show_all:
                files.append({"name": name, "path": full, "is_dir": False,
                              "size": size, "ext": ext, "loadable": loadable})

    dirs.sort(key=lambda e: e["name"].lower())
    files.sort(key=lambda e: e["name"].lower())

    crumbs = _crumbs(dir)

    return {
        "dir": dir,
        "parent": os.path.dirname(dir),
        "crumbs": crumbs,
        "dirs": dirs,
        "files": files,
        "n_hidden_files": 0 if show_all else None,
    }


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
