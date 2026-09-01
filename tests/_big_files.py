"""Files that REPORT a size without spending it on the disk.

A handful of tests here are about a number the app derives from how big a
model file is — a budget-aware download picking a smaller quantisation, a
cached repo's `size_gb`. What those tests need is a file whose `st_size` is
multi-gigabyte; the bytes inside it are never read. Writing them for real
costs both wall clock and disk (one 5GB `write_bytes` was 67s of the suite's
295s, and repeated local runs filled the disk), so set the length instead of
writing data: `ftruncate` is constant time and allocates nothing.

That is also what a real download looks like on this disk. `worker_base`'s
own docstring for `folder_bytes` spells it out: our segments write out of
order, so a `.fusedpart` is created at its final size with `ftruncate` and
filled sparsely.

THE ONE PLACE THIS IS WRONG is a partial file — `.fusedpart` or hf's
`.incomplete`. Download PROGRESS is deliberately measured by allocated
BLOCKS rather than by length, for exactly the reason above: a sparse file's
length is its eventual size, not what has arrived. A sparse `.fusedpart`
would report zero bytes downloaded, so those fixtures have to write real
bytes — which is fine, they are sized in megabytes rather than gigabytes.
"""

import ctypes
import os
import sys

#: FSCTL_SET_SPARSE — see `_mark_sparse`.
_FSCTL_SET_SPARSE = 0x000900C4


def _mark_sparse(fd: int) -> None:
    """Ask NTFS not to reserve the clusters. Best effort, never fatal.

    A POSIX filesystem makes this sparse implicitly: extending the length
    without writing allocates nothing at all. NTFS does not — it sets EOF
    and reserves the space, so a 5GB fixture would still cost 5GB of a
    Windows runner's rather small disk. It stays fast there either way (no
    data is written), which is why failing to set the flag is not an error:
    the worst case is the disk usage we already had before this helper.
    """
    if sys.platform != "win32":
        return
    try:
        import msvcrt

        returned = ctypes.c_ulong(0)
        ctypes.windll.kernel32.DeviceIoControl(
            ctypes.c_void_p(msvcrt.get_osfhandle(fd)), _FSCTL_SET_SPARSE,
            None, 0, None, 0, ctypes.byref(returned), None)
    except Exception:
        pass


def sparse_file(path, size: int) -> None:
    """Create `path` as a `size`-byte file holding no data.

    `os.stat(path).st_size == size`, so every reader that sums or compares
    lengths — `os.path.getsize`, `hub_cache`'s repo walk — sees `size`. Reads
    return NUL bytes. Read the module docstring before reaching for this in a
    partial-download fixture.
    """
    parent = os.path.dirname(os.fspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as f:
        _mark_sparse(f.fileno())
        f.truncate(size)
