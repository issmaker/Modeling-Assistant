"""One-quad, read-only GPU material surface for UMS HUD chrome.

Blender 4.5 and 5.x: GPUShaderCreateInfo with no deprecated GLSL shader API.
Shader and batch are scoped to RenderCache owned by a single editor window.
No image datablocks, no mesh/material nodes, no modal input or mouse zones.
"""
from __future__ import annotations

from .core.material_policy import resolve

_VERTEX="""
void main()
{
    uv_interp = texCoord;
    gl_Position = u_mvp * vec4(pos.xy, 0.0, 1.0);
}
"""

_FRAGMENT="""
float hash2(vec2 p) {
    return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453123);
}
float grain(vec2 p) {
    vec2 i=floor(p), f=fract(p);
    vec2 s=f*f*(3.0-2.0*f);
    return mix(mix(hash2(i),hash2(i+vec2(1.,0.)),s.x),
               mix(hash2(i+vec2(0.,1.)),hash2(i+vec2(1.,1.)),s.x),s.y);
}
void main()
{
    vec2 uv = uv_interp;
    float wave=0.0;
    float spec=0.0;
    float t=u_phase;
    if(u_mode==0) {  // satin architectural metal
        float b=sin((uv.x*1.8+uv.y*.23)*73.0+sin(uv.y*13.0+t*.26)*1.2+t*.5);
        wave=.47+.35*b+.18*grain(uv*vec2(74.,2.));
        spec=pow(max(0.0,sin(uv.x*7.7+uv.y*.9+t*.36)),16.0);
    }
    else if(u_mode==1) { // graphite fiber weave
        vec2 p=uv*vec2(63.,31.);
        float fiber=sin(p.x*2.8+t*.28)*sin(p.y*2.5-t*.18);
        wave=.44+.28*fiber+.28*grain(p*.38);
        spec=pow(max(0.0,sin((uv.x+uv.y)*23.0-t*.32)),25.0);
    }
    else if(u_mode==2) { // natural mineral strata
        float path=uv.x*22.0+uv.y*14.0+sin(uv.y*16.0+t*.23)*1.7+t*.23;
        wave=.5+.33*sin(path)+.17*grain(uv*vec2(8.,13.));
        spec=pow(max(0.0,sin(path*.47)),12.0);
    }
    else if(u_mode==3) { // rolling translucent smoke
        vec2 p=uv*4.7;
        float field=sin(p.x*2.3+sin(p.y*3.7+t*.6)+t*.43);
        wave=.5+.30*field+.2*grain(uv*vec2(9.,7.)+t*.13);
        spec=pow(max(0.0,sin(p.x*1.5+p.y*.6+t*.22)),20.0);
    }
    else { // ink-like liquid layers
        float p=uv.x*18.0+sin(uv.y*13.0-t*.45)*2.4+t*.32;
        wave=.5+.35*sin(p)+.15*grain(uv*vec2(14.,8.));
        spec=pow(max(0.0,sin(p*.63+uv.y*7.0)),18.0);
    }
    wave=clamp(wave,0.0,1.0);
    float highlight=spec*.19;
    vec3 colour=mix(u_secondary,u_primary,wave)+u_primary*highlight;
    // Keep the underlay unobtrusive even on brightly colored custom themes.
    float a=u_opacity*(.46+wave*.37+highlight);
    FragColor=vec4(clamp(colour,0.,1.),clamp(a,0.,.46));
}
"""

def _shader():
    import gpu
    interface=gpu.types.GPUStageInterfaceInfo("ums_style_material_interface")
    interface.smooth("VEC2","uv_interp")
    info=gpu.types.GPUShaderCreateInfo()
    info.vertex_in(0,"VEC2","pos")
    info.vertex_in(1,"VEC2","texCoord")
    info.vertex_out(interface)
    info.fragment_out(0,"VEC4","FragColor")
    info.push_constant("MAT4","u_mvp")
    info.push_constant("VEC3","u_primary")
    info.push_constant("VEC3","u_secondary")
    info.push_constant("FLOAT","u_phase")
    info.push_constant("FLOAT","u_opacity")
    info.push_constant("INT","u_mode")
    info.vertex_source(_VERTEX)
    info.fragment_source(_FRAGMENT)
    return gpu.shader.create_from_info(info)

def render(theme,rect,*,now=0.0,preview=False,cache=None,opacity_multiplier=1.0):
    """Return False if unsupported. Caller should retain its old pattern fallback."""
    try:
        x,y,w,h=(float(v) for v in rect)
    except (TypeError,ValueError,OverflowError):
        return False
    if w<20. or h<16.:return False
    spec=resolve(theme,now=now,preview=preview)
    if spec is None:return False
    if cache is not None and getattr(cache,"material_error",None):
        return False
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
        shader=(cache.material_shader if cache is not None else None)
        if shader is None:
            shader=_shader()
            if cache is not None:cache.material_shader=shader
        def build():
            vertices=((x,y),(x+w,y),(x+w,y+h),(x,y+h))
            coords=((0.,0.),(1.,0.),(1.,1.),(0.,1.))
            return batch_for_shader(shader,"TRIS",
                {"pos":vertices,"texCoord":coords},
                indices=((0,1,2),(0,2,3)))
        if cache is None:
            batch=build()
        else:
            key=("STYLE_MATERIAL",round(x,2),round(y,2),round(w,2),round(h,2))
            batch=cache.fetch(cache.chrome,key,build,max(24,cache.max_patterns))
        shader.bind()
        shader.uniform_float("u_mvp",
            gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix())
        shader.uniform_float("u_primary",spec.primary)
        shader.uniform_float("u_secondary",spec.secondary)
        shader.uniform_float("u_phase",spec.phase)
        shader.uniform_float("u_opacity",spec.opacity*max(0.,min(1.,opacity_multiplier)))
        shader.uniform_int("u_mode",spec.mode)
        batch.draw(shader)
        return True
    except (ImportError,AttributeError,RuntimeError,TypeError,ValueError) as exc:
        if cache is not None:
            cache.material_error=type(exc).__name__+": "+str(exc)[:180]
        return False
