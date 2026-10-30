from __future__ import annotations

import bpy
from . import (
    properties,
    opening_detection,
    window_builder,
    split_tool,
    zone_tool,
    weld,
    ui,
    viewport,
    composer,
    mesh_diagnostics,
    mesh_consistency_blender,
    batch_edit,
    diagnostic_bundle,
    finalize_clean,
    outer_contact,
    opening_preview,
    normal_repair,
    window_assembly,
    contact_refresh,
    geometry_inspector,
    mullion_tool,
    scene_spatial_safety,
    smart_interior_scene,
    smart_interior_3d_modes,
    smart_interior_conflict_ui,
    smart_interior_auto,
    dwos_history,
    reference_frame,
    fixture_gallery,
    interior_fixture_gallery,
    surface_steps,
    surface_corner_union,
    surface_corner_detector,
    surface_mullion_presets,
    surface_seal_ops,
    next_studio,
    surface_zone_ops,
    auto_window,
    w5829_choice_hud,
    window_region_cutter,
    adaptive_edge_cut_ui,
)
from .dwos_log import setup_logging, event as log_event

bl_info = {
    'name': 'DWOS Window Core',
    'author': 'issmaker',
    'version': (3, 7, 56),
    'blender': (4, 5, 0),
    'location': 'View3D > DWOS + Viewport Composer',
    'description': 'DWOS 3.7.44 TEST: explicit LOD repair and local mullion inset',
    'category': 'Mesh',
}

CLASSES = (
    opening_detection.DWOS_OT_find_openings,
    opening_detection.DWOS_OT_use_selected_openings,
    opening_detection.DWOS_OT_use_selected_cap_faces,
    window_builder.DWOS_OT_create_windows,
    window_builder.DWOS_OT_rebuild_window,
    split_tool.DWOS_OT_add_split,
    split_tool.DWOS_OT_add_split_zone,
    split_tool.DWOS_OT_remove_split,
    split_tool.DWOS_OT_delete_selected_mullions,
    split_tool.DWOS_OT_reset_split_thickness,
    split_tool.DWOS_OT_drag_split,
    weld.DWOS_OT_weld_to_wall,
    weld.DWOS_OT_weld_scope,
    weld.DWOS_OT_weld_check,
    weld.DWOS_OT_consolidate_weld,
    mesh_diagnostics.DWOS_OT_analyze_meshes,
    mesh_consistency_blender.DWOS_OT_audit_mesh_consistency,
    diagnostic_bundle.DWOS_OT_export_field_bundle,
    *batch_edit.CLASSES,
    *zone_tool.CLASSES,
    *composer.CLASSES,
    *ui.CLASSES,
    *finalize_clean.CLASSES,
    *outer_contact.CLASSES,
    *opening_preview.CLASSES,
    *normal_repair.CLASSES,
    *window_assembly.CLASSES,
    *contact_refresh.CLASSES,
    *geometry_inspector.CLASSES,
    *mullion_tool.CLASSES,
    *scene_spatial_safety.CLASSES,
    *smart_interior_scene.CLASSES,
    *smart_interior_3d_modes.CLASSES,
    *smart_interior_conflict_ui.CLASSES,
    *reference_frame.CLASSES,
    *fixture_gallery.CLASSES,
    *interior_fixture_gallery.CLASSES,
    *surface_steps.CLASSES,
    *surface_corner_union.CLASSES,
    *surface_corner_detector.CLASSES,
    *surface_mullion_presets.CLASSES,
    *surface_seal_ops.CLASSES,
    *next_studio.CLASSES,
    *surface_zone_ops.CLASSES,
    *auto_window.CLASSES,
    *w5829_choice_hud.CLASSES,
    *window_region_cutter.CLASSES,
    *adaptive_edge_cut_ui.CLASSES,
)


def register():
    setup_logging()
    dwos_history.clear()
    log_event('REGISTER_BEGIN', version='3.7.47-DWOS-NEXT-TEST', module_path=__file__)
    properties.register_props()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    surface_steps.register()
    smart_interior_auto.register()
    viewport.ensure_overlay()
    log_event('REGISTER_OK', version='3.7.47-DWOS-NEXT-TEST')


def unregister():
    log_event('UNREGISTER_BEGIN', version='3.7.47-DWOS-NEXT-TEST')
    dwos_history.clear()
    smart_interior_auto.unregister()
    smart_interior_conflict_ui.stop_overlay()
    composer.remove_active_draw_handler()
    viewport.remove_overlay()
    surface_steps.unregister()
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    properties.unregister_props()
