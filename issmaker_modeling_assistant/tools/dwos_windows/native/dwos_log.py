from __future__ import annotations
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import os
import tempfile
import traceback
import bpy

_LOGGER = logging.getLogger("dwos_window_core")
_LOG_PATH: Path | None = None


def _resolve_log_dir() -> Path:
    try:
        base = bpy.utils.user_resource('CONFIG')
        if base:
            path = Path(base) / 'dwos_window_core' / 'logs'
            path.mkdir(parents=True, exist_ok=True)
            return path
    except Exception:
        pass
    path = Path(tempfile.gettempdir()) / 'dwos_window_core' / 'logs'
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging() -> Path:
    global _LOG_PATH
    if _LOG_PATH is not None and _LOGGER.handlers:
        return _LOG_PATH
    _LOG_PATH = _resolve_log_dir() / 'dwos-latest.log'
    _LOGGER.setLevel(logging.DEBUG if os.environ.get('DWOS_DEBUG_LOG') == '1' else logging.INFO)
    _LOGGER.propagate = False
    if not _LOGGER.handlers:
        handler = RotatingFileHandler(_LOG_PATH, maxBytes=1_500_000, backupCount=1, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
        _LOGGER.addHandler(handler)
    try:
        _LOGGER.info('=== DWOS SESSION START ===')
        _LOGGER.info('Blender=%s | Python=%s | PID=%s', bpy.app.version_string, getattr(bpy.app, 'binary_path_python', '') or 'embedded', os.getpid())
    except Exception:
        pass
    return _LOG_PATH


def logger() -> logging.Logger:
    if not _LOGGER.handlers:
        setup_logging()
    return _LOGGER


def log_path() -> str:
    return str(setup_logging())


def log_dir() -> str:
    return str(Path(setup_logging()).parent)


def event(name: str, **fields) -> None:
    parts = [name] + [f'{k}={v}' for k, v in fields.items()]
    # Repeated per-window geometry and stage timings belong in explicit debug mode.
    detail = name in {'WINDOW_REBUILD_TIMING', 'WINDOW_GEOMETRY_BUILT', 'INTERIOR_BUILT',
                      'CREATE_WINDOW_CANDIDATE', 'BRANCH_TREE_COMMIT'}
    (logger().debug if detail else logger().info)(' | '.join(parts))


def warning(name: str, **fields) -> None:
    parts = [name] + [f'{k}={v}' for k, v in fields.items()]
    logger().warning(' | '.join(parts))


def exception(name: str, exc: BaseException | None = None, **fields) -> None:
    parts = [name] + [f'{k}={v}' for k, v in fields.items()]
    if exc is not None:
        parts.append(f'error={exc!r}')
    logger().error(' | '.join(parts))
    logger().error(traceback.format_exc())


def mesh_report_path() -> str:
    return str(Path(setup_logging()).parent / 'dwos-mesh-analysis-latest.txt')


def write_mesh_report(text: str) -> str:
    path = Path(mesh_report_path())
    try:
        path.write_text(str(text), encoding='utf-8')
        logger().info('MESH_ANALYSIS_REPORT | path=%s | chars=%s', path, len(str(text)))
    except Exception as exc:
        logger().error('MESH_ANALYSIS_REPORT_FAILED | path=%s | error=%r', path, exc)
    return str(path)


def block(title: str, text: str) -> None:
    lg = logger()
    # Full mesh report lives in the on-demand report file/support bundle.
    # Repeating hundreds of mesh lines in ordinary runtime logs is not useful.
    if lg.isEnabledFor(logging.DEBUG):
        lg.debug('===== %s =====', title)
        for line in str(text).splitlines():
            lg.debug('MESH | %s', line)
        lg.debug('===== END %s =====', title)
