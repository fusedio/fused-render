"""Every backend host must guard `os.setsid()` — it does not exist on Windows,
and an unguarded call is an AttributeError (not the OSError the host catches)
before the host reads its request."""
import ast
import glob
import os

import pytest

HOSTS = sorted(glob.glob(os.path.join("fused_render", "claude_agent", "*_host.py")))


def _is_hasattr_setsid(test):
    return (isinstance(test, ast.Call) and isinstance(test.func, ast.Name)
            and test.func.id == "hasattr" and len(test.args) == 2
            and isinstance(test.args[0], ast.Name) and test.args[0].id == "os"
            and isinstance(test.args[1], ast.Constant)
            and test.args[1].value == "setsid")


def _unguarded_setsid_calls(tree):
    bad = []

    def visit(node, guarded):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setsid" and not guarded):
            bad.append(node.lineno)
        for field, value in ast.iter_fields(node):
            kids = value if isinstance(value, list) else [value]
            for kid in kids:
                if isinstance(kid, ast.AST):
                    g = guarded or (isinstance(node, ast.If) and field == "body"
                                    and _is_hasattr_setsid(node.test))
                    visit(kid, g)

    visit(tree, False)
    return bad


def test_hosts_discovered():
    assert HOSTS


@pytest.mark.parametrize("path", HOSTS, ids=os.path.basename)
def test_setsid_is_guarded(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    assert _unguarded_setsid_calls(tree) == [], (
        "os.setsid() must sit inside `if hasattr(os, \"setsid\"):` (Windows)")
