"""Source-loader regressions; the optional compute transport is replaced only.

Exercise run_python's real file read, build_code, compilation and main binding.
No optional fused SDK, venv installation or network is needed.
"""
import asyncio
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
import types
import unittest
from unittest.mock import patch

from fused_render import engine, projectenv


class SourceEncodingTests(unittest.TestCase):
    def check_source(self, encoding, newline='\n'):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            target = directory / 'target.py'
            text = (
                'from __future__ import annotations\n'
                'def main(n: int = 1):\n'
                '    return {"value": n, "text": "Medición", "marker": "MARKER"}\n'
            ).replace('MARKER', chr(0xfeff)).replace('\n', newline)
            target.write_bytes(text.encode(encoding))
            # CPython's file loader is the control: the identical file works
            # with and without a BOM, including its non-ASCII string literal.
            spec = importlib.util.spec_from_file_location('encoding_control', target)
            native = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(native)
            expected = native.main(n=7)

            async def execute(code, requirements, interpreter, files):
                """Only the external transport is replaced; execute its wrapper."""
                for name, data in files.items():
                    (directory / name).write_bytes(data)
                previous = os.getcwd()
                captured = io.StringIO()
                try:
                    os.chdir(directory)
                    namespace = {}
                    with contextlib.redirect_stdout(captured):
                        exec(compile(code, '<encoding-test-runner>', 'exec'), namespace)
                    return types.SimpleNamespace(
                        error=None, return_value=json.dumps(namespace['result']),
                        stdout=captured.getvalue(), stderr='', duration_ms=0, response=None,
                    )
                except Exception:
                    return types.SimpleNamespace(
                        error=traceback.format_exc(), return_value=None,
                        stdout=captured.getvalue(), stderr='', duration_ms=0, response=None,
                    )
                finally:
                    os.chdir(previous)

            with (
                patch.object(engine, 'ensure_warm'),
                patch.object(projectenv, 'project_env_for', return_value=None),
                patch.object(engine, 'app_interpreter', return_value=sys.executable),
                patch.object(engine, '_execute', side_effect=execute),
                patch.dict(sys.modules, {'fused': types.ModuleType('fused')}),
                patch.object(sys, 'path', sys.path.copy()),
                patch.dict(os.environ),
            ):
                result = asyncio.run(engine.run_python(str(target), {'n': '7'}))
            self.assertTrue(result['ok'], result.get('error'))
            self.assertEqual(result['result'], expected)
            self.assertEqual(result['result']['marker'], '\ufeff')

    def test_utf8(self):
        self.check_source('utf-8')

    def test_utf8_bom(self):
        self.check_source('utf-8-sig')

    def test_utf8_bom_crlf(self):
        self.check_source('utf-8-sig', '\r\n')


if __name__ == '__main__':
    unittest.main()
