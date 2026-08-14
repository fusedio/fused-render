"""Runs one pypandoc conversion inside the private export venv (pypandoc-binary),
so the compile path never imports pandoc. Spawned by engine.py's `export` action
with that venv's python:

  python export_worker.py '<args_json>'

args_json = {"src": ..., "to": ..., "format": ..., "out": ..., "extra": [...]}
"""
import json
import sys

import pypandoc


def run(a):
    pypandoc.convert_file(a["src"], a["to"], format=a["format"],
                          outputfile=a["out"], extra_args=a["extra"])


if __name__ == "__main__":
    run(json.loads(sys.argv[1]))
