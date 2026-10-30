"""ISSMAKER Appearance Studio primitives and procedural GPU pattern renderer.

Shader state is not owned by the modal controller; always restore alpha/scissor.
One geometry contract drives both interactive bounds and drawn boxes.
"""
from __future__ import annotations
from math import cos,sin,pi
from collections import OrderedDict
from functools import lru_cache
from .core import pattern_engine, hud_quality, hud_readability


def within(rect,xy):
    x,y,w,h=rect
    return x<=xy[0]<=x+w and y<=xy[1]<=y+h


@lru_cache(maxsize=4096)
def rounded_geometry(x,y,w,h,r=10.,steps=12):
    r=max(0.,min(float(r),w/2.,h/2.))
    corners=((x+w-r,y+h-r,0),(x+r,y+h-r,90),
             (x+r,y+r,180),(x+w-r,y+r,270))
    # Ordered counterclockwise from lower-right, lower-left, upper-left, upper-right.
    angles=((x+w-r,y+r,-90,0),(x+r,y+r,180,270),
            (x+r,y+h-r,90,180),(x+w-r,y+h-r,0,90))
    # Explicit boundary in winding order (start right-top descending clockwise).
    path=[]
    for cx,cy,first,last in ((x+w-r,y+h-r,0,90), (x+r,y+h-r,90,180),
                             (x+r,y+r,180,270), (x+w-r,y+r,270,360)):
        for i in range(steps+1):
            a=(first+(last-first)*i/steps)*pi/180
            path.append((cx+r*cos(a),cy+r*sin(a)))
    return path


class RenderCache:
    """Bounded per-editor GPU batches; destroyed when the editor closes.

    Batches are keyed by complete geometry, never color, so fading/recoloring
    remains live. This cache is owned by exactly one Blender window/context.
    """
    def __init__(self,max_boxes=512,max_patterns=96):
        self.boxes=OrderedDict()
        self.patterns=OrderedDict()
        self.aa_patterns=OrderedDict()
        self.chrome=OrderedDict()
        self.smooth_shader=None
        self.aa_error=None
        self.shader=None
        self.max_boxes=max(32,int(max_boxes))
        self.max_patterns=max(16,int(max_patterns))
        self.hits=0
        self.misses=0

    def fetch(self,mapping,key,create,limit):
        if key in mapping:
            mapping.move_to_end(key)
            self.hits+=1
            return mapping[key]
        self.misses+=1
        value=create()
        mapping[key]=value
        if len(mapping)>limit:mapping.popitem(last=False)
        return value

    def clear(self):
        self.boxes.clear()
        self.patterns.clear()
        self.aa_patterns.clear()
        self.chrome.clear()
        self.smooth_shader=None
        self.aa_error=None
        self.shader=None


class Painter:
    def __init__(self,cache=None):
        import gpu
        from gpu_extras.batch import batch_for_shader
        self.gpu=gpu
        try:self._previous_blend=gpu.state.blend_get()
        except (RuntimeError,AttributeError):self._previous_blend='NONE'
        self.batcher=batch_for_shader
        self.cache=cache
        self.shader=(cache.shader if cache is not None else None) or gpu.shader.from_builtin('UNIFORM_COLOR')
        if cache is not None:cache.shader=self.shader
        gpu.state.blend_set('ALPHA')
        self.font=0
        self.opacity=1.0
    def close(self):
        self.gpu.state.blend_set(self._previous_blend)
    def _paint_color(self,color):
        return tuple(color[:3])+(max(0.,min(1.,color[3]*self.opacity)),)
    def triangles(self,verts,color):
        if not verts:return
        self.shader.bind();self.shader.uniform_float('color',self._paint_color(color))
        self.batcher(self.shader,'TRIS',{'pos':verts}).draw(self.shader)
    def lines(self,verts,color):
        if not verts:return
        self.shader.bind();self.shader.uniform_float('color',self._paint_color(color))
        self.batcher(self.shader,'LINES',{'pos':verts}).draw(self.shader)
    def box(self,x,y,w,h,fill,outline=None,rad=9):
        # BLF text drawing can reset blending between adjacent controls.
        # Each filled box must re-establish alpha, including cached batches.
        self.gpu.state.blend_set('ALPHA')
        def geometry():
            poly=rounded_geometry(x,y,w,h,rad)
            center=(x+w/2.,y+h/2.)
            triangles=[q for i,point in enumerate(poly)
                       for q in (center,point,poly[(i+1)%len(poly)])]
            lines=([q for i,point in enumerate(poly)
                   for q in (point,poly[(i+1)%len(poly)])] if outline else None)
            return (self.batcher(self.shader,'TRIS',{'pos':triangles}),
                    self.batcher(self.shader,'LINES',{'pos':lines}) if lines else None)
        batches=(self.cache.fetch(self.cache.boxes,(x,y,w,h,rad,bool(outline)),geometry,self.cache.max_boxes)
                 if self.cache is not None else geometry())
        self.shader.bind()
        self.shader.uniform_float('color',self._paint_color(fill))
        batches[0].draw(self.shader)
        if outline:
            self.shader.bind()
            self.shader.uniform_float('color',self._paint_color(outline))
            batches[1].draw(self.shader)
    def text(self,txt,x,y,size=12,color=(.9,.96,.95,1),width=None,align='left'):
        import blf
        txt=str(txt)
        blf.size(self.font,max(8,int(size)))
        if width is not None:
            txt=hud_readability.fit_caption(txt,width,
                lambda sample:blf.dimensions(self.font,sample)[0])
            if not txt:return
        tw,th=blf.dimensions(self.font,txt)
        if align=='center':x-=tw/2
        elif align=='right':x-=tw
        blf.position(self.font,hud_readability.snap(x),hud_readability.snap(y),0)
        blf.color(self.font,*self._paint_color(color))
        blf.draw(self.font,txt)
    @staticmethod
    def _pattern_tint(theme,preview):
        # Panels preserve user hue, but decoration never reaches bright UI glyph
        # saturation. Mixing against muted architectural mineral tint also
        # keeps neon accents useful for ACTIVE / ERROR semantic states.
        base=(.20,.38,.35)
        primary=tuple(base[i]*.70+theme.accent[i]*.30 for i in range(3))
        secondary=tuple(base[i]*.78+theme.secondary[i]*.22 for i in range(3))
        return primary,secondary

    def _pattern_smooth(self,pattern,x,y,w,h,theme,phase,preview,sc,policy):
        """Analytic pixel-space AA via GPU vertex alpha; no supersampled texture.

        The five legacy line groups are converted into feathered triangle
        strips. Cached thumbnails retain their batches; interactive animated
        backgrounds continue updating. A GPU shader failure returns False so
        the legacy 1px line renderer can still draw.
        """
        from math import isfinite
        if self.cache is not None and self.cache.aa_error is not None:return False
        try:
            shader=(self.cache.smooth_shader if self.cache is not None else None)
            if shader is None:
                shader=self.gpu.shader.from_builtin('SMOOTH_COLOR')
                if self.cache is not None:self.cache.smooth_shader=shader
            # Cached geometry never crosses region/window contexts.
            bright=max(.25,min(1.,theme.pattern_contrast))
            primary,secondary=self._pattern_tint(theme,preview)
            intensity=theme.pattern_strength*theme.pattern_opacity*(.42 if preview else .35)*(1.-theme.pattern_softness*.15)
            phase=round(phase,3)
            def build():
                units=pattern_engine.segments(pattern,round(max(2,w/sc)),round(max(2,h/sc)),
                    phase,round(theme.pattern_density,2),policy.detail)
                segs=[[] for _ in range(5)]
                for a,b,c,d,alpha in units:
                    bucket=min(4,max(0,int(alpha*5.)))
                    segs[bucket].append((x+a*sc,y+b*sc,x+c*sc,y+d*sc,1.))
                batches=[]
                for bucket,group in enumerate(segs):
                    if not group:
                        batches.append(None);continue
                    opacity=min(.20,intensity*(.10+bucket*.08)*bright)*self.opacity
                    if opacity<=0:
                        batches.append(None);continue
                    t=bucket/4
                    rgb=tuple(secondary[i]*(1.-t*.65)+primary[i]*(t*.65) for i in range(3))
                    geom,coverage=hud_quality.smooth_stroke_mesh(group,(x,y,w,h),width=1.,fringe=policy.aa_fringe_px)
                    if not geom:
                        batches.append(None);continue
                    rgba=[(rgb[0],rgb[1],rgb[2],rgba[3]*opacity) for rgba in coverage]
                    batches.append(self.batcher(shader,'TRIS',{'pos':geom,'color':rgba}))
                return tuple(batches)
            use_cache=(self.cache is not None and self.opacity>.999 and
                       (preview and abs(phase)<1e-8 or getattr(self,'_shared_cache',False)))
            if use_cache:
                # Live HUDs are quantized independently from input/hover: the
                # same decorative phase reused on multiple 60Hz draw callbacks.
                key=(pattern,round(x,2),round(y,2),round(w,2),round(h,2),
                     round(sc,3),round(theme.pattern_density,2),policy.name,
                     round(phase,5),tuple(round(v,4) for v in theme.accent+theme.secondary),
                     round(intensity,4),round(theme.pattern_softness,3),round(theme.pattern_contrast,3))
                batches=self.cache.fetch(self.cache.aa_patterns,key,build,
                                          min(24,self.cache.max_patterns))
            else:batches=build()
            shader.bind()
            for batch in batches:
                if batch is not None:batch.draw(shader)
            return True
        except Exception as exc:
            if self.cache is not None:self.cache.aa_error=type(exc).__name__ + ': ' + str(exc)[:130]
            return False

    def pattern(self,pattern,x,y,w,h,theme,phase=0.,preview=False):
        if pattern=='none' or w<8 or h<8:return
        sc=max(.25,min(4.,theme.pattern_scale))
        policy=hud_quality.resolve(getattr(theme,'render_quality','HIGH'))
        if policy.name!='PERFORMANCE':
            if self._pattern_smooth(pattern,x,y,w,h,theme,phase,preview,sc,policy):
                return
        def pattern_batches():
            units=pattern_engine.segments(pattern,round(max(2,w/sc)),round(max(2,h/sc)),
                round(phase,2),round(theme.pattern_density,2),policy.detail)
            bins=[[] for _ in range(5)]
            for a,b,c,d,alpha in units:
                bucket=min(4,max(0,int(alpha*5.)))
                bins[bucket].extend(((x+a*sc,y+b*sc),(x+c*sc,y+d*sc)))
            return tuple(self.batcher(self.shader,'LINES',{'pos':pts}) if pts else None for pts in bins)
        # Only thumbnails (fixed phase 0) retain GPU batches; streaming hero
        # data stays live, preventing cache churn while the animation moves.
        if self.cache is not None and preview and phase==0.:
            key=(pattern,round(x,3),round(y,3),round(w,3),round(h,3),round(sc,4),round(theme.pattern_density,2))
            bins=self.cache.fetch(self.cache.patterns,key,pattern_batches,self.cache.max_patterns)
        else:
            bins=pattern_batches()
        bright=max(.25,min(1.,theme.pattern_contrast))
        primary,secondary=self._pattern_tint(theme,preview)
        intensity=theme.pattern_strength*theme.pattern_opacity*(.42 if preview else .35)*(1.-theme.pattern_softness*.15)
        for n,batch in enumerate(bins):
            if batch is None:continue
            t=n/4
            rgb=tuple(secondary[i]*(1.-t*.65)+primary[i]*(t*.65) for i in range(3))
            alpha=min(.20, intensity * (0.10+n*.080)*bright)
            self.shader.bind()
            self.shader.uniform_float('color',self._paint_color((*rgb,alpha)))
            batch.draw(self.shader)
        # Light glow is a subdued inset highlight, not a blur that harms legibility.
        if not preview and theme.pattern_glow>.01:
            self.lines([(x+10,y+h-3),(x+w-10,y+h-3)],
                       (*theme.accent,min(.3,(theme.pattern_glow+theme.pattern_relief*.35)*.16)))


def draw_shared_background(theme, xywh, *, strength_factor=.48):
    """Decorative background for existing DWOS, Wall, Brick Hole, Collision HUDs.

    It draws no independent clickable surfaces. All interaction belongs to the
    original HUD and unchanged rectangle/hit-test code.
    """
    import time
    if theme.pattern=='none' or theme.pattern_strength<=0:return
    x,y,w,h=(float(v) for v in xywh)
    if w<10 or h<10:return
    from dataclasses import replace
    dim=replace(theme,pattern_strength=theme.pattern_strength*max(0.,min(.7,strength_factor)))
    from .core.hud_render_budget import decorative_time
    from . import hud_gpu_runtime
    phase=(decorative_time(time.monotonic(),theme.render_quality,
            theme.motion!='OFF')*.60*theme.pattern_speed*theme.pattern_amplitude
            if theme.motion!='OFF' else 0.)
    started=hud_gpu_runtime.perf_counter()
    p=Painter(cache=hud_gpu_runtime.cache())
    p._shared_cache=True
    try:
        p.pattern(theme.pattern,x+6,y+6,w-12,h-12,dim,phase,preview=False)
        # Noninteractive subtle edge light. Never expands panel/hit rectangles.
        if theme.motion!='OFF':
            from math import sin
            glow=(.065+.06*(.5+.5*sin(phase*1.5)))*theme.pattern_strength
            p.box(x+max(5.,w*.13),y+h-4.,max(2.,w*.74),1.5,(*theme.accent,glow),None,rad=.5)
    finally:
        try:p.close()
        finally:hud_gpu_runtime.background_sample(started)
