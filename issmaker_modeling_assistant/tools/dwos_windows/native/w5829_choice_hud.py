"""W58.29 — Experimental two-card VIEW_3D launcher (no Blender popup).
Only candidate code, never install as production. Preserves source for ESC.
"""
import bpy, bmesh, gpu, blf, math
from bpy.props import EnumProperty
from gpu_extras.batch import batch_for_shader
from . import auto_window

_ACTIVE = None
DARK=(.022,.031,.046,.965)
SURFACE=(.060,.083,.110,.98)
SELECTED=(.083,.146,.169,.985)
CYAN=(.28,.88,.88,1.)
COPPER=(.97,.66,.36,1.)
WHITE=(.89,.94,.98,1.)
MUTED=(.58,.67,.76,1.)
BAD=(1.,.41,.43,1.)

def card_rects(width,height,scale=1.0):
    """Pure layout + hit test: dimensions and hitboxes use same coordinate space."""
    scale=min(1.55,max(.75,float(scale)))
    w=min(365*scale, max(210, (width-36)/2-12))
    h=207*scale
    gap=20*scale
    if 2*w+gap+28<=width:
        cx=width*.5
        cy=height*.50
        return ((cx-w-gap*.5,cy-h*.5,w,h),
                (cx+gap*.5,cy-h*.5,w,h))
    w=min(width-32,440*scale)
    h=min(207*scale,max(140,(height-68)/2-10))
    gap=min(18*scale,max(8,(height-2*h)/4))
    x=(width-w)*.5
    y=(height-(2*h+gap))*.5
    return ((x,y+h+gap,w,h),(x,y,w,h))

def hit_test(x,y,rects):
    for i,(rx,ry,w,h) in enumerate(rects):
        if rx<=x<=rx+w and ry<=y<=ry+h:return i
    return -1

def group_face_ids(obj,group_name='Group'):
    if obj is None or obj.type!='MESH':raise ValueError("SELECT_MESH_HOST")
    grp=obj.vertex_groups.get(group_name)
    if grp is None:raise ValueError("SELECT_FACES_OR_VERTEX_GROUP")
    members={v.index for v in obj.data.vertices if any(q.group==grp.index and q.weight>0 for q in v.groups)}
    return [f.index for f in obj.data.polygons if all(int(v) in members for v in f.vertices)]

def ensure_selected_faces(context):
    """Existing edit faces authoritative; else use full-face group coverage."""
    ob=context.view_layer.objects.active
    if ob is None or ob.type!='MESH':raise ValueError("Выберите Mesh-фасад")
    if getattr(ob.dwos_window,'is_window',False):return 'ALREADY_BUILT'
    if ob.mode=='EDIT':
        bm=bmesh.from_edit_mesh(ob.data)
        if any(f.select for f in bm.faces):return 'EDIT_SELECTED'
        bpy.ops.object.mode_set(mode='OBJECT')
    faces=set(group_face_ids(ob))
    if not faces:raise ValueError("В Vertex Group нет полностью выделенных граней")
    bpy.ops.object.select_all(action='DESELECT')
    ob.select_set(True);context.view_layer.objects.active=ob
    context.tool_settings.mesh_select_mode=(False,False,True)
    for f in ob.data.polygons:f.select=f.index in faces
    bpy.ops.object.mode_set(mode='EDIT')
    bm=bmesh.from_edit_mesh(ob.data);bm.faces.ensure_lookup_table();bm.faces.index_update()
    for f in bm.faces:f.select_set(f.index in faces)
    bm.select_flush_mode();bmesh.update_edit_mesh(ob.data)
    return 'GROUP_'+str(len(faces))

def _rect(shader,x,y,w,h,color):
    gpu.state.blend_set('ALPHA')
    batch=batch_for_shader(shader,'TRIS',{'pos':[(x,y),(x+w,y),(x+w,y+h),(x,y),(x+w,y+h),(x,y+h)]})
    shader.bind();shader.uniform_float('color',color);batch.draw(shader)

def _text(x,y,size,value,color=WHITE):
    blf.size(0,max(10,int(size)));blf.color(0,*color);blf.position(0,int(x),int(y),0)
    blf.draw(0,str(value))

class DWOS_OT_w5829_choice_hud(bpy.types.Operator):
    bl_idname='dwos.w5829_choice_hud'
    bl_label='ISSMAKER DWOS · 2 Viewport Cards'
    bl_options={'REGISTER'}
    mode:EnumProperty(name='UV mode',items=[
        ('TEXEL','1000 px/m','Strict metric UV in the single 0-1 tile'),
        ('PLACEHOLDER','Material placeholder','Materials mandatory, UV not enforced')],default='TEXEL')
    _handle=None
    _hover=-1
    _area=None
    _message=''
    _dims=(0,0)
    def _finish(self,context):
        global _ACTIVE
        if self._handle is not None:
            try:bpy.types.SpaceView3D.draw_handler_remove(self._handle,'WINDOW')
            except (RuntimeError,ReferenceError,ValueError):pass
            self._handle=None
        if _ACTIVE is self:_ACTIVE=None
        try:
            if context.area:context.area.tag_redraw()
        except ReferenceError:pass
    def _draw(self):
        context=bpy.context
        area=context.area
        if area is None or area.type!='VIEW_3D':return
        region=context.region
        if region is None:return
        w,h=region.width,region.height
        scale=min(1.55,max(.78,float(context.preferences.system.ui_scale)))
        boxes=card_rects(w,h,scale)
        shader=gpu.shader.from_builtin('UNIFORM_COLOR')
        try:
            left=min(a[0] for a in boxes)
            top=max(a[1]+a[3] for a in boxes)
            totalw=max(a[0]+a[2] for a in boxes)-left
            _rect(shader,left-16,top+13,totalw+32,62*scale,DARK)
            _text(left,top+43*scale,19*scale,'ISSMAKER  /  DWOS WINDOW ENGINE',CYAN)
            _text(left,top+21*scale,11*scale,'Choose construction policy  •  ESC closes',MUTED)
            for i,(x,y,cw,ch) in enumerate(boxes):
                hue=CYAN if i==0 else COPPER
                _rect(shader,x,y,cw,ch,SELECTED if self._hover==i else SURFACE)
                _rect(shader,x,y+ch-7*scale,cw,7*scale,hue)
                _text(x+20*scale,y+ch-48*scale,16*scale,
                      ('01  /  UV 1000 px/m' if i==0 else '02  /  MATERIAL PLACEHOLDER'),WHITE)
                _text(x+20*scale,y+ch-82*scale,11*scale,
                      ('Single 4K tile 0-1, density verified' if i==0 else 'Preserve materials. UV not required.'),MUTED)
                _text(x+20*scale,y+ch-105*scale,11*scale,
                      ('LP  →  HP  →  GLASS  →  INTERIOR'),MUTED)
                _rect(shader,x+18*scale,y+18*scale,cw-36*scale,42*scale,hue)
                _text(x+39*scale,y+31*scale,12*scale,
                      ('BUILD WITH TEXEL' if i==0 else 'BUILD WITH PLACEHOLDER'),(.012,.023,.038,1))
            if self._message:
                _text(left,boxes[0][1]-25*scale,12*scale,self._message[:115],BAD)
        finally:
            gpu.state.blend_set('NONE')
    def invoke(self,context,event):
        global _ACTIVE
        if not context.area or context.area.type!='VIEW_3D':
            self.report({'WARNING'},'DWOS HUD: требуется 3D Viewport')
            return {'CANCELLED'}
        if _ACTIVE is not None:
            self.report({'INFO'},'DWOS HUD уже открыт')
            return {'CANCELLED'}
        # Existing native window uses its OWN Composer, not another modal.
        from . import composer
        if composer.resolve_window(context.active_object) is not None:
            return bpy.ops.dwos.viewport_composer('INVOKE_DEFAULT')
        self._hover=-1;self._message='';self._area=context.area.as_pointer()
        self._handle=bpy.types.SpaceView3D.draw_handler_add(self._draw,(),'WINDOW','POST_PIXEL')
        _ACTIVE=self
        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}
    def execute(self,context):
        """Headless production can test the same actual selection transaction."""
        return self._create(context,self.mode)
    def _create(self,context,mode):
        if mode not in {'TEXEL','PLACEHOLDER'}:raise ValueError("UNKNOWN_UV_MODE")
        source=ensure_selected_faces(context)
        if source=='ALREADY_BUILT':
            self.report({'WARNING'},'Existing window: open native Composer')
            return {'CANCELLED'}
        scene=context.scene
        scene['W5829_window_policy']=mode
        scene['W5829_selected_source']=source
        scene['W5829_interior_intersection_auto_disabled']=True
        # ASK_FIRST explicitly disables all auto reconciliation timers.
        if getattr(scene,'dwos',None) is not None and hasattr(scene.dwos,'smart_interior_conflict_behavior'):
            scene.dwos.smart_interior_conflict_behavior='ASK_FIRST'
        result=bpy.ops.dwos.auto_windows('EXEC_DEFAULT')
        scene['W5829_last_build_status']='CREATED' if 'FINISHED' in result else 'FAILED_NATIVE'
        if 'FINISHED' not in result:return {'CANCELLED'}
        scene['W5829_material_policy']='KEEP_SOURCE_MATERIALS'
        return {'FINISHED'}
    def modal(self,context,event):
        if _ACTIVE is not self:return {'CANCELLED'}
        if event.type=='ESC' and event.value=='PRESS':
            self._finish(context);return {'CANCELLED'}
        if event.type in {'MIDDLEMOUSE','WHEELUPMOUSE','WHEELDOWNMOUSE','NUMPAD_1','NUMPAD_3','NUMPAD_7'}:
            return {'PASS_THROUGH'}
        boxes=card_rects(context.region.width,context.region.height,
                          context.preferences.system.ui_scale)
        if event.type=='MOUSEMOVE':
            self._hover=hit_test(event.mouse_region_x,event.mouse_region_y,boxes)
            context.area.tag_redraw();return {'RUNNING_MODAL'}
        if event.type=='LEFTMOUSE' and event.value=='PRESS':
            i=hit_test(event.mouse_region_x,event.mouse_region_y,boxes)
            if i<0:return {'RUNNING_MODAL'}
            self._finish(context)
            self.mode='TEXEL' if i==0 else 'PLACEHOLDER'
            try:
                result=self._create(context,self.mode)
            except Exception as error:
                self.report({'ERROR'},'DWOS window creation rejected: '+str(error)[:190])
                return {'CANCELLED'}
            if 'FINISHED' in result and context.area and context.area.type=='VIEW_3D':
                try:bpy.ops.dwos.viewport_composer('INVOKE_DEFAULT')
                except (RuntimeError,ValueError) as error:self.report({'WARNING'},str(error)[:130])
            return result
        return {'RUNNING_MODAL'}

CLASSES=(DWOS_OT_w5829_choice_hud,)
