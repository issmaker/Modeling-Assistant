"""Optional high-DPI icon sprites for the ISSMAKER Graphite HUD.

Runtime depends only on Blender's gpu module and Python stdlib.  If GPUTexture
creation is unsupported on a particular GPU backend, the caller *must* use
its geometry fallback rather than lose controls.  No .blend image datablocks.
"""
from pathlib import Path
import math
import zlib

SIZE=128
BASE=Path(__file__).parent/'assets'/'icons'
_ICON_ALPHA={}
_GPU_CACHE={}
_SHADER=None
_FAILED=None
_FAILED_NAMES=set()
_NAMES=None
_COUNTERS={'ready':0,'failures':0,'fallback_draws':0,'sprite_draws':0}


def available():
    global _NAMES
    if _NAMES is None:
        _NAMES=tuple(sorted(p.name[:-8] for p in BASE.glob('*.rgba8.z')))
    return _NAMES


def _alpha(name):
    if name not in _ICON_ALPHA:
        raw=zlib.decompress((BASE/(name+'.rgba8.z')).read_bytes())
        if len(raw)!=SIZE*SIZE:
            raise ValueError('invalid icon pixel count: '+name)
        # Texture origin is bottom-left; exported PNG rows start at top-left.
        _ICON_ALPHA[name]=b''.join(raw[y*SIZE:(y+1)*SIZE] for y in reversed(range(SIZE)))
    return _ICON_ALPHA[name]


def _rgba(name, rgb, strength):
    mask=_alpha(name)
    r,g,b=(round(max(0,min(1,c))*255) for c in rgb)
    alpha_scale=max(0,min(1,strength))
    pixels=bytearray(SIZE*SIZE*4)
    for i,a in enumerate(mask):
        o=i*4;pixels[o]=r;pixels[o+1]=g;pixels[o+2]=b
        pixels[o+3]=round(a*alpha_scale)
    return pixels


def _texture(name, rgb, strength):
    import gpu
    quantized=round(max(0,min(1,strength))*15)
    rgb8=tuple(round(max(0,min(1,v))*255) for v in rgb)
    key=(name,rgb8,quantized)
    cached=_GPU_CACHE.get(key)
    if cached is not None:return cached
    pixels=_rgba(name,tuple(x/255 for x in rgb8),quantized/15)
    # Blender 4.5.5 GPUTexture(..., data=...) only accepts a FLOAT Buffer on
    # supported backends even if GPU texture storage is RGBA8.  Converting the
    # packed 0..255 colour bytes to normalized float avoids the repeated
    # 'Only Buffer of format FLOAT is currently supported' diagnostic.
    # Do not use a flat float tuple as a second long-lived cached resource.
    normalized=tuple(component / 255.0 for component in pixels)
    buf=gpu.types.Buffer('FLOAT',(SIZE*SIZE*4,),normalized)
    texture=gpu.types.GPUTexture((SIZE,SIZE),format='RGBA8',data=buf)
    try:texture.filter_mode('LINEAR')
    except (AttributeError,TypeError,ValueError):pass
    _GPU_CACHE[key]=texture
    _COUNTERS['ready']=len(_GPU_CACHE)
    # Bound memory even after hundreds of palette changes.
    if len(_GPU_CACHE)>240:
        keep={key:texture}
        _GPU_CACHE.clear();_GPU_CACHE.update(keep)
    return texture


def render(name,x,y,display_size,rgb=(.94,.96,1.),opacity=1.,rotation=0.):
    """Return False rather than failing an entire HUD when GPU is unavailable."""
    global _SHADER,_FAILED
    if name in _FAILED_NAMES:return False
    if name not in available():return False
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
        if _SHADER is None:
            # Shader alias differs among Blender backends.
            try:_SHADER=gpu.shader.from_builtin('IMAGE')
            except (ValueError,TypeError):
                _SHADER=gpu.shader.from_builtin('IMAGE_SCENE_LINEAR_TO_REC709_SRGB')
        texture=_texture(name,rgb,opacity)
        radius=display_size*.5
        c=math.cos(rotation);s=math.sin(rotation)
        def transform(px,py):return (x+px*c-py*s,y+px*s+py*c)
        p=tuple(transform(dx*radius,dy*radius) for dx,dy in ((-1,-1),(1,-1),(1,1),(-1,1)))
        batch=batch_for_shader(_SHADER,'TRIS',{'pos':(p[0],p[1],p[2],p[0],p[2],p[3]),
            'texCoord':((0,0),(1,0),(1,1),(0,0),(1,1),(0,1))})
        _SHADER.bind()
        _SHADER.uniform_sampler('image',texture)
        batch.draw(_SHADER)
        _COUNTERS['sprite_draws']+=1
        return True
    except Exception as exc:
        _FAILED='%s: %s'%(type(exc).__name__,str(exc)[:300])
        _FAILED_NAMES.add(name)
        _COUNTERS['failures']+=1
        return False


def note_fallback():_COUNTERS['fallback_draws']+=1


def status():
    return dict(_COUNTERS,failed=_FAILED,assets=len(available()),texture_cache=len(_GPU_CACHE))


def reset():
    global _SHADER,_FAILED
    _GPU_CACHE.clear();_ICON_ALPHA.clear();_FAILED_NAMES.clear()
    _SHADER=None;_FAILED=None
    for k in _COUNTERS:_COUNTERS[k]=0
