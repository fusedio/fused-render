#!/usr/bin/env python3
"""A REAL executable standing in for `antirez/h3.c`, for `test_ai_h3_worker.py`.

Not a canned `subprocess.CompletedProcess` — a real process with real pipes,
so the worker's stdout-reading/stderr-draining/SIGTERM-handling loop is
exercised against real OS behaviour rather than a mock that cannot lie about
those things convincingly. Every knob below is read from the environment
rather than argv, because `worker.py` builds h3's argv itself and a test has
no other channel into this script.

Env knobs:
  H3_FAKE_EXIT_CODE      exit code after the step loop (default 0)
  H3_FAKE_STEP_SLEEP      seconds to sleep between each printed step (default 0)
  H3_FAKE_STDERR_BYTES    bytes of filler written to stderr before the loop —
                          set well past a pipe's OS buffer (~64KB) to prove the
                          worker drains it concurrently instead of deadlocking
  H3_FAKE_TERM_MARKER     if set, a SIGTERM handler is installed that writes
                          this path before exiting, so a test can confirm the
                          child actually received the signal rather than
                          merely inferring it from timing
  H3_FAKE_NOISE_LINES     how many non-step diagnostic lines to print, as
                          fast as possible, before the step loop — for
                          proving the worker throttles its progress POSTs
                          instead of sending one per line
"""
import argparse
import os
import signal
import sys
import time


def _install_term_handler():
    marker = os.environ.get("H3_FAKE_TERM_MARKER")
    if not marker:
        return

    def _on_term(signum, frame):
        with open(marker, "w") as handle:
            handle.write("terminated\n")
        sys.exit(143)

    signal.signal(signal.SIGTERM, _on_term)


def main():
    _install_term_handler()
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", dest="snapshot")
    parser.add_argument("-p", dest="prompt")
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--frames", type=int, default=90)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("-o", dest="out")
    args = parser.parse_args()

    stderr_bytes = int(os.environ.get("H3_FAKE_STDERR_BYTES", "0"))
    if stderr_bytes:
        chunk = ("e" * 200) + "\n"
        written = 0
        while written < stderr_bytes:
            sys.stderr.write(chunk)
            sys.stderr.flush()
            written += len(chunk)

    noise_lines = int(os.environ.get("H3_FAKE_NOISE_LINES", "0"))
    for _ in range(noise_lines):
        print("loading weights...", flush=True)

    sleep_s = float(os.environ.get("H3_FAKE_STEP_SLEEP", "0"))
    steps = args.steps or 1
    for i in range(1, steps + 1):
        print("step %d/%d" % (i, steps), flush=True)
        if sleep_s:
            time.sleep(sleep_s)

    exit_code = int(os.environ.get("H3_FAKE_EXIT_CODE", "0"))
    if exit_code != 0:
        sys.stderr.write("fake h3 failure: the render broke\n")
        sys.exit(exit_code)

    if args.out:
        with open(args.out, "wb") as handle:
            # Not a real mp4 — just enough bytes, and a distinctive marker a
            # test can look for, to prove THIS script is what produced it.
            handle.write(b"\x00\x00\x00\x18ftypmp42fake-h3-output")

    sys.exit(0)


if __name__ == "__main__":
    main()
