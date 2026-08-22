"""Text-to-video (+ audio) on the bundled `h3.c` binary (SPEC §40).

Every other runner in this folder loads a model INTO its own interpreter and
calls a library function to render. This one does not: MiniMax H3's only
working engine on Apple Silicon is `antirez/h3.c`, a standalone Metal binary
this app bundles the way it bundles rclone (`registry.h3_bin()`, modeled on
`shell/mounts/rcd.py::rclone_bin()`). So this worker's job is narrower than its
siblings' — spawn that binary as a subprocess per render, speak its stdout for
progress, and make its one dependency (a real ffmpeg executable, for the mux
step that lands the audio) available to it.

**The binary path arrives through the environment, not through a resolution
ladder run again in here.** The supervisor already ran
`registry.h3_bin()` once, when it decided this capability was available at
all, and hands the answer down as `FUSED_RENDER_H3_BIN` in the child's
environment when it starts this resident (`supervisor.py::_start_resident`).
Re-resolving here would mean two processes could, in principle, disagree about
which binary is running — the same reason `worker_base` never re-derives
anything the parent already decided and handed over.

**Cancellation has exactly one place to happen: between progress lines.** The
h3 process is a plain subprocess this worker owns outright — unlike the
library runners, whose only interruption point is a callback inside an opaque
C call, here the interruption point is "read the next line of stdout, ask the
job row if the ✕ was pressed, and if so terminate the child" — which is a real
process kill, not an exception unwound through a call stack.
"""

import os
import re
import subprocess
import sys
import threading
import time

# The base sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The downloaded snapshot directory and the model id it came from. One per
#: process, set once at `load` time — there is nothing else to "load": the h3
#: binary reads the snapshot itself, per invocation, via `-d`.
_loaded = {}

#: h3's own per-step progress lines look like "...N/M...": some digits, a
#: slash, more digits, somewhere in the line. A generic pattern rather than an
#: anchored one, because the exact wording around the numbers is the binary's
#: to change and is not a contract this worker can pin.
_STEP_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

#: The one file every Diffusers pipeline snapshot carries at its root — the
#: pipeline manifest `DiffusionPipeline.save_pretrained` writes. h3.c reads its
#: own checkpoint layout directly and has no such manifest, so its presence
#: means "this is the wrong engine for this repo" well before spawning the
#: binary on it, the same "refuse by name before touching the library" shape
#: `mflux_image`'s `load` uses for a repo its own variant table does not know.
_DIFFUSERS_MARKER = "model_index.json"


def download(model_id):
    """The whole repo, and nothing clever — h3.c reads its checkpoint as one
    directory, exactly like `mflux_image`'s single-repo download."""
    return worker_base.download_snapshot(model_id)


def load(model_id, fetched):
    """`fetched` is what `download` returned — the snapshot directory.

    There is no weight loading here: h3.c takes the snapshot path itself on
    each invocation (`-d`). What this DOES have to check, once, is that the
    snapshot is a shape h3.c can read at all — refusing now, with a sentence,
    is the same trade `mflux_image.load` makes for a repo in the wrong format;
    the alternative is a render that fails minutes later with h3's own opaque
    exit code.
    """
    if os.path.isfile(os.path.join(fetched, _DIFFUSERS_MARKER)):
        raise RuntimeError(
            f"{model_id} is a Diffusers video pipeline snapshot, not an H3 "
            "checkpoint — h3.c reads its own layout directly and cannot open "
            "one of these. There is no other local engine for video "
            "generation in this build.")
    if not os.environ.get("FUSED_RENDER_H3_BIN"):
        raise RuntimeError(
            "the h3 binary path was not provided to this worker — the "
            "supervisor should not have started this resident without it")
    _loaded["snapshot"] = fetched
    _loaded["model_id"] = model_id
    # h3.c is Metal-only, like every other Apple-Silicon-gated runner here —
    # there is nothing to detect, but the page still shows this field.
    worker_base.set_state(device="mps")


def memory():
    """No python-side model to measure: the weights live in the h3 process,
    not this one. `worker_base` falls back to RSS, which for this worker is
    genuinely the right answer — there is nothing this process is holding
    beyond its own interpreter."""
    return None


# ------------------------------------------------------------------ generation


def _parse_progress(line):
    """`(done, total)` out of one line of h3 stdout, or `(None, None)` for a
    line that does not carry a step count — reported as an INDETERMINATE tick
    rather than dropped, so the job row still moves on lines h3 prints between
    steps (a "loading weights" banner, say) instead of going quiet."""
    match = _STEP_RE.search(line)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _drain_stderr(proc, sink):
    """Read h3's stderr to completion on its own thread.

    Not optional: `generate` reads stdout line by line while the child may be
    writing stderr concurrently, and an unread pipe fills its OS buffer and
    blocks the writer — which would wedge the render on binaries chatty enough
    to fill it, silently, from the reporting loop's point of view. This is the
    real failure `subprocess.PIPE` on both streams invites, and the reason a
    canned `CompletedProcess` in a test could never have caught it.
    """
    try:
        for line in proc.stderr:
            sink.append(line)
    finally:
        proc.stderr.close()


def generate(body):
    """Render one video. Returns `{path, seconds, seed, width, height, frames,
    steps}` — the image runners' shape, minus `guidance` (H3 is CFG-distilled;
    there is no such parameter) and with `frames` added for the one axis a
    video has that an image does not.
    """
    snapshot = _loaded.get("snapshot")
    if not snapshot:
        raise RuntimeError("no model is loaded")
    h3_bin = os.environ.get("FUSED_RENDER_H3_BIN")
    if not h3_bin:
        raise RuntimeError("the h3 binary path was not provided to this worker")

    prompt = str(body.get("prompt") or "")
    width = int(body.get("width") or 768)
    height = int(body.get("height") or 768)
    frames = int(body.get("frames") or 90)
    steps = int(body.get("steps") or 20)
    seed = int(body.get("seed") or 0)
    out = str(body.get("out") or "")
    job = body.get("job") or None
    if not out:
        raise ValueError("'out' must be the path to write the video to")

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    import imageio_ffmpeg

    # `dict(os.environ)`, not a mutation of the live mapping: this worker is
    # single-request-at-a-time (`worker_base.GENERATE_LOCK`), but the child's
    # environment should be a snapshot of this process's, not a shared object
    # a second render could still be editing.
    env = dict(os.environ)
    env["H3_FFMPEG"] = imageio_ffmpeg.get_ffmpeg_exe()

    started = time.time()
    worker_base.report(job=job, state="running", kind="task", unit="",
                       done=0, total=steps, detail="Rendering — step 0/%d" % steps)

    args = [
        h3_bin, "-d", snapshot, "-p", prompt,
        "--width", str(width), "--height", str(height),
        "--frames", str(frames), "--steps", str(steps),
        "--seed", str(seed), "-o", out,
    ]
    # Repo subprocess convention (PROJ atfork history): an ABSOLUTE executable
    # path (h3_bin is one — see `registry.h3_bin`'s resolution ladder),
    # `close_fds=False` (posix_spawn instead of fork()+exec), and no `cwd=`.
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, close_fds=False, env=env,
    )
    stderr_lines = []
    stderr_thread = threading.Thread(
        target=_drain_stderr, args=(proc, stderr_lines), daemon=True)
    stderr_thread.start()

    try:
        for line in proc.stdout:
            done, total = _parse_progress(line)
            detail = line.strip() or "Rendering…"
            try:
                worker_base.report_or_cancel(
                    job=job, kind="task", unit="", done=done, total=total,
                    detail=detail)
            except worker_base.Cancelled:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise
    finally:
        proc.stdout.close()
        returncode = proc.wait()
        stderr_thread.join(timeout=10)

    if returncode != 0:
        raise RuntimeError(
            "h3 exited with code %d: %s" % (
                returncode, "".join(stderr_lines).strip() or "no output"))

    return {
        "path": out,
        "seconds": round(time.time() - started, 2),
        "seed": seed,
        "width": width,
        "height": height,
        "frames": frames,
        "steps": steps,
    }


if __name__ == "__main__":
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory)
