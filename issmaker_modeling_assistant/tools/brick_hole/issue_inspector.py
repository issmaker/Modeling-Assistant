"""Read-only contour collision navigator for Brick Hole (Pass 06.5).

All issue records are immutable world-space numbers.  No BMVert/BMEdge, Mesh or
Blender Object references persist across the transaction rollback, Undo or Esc.
The viewport overlay uses the same RED marks / issue navigation vocabulary as
Collision Inspector but owns independent handlers and lifetime.
"""
import math
import bpy
from mathutils import Vector

_ISSUES = []
_ACTIVE = None
_MAX_ISSUES = 64
_LAST_DRAW_ERROR = ''


def diagnostics():
    return {'active':is_active(),'count':len(_ISSUES),'last_draw_error':_LAST_DRAW_ERROR}


def _closest_points(a, b, c, d):
    """Clamped nearest points on 3D line segments, pure numeric."""
    u = [b[i]-a[i] for i in range(3)]
    v = [d[i]-c[i] for i in range(3)]
    w = [a[i]-c[i] for i in range(3)]
    aa=sum(x*x for x in u); bb=sum(u[i]*v[i] for i in range(3))
    cc=sum(x*x for x in v); dd=sum(u[i]*w[i] for i in range(3))
    ee=sum(v[i]*w[i] for i in range(3)); denominator=aa*cc-bb*bb
    if aa<=1e-24 or cc<=1e-24:
        raise ValueError('Collision marker requires non-zero segments')
    sn=sd=denominator;tn=td=denominator
    if denominator<=1e-24*max(aa*cc,1e-24):
        sn,sd=0.0,1.0
        tn,td=ee,cc
    else:
        sn=bb*ee-cc*dd
        tn=aa*ee-bb*dd
        if sn<0:
            sn,tn,td=0.0,ee,cc
        elif sn>sd:
            sn,tn,td=sd,ee+bb,cc
    if tn<0:
        tn=0.0
        if -dd<0:sn=0.0
        elif -dd>aa:sn,sd=1.0,1.0
        else:sn,sd=-dd,aa
    elif tn>td:
        tn=td
        if -dd+bb<0:sn=0.0
        elif -dd+bb>aa:sn,sd=1.0,1.0
        else:sn,sd=-dd+bb,aa
    sc=0.0 if abs(sn)<1e-24 else sn/sd
    tc=0.0 if abs(tn)<1e-24 else tn/td
    p=tuple(a[i]+sc*u[i] for i in range(3))
    q=tuple(c[i]+tc*v[i] for i in range(3))
    return p,q,math.dist(p,q)


def capture_collision(a, b, c, d, reason='Самопересечение контура'):
    """Capture a geometric witness; never retain input topology identities."""
    points=tuple(tuple(float(v) for v in p) for p in (a,b,c,d))
    if any(not all(math.isfinite(v) for v in p) for p in points):
        return False
    try:p,q,distance=_closest_points(*points)
    except ValueError:return False
    middle=tuple((p[i]+q[i])*0.5 for i in range(3))
    if len(_ISSUES)<_MAX_ISSUES:
        _ISSUES.append({'segments':(points[:2],points[2:]),'point':middle,
                        'distance_m':distance,'reason':str(reason)[:110]})
    return True


def issues():
    return tuple(_ISSUES)


def clear():
    stop()
    _ISSUES.clear()


def is_active():
    return _ACTIVE is not None


def stop():
    active=_ACTIVE
    if active is not None:
        active._finish()


class VIEW3D_OT_brick_hole_issue_inspector(bpy.types.Operator):
    bl_idname='view3d.brick_hole_issue_inspector'
    bl_label='Инспектор пересечений проёмов'
    bl_description='Показать столкнувшиеся сегменты и перейти к проблеме'
    bl_options={'INTERNAL'}

    @classmethod
    def poll(cls,context):
        return bool(_ISSUES and context.area and context.area.type=='VIEW_3D')

    def invoke(self,context,event):
        global _ACTIVE
        stop()
        self._handler_3d=None;self._handler_2d=None;self._timer=None
        self._area=context.area;self._index=0;self._alive=True;self._card_visible=True
        try:
            self._handler_3d=bpy.types.SpaceView3D.draw_handler_add(self._draw_world,(), 'WINDOW','POST_VIEW')
            self._handler_2d=bpy.types.SpaceView3D.draw_handler_add(self._draw_hud,(), 'WINDOW','POST_PIXEL')
            context.window_manager.modal_handler_add(self)
            _ACTIVE=self
            self._area.tag_redraw()
            return {'RUNNING_MODAL'}
        except Exception:
            self._finish()
            raise

    def _finish(self):
        global _ACTIVE
        self._alive=False
        for attr in ('_handler_3d','_handler_2d'):
            handle=getattr(self,attr,None)
            if handle is not None:
                try:bpy.types.SpaceView3D.draw_handler_remove(handle,'WINDOW')
                except (RuntimeError,ReferenceError,ValueError):pass
                setattr(self,attr,None)
        if _ACTIVE is self:_ACTIVE=None
        # Release the read-only card's shared placement immediately, not just
        # after the peer lease expires.  No user-hidden preference is changed.
        try:
            from ...core.window_visibility import invalidate_matching
            invalidate_matching('BRICK_ISSUE_CARD')
        except (ImportError,RuntimeError,ValueError):
            pass
        area=getattr(self,'_area',None)
        if area is not None:
            try:area.tag_redraw()
            except ReferenceError:pass

    def _draw_world(self):
        if not self._alive or not _ISSUES:return
        try:
            import gpu
            from gpu_extras.batch import batch_for_shader
            shader=gpu.shader.from_builtin('UNIFORM_COLOR')
            issue=_ISSUES[self._index % len(_ISSUES)]
            segments=[pt for part in issue['segments'] for pt in part]
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('NONE')
            gpu.state.line_width_set(3.0)
            batch=batch_for_shader(shader,'LINES',{'pos':segments})
            shader.bind();shader.uniform_float('color',(1.0,.25,.24,.99));batch.draw(shader)
            gpu.state.point_size_set(14.0)
            pbatch=batch_for_shader(shader,'POINTS',{'pos':[issue['point']]})
            shader.uniform_float('color',(1.0,.70,.15,1.0));pbatch.draw(shader)
        except Exception as exc:
            # Failure is read-only but MUST be visible in the support snapshot.
            global _LAST_DRAW_ERROR
            _LAST_DRAW_ERROR=f'WORLD: {type(exc).__name__}: {exc}'[:220]
        finally:
            try:
                gpu.state.point_size_set(1.0)
                gpu.state.line_width_set(1.0)
                gpu.state.depth_test_set('NONE')
                gpu.state.blend_set('NONE')
            except Exception:pass

    def _draw_hud(self):
        if not self._alive or not _ISSUES:return
        try:
            import blf
            from ...core.inspector_cards import card_bounds,wrap_for_width
            from ...core.ui_layout_scaling import from_preferences
            from ...core.studio_gpu_components import fill_rect,outline_rect
            from ...core import theme_engine
            item=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
            pref=item.preferences if item else None
            profile=from_preferences(pref)
            theme=theme_engine.from_preferences(pref)
            region=next((r for r in self._area.regions if r.type=='WINDOW'),None)
            if region is None:
                self._card_visible=False
                return
            self._card_visible=False
            bounds=card_bounds(region.width,region.height,scale=profile.safe_window)
            from ...core.window_visibility import scope_from_context, observe
            from ...core.window_manager import place_unobstructed
            scope=scope_from_context(bpy.context,area=self._area,region=region)
            if bounds is None:
                observe(scope,'BRICK_ISSUE_CARD',None)
                return
            x,y,w,h=bounds
            # Pass 15.10: the issue card is keyboard-only/read-only. Its whole
            # painted surface is reserved against DWOS/UV Wall/Collision peers.
            # The placement result owns the draw rectangle; NO_ROOM => no draw.
            placed=place_unobstructed((region.width,region.height),
                'BRICK_ISSUE_CARD',(w,h),(x,y),margin=8*profile.safe_window,
                gap=6*profile.safe_window,scope=scope)
            if placed is None:return
            x,y=placed[0],placed[1]
            self._card_visible=True
            font=0
            from ...core import viewport_skin as _viewport_skin
            from ...core import window_shell as _window_shell
            color=_window_shell.surface(theme)['panel']
            if not fill_rect(x,y,x+w,y+h,color,8*profile.safe_window):
                import gpu
                from gpu_extras.batch import batch_for_shader
                shader=gpu.shader.from_builtin('UNIFORM_COLOR')
                verts=((x,y),(x+w,y),(x+w,y+h),(x,y),(x+w,y+h),(x,y+h))
                shader.bind();shader.uniform_float('color',color)
                batch_for_shader(shader,'TRIS',{'pos':verts}).draw(shader)
            outline_rect(x,y,x+w,y+h,_window_shell.surface(theme)['border'],8*profile.safe_window)
            p=_ISSUES[self._index % len(_ISSUES)]
            blf.size(font,profile.font_px(_viewport_skin.text_px('body')))
            blf.color(font,*_viewport_skin.semantic_ink(theme,role='warning'))
            title=f'ПЕРЕСЕЧЕНИЕ {self._index+1}/{len(_ISSUES)}  ·  {p["distance_m"]:.7f} м'
            from ...core.design_tokens import fit_label
            title,_=fit_label(title,w-24*profile.safe_window,
                              lambda content:blf.dimensions(font,content)[0])
            blf.position(font,x+12*profile.safe_window,y+h-24*profile.safe_window,0)
            blf.draw(font,title)
            blf.size(font,profile.font_px(_viewport_skin.text_px('control')));blf.color(font,*_viewport_skin.ink(theme))
            maxw=w-24*profile.safe_window
            lines=wrap_for_width(p['reason'],maxw,lambda s:blf.dimensions(font,s)[0],max_lines=2)
            for i,line in enumerate(lines):
                blf.position(font,x+12*profile.safe_window,y+h-(46+i*17)*profile.safe_window,0)
                blf.draw(font,line)
            blf.size(font,profile.font_px(_viewport_skin.text_px('caption')));blf.color(font,*_viewport_skin.ink(theme,kind='muted'))
            hint='← / → Дальше    F Перейти    Esc Закрыть'
            lines=wrap_for_width(hint,maxw,lambda s:blf.dimensions(font,s)[0],max_lines=1)
            if lines:
                blf.position(font,x+12*profile.safe_window,y+11*profile.safe_window,0)
                blf.draw(font,lines[0])
        except Exception as exc:
            global _LAST_DRAW_ERROR
            _LAST_DRAW_ERROR=f'CARD: {type(exc).__name__}: {exc}'[:220]

    def modal(self,context,event):
        if not self._alive or context.area!=self._area:
            self._finish();return {'CANCELLED'}
        # A hidden-by-layout keyboard HUD must not keep trapping input.
        if not getattr(self,'_card_visible',True):
            self._finish();return {'PASS_THROUGH'}
        if event.value=='PRESS':
            if event.type=='ESC':
                self._finish();return {'CANCELLED'}
            if event.type in {'LEFT_ARROW','RIGHT_ARROW'}:
                delta=-1 if event.type=='LEFT_ARROW' else 1
                self._index=(self._index+delta)%len(_ISSUES)
                self._area.tag_redraw();return {'RUNNING_MODAL'}
            if event.type=='F':
                pos=_ISSUES[self._index]['point']
                try:
                    space=self._area.spaces.active
                    space.region_3d.view_location=Vector(pos)
                    self._area.tag_redraw()
                except (ReferenceError,AttributeError):pass
                return {'RUNNING_MODAL'}
            if event.type=='Z' and event.ctrl:
                # Undo/Redo belongs to Blender. Never keep stale issue HUD over
                # a changed Mesh or consume Ctrl+Z / Ctrl+Shift+Z.
                self._finish();return {'PASS_THROUGH'}
        return {'PASS_THROUGH'}
