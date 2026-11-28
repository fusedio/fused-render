"""Regression tests for atomic create-only filesystem writes."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.responses import JSONResponse

from fused_render.server import fs_mutate


class CreateOnlyWriteTests(unittest.TestCase):
    """Check conflict handling against real files on disk."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sample.txt"
        self.guard = patch.object(fs_mutate, "_require_fused", return_value=None)
        self.writeable = patch.object(fs_mutate, "_writable", return_value=True)
        self.snapshot = patch.object(fs_mutate, "_snapshot_refusal", return_value=None)
        for guard in (self.guard, self.writeable, self.snapshot):
            guard.start()
            self.addCleanup(guard.stop)

    def write(self, content, create=True):
        """Write through the real file mutation helper."""
        return fs_mutate._fs_write({"path": str(self.path), "content": content, "create": create}, "1")

    def test_create_race_preserves_competing_file(self):
        """A competing creation after the preliminary check must win."""
        real_fsync = os.fsync
        injected = False

        def race(fd):
            nonlocal injected
            real_fsync(fd)
            if not injected:
                injected = True
                self.path.write_text("other writer", encoding="utf-8")

        with patch.object(fs_mutate.os, "fsync", side_effect=race):
            result = self.write("first writer")
        self.assertIsInstance(result, JSONResponse)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(json.loads(result.body), {"error": "conflict"})
        self.assertEqual(self.path.read_text(encoding="utf-8"), "other writer")
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["sample.txt"])

    def test_create_new_file(self):
        """Create-only writes succeed when the destination is absent."""
        result = self.write("Medición")
        self.assertIsInstance(result, dict)
        self.assertTrue(result["created"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "Medición")

    def test_existing_file_conflict(self):
        """Create-only writes never replace an existing file."""
        self.path.write_text("existing", encoding="utf-8")
        result = self.write("replacement")
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "existing")

    def test_normal_overwrite_unchanged(self):
        """Ordinary writes retain the existing replacement semantics."""
        self.path.write_text("old", encoding="utf-8")
        result = self.write("new", create=False)
        self.assertIsInstance(result, dict)
        self.assertFalse(result["created"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "new")

    def test_failed_link_cleans_temporary_file(self):
        """Unsupported hard links fail closed without leaving a temp file."""
        with patch.object(fs_mutate.os, "link", side_effect=OSError("unsupported")):
            result = self.write("content")
        self.assertEqual(result.status_code, 400)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_cleanup_failure_does_not_report_failed_save(self):
        """Once published, failed alias cleanup must not report data loss."""
        real_unlink = os.unlink
        injected = False

        def fail_once(path, *args, **kwargs):
            nonlocal injected
            if not injected and os.path.basename(str(path)) != self.path.name:
                injected = True
                raise PermissionError("alias cleanup denied")
            return real_unlink(path, *args, **kwargs)

        with patch.object(fs_mutate.os, "unlink", side_effect=fail_once):
            result = self.write("saved")
        self.assertIsInstance(result, dict)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "saved")


if __name__ == "__main__":
    unittest.main()
