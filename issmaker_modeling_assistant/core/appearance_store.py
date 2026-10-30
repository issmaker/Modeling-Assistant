"""Versioned, crash-tolerant Blender appearance profile persistence.

Pure-python storage component. The bpy-specific caller provides config_dir
and the root AddonPreferences. No timers or Blender tools imported here.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path
from .theme_engine import SCHEMES, SURFACES, PATTERNS, ACCENTS, from_values
from .pattern_engine import IDS as LEGACY_PATTERN_IDS

SCHEMA = 1
FILE_NAME = 'appearance-v1.json'
FIELDS = (
    'accent','ui_scheme','ui_surface','ui_pattern','ui_custom_accent',
    'ui_accent_color','ui_secondary_color','ui_pattern_strength','ui_pattern_scale',
    'ui_pattern_amplitude','ui_pattern_parallax','ui_pattern_relief',
    'ui_pattern_speed','ui_pattern_density','ui_pattern_opacity',
    'ui_pattern_contrast','ui_pattern_glow','ui_pattern_softness',
    'ui_studio_pos_x','ui_studio_pos_y','ui_studio_tab','ui_studio_collapsed',
    'ui_dwos_hud_custom','ui_dwos_hud_pinned','ui_dwos_hud_x','ui_dwos_hud_y',
    'ui_wall_workspace_pos_x','ui_wall_workspace_pos_y','ui_wall_workspace_user_moved',
    'ui_motion_quality','ui_reduced_motion',
    'font_scale','window_scale','hud_display_preset','button_scale','density','ui_debug_bounds','ui_render_quality',
    'radial_center_cursor','ui_mode','dock_sizing','dock_manual_width','dock_manual_height',
    'show_uv_issue_overlay','overlay_opacity','show_3d_issue_overlay','overlay_3d_fill','overlay_3d_xray',
)
ENUMS = {'hud_display_preset':('AUTO','QHD','UHD'), 'ui_render_quality':('ULTRA','HIGH','BALANCED','PERFORMANCE'), 'accent':tuple(ACCENTS), 'ui_scheme':tuple(x.upper() for x in SCHEMES), 'ui_surface':tuple(x.upper() for x in SURFACES),
         'ui_pattern':tuple(x.upper() for x in PATTERNS), 'ui_motion_quality':('OFF','BALANCED','RICH'),
         'ui_studio_tab':('GALLERY','DESIGN','TOOLS'), 'density':('COMFORT','COMPACT'), 'ui_mode':('FLOATING_PRO','STUDIO_DOCK'),
         'dock_sizing':('AUTO','MANUAL')}
LIMITS = {'font_scale':(.75,2.),'window_scale':(.75,2.),
    'ui_pattern_strength':(0.,1.),'ui_pattern_scale':(.25,4.),
    'ui_pattern_density':(.25,2.5),'ui_pattern_opacity':(0.,1.),
    'ui_pattern_contrast':(.1,2.),'ui_pattern_glow':(0.,1.),
    'ui_pattern_softness':(0.,1.), 'ui_studio_pos_x':(0.,1.),
    'ui_studio_pos_y':(0.,1.),'ui_dwos_hud_x':(0.,1.),'ui_dwos_hud_y':(0.,1.),'ui_wall_workspace_pos_x':(0.,1.),
    'ui_wall_workspace_pos_y':(0.,1.),
    'ui_pattern_amplitude':(0.,2.),'ui_pattern_parallax':(0.,1.),
    'ui_pattern_relief':(0.,1.),'ui_pattern_speed':(0.,3.),
    'button_scale':(.8,1.3),'overlay_opacity':(.25,1.),'overlay_3d_fill':(.25,.92),
    'dock_manual_width':(280,520),'dock_manual_height':(344,1100)}
BOOLS = {'ui_dwos_hud_custom','ui_dwos_hud_pinned','ui_wall_workspace_user_moved','ui_studio_collapsed','ui_custom_accent','ui_reduced_motion','ui_debug_bounds','radial_center_cursor','show_uv_issue_overlay','show_3d_issue_overlay','overlay_3d_xray'}
COLORS = {'ui_accent_color','ui_secondary_color'}


def _validate(data):
    if not isinstance(data,dict):raise ValueError('profile is not object')
    clean={}
    # Pass 12: old 40-pattern profiles must not become un-loadable. Store remains
    # schema 1; map old pattern to the closest premium family on restore.
    old_pattern=str(data.get('ui_pattern','')).upper()
    if old_pattern and old_pattern not in ENUMS['ui_pattern']:
        if old_pattern.lower() not in LEGACY_PATTERN_IDS:
            raise ValueError('unknown pattern: '+old_pattern)
        legacy_map={'ISOBARS':'CONTOURS','TERRACES':'STRATA','LATTICE':'PRISM',
                    'HALO':'AURORA','CAUSTICS':'FLOW','FACETS':'PRISM',
                    'SURVEY':'CONTOURS','RIDGES':'STRATA','DUNES':'FLOW',
                    'WAVES':'FLOW','PULSE':'AURORA','NEBULA':'AURORA',
                    'MAGNETIC_FIELD':'FLOW','GLACIER':'PRISM'}
        data=dict(data);data['ui_pattern']=legacy_map.get(old_pattern,'CONTOURS')
    for key,value in data.items():
        if key not in FIELDS:continue
        if key in ENUMS:
            if not isinstance(value,str) or value not in ENUMS[key]:raise ValueError('bad enum: '+key)
        elif key in LIMITS:
            from math import isfinite
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not isfinite(value) or not LIMITS[key][0]<=value<=LIMITS[key][1]:
                raise ValueError('bad number: '+key)
            value=float(value)
        elif key in BOOLS:
            if not isinstance(value,bool):raise ValueError('bad bool: '+key)
        elif key in COLORS:
            from math import isfinite
            if not isinstance(value,(list,tuple)) or len(value)!=3 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not isfinite(v) or not 0<=v<=1 for v in value):
                raise ValueError('bad color: '+key)
            value=list(value)
        if key in {'dock_manual_width','dock_manual_height'}:
            if not float(value).is_integer():raise ValueError('bad integer dimension')
            value=int(value)
        clean[key]=value
    from_values(clean) # sanity resolver must also stay pure
    return clean


def _paths(config_dir):
    root=Path(config_dir)
    return root/FILE_NAME,root/(FILE_NAME+'.bak')


def serialize_preferences(prefs):
    def value(k):
        v=getattr(prefs,k)
        if k in COLORS:return [float(x) for x in v]
        if k in LIMITS:return int(v) if k in {'dock_manual_width','dock_manual_height'} else float(v)
        if k in BOOLS:return bool(v)
        return str(v)
    return _validate({k:value(k) for k in FIELDS})


def read(config_dir):
    """Return (profile, source). Never auto-write while recovering a bad file."""
    main,backup=_paths(config_dir)
    failures=[]
    for name,path in (('primary',main),('backup',backup)):
        if not path.exists():continue
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data,dict):raise ValueError('profile root is not object')
            if data.get('schema')!=SCHEMA:raise ValueError('unsupported schema')
            return _validate(data['values']),name
        except (ValueError,TypeError,KeyError,UnicodeError,OSError) as exc:
            failures.append(f'{name}: {type(exc).__name__}: {exc}')
    if failures:raise ValueError('; '.join(failures))
    return None,'missing'


def write(config_dir, values):
    """Atomic replace plus last-known-good backup; no partial JSON files."""
    clean=_validate(values)
    primary,backup=_paths(config_dir)
    primary.parent.mkdir(parents=True,exist_ok=True)
    if primary.exists():
        try:
            previous=json.loads(primary.read_text(encoding='utf8'))
            if not isinstance(previous,dict):raise ValueError('profile root is not object')
            if previous.get('schema')==SCHEMA:_validate(previous['values'])
            else:raise ValueError('unsupported')
        except (OSError,ValueError,KeyError,TypeError):pass
        else:shutil.copy2(primary,backup)
    payload=json.dumps({'schema':SCHEMA,'values':clean},ensure_ascii=False,sort_keys=True,indent=2)+'\n'
    tmp_name=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=primary.parent,
              prefix='.appearance-',suffix='.tmp',delete=False) as f:
            tmp_name=f.name
            f.write(payload);f.flush();os.fsync(f.fileno())
        os.replace(tmp_name,primary)
    finally:
        if tmp_name and os.path.exists(tmp_name):os.unlink(tmp_name)
    return primary


def restore_into_preferences(prefs, profile):
    clean=_validate(profile)
    for k,v in clean.items():setattr(prefs,k,v)
    return clean


def migrate_legacy(prefs):
    """One-time legacy interpretation; original RNA props stay for old blends."""
    old=str(getattr(prefs,'ui_theme','OBSIDIAN')).upper()
    scheme={'OBSIDIAN':'obsidian','BLUEPRINT':'cobalt',
            'TITANIUM':'sand','EMBER':'copper'}.get(old,'obsidian')
    # Some older UV WALL sessions have local animation disabled.
    motion='OFF' if getattr(prefs,'dock_animation','ON')=='OFF' else 'BALANCED'
    return {'ui_scheme':scheme.upper(),'ui_motion_quality':motion}
