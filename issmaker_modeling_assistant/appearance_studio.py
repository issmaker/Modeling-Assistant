"""Pass 11B: Blender GPU Appearance Studio.

This editor never calls mesh or modeling operators. Every control uses one
bounded local rect for both pixels and hit test. Theme values live exclusively
in root AddonPreferences and its versioned autosave profile.
"""
from __future__ import annotations
import time
import math
from dataclasses import replace
import bpy
from bpy.types import Operator
from .core import pattern_engine, theme_engine, motion_engine, window_manager, window_visibility, action_trace, frame_diagnostics, hud_quality, appearance_layout
from .core import viewport_skin as _viewport_skin
from .core.design_tokens import mix_color as _mix_ui_color
from .appearance_gpu import Painter, RenderCache, within

_ACTIVE = {}
SWATCHES=((.06,.62,.49),(.12,.43,.88),(.51,.33,.78),(.85,.64,.23),
          (.04,.69,.78),(.91,.34,.32),(.56,.72,.17),(.78,.36,.57),
          (.62,.70,.77),(.84,.58,.46))
SAMPLE_TOOLS=(('DWOS','ОКНА И ДВЕРИ','FRAME · BOX · GLASS'),
              ('UV Wall','СТЕНА','СТЕНА · ПОВЕРХНОСТЬ · КАРНИЗ'),
              ('Collision','КОЛЛИЗИИ','ПРОБЛЕМЫ · НАВИГАЦИЯ'),
              ('Brick Hole','ПРОЁМЫ','ШИРИНА · ГЛУБИНА · UV'))


def _prefs():
    try:
        addon=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
        return addon.preferences if addon is not None else None
    except (AttributeError,RuntimeError,ReferenceError):return None


def _blf_draw(p,txt,x,y,size,color,width=None,align='left'):
    p.text(txt,x,y,size=size,color=color,width=width,align=align)


def draw_radial_entry(center,scale,hovered,accent):
    """Separate bottom settings chip; four existing sectors remain untouched."""
    theme=theme_engine.from_preferences(_prefs())
    p=Painter()
    try:
        cx,cy=center
        x=cx-161*scale;y=cy-220*scale;w=154*scale;h=47*scale
        visual=_viewport_skin.component('button','hover' if hovered else 'normal','primary',theme)
        p.box(x,y,w,h,visual.background,visual.border,rad=_viewport_skin.control_radius('button',scale))
        p.text('СТИЛЬ',x+w/2,y+19*scale,round(12*scale),_viewport_skin.ink(theme),align='center')
        p.text('Appearance Studio',x+w/2,y+7*scale,round(_viewport_skin.text_px('caption')*scale),
               _viewport_skin.ink(theme,kind='muted'),align='center')
    finally:p.close()


def draw_geometry_entry(center,scale,hovered,accent):
    theme=theme_engine.from_preferences(_prefs())
    p=Painter()
    try:
        cx,cy=center;x=cx+7*scale;y=cy-220*scale;w=154*scale;h=47*scale
        visual=_viewport_skin.component('button','hover' if hovered else 'normal','primary',theme)
        p.box(x,y,w,h,visual.background,visual.border,rad=_viewport_skin.control_radius('button',scale))
        p.text('ГЕОМЕТРИЯ',x+w/2,y+19*scale,round(12*scale),_viewport_skin.ink(theme),align='center')
        p.text('Проверки и качество',x+w/2,y+7*scale,round(_viewport_skin.text_px('caption')*scale),
               _viewport_skin.ink(theme,kind='muted'),align='center')
    finally:p.close()


class IMA_OT_AppearanceAdvanced(Operator):
    bl_idname='ima.appearance_advanced'
    bl_label='ISSMAKER · точные настройки оформления'
    bl_options={'INTERNAL'}

    def invoke(self,context,event):
        return context.window_manager.invoke_popup(self,width=470)

    def execute(self,context):return {'FINISHED'}

    def draw(self,context):
        pref=_prefs()
        if pref is None:return
        L=self.layout
        L.label(text='APPEARANCE STUDIO · НАСТРОЙКИ',icon='SHADING_SOLID')
        col=L.column(align=True)
        col.prop(pref,'ui_scheme',text='Схема')
        col.prop(pref,'ui_surface',text='Материал корпуса')
        col.prop(pref,'ui_pattern',text='Фон')
        col.prop(pref,'ui_render_quality',text='Качество HUD')
        col.prop(pref,'ui_custom_accent',text='Свой цвет')
        col.prop(pref,'ui_accent_color',text='Акцент')
        col.prop(pref,'ui_secondary_color',text='Второй цвет')
        for field in ('ui_pattern_strength','ui_pattern_opacity','ui_pattern_scale','ui_pattern_density',
                      'ui_pattern_contrast','ui_pattern_softness','ui_pattern_glow',
                      'ui_pattern_speed','ui_pattern_amplitude','ui_pattern_parallax','ui_pattern_relief'):
            col.prop(pref,field,slider=True)
        col.prop(pref,'ui_motion_quality',text='Анимации')
        col.prop(pref,'ui_reduced_motion',text='Снизить движение')
        col.separator()
        col.prop(pref,'hud_display_preset',text='Читаемость дисплея')
        col.label(text='2K/4K — нижний порог, не двойной DPI Blender')
        col.prop(pref,'font_scale',text='Размер шрифта',slider=True)
        col.prop(pref,'window_scale',text='Размер окон',slider=True)
        col.prop(pref,'ui_debug_bounds',text='UI Debug')
        L.label(text='Изменения автоматически сохраняются после короткой паузы',icon='FILE_TICK')


class IMA_OT_AppearanceStudio(Operator):
    bl_idname='ima.appearance_studio'
    bl_label='ISSMAKER · Appearance Studio'
    bl_description='Настройки оформления и анимированных фонов в отдельном плавающем окне'
    bl_options={'INTERNAL'}

    @classmethod
    def poll(cls,context):
        return bool(context.area and context.area.type=='VIEW_3D' and
                    context.region and context.region.type=='WINDOW')

    def invoke(self,context,event):
        scope=window_visibility.scope_from_context(context)
        if not scope:return {'CANCELLED'}
        existing=_ACTIVE.get(scope)
        if existing and not existing._closed:
            window_visibility.set_hidden(scope,'APPEARANCE_STUDIO',False)
            existing._minimized=False
            try:existing._area.tag_redraw()
            except (ReferenceError,RuntimeError):pass
            self.report({'INFO'},'Appearance Studio уже открыта')
            return {'FINISHED'}
        pref=_prefs()
        if pref is None:
            self.report({'ERROR'},'Нет активного профиля ISSMAKER')
            return {'CANCELLED'}
        self._window=context.window;self._area=context.area;self._region=context.region
        self._scope=scope;self._closed=False;self._handler=None;self._timer=None
        self._started=time.monotonic();self._hover=None;self._hover_at=self._started;self._tab_at=self._started;self._active_slider=None
        self._motion=motion_engine.MotionBank();self._render_cache=RenderCache();self._fps_stats=frame_diagnostics.FrameDiagnostics();self._closing_at=None;self._pending_min=None;self._press_at=None;self._press_key=None;self._min_reveal_at=None;self._pattern_prev=None;self._pattern_change_at=None;self._tab_previous=self._tab if hasattr(self,'_tab') else 'GALLERY'
        self._drag=False;self._minimized=bool(pref.ui_studio_collapsed)
        self._tab=pref.ui_studio_tab;self._tab_previous=self._tab;self._scroll=0;self._advanced_mode=False;self._adv_scroll=0
        self._hits=[];self._r=None;self._draw_error=''
        self._pw=min(794.,max(400.,context.region.width-32.))
        self._ph=min(680.,max(350.,context.region.height-32.))
        if context.region.width<430 or context.region.height<395:
            self.report({'WARNING'},'Увеличьте область Viewport для Appearance Studio')
            return {'CANCELLED'}
        self._preferred=(pref.ui_studio_pos_x*context.region.width-self._pw/2,
                         pref.ui_studio_pos_y*context.region.height-self._ph/2)
        if not self._place(context):
            self.report({'WARNING'},'Appearance Studio: нет свободного места среди окон HUD')
            return {'CANCELLED'}
        try:
            self._handler=bpy.types.SpaceView3D.draw_handler_add(self._draw,(),'WINDOW','POST_PIXEL')
            self._sync_timer(context)
            _ACTIVE[scope]=self
            context.window_manager.modal_handler_add(self)
            self._area.tag_redraw()
            action_trace.record('APPEARANCE_OPEN','UI','Studio opened via radial menu')
            return {'RUNNING_MODAL'}
        except Exception as exc:
            self._close(context)
            self.report({'ERROR'},'Appearance Studio: '+str(exc)[:100])
            return {'CANCELLED'}

    def _motion_spec(self):
        pref=_prefs()
        if pref is None:return motion_engine.MotionSpec(mode='OFF')
        return motion_engine.MotionSpec(pref.ui_motion_quality,pref.ui_reduced_motion,
                                       max(.3,float(pref.ui_pattern_speed)))

    def _sync_timer(self,context):
        interval=self._motion_spec().fps_interval()
        if interval is None and self._closing_at is not None:
            self._close(context);return
        if interval is None and self._pending_min is not None:
            self._commit_min(context)
        if interval is None and self._timer is not None:
            try:context.window_manager.event_timer_remove(self._timer)
            except (RuntimeError,ReferenceError,ValueError,AttributeError):pass
            self._timer=None
        elif interval is not None and self._timer is None:
            self._timer=context.window_manager.event_timer_add(interval,window=self._window)

    def _begin_close(self,context):
        spec=self._motion_spec()
        self._hits=[];self._drag=False;self._active_slider=None
        if not spec.enabled:
            self._close(context)
        elif self._closing_at is None:
            self._closing_at=time.monotonic()
            self._sync_timer(context)

    def _commit_min(self,context):
        if self._pending_min is None:return
        previous=self._minimized
        self._minimized=bool(self._pending_min)
        if not self._place(context):
            self._minimized=previous
            self._place(context)
            self.report({'WARNING'},'Недостаточно места, расширьте Viewport')
        else:
            pref=_prefs()
            if pref:pref.ui_studio_collapsed=self._minimized
        self._pending_min=None
        self._min_reveal_at=time.monotonic()
        self._hits=[]

    def _valid(self,context):
        try:
            return bool(self._area and self._area.type=='VIEW_3D' and
                        self._region.type=='WINDOW' and any(win == self._window for win in context.window_manager.windows))
        except (RuntimeError,ReferenceError,AttributeError):return False

    def _blockers(self):
        return [row['rect'] for row in window_visibility.snapshot(self._scope,active_within=30.)
                if row['key']!='APPEARANCE_STUDIO' and row['rect'] and not row['hidden']]

    def _current_signature(self):
        blockers=tuple(sorted(tuple(round(v,2) for v in r) for r in self._blockers()))
        return (self._region.width,self._region.height,blockers)

    def _reflow_if_needed(self,context):
        signature=self._current_signature()
        if signature==getattr(self,'_layout_signature',None):return True
        if self._region.width<430 or self._region.height<395:
            self._hits=[];self._r=None
            return False
        self._pw=min(794.,max(400.,self._region.width-32.))
        self._ph=min(680.,max(350.,self._region.height-32.))
        return self._place(context)

    def _place(self,context,preferred=None):
        w=self._pw; h=62. if self._minimized else self._ph
        viewport=(self._region.width,self._region.height)
        preferred=preferred if preferred is not None else self._preferred
        # Global strict allocator considers existing HUDs. Render & click use ONLY this rect.
        rect=window_manager.place_unobstructed(viewport,'APPEARANCE_STUDIO',(w,h),preferred,
                   blockers=self._blockers(),scope=self._scope,margin=7.,gap=9.)
        if rect is None:
            self._hits=[]
            return False
        self._r=rect
        self._preferred=(rect[0],rect[1])
        self._layout_signature=self._current_signature()
        return True

    def _close(self,context=None):
        if getattr(self,'_closed',False):return
        self._closed=True;self._hits=[];self._r=None
        if hasattr(self,'_fps_stats') and len(self._fps_stats.draw_times)>=3:
            perf=self._fps_stats.snapshot()
            action_trace.record('APPEARANCE_FRAME_STATS','UI',
                f"draw_fps={perf['draw_fps']} cpu_ms={perf['cpu_draw_ms']} "+
                f"decorative_hz={perf['pattern_hz']} samples={perf['frames']}")
        if hasattr(self,'_render_cache') and self._render_cache.aa_error:
            action_trace.record('HUD_QUALITY_FALLBACK','UI',self._render_cache.aa_error)
        if hasattr(self,'_motion'):self._motion.clear()
        if hasattr(self,'_render_cache'):self._render_cache.clear()
        # Shared background caches belong to the current region and survive
        # this modal window. They are cleared by addon unregister / LRU.
        if hasattr(self,'_fps_stats'):self._fps_stats.clear()
        if _ACTIVE.get(self._scope) is self:_ACTIVE.pop(self._scope,None)
        window_visibility.forget(self._scope,'APPEARANCE_STUDIO')
        if self._handler is not None:
            try:bpy.types.SpaceView3D.draw_handler_remove(self._handler,'WINDOW')
            except (RuntimeError,ReferenceError,ValueError):pass
            self._handler=None
        if self._timer is not None:
            try:(context or bpy.context).window_manager.event_timer_remove(self._timer)
            except (RuntimeError,ReferenceError,ValueError,AttributeError):pass
            self._timer=None
        try:self._area.tag_redraw()
        except (ReferenceError,RuntimeError):pass

    def _persist_position(self):
        pref=_prefs()
        if pref and self._r:
            x0,y0,x1,y1=self._r
            pref.ui_studio_pos_x=max(0.,min(1.,(x0+x1)*.5/self._region.width))
            pref.ui_studio_pos_y=max(0.,min(1.,(y0+y1)*.5/self._region.height))

    def _hit(self,rect,action,data=None):
        if self._closing_at is not None or self._pending_min is not None:return
        if getattr(self,'_content_locked',False) and action in ('pattern','surface','swatch','slider'):
            return
        self._hits.append((rect,action,data))

    def _button(self,p,rect,label,action,data=None,active=False,small=False):
        x,y,w,h=rect
        theme=getattr(self,'_frame_theme',None) or theme_engine.from_preferences(_prefs())
        now=getattr(self,'_frame_now',None) or time.monotonic()
        hovered=self._hover==(action,data)
        spec=getattr(self,'_frame_spec',None) or self._motion_spec()
        hover_phase=self._motion.sample(('hover',action,str(data)),hovered,now,spec.duration('hover'))
        press_phase=motion_engine.envelope(self._press_at,now,spec.duration('press')) if self._press_at is not None and self._press_key==(action,data) else 0.
        visual=_viewport_skin.component('button','active' if active else 'normal',
                                        'neutral',theme)
        hovered_visual=_viewport_skin.component('button','hover','neutral',theme)
        # Continuous transition preserves Appearance Studio's accepted hover
        # motion while using exactly the same shared component palette.
        bg=visual.background if active else _mix_ui_color(visual.background,hovered_visual.background,hover_phase)
        edge=visual.border if active else _mix_ui_color(visual.border,hovered_visual.border,
                                                       min(1.,hover_phase+.12*press_phase))
        idle_ink=_viewport_skin.control_ink(theme,part='button',state='normal')
        hover_ink=_viewport_skin.control_ink(theme,part='button',state='hover')
        fg=(_viewport_skin.control_ink(theme,part='button',state='active') if active
            else _mix_ui_color(idle_ink,hover_ink,hover_phase))
        p.box(x,y,w,h,bg,edge,rad=_viewport_skin.CORNER_PX['button'])
        if active:
            p.box(x+10,y+2,max(2,w-20),2.0,(*theme.accent,.64),None,rad=1.)
        p.text(label,x+w/2,y+h/2-(4 if small else 5),
               _viewport_skin.text_px('caption') if small else _viewport_skin.text_px('body'),
               fg,width=w-10,align='center')
        self._hit(rect,action,data)

    def _draw(self):
        try:
            context=bpy.context
            if self._closed or context.area!=self._area or context.region!=self._region:return
            if window_visibility.is_hidden(self._scope,'APPEARANCE_STUDIO'):
                self._hits=[];return
            if not self._r:return
            started=time.monotonic()
            self._render(context)
            self._fps_stats.sample(started,time.monotonic())
            if self._closing_at is not None or self._pending_min is not None:self._hits=[]
        except Exception as exc:
            if not self._draw_error:
                self._draw_error=repr(exc)
                action_trace.record('APPEARANCE_DRAW_ERROR','UI',self._draw_error[:220])
            self._hits=[]

    def _render(self,context):
        pref=_prefs()
        if pref is None:return
        theme=theme_engine.from_preferences(pref)
        x,y,x2,y2=self._r;w=x2-x;h=y2-y
        self._render_cache.max_patterns=hud_quality.resolve(theme.render_quality).max_cached_patterns
        p=Painter(cache=self._render_cache)
        self._hits=[]
        spec=self._motion_spec();now=time.monotonic()
        self._frame_theme=theme;self._frame_spec=spec;self._frame_now=now
        entry=motion_engine.ease_out_cubic(motion_engine.progress(self._started,now,spec.duration('enter')))
        exit_factor=1.-motion_engine.ease_in_out_cubic(motion_engine.progress(self._closing_at,now,spec.duration('exit'))) if self._closing_at is not None else 1.
        p.opacity=max(0.,min(1.,entry*exit_factor))
        self._content_locked=False
        try:
            # Panel shell: opaque enough for dense Russian labels on patterned surfaces.
            # Cosmetic shell lighting follows motion phase, never position/size.
            shell_glow=.015*motion_engine.envelope(self._started,now,max(.01,spec.duration('enter')))
            shell=_viewport_skin.shell(theme)
            p.box(x,y,w,h,shell['panel'],(*shell['border'][:3],min(.99,.9+shell_glow)),rad=15)
            # Restrained procedural material underneath all controls.
            from dataclasses import replace
            quiet=replace(theme,pattern_strength=theme.pattern_strength*.12)
            if not self._minimized and h>160:
                elapsed=frame_diagnostics.FrameDiagnostics.quantized_seconds(
                    now-self._started,self._fps_stats.decoration_hz())
                phase=elapsed*.48*theme.pattern_speed*theme.pattern_amplitude if spec.enabled else 0.
                # Very subtle footer relief, never an all-over wireframe.
                p.pattern(theme.pattern,x+14,y+57,w-28,min(51.,h*.10),quiet,phase,preview=True)
            p.box(x+5,y+h-54,w-10,47,shell['header'],(*shell['border'][:3],.38),rad=11)
            p.text('ISSMAKER  /  APPEARANCE STUDIO',x+23,y+h-36,15,shell['title'],width=w-145)
            self._button(p,(x+w-88,y+h-46,34,30),'−','min')
            self._button(p,(x+w-48,y+h-46,34,30),'×','close')
            self._hit((x+12,y+h-55,w-115,54),'drag')
            if self._minimized:return
            if self._advanced_mode:
                self._render_advanced(p,theme,x,y,w,h)
                return
            full_opacity=p.opacity
            if self._pending_min is True:
                q=motion_engine.progress(self._min_at,now,spec.duration('minimize'))
                p.opacity*=1.-motion_engine.ease_in_out_cubic(q)
            elif self._min_reveal_at is not None:
                q=motion_engine.progress(self._min_reveal_at,now,spec.duration('restore'))
                p.opacity*=motion_engine.ease_out_cubic(q)
                if q>=1.:self._min_reveal_at=None
            # Top tabs intentionally avoid sidebars; all customization lives in Studio.
            tabs=(('GALLERY','ФОНЫ'),('DESIGN','ОФОРМЛЕНИЕ'),('TOOLS','ПРИМЕРЫ'))
            for i,(name,title) in enumerate(tabs):
                left=x+18+i*(w-36)/3
                self._button(p,(left,y+h-100,(w-42)/3-5,34),title,'tab',name,active=self._tab==name)
            tab_elapsed=motion_engine.progress(self._tab_at,now,spec.duration('tab'))
            current_index=next((i for i,(key,_) in enumerate(tabs) if key==self._tab),0)
            before_index=next((i for i,(key,_) in enumerate(tabs) if key==self._tab_previous),current_index)
            selected_index=motion_engine.interpolate(before_index,current_index,tab_elapsed,'in_out')
            under_x=x+18+selected_index*(w-36)/3
            p.box(under_x+8,y+h-102,(w-42)/3-21,2.,(*theme.accent,.90),None,rad=1.)
            prev=(x+18,y+h-229,w-36,117)
            p.box(*prev,(*theme.header,.92),(*theme.border,.55),rad=10)
            label_area,canvas=appearance_layout.hero_regions(*prev)
            p.box(*canvas,(*theme.panel,.98),(*theme.border,.23),rad=8)
            elapsed=frame_diagnostics.FrameDiagnostics.quantized_seconds(
                now-self._started,self._fps_stats.decoration_hz())
            phase=elapsed*.65*theme.pattern_speed*theme.pattern_amplitude
            if theme.motion=='OFF':phase=0.
            pattern_phase=motion_engine.progress(self._pattern_change_at,now,spec.duration('tab')) if self._pattern_change_at is not None else 1.
            sample_theme=replace(theme,pattern_strength=theme.pattern_strength*.86)
            if self._pattern_prev is not None and pattern_phase<1.:
                before=p.opacity
                p.opacity*=1.-motion_engine.ease_in_out_cubic(pattern_phase)
                p.pattern(self._pattern_prev,*canvas,sample_theme,phase)
                p.opacity=before*motion_engine.ease_in_out_cubic(pattern_phase)
                p.pattern(theme.pattern,*canvas,sample_theme,phase)
                p.opacity=before
            else:
                self._pattern_prev=None
                p.pattern(theme.pattern,*canvas,sample_theme,phase)
            p.text('АКТИВНЫЙ ФОН',label_area[0]+4,prev[1]+83,10,shell['muted'])
            p.text(pattern_engine.NAMES.get(theme.pattern,'Без фона'),label_area[0]+4,prev[1]+54,20,shell['title'],width=label_area[2]-14)
            p.text(theme.surface.title()+'  ·  '+theme.render_quality, label_area[0]+4,prev[1]+28,10,shell['muted'],width=label_area[2]-13)
            # Pause is part of the preview's own chrome, away from all title text.
            self._button(p,(canvas[0]+canvas[2]-71,canvas[1]+canvas[3]-27,64,22),'ПАУЗА' if theme.motion!='OFF' else 'ПУСК','pause',small=True)
            # Only tab content fades. Its locations/hit rectangles never drift.
            previous_opacity=p.opacity
            p.opacity*=motion_engine.ease_out_cubic(tab_elapsed)
            self._content_locked=tab_elapsed<.48
            if self._tab=='GALLERY': self._draw_gallery(p,theme,x,y,w,h,phase)
            elif self._tab=='DESIGN': self._draw_design(p,theme,x,y,w,h)
            else:self._draw_tools(p,theme,x,y,w,h,phase)
            self._content_locked=False
            p.opacity=previous_opacity
            p.box(x+12,y+10,w-24,38,(*theme.header,.72),None,rad=8)
            p.text('Колёсико: каталог   ·   Заголовок: перемещение   ·   Esc: закрыть',
                   x+25,y+23,10,shell['muted'],width=w-220)
            if getattr(pref,'ui_debug_bounds',False):
                stat=self._fps_stats.snapshot()
                aa='AA' if theme.render_quality!='PERFORMANCE' and not self._render_cache.aa_error else 'Legacy'
                from . import hud_gpu_runtime
                shared=hud_gpu_runtime.snapshot()
                p.text(f"{theme.render_quality} · {aa} · {stat['draw_fps']:.0f} FPS · "
                       f"{stat['cpu_draw_ms']:.1f} мс CPU · "
                       f"Cache {shared.get('cache_hits',0)}/{shared.get('cache_misses',0)} "
                       f"Bkg {shared.get('background_avg_cpu_ms',0):.1f}ms",
                       x+23,y+h-49,9,shell['accent'],width=w-160)
            self._button(p,(x+w-172,y+15,149,27),'ТОЧНЫЕ НАСТРОЙКИ','advanced',small=True)
            p.opacity=full_opacity
        finally:p.close()

    def _render_advanced(self,p,theme,x,y,w,h):
        """Premium material controls inside the same GPU window, with real hitboxes.

        Native Blender picker is retained for direct colour editing and as an
        accessibility fallback.  Scroll merely changes visible UI rows.
        """
        pref=_prefs()
        if pref is None:return
        skin=_viewport_skin.shell(theme)
        p.box(x+13,y+h-119,w-26,57,(*theme.header,.97),(*theme.border,.54),rad=10)
        p.box(x+17,y+h-113,3,43,(*theme.accent,.88),None,rad=1.5)
        p.text('ТОЧНЫЕ НАСТРОЙКИ  /  MATERIAL LAB',x+32,y+h-84,15,skin['title'],width=w-210)
        p.text('Свойства материала · GPU-превью · мгновенное применение',x+32,y+h-104,10,skin['muted'],width=w-210)
        self._button(p,(x+w-128,y+h-106,110,30),'← НАЗАД','advanced_back',small=True)

        # Material cards: the selected surface affects all ISSMAKER tools.
        ycards=y+h-180; spacing=7.;cardw=(w-42)/5
        for i,name in enumerate(theme_engine.SURFACES):
            bx=x+18+i*cardw
            active=theme.surface==name
            base=(*theme.header[:3],.96) if active else (*theme.panel[:3],.96)
            p.box(bx,ycards,cardw-6,45,base,(*theme.accent,.83) if active else (*theme.border,.42),rad=8)
            # Multi-layer material swatch: subtle upper sheen + inset emboss.
            p.box(bx+4,ycards+4,cardw-14,35,(*theme.secondary,.065),None,rad=5)
            p.box(bx+8,ycards+29,cardw-22,4,(*theme.accent,.8) if active else (*theme.secondary,.4),None,rad=2)
            short={'architectural':'ARCH','graphite':'GRAPH','mineral':'MINERAL','smoke':'SMOKE','ink':'INK'}
            p.text(name.title() if cardw>=104 else short.get(name,name.upper()),bx+8,ycards+11,10,skin['title'],width=cardw-19)
            self._hit((bx,ycards,cardw-6,45),'surface',name)

        # Polished inset surface under parameter rows; no excessive glow.
        content_bottom=y+58;content_top=ycards-10
        p.box(x+16,content_bottom,w-32,max(35,content_top-content_bottom),
              (*theme.panel[:3],.985),(*theme.border[:3],.56),rad=10)
        p.box(x+20,content_top-5,w-40,2,(*theme.accent,.37),None,rad=1)
        rows=[
            ('section','МАТЕРИАЛ И СВЕТ'),
            ('enum','ui_scheme','Цветовая схема',tuple(theme_engine.SCHEMES)),
            ('enum','ui_pattern','Процедурный фон',tuple(theme_engine.PATTERNS)),
            ('enum','ui_render_quality','Качество HUD',('ULTRA','HIGH','BALANCED','PERFORMANCE')),
            ('slider','ui_pattern_strength','Яркость материала',0.,1.),
            ('slider','ui_pattern_opacity','Прозрачность рисунка',0.,1.),
            ('slider','ui_pattern_relief','Рельеф',0.,1.),
            ('slider','ui_pattern_glow','Свечение',0.,1.),
            ('slider','ui_pattern_contrast','Контраст',.1,2.),
            ('slider','ui_pattern_softness','Мягкость',0.,1.),
            ('section','ГЕОМЕТРИЯ РИСУНКА'),
            ('slider','ui_pattern_scale','Масштаб фона',.25,4.),
            ('slider','ui_pattern_density','Плотность',.25,2.5),
            ('slider','ui_pattern_amplitude','Амплитуда',0.,2.),
            ('slider','ui_pattern_parallax','Параллакс',0.,1.),
            ('slider','ui_pattern_speed','Скорость',0.,3.),
            ('section','ОТОБРАЖЕНИЕ И УДОБСТВО'),
            ('enum','hud_display_preset','Монитор',('AUTO','QHD','UHD')),
            ('slider','font_scale','Шрифт HUD',.75,2.),
            ('slider','window_scale','Размер HUD',.75,2.),
            ('enum','ui_motion_quality','Анимация',('OFF','BALANCED','RICH')),
            ('toggle','ui_reduced_motion','Снизить движение'),
            ('toggle','ui_custom_accent','Свой цвет'),
            ('toggle','ui_debug_bounds','Границы UI'),
        ]
        step=36; available=max(1,int((content_top-content_bottom-16)//step))
        self._adv_scroll=max(0,min(self._adv_scroll,max(0,len(rows)-available)))
        for i,row in enumerate(rows[self._adv_scroll:self._adv_scroll+available]):
            ry=content_top-12-(i+1)*step
            kind=row[0]
            if kind=='section':
                p.text(row[1],x+28,ry+10,10,skin['accent'],width=w-55)
                p.box(x+28,ry+5,w-56,1,(*theme.border,.52),None,rad=0)
                continue
            field,label=row[1:3]
            if i%2==0:p.box(x+23,ry-3,w-46,33,(*theme.header[:3],.32),None,rad=6)
            p.text(label,x+31,ry+10,11,skin['title'],width=w*.43)
            rx=x+min(230,w*.52);rw=max(100,w-(rx-x)-29)
            if kind=='slider':
                lo,hi=row[3:5];value=float(getattr(pref,field))
                track=(rx+9,ry+12,rw-73,5)
                u=max(0.,min(1.,(value-lo)/(hi-lo)))
                p.box(*track,(*theme.secondary,.48),None,rad=2)
                p.box(track[0],track[1],max(2.,track[2]*u),5,(*theme.accent,.95),None,rad=2)
                p.box(track[0]+track[2]*u-4,ry+9,9,11,(*theme.accent,1.),None,rad=4)
                p.text(f'{value:.2f}',rx+rw-6,ry+9,11,skin['title'],align='right')
                self._hit((rx,ry,rw,30),'slider',(field,lo,hi,track[0],track[2]))
            elif kind=='enum':
                values=row[3];current=str(getattr(pref,field)).upper()
                display=pattern_engine.NAMES.get(current.lower(),current.replace('_',' ').title())
                p.box(rx,ry+1,rw,29,(*theme.header,.97),(*theme.border,.65),rad=6)
                p.text(display,rx+10,ry+10,11,skin['title'],width=rw-30)
                p.text('›',rx+rw-17,ry+9,13,skin['accent'])
                self._hit((rx,ry+1,rw,29),'advanced_cycle',(field,values))
            elif kind=='toggle':
                value=bool(getattr(pref,field))
                self._button(p,(rx+rw-89,ry+1,86,28),'ВКЛ' if value else 'ВЫКЛ','advanced_toggle',field,active=value,small=True)
        p.box(x+12,y+10,w-24,39,(*theme.header,.95),(*theme.border,.5),rad=8)
        p.text(f'МАТЕРИАЛ · {theme.surface.upper()}   /   {self._adv_scroll+1}–{min(len(rows),self._adv_scroll+available)} из {len(rows)}',
               x+22,y+24,10,skin['muted'],width=w-190)
        self._button(p,(x+w-166,y+15,146,29),'ЦВЕТА / BLENDER','advanced_native',small=True)

    def _draw_gallery(self,p,theme,x,y,w,h,phase):
        start_y=y+h-244
        area_bottom=y+57
        columns=3 if w>=710 else 2
        gutter=12.;size=(w-36-(columns-1)*gutter)/columns
        card_h=min(133.,max(76.,start_y-area_bottom-7.)); stride=card_h+11
        count=max(1,int((start_y-area_bottom)//stride))
        catalog=pattern_engine.PATTERN_CATALOG+(('none','Без фона'),)
        max_rows=max(0,math.ceil(len(catalog)/columns)-count)
        self._scroll=min(self._scroll,max_rows)
        for row in range(count):
            for col in range(columns):
                index=(row+self._scroll)*columns+col
                if index>=len(catalog):continue
                ident,name=catalog[index]
                cx=x+18+col*(size+gutter);cy=start_y-(row+1)*stride+11
                if cy<area_bottom-1:continue
                selected=(theme.pattern==ident)
                prior_opacity=p.opacity
                elapsed=motion_engine.progress(self._tab_at,time.monotonic(),self._motion_spec().duration('tab'))
                stagger=max(0.,min(1.,(elapsed-.07*row)/(1.-.07*row)))
                p.opacity*=motion_engine.ease_out_cubic(stagger)
                hover=self._motion.sample(('card',ident),self._hover==('pattern',ident),time.monotonic(),self._motion_spec().duration('hover'))
                p.box(cx,cy,size,card_h,(*theme.header,.98),(*theme.accent,.62) if selected else (*theme.border,.26+.27*hover),rad=10)
                p.box(cx+5,cy+29,size-10,card_h-35,(*theme.panel,.99),(*theme.border,.18),rad=7)
                # Only selected/hovered cards animate geometry. Every other
                # thumbnail stays GPU cached, so 40-rich gallery remains fast.
                # Hit rectangles are independent of procedural geometry.
                preview_phase=phase if (selected or self._hover==('pattern',ident)) and theme.motion!='OFF' else 0.
                card_theme=replace(theme,pattern_strength=theme.pattern_strength*.87)
                p.pattern(ident,cx+7,cy+31,size-14,card_h-39,card_theme,preview_phase,preview=True)
                if selected:
                    p.box(cx+7,cy+card_h-8,size-14,2.,(*theme.accent,.78),None,rad=1.)
                p.text(name,cx+12,cy+10,11,_viewport_skin.ink(theme),width=size-24)
                self._hit((cx,cy,size,card_h),'pattern',ident)
                p.opacity=prior_opacity
        if max_rows:
            p.text(f'Коллекция · {len(pattern_engine.PATTERN_CATALOG)} премиальных фонов · ряд {self._scroll+1}/{max_rows+1}',x+22,y+56,10,_viewport_skin.ink(theme,kind='muted'))

    def _draw_design(self,p,theme,x,y,w,h):
        pref=_prefs();top=y+h-256
        p.text('МАТЕРИАЛ ВНЕШНЕЙ ПАНЕЛИ',x+22,top,11,_viewport_skin.ink(theme,kind='muted'))
        sw=(w-46)/5
        for i,name in enumerate(theme_engine.SURFACES):
            self._button(p,(x+20+i*sw,top-49,sw-5,36),name.title(),'surface',name,active=theme.surface==name,small=True)
        p.text('ЦВЕТ АКЦЕНТА',x+22,top-81,11,_viewport_skin.ink(theme,kind='muted'))
        for i,color in enumerate(SWATCHES if top-123>y+56 else ()):
            bx=x+22+i*min(46,(w-48)/10);by=top-123
            p.box(bx,by,31,30,(*color,1),(*theme.accent,.9) if theme.accent==color else (.75,.85,.83,.24),rad=7)
            self._hit((bx,by,31,30),'swatch',i)
        # Slider positions are encoded in hit data and never recomputed differently during events.
        params=(('ui_pattern_strength','Яркость',0.,1.),('ui_pattern_scale','Масштаб',.25,4.),
                ('ui_pattern_speed','Скорость',0.,3.),('ui_pattern_density','Плотность',.25,2.5))
        for i,(field,title,lo,hi) in enumerate(params):
            sy=top-162-i*45
            if sy<y+81:break
            p.text(title,x+22,sy+8,12,_viewport_skin.ink(theme))
            track=(x+min(165,w*.33),sy+11,w-min(250,w*.49),5)
            value=getattr(pref,field)
            proportion=min(1.,max(0.,(value-lo)/(hi-lo)))
            p.box(*track,(*theme.secondary,.33),None,rad=3)
            p.box(track[0],track[1],max(3,track[2]*proportion),track[3],(*theme.accent,.85),None,rad=3)
            p.text('%0.2f'%value,x+w-31,sy+7,10,_viewport_skin.ink(theme),align='right')
            self._hit((track[0]-8,sy-4,track[2]+16,30),'slider',(field,lo,hi,track[0],track[2]))

    def _draw_tools(self,p,theme,x,y,w,h,phase):
        top=y+h-242;cellw=(w-52)/2;cellh=min(162,(h-324)/2)
        if cellh<114:
            p.box(x+20,y+100,w-40,70,(*theme.header,.8),(*theme.border,.5),rad=9)
            p.text('Для предпросмотров расширьте Viewport или увеличьте высоту окна',
                   x+33,y+130,12,_viewport_skin.ink(theme),width=w-70)
            return
        for i,(key,title,sub) in enumerate(SAMPLE_TOOLS):
            col=i%2; row=i//2
            bx=x+18+col*(cellw+15);by=top-(row+1)*(cellh+9)
            p.box(bx,by,cellw,cellh,(*theme.header,.94),(*theme.border,.5),rad=10)
            p.pattern(theme.pattern,bx+8,by+cellh-75,cellw-16,27,replace(theme,pattern_strength=theme.pattern_strength*.55),0.,preview=True)
            p.box(bx+10,by+cellh-43,cellw-20,31,(*theme.panel,.96),None,rad=7)
            p.text(title,bx+23,by+cellh-33,13,_viewport_skin.ink(theme),width=cellw-47)
            p.text(sub,bx+15,by+cellh-71,11,_viewport_skin.ink(theme,kind='muted'),width=cellw-30)
            # Representative controls visually match the unified shell; never run tools.
            for k in range(3):
                off=bx+15+k*(cellw-30)/3
                p.box(off,by+22,(cellw-48)/3,27,(*theme.panel,.95),(*theme.accent,.35),rad=5)
                p.text(('0,08','4,00','1000')[k],off+8,by+30,10,_viewport_skin.ink(theme),width=(cellw-65)/3)
        p.text('Предпросмотры не запускают инструменты и не меняют геометрию',x+22,y+54,10,_viewport_skin.ink(theme,kind='muted'),width=w-42)

    def _action(self,context,action,data,px):
        pref=_prefs()
        if pref is None:return
        if action=='close':self._begin_close(context);return
        if action=='min':
            self._pending_min=not self._minimized
            self._min_at=time.monotonic()
            if not self._motion_spec().enabled:self._commit_min(context)
        elif action=='tab':
            self._tab_previous=self._tab;self._tab=data;self._tab_at=time.monotonic();pref.ui_studio_tab=data;self._scroll=0
        elif action=='pattern':
            if pref.ui_pattern!=data.upper():
                self._pattern_prev=pref.ui_pattern.lower()
                self._pattern_change_at=time.monotonic()
            pref.ui_pattern=data.upper()
        elif action=='surface':pref.ui_surface=data.upper()
        elif action=='swatch':
            pref.ui_accent_color=SWATCHES[data];pref.ui_custom_accent=True
        elif action=='pause':
            pref.ui_motion_quality='OFF' if pref.ui_motion_quality!='OFF' else 'BALANCED'
            self._sync_timer(context)
        elif action=='slider':
            field,lo,hi,left,length=data
            setattr(pref,field,round(lo+(hi-lo)*max(0.,min(1.,(px-left)/length)),3))
        elif action=='advanced':
            self._advanced_mode=True;self._adv_scroll=0
        elif action=='advanced_back':
            self._advanced_mode=False;self._adv_scroll=0
        elif action=='advanced_cycle':
            field,values=data;values=tuple(str(v).upper() for v in values)
            current=str(getattr(pref,field)).upper()
            setattr(pref,field,values[(values.index(current)+1)%len(values)] if current in values else values[0])
        elif action=='advanced_toggle':
            setattr(pref,data,not bool(getattr(pref,data)))
        elif action=='advanced_native':
            try:bpy.ops.ima.appearance_advanced('INVOKE_DEFAULT')
            except (RuntimeError,ValueError,AttributeError) as exc:
                self.report({'WARNING'},'Настройки недоступны: '+str(exc)[:90])
        self._press_at=time.monotonic()
        self._press_key=(action,data)
        try:self._area.tag_redraw()
        except (RuntimeError,ReferenceError):pass

    def modal(self,context,event):
        if self._closed:return {'FINISHED'}
        if not self._valid(context):
            self._close(context);return {'CANCELLED'}
        if (event.type=='ESC' and event.value=='PRESS') or event.type=='WINDOW_DEACTIVATE':
            self._begin_close(context)
            return {'RUNNING_MODAL'} if not self._closed else {'CANCELLED'}
        self._sync_timer(context)
        if self._closed:return {'FINISHED'}
        if self._closing_at is not None:
            if motion_engine.progress(self._closing_at,time.monotonic(),self._motion_spec().duration('exit'))>=1.:
                self._close(context);return {'FINISHED'}
            return {'PASS_THROUGH'}
        if self._pending_min is not None:
            if motion_engine.progress(self._min_at,time.monotonic(),self._motion_spec().duration('minimize'))>=1.:
                self._commit_min(context)
            else:return {'PASS_THROUGH'}
        if event.type=='TIMER':
            if getattr(event,'timer',None) != self._timer:return {'PASS_THROUGH'}
            if not window_visibility.is_hidden(self._scope,'APPEARANCE_STUDIO'):
                try:self._area.tag_redraw()
                except (ReferenceError,RuntimeError):pass
            return {'PASS_THROUGH'}
        if self._draw_error:
            self._close(context)
            self.report({'ERROR'},'Ошибка отрисовки Appearance Studio: '+self._draw_error[:95])
            return {'CANCELLED'}
        if not window_visibility.is_hidden(self._scope,'APPEARANCE_STUDIO') and not self._reflow_if_needed(context):
            self.report({'WARNING'},'Appearance Studio: недостаточно свободного места')
            self._close(context)
            return {'CANCELLED'}
        if not self._r or window_visibility.is_hidden(self._scope,'APPEARANCE_STUDIO'):
            return {'PASS_THROUGH'}
        px=event.mouse_x-self._region.x;py=event.mouse_y-self._region.y
        inside=self._r[0]<=px<=self._r[2] and self._r[1]<=py<=self._r[3]
        if event.type in {'WHEELDOWNMOUSE','WHEELUPMOUSE'} and inside:
            if self._advanced_mode:
                self._adv_scroll=max(0,self._adv_scroll+(1 if event.type=='WHEELDOWNMOUSE' else -1))
                self._area.tag_redraw();return {'RUNNING_MODAL'}
            if self._tab=='GALLERY':
                columns=3 if self._pw>=710 else 2
                y0=self._r[1];height=self._r[3]-y0
                space=(y0+height-244)-(y0+57)
                card_height=min(133.,max(76.,space-7.))
                count=max(1,int(space/(card_height+11.)))
                max_rows=max(0,math.ceil((len(pattern_engine.PATTERN_CATALOG)+1)/columns)-count)
                self._scroll=max(0,min(max_rows,self._scroll+(1 if event.type=='WHEELDOWNMOUSE' else -1)))
            self._area.tag_redraw();return {'RUNNING_MODAL'}
        if event.type=='MOUSEMOVE':
            if self._drag:
                old=self._r
                requested=(self._drag_orig[0]+event.mouse_x-self._drag_mouse[0],
                           self._drag_orig[1]+event.mouse_y-self._drag_mouse[1])
                self._place(context,requested)
                self._area.tag_redraw()
                return {'RUNNING_MODAL'}
            if self._active_slider:
                self._action(context,'slider',self._active_slider,px)
                return {'RUNNING_MODAL'}
            h=next(((a,d) for rect,a,d in reversed(self._hits) if within(rect,(px,py))),None)
            if h!=self._hover:self._hover=h;self._hover_at=time.monotonic();self._area.tag_redraw()
            return {'RUNNING_MODAL'} if inside else {'PASS_THROUGH'}
        if event.type=='LEFTMOUSE' and event.value=='RELEASE':
            if self._drag:
                self._drag=False;self._persist_position();return {'RUNNING_MODAL'}
            if self._active_slider:
                self._action(context,'slider',self._active_slider,px)
                self._active_slider=None
                return {'RUNNING_MODAL'}
        if event.type=='LEFTMOUSE' and event.value=='PRESS' and inside:
            for rect,action,data in reversed(self._hits):
                if within(rect,(px,py)):
                    if action=='drag':
                        self._drag=True
                        self._drag_mouse=(event.mouse_x,event.mouse_y)
                        self._drag_orig=self._preferred
                    elif action=='slider':
                        self._active_slider=data
                        self._action(context,action,data,px)
                    else:self._action(context,action,data,px)
                    return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'} if inside else {'PASS_THROUGH'}


def close_all():
    for editor in tuple(_ACTIVE.values()):
        try:editor._close(bpy.context)
        except (RuntimeError,ReferenceError,AttributeError):pass
    _ACTIVE.clear()


def has_active():
    return any(not editor._closed and not window_visibility.is_hidden(editor._scope,'APPEARANCE_STUDIO')
               for editor in _ACTIVE.values())
