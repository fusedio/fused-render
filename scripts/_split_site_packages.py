"""Move every non-stdlib module out of py2app's stdlib locations into site-packages.

Usage: python scripts/_split_site_packages.py <Contents/Resources/lib/python3.12> <python312.zip>

py2app puts third-party packages in lib/python3.12/, python312.zip and
lib-dynload/, which are stdlib dirs on every venv's sys.path, ahead of its own
site-packages, so a venv could neither pin nor hide them. lib/python3.12/site-packages
is the base interpreter's real site-packages: the bundled python adds it and a
venv with include-system-site-packages = false does not.

Stdlib only, no fused_render import: build_dmg.sh runs it with the build venv,
whose interpreter is the one py2app copied into the bundle.
"""
import os
import re
import shutil
import stat
import sys
import sysconfig
import tempfile
import zipfile

SITE = "site-packages"
DYNLOAD = "lib-dynload"
META_SUFFIXES = (".dist-info", ".egg-info")
SITE_MODULE = "_fused_render_site"
SITE_PTH = SITE_MODULE + ".pth"
SITE_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), SITE_MODULE + ".py")
# Stdlib test/build-config modules missing from sys.stdlib_module_names; same rule as _site_isolation_check.py.
STDLIB_EXTRA = re.compile(r"^(test|_test\w*|_ctypes_test|_sysconfigdata_\w*|_?xx\w*|__phello__|__hello\w*__)$")


def stdlib_names(stdlib_dir=None):
    """Top-level names the base CPython install provides, test modules included."""
    names = set(sys.stdlib_module_names)
    stdlib_dir = stdlib_dir or sysconfig.get_paths()["stdlib"]
    for d in (stdlib_dir, os.path.join(stdlib_dir, DYNLOAD)):
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for entry in entries:
            name = module_name(entry)
            if name:
                names.add(name)
    return names


def module_name(entry):
    """The importable top-level name an entry provides, or None (metadata, data, dirs like lib-dynload)."""
    if entry.endswith(META_SUFFIXES) or entry == "__pycache__":
        return None
    name = entry.split(".", 1)[0]
    return name if name.isidentifier() else None


def third_party(entry, stdlib):
    """True for a top-level entry that belongs in site-packages."""
    if entry.endswith(META_SUFFIXES):
        return True
    name = module_name(entry)
    return name is not None and name not in stdlib and not STDLIB_EXTRA.match(name)


def _provides(site, name):
    """Does site-packages already hold a regular module or package called `name`?"""
    pkg = os.path.join(site, name)
    if os.path.isfile(os.path.join(pkg, "__init__.py")) or os.path.isfile(os.path.join(pkg, "__init__.pyc")):
        return True
    try:
        entries = os.listdir(site)
    except OSError:
        return False
    return any(os.path.isfile(os.path.join(site, e)) and e.split(".", 1)[0] == name
               and e.endswith((".py", ".pyc", ".so")) for e in entries)


def _move(src, dst):
    if os.path.lexists(dst):
        raise RuntimeError("both %s and %s exist; refusing to pick one" % (src, dst))
    os.rename(src, dst)


def _safe_member(name):
    parts = name.split("/")
    return not name.startswith("/") and ".." not in parts


def split(pylib, zip_path, stdlib=None):
    """Relocate third-party entries into pylib/site-packages; return a report dict.

    The whole-copied package dir wins over a zip copy of the same name: zip
    fragments beside a regular package were never imported, and a duplicate
    module is the same build-venv file.
    """
    stdlib = stdlib if stdlib is not None else stdlib_names()
    site = os.path.join(pylib, SITE)
    os.makedirs(site, exist_ok=True)
    report = {"dirs": [], "dynload": [], "extracted": 0, "dropped": 0}

    for entry in sorted(os.listdir(pylib)):
        if entry in (SITE, DYNLOAD) or not third_party(entry, stdlib):
            continue
        _move(os.path.join(pylib, entry), os.path.join(site, entry))
        report["dirs"].append(entry)

    dynload = os.path.join(pylib, DYNLOAD)
    if os.path.isdir(dynload):
        for entry in sorted(os.listdir(dynload)):
            if os.path.isfile(os.path.join(dynload, entry)) and third_party(entry, stdlib):
                _move(os.path.join(dynload, entry), os.path.join(site, entry))
                report["dynload"].append(entry)

    shadowed = {}
    tmp = None
    try:
        with zipfile.ZipFile(zip_path) as zin:
            infos = zin.infolist()
            keep = []
            for info in infos:
                top = info.filename.split("/", 1)[0]
                if not top or not third_party(top, stdlib):
                    keep.append(info)
                    continue
                name = module_name(top)
                if name is not None and name not in shadowed:
                    shadowed[name] = _provides(site, name)
                if info.is_dir():
                    continue
                parts = info.filename.split("/")
                dest = os.path.join(site, *parts)
                if ("__pycache__" in parts or (name is not None and shadowed[name])
                        or not _safe_member(info.filename) or os.path.lexists(dest)):
                    report["dropped"] += 1
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zin.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                mode = (info.external_attr >> 16) & 0o777
                os.chmod(dest, mode or 0o644)
                report["extracted"] += 1
            if len(keep) == len(infos):
                return report
            fd, tmp = tempfile.mkstemp(suffix=".tmp", dir=os.path.dirname(os.path.abspath(zip_path)))
            os.close(fd)
            with zipfile.ZipFile(tmp, "w") as zout:
                for info in keep:
                    zout.writestr(info, zin.read(info.filename))
        os.chmod(tmp, stat.S_IMODE(os.stat(zip_path).st_mode))
        os.replace(tmp, zip_path)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)
    return report


def install_site_hook(pylib):
    """Drop the user-site reordering hook into site-packages (see _fused_render_site.py)."""
    site = os.path.join(pylib, SITE)
    shutil.copyfile(SITE_SOURCE, os.path.join(site, SITE_MODULE + ".py"))
    with open(os.path.join(site, SITE_PTH), "w", encoding="utf-8") as fh:
        fh.write("import %s; %s.bundle_before_user_site()\n" % (SITE_MODULE, SITE_MODULE))


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: _split_site_packages.py <lib/python3.X> <python3X.zip>\n")
        return 2
    pylib, zip_path = argv[1], argv[2]
    for path, test in ((pylib, os.path.isdir), (zip_path, os.path.isfile)):
        if not test(path):
            sys.stderr.write("FATAL: %s does not exist\n" % path)
            return 1
    report = split(pylib, zip_path)
    install_site_hook(pylib)
    print("moved %d dirs/modules and %d lib-dynload extensions into %s/" % (
        len(report["dirs"]), len(report["dynload"]), SITE))
    print("    extracted %d zip entries, dropped %d shadowed/cache entries" % (
        report["extracted"], report["dropped"]))
    if report["dynload"]:
        print("    lib-dynload: %s" % ", ".join(report["dynload"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
