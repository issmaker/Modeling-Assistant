"""Preflight and safe dispatch for all UV Wall production build entry points.

Never silently change Blender mode, selection or the authored mesh. Poll errors must
be diagnosed before invoking Blender's operator so user drafts remain untouched.
"""

import bpy
import traceback
from contextlib import nullcontext


def object_mode_error(context):
    mode = getattr(context, "mode", None)
    if mode != "OBJECT":
        return ("UV Wall: построение доступно в Object Mode. "
                f"Сейчас: {mode or 'неизвестный режим'}. "
                "Нажмите Tab для выхода из Edit Mode и повторите действие. "
                "Несохранённые параметры аддона не применены.")
    return ""


def build_readiness(context):
    """Pure preflight: (ready, human-readable reason), no mutations."""
    reason = object_mode_error(context)
    if reason:
        return False, reason
    scene = getattr(context, "scene", None)
    settings = getattr(scene, "uv_wall_settings", None)
    if settings is None:
        return False, "UV Wall: настройки сцены не зарегистрированы. Перезапустите Blender и проверьте установку аддона."
    path = getattr(settings, "path_mesh", None)
    if path is None or getattr(path, "type", None) != "MESH":
        return False, "UV Wall: не назначен корректный Mesh-путь (Path). Выберите путь в настройках UV Wall."
    wm = getattr(context, "window_manager", None)
    if bool(getattr(wm, "uvwall_viewport_commit_active", False)):
        # The payload is applied inside the production operator after undo snapshot.
        return True, ""
    mode = getattr(settings, "build_mode", None)
    if mode not in {"WALL", "ROOF_ONLY", "CORNICE"}:
        return False, "UV Wall: режим построения не выбран. Выберите Стена, Поверхность или Карниз."
    if mode == "CORNICE" and getattr(settings, "cornice_profile", None) is None:
        return False, "UV Wall: для карниза сначала назначьте профиль."
    try:
        from ..core.definitions import definition_for
        definition = definition_for(path)
    except Exception:
        definition = None
    virtual = bool(definition is not None and getattr(definition, "initialized", False)
                   and getattr(definition, "use_virtual_reference", False))
    if getattr(settings, "source_plane", None) is None and not virtual:
        return False, "UV Wall: назначьте UV-эталон или включите виртуальный модуль."
    return True, ""


def _report_context_rejection(reason):
    # Poll is queried continuously by Blender UI. Log only real user commits,
    # never inside build_readiness() or Operator.poll().
    try:
        from ..production.validation import log_event
        log_event("BUILD_CONTEXT_PRECHECK_FAILED; " + reason, "ERROR")
    except Exception:
        pass


def area_belongs_to_window(window, area):
    """Test AREA ownership without Blender RNA collection ``__contains__``.

    ``screen.areas`` is a ``bpy_prop_collection``. Unlike Python lists its
    ``in`` operator accepts *names*, not Area datablocks, and raises TypeError
    for an Area object. Compare stable RNA pointers instead. Never mutate the
    Blender screen or switch the active area just to perform this check.
    """
    if window is None or area is None:
        return False
    try:
        screen = window.screen
        candidates = screen.areas
        pointer = area.as_pointer()
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False
    if not isinstance(pointer, int) or pointer <= 0:
        return False
    try:
        for candidate in candidates:
            try:
                if candidate.as_pointer() == pointer:
                    return True
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                continue
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False
    return False


def _compatible_with_current_context(context):
    """Prevent a stale popup/window context from rebuilding another scene.

    Blender background mode legitimately has no active window/area, so only
    compare identifiers when both the requested and current values exist.
    """
    current = getattr(bpy, "context", None)
    if current is None:
        return True
    try:
        for field in ("window", "scene"):
            expected = getattr(context, field, None)
            live = getattr(current, field, None)
            if expected is not None and live is not None:
                # Reference equality is safe for mocked and real RNA wrappers.
                if expected != live:
                    return False
    except (ReferenceError, AttributeError, RuntimeError, TypeError):
        return False
    return True


def execute_build(context):
    """Invoke Blender's UNDO operator, but never allow a raw poll exception.

    Raise ValueError containing a useful explanation for the editor to report;
    the calling transaction owns rollback of its Definition and Preview.
    """
    # Build execution may outlive ZIP replacement inside a running Blender.
    # Verify the function objects, not merely __init__.py/bl_info on disk.
    if globals().get('__package__') == 'UV_Wall_Professional.operators':
        from ..core.runtime_identity import verify_critical_geometry_sources
        stale = verify_critical_geometry_sources()
    else:
        stale = []
    if stale:
        reason = ("UV Wall: смешанные исходники и исполняемый код. "
                  "Полностью перезапустите Blender после замены аддона. "
                  + "; ".join(stale))
        _report_context_rejection(reason)
        raise ValueError(reason)
    ready, reason = build_readiness(context)
    if not ready:
        _report_context_rejection(reason)
        raise ValueError(reason)
    operator = bpy.ops.object.build_uv_wall_along_path
    # A Viewport modal can pass a valid context while bpy.ops sees the context
    # of a temporary popup or another area. Bind the matching Blender window
    # and area; never override mode, active object, scene or current selection.
    win = getattr(context, "window", None)
    area = getattr(context, "area", None)
    valid_area = area_belongs_to_window(win, area)
    if not valid_area and not _compatible_with_current_context(context):
        reason = ("UV Wall: изменилось активное окно или сцена Blender. "
                  "Построение не запущено; повторите команду в нужном Viewport.")
        _report_context_rejection(reason)
        raise ValueError(reason)
    executing_build = False
    try:
        # Only invoke temp_override for a still-live RNA Area owned by window.
        # A headless/background build may correctly have no matching Area.
        override = (bpy.context.temp_override(window=win, area=area)
                    if valid_area else nullcontext())
        with override:
            if not operator.poll():
                reason = ("UV Wall: Blender отклонил контекст Build. "
                          "Проверьте Object Mode, выбранный Path, режим построения и UV-эталон. "
                          "Изменения не применены.")
                _report_context_rejection(reason)
                raise ValueError(reason)
            executing_build = True
            return operator("EXEC_DEFAULT")
    except (ReferenceError, RuntimeError, TypeError) as exc:
        full_tb = traceback.format_exc()
        # Blender can surface an operator report(ERROR)+CANCELLED as a
        # RuntimeError even though the production transaction already handled
        # it as a normal fail-closed validation. Do not misclassify that user
        # geometry problem as an internal crash or duplicate its traceback.
        try:
            from ..core.support_logging import build_runtime_state
            state = build_runtime_state()
        except Exception:
            state = {}
        if executing_build and state.get("status") == "CANCELLED":
            try:
                from ..production.validation import log_event
                log_event(
                    f"BUILD_VALIDATION_BLOCKED; stage={state.get('stage','BUILD')}; "
                    f"reason={str(exc).replace(chr(10),' ')}",
                    "CANCELLED",
                )
            except Exception:
                pass
            return {"CANCELLED"}
        try:
            from ..core.support_logging import append_traceback, record_failure, build_runtime_state
            state = build_runtime_state()
            append_traceback(
                "BUILD_DISPATCH_TRACEBACK", full_tb,
                stage=state.get("stage", "BUILD_DISPATCH"),
                token=state.get("transaction_token", ""),
            )
            record_failure(
                stage=state.get("stage", "BUILD_DISPATCH"), exception=exc,
                traceback_text=full_tb, context=context, category="BUILD_DISPATCH",
            )
        except Exception:
            pass
        if executing_build:
            reason = ("UV Wall: ошибка генерации/оператора Build "
                      f"({type(exc).__name__}): {exc}. "
                      "Commit не подтверждён; полный traceback сохранён в логах.")
            try:
                from ..production.validation import log_event
                log_event("BUILD_OPERATOR_EXECUTION_FAILED; " + reason, "ERROR")
            except Exception:
                pass
        else:
            reason = ("UV Wall: Blender сообщил об ошибке контекста "
                      f"({type(exc).__name__}): {exc}. "
                      "Commit не подтверждён; полный traceback сохранён в логах.")
            _report_context_rejection(reason)
        raise ValueError(reason) from exc
