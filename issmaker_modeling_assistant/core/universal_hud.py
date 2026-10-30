"""ISSMAKER Universal HUD Framework (Pass 15.0).

Blender-free component specification, compact workspace header actions,
restoration glyphs, and mode metadata.  Tools retain their event handlers,
authoritative geometry, and existing Blender transactions; this module owns
*only* common appearance semantics / measured hit rectangles.
"""
from dataclasses import dataclass
from math import isfinite

MODES = (
    ('WALL', 'Стена', 'wall'),
    ('SURFACE', 'Поверхность', 'surface'),
    ('CORNICE', 'Карниз', 'cornice'),
)

@dataclass(frozen=True)
class PanelDescriptor:
    key: str
    title: str
    icon: str
    tool: str = 'UV_WALL'
    group: str = 'RIGHT'

@dataclass(frozen=True)
class ActionSpec:
    key: str
    hint: str
    glyph: str

WORKSPACE_ACTIONS = (
    ActionSpec('GROUP_HIDE_ALL', 'Скрыть все окна — вернуть через иконки', '◉'),
    ActionSpec('GROUP_COLLAPSE_ALL', 'Свернуть или развернуть все разделы', '▱'),
    ActionSpec('GROUP_CLOSE_ALL', 'Закрыть все редакторы (несохранённые черновики будут потеряны)', '×'),
)

PANEL_ICONS = {
    'HEIGHT':'height', 'THICKNESS':'thickness', 'PLACEMENT':'placement',
    'NORMALS':'normals','WALL':'wall','CAP':'cap','PLANT':'plant',
    'SURFACE':'surface','CORNICE':'cornice','CORNICE_ROT':'rotation',
    'CORNICE_SIDE':'mirror',
}

def panel_descriptor(key, title):
    if not key or not str(title): raise ValueError('A panel needs a key and a title')
    return PanelDescriptor(str(key),str(title),PANEL_ICONS.get(str(key),'generic'))

def normalize_workspace_anchor(group_rect, region, scale=1.):
    """User drag -> normalized free-space anchor independent of resolution."""
    from math import isfinite
    x0,y0,x1,y1=(float(v) for v in group_rect)
    rw,rh=(float(v) for v in region)
    scale=float(scale)
    if not all(isfinite(v) for v in (x0,y0,x1,y1,rw,rh,scale)) or scale<=0:
        raise ValueError('Invalid workspace drag')
    if x1<=x0 or y1<=y0 or rw<=0 or rh<=0:raise ValueError('Invalid bounds')
    px=max(0.,rw-(x1-x0)-16*scale)
    py=max(0.,rh-(y1-y0)-16*scale)
    return (max(0.,min(1.,(x0-8*scale)/px)) if px>0 else 0.,
            max(0.,min(1.,(y0-8*scale)/py)) if py>0 else 0.)


def marker_zones(key, rect, hovered, *, secondary_enabled=True):
    """Persistent visible halves: no invisible secondary click surface.

    The first zone remains full width when the second action is unavailable.
    W_HEIGHT and single-value rotation/Z cards never need a popup half.
    """
    if key not in {'W_THICKNESS','W_WALL','W_CAP','W_PLANT',
                                   'C_SIDE','C_PROFILE'}:
        return ()
    x0,y0,x1,y1=(float(v) for v in rect)
    if not secondary_enabled:
        return ((key+':LEFT',(x0,y0,x1,y1)),)
    xm=(x0+x1)/2
    return ((key+':LEFT',(x0,y0,xm,y1)),(key+':RIGHT',(xm,y0,x1,y1)))


def marker_action(key, half, *, double=False, top_cap=False, plant=False):
    """Pure routing manifest, shared by hit testing and context audit."""
    actions={
        'W_HEIGHT': ('INLINE_HEIGHT', None),
        'W_WALL': ('DETAIL_POSITION', 'INLINE_OFFSET'),
        'W_THICKNESS': ('DETAIL_CONSTRUCTION', 'INLINE_THICKNESS' if double else None),
        'W_CAP': ('TOGGLE_CAP','DETAIL_CAP' if top_cap else None),
        'W_PLANT': ('TOGGLE_PLANT','DETAIL_PLANT' if plant else None),
        'S_Z': ('INLINE_SURFACE_Z',None),
        'S_ROT': ('INLINE_SURFACE_ROT',None),
        'S_DIR': ('TOGGLE_SURFACE_DIRECTION',None),
        'C_ROT': ('INLINE_CORNICE_ROT',None),
        'C_SIDE': ('TOGGLE_CORNICE_SIDE','DETAIL_CORNICE_SIDE'),
        'C_PROFILE': ('PICK_PROFILE','DETAIL_CORNICE'),
    }
    if key not in actions or half not in {'LEFT','RIGHT'}:
        return None
    return actions[key][0 if half=='LEFT' else 1]


def height_from_signed_drag(start_height, start_direction, displacement, minimum=.01):
    """Signed displacement measured from the local path at the handle.

    Above local path -> UP; below -> DOWN. Never reference the scene origin
    or global min/max Z, which would fail for a path with variable elevations.
    """
    from math import isfinite
    height=float(start_height); delta=float(displacement)
    if not isfinite(height) or not isfinite(delta) or height<0 or minimum<=0:
        raise ValueError('Invalid height gesture')
    signed_start=height*(-1 if str(start_direction)=='DOWN' else 1)
    signed_end=signed_start+delta
    direction='DOWN' if signed_end < -minimum else ('UP' if signed_end > minimum else str(start_direction))
    return max(minimum,abs(signed_end)),direction


def group_header_height(scale=1., hidden_count=0):
    scale=float(scale)
    if not isfinite(scale) or scale<=0:raise ValueError('Invalid scale')
    return (36. + (30. if hidden_count else 0.))*scale

def group_control_rects(outer, scale=1., hidden=()):
    """Measured button rectangles used unchanged by rendering AND hit-testing.

    All rectangles are inside the reserved group header and do not overlap.
    Hidden panel glyphs wrap within one icon rail; remaining windows can be
    restored through the generic Visibility popup if there are too many.
    """
    x0,y0,x1,y1=(float(x) for x in outer)
    scale=float(scale)
    if not all(isfinite(v) for v in (x0,y0,x1,y1,scale)) or x1<=x0 or y1<=y0 or scale<=0:
        return ()
    size=25.*scale
    gap=5.*scale
    margin=8.*scale
    y=y1-30.*scale
    actions=[]
    for idx,action in enumerate(WORKSPACE_ACTIONS):
        right=x1-margin-idx*(size+gap)
        rect=(right-size,y,right,y+size)
        if rect[0]>=x0+130.*scale:actions.append((action.key,action.hint,rect))
    # The header itself is draggable; individual control rects take priority.
    drag=(x0+8*scale,y1-32*scale,x1-106*scale,y1-4*scale)
    if drag[2]>drag[0]+24*scale:actions.insert(0,('GROUP_DRAG','Переместить всю группу · позиция сохранится',drag))
    if hidden:
        rail_y=y1-62.*scale
        for idx,panel in enumerate(hidden):
            left=x0+margin+idx*(size+gap)
            if left+size>x1-margin:break
            actions.append(('GROUP_RESTORE:'+str(panel.key), 'Показать: '+panel.title,
                            (left,rail_y,left+size,rail_y+size)))
    return tuple(actions)

def icon_segments(icon, rect):
    """Tiny normalized vector glyphs. No font icon dependencies or images."""
    x0,y0,x1,y1=(float(v) for v in rect)
    if x1<=x0 or y1<=y0:return ()
    cx=(x0+x1)*.5;cy=(y0+y1)*.5
    dx=(x1-x0)*.70;dy=(y1-y0)*.64
    def p(a,b):return (cx+(a-.5)*dx,cy+(b-.5)*dy)
    shapes={
        'hide':(((.10,.48),(.34,.72)),((.34,.72),(.66,.72)),((.66,.72),(.90,.48)),
                ((.10,.48),(.34,.25)),((.34,.25),(.66,.25)),((.66,.25),(.90,.48)),
                ((.19,.12),(.81,.85))),
        'collapse':(((.16,.72),(.5,.34)),((.5,.34),(.84,.72))),
        'close':(((.20,.20),(.80,.80)),((.20,.80),(.80,.20))),
        'pin':(((.22,.69),(.78,.69)),((.78,.69),(.5,.43)),((.5,.43),(.22,.69)),
               ((.5,.43),(.5,.08))),
        'height':(((.48,.10),(.54,.89)),((.30,.75),(.52,.92)),((.52,.92),(.73,.75))),
        'thickness':(((.16,.17),(.16,.84)),((.80,.17),(.80,.84)),((.25,.52),(.71,.52))),
        'placement':(((.10,.50),(.89,.50)),((.10,.50),(.28,.70)),((.10,.50),(.28,.30)),((.89,.50),(.72,.70)),((.89,.50),(.72,.30))),
        'normals':(((.16,.77),(.81,.77)),((.5,.67),(.5,.16)),((.5,.16),(.35,.33)),((.5,.16),(.65,.33))),
        'cap':(((.12,.79),(.86,.79)),((.2,.2),(.2,.77)),((.77,.2),(.77,.77))),
        'plant':(((.10,.28),(.9,.28)),((.29,.32),(.42,.70)),((.42,.70),(.7,.40))),
        'wall':(((.12,.18),(.12,.85)),((.88,.18),(.88,.85)),((.12,.18),(.88,.18)),
                ((.12,.52),(.88,.52)),((.12,.85),(.88,.85)),((.5,.18),(.5,.52)),
                ((.31,.52),(.31,.85)),((.69,.52),(.69,.85))),
        'surface':(((.07,.24),(.31,.56)),((.31,.56),(.46,.39)),
                   ((.46,.39),(.68,.79)),((.68,.79),(.95,.24)),
                   ((.08,.24),(.95,.24)),((.20,.19),(.80,.19))),
        'cornice':(((.13,.18),(.13,.84)),((.13,.84),(.88,.84)),
                   ((.88,.84),(.88,.68)),((.88,.68),(.63,.68)),
                   ((.63,.68),(.63,.50)),((.63,.50),(.40,.50)),
                   ((.40,.50),(.40,.18)),((.12,.18),(.40,.18))),
        'rotation':(((.18,.18),(.74,.18)),((.74,.18),(.86,.68)),((.86,.68),(.68,.56))),
        'mirror':(((.5,.15),(.5,.85)),((.18,.35),(.35,.5)),((.35,.5),(.18,.65)),((.82,.35),(.65,.5)),((.65,.5),(.82,.65))),
    }
    return tuple((p(*a),p(*b)) for a,b in shapes.get(icon,shapes['wall']))

def parity_contract():
    return {'component_source':'core.window_shell + core.workspace_components',
            'geometry_source':'tool-owned model callbacks',
            'dock_policy':'one unified right-hand group, even with one panel',
            'restore':'icons survive hide, drafts remain owned by tool',
            'mode_policy':'icon + tooltip, active mode is always visible',
            'modal_policy':'no modal handler in the UI contract'}
