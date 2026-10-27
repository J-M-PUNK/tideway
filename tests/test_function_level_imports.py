"""Every function-level import of our own code names something that exists.

A lot of server.py and app/ imports first-party modules inside the
function that needs them, to keep startup fast or to dodge an import
cycle. Python only resolves those when the function runs, so a rename
or removal on the other side goes unnoticed until someone calls it.
That's how /api/upnp/devices sat broken from May to October: the UPnP
rewrite replaced `app.audio.upnp.get_manager()` with a module-level
`upnp_manager`, the route's lazy `from app.audio.upnp import
get_manager` kept parsing, and every request 500'd.

This resolves them all statically. A module that can't be imported on
this platform at all (macOS-only bridges, optional extras) is skipped;
a module that imports fine but lacks the name is the bug.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIRST_PARTY = ("app", "server")


def _function_level_imports():
    files = [ROOT / "server.py", *sorted((ROOT / "app").rglob("*.py"))]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.level == 0
                    and node.module
                    and node.module.split(".")[0] in FIRST_PARTY
                ):
                    rel = path.relative_to(ROOT).as_posix()
                    for alias in node.names:
                        yield f"{rel}:{node.lineno}", node.module, alias.name


def _resolves(module_name: str, name: str) -> bool:
    module = importlib.import_module(module_name)
    if hasattr(module, name):
        return True
    # `from app.audio import upnp` names a submodule, which isn't an
    # attribute of the package until something imports it.
    try:
        importlib.import_module(f"{module_name}.{name}")
    except ImportError:
        return False
    return True


def test_function_level_imports_name_things_that_exist():
    broken = []
    checked = 0
    for where, module_name, name in _function_level_imports():
        if name == "*":
            continue
        try:
            ok = _resolves(module_name, name)
        except ImportError:
            # The module itself isn't importable here (a platform-only
            # bridge or a missing optional extra); there's nothing to
            # check against on this machine.
            continue
        checked += 1
        if not ok:
            broken.append(f"{where}: from {module_name} import {name}")
    assert checked > 0
    assert not broken, "imports that would fail when the function runs:\n" + "\n".join(
        broken
    )


def test_the_dead_upnp_route_is_gone():
    import server

    routes = {getattr(r, "path", None) for r in server.app.routes}
    assert "/api/upnp/devices" not in routes
    # The DLNA picker's real endpoint is untouched.
    assert "/api/dlna/devices" in routes
