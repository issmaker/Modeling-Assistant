"""ISSMAKER UI Core — pure, Blender-independent scale and layout policy.

No Blender imports, GPU handles, operators, or mesh updates. Scale values are
relative to Blender's existing UI theme DPI; NEVER multiply the system scale
here a second time. Viewport draw and hit rectangles must use one geometry scale.
"""
from __future__ import annotations
from dataclasses import dataclass
import math

MIN_SCALE=0.75
MAX_SCALE=2.0
# Explicit readability presets do not multiply Blender's system DPI.
# AUTO is adaptive only when a real Window/context is supplied. Pure calls
# without screen metrics retain the baseline for saved settings and tests.
DISPLAY_PRESETS={'AUTO':(1.0,1.0), 'QHD':(1.18,1.18), 'UHD':(1.42,1.42)}


def auto_display_floor(window_size, blender_ui_scale=1.0):
    """Extra readability above Blender DPI, bounded and safe for split windows.

    Screen resolution is not DPI: use physical window height as a modest
    *readability floor*, then subtract existing Blender UI scale so Windows
    150%/200% does not get scaled again. No effect when window data is unknown.
    Returns an ADDITIONAL multiplier, not a replacement for Blender DPI.
    """
    try:
        width,height=(float(v) for v in window_size)
        ui=float(blender_ui_scale)
        if not all(math.isfinite(v) for v in (width,height,ui)) or min(width,height,ui)<=0:
            return 1.0
    except (TypeError,ValueError,OverflowError):
        return 1.0
    # Approximate 1080p -> 1.00, 1440p -> 1.16, 2160p -> 1.36.
    # Keep a small 1024x768 window at legacy scale and avoid overgrown HUDs.
    desired=1.0 + min(.36,max(0.0,(height-1080.)/1080.)*.36)
    return sanitized(max(1.0,desired/max(1.0,ui)),low=1.0,high=1.36)


def _screen_context_info(context):
    try:
        window=getattr(context,'window',None)
        width=float(getattr(window,'width',0))
        height=float(getattr(window,'height',0))
        system=getattr(getattr(context,'preferences',None),'system',None)
        dpi=float(getattr(system,'ui_scale',1.0))
        return (width,height),dpi
    except (TypeError,ValueError,OverflowError,AttributeError,ReferenceError,RuntimeError):
        return (0.,0.),1.0



def sanitized(value, default=1.0, low=MIN_SCALE, high=MAX_SCALE):
    try:
        x=float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return max(float(low),min(float(high),x)) if math.isfinite(x) else float(default)


@dataclass(frozen=True)
class UIProfile:
    font: float=1.0
    window: float=1.0
    debug: bool=False
    display_preset: str='AUTO'

    @property
    def safe_window(self):
        # Fonts cannot silently outgrow the row height. Enlarge window and row
        # spacing as needed rather than shrinking critical text without notice.
        return min(MAX_SCALE,max(self.window,self.font))

    def font_px(self, size):
        return max(7,int(round(float(size)*self.font)))

    def window_px(self, size):
        return float(size)*self.safe_window


def profile(font=1.0, window=1.0, debug=False, display_preset='AUTO'):
    preset=str(display_preset).upper()
    if preset not in DISPLAY_PRESETS:
        preset='AUTO'
    font_floor,window_floor=DISPLAY_PRESETS[preset]
    return UIProfile(max(sanitized(font),font_floor),
                     max(sanitized(window),window_floor),bool(debug),preset)


def from_preferences(prefs, *, context=None):
    """Resolve a display profile. `context` is optional to keep offline code pure.

    Only AUTO uses window metrics; QHD/UHD are explicit manual minimums.
    UI components are still rendered and hit-tested using one geometry scale.
    """
    result=profile(getattr(prefs,'font_scale',1.0),
                   getattr(prefs,'window_scale',1.0),
                   getattr(prefs,'ui_debug_bounds',False),
                   getattr(prefs,'hud_display_preset','AUTO'))
    if result.display_preset!='AUTO' or context is None:
        return result
    dims,system_scale=_screen_context_info(context)
    floor=auto_display_floor(dims,system_scale)
    return UIProfile(max(result.font,floor),max(result.window,floor),
                     result.debug,result.display_preset)


def readable_snapshot(prefs):
    resolved=from_preferences(prefs)
    return {'display_preset':resolved.display_preset,
            'font_scale':resolved.font,
            'window_scale':resolved.safe_window,
            'policy':'explicit_minimum_no_os_dpi_multiplication'}


def select_font(text, original_px, max_width, measure, preferred_scale=1.0,
                *, minimum_px=8, max_height=None):
    """Pick largest fitting font using *actual* backend text measurements.

    Does not truncate meaning-bearing labels. Returns None when even smallest
    legible text cannot fit; caller must expand/scroll container or show a
    deliberate overflow state, not draw outside or obscure another control.
    """
    if not callable(measure) or not math.isfinite(float(max_width)):
        return None
    top=max(minimum_px,int(round(float(original_px)*sanitized(preferred_scale))))
    if max_height is not None:
        top=min(top,max(0,int(float(max_height))))
    for size in range(top,minimum_px-1,-1):
        try: width=float(measure(str(text),size))
        except (TypeError, ValueError, RuntimeError): return None
        if math.isfinite(width) and width<=max_width: return size
    return None


def as_xyxy(rect):
    if len(rect)!=4: raise ValueError('Rectangle requires 4 numbers')
    coords=tuple(float(x) for x in rect)
    if not all(math.isfinite(x) for x in coords):raise ValueError('Non-finite rectangle')
    if coords[2]<coords[0] or coords[3]<coords[1]:raise ValueError('Invalid rectangle order')
    return coords


def intersection(a,b):
    a=as_xyxy(a);b=as_xyxy(b)
    return max(0.,min(a[2],b[2])-max(a[0],b[0]))*max(0.,min(a[3],b[3])-max(a[1],b[1]))


def inspect_layout(container, controls, *, intentional=(), tolerance=1., margin=0.):
    """Pure QA for *peer* controls; caller excludes nested/child hit targets.

    Rectangles are xyxy, unlike DWOS's x/y/w/h. Intentional overlap pairs
    (e.g. parent row plus favorite star) must be explicitly enumerated.
    """
    try: outer=as_xyxy(container)
    except (TypeError,ValueError,OverflowError):return ['container: INVALID_RECT']
    allow={frozenset(pair) for pair in intentional}
    good=[];errors=[]
    for key,bounds in controls:
        key=str(key)
        try:r=as_xyxy(bounds)
        except (TypeError,ValueError,OverflowError):
            errors.append(f'{key}: INVALID_RECT');continue
        if r[2]-r[0]<0.01 or r[3]-r[1]<0.01:
            errors.append(f'{key}: EMPTY_RECT');continue
        if (r[0]<outer[0]+margin-tolerance or r[1]<outer[1]+margin-tolerance or
            r[2]>outer[2]-margin+tolerance or r[3]>outer[3]-margin+tolerance):
            errors.append(f'{key}: OUT_OF_BOUNDS')
        for other,o in good:
            if frozenset((key,other)) not in allow and intersection(r,o)>tolerance:
                errors.append(f'{other}/{key}: OVERLAP')
        good.append((key,r))
    return errors

# Bounded, purely diagnostic state; no Blender references, handlers or draw calls.
_LAYOUT_SNAPSHOTS={}


def record_layout(tool, width, height, controls, *, ignore=()):
    region=(0.,0.,max(0.,float(width)),max(0.,float(height)))
    filtered=[(name,rect) for name,rect in controls if name not in set(ignore)]
    issues=inspect_layout(region,filtered)
    _LAYOUT_SNAPSHOTS[str(tool)]={
        'region':[region[2],region[3]],'count':len(filtered),
        'issues':issues[:100],'overflow_count':max(0,len(issues)-100)}
    return issues


def snapshot_layout():
    return {name:{'region':list(row['region']),'count':row['count'],
                  'issues':list(row['issues']),'overflow_count':row['overflow_count']}
            for name,row in _LAYOUT_SNAPSHOTS.items()}
