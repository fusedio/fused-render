"""Strip setuptools' shim and the jaraco init from py2app's frozen stdlib zip.

Usage: python scripts/_prune_stdlib_zip.py <path/to/python312.zip>

Contents/Resources/lib/python312.zip is on the BASE sys.path of every venv the
app builds (Contents/lib is a symlink to Resources/lib), ahead of the venv's own
site-packages. build_dmg.sh step 4a pruned setuptools/_distutils_hack from
lib/python3.12/ but the zip carried its own copies, so any source build using
the app's python imported the frozen `_distutils_hack.override` and died on
`No module named 'jaraco.text'`.

Stdlib only, no fused_render import: build_dmg.sh runs it with the build venv.
"""
import collections
import os
import stat
import sys
import tempfile
import zipfile

# Names that must not be importable from the zip at all.
PRUNE_PREFIXES = ("_distutils_hack/", "setuptools/", "pkg_resources/")
PRUNE_FILES = ("distutils-precedence.pth",)

# Dropping the init turns `jaraco` into a PEP 420 namespace package: a venv's
# jaraco.text merges in, while the zip's jaraco.classes/context/functools keep
# serving keyring. Deleting jaraco wholesale would break keyring.
NAMESPACE_INITS = ("jaraco/__init__.pyc", "jaraco/__init__.py")


def _doomed(name):
    return (
        name.startswith(PRUNE_PREFIXES)
        or name in PRUNE_FILES
        or name in NAMESPACE_INITS
    )


def prune(zip_path):
    """Rewrite zip_path without the doomed entries; return removed names sorted.

    No match means no rewrite: the file is left untouched.
    """
    tmp = None
    try:
        with zipfile.ZipFile(zip_path) as zin:
            infos = zin.infolist()
            removed = sorted(i.filename for i in infos if _doomed(i.filename))
            if not removed:
                return []
            fd, tmp = tempfile.mkstemp(
                suffix=".tmp", dir=os.path.dirname(os.path.abspath(zip_path))
            )
            os.close(fd)
            with zipfile.ZipFile(tmp, "w") as zout:
                for info in infos:
                    if not _doomed(info.filename):
                        zout.writestr(info, zin.read(info))
        # The source zip is closed here: Windows refuses to replace an open file.
        os.chmod(tmp, stat.S_IMODE(os.stat(zip_path).st_mode))
        os.replace(tmp, zip_path)
    except BaseException:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return removed


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: _prune_stdlib_zip.py <zip>\n")
        return 2
    path = argv[1]
    if not os.path.isfile(path):
        sys.stderr.write("FATAL: %s does not exist\n" % path)
        return 1
    removed = prune(path)
    print("removed %d entries from %s" % (len(removed), path))
    groups = collections.Counter(n.split("/", 1)[0] for n in removed)
    for g, count in sorted(groups.items()):
        print("    %s (%d)" % (g, count))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
