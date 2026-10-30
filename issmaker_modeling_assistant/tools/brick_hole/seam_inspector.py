"""BH0462 read-only UV seam scanner and modal visual issue navigator.

No Mesh/BMesh references are retained in issue records. The scanner detects
mathematical UV boundary mismatches; it CANNOT prove an image pixel mismatch
without reading the texture itself. Intentional marked seams are REVIEW,
not automatic geometry errors. Esc releases BOTH draw handlers.
"""
import math
import traceback
import heapq
import bpy
from bpy.types import Operator
from mathutils import Vector
from .seam_geometry_core import module_pair_signature

_ISSUES=[]
_ACTIVE=None
_LAST={'count':0,'proven_breaks':0,'review':0,'truncated':False,
       'image_pixels_tested':False,'draw_error':'','scanned_edges':0}
_MAX=512


def _xy(v):return tuple(float(x) for x in v)
def _dist(a,b):return math.dist(a,b)


def classify_pair(edge_uv_a,edge_uv_b,resolution=4096,marked=False,
                  material_difference=False,threshold_px=0.75,tile_span=4000/4096,
                  module_internal=False):
    """Pure classification; endpoint pairs ordered by *same* 3D BMVert IDs.

    A difference of exactly the known tile span may be an intentional tiled
    repetition, NOT necessarily invisible in the texture. No image pixels are
    sampled, so no class is named 'visible seam confirmed'.
    """
    if len(edge_uv_a)!=2 or len(edge_uv_b)!=2:raise ValueError('UV pair endpoints missing')
    a,b=[tuple(map(float,x)) for x in edge_uv_a],[tuple(map(float,x)) for x in edge_uv_b]
    if not all(math.isfinite(c) for p in a+b for c in p):raise ValueError('Nonfinite UV')
    gap=max(_dist(p,q) for p,q in zip(a,b))*resolution
    phase=max(math.dist(tuple((p[k]-q[k])-round((p[k]-q[k])/tile_span)*tile_span
                              for k in range(2)),(0.,0.)) for p,q in zip(a,b))*resolution
    # New ISSMAKER periodic cut always jumps exactly the SAFE UV width.
    delta0=tuple(a[0][i]-b[0][i] for i in range(2))
    delta1=tuple(a[1][i]-b[1][i] for i in range(2))
    periodic=(gap>threshold_px and
              _dist(delta0,delta1)*resolution<=threshold_px and
              phase<=threshold_px and
              any(abs(round(d/tile_span))>=1 for d in delta0))
    if gap<=threshold_px and not material_difference:
        return {'kind':'OK','severity':'NONE','gap_px':gap,'phase_gap_px':phase}
    if periodic:
        return {'kind':'PERIODIC_UV_REPEAT','severity':'REVIEW','gap_px':gap,
                'phase_gap_px':phase}
    if material_difference:
        return {'kind':'MATERIAL_BOUNDARY','severity':'REVIEW','gap_px':gap,
                'phase_gap_px':phase}
    if module_internal and not material_difference:
        return {'kind':'MODULE_INTERNAL_UV_GAP','severity':'ERROR','gap_px':gap,
                'phase_gap_px':phase}
    if marked:
        return {'kind':'AUTHORED_UV_SEAM','severity':'REVIEW','gap_px':gap,
                'phase_gap_px':phase}
    return {'kind':'UNEXPECTED_UV_BREAK','severity':'ERROR','gap_px':gap,
            'phase_gap_px':phase}


def _face_edge_uv(face,edge,uv_layer):
    loop=next((loop for loop in face.loops if loop.edge is edge),None)
    if loop is None:raise ValueError('Missing face edge loop')
    return {loop.vert:tuple(loop[uv_layer].uv),
            loop.link_loop_next.vert:tuple(loop.link_loop_next[uv_layer].uv)}


def scan_bmesh(bm,obj,resolution=4096):
    """All edges, deterministic order; never touches BMesh selection/UV."""
    layer=bm.loops.layers.uv.active
    if layer is None:raise ValueError('На Mesh нет активной UV-карты')
    # Separate bounded top-K buckets: many ordinary (yellow) seams must
    # never hide a later critical (red) seam on a large production wall.
    red_heap=[];yellow_heap=[]
    counts={'edges':0,'matched':0,'broken':0,'review':0,'no_material':0,
                      'module_internal_breaks':0}
    for edge_index,edge in enumerate(bm.edges):
        counts['edges']+=1
        if len(edge.link_faces)!=2:continue  # boundary is not necessarily a BAD seam
        a,b=tuple(edge.verts)
        fa,fb=edge.link_faces
        module_witness=None
        try:
            map_a=_face_edge_uv(fa,edge,layer)
            map_b=_face_edge_uv(fb,edge,layer)
            endpoints_a=(map_a[a],map_a[b]);endpoints_b=(map_b[a],map_b[b])
            same_material=fa.material_index==fb.material_index
            # Positive evidence for an authored 4×4/2×2 wall module: a UV shift
            # on its *internal* shared edge is not excused by edge.seam=True.
            # The geometric witness is calculated only for candidate gaps.
            classification=classify_pair(endpoints_a,endpoints_b,resolution,
                bool(edge.seam),not same_material)
            if classification['kind'] not in ('OK','PERIODIC_UV_REPEAT','MATERIAL_BOUNDARY') and same_material:
                # Physical geometry without world translation: module sizing
                # must not change because the CAD object is 1e6+ metres away.
                linear=obj.matrix_world.to_3x3()
                anchor=a.co.copy()
                metric_a=tuple(tuple(float(v) for v in linear @ (vert.co-anchor))
                               for vert in fa.verts)
                metric_b=tuple(tuple(float(v) for v in linear @ (vert.co-anchor))
                               for vert in fb.verts)
                metric_edge=((0.0,0.0,0.0),
                             tuple(float(v) for v in linear @ (b.co-anchor)))
                module_witness=module_pair_signature(metric_edge,metric_a,metric_b)
                if module_witness:
                    classification=classify_pair(endpoints_a,endpoints_b,resolution,
                        bool(edge.seam),False,module_internal=True)
        except (KeyError,ValueError) as exc:
            classification={'kind':'INVALID_UV_DATA','severity':'ERROR',
                            'gap_px':float('inf'),'phase_gap_px':float('inf'),
                            'detail':str(exc)}
        if classification['kind']=='OK':counts['matched']+=1;continue
        if classification['kind']=='MODULE_INTERNAL_UV_GAP':
            counts['module_internal_breaks']+=1
        severity=classification['severity']
        counts['broken' if severity=='ERROR' else 'review']+=1
        bucket=red_heap if severity=='ERROR' else yellow_heap
        priority=(classification['gap_px'] if math.isfinite(classification['gap_px']) else 1.e20,
                  -edge_index)
        if len(bucket)>=_MAX and priority<=bucket[0][0]:
            continue
        xyz_a=tuple(float(v) for v in obj.matrix_world @ a.co)
        xyz_b=tuple(float(v) for v in obj.matrix_world @ b.co)
        metric_edge=tuple(float(v) for v in obj.matrix_world.to_3x3() @ (b.co-a.co))
        mid=tuple((xyz_a[i]+xyz_b[i])*0.5 for i in range(3))
        entry=dict(classification)
        entry.update({'reason_code':classification['kind'],
                      'uv_endpoints_a':endpoints_a,'uv_endpoints_b':endpoints_b,
                      'scan_resolution_px':int(resolution),
                      'edge_index':edge_index,'a':xyz_a,'b':xyz_b,'point':mid,
                      'marked_seam':bool(edge.seam),'material_same':same_material,
                      'faces':(fa.index,fb.index),'source_object':obj.name,
                      'edge_length_m':math.sqrt(sum(v*v for v in metric_edge)),
                      'module_witness':module_witness})
        if len(bucket)<_MAX:heapq.heappush(bucket,(priority,entry))
        else:heapq.heapreplace(bucket,(priority,entry))
    report=[entry for _priority,entry in red_heap+yellow_heap]
    report.sort(key=lambda e:(e['severity']!='ERROR',-e['gap_px'] if math.isfinite(e['gap_px']) else -1e20,e['edge_index']))
    counts['truncated']=counts['broken']+counts['review']>len(report)
    return tuple(report),counts


def diagnostic_scan(context):
    """Read-only, fresh UV evidence for one-click support ZIP.

    The modal inspector need not be active. Never replaces _ISSUES or
    interacts with draw handlers; exports counts plus bounded witnesses.
    """
    obj=getattr(context,'edit_object',None) or getattr(context,'active_object',None)
    if obj is None or getattr(obj,'type',None)!='MESH':
        return {'summary':{'status':'NO_MESH'},'issues':[],'image_pixels_tested':False}
    # Explicit documented performance ceiling, not a silent partial scan.
    n_edges=len(obj.data.edges)
    if n_edges>2000000:
        return {'summary':{'status':'RESOURCE_LIMIT','edges':n_edges,
                           'max_edges':2000000,
                           'reason':'Диагностический UV scan ограничен ресурсами, это не ошибка геометрии'},
                'issues':[],'image_pixels_tested':False}
    import bmesh
    owns=obj.mode!='EDIT'
    bm=bmesh.new() if owns else bmesh.from_edit_mesh(obj.data)
    try:
        if owns:bm.from_mesh(obj.data)
        bm.faces.index_update()
        records,stats=scan_bmesh(bm,obj,resolution=int(
            getattr(context.scene,'brick_hole_resolution','4096')))
        return {'summary':dict(stats,status='SCANNED',object_name=obj.name),
                'issues':list(records),'image_pixels_tested':False,
                'note':'UV loop mismatch is a candidate, not sampled texture color.'}
    finally:
        if owns:bm.free()


def report_state():return dict(_LAST)
def is_active():
    return bool(_ACTIVE is not None and getattr(_ACTIVE, '_alive', False))

def stop():
    global _ACTIVE
    if _ACTIVE is not None:_ACTIVE._finish()
def clear():
    stop();_ISSUES.clear()

def issues():return tuple(_ISSUES)


class VIEW3D_OT_brick_hole_seam_scan(Operator):
    bl_idname='view3d.brick_hole_seam_scan'
    bl_label='ГЕОМЕТРИЯ · Найти неправильные UV-швы'
    bl_description=('Проверить общие Mesh-рёбра на UV-расхождения. '
                    'Красный = непреднамеренный разрыв, янтарный = проверить авторский/повторный шов. '
                    'Не меняет геометрию или UV')
    bl_options={'REGISTER'}

    @classmethod
    def poll(cls,context):
        return (context.area is not None and context.area.type=='VIEW_3D' and
                context.active_object is not None and context.active_object.type=='MESH')

    def execute(self,context):
        global _LAST
        from . import issue_inspector, stop_interactive_preview
        obj=context.active_object
        stop();issue_inspector.stop()
        # Inspection owns its own modal HUD. Close the prior Brick preview to
        # prevent stale input handlers or competing Esc ownership.
        stop_interactive_preview()
        _ISSUES.clear()
        try:
            import bmesh
            if obj.mode=='EDIT':
                bm=bmesh.from_edit_mesh(obj.data)
                bm.faces.index_update()
                records,statistics=scan_bmesh(bm,obj,
                    resolution=int(getattr(context.scene,'brick_hole_resolution','4096')))
            else:
                bm=bmesh.new()
                try:
                    bm.from_mesh(obj.data);bm.faces.index_update()
                    records,statistics=scan_bmesh(bm,obj,
                        resolution=int(getattr(context.scene,'brick_hole_resolution','4096')))
                finally:bm.free()
            _ISSUES.extend(records)
            _LAST={'count':len(records),'proven_breaks':statistics['broken'],
                   'review':statistics['review'],'truncated':statistics['truncated'],
                   'image_pixels_tested':False,'draw_error':'',
                   'scanned_edges':statistics['edges'],
                   'module_internal_breaks':statistics['module_internal_breaks'],
                   'source_object':obj.name,
                   'source_mode':obj.mode,
                   'source_mesh_id':obj.data.as_pointer(),
                   'source_counts':(len(obj.data.vertices),len(obj.data.edges),len(obj.data.polygons)) if obj.mode!='EDIT' else (len(bm.verts),len(bm.edges),len(bm.faces))}
            if not records:
                self.report({'INFO'},'UV: неожиданных UV-разрывов на общих Mesh-рёбрах не найдено; текстуры не анализировались')
                return {'FINISHED'}
            self.report({'WARNING'} if statistics['broken'] else {'INFO'},
                f'UV-швы: ошибок {statistics["broken"]}, проверить {statistics["review"]}. '
                'Стрелки — выбор, F — фокус, Esc — выход')
            # Blender requires explicit INVOKE_DEFAULT to start modal inspector.
            result=bpy.ops.view3d.brick_hole_seam_inspector('INVOKE_DEFAULT')
            return {'FINISHED'} if 'RUNNING_MODAL' in result else {'CANCELLED'}
        except Exception as exc:
            _LAST['draw_error']='SCAN: '+traceback.format_exc()[-3000:]
            self.report({'ERROR'},'UV анализ: '+str(exc)[:175]);return {'CANCELLED'}


def filtered_issue_indices(records, errors=True, reviews=False):
    """Pure read-only category filter; categorizes all records exactly once.

    Neither UI filters nor focus actions may recompute or mutate the BMesh.
    Unknown severities are withheld, not silently presented as confirmed errors.
    """
    return tuple(i for i, issue in enumerate(records)
                 if (errors and issue.get('severity') == 'ERROR')
                 or (reviews and issue.get('severity') == 'REVIEW'))


class VIEW3D_OT_brick_hole_seam_inspector(Operator):
    bl_idname='view3d.brick_hole_seam_inspector'
    bl_label='ISSMAKER · Smart UV Seam Inspector'
    bl_description=('Красные/жёлтые фильтры, ←/→ навигация, F фокус, Esc выход. '
                    'Только просмотр, UV/геометрия не меняются')
    bl_options={'INTERNAL'}

    @classmethod
    def poll(cls,context):
        return bool(_ISSUES and context.area and context.area.type=='VIEW_3D')

    def _visible(self):
        return filtered_issue_indices(_ISSUES,self._show_errors,self._show_reviews)

    def _selected(self):
        visible=self._visible()
        if not visible:return None
        if self._index not in visible:self._index=visible[0]
        return _ISSUES[self._index]

    def _move(self,delta):
        visible=self._visible()
        if not visible:return
        try:current=visible.index(self._index)
        except ValueError:current=0
        self._index=visible[(current+delta)%len(visible)]
        _LAST['selected_edge_index']=_ISSUES[self._index].get('edge_index')
        self._area.tag_redraw()

    def _toggle(self,category):
        if category=='ERROR':self._show_errors=not self._show_errors
        elif category=='REVIEW':self._show_reviews=not self._show_reviews
        selected=self._selected()
        _LAST['selected_edge_index']=selected.get('edge_index') if selected is not None else None
        _LAST['show_errors']=self._show_errors
        _LAST['show_reviews']=self._show_reviews
        _LAST['visible_count']=len(self._visible())
        self._area.tag_redraw()

    def invoke(self,context,event):
        global _ACTIVE
        stop()
        self._area=context.area
        self._index=0
        self._show_errors=True
        self._show_reviews=False
        self._alive=True
        self._draw_world_handle=None
        self._draw_card_handle=None
        self._card=None
        self._buttons={}
        self._hover=None
        self._cache=None
        _LAST.update({'show_errors':True,'show_reviews':False,
                      'visible_count':len(self._visible()),'inspector_active':True,
                      'selected_edge_index':_ISSUES[self._index].get('edge_index')})
        try:
            from ...appearance_gpu import RenderCache
            self._cache=RenderCache(max_boxes=96)
            self._draw_world_handle=bpy.types.SpaceView3D.draw_handler_add(
                self._draw_world,(),'WINDOW','POST_VIEW')
            self._draw_card_handle=bpy.types.SpaceView3D.draw_handler_add(
                self._draw_card,(),'WINDOW','POST_PIXEL')
            context.window_manager.modal_handler_add(self)
            _ACTIVE=self
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        except Exception:
            self._finish()
            raise

    def _finish(self):
        global _ACTIVE
        self._alive=False
        for key in ('_draw_world_handle','_draw_card_handle'):
            handle=getattr(self,key,None)
            if handle is not None:
                try:bpy.types.SpaceView3D.draw_handler_remove(handle,'WINDOW')
                except (ReferenceError,RuntimeError,ValueError):pass
                setattr(self,key,None)
        cache=getattr(self,'_cache',None)
        if cache is not None:cache.clear();self._cache=None
        self._card=None
        self._buttons={}
        if _ACTIVE is self:_ACTIVE=None
        _LAST['inspector_active']=False
        area=getattr(self,'_area',None)
        if area:
            try:area.tag_redraw()
            except ReferenceError:pass

    def _draw_world(self):
        if not self._alive or not self._visible():return
        if bpy.context.area != self._area:return
        try:
            import gpu
            from gpu_extras.batch import batch_for_shader
            shader=gpu.shader.from_builtin('UNIFORM_COLOR')
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('NONE')
            shader.bind()
            selected=self._selected()
            # Draw bounded diagnostic edges (up to 512 per severity): all matching filters are shown
            # subdued, while only the selected edge receives the focus glow.
            visible=self._visible()
            for severity,color in (
                ('ERROR',(.88,.21,.22,.35)),
                ('REVIEW',(1.0,.69,.22,.31))):
                verts=[]
                for i in visible:
                    issue=_ISSUES[i]
                    if issue['severity']==severity and issue is not selected:
                        verts.extend((issue['a'],issue['b']))
                if verts:
                    shader.uniform_float('color',color)
                    gpu.state.line_width_set(1.5)
                    batch_for_shader(shader,'LINES',{'pos':verts}).draw(shader)
            if selected is not None:
                color=((1.,.31,.30,1.) if selected['severity']=='ERROR'
                       else (1.,.76,.30,1.))
                shader.uniform_float('color',color)
                gpu.state.line_width_set(4.)
                batch_for_shader(shader,'LINES',{'pos':(selected['a'],selected['b'])}).draw(shader)
                gpu.state.point_size_set(9.)
                batch_for_shader(shader,'POINTS',{'pos':(selected['a'],selected['b'])}).draw(shader)
        except Exception as exc:
            _LAST['draw_error']='WORLD: '+str(exc)[:250]
        finally:
            try:
                gpu.state.point_size_set(1.);gpu.state.line_width_set(1.)
                gpu.state.depth_test_set('NONE');gpu.state.blend_set('NONE')
            except (NameError,RuntimeError,AttributeError):pass

    def _draw_card(self):
        if not self._alive or bpy.context.area != self._area:return
        painter=None
        try:
            from ...appearance_gpu import Painter
            from ...core import viewport_skin as skin
            from ...core import theme_engine
            region=next((r for r in self._area.regions if r.type=='WINDOW'),None)
            if region is None:return
            width,height=region.width,region.height
            if width<350 or height<250:
                self._card=None;self._buttons={};return
            prefs=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
            theme=theme_engine.from_preferences(prefs.preferences if prefs else None)
            style=skin.shell(theme)
            painter=Painter(cache=self._cache)
            w=min(540.,width-24.);h=212.;x=(width-w)/2.;y=19.
            self._card=(x,y,w,h)
            self._buttons={}
            painter.box(x,y,w,h,style['panel'],style['border'],rad=skin.CORNER_PX['panel'])
            painter.box(x+6,y+h-54,w-12,48,style['header'],rad=7)
            painter.text('ISSMAKER / ГЕОМЕТРИЯ',x+17,y+h-24,13,style['title'],width=w-40)
            painter.text('UV SEAM INSPECTOR · режим просмотра',x+17,y+h-43,10,style['muted'],width=w-35)
            def button(key,rect,label,role='neutral',active=False,enabled=True):
                state='disabled' if not enabled else 'active' if active else 'hover' if key==self._hover else 'normal'
                visual=skin.component('button',state,role,theme)
                painter.box(*rect,visual.background,visual.border,rad=skin.CORNER_PX['button'])
                color=skin.control_ink(theme,part='button',state=state,role=role)
                painter.text(label,rect[0]+rect[2]/2,rect[1]+rect[3]/2-4,
                             11,color,width=rect[2]-12,align='center')
                if enabled:self._buttons[key]=rect
            counts=(sum(issue['severity']=='ERROR' for issue in _ISSUES),
                    sum(issue['severity']=='REVIEW' for issue in _ISSUES))
            gap=9.;fw=(w-34-gap)/2
            button('ERROR',(x+13,y+h-99,fw,36),
                   ('● ' if self._show_errors else '○ ')+'КРАСНЫЕ  '+str(counts[0]),
                   'danger',self._show_errors)
            button('REVIEW',(x+22+fw,y+h-99,fw,36),
                   ('● ' if self._show_reviews else '○ ')+'ЖЁЛТЫЕ  '+str(counts[1]),
                   'warning',self._show_reviews)
            selected=self._selected();visible=self._visible()
            info=(x+13,y+h-147,w-26,40)
            painter.box(*info,style['header'],rad=6)
            if selected is None:
                painter.text('Фильтры выключены — нет отображаемых швов',
                             x+24,y+h-125,12,style['muted'],width=w-50)
            else:
                n=visible.index(self._index)+1
                sev=selected['severity']
                title='ОШИБКА UV' if sev=='ERROR' else 'ПРОВЕРКА ШВА'
                accent=(skin.semantic_ink(theme,role='danger') if sev=='ERROR'
                        else skin.semantic_ink(theme,role='warning'))
                painter.text(f'{title}  {n}/{len(visible)}  ·  ребро {selected["edge_index"]}',
                             x+23,y+h-122,12,accent,width=w-50)
                gap_px=float(selected['gap_px'])
                gap_label=f'{gap_px:.2f} px' if math.isfinite(gap_px) else 'неопределён'
                explanations={'MODULE_INTERNAL_UV_GAP':'UV-зазор внутри модуля',
                              'UNEXPECTED_UV_BREAK':'Несовместимый UV-разрыв',
                              'PERIODIC_UV_REPEAT':'Периодический UV-повтор',
                              'MATERIAL_BOUNDARY':'Граница материалов',
                              'AUTHORED_UV_SEAM':'Авторский UV-шов',
                              'INVALID_UV_DATA':'Некорректные UV-данные'}
                reason=explanations.get(selected['kind'],selected['kind'])
                module=selected.get('module_witness')
                if module:
                    size=float(module['module_size_m'])
                    reason+=f' {size:g}×{size:g} м'
                painter.text(f'{reason}  |  UV {gap_label}  |  F — фокус',
                             x+23,y+h-140,10,style['muted'],width=w-50)
            painter.text('Фильтры не меняют Mesh/UV. Цвет пикселей не проверен.',
                         x+15,y+49,10,style['muted'],width=w-30)
            controls=(('BACK','← НАЗАД'),('NEXT','ДАЛЬШЕ →'),('FOCUS','F · ФОКУС'),('CLOSE','ESC · ВЫХОД'))
            bw=(w-26-3*8)/4
            for i,(key,label) in enumerate(controls):
                button(key,(x+13+i*(bw+8),y+10,bw,32),label,
                       'danger' if key=='CLOSE' else 'primary' if key=='FOCUS' else 'neutral',
                       enabled=(selected is not None or key=='CLOSE'))
        except Exception as exc:
            _LAST['draw_error']='CARD: '+str(exc)[:250]
            self._buttons={};self._card=None
        finally:
            if painter is not None:
                try:painter.close()
                except (RuntimeError,AttributeError):pass

    def _focus(self):
        issue=self._selected()
        if issue is None:return
        target=issue['point']
        try:
            space=self._area.spaces.active
            space.region_3d.view_location=Vector(target)
            space.region_3d.view_distance=max(1.0,issue['edge_length_m']*3)
            self._area.tag_redraw()
        except (AttributeError,ReferenceError):pass

    def modal(self,context,event):
        if not self._alive:return {'CANCELLED'}
        # The inspector must never retain Esc ownership after changing areas,
        # selection or mesh revision (Undo/Redo and mode switches included).
        if context.area!=self._area:
            self._finish();return {'PASS_THROUGH'}
        obj=getattr(context,'active_object',None)
        if (obj is None or obj.type!='MESH' or obj.name!=_LAST.get('source_object')
                or obj.data.as_pointer()!=_LAST.get('source_mesh_id')
                or obj.mode!=_LAST.get('source_mode',obj.mode)):
            self._finish();return {'PASS_THROUGH'}
        if event.value=='PRESS' and event.type=='ESC':
            self._finish();return {'CANCELLED'}
        if event.value=='PRESS' and event.ctrl and event.type in ('Z','Y'):
            self._finish();return {'PASS_THROUGH'}
        if event.value=='PRESS' and event.type in ('RIGHT_ARROW','LEFT_ARROW'):
            self._move(1 if event.type=='RIGHT_ARROW' else -1)
            return {'RUNNING_MODAL'}
        if event.value=='PRESS' and event.type in ('ONE','NUMPAD_1','TWO','NUMPAD_2'):
            self._toggle('ERROR' if event.type in ('ONE','NUMPAD_1') else 'REVIEW')
            return {'RUNNING_MODAL'}
        if event.value=='PRESS' and event.type=='F':
            self._focus();return {'RUNNING_MODAL'}
        window=next((r for r in self._area.regions if r.type=='WINDOW'),None)
        if window is not None and self._card is not None:
            x=event.mouse_x-window.x;y=event.mouse_y-window.y
            x0,y0,w,h=self._card
            inside=x0<=x<=x0+w and y0<=y<=y0+h
            if event.type=='MOUSEMOVE':
                hover=next((k for k,(bx,by,bw,bh) in self._buttons.items()
                            if bx<=x<=bx+bw and by<=y<=by+bh),None)
                if hover!=self._hover:
                    self._hover=hover;self._area.tag_redraw()
            if event.type=='LEFTMOUSE' and event.value=='PRESS' and inside:
                key=next((k for k,(bx,by,bw,bh) in self._buttons.items()
                          if bx<=x<=bx+bw and by<=y<=by+bh),None)
                if key=='CLOSE':self._finish();return {'CANCELLED'}
                if key in ('ERROR','REVIEW'):self._toggle(key)
                elif key=='FOCUS':self._focus()
                elif key in ('BACK','NEXT'):self._move(-1 if key=='BACK' else 1)
                return {'RUNNING_MODAL'}
            if inside and event.type in ('LEFTMOUSE','MOUSEMOVE'):
                return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}
