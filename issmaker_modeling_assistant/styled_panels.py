"""Small shared viewport panels: one measured rectangle for drawing and input."""
import math
import bpy
from bpy.types import Operator
from .appearance_gpu import Painter, RenderCache, within
from .core import theme_engine, window_visibility
from .core import viewport_skin as _viewport_skin

_ACTIVE = {}
# Resource ownership is independent of Blender Operator RNA: a stale Operator
# can raise ReferenceError on ANY attribute read (including close_panel).
_ACTIVE_RESOURCES = {}
_REGISTRY_FAILURES = []


def _note_registry_failure(stage, error):
    _REGISTRY_FAILURES.append({'stage': str(stage), 'error':
                               type(error).__name__+': '+str(error)[:240]})
    if len(_REGISTRY_FAILURES) > 32:
        del _REGISTRY_FAILURES[:-32]


def _slot_for_owner(owner):
    # Object identity comparisons do not dereference Blender RNA properties.
    for slot, active in tuple(_ACTIVE.items()):
        if active is owner:
            return slot
    return None


def _release_slot(slot, context=None, *, owner=None):
    """Fail-safe, identity-guarded cleanup; no Blender Operator field access needed."""
    current = _ACTIVE.get(slot)
    if owner is not None and current is not owner:
        return False  # late close from old owner cannot erase new owner's HUD
    resource = _ACTIVE_RESOURCES.pop(slot, None)
    _ACTIVE.pop(slot, None)
    if current is not None:
        try:
            current._closed = True
        except (ReferenceError, RuntimeError) as exc:
            _note_registry_failure('RNA_STALE', exc)
    if resource:
        handle = resource.get('handler')
        if handle is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            except (ReferenceError, RuntimeError, ValueError) as exc:
                _note_registry_failure('DRAW_REMOVE', exc)
        timer = resource.get('timer')
        if timer is not None:
            try:
                wm = getattr(context, 'window_manager', None) if context is not None else None
                if wm is None:
                    wm = bpy.context.window_manager
                wm.event_timer_remove(timer)
            except (AttributeError, ReferenceError, RuntimeError, ValueError) as exc:
                _note_registry_failure('TIMER_REMOVE', exc)
        cache = resource.get('cache')
        if cache is not None:
            try:
                cache.clear()
            except (ReferenceError, RuntimeError) as exc:
                _note_registry_failure('CACHE_CLEAR', exc)
    try:
        window_visibility.forget(slot[0], slot[1])
    except (AttributeError, ReferenceError, RuntimeError, ValueError) as exc:
        _note_registry_failure('WINDOW_VISIBILITY', exc)
    if context is not None:
        try:
            if context.area is not None:
                context.area.tag_redraw()
        except (ReferenceError, RuntimeError) as exc:
            _note_registry_failure('REDRAW', exc)
    return True


def exclusive_panel_active(region):
    """Ask without trusting saved Blender RNA. Prune stale instances eagerly."""
    for slot, instance in tuple(_ACTIVE.items()):
        try:
            if instance._closed:
                _release_slot(slot, owner=instance)
                continue
            if instance.exclusive and instance._region == region:
                return True
        except (ReferenceError, RuntimeError) as exc:
            _note_registry_failure('RNA_STALE', exc)
            _release_slot(slot, owner=instance)
    return False


def popup_registry_snapshot():
    """JSON-safe, bounded, no RNA dereferences. Suitable for Support ZIP."""
    return {'active_slots': [str(s) for s in _ACTIVE],
            'resource_slots': len(_ACTIVE_RESOURCES),
            'failures': list(_REGISTRY_FAILURES[-16:])}


class StyledPanel:
    exclusive = False
    title = 'ISSMAKER'
    panel_height = 520

    def open_panel(self, context):
        if context.area is None or context.area.type != 'VIEW_3D':return {'CANCELLED'}
        region=next((r for r in context.area.regions if r.type=='WINDOW'),None)
        minimum_height=365 if self.exclusive else 550
        if region is None or region.width<350 or region.height<minimum_height:
            self.report({'WARNING'},'Увеличьте область 3D View для этого HUD')
            return {'CANCELLED'}
        self._area=context.area;self._region=region;self._window=context.window
        self._scope=window_visibility.scope_from_context(context,region=region)
        self._key=self.bl_idname
        slot=(self._scope,self._key)
        # Old Blender RNA may already have been freed; resource cleanup must
        # never call old.close_panel() or inspect old properties.
        if slot in _ACTIVE or slot in _ACTIVE_RESOURCES:
            _release_slot(slot, context)
        self._cache=RenderCache();self._closed=False;self._hits=[];self._hover=None
        self._tab='CHECK';self._message='';self._edit=None;self._drag=None
        self._size=None;self._rect=None;self._error=''
        self._handler=None;self._timer=None
        try:
            self._handler=bpy.types.SpaceView3D.draw_handler_add(self._safe_draw_panel,(),'WINDOW','POST_PIXEL')
            self._timer=context.window_manager.event_timer_add(.15,window=context.window)
            _ACTIVE[slot]=self
            _ACTIVE_RESOURCES[slot]={'handler':self._handler,'timer':self._timer,'cache':self._cache}
            context.window_manager.modal_handler_add(self)
            context.area.tag_redraw()
        except Exception:
            if slot in _ACTIVE_RESOURCES:
                _release_slot(slot, context, owner=self)
            else:
                # Failure before owner registration: independently rollback.
                if self._handler is not None:
                    try:bpy.types.SpaceView3D.draw_handler_remove(self._handler,'WINDOW')
                    except (ReferenceError, RuntimeError, ValueError) as exc:_note_registry_failure('OPEN_DRAW_ROLLBACK',exc)
                if self._timer is not None:
                    try:context.window_manager.event_timer_remove(self._timer)
                    except (ReferenceError, RuntimeError, ValueError) as exc:_note_registry_failure('OPEN_TIMER_ROLLBACK',exc)
                try:self._cache.clear()
                except (ReferenceError, RuntimeError) as exc:_note_registry_failure('OPEN_CACHE_ROLLBACK',exc)
            raise
        return {'RUNNING_MODAL'}

    def _safe_draw_panel(self):
        # Callback can outlive its RNA owner after Undo or workspace change.
        slot = _slot_for_owner(self)
        if slot is None:
            return
        try:
            self._draw_panel()
        except (ReferenceError, RuntimeError) as exc:
            _note_registry_failure('DRAW_STALE_RNA',exc)
            _release_slot(slot, owner=self)

    def close_panel(self, context):
        # Find by Python identity; accessing self._scope on freed RNA can throw.
        slot=_slot_for_owner(self)
        if slot is not None:
            _release_slot(slot,context,owner=self)
        else:
            try:self._closed=True
            except (ReferenceError,RuntimeError):pass

    def _button(self,p,rect,label,key,enabled=True,active=False):
        t=self._theme
        state='disabled' if not enabled else 'active' if active else 'hover' if key==self._hover else 'normal'
        role='danger' if key=='CLOSE' else 'primary' if key=='APPLY' else 'neutral'
        visual=_viewport_skin.component('button',state,role,t)
        p.box(*rect,visual.background,visual.border,rad=_viewport_skin.CORNER_PX['button'])
        p.text(label,rect[0]+rect[2]/2,rect[1]+rect[3]/2-4,
               size=_viewport_skin.text_px('body'),
               color=_viewport_skin.control_ink(t,part='button',state=state,role=role),
               width=rect[2]-16,align='center')
        if enabled:self._hits.append((key,rect))

    def _angle(self):
        return math.degrees(self.smooth_angle) if self.exclusive else float(bpy.context.scene.ima_uv_smooth_angle)

    def _set_angle(self,degrees):
        value=math.radians(max(0.,min(180.,float(degrees))))
        if self.exclusive:self.smooth_angle=value
        else:bpy.context.scene.ima_uv_smooth_angle=math.degrees(value)

    def _angle_controls(self,p,x,y,w):
        p.text('Угол сглаживания',x,y+60,_viewport_skin.text_px('body'),_viewport_skin.ink(self._theme,kind='muted'))
        value=self._edit if self._edit is not None else f'{self._angle():.1f}°'
        self._button(p,(x,y+10,w,40),value+('│' if self._edit is not None else ''),'ANGLE')
        for i,n in enumerate((30,45,60,90)):
            self._button(p,(x+i*(w+6)/4,y-26,(w-18)/4,28),f'{n}°','PRESET:'+str(n))

    def _draw_panel(self):
        if self._closed:return
        context=bpy.context
        if context.area!=self._area or context.region!=self._region:return
        region=self._region
        self._size=(region.width,region.height)
        w=min(510. if not self.exclusive else 440.,region.width-28.);h=min(float(self.panel_height),region.height-32.)
        if self._rect is None:self._rect=((region.width-w)/2,(region.height-h)/2,w,h)
        x,y,_,_=self._rect
        x=max(8.,min(x,region.width-w-8));y=max(8.,min(y,region.height-h-8))
        self._rect=(x,y,w,h)
        window_visibility.observe(self._scope,self._key,(x,y,x+w,y+h))
        prefs=context.preferences.addons.get('issmaker_modeling_assistant')
        self._theme=theme_engine.from_preferences(prefs.preferences if prefs else None)
        t=self._theme;p=Painter(cache=self._cache);self._hits=[]
        try:
            chrome=_viewport_skin.shell(t)
            p.box(x,y,w,h,chrome['panel'],chrome['border'],rad=_viewport_skin.CORNER_PX['panel'])
            p.box(x+5,y+h-54,w-10,48,chrome['header'],rad=_viewport_skin.CORNER_PX['panel'])
            p.text(self.title,x+19,y+h-34,14,chrome['title'],width=w-76)
            self._button(p,(x+w-43,y+h-42,30,28),'×','CLOSE')
            self._hits.append(('DRAG',(x+10,y+h-54,w-65,49)))
            if self.exclusive:
                p.text('Завершение стены',x+22,y+h-85,13,chrome['accent'])
                p.text('Выберите угол и подтвердите результат',x+22,y+h-109,11,chrome['muted'],width=w-44)
                self._angle_controls(p,x+22,y+112,w-44)
                p.text('Сглаживание будет применено к Mesh',x+22,y+72,11,chrome['muted'])
                self._button(p,(x+18,y+18,(w-44)/2,36),'Отмена','CLOSE')
                self._button(p,(x+26+(w-44)/2,y+18,(w-44)/2,36),'Применить','APPLY',active=True)
            else:self._draw_quality(p,context,x,y,w,h)
            if self._message:p.text(self._message,x+18,y+61,10,_viewport_skin.semantic_ink(t,role='warning'),width=w-36)
        except Exception as exc:
            self._error=str(exc);self._hits=[]
        finally:p.close()

    def _draw_quality(self,p,context,x,y,w,h):
        """One shared ISSMAKER visual system, with preserved operator routes.

        Drawing/hit testing use _button; nothing here triggers mesh reads beyond
        inexpensive counts. No diagnostics are fabricated before scan actions.
        """
        obj=context.active_object
        mesh=obj.data if obj is not None and obj.type=='MESH' else None
        t=self._theme
        skin=_viewport_skin
        xpad=17
        upper=y+h
        p.box(x+12,upper-111,w-24,51,skin.shell(t)['header'],rad=7)
        p.text('ОБЪЕКТ',x+22,upper-76,10,skin.ink(t,kind='muted'))
        p.text(obj.name if mesh else 'Mesh не выбран',x+88,upper-76,12,
               skin.ink(t),width=w-120)
        if mesh:
            status=(f'{len(mesh.vertices)} вершин  ·  {len(mesh.edges)} рёбер'
                    f'  ·  {len(mesh.polygons)} граней')
        else:status='Выберите Mesh для анализа'
        p.text(status,x+22,upper-98,11,skin.ink(t,kind='muted'),width=w-43)
        tabs=(('CHECK','ПРОВЕРКИ'),('VIEW','ОТОБРАЖЕНИЕ'),('SMOOTH','ОПЕРАЦИИ'))
        for i,(tab,label) in enumerate(tabs):
            self._button(p,(x+16+i*(w-32)/3,upper-155,(w-43)/3,32),
                         label,'TAB:'+tab,active=self._tab==tab)
        enabled=mesh is not None and context.mode=='OBJECT'
        seam_enabled=enabled and mesh.uv_layers.active is not None
        base=upper-191
        if self._tab=='CHECK':
            p.text('01   UV И ГЕОМЕТРИЯ',x+21,base,12,skin.ink(t,kind='accent'))
            # Featured read-only scan: returns a separate modal inspector
            # instead of nesting GPU handlers inside the Geometry popup.
            self._button(p,(x+17,base-55,w-34,44),
                         'НАЙТИ UV-ШВЫ  ·  КРАСНЫЕ / ЖЁЛТЫЕ',
                         'UV_SEAM_INSPECT',enabled)
            p.text('Инспектор · независимые фильтры · F фокус · Esc выход',
                   x+22,base-69,10,skin.ink(t,kind='muted'),width=w-44)
            p.text('02   ДРУГИЕ ПРОВЕРКИ',x+21,base-90,12,skin.ink(t,kind='accent'))
            actions=(('Зеркальные UV','FIND'),('Полный аудит UV','AUDIT'),
                     ('Нормали и роли','NORMALS'),('Вместимость 2×2 / 2K','COVER2'),
                     ('Вместимость 4×4 / 4K','COVER4'),('Исправить UV flip','FIX'),
                     ('Подобрать оси Extrude','AXES'))
            gap=8.;bw=(w-34-gap)/2.
            for i,(label,key) in enumerate(actions):
                self._button(p,(x+17+(i%2)*(bw+gap),base-132-(i//2)*42,
                                bw,36),label,key,enabled)
            if h>545:
                p.text('Проверки не меняют Mesh, кроме явных исправлений.',
                       x+21,y+62,10,skin.ink(t,kind='muted'),width=w-42)
        elif self._tab=='VIEW':
            from . import module_visualization
            state=module_visualization.status(context)
            p.text('01   UV-ПОДСВЕТКА',x+21,base,12,skin.ink(t,kind='accent'))
            self._button(p,(x+17,base-60,w-34,39),
                         'Скрыть UV-границы' if state.get('active') else 'Показать UV-границы  ·  READ ONLY',
                         'OVERLAY',seam_enabled,active=bool(state.get('active')))
            self._button(p,(x+17,base-108,w-34,39),
                         'Пометить Seam  ·  ИЗМЕНЯЕТ MESH','SEAMS',seam_enabled)
            p.text('02   ЦВЕТ ПОДСВЕТКИ',x+21,base-139,12,skin.ink(t,kind='accent'))
            p.text('Видимые линии не означают доказанный дефект UV.',
                   x+21,base-159,10,skin.ink(t,kind='muted'))
            for i,color in enumerate(((.1,.8,.65),(.25,.55,1),(.95,.65,.15),(.9,.3,.4),(.8,.8,.85))):
                rect=(x+19+i*(w-38)/5,base-208,(w-54)/5,31)
                p.box(*rect,(*color,.95),rad=7)
                self._hits.append(('COLOR:'+str(i),rect))
        else:
            p.text('01   ЗАТЕНЕНИЕ MESH',x+21,base,12,skin.ink(t,kind='accent'))
            p.text('Операции меняют геометрию или свойства Mesh.',
                   x+21,base-24,11,skin.ink(t,kind='muted'))
            self._angle_controls(p,x+22,base-113,w-44)
            self._button(p,(x+18,base-222,w-36,36),
                         'Применить Smooth by Angle','SMOOTH',enabled)
            self._button(p,(x+18,base-267,w-36,36),
                         'Плоское затенение','FLAT',enabled)
        self._button(p,(x+18,y+16,w-36,32),'Закрыть','CLOSE')

    def _commit_edit(self):
        if self._edit is None:return True
        try:
            value=float(self._edit.replace(',','.'))
            if not math.isfinite(value):raise ValueError
            self._set_angle(value);self._edit=None;return True
        except ValueError:self._message='Введите угол от 0 до 180°';return False

    def modal(self,context,event):
        try:return self._modal_event(context,event)
        except ReferenceError:
            # Do not perform Blender RNA lookups while recovering a stale
            # operator. Sidecar handles/timer remain independently reclaimable.
            slot=_slot_for_owner(self)
            if slot is not None:_release_slot(slot,context,owner=self)
            return {'CANCELLED'}
        except (RuntimeError,ValueError,TypeError) as exc:
            self._message='Ошибка: '+str(exc)[:130]
            self.report({'ERROR'},self._message)
            if not getattr(self,'_closed',True):self._area.tag_redraw()
            return {'RUNNING_MODAL'}

    def _modal_event(self,context,event):
        if self._closed:return {'FINISHED'}
        if not any(area==self._area for area in self._window.screen.areas):
            self.close_panel(context);return {'CANCELLED'}
        x=event.mouse_x-self._region.x;y=event.mouse_y-self._region.y
        if event.type=='TIMER':
            self._area.tag_redraw();return {'PASS_THROUGH'}
        if event.type=='ESC' and event.value=='PRESS':
            if self._edit is not None:self._edit=None;return {'RUNNING_MODAL'}
            self.close_panel(context);return {'CANCELLED'}
        if self._edit is not None and event.value=='PRESS':
            if event.type in {'RET','NUMPAD_ENTER'}:
                self._commit_edit();return {'RUNNING_MODAL'}
            if event.type=='BACK_SPACE':self._edit=self._edit[:-1];return {'RUNNING_MODAL'}
            if event.ctrl and event.type=='A':self._edit='';return {'RUNNING_MODAL'}
            char=getattr(event,'unicode','')
            if char and all(c in '0123456789.,' for c in char):self._edit+=char;return {'RUNNING_MODAL'}
        if event.type in {'RET','NUMPAD_ENTER'} and event.value=='PRESS' and self.exclusive:
            return self._apply_finish(context)
        if event.type=='MOUSEMOVE':
            if self._drag is not None:
                ox,oy,px,py=self._drag;rx,ry,w,h=self._rect
                self._rect=(ox+x-px,oy+y-py,w,h);self._area.tag_redraw();return {'RUNNING_MODAL'}
            self._hover=next((key for key,r in reversed(self._hits) if within(r,(x,y))),None)
            self._area.tag_redraw()
        if event.type=='LEFTMOUSE' and event.value=='RELEASE' and self._drag is not None:
            self._drag=None;return {'RUNNING_MODAL'}
        if event.type=='LEFTMOUSE' and event.value=='PRESS':
            if self._size!=(self._region.width,self._region.height):
                self._area.tag_redraw();return {'RUNNING_MODAL'}
            key=next((key for key,r in reversed(self._hits) if within(r,(x,y))),None)
            if key=='DRAG':self._drag=(*self._rect[:2],x,y);return {'RUNNING_MODAL'}
            if key=='CLOSE':self.close_panel(context);return {'CANCELLED'}
            if key=='ANGLE':self._edit='';return {'RUNNING_MODAL'}
            if key is not None:
                if not self._commit_edit():return {'RUNNING_MODAL'}
                if key.startswith('PRESET:'):self._set_angle(float(key.split(':')[1]))
                elif key=='APPLY':return self._apply_finish(context)
                elif key.startswith('TAB:'):self._tab=key.split(':')[1];self._message=''
                else:self._quality_action(context,key)
                if self._closed:return {'CANCELLED'}
                self._area.tag_redraw();return {'RUNNING_MODAL'}
        inside=self._rect is not None and within(self._rect,(x,y))
        return {'RUNNING_MODAL'} if self.exclusive or inside else {'PASS_THROUGH'}

    def _apply_finish(self,context):
        if not self._commit_edit():return {'RUNNING_MODAL'}
        status=self.execute(context)
        if 'FINISHED' in status:self.close_panel(context);return {'FINISHED'}
        self._message='Не удалось принять стену. Проверьте готовый результат.'
        return {'RUNNING_MODAL'}

    def _quality_action(self,context,key):
        if key.startswith('COLOR:'):
            colors=((.1,.8,.65),(.25,.55,1),(.95,.65,.15),(.9,.3,.4),(.8,.8,.85))
            context.scene.ima_uv_module_tint=(*colors[int(key.split(':')[1])],1.) if len(context.scene.ima_uv_module_tint)==4 else colors[int(key.split(':')[1])]
            return
        if key=='SMOOTH':
            from .geometry_shading import apply_angle_smoothing
            apply_angle_smoothing(context,context.active_object,math.radians(context.scene.ima_uv_smooth_angle))
            bpy.ops.ed.undo_push(message='ISSMAKER · Сглаживание применено')
            self._message='Сглаживание применено к Mesh';return
        routes={'FIND':('select_flipped',{},True),'AUDIT':('uv_quality_report',{},False),
                'NORMALS':('role_surface_report',{},False),'COVER2':('uv_module_coverage_report',{'resolution':'2048'},False),
                'COVER4':('uv_module_coverage_report',{'resolution':'4096'},False),
                'FIX':('uv_fix_mirrored_islands',{},False),'OVERLAY':('module_overlay',{},True),
                'SEAMS':('module_mark_uv_seams',{},False),'FLAT':('wall_safe_shading',{'mode':'FLAT'},False),
                'AXES':('module_pick_axes',{},True)}
        if key=='UV_SEAM_INSPECT':
            # Two modal HUDs cannot own the same Esc/click route.
            self.close_panel(context)
            try:
                with context.temp_override(area=self._area,region=self._region):
                    bpy.ops.view3d.brick_hole_seam_scan('EXEC_DEFAULT')
            except RuntimeError as exc:self.report({'ERROR'},str(exc)[:200])
            return
        if key not in routes:return
        name,kwargs,invoke=routes[key]
        if key=='FIND':
            self.close_panel(context)
            if context.mode=='OBJECT':bpy.ops.object.mode_set(mode='EDIT')
        try:
            with context.temp_override(area=self._area,region=self._region):
                result=getattr(bpy.ops.ima,name)('INVOKE_DEFAULT' if invoke else 'EXEC_DEFAULT',**kwargs)
            if 'CANCELLED' not in result:
                self._message='Готово'
                if key == 'OVERLAY':
                    from . import module_visualization
                    state = module_visualization.status(context)
                    hidden = int(state.get('omitted_edges',0))
                    if hidden:
                        self._message=f'UV-подсветка частичная: {hidden} линий скрыто (GPU-бюджет)'
            elif key in {'SEAMS','OVERLAY'}:
                self._message='Выберите Mesh с UV-картой в Object Mode'
            else:
                self._message='Проверьте объект и UV-карту'
        except RuntimeError as exc:self._message=str(exc)[-140:]


class IMA_OT_GeometryQualityPopup(StyledPanel,Operator):
    bl_idname='ima.geometry_quality_popup'
    bl_label='Геометрия · качество'
    bl_description='Проверки UV, визуализация и сглаживание в отдельном HUD'
    bl_options={'INTERNAL'}
    title='ISSMAKER  /  ГЕОМЕТРИЯ'
    panel_height=565
    def invoke(self,context,event):return self.open_panel(context)
    def execute(self,context):return {'FINISHED'}


def shutdown(context):
    # No access to stored RNA operators during shutdown (they may be dead).
    for slot in tuple(set(_ACTIVE) | set(_ACTIVE_RESOURCES)):
        _release_slot(slot, context)
