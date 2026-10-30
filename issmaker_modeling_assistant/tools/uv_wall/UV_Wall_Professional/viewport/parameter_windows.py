"""UV Wall 1.11 floating parameter editors.

The document remains authoritative: windows contain independent *drafts* only.
No Blender imports, object creation, geometry mutation or production commits here.
Rendering uses injected 3D View callbacks and hitboxes match rendered pixels.
"""
from dataclasses import dataclass, field
from math import isfinite, sin, tau
from time import perf_counter
from ..ui.numeric_input import parse_value, format_value
from issmaker_modeling_assistant.core.text_input_core import edit as text_edit, caret_from_x, NUMERIC_CHARS
from issmaker_modeling_assistant.core import design_tokens as _iss_style
from issmaker_modeling_assistant.core.workspace_components import component_visual as _workspace_visual
from issmaker_modeling_assistant.core import viewport_skin as _viewport_skin
from issmaker_modeling_assistant.core import window_shell as _shell
from issmaker_modeling_assistant.core.overlay_placement import StrictOverlayAllocator
from issmaker_modeling_assistant.core import window_visibility
from issmaker_modeling_assistant.core import context_workspace as _context_ui
from .adaptive_editor_layout import plan_right_column
from issmaker_modeling_assistant.core import universal_hud as _universal

# (name, localized caption, presentation, choices, visibility dependency)
FIELDS = {
    'HEIGHT': (
        ('height', 'Высота стены от линии пути', 'number', (), None),
    ),
    'THICKNESS': (
        ('inset_distance','Толщина между стенами','number',(),'create_inner'),
        ('create_inner','Конструкция','choice',((False,'Одинарная'),(True,'Двойная')),None),
        ('close_ends','Торцы открытого пути','choice',((False,'Выкл'),(True,'Вкл')),'create_inner'),
        ('close_start_end','Торец · начало','choice',((False,'Выкл'),(True,'Вкл')),'close_ends'),
        ('close_finish_end','Торец · конец','choice',((False,'Выкл'),(True,'Вкл')),'close_ends'),
    ),
    'PLACEMENT': (
        ('wall_alignment','Положение','choice',(('INSIDE','Внутрь'),('CENTER','Центр'),('OUTSIDE','Наружу')),None),
        ('wall_offset','Доп. смещение','number',(),None),
    ),
    # SINGLE has no thickness: direction here is the produced face normal,
    # not a fictitious inset/placement derived from a disabled second wall.
    'NORMALS': (
        ('wall_normal_mode','Направление нормалей','choice',(
            ('OUTWARD','Наружу'),('INWARD','Внутрь'),
            ('PATH_LEFT','Влево от пути'),('PATH_RIGHT','Вправо от пути')),None),
        ('wall_offset','Смещение от пути','number',(),None),
    ),
    'WALL': (
        ('wall_module_rotation','Поворот сетки','number',(),None),
        ('wall_uv_origin_mode','UV по высоте','choice',(('PATH','От пути'),('WORLD','Мировой Z')),None),
        ('module_standard','Размер модуля','choice',(('2','2'),('4','4')),None),
    ),
    'CAP': (
        ('create_top_cap','Верхняя крышка','choice',((False,'Выключена'),(True,'Включена')),None),
        ('cap_material_override','Материал верха','choice',((False,'Наследовать от стены'),(True,'Переопределить')),'create_top_cap'),
        ('cap_material','Материал верха','datablock',(),'cap_material_override'),
        ('create_bottom_cap','Нижняя крышка','choice',((False,'Выключена'),(True,'Включена')),None),
        ('bottom_cap_material_override','Материал низа','choice',((False,'Наследовать от стены'),(True,'Переопределить')),'create_bottom_cap'),
        ('bottom_cap_material','Материал низа','datablock',(),'bottom_cap_material_override'),
        ('cap_rotation','Поворот сетки','number',(),None),
    ),
    'PLANT': (
        ('create_inner_surface','Грунт','choice',((False,'Выключен'),(True,'Включен')),None),
        ('inner_surface_offset_auto','Отступ','choice',((True,'Авто'),(False,'Вручную')),'create_inner_surface'),
        ('inner_surface_offset','Доп. отступ','number',(),'create_inner_surface'),
        ('inner_surface_height_offset','Высота относительно верха','number',(),'create_inner_surface'),
        ('surface_module_rotation','Поворот сетки','number',(),'create_inner_surface'),
        ('inner_surface_material','Материал','datablock',(),'create_inner_surface'),
    ),
    'SURFACE': (
        ('surface_height_offset','Смещение Z','number',(),None),
        ('surface_module_rotation','Поворот сетки','number',(),None),
        ('roof_normal','Нормали','choice',(('UP','Вверх'),('DOWN','Вниз')),None),
        ('module_standard','Размер модуля','choice',(('2','2'),('4','4')),None),
    ),
    'CORNICE_ROT': (
        ('cornice_profile_rotation','Поворот профиля','number',(),None),
    ),
    'CORNICE_SIDE': (
        ('cornice_flip_side','Обратить сторону','choice',((False,'Нет'),(True,'Да')),None),
        ('cornice_profile_flip_x','Отразить профиль X','choice',((False,'Нет'),(True,'Да')),None),
        ('cornice_profile_flip_y','Отразить профиль Z','choice',((False,'Нет'),(True,'Да')),None),
        ('cornice_reverse_path','Порядок пути','choice',((False,'Прямой'),(True,'Обратный')),None),
    ),
    'CORNICE': (
        ('cornice_profile','Профиль','datablock',(),None),
        ('cornice_profile_rotation','Поворот профиля','number',(),None),
        ('cornice_flip_side','Сторона','choice',((False,'Обычная'),(True,'Обратная')),None),
        ('cornice_profile_flip_x','Отразить X','choice',((False,'Нет'),(True,'Да')),None),
        ('cornice_profile_flip_y','Отразить Z','choice',((False,'Нет'),(True,'Да')),None),
        ('cornice_reverse_path','Путь','choice',((False,'Прямой'),(True,'Обратный')),None),
    ),
}
TITLES = {'HEIGHT':'Высота стены','THICKNESS':'Конструкция стены','PLACEMENT':'Положение стены','NORMALS':'Нормали одинарной стены','WALL':'Параметры стены',
          'CAP':'Крышки и торцы','PLANT':'Внутренняя поверхность','SURFACE':'Поверхность',
          'CORNICE':'Карниз','CORNICE_ROT':'Поворот профиля','CORNICE_SIDE':'Сторона и отражение'}


LENGTH_FIELDS = {
    'height','inset_distance','wall_offset','inner_surface_offset',
    'inner_surface_height_offset','surface_height_offset',
}
ANGLE_FIELDS = {'wall_module_rotation','cap_rotation','surface_module_rotation','cornice_profile_rotation','cornice_smooth_warning_angle'}


def display_number(name, value):
    text = format_value(value)
    if name in ANGLE_FIELDS:
        return text + "°"
    if name in LENGTH_FIELDS:
        return text + " m"
    return text


def _draft_default(name):
    defaults = {
        'wall_alignment': 'LEGACY',
        'cornice_uv_mode': 'MAX_REAL_WORLD',
        'cornice_smooth_warning_angle': 45.0,
        'close_ends': True,
        'close_start_end': True,
        'close_finish_end': True,
    }
    return defaults.get(name)


def draft_from_definition(kind, definition, wall_height):
    return {name: (wall_height if name=='height' else
                   getattr(definition,name).name if name in ('inner_surface_material','cornice_profile','cap_material','bottom_cap_material') and getattr(definition,name,None) else
                   '' if name in ('inner_surface_material','cornice_profile','cap_material','bottom_cap_material') else getattr(definition,name,_draft_default(name)))
            for name,*_ in FIELDS[kind]}


def _dependency_visible(dependency, draft):
    if not dependency:
        return True
    if ':' in dependency:
        field, expected = dependency.split(':', 1)
        return str(draft.get(field, '')) == expected
    return bool(draft.get(dependency))


def visible_fields(kind,draft,path_closed=False):
    cap_fields={'close_ends','close_start_end','close_finish_end'}
    return [f for f in FIELDS[kind]
            if _dependency_visible(f[4],draft)
            and not (f[0] in cap_fields and (path_closed or not draft.get('create_inner',False)))
            # A dependent material must be hidden even if its override flag
            # stays True while the corresponding cap has been switched off.
            and not (kind=='CAP' and f[0]=='cap_material' and not draft.get('create_top_cap',False))
            and not (kind=='CAP' and f[0]=='bottom_cap_material' and not draft.get('create_bottom_cap',False))
            and not (kind=='CAP' and f[0]=='cap_rotation' and not
                     (draft.get('create_top_cap',False) or draft.get('create_bottom_cap',False)))]


def validated_draft(kind,draft):
    """Validate/normalize on Apply, never while opening a dialog."""
    values=dict(draft)
    for name,_,typ,choices,_ in FIELDS[kind]:
        if typ=='number':
            v=float(values[name])
            if not isfinite(v):raise ValueError('Значение должно быть конечным числом')
            if name in ('height','inset_distance') and v<=0:raise ValueError('Размер должен быть больше нуля')
            if name.endswith('rotation') and not -180<=v<=180:raise ValueError('Допустимый поворот: -180…180')
            if name=='cornice_smooth_warning_angle' and not 1<=v<=179:raise ValueError('Порог острого угла: 1…179°')
        elif typ=='choice' and values[name] not in [x for x,_ in choices]:
            raise ValueError('Недопустимое значение параметра: '+name)
    if kind=='PLANT' and values['create_inner_surface']:
        values['create_inner']=True
    if kind=='THICKNESS' and not values['create_inner']:
        values['create_inner_surface']=False
    return values


def changed_patch(kind, draft, baseline, current):
    """Three-way editor merge, not a replay of an obsolete entire Definition.

    A closed or untouched editor has no writes.  A concurrent edit to the SAME
    field raises a conflict before the Blender/Undo transaction begins.
    Hidden fields are kept as drafts, but cannot spontaneously roll back changes
    from a second editor.  Explicit mode changes are the only derived writes.
    """
    dirty = [key for key in draft if draft[key] != baseline[key]]
    if not dirty:
        return {}
    # A stored legacy enum is read-only until the user actually changes it.
    # Validate changed values while normalising untouched historic values in a
    # temporary copy; never commit those normalisations to the document.
    candidate=dict(draft)
    for name, _, typ, choices, _ in FIELDS[kind]:
        if name not in dirty and typ=='choice' and candidate[name] not in [v for v,_ in choices]:
            candidate[name]=choices[0][0]
    validated = validated_draft(kind, candidate)
    changes = {key: validated[key] for key in dirty}
    for name, target in changes.items():
        live = current.get(name)
        if live != baseline[name] and live != target:
            raise ValueError('Параметр изменён в другом окне: ' +
                             next((label for key,label,*_ in FIELDS[kind] if key==name),name))
    if kind == 'PLANT' and changes.get('create_inner_surface') is True:
        changes['create_inner'] = True
    if kind == 'THICKNESS' and changes.get('create_inner') is False:
        changes['create_inner_surface'] = False
    # A second editor may already have committed the exact proposed value.
    # Re-applying it would create a misleading extra Blender Undo/build step.
    return {key: value for key,value in changes.items()
            if current.get(key, object()) != value}


@dataclass
class Editor:
    kind:str
    uid:str
    draft:dict
    x:float
    y:float
    baseline:dict=field(default_factory=dict)
    width:float=318
    error:str=''
    hover:tuple=('',None)
    edit_field:str=''
    edit_buffer:str=''
    caret:int=0
    select_all:bool=False
    hitboxes:list=field(default_factory=list)
    dragging:bool=False
    pinned:bool=False
    collapsed:bool=False
    dock_side:str='RIGHT'
    scroll:int=0
    choice_field:str=''
    list_field:str=''  # exact calling field for LIST/PICK; never inferred from override flags
    choice_anchor:tuple=(0,0)
    region_height:float=720
    drag_origin:tuple=(0,0)
    start_pos:tuple=(0,0)
    ui_scale:float=1.0
    path_closed:bool=False
    hidden:bool=False         # explicit user hide: draft and editor remain alive
    layout_hidden:bool=False  # no viewport space: preserve draft, disable hitboxes
    auto_compact:bool=False   # viewport-only presentation, does NOT mutate user choice
    detached:bool=False       # user dragged/unpinned from an automatic group
    grouped:bool=False
    dynamic_height_cap:float=0.0  # visual viewport height budget; safe scroll

    # NEXTGEN 1.7.0: all windows share one measured vertical rhythm.
    # A resource editor reserves actions below the scrolling field surface.
    @property
    def resource_actions(self):
        return _context_ui.resource_actions(self.kind,self.draft)

    @property
    def compact_rows(self):
        return _context_ui.compact_input_rows(self.width,self.ui_scale)

    @property
    def extra_height(self):
        return (42 if self.resource_actions else 0)*self.ui_scale

    @property
    def footer_height(self):
        # Critical resource actions stay available on short Blender regions.
        # DWOS-density footer: short status + compact action row.  Resource
        # pickers reserve their own band and never overlap action buttons.
        return 66*self.ui_scale+self.extra_height

    @property
    def row_height(self):
        # DWOS-like label/field pairs need less vertical space. Tall vertical
        # rows remain the fallback on narrow/superscaled regions.
        if self.compact_rows:return 32*self.ui_scale
        return (44 if self.region_height < 430*self.ui_scale else 48)*self.ui_scale

    @property
    def height(self):
        if self.collapsed or self.auto_compact:
            return 35*self.ui_scale
        natural=(35+10)*self.ui_scale+len(visible_fields(self.kind,self.draft,self.path_closed))*self.row_height+self.footer_height
        natural=min(natural,max(35*self.ui_scale,self.region_height-12*self.ui_scale))
        return min(natural,self.dynamic_height_cap) if self.dynamic_height_cap>0 else natural

    @property
    def dirty_count(self):
        return sum(self.draft[key]!=self.baseline[key] for key in self.draft)

    @property
    def row_capacity(self):
        return max(0,int((self.height-45*self.ui_scale-self.footer_height)//self.row_height))

    def rect(self):
        return self.x,self.y,self.x+self.width,self.y+self.height

    def contains(self,x,y):
        a,b,c,d=self.rect();return a<=x<=c and b<=y<=d


class EditorStack:
    """Ordered independent windows for one source Path UID."""
    def __init__(self):
        self.windows=[]
        self.uid=''
        self.region_size=(1000,700)
        self.visibility_scope=None
        self.group_rect=None
        self.group_count=0
        self.group_hitboxes=[]
        self.group_hover=''
        self.group_hidden=()
        self.close_all_armed=False
        self.last_drawn_region=None
        self.last_focus_status="OFF"
        self.workspace_anchor=None  # loaded once from persistent global preferences
        self.workspace_dragging=False
        self.workspace_drag_origin=None
        self.workspace_drag_rect=None
        self.workspace_drag_saved=None
        self._last_scale=1.0

    def clear(self):
        if self.visibility_scope:
            for w in self.windows:
                window_visibility.forget(self.visibility_scope,window_visibility.editor_key(w.kind))
        self.windows.clear();self.uid=''
        self.group_rect=None;self.group_hitboxes=[];self.group_hidden=()
        self.close_all_armed=False

    def set_source(self,uid):
        if self.uid and self.uid!=uid:self.clear()
        self.uid=uid

    def editor(self,kind):
        return next((w for w in self.windows if w.kind==kind),None)

    @staticmethod
    def _intersection_area(a,b):
        return max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))

    def _place(self,w,h,hud):
        rw,rh=self.region_size
        manager=StrictOverlayAllocator(rw,rh,margin=7.,gap=7.)
        manager.reserve('MODE_HUD',hud)
        for i,other in enumerate(self.windows):
            if not other.layout_hidden:
                manager.reserve(f'EDITOR:{i}',other.rect())
        place=manager.place('EDITOR_NEW',(w,h),(hud[2]+10,hud[3]-h))
        return (place[0],place[1]) if place is not None else None

    def open(self,kind,uid,definition,height,hud,region,*,path_closed=False):
        self.set_source(uid)
        self.region_size=region
        if kind not in FIELDS:raise ValueError(kind)
        old=self.editor(kind)
        if old:
            old.hidden=False
            if self.visibility_scope:
                window_visibility.set_hidden(self.visibility_scope,window_visibility.editor_key(kind),False)
            old.path_closed=bool(path_closed)
            self.windows.remove(old);self.windows.append(old)
            return old
        draft=draft_from_definition(kind,definition,height)
        win=Editor(kind,uid,draft,0,0,baseline=draft.copy(),path_closed=bool(path_closed))
        win.width=min(324,max(190,region[0]-16))
        win.region_height=region[1]
        point=self._place(win.width,win.height,hud)
        if point is None:
            win.layout_hidden=True
            win.x=8.;win.y=8.
        else:
            win.x,win.y=point
        self.windows.append(win)
        return win

    def refresh(self, definition, height):
        """Rebase clean fields after sibling Apply/Undo without losing user edits."""
        for w in self.windows:
            fresh=draft_from_definition(w.kind,definition,height)
            for key, value in fresh.items():
                if key != w.edit_field and w.draft[key] == w.baseline[key]:
                    w.draft[key] = value
                    w.baseline[key] = value

    def accept(self, win, definition, height):
        """Successful Apply accepts the authoritative Definition for this window."""
        fresh=draft_from_definition(win.kind,definition,height)
        win.draft=fresh
        win.baseline=fresh.copy()
        win.edit_field=''
        win.edit_buffer=''
        win.error=''
        self.refresh(definition,height)

    def close(self,kind):
        old=self.editor(kind)
        if old:
            self.windows.remove(old)
            if self.visibility_scope:
                window_visibility.forget(self.visibility_scope,window_visibility.editor_key(kind))

    def clamp(self,region,obstacles=(),ui_scale=1.0,focus_areas=()):
        """Reflow against a complete single-frame obstacle list.

        Two compatible open windows become one right-anchored column. User
        dragging detaches a window; returning to PIN reattaches it. When the
        region is too small, the legacy strict layout is the safe fallback.
        No failed/hidden window retains live mouse hitboxes.
        """
        self.region_size=region
        rw,rh=region
        self._last_scale=ui_scale
        try: scale=max(.75,min(2.,float(ui_scale or 1.)))
        except (TypeError,ValueError,OverflowError):scale=1.
        self.group_rect=None
        self.group_count=0
        self.group_hitboxes=[]
        self.group_hidden=()
        self.last_focus_status="OFF"
        for w in self.windows:
            w.region_height=rh
            w.ui_scale=scale
            w.grouped=False
            w.auto_compact=False
            w.dynamic_height_cap=0.0
            preferred=(324 if rw>=720 else 292)*scale
            w.width=min(preferred,max(190*scale,rw-16*scale))
        manager=StrictOverlayAllocator(rw,rh,margin=6.,gap=7.)
        for i,r in enumerate(obstacles):
            manager.reserve('EXTERNAL:'+str(i),r)
        def place_aware(key,size,preferred):
            # Model projections are preferences; existing UI windows are hard
            # blockers. Reserve on the authoritative manager exactly once.
            if focus_areas:
                trial=StrictOverlayAllocator(rw,rh,margin=6.,gap=7.)
                for reserved in manager.surfaces:
                    trial.reserve(reserved.key,reserved.bounds)
                for index,area in enumerate(focus_areas):
                    trial.reserve('MODEL_FOCUS:'+str(index),area)
                found=trial.place(key,size,preferred)
                if found is not None:
                    manager.reserve(key,found)
                    return found
            return manager.place(key,size,preferred)
        available=[]
        hidden=[]
        for w in self.windows:
            key=window_visibility.editor_key(w.kind)
            # External Window Manager (F3 / HUD list) is authoritative when
            # scoped. Otherwise a restored panel could remain stuck hidden
            # because the local flag was never cleared.
            if self.visibility_scope:
                w.hidden=window_visibility.is_hidden(self.visibility_scope,key)
            if w.hidden:
                w.hidden=True
                if self.visibility_scope:
                    window_visibility.observe(self.visibility_scope,key,None)
                w.layout_hidden=True
                w.hitboxes.clear();w.dragging=False
                hidden.append(_universal.panel_descriptor(w.kind,TITLES[w.kind]))
                continue
            available.append(w)
        self.group_hidden=tuple(hidden)
        # Universal Workspace: old positions, PIN and drag history never cause
        # detached editors. Only an explicit future 'undock' operation could.
        for w in available:
            w.detached=False
            w.dock_side='RIGHT'
            w.dragging=False
        if available:
            hh=_universal.group_header_height(scale,len(hidden)) / scale
            plan=plan_right_column(region,
                 [(w.kind,w.height,w.collapsed) for w in available],
                 manager.snapshot_obstacles() if hasattr(manager,'snapshot_obstacles') else
                 [surf.bounds for surf in manager.surfaces],scale=scale,
                 header_height=hh,focus_areas=focus_areas,anchor=self.workspace_anchor)
            if plan is not None:
                self.group_rect=plan.rect
                self.group_count=len(available)
                manager.reserve('UNIVERSAL_WORKSPACE',plan.rect)
                if focus_areas:
                    from issmaker_modeling_assistant.core.overlay_placement import overlap_area
                    self.last_focus_status=('AVOIDED' if all(overlap_area(plan.rect,r)==0 for r in focus_areas)
                                            else 'FALLBACK_MODEL_OVERLAP')
                by_kind={w.kind:w for w in available}
                for kind,rect,compact in plan.editors:
                    w=by_kind[kind]
                    w.grouped=True;w.auto_compact=compact;w.width=rect[2]-rect[0]
                    w.dynamic_height_cap=rect[3]-rect[1]
                    w.x,w.y=rect[:2]
                    w.layout_hidden=False
                    if self.visibility_scope:
                        observed=window_visibility.observe(self.visibility_scope,
                            window_visibility.editor_key(w.kind),rect)
                        if observed is None:
                            w.hidden=True;w.layout_hidden=True;w.hitboxes.clear()
            else:
                # A single compact, recoverable group must be preferred to
                # multiple floating editors. If room truly fails, no stale
                # click rectangle is allowed to survive.
                for w in available:
                    w.layout_hidden=True;w.hitboxes.clear()
                    if self.visibility_scope:
                        window_visibility.observe(self.visibility_scope,
                            window_visibility.editor_key(w.kind),None)
        elif self.windows:
            # Even hiding *all* editors leaves a right-side restoration rail.
            rail_h=_universal.group_header_height(scale,len(hidden))
            rail_w=min(370*scale,max(225*scale,rw-16*scale))
            rail=manager.place('UNIVERSAL_RESTORE_RAIL',(rail_w,rail_h),
                               (rw-rail_w-10*scale,rh-rail_h-55*scale))
            self.group_rect=rail
            self.group_count=0
        if self.group_rect is not None:
            self.group_hitboxes=list(_universal.group_control_rects(
                self.group_rect,scale,self.group_hidden))
        else:
            self.group_hitboxes=[]
        for w in self.windows:
            w.scroll=max(0,min(w.scroll,
                 max(0,len(visible_fields(w.kind,w.draft,w.path_closed))-w.row_capacity)))
        return manager.snapshot()

    def dismiss_on_canvas(self):
        """The universal workspace persists until explicitly closed.

        Canvas clicks may dismiss a dropdown, but never silently remove
        editors or their unsaved drafts from the right-hand workspace.
        """
        changed=False
        for win in self.windows:
            if win.choice_field:
                win.choice_field='';changed=True
        return changed

    def top_at(self,x,y):
        for w in reversed(self.windows):
            if self.visibility_scope and window_visibility.is_hidden(self.visibility_scope,window_visibility.editor_key(w.kind)):
                w.hitboxes.clear()
                continue
            if not w.layout_hidden and not w.hidden and w.contains(x,y):return w
        return None

    def draw(self,region,rect,text,visual_effect='GRAPHITE',obstacles=(),ui_scale=1.0,
             scope=None,focus_areas=(),soft_rect=None,stroke_line=None):
        """Paint deliberately measured, fully interactive UI rectangles.

        All coordinates are region-local and derived once, and every click
        target comes from the exact same rectangle as its visual counterpart.
        The field list scrolls; the actions never scroll out of view.
        """
        self.visibility_scope=scope
        if self.workspace_anchor is None:
            try:
                import bpy
                addon=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
                prefs=addon.preferences if addon else None
                if prefs is not None and prefs.ui_wall_workspace_user_moved:
                    self.workspace_anchor=(float(prefs.ui_wall_workspace_pos_x),
                                           float(prefs.ui_wall_workspace_pos_y))
            except (ImportError,AttributeError,RuntimeError,ReferenceError,TypeError,ValueError):pass
        self.clamp(region,obstacles,ui_scale,focus_areas)
        self.last_drawn_region=tuple(region)
        # Real rounded GPU surfaces, matching DWOS.  Only paint is rounded;
        # original rectangles remain the one authoritative hitbox geometry.
        panel = soft_rect if soft_rect is not None else rect
        palettes={
            'GRAPHITE': (_iss_style.PANEL_BG[:3]+(.975,),_iss_style.mix_color(_iss_style.PANEL_BORDER,_iss_style.MINT,.27)[:3]+(.98,),_iss_style.MINT),
            'AURORA': ((.025,.044,.052,.995),(.19,.35,.32,1),(.33,.94,.76,1)),
            'TOPO': ((.035,.048,.050,.995),(.19,.29,.27,1),(.40,.82,.70,1)),
            'BLUEPRINT': ((.029,.044,.062,.995),(.24,.38,.52,1),(.46,.78,.99,1)),
            'CLEAN': ((.044,.050,.054,.995),(.22,.26,.28,1),(.51,.85,.75,1)),
        }
        body,border,accent=palettes.get((visual_effect or 'GRAPHITE').upper(),palettes['GRAPHITE'])
        shared_theme=None
        try:
            import bpy
            from issmaker_modeling_assistant.core.theme_engine import from_preferences
            addon=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
            shared_theme=from_preferences(addon.preferences if addon else None)
            shared=shared_theme.uv_palette()
            shell_colors=_shell.surface(shared_theme)
            body,border,accent=shell_colors['panel'],shell_colors['border'],shell_colors['accent']
        except (ImportError,AttributeError,RuntimeError,ReferenceError,KeyError,TypeError):
            pass  # never prevent editing due to a decorative theme failure
        ink=_viewport_skin.ink(shared_theme)
        muted=_viewport_skin.ink(shared_theme,kind='muted')
        if self.group_rect is not None:
            gx0,gy0,gx1,gy1=self.group_rect
            panel(gx0-4,gy0-4,gx1+4,gy1+4,(.004,.009,.011,.5))
            panel(gx0,gy0,gx1,gy1,body,border)
            if shared_theme is not None:
                from issmaker_modeling_assistant.core.studio_gpu_components import shell_background
                shell_background(shared_theme,(gx0,gy0,gx1,gy1))
            panel(gx0,gy1-_shell.spec('group',scale=ui_scale).header,gx1,gy1,
                 _shell.surface(shared_theme)['header'])
            rect(gx0+3,gy1-3,gx1-3,gy1,accent)
            label=('Подтвердите закрытие' if self.close_all_armed
                   else 'UV WALL / WORKSPACE')
            text(gx0+12,gy1-21,label,_viewport_skin.text_px('caption'),
                 (.98,.70,.55,1) if self.close_all_armed else _viewport_skin.ink(shared_theme))
            for action,hint,box in self.group_hitboxes:
                if action=='GROUP_DRAG':
                    # Two subtle grip lines, instead of a clickable opaque bar.
                    mx=box[0]+5*ui_scale; my=(box[1]+box[3])*.5
                    rect(mx,my-4*ui_scale,mx+13*ui_scale,my-3*ui_scale,muted)
                    rect(mx,my+3*ui_scale,mx+13*ui_scale,my+4*ui_scale,muted)
                    continue
                selected=self.group_hover==action
                visual=_workspace_visual('button','hover' if selected else 'normal',
                                         'danger' if action=='GROUP_CLOSE_ALL' else 'neutral',shared_theme)
                from issmaker_modeling_assistant.core.studio_gpu_components import draw_component
                state='hover' if selected else 'normal'
                role='danger' if action=='GROUP_CLOSE_ALL' else 'neutral'
                if not draw_component(box,part='button',state=state,role=role,
                                      theme=shared_theme,scale=ui_scale):
                    panel(*box,visual.background,visual.border)
                if action.startswith('GROUP_RESTORE:'):
                    kind=action.split(':',1)[1]
                    glyph=_universal.panel_descriptor(kind,TITLES[kind]).icon
                    # Draw actual diagonals. The previous horizontal/vertical
                    # approximation made wall/cornice icons disappear.
                    strokes=_universal.icon_segments(glyph,box)
                    if stroke_line is not None:
                        for a,b in strokes:stroke_line(a,b,accent,max(1.,1.7*ui_scale))
                    else:
                        for a,b in strokes:
                            text((a[0]+b[0])*.5,(a[1]+b[1])*.5,'·',8,accent,'CENTER')
                else:
                    glyph={'GROUP_HIDE_ALL':'hide','GROUP_COLLAPSE_ALL':'collapse',
                           'GROUP_CLOSE_ALL':'close'}.get(action,'generic')
                    if stroke_line is not None:
                        for a,b in _universal.icon_segments(glyph,box):
                            stroke_line(a,b,visual.foreground,max(1.,1.7*ui_scale))
                    else:
                        symbol={'GROUP_HIDE_ALL':'H','GROUP_COLLAPSE_ALL':'-',
                                'GROUP_CLOSE_ALL':'X'}.get(action,'?')
                        text((box[0]+box[2])/2,box[1]+6*ui_scale,
                             symbol,12,visual.foreground,'CENTER')
            if not self.group_count:
                text(gx0+12,gy0+8*ui_scale,'Скрыто · выберите иконку для восстановления',9,muted)
        ink=_viewport_skin.ink(shared_theme)
        muted=_viewport_skin.ink(shared_theme,kind='muted')
        for w in self.windows:
            w.hitboxes=[]
            if w.layout_hidden:
                continue
            x,y,width,height=w.x,w.y,w.width,w.height
            top=y+height
            is_front=(w is self.windows[-1])
            if not w.grouped:
                panel(x-4,y-5,x+width+4,top+3,(.005,.010,.014,.65))
            panel(x,y,x+width,top,body,border)
            if shared_theme is not None and not w.grouped:
                from issmaker_modeling_assistant.core.studio_gpu_components import shell_background
                shell_background(shared_theme,(x,y,x+width,top))
            shell_zones=_shell.zones((x,y,x+width,top),kind='editor',scale=w.ui_scale,
                                      footer=0. if (w.collapsed or w.auto_compact) else w.footer_height,
                                      reserved_right=125*w.ui_scale)
            shell_top=shell_zones.header[1] if shell_zones else top-35*w.ui_scale
            panel(x,shell_top,x+width,top,_shell.surface(shared_theme)['header'])
            rect(x,top-35,x+2.5,top,accent)
            if is_front:
                effect=(visual_effect or 'GRAPHITE').upper()
                if effect=='AURORA':
                    pulse=.72+.16*sin(perf_counter()*tau*.45)
                    rect(x+3,top-2,x+width-2,top,(accent[0],accent[1],accent[2],pulse))
                else:
                    rect(x+8,top-2,x+width-8,top,(accent[0],accent[1],accent[2],.58))
                # Decorative patterns are rendered by the shared Theme Engine.
                # The legacy striped overdraw looked like broken pixels on
                # translucent windows and conflicted with DWOS panel materials.
            def hit(action,name,bounds):
                w.hitboxes.append((action,name,bounds))
            def button(action,caption,bounds,bg,fg=ink,edge=None,name=None):
                # Legacy call sites still pass semantic backgrounds, but all
                # controls now obtain their surface from the same HUD contract.
                role=('primary' if action in {'APPLY','LIST'} else
                      'danger' if action=='CLOSE' else 'neutral')
                selected=(action=='PIN' and w.pinned)
                state='active' if selected else ('hover' if w.hover[0]==action else 'normal')
                style=_viewport_skin.component('button',state,role,shared_theme)
                from issmaker_modeling_assistant.core.studio_gpu_components import draw_component
                if not draw_component(bounds,part='button',state=state,role=role,
                                      theme=shared_theme,scale=w.ui_scale):
                    panel(*bounds,style.background,style.border)
                button_ink=_viewport_skin.control_ink(shared_theme,part='button',state=state,role=role)
                a,b,c,d=bounds
                glyph={'HIDE':'hide','COLLAPSE':'collapse','PIN':'pin','CLOSE':'close'}.get(action)
                if glyph and stroke_line is not None:
                    for start,end in _universal.icon_segments(glyph,bounds):
                        stroke_line(start,end,button_ink,max(1.,1.6*w.ui_scale))
                    hit(action,name,bounds)
                    return
                visible=shorten(str(caption),max(0.,c-a-16),10)
                if visible:
                    text((a+c)/2,b+max(4,(d-b-11)/2),visible,10,button_ink,'CENTER')
                hit(action,name,bounds)
            def shorten(value,available,font=10):
                value=str(value)
                if available<=0:return ''
                # Geometry and rendering use the same callback. Width depends
                # on actual font metrics, not raw Cyrillic character counts.
                try:
                    import blf
                    from issmaker_modeling_assistant.core.ui_layout_scaling import from_preferences
                    import bpy
                    addon=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
                    style=from_preferences(addon.preferences if addon else None)
                    blf.size(0,style.font_px(font))
                    measure=lambda v: blf.dimensions(0,v)[0]
                except (ImportError,AttributeError,RuntimeError,TypeError):
                    measure=lambda v:len(v)*font*.72
                return _iss_style.fit_label(value,available,measure)[0]
            # Header: title never occupies the action area.
            heading=('Расстояние между стенами' if w.kind=='THICKNESS' and w.draft.get('create_inner')
                     else TITLES[w.kind])
            # Titles are fitted before they enter the shared header; actions
            # retain the exact historical hit targets.
            text(x+12,top-23,shorten('UV WALL / '+heading.upper(),width-152,_viewport_skin.text_px('control')),
                 _viewport_skin.text_px('control'),_viewport_skin.ink(shared_theme,kind='accent'))
            if w.dirty_count and width>290:
                text(x+width-105,top-39,f'{w.dirty_count} измен.',8,accent,'RIGHT')
            button('HIDE','◉',(x+width-122,top-29,x+width-98,top-7),(.065,.105,.112,1))
            button('COLLAPSE','▸' if (w.collapsed or w.auto_compact) else '−',
                   (x+width-92,top-29,x+width-68,top-7),(.094,.130,.133,1))
            button('PIN','◆' if (w.pinned or w.grouped) else '◇',
                   (x+width-62,top-29,x+width-38,top-7),(.070,.21,.175,1) if w.pinned else (.094,.13,.133,1))
            button('CLOSE','×',(x+width-32,top-29,x+width-8,top-7),(.16,.13,.14,1))
            hit('DRAG',None,(x,shell_top,x+width-128,top))
            if w.collapsed or w.auto_compact:
                continue
            # Footer has three explicit stages: select resource, inspect state,
            # then apply / revert / close. This is fixed while rows scroll.
            footer=w.footer_height
            fields=visible_fields(w.kind,w.draft,w.path_closed)
            count=w.row_capacity
            # Keep a predictable space for status and action buttons.
            field_bottom=y+footer+3
            if count==0:
                text(x+16,top-59,shorten('Расширьте 3D-вид для параметров',width-32,10),10,(.97,.69,.42,1))
            elif fields:
                if w.scroll:
                    text(x+width-18,top-53,'↑',10,accent,'RIGHT')
                if w.scroll+count<len(fields):
                    text(x+width-18,field_bottom+3,'↓',10,accent,'RIGHT')
                pos=top-53
                for name,caption,typ,choices,_ in fields[w.scroll:w.scroll+count]:
                    value=w.draft[name]
                    active=w.edit_field==name
                    hovered=w.hover==('FIELD',name)
                    # Two-column compact presentation matches DWOS Composer:
                    # one label + one number/select per row, no stacked blank
                    # space. In narrow viewports retain the original layout.
                    if w.compact_rows:
                        left=x+16
                        start=x+width*.46
                        text(left,pos-14,shorten(caption,max(0.,start-left-12),10),10,muted)
                        box=(start,pos-24,x+width-14,pos+3)
                    else:
                        text(x+16,pos,shorten(caption,width-36,10),10,muted)
                        box=(x+14,pos-36,x+width-14,pos-8)
                    field_state='focused' if active else ('hover' if hovered else 'normal')
                    field_style=_viewport_skin.component('field',field_state,theme=shared_theme)
                    from issmaker_modeling_assistant.core.studio_gpu_components import draw_component
                    if not draw_component(box,part='field',state=field_state,
                                          theme=shared_theme,scale=w.ui_scale):
                        panel(*box,field_style.background,field_style.border)
                    if typ=='choice':
                        label=next((desc for code,desc in choices if code==value),str(value))
                        label+='  ▾'
                    elif typ=='number':
                        label=w.edit_buffer if active else display_number(name,value)
                    else:
                        label=w.edit_buffer if active else (str(value) if value else 'Не выбран · выбрать ресурс')
                    if active:
                        caret=max(0,min(len(w.edit_buffer),w.caret))
                        label=w.edit_buffer[:caret]+'│'+w.edit_buffer[caret:]
                    field_ink=_viewport_skin.control_ink(shared_theme,part='field',state=field_state)
                    text(box[0]+9,box[1]+7,shorten(label,max(0.,box[2]-box[0]-22),11),11,field_ink)
                    if w.draft.get(name)!=w.baseline.get(name):
                        rect(box[2]-5,box[1]+5,box[2]-3,box[3]-5,accent)
                    hit('FIELD',name,box)
                    pos-=w.row_height
            if w.resource_actions:
                split=x+width/2
                by=y+57*w.ui_scale
                top_y=y+84*w.ui_scale
                if w.kind == 'CAP':
                    # Disabled caps do not consume screen height or display a
                    # useless material button. One enabled cap gets half-row
                    # actions; two enabled caps get three actions.
                    count=1+len(w.resource_actions)
                    gap=6.
                    bw=(width-28-gap*(count-1))/count
                    button('PICK','Выдел.',(x+14,by,x+14+bw,top_y),(.069,.106,.113,1))
                    for i,(label,field_name) in enumerate(w.resource_actions,1):
                        bx=x+14+i*(bw+gap)
                        button('LIST',label,(bx,by,bx+bw,top_y),(.055,.19,.154,1),name=field_name)
                else:
                    button('PICK','Из выделенного' if width>=276 else 'Выделенное',
                           (x+14,by,split-4,top_y),(.069,.106,.113,1))
                    button('LIST','Список…',(split+4,by,x+width-14,top_y),(.055,.19,.154,1))
            # Full meaning of compact action buttons remains accessible on hover.
            # Never let a tooltip hide a validation error or pending edits.
            action_hints={
                'RESET':'Сбросить изменения текущего окна',
                'CANCEL':'Отменить изменения без закрытия модели',
                'APPLY':'Применить параметры к выбранному пути',
                'PICK':'Взять ресурс из текущего выделения',
                'LIST':'Открыть список ресурсов',
                'PIN':'Защитить раздел от автоматического сворачивания',
                'HIDE':'Скрыть редактор до восстановления через его иконку',
                'COLLAPSE':'Свернуть или развернуть настройки',
                'CLOSE':'Закрыть настройки без подтверждения',
            }
            status=w.error or ('Есть неприменённые изменения' if w.dirty_count else
                               action_hints.get(w.hover[0],'Все изменения применены'))
            status_color=(1.,.63,.49,1) if w.error else (_viewport_skin.ink(shared_theme,kind='accent') if w.dirty_count else muted)
            text(x+12,y+42*w.ui_scale,shorten(status,width-24,9),9,status_color)
            # Revert is not equivalent to closing: it keeps the editor ready.
            footer_buttons=_shell.action_bar((x+12,y+9*w.ui_scale,x+width-12,y+33*w.ui_scale),3,
                                              scale=w.ui_scale,min_width=42.,weights=(.8,1.,1.25))
            if footer_buttons:
                button('RESET','Сброс',footer_buttons[0],(.091,.105,.112,1))
                button('CANCEL','Отмена',footer_buttons[1],(.106,.119,.128,1))
                button('APPLY','Применить',footer_buttons[2],(.061,.27,.215,1),(.95,1.,.97,1),accent)
            # Drop-down is rendered last and catches input ahead of underlying
            # fields. The anchor is recalculated from currently visible rows.
            if w.choice_field:
                entry=next((f for f in fields if f[0]==w.choice_field and f[2]=='choice'),None)
                indices=[f[0] for f in fields[w.scroll:w.scroll+count]]
                if entry is None or w.choice_field not in indices:
                    w.choice_field=''
                else:
                    row_index=indices.index(w.choice_field)
                    row_top=top-53-row_index*w.row_height
                    options=entry[3]
                    # A choice list cannot spill onto the next joined
                    # editor, the persistent action footer, or Blender chrome.
                    # Show a bounded window of options; arrow keys still cycle
                    # the full selection through the existing event handler.
                    oh=30
                    available=top-48-(y+footer+7)
                    count_visible=max(0,int((available-8)//oh))
                    if count_visible<1:
                        w.choice_field=''
                        w.error='Расширьте редактор, чтобы открыть список'
                    else:
                        selected=next((i for i,option in enumerate(options)
                            if option[0]==w.draft[entry[0]]),0)
                        start=max(0,min(selected-count_visible//2,len(options)-count_visible))
                        displayed=options[start:start+count_visible]
                        popup_height=8+len(displayed)*oh
                        popup_y=top-48-popup_height
                        px0=x+14;px1=x+width-14
                        rect(px0-2,popup_y-3,px1+2,popup_y+popup_height+3,(.009,.016,.020,.96))
                        popup_style=_viewport_skin.component('card',theme=shared_theme)
                        panel(px0,popup_y,px1,popup_y+popup_height,popup_style.background,popup_style.border)
                        from issmaker_modeling_assistant.core.studio_gpu_components import draw_component
                        for i,(code,label) in enumerate(displayed):
                            by=popup_y+popup_height-5-(i+1)*oh
                            is_sel=(w.draft[entry[0]]==code)
                            area=(px0+3,by,px1-3,by+oh-2)
                            state='active' if is_sel else 'normal'
                            row_style=_viewport_skin.component('button',state,theme=shared_theme)
                            if not draw_component(area,part='button',state=state,theme=shared_theme,scale=w.ui_scale):
                                panel(*area,row_style.background,row_style.border)
                            text(px0+13,by+9,shorten(('✓  ' if is_sel else '    ')+label,px1-px0-35,10),10,
                                 _viewport_skin.control_ink(shared_theme,part='button',state=state))
                            hit('CHOICE',code,area)

    def _commit_field(self,w):
        if not w.edit_field:return True
        name=w.edit_field; buf=w.edit_buffer.strip()
        typ=next(f[2] for f in FIELDS[w.kind] if f[0]==name)
        try:
            if typ=='number':
                v=parse_value(buf,angle=name.endswith('rotation'))
                if not isfinite(v):raise ValueError('Введите конечное число')
            else:v=buf
            w.draft[name]=v
        except ValueError as exc:
            w.error=str(exc);return False
        w.error='';w.edit_field='';w.edit_buffer='';w.caret=0;w.select_all=False
        return True

    def _field_click(self,w,name,click_x=None,rect=None,measure_text=None):
        if w.edit_field==name:
            if measure_text is not None and rect is not None and click_x is not None:
                w.caret=caret_from_x(w.edit_buffer,click_x,rect[0]+9,measure_text)
            w.select_all=False
            return ('CONSUMED',w)
        if not self._commit_field(w):return ('CONSUMED',w)
        field=next(f for f in FIELDS[w.kind] if f[0]==name)
        if field[2]=='choice':
            w.choice_field='' if w.choice_field==name else name
        elif field[2]=='datablock':
            w.choice_field=''
            w.list_field=name
            return ('LIST',w)
        else:
            w.choice_field=''
            w.edit_field=name;w.edit_buffer=(format_value(w.draft[name]) if field[2]=='number' else str(w.draft[name] or ''))
            w.caret=len(w.edit_buffer);w.select_all=False
        return ('CONSUMED',w)

    def _focus_next(self,w,current,reverse=False):
        fields=[field for field in visible_fields(w.kind,w.draft,w.path_closed) if field[2] in {'number','datablock'}]
        if not fields:return
        names=[field[0] for field in fields]
        if current in names:
            index=(names.index(current)+(-1 if reverse else 1))%len(names)
        else:index=len(names)-1 if reverse else 0
        name=names[index];typ=next(field[2] for field in fields if field[0]==name)
        w.edit_field=name
        w.edit_buffer=format_value(w.draft[name]) if typ=='number' else str(w.draft[name] or '')
        w.caret=len(w.edit_buffer);w.select_all=False

    def event(self,ev,x,y,measure_text=None):
        """Return (action, window): CONSUMED, APPLY, CLOSED, or NONE."""
        if self.visibility_scope:
            for w in self.windows:
                if window_visibility.is_hidden(self.visibility_scope,window_visibility.editor_key(w.kind)):
                    w.layout_hidden=True
                    w.hitboxes.clear()
                    w.dragging=False
                    # Keep edit_buffer and focus for a later explicit Restore.
        if self.workspace_dragging:
            if ev.type in ('ESC','RIGHTMOUSE') and ev.value=='PRESS':
                self.workspace_anchor=self.workspace_drag_saved
                self.workspace_dragging=False
                self.group_hitboxes=[]
                return ('CONSUMED',None)
            if ev.type=='MOUSEMOVE' and self.workspace_drag_origin is not None:
                start_x,start_y=self.workspace_drag_origin
                rect0=self.workspace_drag_rect
                dx=x-start_x;dy=y-start_y
                next_rect=(rect0[0]+dx,rect0[1]+dy,rect0[2]+dx,rect0[3]+dy)
                self.workspace_anchor=_universal.normalize_workspace_anchor(
                    next_rect,self.region_size,self._last_scale)
                self.group_hitboxes=[]
                for win in self.windows:win.hitboxes.clear()
                return ('CONSUMED',None)
            if ev.type=='LEFTMOUSE' and ev.value=='RELEASE':
                self.workspace_dragging=False
                if self.workspace_anchor is not None:
                    try:
                        import bpy
                        addon=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
                        prefs=addon.preferences if addon else None
                        if prefs is not None:
                            prefs.ui_wall_workspace_pos_x=self.workspace_anchor[0]
                            prefs.ui_wall_workspace_pos_y=self.workspace_anchor[1]
                            prefs.ui_wall_workspace_user_moved=True
                    except (ImportError,AttributeError,RuntimeError,ReferenceError,TypeError,ValueError):pass
                return ('CONSUMED',None)
            return ('CONSUMED',None)
        dragging=next((w for w in self.windows if w.dragging),None)
        if dragging:
            if ev.type in ('ESC','RIGHTMOUSE') and ev.value=='PRESS':
                dragging.x,dragging.y=dragging.start_pos
                dragging.dragging=False
                return ('CONSUMED',dragging)
            if ev.type=='MOUSEMOVE':
                dx=x-dragging.drag_origin[0];dy=y-dragging.drag_origin[1]
                dragging.x=dragging.start_pos[0]+dx;dragging.y=dragging.start_pos[1]+dy
                # Do not reflow here without the full obstacle/focus list.
                # The next authoritative draw reflows and refreshes hitboxes.
                dragging.hitboxes.clear()
                return ('CONSUMED',dragging)
            if ev.type=='LEFTMOUSE' and ev.value=='RELEASE':
                dragging.dragging=False;return ('CONSUMED',dragging)
            return ('CONSUMED',dragging)
        focus=next((w for w in reversed(self.windows) if w.edit_field and not w.layout_hidden),None)
        menu=next((w for w in reversed(self.windows) if w.choice_field and not w.layout_hidden),None)
        if menu and ev.value=='PRESS' and ev.type in ('ESC','RIGHTMOUSE'):
            menu.choice_field=''
            return ('CONSUMED',menu)
        if menu and ev.value=='PRESS' and ev.type in ('UP_ARROW','DOWN_ARROW','RET','NUMPAD_ENTER'):
            choice=next((f for f in FIELDS[menu.kind] if f[0]==menu.choice_field),None)
            if choice:
                keys=[item[0] for item in choice[3]]
                idx=keys.index(menu.draft[menu.choice_field]) if menu.draft[menu.choice_field] in keys else 0
                if ev.type=='UP_ARROW':idx=(idx-1)%len(keys)
                elif ev.type=='DOWN_ARROW':idx=(idx+1)%len(keys)
                else:menu.choice_field='';return ('CONSUMED',menu)
                menu.draft[choice[0]]=keys[idx]
            return ('CONSUMED',menu)
        # Blender owns Undo/Redo.  An active text field must not eat Ctrl+Z.
        if getattr(ev,'ctrl',False) and ev.type=='Z':
            return ('NONE',None)
        group_click=(ev.type=='LEFTMOUSE' and ev.value=='PRESS' and
            any(r[0]<=x<=r[2] and r[1]<=y<=r[3]
                for _action,_hint,r in self.group_hitboxes))
        if focus and ev.type=='LEFTMOUSE' and ev.value=='PRESS' and not group_click:
            # A repeated click in the focused input moves the caret, not commits.
            for action,name,rect in reversed(focus.hitboxes):
                if (action=='FIELD' and name==focus.edit_field and
                        rect[0]<=x<=rect[2] and rect[1]<=y<=rect[3]):
                    return self._field_click(focus,name,x,rect,measure_text)
            # Clicking outside the field may commit the draft, not the model.
            if not self._commit_field(focus):
                return ('CONSUMED',focus)
        if focus and ev.value=='PRESS':
            if ev.type in ('RET','NUMPAD_ENTER'):
                return (('APPLY',focus) if self._commit_field(focus) else ('CONSUMED',focus))
            if ev.type=='ESC':
                focus.edit_field='';focus.edit_buffer='';focus.error='';return ('CONSUMED',focus)
            kind=next((f[2] for f in FIELDS[focus.kind] if f[0]==focus.edit_field),'number')
            allowed=NUMERIC_CHARS if kind=='number' else None
            if getattr(ev,'ctrl',False) and ev.type in ('A','C','X','V'):
                if ev.type=='A':
                    focus.edit_buffer,focus.caret,focus.select_all=text_edit(
                        focus.edit_buffer,focus.caret,'SELECT_ALL')
                elif ev.type in ('C','X'):
                    if focus.select_all:
                        setter=getattr(ev,'clipboard_set',None)
                        if callable(setter):setter(focus.edit_buffer)
                        if ev.type=='X':
                            focus.edit_buffer,focus.caret,focus.select_all=text_edit(
                                focus.edit_buffer,focus.caret,'DELETE',selected=True)
                else:
                    focus.edit_buffer,focus.caret,focus.select_all=text_edit(
                        focus.edit_buffer,focus.caret,'INSERT',
                        insert=str(getattr(ev,'clipboard','') or ''),
                        selected=focus.select_all,allowed=allowed)
                focus.error='';return ('CONSUMED',focus)
            if ev.type in ('BACK_SPACE','DEL','HOME','END','LEFT_ARROW','RIGHT_ARROW'):
                command={'BACK_SPACE':'BACKSPACE','DEL':'DELETE',
                         'LEFT_ARROW':'LEFT','RIGHT_ARROW':'RIGHT'}.get(ev.type,ev.type)
                focus.edit_buffer,focus.caret,focus.select_all=text_edit(
                    focus.edit_buffer,focus.caret,command,selected=focus.select_all)
                focus.error='';return ('CONSUMED',focus)
            char=getattr(ev,'unicode','') or ''
            if not char:
                _key_chars={'NUMPAD_0':'0','NUMPAD_1':'1','NUMPAD_2':'2',
                    'NUMPAD_3':'3','NUMPAD_4':'4','NUMPAD_5':'5','NUMPAD_6':'6',
                    'NUMPAD_7':'7','NUMPAD_8':'8','NUMPAD_9':'9','NUMPAD_PERIOD':'.',
                    'NUMPAD_MINUS':'-','NUMPAD_PLUS':'+','PERIOD':'.','COMMA':','}
                char=_key_chars.get(ev.type,'')
            if char and char.isprintable() and not getattr(ev,'ctrl',False) and not getattr(ev,'alt',False):
                focus.edit_buffer,focus.caret,focus.select_all=text_edit(
                    focus.edit_buffer,focus.caret,'INSERT',insert=char,
                    selected=focus.select_all,allowed=allowed)
                focus.error=''
                return ('CONSUMED',focus)
            if ev.type=='TAB':
                current=focus.edit_field
                if self._commit_field(focus):
                    self._focus_next(focus,current,reverse=bool(getattr(ev,'shift',False)))
                return ('CONSUMED',focus)
            # Text input owns keyboard, including shortcuts, while editing.
            if ev.type not in ('LEFTMOUSE','RIGHTMOUSE','MOUSEMOVE'):
                return ('CONSUMED',focus)
        if ev.type=='LEFTMOUSE' and ev.value=='PRESS':
            for action,hint,box in reversed(self.group_hitboxes):
                if box[0]<=x<=box[2] and box[1]<=y<=box[3]:
                    if action=='GROUP_DRAG' and self.group_rect is not None:
                        self.workspace_dragging=True
                        self.workspace_drag_origin=(x,y)
                        self.workspace_drag_rect=self.group_rect
                        self.workspace_drag_saved=self.workspace_anchor
                        return ('CONSUMED',None)
                    if action!='GROUP_CLOSE_ALL':self.close_all_armed=False
                    if action=='GROUP_HIDE_ALL':
                        for win in self.windows:
                            win.hidden=True;win.layout_hidden=True;win.hitboxes.clear()
                            win.choice_field='';win.dragging=False
                            if self.visibility_scope:
                                window_visibility.set_hidden(self.visibility_scope,
                                    window_visibility.editor_key(win.kind),True)
                    elif action=='GROUP_COLLAPSE_ALL':
                        collapse=not all(win.collapsed for win in self.windows if not win.hidden)
                        for win in self.windows:
                            if not win.hidden:
                                win.collapsed=collapse;win.hitboxes.clear()
                                win.choice_field=''
                    elif action=='GROUP_CLOSE_ALL':
                        # Destructive close-all requires a second deliberate
                        # click when any editor has an unsaved draft.
                        if any(win.dirty_count or win.edit_field for win in self.windows) and not self.close_all_armed:
                            self.close_all_armed=True
                        else:
                            self.clear()
                    elif action.startswith('GROUP_RESTORE:'):
                        kind=action.split(':',1)[1]
                        win=self.editor(kind)
                        if win is not None:
                            win.hidden=False;win.collapsed=False
                            if self.visibility_scope:
                                window_visibility.set_hidden(self.visibility_scope,
                                    window_visibility.editor_key(kind),False)
                    # Next draw reflows *once* with the complete obstacle map.
                    self.group_hitboxes=[]
                    for win in self.windows:win.hitboxes.clear()
                    return ('CONSUMED',None)
            w=self.top_at(x,y)
            if w is None:return ('NONE',None)
            self.windows.remove(w);self.windows.append(w)
            for action,name,r in reversed(w.hitboxes):
                if r[0]<=x<=r[2] and r[1]<=y<=r[3]:
                    if action=='CLOSE' or action=='CANCEL':
                        self.windows.remove(w)
                        if self.visibility_scope:
                            window_visibility.forget(self.visibility_scope,window_visibility.editor_key(w.kind))
                        return ('CLOSED',w)
                    if action=='HIDE':
                        w.hidden=True;w.layout_hidden=True
                        w.hitboxes.clear();w.choice_field='';w.dragging=False
                        if self.visibility_scope:
                            window_visibility.set_hidden(self.visibility_scope,
                                window_visibility.editor_key(w.kind),True)
                        self.group_hitboxes=[]
                        return ('CONSUMED',w)
                    if action=='PIN':
                        # PIN now protects this section from auto-collapse;
                        # unlike the legacy UI it can NEVER split the group.
                        w.pinned=not w.pinned
                        w.detached=False;w.dock_side='RIGHT'
                        return ('CONSUMED',w)
                    if action=='COLLAPSE':
                        if w.auto_compact:
                            self.windows.remove(w);self.windows.append(w)
                            w.auto_compact=False;w.collapsed=False
                        else:
                            w.collapsed=not w.collapsed
                        w.choice_field=''
                        w.scroll=0
                        self.clamp(self.region_size)
                        return ('CONSUMED',w)
                    if action=='DRAG':
                        # Unified right workspace has one stable anchor.
                        # Section dragging no longer tears its panels apart.
                        return ('CONSUMED',w)
                    if action=='CHOICE':
                        field=w.choice_field
                        if field:
                            w.draft[field]=name
                        w.choice_field=''
                        w.error=''
                        return ('CONSUMED',w)
                    if action=='FIELD':return self._field_click(w,name,x,r,measure_text)
                    if action=='RESET':
                        w.draft=w.baseline.copy()
                        w.edit_field='';w.edit_buffer='';w.choice_field='';w.error='';w.scroll=0
                        return ('CONSUMED',w)
                    if action=='PICK':return ('PICK',w)
                    if action=='LIST':
                        if name:w.list_field=name
                        return ('LIST',w)
                    if action=='APPLY':
                        if not self._commit_field(w):return ('CONSUMED',w)
                        return ('APPLY',w)
            if w.choice_field:
                w.choice_field=''
            return ('CONSUMED',w)
        hovered=self.top_at(x,y)
        if ev.type=='MOUSEMOVE':
            self.group_hover=next((action for action,_hint,box in self.group_hitboxes
                 if box[0]<=x<=box[2] and box[1]<=y<=box[3]),'')
            for w in self.windows:w.hover=('',None)
            if hovered is not None:
                for action,name,r in reversed(hovered.hitboxes):
                    if r[0]<=x<=r[2] and r[1]<=y<=r[3]:
                        hovered.hover=(action,name);break
        if hovered is not None and ev.type in ('WHEELUPMOUSE','WHEELDOWNMOUSE'):
            max_scroll=max(0,len(visible_fields(hovered.kind,hovered.draft,hovered.path_closed))-hovered.row_capacity)
            delta=-1 if ev.type=='WHEELUPMOUSE' else 1
            hovered.scroll=max(0,min(max_scroll,hovered.scroll+delta))
            return ('CONSUMED',hovered)
        if hovered is not None and ev.type in ('MOUSEMOVE','RIGHTMOUSE','LEFTMOUSE'):
            return ('CONSUMED',hovered)
        if self.group_rect is not None and ev.type in ('MOUSEMOVE','LEFTMOUSE','RIGHTMOUSE'):
            a,b,c,d=self.group_rect
            if a<=x<=c and b<=y<=d:return ('CONSUMED',None)
        if ev.type=='ESC' and ev.value=='PRESS' and self.windows:
            top=self.windows[-1]
            self.close(top.kind)
            return ('CLOSED',top)
        return ('NONE',None)
