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
