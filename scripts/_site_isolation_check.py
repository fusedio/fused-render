"""Prove a venv built on the bundled interpreter cannot see, or be shadowed by, the bundle's packages.

Usage:
  <venv>/bin/python _site_isolation_check.py <bundle site-packages>
      run inside an empty venv made from the bundled python: fails if a bundled
      third-party name or distribution resolves from the venv, or wins over a
      package installed into the venv.
  <bundled python> _site_isolation_check.py --bundled <bundle site-packages>
      run by the bundled python itself: fails if a user-site package of the
      same name wins over the bundle's own.

Stdlib only; build_dmg.sh, build_linux_appimage.sh and build_windows_installer.ps1 run it.
Prints one "SITE-ISOLATION ok ..." or "SITE-ISOLATION FAIL: ..." line, exit 0/1.
"""
import importlib
import importlib.machinery
import importlib.metadata
import importlib.util
import os
import re
import subprocess
import sys
import sysconfig
import tempfile

FAKE_VERSION = "0.0.0.post999"
# Stdlib modules that sys.stdlib_module_names leaves out (test and build-config modules).
_STDLIB_EXTRA = re.compile(r"^(test|_test\w*|_ctypes_test|_sysconfigdata_\w*|_?xx\w*|__phello__|__hello\w*__)$")
_META = (".dist-info", ".egg-info")
# Imported by name from the bundle's .pth (scripts/_fused_render_site.py); a user-site fake of it would break the hook, not the order.
_HOOK = "_fused_render_site"


def _is_stdlib(name):
    return name in sys.stdlib_module_names or bool(_STDLIB_EXTRA.match(name))


def _under(path, root):
    path, root = (os.path.normcase(os.path.realpath(p)) for p in (path, root))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def top_level(entries, is_pkg_dir):
    """{name: "regular"|"namespace"} for importable top-level entries; is_pkg_dir(entry) -> bool|None (None = not a dir)."""
    suffixes = tuple(importlib.machinery.all_suffixes())
    found = {}
    for entry in entries:
        if entry.endswith(_META) or entry == "__pycache__":
            continue
        pkg = is_pkg_dir(entry)
        if pkg is None:
            name = entry.split(".", 1)[0]
            if entry.endswith(suffixes) and name.isidentifier():
                found[name] = "regular"
        elif entry.isidentifier():
            if pkg:
                found[entry] = "regular"
            else:
                found.setdefault(entry, "namespace")
    return found


def dir_top_level(path):
    def is_pkg_dir(entry):
        full = os.path.join(path, entry)
        if not os.path.isdir(full):
            return None
        return any(os.path.isfile(os.path.join(full, "__init__" + s)) for s in (".py", ".pyc"))
    return top_level(os.listdir(path), is_pkg_dir)


def listing(path):
    """(top-level importables, metadata dirs) of one sys.path entry: a directory or a zip."""
    if os.path.isdir(path):
        return dir_top_level(path), sorted(e for e in os.listdir(path) if e.endswith(_META))
    if not os.path.isfile(path):
        return {}, []
    import zipfile

    with zipfile.ZipFile(path) as z:
        names = z.namelist()
    tops = {n.split("/", 1)[0] for n in names if "/" in n}
    inits = {n.split("/", 1)[0] for n in names if n.count("/") == 1 and n.split("/")[1] in ("__init__.py", "__init__.pyc")}
    found = top_level([n for n in names if "/" not in n], lambda e: None)
    found.update(top_level(sorted(tops), lambda e: e in inits))
    return found, sorted(t for t in tops if t.endswith(_META))


def dist_names(site):
    """Project names of the dist-info/egg-info directories in `site`."""
    names = []
    for entry in sorted(os.listdir(site)):
        if not entry.endswith(_META):
            continue
        for meta in ("METADATA", "PKG-INFO"):
            try:
                with open(os.path.join(site, entry, meta), encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if line.lower().startswith("name:"):
                            names.append(line.split(":", 1)[1].strip())
                            break
                break
            except OSError:
                continue
    return sorted(set(names))


def _where(name):
    """Every location `name` would import from, or [] when it is not importable."""
    spec = importlib.util.find_spec(name)
    if spec is None:
        return []
    if spec.origin and spec.origin not in ("namespace", "built-in", "frozen"):
        return [spec.origin]
    return list(spec.submodule_search_locations or [])


def write_fakes(target, names, dists):
    """A stub regular package per name and a stub distribution per project, in `target`."""
    os.makedirs(target, exist_ok=True)
    for name in names:
        os.makedirs(os.path.join(target, name), exist_ok=True)
        with open(os.path.join(target, name, "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write("")
    for dist in dists:
        norm = re.sub(r"[-_.]+", "_", dist).lower()
        info = os.path.join(target, "%s-%s.dist-info" % (norm, FAKE_VERSION))
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "METADATA"), "w", encoding="utf-8") as fh:
            fh.write("Metadata-Version: 2.1\nName: %s\nVersion: %s\n" % (dist, FAKE_VERSION))
    importlib.invalidate_caches()


def check_venv(bundle_site):
    bad = []
    if sys.prefix == sys.base_prefix:
        return ["not running inside a venv (sys.prefix == sys.base_prefix == %s)" % sys.prefix], ""
    base_dirs = [p for p in sys.path if p and not _under(p, sys.prefix)]
    for p in base_dirs:
        if _under(p, bundle_site):
            bad.append("bundle site-packages is on the venv's sys.path: %s" % p)
            continue
        try:
            found, meta = listing(p)
        except (OSError, ValueError) as exc:
            bad.append("could not list %s: %s" % (p, exc))
            continue
        leaked = sorted(n for n, kind in found.items() if kind == "regular" and not _is_stdlib(n))
        if leaked:
            bad.append("%s holds non-stdlib %s" % (p, ", ".join(leaked)))
        if meta:
            bad.append("%s holds distribution metadata %s" % (p, ", ".join(meta)))

    names = sorted(dir_top_level(bundle_site))
    dists = dist_names(bundle_site)
    visible = sorted(n for n in names if _where(n))
    if visible:
        bad.append("bundled names importable in an empty venv: %s" % ", ".join(visible))
    foreign = sorted({d.metadata.get("Name") or str(d.locate_file("")) for d in importlib.metadata.distributions()
                      if not _under(str(d.locate_file("")), sys.prefix)})
    if foreign:
        bad.append("distributions visible from outside the venv: %s" % ", ".join(foreign))

    purelib = sysconfig.get_paths()["purelib"]
    at = [i for i, p in enumerate(sys.path) if p and _under(p, purelib) and _under(purelib, p)]
    if not at:
        return bad + ["the venv's own site-packages %s is not on sys.path" % purelib], ""
    # Stand-ins for venv-installed packages, at the venv site-packages' own sys.path slot; the venv stays untouched.
    with tempfile.TemporaryDirectory() as installed:
        write_fakes(installed, names, dists)
        sys.path.insert(at[0], installed)
        shadowed = sorted(n for n in names if not all(_under(w, installed) for w in _where(n) or ["<missing>"]))
        if shadowed:
            bad.append("venv-installed packages shadowed by the bundle: %s" % ", ".join(
                "%s -> %s" % (n, _where(n)[:1]) for n in shadowed))
        wrong = sorted(d for d in dists if _version(d) != FAKE_VERSION)
        if wrong:
            bad.append("venv-installed distributions shadowed: %s" % ", ".join(
                "%s==%s" % (d, _version(d)) for d in wrong))
        sys.path.remove(installed)
    return bad or None, "%d names, %d distributions" % (len(names), len(dists))


def _version(dist):
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def check_bundled_child(bundle_site):
    import site

    bad = []
    user = site.USER_SITE
    paths = [os.path.realpath(p) for p in sys.path if p]
    if os.path.realpath(bundle_site) not in paths:
        return ["bundle site-packages is not on the bundled interpreter's sys.path"], ""
    if not user or os.path.realpath(user) not in paths:
        return ["the user site %s was not added; nothing was tested" % user], ""
    regular = sorted(n for n, kind in dir_top_level(bundle_site).items() if kind == "regular" and n != _HOOK)
    lost = sorted(n for n in regular if not any(_under(w, bundle_site) for w in _where(n)))
    if lost:
        bad.append("user-site packages win over the bundle: %s" % ", ".join(lost))
    wrong = sorted(d for d in dist_names(bundle_site) if _version(d) == FAKE_VERSION)
    if wrong:
        bad.append("user-site distributions win over the bundle: %s" % ", ".join(wrong))
    return bad or None, "%d names against a populated user site" % len(regular)


def check_bundled(bundle_site):
    if sys.prefix != sys.base_prefix:
        return ["--bundled must run on the bundled interpreter, not a venv"], ""
    with tempfile.TemporaryDirectory() as userbase:
        scheme = sysconfig.get_preferred_scheme("user")
        user_site = sysconfig.get_path("purelib", scheme, vars={"userbase": userbase})
        write_fakes(user_site, sorted(n for n in dir_top_level(bundle_site) if n != _HOOK), dist_names(bundle_site))
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONNOUSERSITE", "VIRTUAL_ENV")}
        env["PYTHONUSERBASE"] = userbase
        proc = subprocess.run([sys.executable, "-B", os.path.abspath(__file__), "--bundled-child", bundle_site],
                              env=env, capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return [out or "child exited %d" % proc.returncode], ""
    return None, out.split("SITE-ISOLATION ok ", 1)[-1]


def main(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    # This script's own dir (sys.path[0] without -I/-P) holds build scripts like app_entry.py, not the bundle.
    sys.path[:] = [p for p in sys.path if p and not _under(p, here)]
    mode, args = "venv", argv[1:]
    if args and args[0] in ("--bundled", "--bundled-child"):
        mode, args = args[0][2:], args[1:]
    if len(args) != 1 or not os.path.isdir(args[0]):
        sys.stderr.write("usage: _site_isolation_check.py [--bundled] <bundle site-packages dir>\n")
        return 2
    check = {"venv": check_venv, "bundled": check_bundled, "bundled-child": check_bundled_child}[mode]
    bad, summary = check(args[0])
    if bad:
        print("SITE-ISOLATION FAIL (%s): %s" % (mode, "; ".join(bad)))
        return 1
    print("SITE-ISOLATION ok %s" % summary)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
