from __future__ import annotations
import uuid
import json
import bpy
from bpy.props import EnumProperty, IntProperty, StringProperty
from mathutils import Vector
from bpy_extras import view3d_utils
from .numeric import parse_meter_text, format_meter
from .window_builder import rebuild_window
from .dwos_log import event as log_event, exception as log_exception
from .dwos_history import push as history_push
from .mullion_core import parse_ids, encode_ids
from .branch_core import decode_state as decode_branch_state, encode_state as encode_branch_state, remove_split as branch_remove_split, layout_state as branch_layout_state
from .corner_rules import corner_posts as calc_corner_posts


def _active_window(context):
    obj=context.active_object
    for _ in range(6):
        if obj is None: break
        s=getattr(obj,'dwos_window',None)
        if s and s.is_window and not s.finalized: return obj
        obj=obj.parent
    return None

def _corner_topology(s):
    try:
        payload=json.loads(s.opening_payload_json or '{}');topo=payload.get('adaptive_topology')
        return topo if isinstance(topo,dict) and topo.get('classification')=='CORNER' else None
    except Exception:return None

class DWOS_OT_add_split(bpy.types.Operator):
    bl_idname='dwos.add_split'; bl_label='Добавить Split'; bl_options={'REGISTER'}
    axis: EnumProperty(items=[('V','Vertical',''),('H','Horizontal','')], default='V')
    def execute(self,context):
        history_push(context,'ADD_SPLIT')
        obj=_active_window(context)
        if not obj: self.report({'ERROR'},'Выберите редактируемое окно'); return {'CANCELLED'}
        s=obj.dwos_window
        if _corner_topology(s) and not s.zone_tree_json:
            zid=s.active_zone_id
            if not zid:
                self.report({'ERROR'},'Выберите сторону/зону CORNER окна во Viewport Composer');return {'CANCELLED'}
            return bpy.ops.dwos.zone_add('EXEC_DEFAULT',zone_id=zid,axis=self.axis)
        if s.zone_tree_json:
            from .zone_core import decode,layout
            w=parse_meter_text(s.width_text);h=parse_meter_text(s.height_text)
            fw=parse_meter_text(s.frame_width_text)
            leaves,_=layout(decode(s.zone_tree_json),w,h,fw)
            zid=s.active_zone_id or (leaves[0][0] if len(leaves)==1 else '')
            if not zid:
                self.report({'ERROR'},'Выберите зону во Viewport Composer')
                return {'CANCELLED'}
            return bpy.ops.dwos.zone_add('EXEC_DEFAULT',zone_id=zid,axis=self.axis)
        w=parse_meter_text(s.width_text); h=parse_meter_text(s.height_text); t=s.internal_width_text
        from .w5829_spacing_core import assert_center_allowed,assert_frame_allowed
        pos=(w if self.axis=='V' else h)*.5
        iw=parse_meter_text(s.internal_width_text)
        fw=parse_meter_text(s.frame_width_text)
        rows=[{'center':parse_meter_text(v.position_text),
               'width':parse_meter_text(v.thickness_text if v.override_thickness else s.internal_width_text)}
              for v in s.splits if v.axis==self.axis]
        try:
            assert_center_allowed(pos,iw,rows)
            assert_frame_allowed(pos,iw,0.,(w if self.axis=='V' else h),fw)
        except ValueError as exc:
            self.report({'WARNING'},str(exc));return {'CANCELLED'}
        sp=s.splits.add(); sp.split_id=uuid.uuid4().hex[:8]; sp.axis=self.axis
        sp.position_text=format_meter((w if self.axis=='V' else h)*0.5); sp.thickness_text=t; sp.override_thickness=False
        s.active_split_index=len(s.splits)-1
        try: rebuild_window(obj)
        except Exception as exc:
            s.splits.remove(len(s.splits)-1); log_exception('ADD_SPLIT_FAILED', exc, window=obj.name, axis=self.axis); self.report({'ERROR'},str(exc)); return {'CANCELLED'}
        log_event('ADD_SPLIT_OK', window=obj.name, axis=self.axis, split=sp.split_id, position=sp.position_text)
        return {'FINISHED'}

class DWOS_OT_add_split_zone(bpy.types.Operator):
    bl_idname='dwos.add_split_zone'
    bl_label='Добавить Split в выбранной зоне'
    bl_options={'REGISTER'}
    axis: EnumProperty(items=[('V','Vertical',''),('H','Horizontal','')],default='V')
    position_text: StringProperty(default='0')
    def execute(self,context):
        history_push(context,'ADD_SPLIT_ZONE')
        obj=_active_window(context)
        if not obj:return {'CANCELLED'}
        s=obj.dwos_window
        if _corner_topology(s) and not s.zone_tree_json:
            if not s.active_zone_id:
                self.report({'ERROR'},'Выберите сторону CORNER окна');return {'CANCELLED'}
            return bpy.ops.dwos.zone_add('EXEC_DEFAULT',zone_id=s.active_zone_id,axis=self.axis)
        if s.zone_tree_json:
            return bpy.ops.dwos.zone_add('EXEC_DEFAULT',zone_id=s.active_zone_id or 'root',axis=self.axis)
        try:
            position=parse_meter_text(self.position_text)
            limit=parse_meter_text(s.width_text if self.axis=='V' else s.height_text)
            if not 0 < position < limit:
                raise ValueError('Положение Split вне проёма')
            from .w5829_spacing_core import assert_center_allowed,assert_frame_allowed
            iw=parse_meter_text(s.internal_width_text)
            fw=parse_meter_text(s.frame_width_text)
            rows=[{'center':parse_meter_text(v.position_text),
                   'width':parse_meter_text(v.thickness_text if v.override_thickness else s.internal_width_text)}
                  for v in s.splits if v.axis==self.axis]
            assert_center_allowed(position,iw,rows)
            assert_frame_allowed(position,iw,0.,limit,fw)
            split=s.splits.add()
            split.split_id=uuid.uuid4().hex[:8]
            split.axis=self.axis
            split.position_text=format_meter(position)
            split.thickness_text=s.internal_width_text
            split.override_thickness=False
            s.active_split_index=len(s.splits)-1
            try:rebuild_window(obj)
            except Exception:
                s.splits.remove(len(s.splits)-1)
                s.active_split_index=min(s.active_split_index,len(s.splits)-1)
                raise
        except Exception as exc:
            log_exception('ZONE_SPLIT_FAILED',exc,window=obj.name,axis=self.axis)
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        log_event('ZONE_SPLIT_ADDED',window=obj.name,axis=self.axis,position=format_meter(position))
        return {'FINISHED'}


class DWOS_OT_remove_split(bpy.types.Operator):
    bl_idname='dwos.remove_split'; bl_label='Удалить Split'; bl_options={'REGISTER'}
    index: IntProperty(default=-1)
    def execute(self,context):
        history_push(context,'REMOVE_SPLIT')
        obj=_active_window(context)
        if not obj: return {'CANCELLED'}
        s=obj.dwos_window
        if (_corner_topology(s) and not s.zone_tree_json) or s.zone_tree_json:
            if not s.active_zone_split_id:return {'CANCELLED'}
            return bpy.ops.dwos.zone_delete('EXEC_DEFAULT',split_id=s.active_zone_split_id)
        idx=self.index if self.index>=0 else s.active_split_index
        if idx<0 or idx>=len(s.splits): return {'CANCELLED'}
        split_id=s.splits[idx].split_id
        s.splits.remove(idx); s.active_split_index=min(idx,len(s.splits)-1)
        rebuild_window(obj); log_event('REMOVE_SPLIT_OK', window=obj.name, split=split_id); return {'FINISHED'}


class DWOS_OT_delete_selected_mullions(bpy.types.Operator):
    bl_idname='dwos.delete_selected_mullions'
    bl_label='Удалить выбранные импосты'
    bl_options={'REGISTER'}

    def execute(self,context):
        history_push(context,'DELETE_MULLION')
        obj=_active_window(context)
        if not obj:return {'CANCELLED'}
        s=obj.dwos_window
        ids=parse_ids(s.mullion_selection_json)
        if not ids:
            if s.zone_tree_json and s.active_zone_split_id:
                ids=[s.active_zone_split_id]
            elif 0<=s.active_split_index<len(s.splits):
                ids=[s.splits[s.active_split_index].split_id]
        if not ids:
            self.report({'INFO'},'Сначала выберите импост')
            return {'CANCELLED'}
        try:
            topo=_corner_topology(s)
            if topo and not s.zone_tree_json:
                w=parse_meter_text(s.width_text);h=parse_meter_text(s.height_text);fw=parse_meter_text(s.frame_width_text)
                posts=calc_corner_posts(topo,getattr(s,'corner_post_mode','AUTO'),parse_meter_text(s.internal_width_text),parse_meter_text(s.frame_depth_text))
                state=decode_branch_state(getattr(s,'branch_state_json',''),topo);old=s.branch_state_json
                existing={r[0] for r in branch_layout_state(state,topo,h,fw,posts)[1]}
                ordered=[sid for sid in ids if sid in existing]
                if not ordered:raise ValueError('Выбранные импосты больше не существуют')
                for sid in ordered:state=branch_remove_split(state,topo,h,fw,sid,posts)
                s.branch_state_json=encode_branch_state(state);s.active_zone_split_id='';s.active_zone_id=''
                try:rebuild_window(obj)
                except Exception:
                    s.branch_state_json=old;rebuild_window(obj);raise
                deleted=ordered
            elif s.zone_tree_json:
                from .zone_core import decode,encode,remove,find
                w=parse_meter_text(s.width_text);h=parse_meter_text(s.height_text);fw=parse_meter_text(s.frame_width_text)
                tree=decode(s.zone_tree_json)
                # Delete deepest nodes first. Parent deletion intentionally merges its
                # two zones; descendants disappear only when their parent is deleted.
                def depth(node,target,d=0):
                    if node.get('id')==target:return d
                    for child in node.get('children',[]):
                        q=depth(child,target,d+1)
                        if q is not None:return q
                    return None
                ordered=sorted((sid for sid in ids if find(tree,sid) is not None),
                               key=lambda sid:(depth(tree,sid) or 0),reverse=True)
                if not ordered:raise ValueError('Выбранные импосты больше не существуют')
                old=s.zone_tree_json
                for sid in ordered:
                    if find(tree,sid) is not None:
                        tree=remove(tree,sid,w,h,fw)
                s.zone_tree_json=encode(tree)
                s.active_zone_split_id='';s.active_zone_id=''
                try:rebuild_window(obj)
                except Exception:
                    s.zone_tree_json=old;rebuild_window(obj);raise
                deleted=ordered
            else:
                snapshot=[(sp.split_id,sp.axis,sp.position_text,sp.thickness_text,sp.override_thickness) for sp in s.splits]
                wanted=set(ids);deleted=[row[0] for row in snapshot if row[0] in wanted]
                if not deleted:raise ValueError('Выбранные импосты больше не существуют')
                for i in reversed(range(len(s.splits))):
                    if s.splits[i].split_id in wanted:s.splits.remove(i)
                s.active_split_index=-1
                try:rebuild_window(obj)
                except Exception:
                    s.splits.clear()
                    for sid,axis,pos,thick,over in snapshot:
                        sp=s.splits.add();sp.split_id=sid;sp.axis=axis;sp.position_text=pos;sp.thickness_text=thick;sp.override_thickness=over
                    rebuild_window(obj);raise
            s.mullion_selection_json='[]'
            log_event('DELETE_MULLION_OK',window=obj.name,count=len(deleted),ids=','.join(deleted))
        except Exception as exc:
            log_exception('DELETE_MULLION_REJECTED',exc,window=obj.name)
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        return {'FINISHED'}

class DWOS_OT_reset_split_thickness(bpy.types.Operator):
    bl_idname='dwos.reset_split_thickness'; bl_label='Общая толщина'; bl_options={'REGISTER'}
    index: IntProperty(default=-1)
    def execute(self,context):
        obj=_active_window(context)
        if not obj:return {'CANCELLED'}
        s=obj.dwos_window
        if s.zone_tree_json or (_corner_topology(s) and not s.zone_tree_json):
            if not s.active_zone_split_id:
                self.report({'WARNING'},'Выберите импост на активной стороне CORNER / Zone')
                return {'CANCELLED'}
            # zone_edit performs its own validation, history checkpoint and rollback.
            return bpy.ops.dwos.zone_edit('EXEC_DEFAULT',split_id=s.active_zone_split_id,
                target='THICKNESS',value_text=s.internal_width_text)
        idx=self.index
        if idx<0 or idx>=len(s.splits):return {'CANCELLED'}
        split=s.splits[idx]
        old_thickness,old_override=split.thickness_text,bool(split.override_thickness)
        history_push(context,'RESET_SPLIT_THICKNESS')
        try:
            split.thickness_text=s.internal_width_text
            split.override_thickness=False
            rebuild_window(obj)
        except Exception as exc:
            split.thickness_text=old_thickness
            split.override_thickness=old_override
            try:rebuild_window(obj)
            except Exception as rollback_exc:
                log_exception('RESET_SPLIT_ROLLBACK_FAILED',rollback_exc,window=obj.name)
            log_exception('RESET_SPLIT_REJECTED',exc,window=obj.name)
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}

class DWOS_OT_drag_split(bpy.types.Operator):
    bl_idname='dwos.drag_split'; bl_label='Двигать Split во Viewport'; bl_options={'REGISTER','BLOCKING'}
    _obj=None; _idx=-1; _old=''; _ghost=None
    def invoke(self,context,event):
        obj=_active_window(context)
        if not obj or context.area.type!='VIEW_3D': return {'CANCELLED'}
        if obj.dwos_window.zone_tree_json or (_corner_topology(obj.dwos_window) and not obj.dwos_window.zone_tree_json):
            self.report({'INFO'},'Перетаскивайте локальный Split прямо в Composer')
            return {'CANCELLED'}
        idx=obj.dwos_window.active_split_index
        if idx<0 or idx>=len(obj.dwos_window.splits): self.report({'ERROR'},'Сначала выберите Split в списке'); return {'CANCELLED'}
        self._obj=obj; self._idx=idx; self._old=obj.dwos_window.splits[idx].position_text
        context.window_manager.modal_handler_add(self); return {'RUNNING_MODAL'}
    def _local_hit(self,context,event):
        region=context.region; rv3d=context.region_data
        coord=(event.mouse_region_x,event.mouse_region_y)
        origin=view3d_utils.region_2d_to_origin_3d(region,rv3d,coord); direction=view3d_utils.region_2d_to_vector_3d(region,rv3d,coord)
        inv=self._obj.matrix_world.inverted(); o=inv@origin; d=inv.to_3x3()@direction
        if abs(d.z)<1e-8:return None
        plane_z=parse_meter_text(self._obj.dwos_window.frame_offset_text,allow_negative=True)
        t=(plane_z-o.z)/d.z
        if t<0:return None
        return o+d*t
    def modal(self,context,event):
        if event.type=='MOUSEMOVE':
            hit=self._local_hit(context,event)
            if hit is not None:
                s=self._obj.dwos_window; sp=s.splits[self._idx]
                w=parse_meter_text(s.width_text); h=parse_meter_text(s.height_text)
                raw=(hit.x+w*0.5) if sp.axis=='V' else (hit.y+h*0.5)
                limit=(w if sp.axis=='V' else h)
                raw=max(0.0,min(limit,raw)); self._ghost=raw
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type=='LEFTMOUSE' and event.value=='RELEASE':
            if self._ghost is None:return {'CANCELLED'}
            history_push(context,'DRAG_SPLIT_LEGACY')
            self._obj.dwos_window.splits[self._idx].position_text=format_meter(self._ghost)
            try: rebuild_window(self._obj)
            except Exception as exc:
                self._obj.dwos_window.splits[self._idx].position_text=self._old
                log_exception('DRAG_SPLIT_FAILED', exc, window=self._obj.name)
                self.report({'ERROR'},str(exc)); return {'CANCELLED'}
            log_event('DRAG_SPLIT_OK', window=self._obj.name, position=self._obj.dwos_window.splits[self._idx].position_text)
            return {'FINISHED'}
        if event.type in {'ESC','RIGHTMOUSE'}:
            self._ghost=None; context.area.tag_redraw(); return {'CANCELLED'}
        return {'RUNNING_MODAL'}
