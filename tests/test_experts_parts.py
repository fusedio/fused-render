"""Rule 1 of docs/experts.md, enforced statically: the expert has three parts (Notice, Learn, Answer)
that meet only at files. No module of one part imports a module of another part, at module level or inside a
function, by `import` or by name through importlib."""
from __future__ import annotations

import ast
import os

import pytest

import fused_render.experts as experts

PARTS = {
    "Notice": ("capture", "observations"),
    "Learn": ("consolidate", "tidy", "gaps", "distill"),
    "Answer": ("consult", "chat"),
}
PART_OF = {mod: part for part, mods in PARTS.items() for mod in mods}
PKG = "fused_render.experts"
DIR = os.path.dirname(experts.__file__)


def _imports(tree) -> set:
    """Every `fused_render.experts` module the code names: imports anywhere in the file, and dotted module names in
    string literals (importlib)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name[len(PKG) + 1:].split(".")[0] for a in node.names if a.name.startswith(PKG + ".")}
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:  # relative: `from . import x`, `from .x import y`, `from ..experts import x`
                base = PKG.split(".")[:len(PKG.split(".")) - node.level + 1]
                full = ".".join(base + ([mod] if mod else []))
                if full == PKG:
                    out |= {a.name for a in node.names}
                elif full.startswith(PKG + "."):
                    out.add(full[len(PKG) + 1:].split(".")[0])
            elif mod == PKG:
                out |= {a.name for a in node.names}
            elif mod.startswith(PKG + "."):
                out.add(mod[len(PKG) + 1:].split(".")[0])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(PKG + "."):
            out.add(node.value[len(PKG) + 1:].split(".")[0])
    return out


def test_every_part_module_exists_and_the_removed_ones_are_gone():
    for mod in PART_OF:
        assert os.path.isfile(os.path.join(DIR, mod + ".py")), mod
    for gone in ("proposals", "cards", "gate"):
        assert not os.path.exists(os.path.join(DIR, gone + ".py")), gone


@pytest.mark.parametrize("mod", sorted(PART_OF))
def test_no_part_imports_another_part(mod):
    with open(os.path.join(DIR, mod + ".py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    foreign = sorted(m for m in _imports(tree) if m in PART_OF and PART_OF[m] != PART_OF[mod])
    assert not foreign, f"{mod}.py ({PART_OF[mod]}) imports {foreign} of another part"


def test_the_checker_sees_late_relative_and_named_imports():
    src = ("def f():\n    from fused_render.experts import chat, store\n    from . import tidy\n"
           "    import fused_render.experts.gaps as g\n    from fused_render.experts.capture import x\n"
           "    importlib.import_module('fused_render.experts.consult')\n")
    want = {"chat", "store", "tidy", "gaps", "capture", "consult"}
    assert _imports(ast.parse(src)) == want
    # A parent-relative import names the same modules: resolved against the package, never recorded as "experts".
    src += "    from ..experts import chat\n    from ..experts.capture import y\n    from .. import other\n"
    assert _imports(ast.parse(src)) == want
    assert _imports(ast.parse("from ..experts import distill\n")) == {"distill"}
