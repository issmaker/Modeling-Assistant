"""Two-choice Shader Editor radial UI; render-only, no material/node mutations."""
import math
import time
from .core import viewport_skin as _hud_skin
from .core import theme_engine as _theme_engine

CHOICES=('EMPTY','FOLDER')

def hit(dx,dy,scale=1.0):
    """Hit geometry matches the TWO visible 144-degree annular sectors."""
    r=math.hypot(dx,dy)
    if r<=39*scale:return -2  # center dismiss
    if r<49*scale or r>140*scale:return -1
    theta=abs(math.degrees(math.atan2(dy,dx)))
    if dx>=0 and theta<=72:return 1
    if dx<0 and theta>=108:return 0
    return -1

def _sector(center,side,ri,ro,scale):
    # Annulus with 2 generous 145-degree sectors, left and right.
    a0,a1=(math.radians(108),math.radians(252)) if side==0 else (math.radians(-72),math.radians(72))
    outer=[(center[0]+ro*scale*math.cos(a0+(a1-a0)*i/28),center[1]+ro*scale*math.sin(a0+(a1-a0)*i/28)) for i in range(29)]
    inner=[(center[0]+ri*scale*math.cos(a0+(a1-a0)*i/28),center[1]+ri*scale*math.sin(a0+(a1-a0)*i/28)) for i in range(29)]
    triangles=[]
    for i in range(28):
        triangles.extend((outer[i],outer[i+1],inner[i+1],outer[i],inner[i+1],inner[i]))
    return triangles

def fitted_font_size(text, scale, width, dimensions):
    """Pure logic: choose <= width via actual Blender blf.dimensions wrapper."""
    size = max(9, round(12*scale))
    while size > 9 and dimensions(text, size)[0] > width:
        size -= 1
    return size


def render(center, scale, opened, hover):
    """Two readable sectors, English short labels, one tooltip BELOW the wheel."""
    import gpu, blf
    import bpy
    from gpu_extras.batch import batch_for_shader
    # Style-only: Shader Editor uses the same live appearance profile as Viewport.
    addon = bpy.context.preferences.addons.get('issmaker_modeling_assistant')
    theme = _theme_engine.from_preferences(addon.preferences if addon else None)
    hud = _hud_skin.shell(theme)
    ink = _hud_skin.ink(theme)
    muted_ink = _hud_skin.ink(theme, kind='muted')
    from .nav_hud_geometry import triangulate_fan, rounded_rect
    progress = min(1.0, (time.monotonic()-opened)/.18)
    progress = 1-(1-progress)**3
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    def tris(coords, color):
        if not coords: return
        shader.bind(); shader.uniform_float('color', color)
        batch_for_shader(shader, 'TRIS', {'pos':coords}).draw(shader)
    def words(label, x, y, size, max_width, rgba):
        def dim(text, font_size):
            blf.size(0,font_size)
            return blf.dimensions(0,text)
        pt=fitted_font_size(label,scale,max_width,dim)
        blf.size(0,pt)
        w,h=blf.dimensions(0,label)
        # Never extend beyond the owning segment's text box.
        if w>max_width: return
        blf.position(0,x-w/2,y-h/2,0)
        blf.color(0,*rgba)
        blf.draw(0,label)
    gpu.state.blend_set('ALPHA')
    try:
        for index in (0,1):
            active=hover==index
            visual = _hud_skin.component('button', 'hover' if active else 'normal',
                                         role='primary' if index else 'neutral', theme=theme)
            rgb = theme.accent if index else theme.secondary
            outer=(142 if active else 138)*progress
            tris(_sector(center,index,49,outer,scale),(*visual.background[:3],.96*progress))
            tris(_sector(center,index,54,outer-3,scale),(*rgb,(.32 if active else .13)*progress))
            x=center[0]+(-96 if index==0 else 96)*scale
            label='EMPTY' if index==0 else 'UDIM'
            foreground = _hud_skin.control_ink(theme, part='button',
                    state='hover' if active else 'normal',
                    role='primary' if index else 'neutral')
            words(label,x,center[1],12,64*scale,(*foreground[:3],foreground[3]*progress))
        core=triangulate_fan(rounded_rect(center[0]-39*scale,center[1]-35*scale,
                                           center[0]+39*scale,center[1]+35*scale,20*scale))
        tris(core,(*hud['header'][:3],.99*progress))
        words('NODES',center[0],center[1]+8*scale,11,68*scale,(*ink[:3],ink[3]*progress))
        words('ESC',center[0],center[1]-13*scale,9,68*scale,(*muted_ink[:3],muted_ink[3]*progress))
        # Outside segments with a bounded caption that cannot collide with wedges.
        message=('Create empty nodes' if hover==0 else
                 'Choose texture folder' if hover==1 else
                 'Diffuse / ERM / Normal')
        words(message,center[0],center[1]-171*scale,11,254*scale,
              (*muted_ink[:3],muted_ink[3]*.92*progress))
    finally:
        gpu.state.blend_set('NONE')
