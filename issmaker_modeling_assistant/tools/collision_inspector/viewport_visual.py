"""Pure screen-space marker presentation. No Blender imports or Mesh mutations.

Keeps interaction hit rectangles, animation maths and rings directly testable
outside Blender. Coordinates are WINDOW-region-local pixel coordinates.
"""
import math

STYLES = ('DOT', 'HALO', 'BEACON')
STYLE_NAMES = {'DOT': 'Точка', 'HALO': 'Кольцо', 'BEACON': 'Маяк'}
# One always-visible button replaces the previous five-button ribbon.
TOOLBAR = (('MENU', 'Режим маркеров', 140),)
MENU_ROWS = (
    ('DOT', 'Точка'),
    ('HALO', 'Кольцо'),
    ('BEACON', 'Маяк'),
    ('PULSE', 'Пульсация'),
    ('ALL', 'Показать все'),
    ('XRAY', 'X-Ray (сквозь модель)'),
)


def _scale(value):
    """One factor for draw and hit boxes. Default 1.0 retains old geometry."""
    try:
        factor=float(value)
    except (TypeError,ValueError,OverflowError):return 1.0
    return max(.75,min(2.,factor)) if math.isfinite(factor) else 1.0


def _srect(rect, scale):
    return tuple(float(v)*scale for v in rect)


def controls(width, height, style='HALO', *, scale=1.0):
    """One compact selector, hidden if enlarged dropdown would escape WINDOW."""
    k=_scale(scale)
    if width < 415*k or height < 290*k:return ()
    name = STYLE_NAMES.get(style, STYLE_NAMES['HALO'])
    return (('MENU', name + '  ▾', _srect((72.0, 72.0, 140.0, 29.0), k)),)


def menu_rect(width, height, *, scale=1.0):
    """A menu replaces (rather than obscures) the left information card."""
    if not controls(width, height, scale=scale):
        return None
    return _srect((72.0, 106.0, 180.0, 174.0), _scale(scale))


def menu_controls(width, height, expanded=False, *, scale=1.0):
    if not expanded or menu_rect(width, height, scale=scale) is None:
        return ()
    bx, by, bw, bh = menu_rect(width, height, scale=scale)
    # Blender region has origin at bottom-left; first row is topmost.
    k=_scale(scale)
    return tuple((action, label, (bx+6*k, by+bh-6*k-(i+1)*27*k, bw-12*k, 26.0*k))
                 for i, (action, label) in enumerate(MENU_ROWS))


def hit_control(width, height, x, y, expanded=False, *, scale=1.0):
    """The menu never steals clicks beyond its concrete screen rectangles."""
    for action, _label, (bx, by, bw, bh) in menu_controls(width, height, expanded, scale=scale):
        if bx <= x <= bx+bw and by <= y <= by+bh:
            return action
    for action, _label, (bx, by, bw, bh) in controls(width, height, scale=scale):
        if bx <= x <= bx+bw and by <= y <= by+bh:
            return action
    if expanded:
        panel = menu_rect(width, height, scale=scale)
        if panel is not None:
            bx, by, bw, bh = panel
            if bx <= x <= bx+bw and by <= y <= by+bh:
                return 'MENU_PANEL'  # Never pass empty menu gutter clicks to Blender.
    return None


def pulse_value(seconds, index=0, enabled=True):
    """Slow continuous brightness breathing; intentionally never fully hidden."""
    if not enabled:
        return 0.65
    return 0.56 + 0.44 * (.5 + .5 * math.sin(seconds * 2 * math.pi * .74 + index * .63))


def marker_radius(style, active, scale=1.0):
    base = {'DOT': 9.0, 'HALO': 13.0, 'BEACON': 18.0}.get(style, 13.0)
    return base * (1.16 if active else 1.0) * max(.65, min(1.8, scale))


def ring_segments(x, y, radius, count=32):
    """One LINE list suitable for GPU batch_for_shader; constant pixel radius."""
    if not all(math.isfinite(v) for v in (x, y, radius)) or radius <= 0:
        return []
    count = max(12, min(int(count), 64))
    result = []
    for i in range(count):
        a = i*2*math.pi/count
        b = (i+1)*2*math.pi/count
        result.extend(((x+math.cos(a)*radius, y+math.sin(a)*radius),
                       (x+math.cos(b)*radius, y+math.sin(b)*radius)))
    return result


# Bottom-center command surface; each rectangle is WINDOW-region-local.
# The label/count is not an interactive control. No Blender data is touched here.
FOOTER_BUTTONS = (
    ('PREV', '◀', 42),
    ('COUNT', '', 74),
    ('NEXT', '▶', 42),
    ('GROUP', 'Все ▾', 98),
    ('CHECK', 'ПРОВЕРИТЬ ВСЁ', 142),
)
GROUP_CHOICES = (
    ('ALL', 'Все'),
    ('CRITICAL', 'Критичные'),
    ('TOPOLOGY', 'Топология'),
    ('SHAPE', 'Форма'),
    ('CLEARANCE', 'Зазоры'),
)


def group_name(value):
    return dict(GROUP_CHOICES).get(value, 'Все')


def group_menu_rect(width, height, can_repair=False, *, scale=1.0, scope=None):
    """One strict menu rectangle shared by rendering and hit testing.

    The previous fixed footer anchor could cover the bottom-left issue card
    at ordinary viewport widths.  Never use a partially colliding fallback.
    """
    from issmaker_modeling_assistant.core.overlay_placement import StrictOverlayAllocator
    buttons = footer_controls(width,height,can_repair=can_repair,scale=scale)
    if not buttons:
        from issmaker_modeling_assistant.core import window_visibility
        window_visibility.observe(scope,"COLLISION_GROUP_MENU",None)
        return None
    gx, gy, gw, gh = next(rect for action, _name, rect, _enabled in buttons if action == 'GROUP')
    k=_scale(scale)
    mw, mh = 166.*k, 132.*k
    manager=StrictOverlayAllocator(width,height,margin=6.*k,gap=6.*k)
    left=min(rect[0] for _,_,rect,_ in buttons)-7*k
    bottom=min(rect[1] for _,_,rect,_ in buttons)-6*k
    right=max(rect[0]+rect[2] for _,_,rect,_ in buttons)+7*k
    top=max(rect[1]+rect[3] for _,_,rect,_ in buttons)+6*k
    manager.reserve('FOOTER',(left,bottom,right,top))
    # Pass 15.9: group menu is also an allocated peer, not a private popup.
    # Both draw and hit_group_menu call this identical pure placement function.
    from issmaker_modeling_assistant.core import window_visibility
    for peer_key, peer_rect in window_visibility.peer_rectangles(scope,'COLLISION_GROUP_MENU'):
        manager.reserve('peer:' + peer_key, peer_rect)
    card=hud_rect(width,height,has_data=True,scale=k)
    if card is not None:
        cx,cy,cw,ch=card
        manager.reserve('ISSUE_CARD',(cx,cy,cx+cw,cy+ch))
    # The marker-toolbar flyout may also occupy the lower-left corner.
    toolbar=menu_rect(width,height,scale=k)
    if toolbar is not None:
        tx,ty,tw,th=toolbar
        manager.reserve('MARKER_MENU_LANE',(tx,ty,tx+tw,ty+th))
    result=manager.place('GROUP_MENU',(mw,mh),(gx+gw/2-mw/2,gy+gh+12*k))
    from issmaker_modeling_assistant.core import window_visibility
    result=window_visibility.observe(scope,'COLLISION_GROUP_MENU',result)
    if result is None:
        return None
    x,y,x1,y1=result
    return (x,y,x1-x,y1-y)


def group_menu_controls(width, height, expanded=False, can_repair=False, *, scale=1.0, scope=None):
    bounds = group_menu_rect(width,height,can_repair,scale=scale,scope=scope) if expanded else None
    if bounds is None:
        return ()
    x,y,w,h = bounds
    k=_scale(scale)
    return tuple(('GROUP_' + key, name, (x+5*k, y+h-6*k-(i+1)*24*k, w-10*k, 23.*k))
                 for i, (key, name) in enumerate(GROUP_CHOICES))


def hit_group_menu(width, height, x, y, expanded=False, can_repair=False, *, scale=1.0, scope=None):
    if not expanded:
        return None
    for action, name, (bx, by, bw, bh) in group_menu_controls(width,height,True,can_repair,scale=scale,scope=scope):
        if bx <= x <= bx+bw and by <= y <= by+bh:
            return action
    rect = group_menu_rect(width,height,can_repair,scale=scale,scope=scope)
    if rect:
        bx,by,bw,bh = rect
        if bx <= x <= bx+bw and by <= y <= by+bh:
            return 'GROUP_PANEL'
    return None


def footer_controls(width, height, total=0, current=0, group='ALL', can_repair=False, *, scale=1.0):
    """Immutable and deterministic footer rectangles: centered, with safe margins."""
    k=_scale(scale)
    if width < 430*k or height < 210*k:
        return ()
    gap = 5.0*k
    # Adaptive row; no element may extend beyond the WINDOW region even with
    # FHD scaling, high-DPI, or a dynamically appearing repair action.
    actions = list(FOOTER_BUTTONS)
    if can_repair:
        actions.append(('REPAIR', 'ИСПРАВИТЬ', 120))
    min_widths={'PREV':32.,'NEXT':32.,'COUNT':61.,'GROUP':84.,'CHECK':118.,'REPAIR':103.}
    available=float(width)-2*12.*k-gap*(len(actions)-1)
    stacked=False
    if sum(min_widths[action]*k for action,_,_ in actions)>available and can_repair:
        stacked=True
        # The repair action receives its own row, never becomes unreachable.
        available=float(width)-2*12.*k-gap*(len(FOOTER_BUTTONS)-1)
        row_actions=list(FOOTER_BUTTONS)
    else:
        row_actions=actions
    if sum(min_widths[action]*k for action,_,_ in row_actions)>available:
        return ()
    nominal=sum(item[2] for item in row_actions)*k
    if nominal>available:
        minimum=sum(min_widths[action] for action,_,_ in row_actions)*k
        ratio=(available-minimum)/(nominal-minimum) if nominal>minimum else 0.
        widths={action:(min_widths[action]+(nominal_width-min_widths[action])*ratio)*k
                for action,_,nominal_width in row_actions}
    else:
        widths={action:nominal_width*k for action,_,nominal_width in row_actions}
    width_total=sum(widths.values())+gap*(len(row_actions)-1)
    x=(float(width)-width_total)*.5
    y = 25.0*k
    total = max(0, int(total))
    current = max(0, min(int(current), total))
    results = []
    for action, label, w in actions:
        if stacked and action=='REPAIR':
            repair_w=min(150.*k,float(width)-24.*k)
            results.append((action,label,((float(width)-repair_w)*.5,68.*k,repair_w,38.*k),True))
            continue
        enabled = total > 1 if action in ('PREV','NEXT') else True
        if action == 'COUNT':
            label = f'{current:02d} / {total:02d}'
            enabled = False
        elif action == 'GROUP':
            label = group_name(group) + ' ▾' 
        actual_width=widths[action]
        results.append((action, label, (x, y, actual_width, 38.0*k), enabled))
        x += actual_width + gap
    return tuple(results)


def hit_footer(width, height, x, y, total=0, current=0, group='ALL', expanded=False, can_repair=False, *, scale=1.0, scope=None):
    """Never intercept the count, disabled arrows, or clicks outside button cells."""
    menu_hit = hit_group_menu(width,height,x,y,expanded,can_repair,scale=scale,scope=scope)
    if menu_hit is not None:
        return menu_hit
    for action, _label, (bx, by, bw, bh), enabled in footer_controls(width, height, total, current,group,can_repair,scale=scale):
        if enabled and bx <= x <= bx + bw and by <= y <= by + bh:
            return action
    return None


def hud_rect(width, height, has_data=True, *, scale=1.0):
    """Bottom-left information card; top header, native toolbar and footer kept free.

    Suppress rather than overlap if the viewport cannot contain the card.
    Shared source of truth for drawing and offline layout tests.
    """
    k=_scale(scale)
    if width < 490*k or height < 320*k:
        return None
    x, y = 72.0*k, 117.0*k
    card_width = min(410.0*k, float(width)-90.0*k)
    card_height = (134.0 if has_data else 82.0)*k
    if y + card_height + 18*k > float(height):
        return None
    return x, y, card_width, card_height
