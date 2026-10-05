"""`_child.py`'s worker appends `templates/shared` onto `sys.path` (after the
user module's own dir), so a user `.py` can `import fused_ai` under the
built-in executor exactly as it can under the fused engine (`engine.py`'s
generated wrapper does the matching append — see `test_engine.py`'s own
tests for that half). Both, or neither: `_child.py`'s module docstring names
that trap.

Run through `executor.run_python` for real — a genuine subprocess via
`_child.py`, not an in-process stand-in — so this proves the seeding as
production actually exercises it.
"""
import os
import textwrap

from fused_render import executor


def test_a_user_py_can_import_fused_ai_under_the_builtin_executor(tmp_path):
    script = tmp_path / "uses_fused_ai.py"
    script.write_text(textwrap.dedent(
        """
        import fused_ai

        def main():
            return {
                "has_text": callable(fused_ai.text),
                "has_ai": callable(fused_ai.ai.text),
                "module_file": fused_ai.__file__,
            }
        """
    ))
    out = executor.run_python(str(script), {})
    assert out["ok"] is True, out.get("error")
    assert out["result"]["has_text"] is True
    assert out["result"]["has_ai"] is True


def test_a_same_named_user_module_still_wins_over_the_shared_copy(tmp_path):
    """The append (not insert-at-0) precedence, proven rather than asserted:
    a user's own `appenv.py` beside the script must shadow the shared one."""
    (tmp_path / "appenv.py").write_text("MARKER = 'user-owned'\n")
    script = tmp_path / "uses_appenv.py"
    script.write_text(textwrap.dedent(
        """
        import appenv

        def main():
            return getattr(appenv, "MARKER", None)
        """
    ))
    out = executor.run_python(str(script), {})
    assert out["ok"] is True, out.get("error")
    assert out["result"] == "user-owned"


def test_a_user_py_sees_its_own_path_as_fused_render_page(tmp_path):
    """D1316: a detached worker the script spawns inherits this and sends
    it as `X-Fused-Page`, so its job rows group by app and click through."""
    script = tmp_path / "reports_page.py"
    script.write_text("import os\n\ndef main():\n    return os.environ.get('FUSED_RENDER_PAGE')\n")
    out = executor.run_python(str(script), {})
    assert out["ok"] is True, out.get("error")
    assert out["result"] == str(script)
