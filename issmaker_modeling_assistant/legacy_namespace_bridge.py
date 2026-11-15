"""Compatibility for an immutable embedded legacy component's absolute imports.

The installed add-on can be imported either as:
    issmaker_modeling_assistant
or
    bl_ext.user_default.issmaker_modeling_assistant

The nested UV Wall 2.0.0 bundle has its own byte-verified source integrity
manifest and still imports the classic absolute name. Altering those files
would be invalid. Instead map only imports rooted at that single known
classic name onto the SAME objects in the active Blender Extension package.

No Blender scene, addon preferences, sys.path, or filesystem is modified.
Each alias returns the exact existing fully-qualified module object; the
legacy package is not imported a second time, avoiding duplicate RNA classes.
The meta-path hook is removed when the add-on unregisters.
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import pathlib
import sys

_LEGACY = "issmaker_modeling_assistant"
_REAL = __package__ or _LEGACY
_ROOT = pathlib.Path(__file__).resolve().parent
_MARKER = "_issmaker_legacy_extension_alias_v1"
_HOOK = None


class _LegacyAliasLoader(importlib.abc.Loader):
    def create_module(self, spec):
        resolved = _REAL + spec.name[len(_LEGACY):]
        # Never execute a second, canonical copy of an extension module.
        return importlib.import_module(resolved)

    def exec_module(self, module):
        # Importlib has already loaded the same module under its active ID.
        return None


class _LegacyAliasFinder(importlib.abc.MetaPathFinder):
    def __init__(self):
        setattr(self, _MARKER, _REAL)
        self.loader = _LegacyAliasLoader()

    def find_spec(self, fullname, path=None, target=None):
        prefix = _LEGACY + "."
        if not fullname.startswith(prefix):
            return None
        remainder = fullname[len(prefix):]
        segments = remainder.split(".")
        if any(not x or not x.isidentifier() for x in segments):
            return None
        path = _ROOT.joinpath(*segments)
        is_package = (path / "__init__.py").is_file()
        is_module = path.with_suffix(".py").is_file()
        if not is_package and not is_module:
            return None
        return importlib.util.spec_from_loader(
            fullname, self.loader, is_package=is_package
        )


def install():
    """Activate only for an actual Blender Extensions module namespace."""
    global _HOOK
    if _REAL == _LEGACY:
        return False
    if not _REAL.endswith("." + _LEGACY):
        raise RuntimeError("Unrecognized ISSMAKER extension package")
    installed_root = sys.modules.get(_REAL)
    if installed_root is None:
        raise RuntimeError("Real extension module not initialized")
    previous = sys.modules.get(_LEGACY)
    if previous is not None and previous is not installed_root:
        raise RuntimeError(
            "ISSMAKER module collision: classic and extension loaded simultaneously"
        )
    sys.modules[_LEGACY] = installed_root

    for finder in sys.meta_path:
        if getattr(finder, _MARKER, None) == _REAL:
            _HOOK = finder
            return True
    _HOOK = _LegacyAliasFinder()
    sys.meta_path.insert(0, _HOOK)
    return True


def uninstall():
    """Remove ONLY aliases belonging to this active extension instance."""
    global _HOOK
    if _REAL == _LEGACY:
        return
    for finder in tuple(sys.meta_path):
        if getattr(finder, _MARKER, None) == _REAL:
            sys.meta_path.remove(finder)
    _HOOK = None
    for name,module in tuple(sys.modules.items()):
        if name == _LEGACY or name.startswith(_LEGACY + "."):
            true_name = _REAL + name[len(_LEGACY):]
            if module is sys.modules.get(true_name):
                del sys.modules[name]


def snapshot():
    return {
        "active_package": _REAL,
        "alias_root_installed": sys.modules.get(_LEGACY) is sys.modules.get(_REAL),
        "finder_count": sum(
            getattr(f,_MARKER,None)==_REAL for f in sys.meta_path
        ),
        "active_alias_count": sum(
            name == _LEGACY or name.startswith(_LEGACY + ".")
            for name in sys.modules
        ) if _REAL != _LEGACY else 0,
    }
