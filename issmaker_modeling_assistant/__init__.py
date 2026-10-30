bl_info = {
    "name": "ISSMAKER Unified Modeling Suite",
    "author": "issmaker",
    "version": (2, 0, 26),
    "blender": (4, 5, 0),
    "location": "3D Viewport / UV Editor / Shader Editor",
    "description": "W58.28 UNIVERSAL WINDOW LAB TEST ONLY: LP-normalized tangent corner solver, curved/mixed geometric QA; NOT production",
    "category": "3D View",
}

import bpy

from . import log, runtime, view_tools, modeling_tools, uv_tools, shader_tools, workspace_tools, uv_feedback, weld_preview, module_visualization, ui, diagnostics, hud_evidence_lab, unified_issue_navigator, deep_autolab
from .tools.brick_hole import integration as brick_hole_integration
from .tools.collision_inspector import integration as collision_integration
from .tools.uv_wall import integration as uv_wall_integration
from .tools.dwos_windows import integration as dwos_integration
from .core.registry import validate_registry
from .core import undo_audit


MODULES = (
    view_tools, weld_preview, modeling_tools, uv_tools, shader_tools,
    workspace_tools, uv_feedback, module_visualization, brick_hole_integration, collision_integration, uv_wall_integration, dwos_integration, ui, diagnostics, hud_evidence_lab, unified_issue_navigator, deep_autolab,
)


def _validate_registration_plan():
    """Fail before touching Blender if a module or operator is listed twice."""
    seen_modules = set()
    seen_classes = {}
    for module in MODULES:
        if module in seen_modules:
            raise RuntimeError(f"ISSMAKER: duplicate registration module {module.__name__}")
        seen_modules.add(module)
        for cls in getattr(module, 'CLASSES', ()):
            key = cls.__name__
            previous = seen_classes.get(key)
            if previous is not None:
                raise RuntimeError(
                    f"ISSMAKER: duplicate Blender class {key}: "
                    f"{previous} and {module.__name__}")
            seen_classes[key] = module.__name__



def register():
    _validate_registration_plan()
    validate_registry()
    log.initialize()
    registered = []
    try:
        for module in MODULES:
            try:
                module.register()
            except Exception:
                try:
                    module.unregister()
                except Exception as cleanup_exc:
                    log.write_exception(
                        f"Failed module cleanup: {module.__name__}", cleanup_exc)
                raise
            registered.append(module)
        undo_audit.register()
        ui.initialize_appearance()
        from . import hud_gpu_runtime
        hud_gpu_runtime.install_hooks()
    except Exception as exc:
        undo_audit.unregister()
        log.write_exception("Registration failed; rolling back", exc)
        for module in reversed(registered):
            try:
                module.unregister()
            except Exception as rollback_exc:
                log.write_exception("Registration rollback failed", rollback_exc)
        runtime.reset()
        try:
            from . import hud_gpu_runtime
            hud_gpu_runtime.remove_hooks()
            hud_gpu_runtime.release()
        except Exception:pass
        raise
    log.write(f"Add-on {'.'.join(map(str, bl_info['version']))} registered | Blender {bpy.app.version_string}")


def unregister():
    """Best-effort shutdown: one failing subsystem must not strand the rest."""
    def phase(label, callback):
        try:
            callback()
        except Exception as exc:
            try:
                log.write_exception('Shutdown phase failed: ' + label, exc)
            except Exception:
                pass

    def close_modal_owners():
        from .core import active_tools
        ok, reason = active_tools.switch_to('UNREGISTER', bpy.context)
        if not ok:
            raise RuntimeError(reason)
    phase('exclusive tool cleanup', close_modal_owners)
    phase('restore state', view_tools.restore_all_runtime_state)
    phase('appearance flush', ui.flush_appearance)
    from . import styled_panels
    phase('styled panels', lambda: styled_panels.shutdown(bpy.context))
    phase('undo audit', undo_audit.unregister)
    phase('runtime reset', runtime.reset)
    for module in reversed(MODULES):
        phase('unregister ' + module.__name__, module.unregister)

    def gpu_cleanup():
        from . import hud_gpu_runtime
        phase('HUD GPU snapshot', lambda: log.write('HUD GPU cache shutdown: '+str(hud_gpu_runtime.snapshot())))
        phase('HUD GPU remove hooks', hud_gpu_runtime.remove_hooks)
        phase('HUD GPU release', hud_gpu_runtime.release)
    phase('GPU cleanup', gpu_cleanup)

    def component_cleanup():
        from .core import workspace_components
        phase('HUD components diagnostics', lambda: log.write('HUD components shutdown: '+str(workspace_components.diagnostics())))
        phase('HUD components release', workspace_components.release)
    phase('components cleanup', component_cleanup)
    phase('shutdown log', lambda: log.write('Add-on unregistered'))
