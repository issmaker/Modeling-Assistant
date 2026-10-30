"""2.3 Blender integration for logical mullion selection, groups and constraints.

All changes are staged in pure data, fully validated before committing to
window Mesh. Host geometry, materials, UV and glass semantics are untouched.
"""
from __future__ import annotations
import copy
import json
import uuid
import math
import bpy
from bpy.types import Operator
from bpy.props import BoolProperty, EnumProperty, StringProperty
from .mullion_core import (parse_ids, encode_ids, clean_ids, select_ids, entries,
                           targets, apply_zone_targets, legacy_targets,
                           decode_groups, encode_groups)
from .zone_core import decode, encode, layout
from .numeric import parse_meter_text, format_meter
from .window_builder import rebuild_window
from .batch_edit import resolve_window, target_windows
from .dwos_log import event as log_event, exception as log_exception
from .dwos_history import push as history_push
from .branch_core import (decode_state as decode_branch_state, encode_state as encode_branch_state,
    rows_as_dicts as branch_rows, apply_positions as apply_branch_positions)
from .corner_rules import corner_posts as calc_corner_posts


def _window(context):
    obj=resolve_window(context.active_object)
    if obj is None or obj.dwos_window.finalized or obj.get('DWOS_archived_source'):
        raise ValueError('Выберите редактируемое окно DWOS')
    return obj


def _dims(s):
    return tuple(parse_meter_text(v) for v in (s.width_text,s.height_text,s.frame_width_text))


def _topology(s):
    try:
        payload=json.loads(s.opening_payload_json or '{}')
        topo=payload.get('adaptive_topology')
        return topo if isinstance(topo,dict) and topo.get('classification')=='CORNER' else None
    except Exception:
        return None


def _branch_rows(s):
    topo=_topology(s)
    if not topo or s.zone_tree_json:return None
    _w,h,fw=_dims(s)
    posts=calc_corner_posts(topo,getattr(s,'corner_post_mode','AUTO'),parse_meter_text(s.internal_width_text),parse_meter_text(s.frame_depth_text))
    state=decode_branch_state(getattr(s,'branch_state_json',''),topo)
    return branch_rows(state,topo,h,fw,posts)


def _rows(s):
    w,h,fw=_dims(s)
    b=_branch_rows(s)
    if b is not None:return b
    if s.zone_tree_json:
        return entries(s.zone_tree_json,w,h,fw)
    result=[]
    for sp in s.splits:
        result.append({'id':sp.split_id,'axis':sp.axis,
                       'position':parse_meter_text(sp.position_text),
                       'thickness':parse_meter_text(sp.thickness_text if sp.override_thickness else s.internal_width_text),
                       'rect': (fw,w-fw,fw,h-fw)})
    return result


def _selection(s):
    return clean_ids(s.mullion_selection_json,[r['id'] for r in _rows(s)])


def _stage(s, wanted):
    w,h,fw=_dims(s)
    topo=_topology(s)
    if topo and not s.zone_tree_json:
        posts=calc_corner_posts(topo,getattr(s,'corner_post_mode','AUTO'),parse_meter_text(s.internal_width_text),parse_meter_text(s.frame_depth_text))
        state=decode_branch_state(getattr(s,'branch_state_json',''),topo)
        return 'branch', encode_branch_state(apply_branch_positions(state,topo,h,fw,wanted,posts))
    if s.zone_tree_json:
        return 'tree', encode(apply_zone_targets(s.zone_tree_json,w,h,fw,wanted))
    return 'legacy', {sp.split_id:format_meter(value) for sp in s.splits
                      if (value:=wanted.get(sp.split_id)) is not None}


def _snapshot(s):
    return s.zone_tree_json, getattr(s,'branch_state_json',''), [(sp.split_id,sp.position_text) for sp in s.splits]


def _restore(s,old):
    s.zone_tree_json=old[0];s.branch_state_json=old[1]
    mapping=dict(old[2])
    for sp in s.splits:
        if sp.split_id in mapping:
            sp.position_text=mapping[sp.split_id]


def _commit_many(actions):
    """All staged first; rollback every window when Blender rebuild fails."""
    backups=[(obj,_snapshot(obj.dwos_window)) for obj,_ in actions]
    touched=[]
    try:
        for obj,(kind,data) in actions:
            s=obj.dwos_window
            if kind=='tree': s.zone_tree_json=data
            elif kind=='branch': s.branch_state_json=data
            else:
                for sp in s.splits:
                    if sp.split_id in data:sp.position_text=data[sp.split_id]
            touched.append(obj)
            rebuild_window(obj)
    except Exception:
        for obj,old in backups:
            _restore(obj.dwos_window,old)
        for obj in touched:
            try:rebuild_window(obj)
            except Exception as exc:log_exception('MULLION_ROLLBACK_REBUILD_FAILED',exc,window=obj.name)
        raise


class DWOS_OT_mullion_select(Operator):
    bl_idname='dwos.mullion_select';bl_label='Выделить импост';bl_options={'INTERNAL'}
    split_id:StringProperty(default='')
    extend:BoolProperty(default=False)
    def execute(self,context):
        try:
            s=_window(context).dwos_window
            found={r['id'] for r in _rows(s)}
            if self.split_id and self.split_id not in found:raise ValueError('Split не найден')
            s.mullion_selection_json=encode_ids(select_ids(_selection(s),self.split_id,extend=self.extend) if self.split_id else [])
            if self.split_id:
                if s.zone_tree_json or _branch_rows(s) is not None:s.active_zone_split_id=self.split_id
                else:
                    s.active_split_index=next((i for i,sp in enumerate(s.splits) if sp.split_id==self.split_id),-1)
        except Exception as exc:
            self.report({'WARNING'},str(exc));return {'CANCELLED'}
        return {'FINISHED'}


class DWOS_OT_mullion_select_axis(Operator):
    bl_idname='dwos.mullion_select_axis';bl_label='Выбрать импосты по направлению';bl_options={'REGISTER'}
    axis:EnumProperty(items=[('H','Горизонтальные',''),('V','Вертикальные',''),('ALL','Все',''),('NONE','Очистить','')],default='H')
    def execute(self,context):
        try:
            s=_window(context).dwos_window
            ids=[r['id'] for r in _rows(s) if self.axis=='ALL' or (self.axis!='NONE' and r['axis']==self.axis)]
            s.mullion_selection_json=encode_ids(ids)
            self.report({'INFO'},f'Выбрано импостов: {len(ids)}')
        except Exception as exc:
            self.report({'WARNING'},str(exc));return {'CANCELLED'}
        return {'FINISHED'}


class DWOS_OT_mullion_group_edit(Operator):
    bl_idname='dwos.mullion_group_edit';bl_label='Групповое редактирование импостов';bl_options={'REGISTER'}
    delta_text:StringProperty(default='')
    action:EnumProperty(items=[('MOVE','Сдвинуть',''),('DISTRIBUTE','Распределить',''),
        ('STEP','Равный шаг',''),('CENTER','Центрировать группу',''),('ALIGN','В одну линию','')],default='MOVE')
    def execute(self,context):
        history_push(context,'MULLION_GROUP_EDIT')
        try:
            obj=_window(context);s=obj.dwos_window
            selected=_selection(s);rows=_rows(s)
            value=(parse_meter_text(self.delta_text or s.mullion_offset_text,allow_negative=True) if self.action=='MOVE' else
                   parse_meter_text(s.mullion_step_text) if self.action=='STEP' else 0.)
            # Pure geometry preflight permits ALIGN only for disjoint zone bands.
            # Legacy global splits are necessarily overlapping and reject ALIGN.
            if s.zone_tree_json or _branch_rows(s) is not None:
                desired=targets(rows,selected,self.action,value=value)
            else:
                w,h,fw=_dims(s)
                desired=legacy_targets(rows,selected,self.action,value,width=w,height=h,frame_width=fw)
            staged=_stage(s,desired)
            _commit_many([(obj,staged)])
            log_event('MULLION_GROUP_EDIT_OK',window=obj.name,action=self.action,count=len(selected),value=value)
        except Exception as exc:
            log_exception('MULLION_GROUP_EDIT_REJECTED',exc,action=self.action)
            self.report({'WARNING'},str(exc));return {'CANCELLED'}
        return {'FINISHED'}


class DWOS_OT_mullion_link(Operator):
    bl_idname='dwos.mullion_link';bl_label='Связать выбранные импосты';bl_options={'REGISTER'}
    unlink:BoolProperty(default=False)
    def execute(self,context):
        history_push(context,'MULLION_LINK')
        try:
            obj=_window(context);s=obj.dwos_window
            groups=decode_groups(s.mullion_groups_json)
            name=s.mullion_group_name.strip()
            if not name:raise ValueError('Введите имя группы')
            if self.unlink:
                if name not in groups:raise ValueError('Группа не найдена')
                del groups[name]
            else:
                ids=_selection(s)
                if len(ids)<2:raise ValueError('Выберите минимум два импоста')
                groups[name]=ids
            s.mullion_groups_json=encode_groups(groups)
        except Exception as exc:
            self.report({'WARNING'},str(exc));return {'CANCELLED'}
        return {'FINISHED'}


class DWOS_OT_mullion_load_group(Operator):
    bl_idname='dwos.mullion_load_group';bl_label='Выделить связанную группу';bl_options={'REGISTER'}
    def execute(self,context):
        history_push(context,'MULLION_LOAD_GROUP')
        try:
            s=_window(context).dwos_window
            ids=decode_groups(s.mullion_groups_json).get(s.mullion_group_name.strip())
            if not ids:raise ValueError('Нет группы с таким именем')
            selected=clean_ids(ids,[r['id'] for r in _rows(s)])
            if len(selected)<2:raise ValueError('Группа устарела, изменилось дерево Split')
            s.mullion_selection_json=encode_ids(selected)
        except Exception as exc:
            self.report({'WARNING'},str(exc));return {'CANCELLED'}
        return {'FINISHED'}


def _layout_signature(tree):
    """Topology, excluding position and ID; used to reject unsafe transfers."""
    n=decode(tree)
    def walk(node):
        if node.get('axis') is None:return ('leaf',)
        return (node['axis'],walk(node['children'][0]),walk(node['children'][1]))
    return walk(n)


def _sync_stage(active,other,method):
    a=active.dwos_window;b=other.dwos_window
    ar=_rows(a);br=_rows(b)
    ids=_selection(a)
    a_by={r['id']:r for r in ar}
    selected=[a_by[i] for i in ids]
    if not selected:raise ValueError('Нет выбранных импостов для синхронизации')
    if bool(a.zone_tree_json)!=bool(b.zone_tree_json):
        raise ValueError('Разная структура Split Tree / Legacy')
    if a.zone_tree_json and _layout_signature(a.zone_tree_json)!=_layout_signature(b.zone_tree_json):
        raise ValueError('Не совпадает структура зон')
    wa,ha,_=_dims(a);wb,hb,_=_dims(b)
    # Match by preorder in zone tree, or axis+position ranking in Legacy.
    if a.zone_tree_json:
        if len(ar)!=len(br) or [r['axis'] for r in ar]!=[r['axis'] for r in br]:
            raise ValueError('Не совпадает структура импостов')
        mapping={ar[i]['id']:br[i] for i in range(len(ar))}
    else:
        mapping={}
        for axis in ('V','H'):
            aa=sorted((r for r in ar if r['axis']==axis),key=lambda r:(r['position'],r['id']))
            bb=sorted((r for r in br if r['axis']==axis),key=lambda r:(r['position'],r['id']))
            if len(aa)!=len(bb):raise ValueError('Разное число импостов '+axis)
            mapping.update({x['id']:y for x,y in zip(aa,bb)})
    wanted={}
    for src in selected:
        dst=mapping[src['id']]
        da=wa if src['axis']=='V' else ha;db=wb if src['axis']=='V' else hb
        if a.zone_tree_json:
            relative=(src['position']/da + .5)
            pos=(src['position']+(da-db)/2) if method=='ABSOLUTE' else (relative-.5)*db
        else:
            relative=src['position']/da
            pos=src['position'] if method=='ABSOLUTE' else relative*db
        wanted[dst['id']]=pos
    if not b.zone_tree_json:
        # Do not allow overlapping, even with non-selected splits.
        rows=_rows(b)
        w,h,fw=_dims(b)
        all_values={r['id']:wanted.get(r['id'],r['position']) for r in rows}
        for axis,extent in (('V',w),('H',h)):
            same=sorted((all_values[r['id']],r['thickness']) for r in rows if r['axis']==axis)
            for (x,tx),(y,ty) in zip(same,same[1:]):
                if y-x<=(tx+ty)/2+1e-6:raise ValueError('После синхронизации импосты наложатся')
            for x,t in same:
                if x-t/2<=fw+1e-6 or x+t/2>=extent-fw-1e-6:
                    raise ValueError('Импост выйдет за край целевого окна')
    return _stage(b,wanted)


class DWOS_OT_mullion_sync(Operator):
    bl_idname='dwos.mullion_sync';bl_label='Синхронизировать импосты окон';bl_options={'REGISTER'}
    method:EnumProperty(items=[('ABSOLUTE','Метры от начала',''),('RELATIVE','Процент окна','')],default='ABSOLUTE')
    def execute(self,context):
        history_push(context,'MULLION_SYNC')
        try:
            active=_window(context)
            others=[obj for obj in target_windows(context,active) if obj!=active and not obj.dwos_window.finalized]
            if not others:raise ValueError('Выберите область SEL / SIZE / ALL с другими окнами')
            staged=[(obj,_sync_stage(active,obj,self.method)) for obj in others]
            _commit_many(staged)
            log_event('MULLION_SYNC_OK',source=active.name,targets=len(others),method=self.method)
            self.report({'INFO'},f'Синхронизировано окон: {len(others)}')
        except Exception as exc:
            log_exception('MULLION_SYNC_REJECTED',exc)
            self.report({'WARNING'},str(exc));return {'CANCELLED'}
        return {'FINISHED'}


CLASSES=(DWOS_OT_mullion_select,DWOS_OT_mullion_select_axis,
         DWOS_OT_mullion_group_edit,DWOS_OT_mullion_link,
         DWOS_OT_mullion_load_group,DWOS_OT_mullion_sync)


def _preset_data(s):
    from .zone_core import layout, decode
    w,h,fw=_dims(s)
    if s.zone_tree_json:
        tree=decode(s.zone_tree_json)
        layout(tree,w,h,fw)
        return {'kind':'TREE','tree':tree}
    return {'kind':'LEGACY', 'splits':[
        {'axis':r['axis'], 'ratio':r['position']/(w if r['axis']=='V' else h),
         'thickness':r['thickness']} for r in _rows(s)]}


def _rekey_tree(tree):
    tree=copy.deepcopy(tree)
    def visit(node):
        if node['id']!='root':node['id']=uuid.uuid4().hex[:12]
        for child in node.get('children',[]):visit(child)
    visit(tree)
    return tree


class DWOS_OT_mullion_preset_save(Operator):
    bl_idname='dwos.mullion_preset_save';bl_label='Сохранить раскладку импостов';bl_options={'REGISTER'}
    def execute(self,context):
        try:
            obj=_window(context);name=context.scene.dwos.mullion_preset_name.strip()
            if not name or len(name)>70:raise ValueError('Введите название пресета до 70 символов')
            stored=json.loads(context.scene.dwos.mullion_presets_json or '{}')
            if not isinstance(stored,dict):raise ValueError('Повреждены пресеты')
            stored[name]=_preset_data(obj.dwos_window)
            context.scene.dwos.mullion_presets_json=json.dumps(stored,ensure_ascii=False,separators=(',',':'))
        except Exception as exc:self.report({'WARNING'},str(exc));return {'CANCELLED'}
        return {'FINISHED'}


class DWOS_OT_mullion_preset_apply(Operator):
    bl_idname='dwos.mullion_preset_apply';bl_label='Применить раскладку импостов';bl_options={'REGISTER'}
    def invoke(self,context,event):
        return context.window_manager.invoke_confirm(self,event)
    def execute(self,context):
        history_push(context,'MULLION_PRESET_APPLY')
        try:
            obj=_window(context);s=obj.dwos_window
            name=context.scene.dwos.mullion_preset_name.strip()
            p=json.loads(context.scene.dwos.mullion_presets_json or '{}').get(name)
            if not isinstance(p,dict):raise ValueError('Пресет не найден')
            w,h,fw=_dims(s)
            if p.get('kind')=='TREE':
                tree=_rekey_tree(p['tree'])
                layout(tree,w,h,fw)
                staged=('tree',encode(tree))
            elif p.get('kind')=='LEGACY':
                if len(p.get('splits',[]))>128:raise ValueError('Слишком много импостов')
                clean=[]
                for raw in p['splits']:
                    axis=raw['axis'];ratio=float(raw['ratio']);thickness=float(raw['thickness'])
                    if axis not in ('H','V') or not (0<ratio<1) or not math.isfinite(ratio) or not math.isfinite(thickness) or thickness<=0:
                        raise ValueError('Повреждённый пресет')
                    pos=ratio*(w if axis=='V' else h)
                    clean.append({'axis':axis,'position':pos,'thickness':thickness,'id':uuid.uuid4().hex[:8]})
                if clean:
                    legacy_targets(clean,[r['id'] for r in clean],'MOVE',0,width=w,height=h,frame_width=fw)
                staged=('legacy_preset',clean)
            else:raise ValueError('Неподдерживаемый формат пресета')
            old_json=s.zone_tree_json
            old_splits=[(sp.split_id,sp.axis,sp.position_text,sp.thickness_text,sp.override_thickness) for sp in s.splits]
            old_sel=s.mullion_selection_json
            old_groups=s.mullion_groups_json
            old_zone=s.active_zone_id,s.active_zone_split_id,s.active_split_index
            try:
                s.splits.clear()
                s.zone_tree_json=staged[1] if staged[0]=='tree' else ''
                if staged[0]=='legacy_preset':
                    for row in staged[1]:
                        sp=s.splits.add();sp.split_id=row['id'];sp.axis=row['axis']
                        sp.position_text=format_meter(row['position'])
                        sp.thickness_text=format_meter(row['thickness']);sp.override_thickness=True
                s.mullion_selection_json='[]'
                s.mullion_groups_json='{}'
                s.active_zone_id='';s.active_zone_split_id='';s.active_split_index=-1
                rebuild_window(obj)
            except Exception:
                s.splits.clear();s.zone_tree_json=old_json
                for id_,axis,pos,thick,override in old_splits:
                    sp=s.splits.add();sp.split_id=id_;sp.axis=axis
                    sp.position_text=pos;sp.thickness_text=thick;sp.override_thickness=override
                s.mullion_selection_json=old_sel
                s.mullion_groups_json=old_groups
                s.active_zone_id,s.active_zone_split_id,s.active_split_index=old_zone
                try:rebuild_window(obj)
                except Exception as exc:log_exception('PRESET_ROLLBACK_REBUILD_FAILED',exc,window=obj.name)
                raise
            log_event('MULLION_PRESET_APPLIED',name=name,window=obj.name)
        except Exception as exc:
            log_exception('MULLION_PRESET_REJECTED',exc)
            self.report({'WARNING'},str(exc));return {'CANCELLED'}
        return {'FINISHED'}


# Extend the registration tuple without repeating operators.
CLASSES += (DWOS_OT_mullion_preset_save,DWOS_OT_mullion_preset_apply)
