"""Explicit, non-destructive controls for DWOS opening-detection outlines."""
from __future__ import annotations
import bpy
from bpy.types import Operator
from .dwos_log import event as log_event


class DWOS_OT_hide_opening_outlines(Operator):
    bl_idname='dwos.hide_opening_outlines'
    bl_label='Скрыть обводки проёмов'
    bl_description='Выключить только зелёные линии поиска, не изменяя Mesh и кандидаты'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        context.scene.dwos.show_overlay=False
        for area in context.screen.areas if context.screen else []:
            if area.type=='VIEW_3D':area.tag_redraw()
        log_event('OPENING_OUTLINES_HIDDEN',candidates=len(context.scene.dwos.openings))
        return {'FINISHED'}


class DWOS_OT_reset_opening_outlines(Operator):
    bl_idname='dwos.reset_opening_outlines'
    bl_label='Очистить найденные обводки'
    bl_description='Забыть кэш найденных проёмов; существующие Mesh окна не удаляются'
    bl_options={'REGISTER','UNDO'}

    def invoke(self,context,event):
        return context.window_manager.invoke_confirm(self,event)

    def execute(self,context):
        s=context.scene.dwos
        count=len(s.openings)
        s.show_overlay=False
        s.openings.clear()
        s.wall_name=''
        s.status='Контуры очищены. Для новых окон снова нажмите «Найти проёмы».'
        for area in context.screen.areas if context.screen else []:
            if area.type=='VIEW_3D':area.tag_redraw()
        log_event('OPENING_OUTLINES_RESET',removed=count)
        self.report({'INFO'},f'Кэш {count} обводок сброшен. Геометрия не изменялась.')
        return {'FINISHED'}


CLASSES=(DWOS_OT_hide_opening_outlines,DWOS_OT_reset_opening_outlines)
