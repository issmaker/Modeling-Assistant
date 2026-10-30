"""Blender transactions for recursive Zone Split edits. No BMesh handles retained."""
from __future__ import annotations
import copy
import uuid
import json
import bpy
from bpy.types import Operator
from bpy.props import EnumProperty, StringProperty
from .numeric import parse_meter_text,format_meter
from .zone_core import decode,encode,layout,add,update,remove,from_global,find
from .window_builder import rebuild_window,_splits_from_settings
from .batch_edit import resolve_window
from .dwos_log import event as log_event,exception as log_exception
from .dwos_history import push as history_push
from .branch_core import (decode_state as decode_branch_state, encode_state as encode_branch_state,
    layout_state as branch_layout_state, add_split as branch_add_split,
    update_split as branch_update_split, remove_split as branch_remove_split,
    find_branch as find_branch_state, validate_post_clearance as validate_branch_post_clearance)
from .corner_rules import corner_posts as calc_corner_posts


def _dims(s):
    return tuple(parse_meter_text(x) for x in (s.width_text,s.height_text,s.frame_width_text))


def _tree(s):
    w,h,fw=_dims(s)
    if s.zone_tree_json:
        return decode(s.zone_tree_json)
    if s.splits:
        return from_global(_splits_from_settings(s,w,h),w,h,fw,
                           parse_meter_text(s.internal_width_text))
    return decode('')



def _adaptive_topology(s):
    try:
        payload=json.loads(s.opening_payload_json or '{}')
        topo=payload.get('adaptive_topology')
        return topo if isinstance(topo,dict) and topo.get('classification')=='CORNER' else None
    except Exception:
        return None


def _branch_mode(s):
    return bool(_adaptive_topology(s) and not s.zone_tree_json)


def _perform_branch(obj, operation):
    s=obj.dwos_window; topo=_adaptive_topology(s)
    if not topo:raise ValueError('CORNER Branch topology is unavailable')
    w,h,fw=_dims(s); internal=parse_meter_text(s.internal_width_text)
    posts=calc_corner_posts(topo,getattr(s,'corner_post_mode','AUTO'),internal,parse_meter_text(s.frame_depth_text))
    previous=s.branch_state_json; old_zone=s.active_zone_id; old_split=s.active_zone_split_id; old_branch=getattr(s,'active_branch_id','')
    state=decode_branch_state(previous,topo)
    next_state,zone_ref,split_id,branch_id=operation(state,topo,w,h,fw,posts)
    validate_branch_post_clearance(next_state,topo,h,fw,posts)
    try:
        s.branch_state_json=encode_branch_state(next_state)
        s.active_zone_id=zone_ref or ''
        s.active_zone_split_id=split_id or ''
        s.active_branch_id=branch_id or old_branch
        rebuild_window(obj)
        s.splits.clear();s.active_split_index=-1
    except Exception:
        s.branch_state_json=previous;s.active_zone_id=old_zone;s.active_zone_split_id=old_split;s.active_branch_id=old_branch
        try:rebuild_window(obj)
        except Exception as ex:log_exception('BRANCH_ROLLBACK_REBUILD_FAILED',ex,window=obj.name)
        raise
    leaves,bands=branch_layout_state(next_state,topo,h,fw,posts)
    log_event('BRANCH_TREE_COMMIT',window=obj.name,branches=len(next_state.get('branches',[])),zones=len(leaves),splits=len(bands))

def _perform(obj, operation):
    s=obj.dwos_window
    w,h,fw=_dims(s)
    prev_json=s.zone_tree_json
    original_splits=[(a.split_id,a.axis,a.position_text,a.thickness_text,a.override_thickness) for a in s.splits]
    old_zone=s.active_zone_id; old_split=s.active_zone_split_id
    tree=_tree(s)
    next_tree,zone_id,split_id=operation(tree,w,h,fw)
    layout(next_tree,w,h,fw)
    try:
        s.zone_tree_json=encode(next_tree)
        s.active_zone_id=zone_id
        s.active_zone_split_id=split_id
        rebuild_window(obj)
        # Legacy is no longer authoritative once the new tree is committed.
        s.splits.clear()
        s.active_split_index=-1
    except Exception:
        s.zone_tree_json=prev_json
        s.active_zone_id=old_zone; s.active_zone_split_id=old_split
        s.splits.clear()
        for rid,axis,pos,thick,override in original_splits:
            sp=s.splits.add()
            sp.split_id=rid;sp.axis=axis;sp.position_text=pos
            sp.thickness_text=thick;sp.override_thickness=override
        try:rebuild_window(obj)
        except Exception as ex: log_exception('ZONE_ROLLBACK_REBUILD_FAILED',ex,window=obj.name)
        raise
    log_event('ZONE_TREE_COMMIT',window=obj.name,zones=len(layout(next_tree,w,h,fw)[0]),
              splits=len(layout(next_tree,w,h,fw)[1]),op=operation.__name__)


class DWOS_OT_zone_add(Operator):
    bl_idname='dwos.zone_add';bl_label='Разделить выбранную зону';bl_options={'REGISTER'}
    zone_id:StringProperty(default='root')
    axis:EnumProperty(items=[('V','Вертикально',''),('H','Горизонтально','')],default='V')
    def execute(self,context):
        history_push(context,'ZONE_ADD')
        obj=resolve_window(context.active_object)
        if not obj or obj.dwos_window.finalized:return {'CANCELLED'}
        try:
            if _branch_mode(obj.dwos_window):
                def action(state,topo,w,h,fw,posts):
                    zone_ref=self.zone_id or ''
                    if '|' not in zone_ref:
                        bid=obj.dwos_window.active_branch_id
                        if not bid:raise ValueError('Выберите сторону CORNER окна')
                        zone_ref=f'{bid}|{zone_ref or "root"}'
                    staged,sid=branch_add_split(state,topo,h,fw,zone_ref,self.axis,
                        parse_meter_text(obj.dwos_window.internal_width_text),posts)
                    bid=zone_ref.split('|',1)[0]
                    return staged,'',sid,bid
                _perform_branch(obj,action)
            else:
                def action(tree,w,h,fw):
                    new,sid=add(tree,self.zone_id or 'root',self.axis,
                                parse_meter_text(obj.dwos_window.internal_width_text),w,h,fw)
                    return new, '',sid
                _perform(obj,action)
        except Exception as exc:
            log_exception('ZONE_ADD_REJECTED',exc,zone=self.zone_id)
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        return {'FINISHED'}


class DWOS_OT_zone_add_many(Operator):
    bl_idname='dwos.zone_add_many'; bl_label='Разделить выбранные зоны';bl_options={'REGISTER'}
    zone_ids_json:StringProperty(default='[]')
    axis:EnumProperty(items=[('V','Вертикально',''),('H','Горизонтально','')],default='V')
    def execute(self,context):
        obj=resolve_window(context.active_object)
        if not obj or obj.dwos_window.finalized:return {'CANCELLED'}
        from .zone_multi_core import normalize_zone_ids,stage_zone_add_many,stage_branch_add_many
        try:
            ids=normalize_zone_ids(self.zone_ids_json)
            thickness=parse_meter_text(obj.dwos_window.internal_width_text)
            if _branch_mode(obj.dwos_window):
                def action(state,topo,w,h,fw,posts):
                    staged=stage_branch_add_many(state,topo,ids,self.axis,thickness,h,fw,posts)
                    return staged,'','',str(ids[-1].split('|',1)[0])
                # One undo step / one staged rebuild, not N partial operators.
                _perform_branch(obj,action)
            else:
                def action(tree,w,h,fw):
                    staged=stage_zone_add_many(tree,ids,self.axis,thickness,w,h,fw)
                    return staged,'',''
                _perform(obj,action)
        except Exception as exc:
            reason=f'{type(exc).__name__}: {exc}'
            log_exception('ZONE_ADD_MANY_REJECTED',exc,window=obj.name,zone_ids=self.zone_ids_json,axis=self.axis)
            obj['DWOS_last_group_split_error']=reason
            self.report({'WARNING'},'Групповой Split: '+str(exc));return {'CANCELLED'}
        if 'DWOS_last_group_split_error' in obj:del obj['DWOS_last_group_split_error']
        log_event('ZONE_ADD_MANY_OK',window=obj.name,count=len(ids),axis=self.axis)
        return {'FINISHED'}


class DWOS_OT_zone_edit(Operator):
    bl_idname='dwos.zone_edit';bl_label='Изменить локальный Split';bl_options={'REGISTER'}
    split_id:StringProperty(default='')
    target:EnumProperty(items=[('POSITION','Позиция',''),('THICKNESS','Толщина','')],default='POSITION')
    value_text:StringProperty(default='0')
    def execute(self,context):
        history_push(context,'ZONE_EDIT')
        obj=resolve_window(context.active_object)
        if not obj or obj.dwos_window.finalized:return {'CANCELLED'}
        try:
            value=parse_meter_text(self.value_text)
            if _branch_mode(obj.dwos_window):
                def action(state,topo,w,h,fw,posts):
                    # Determine axis from the current branch layout row.
                    rows=branch_layout_state(state,topo,h,fw,posts)[1]
                    row=next((r for r in rows if r[0]==self.split_id),None)
                    if row is None:raise ValueError('Split больше не существует')
                    axis=row[1];center=None;thickness=None
                    if self.target=='POSITION':center=value-((w if axis=='V' else h)*0.5)
                    else:thickness=value
                    staged=branch_update_split(state,topo,h,fw,self.split_id,center=center,thickness=thickness,posts=posts)
                    bid=next((r[6] for r in rows if r[0]==self.split_id),'')
                    return staged,'',self.split_id,bid
                _perform_branch(obj,action)
            else:
                def action(tree,w,h,fw):
                    node=find(tree,self.split_id)
                    if not node or node.get('axis') is None:raise ValueError('Split больше не существует')
                    if self.target=='POSITION':
                        # global metre coordinate, measured from left/bottom of entire Opening
                        local=value-((w if node['axis']=='V' else h)*0.5)
                        new=update(tree,self.split_id,w,h,fw,center=local)
                    else:
                        new=update(tree,self.split_id,w,h,fw,thickness=value)
                    return new, '',self.split_id
                _perform(obj,action)
        except ValueError as exc:
            # Expected dimensional guard: not an internal error/traceback.
            log_event('ZONE_EDIT_LIMIT',split=self.split_id,target=self.target,reason=str(exc))
            self.report({'WARNING'},str(exc));return {'CANCELLED'}
        except Exception as exc:
            log_exception('ZONE_EDIT_FAILED',exc,split=self.split_id,target=self.target)
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        return {'FINISHED'}


class DWOS_OT_zone_delete(Operator):
    bl_idname='dwos.zone_delete';bl_label='Удалить локальный Split';bl_options={'REGISTER'}
    split_id:StringProperty(default='')
    def execute(self,context):
        history_push(context,'ZONE_DELETE')
        obj=resolve_window(context.active_object)
        if not obj or obj.dwos_window.finalized:return {'CANCELLED'}
        try:
            if _branch_mode(obj.dwos_window):
                def action(state,topo,w,h,fw,posts):
                    rows=branch_layout_state(state,topo,h,fw,posts)[1]
                    bid=next((r[6] for r in rows if r[0]==self.split_id),'')
                    return branch_remove_split(state,topo,h,fw,self.split_id,posts),'','',bid
                _perform_branch(obj,action)
            else:
                def action(tree,w,h,fw):
                    return remove(tree,self.split_id,w,h,fw),'',''
                _perform(obj,action)
        except Exception as exc:
            log_exception('ZONE_DELETE_REJECTED',exc,split=self.split_id)
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        return {'FINISHED'}


CLASSES=(DWOS_OT_zone_add,DWOS_OT_zone_add_many,DWOS_OT_zone_edit,DWOS_OT_zone_delete)
