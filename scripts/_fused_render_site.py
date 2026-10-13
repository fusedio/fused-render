"""Keep the bundle's site-packages ahead of the user site for the app's own python.

build_dmg.sh copies this into Contents/Resources/lib/python3.X/site-packages with
a .pth that calls bundle_before_user_site() while site.py adds that directory.
Venvs never load it: they do not see the bundle's site-packages at all.
"""
import os
import site
import sys


def reorder(path, user_site, bundle_site):
    """`path` with the user-site block (it and what its .pth files added) moved after `bundle_site`."""
    norm = [os.path.abspath(p) if p else p for p in path]
    try:
        start = norm.index(os.path.abspath(user_site))
        end = norm.index(os.path.abspath(bundle_site))
    except (TypeError, ValueError):
        return list(path)
    if start > end:
        return list(path)
    return path[:start] + [path[end]] + path[start:end] + path[end + 1:]


def bundle_before_user_site():
    sys.path[:] = reorder(sys.path, site.USER_SITE, os.path.dirname(os.path.abspath(__file__)))
