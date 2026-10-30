"""DWOS Next: LP-FIRST-first sidebar. Wave 41 migration shell.

Important: this UI never claims a zone action is implemented while the new
Zone Graph is under development. The legacy window editor remains available
for existing windows, with its data and operators untouched.
"""
from __future__ import annotations
import json
import bpy

from . import surface_steps as surface
from .batch_edit import resolve_window

CAPTIONS={
  1:"Исходный материал / Backup",
  2:"LP: создать сетку рам и зон",
  3:"HP: построить глубину",
  4:"Открыть фасад",
  5:"Добавить стекло",
  6:"Создать Interior",
  7:"Добавить импосты",
  8:"Пересчитать ширину"
}
PARAMETERS={
  2:("frame_width",),
  3:("frame_depth","window_offset"),
  6:("interior_depth",),
  7:("mullion_width","mullion_depth"),
  8:("revised_frame_width","revised_mullion_width"),
}

class DWOS_PT_next_studio(bpy.types.Panel):
    bl_idname="DWOS_PT_next_studio"
    bl_label="DWOS NEXT · LP-FIRST"
    bl_space_type="VIEW_3D"
    bl_region_type="UI"
    bl_category="DWOS"
    bl_order=0

    def draw(self,context):
        ui=self.layout
        header=ui.box()
        header.label(text="ISSMAKER · DWOS NEXT",icon="MESH_GRID")
        header.label(text="LP-FIRST / TEST",icon="INFO")
        header.operator("dwos.surface_export",text="Диагностика одним нажатием",icon="PACKAGE")
        obj=context.active_object
        if obj is not None and obj.type=="MESH" and obj.mode=="EDIT":
            auto_box=ui.box()
            auto_box.label(text="DWOS W58 · АВТОМАТИЧЕСКИЕ ОКНА",icon="WINDOW")
            auto_box.label(text="Выдели полигоны · 1/много модулей")
            auto_box.operator("dwos.auto_windows",text="СОЗДАТЬ ВСЕ ОКНА · HP + LP",icon="PLAY")
            auto_box.label(text="Рама + стекло + Interior; HUD автоматически",icon="INFO")
        elif obj is not None and obj.get("DWOS_W58_auto_role")=="HP":
            auto_box=ui.box()
            auto_box.label(text="DWOS W58 · ГОТОВОЕ ОКНО",icon="WINDOW")
            auto_box.operator("dwos.viewport_composer",text="Открыть Window Composer",icon="TOOL_SETTINGS")
            row=auto_box.row(align=True)
            row.operator("dwos.auto_view",text="HP").mode="HP"
            row.operator("dwos.auto_view",text="LP").mode="LP"
        from . import surface_composer_bridge
        composer_host=surface_composer_bridge.resolve_host(context)
        if composer_host is not None:
            header.operator("dwos.viewport_composer",text="DWOS STUDIO · ЕДИНЫЙ HUD",icon="ORIENTATION_VIEW")

        legacy=resolve_window(obj) if obj is not None else None

        if legacy is not None:
            section=ui.box()
            section.label(text="Ранее созданное окно",icon="WINDOW")
            section.label(text="Редактор совместимости")
            try:
                section.label(text=legacy.name[:40])
                props=legacy.dwos_window
                section.label(text="Зона: "+(str(props.active_zone_id) or "не выбрана")[:25])
            except (ReferenceError,AttributeError):
                pass
            section.operator("dwos.viewport_composer",text="Открыть прежний редактор",icon="TOOL_SETTINGS")
            ui.label(text="Миграция зон LP-FIRST ещё не выполнена",icon="INFO")
            return

        host=None
        session=None
        if obj is not None and obj.type=="MESH" and obj.get(surface.SESSION_KEY):
            try:
                host,session=surface._get_session_wall(context,allow_legacy=True)
            except (ValueError,TypeError,KeyError):
                pass
        elif obj is not None and obj.get(surface.HOST_NAME_KEY):
            try:
                host,session=surface._get_session_wall(context,allow_legacy=True)
            except (ValueError,TypeError,KeyError):
                pass
        if session and session.get("schema")!=surface.SCHEMA:
            box=ui.box()
            box.label(text="Старая DWOS-сессия",icon="ERROR")
            box.label(text="Оригинал останется без изменений")
            box.operator("dwos.surface_clone_legacy",text="Новый тест из Backup",icon="DUPLICATE")
            ui.operator("dwos.surface_export",text="Диагностика ZIP",icon="PACKAGE")
            return

        if session is None:
            if obj is not None and obj.type=="MESH" and obj.get(surface.SESSION_KEY):
                try:
                    old=json.loads(str(obj[surface.SESSION_KEY]))
                    if (old.get("backup_kind")=="MESH_DATABLOCK_FAKE_USER_V43"
                            and int(old.get("step",0))==1 and not old.get("objects")
                            and bpy.data.meshes.get(str(old.get("backup_name",""))) is None):
                        warning=ui.box()
                        warning.label(text="STEP 1 ОТМЕНЁН · BACKUP ОТСУТСТВУЕТ",icon="ERROR")
                        warning.label(text="Redo восстановит Backup в текущей сессии.")
                        warning.label(text="Если Redo недоступен — откройте сохранённый .blend.")
                        warning.operator("dwos.surface_export",text="Собрать ZIP",icon="PACKAGE")
                        return
                except (ValueError,TypeError,KeyError):
                    pass
            if obj is None or obj.type!="MESH":
                box=ui.box()
                box.label(text="Выберите фасад Mesh",icon="INFO")
                box.operator("dwos.find_openings",text="Поиск проёмов",icon="VIEWZOOM")
                return
            box=ui.box()
            box.label(text="Новая LP-FIRST стена",icon="MESH_DATA")
            box.label(text="Edit Mode: выберите грани фасада")
            box.prop(context.scene.dwos_surface_lab,"classic_glass_midframe",
                     text="Классическое стекло Plane + Interior BOX [TEST]")
            row=box.row()
            row.enabled=obj.mode=="EDIT"
            row.operator("dwos.surface_step",text="Создать LP-FIRST",icon="PLAY").number=1
            ui.operator("dwos.find_openings",text="Поиск проёмов",icon="VIEWZOOM")
            return

        count=int(session.get("step",0))
        if count>=2:
            missing=[mode for mode in ("HP","LP")
                     if not bpy.data.objects.get(session.get("objects",{}).get(mode,""))]
            if missing:
                warning=ui.box()
                warning.label(text="СЕССИЯ ПОСЛЕ UNDO: НЕТ "+"/".join(missing),icon="ERROR")
                warning.label(text="Остановлено: геометрия и состояние расходятся.")
                warning.label(text="Object Mode → Restore из Backup.")
                warning.operator("dwos.surface_export",text="Собрать диагностику ZIP",icon="PACKAGE")
                if count in (2,3):
                    recover=warning.row()
                    recover.enabled=context.mode=="OBJECT"
                    recover.operator("dwos.surface_recover_missing_lods",
                                     text="Repair LOD · восстановить HP/LP",icon="FILE_REFRESH")
                restore=warning.row()
                restore.enabled=context.mode=="OBJECT"
                restore.operator("dwos.surface_restore",text="Восстановить исходную стену",icon="LOOP_BACK")
                return
        actions=sorted(surface.STEPS)
        next_actions=[n for n in actions if n>count]
        head=ui.box()
        head.label(text="Объект: "+host.name[:35],icon="MESH_DATA")
        head.label(text=f"Текущий этап: {count} · LP-FIRST TEST")
        if next_actions:
            step=next_actions[0]
            panel=ui.box()
            panel.label(text="Следующее действие",icon="PLAY")
            panel.label(text=CAPTIONS.get(step,surface.STEPS[step]))
            props=context.scene.dwos_surface_lab
            for name in PARAMETERS.get(step,()):
                panel.prop(props,name)
            panel.operator("dwos.surface_step",text="Применить: "+CAPTIONS.get(step,"Действие"),icon="CHECKMARK").number=step
        else:
            ui.label(text="Текущие этапы выполнены",icon="CHECKMARK")
            ui.label(text="Дальнейшие операции будут добавляться")

        if 2<=count<7:
            # Zone Graph acts on the upcoming stage 7 geometry. It cannot
            # silently edit already published meshes or previously built HP.
            from . import zone_core,surface_step_core
            zbox=ui.box()
            zbox.label(text="DWOS NEXT · Zone Composer",icon="MOD_ARRAY")
            zprops=context.scene.dwos_surface_lab
            zbox.prop(zprops,"zone_split_thickness")
            try:
                from .surface_graph import _orthogonal_bounds
                ax=(surface_step_core.rectangle_axes(session['source']['points'])
                    or _orthogonal_bounds(session['source']['points']))
                if ax is not None:
                    lo,hi,low,high=ax
                    fw=float(session["parameters"]["frame_width"])
                    tree=zone_core.decode(session.get("zone_tree_pending",""))
                    leaves,bands=zone_core.layout(tree,hi-lo,high-low,fw)
                    zbox.label(text=f"Зон: {len(leaves)} · разделителей: {len(bands)}")
                    for zone_id,rect in leaves[:24]:
                        row=zbox.row(align=True)
                        row.label(text="Зона "+str(zone_id)[:7])
                        op=row.operator("dwos.surface_zone_split",text="V",icon="ADD")
                        op.zone_id=zone_id;op.axis="V"
                        op=row.operator("dwos.surface_zone_split",text="H",icon="ADD")
                        op.zone_id=zone_id;op.axis="H"
                        leaf=zone_core.find(tree,zone_id)
                        current_fill=leaf.get("fill","GLASS")
                        row=zbox.row(align=True)
                        row.label(text="Заполнение: "+current_fill)
                        op=row.operator("dwos.surface_zone_fill",text="GLASS",depress=current_fill=="GLASS")
                        op.zone_id=zone_id;op.fill="GLASS"
                        op=row.operator("dwos.surface_zone_fill",text="PANEL OPT",depress=current_fill=="PANEL")
                        op.zone_id=zone_id;op.fill="PANEL";op.panel_mode="OPT"
                        op=row.operator("dwos.surface_zone_fill",text="EMPTY",depress=current_fill=="EMPTY")
                        op.zone_id=zone_id;op.fill="EMPTY"
                    if bands:
                        sep=zbox.column()
                        sep.label(text="ЛОКАЛЬНЫЙ INSET ИМПОСТОВ",icon="MOD_SOLIDIFY")
                        for split_id,axis,center,thickness,rect,band in bands[:16]:
                            row=sep.row(align=True)
                            row.label(text=f"{axis} {split_id[:6]} · {thickness:.3f} м")
                            op=row.operator("dwos.surface_zone_resize_split",text="−5 мм",icon="REMOVE")
                            op.split_id=split_id;op.delta=-.005
                            op=row.operator("dwos.surface_zone_resize_split",text="+5 мм",icon="ADD")
                            op.split_id=split_id;op.delta=.005
                    if session.get("zone_tree_pending"):
                        zbox.operator("dwos.surface_zone_reset",text="Стандартная раскладка",icon="LOOP_BACK")
                    zbox.label(text="Изменения появятся на этапе 7",icon="INFO")
            except (KeyError,TypeError,ValueError) as exc:
                zbox.label(text=("План зон: "+str(exc))[:75],icon="ERROR")
        elif count>=7 and session.get("parameters",{}).get("zone_tree_next"):
            ui.label(text="Пользовательские зоны опубликованы",icon="CHECKMARK")
            ui.label(text="Изменение готовой геометрии: следующий проход",icon="INFO")

        if count>=2:
            row=ui.row(align=True)
            for mode,title in (("HP","HP"),("LP","LP"),("BOTH","Оба"),("NONE","Скрыть")):
                row.operator("dwos.surface_view",text=title).mode=mode
        foot=ui.box()
        foot.operator("dwos.surface_export",text="Полная диагностика ZIP",icon="PACKAGE")
        foot.operator("dwos.surface_restore",text="Восстановить Backup",icon="LOOP_BACK")
        foot.label(text="Zone Composer NEXT: в разработке",icon="INFO")

CLASSES=(DWOS_PT_next_studio,)
