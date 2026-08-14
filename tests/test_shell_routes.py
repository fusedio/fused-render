"""Every client-side route the shell recognises must be served by the server.

The shell routes in the browser, so in-app navigation never asks the server
anything — it is a pushState. A bookmark, a refresh, or a link from outside the
app is a REAL GET, and if `routers/shell.py` has no entry for that path it 404s.

That asymmetry is what makes the failure so easy to ship: the page works
perfectly for the person who built it (they always arrive by clicking) and is
broken for everyone who reloads. `/scheduled` shipped exactly that way and was
found by hand.

So the list is DERIVED rather than restated. The shell's own route table is the
set of `pathname === "..."` comparisons in App.tsx, and each one is requested
here for real. A page added to the shell without a server entry fails this test
instead of waiting for someone to press ⌘R.
"""
import os
import re

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app

_APP_TSX = os.path.join("frontend", "src", "shell", "App.tsx")


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(start_dir=os.getcwd()))


def shell_routes() -> list[str]:
    """Every literal path App.tsx routes on, in source order.

    Both spellings are picked up: the `pathname === "/x"` the sentinels use and
    the `location.pathname === "/x"` the redirect branches use. A path built from
    a variable is not a fixed route and is out of scope by construction."""
    with open(_APP_TSX, encoding="utf-8") as f:
        source = f.read()
    found = re.findall(r'(?:location\.)?pathname === "([^"]+)"', source)
    # Order-preserving dedupe: the same sentinel can be compared twice.
    seen, routes = set(), []
    for path in found:
        if path not in seen:
            seen.add(path)
            routes.append(path)
    return routes


def test_the_shell_actually_declares_routes():
    """A guard on the guard: if the `pathname ===` idiom is ever replaced, this
    file would silently assert nothing at all."""
    routes = shell_routes()
    assert len(routes) >= 8, routes
    assert "/scheduled" in routes, "the page this test was written for"
    assert "/mounts" in routes


@pytest.mark.parametrize("path", shell_routes())
def test_every_shell_route_survives_a_refresh(client, path):
    res = client.get(path)
    assert res.status_code == 200, (
        f"GET {path} is {res.status_code}: the shell routes to it, but "
        f"routers/shell.py does not serve it, so a refresh or a bookmark 404s")
    assert res.headers["content-type"].startswith("text/html")
