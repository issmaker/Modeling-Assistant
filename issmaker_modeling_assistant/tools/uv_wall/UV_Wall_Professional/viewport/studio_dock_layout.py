"""Pure geometry and content schema for the NEXTGEN Viewport Studio Dock.

No Blender imports: layout constraints are checked independently of GPU/runtime.
The native sidebar is only a launcher/diagnostics surface; this dock is drawn
inside the Viewport window with one durable inspector, not moving editors.
"""

DOCK_WIDTH = 334.0
GIZMO_GUTTER = 146.0
LEFT_TOOLBAR = 58.0

MODE_FIELDS = {
    'WALL': (
        ('SECTION', 'ГЕОМЕТРИЯ СТЕНЫ'),
        ('EDIT:module_standard','Размер модуля'),
        ('EDIT:stories','Секции'),
        ('TOGGLE:custom_first_story','Особая первая секция'),
        ('EDIT:first_story_height','Высота первой секции'),
        ('EDIT:wall_alignment','Положение'),
        ('EDIT:wall_offset','Доп. смещение'),
        ('EDIT:wall_module_rotation','Поворот UV'),
        ('TOGGLE:create_inner','Вторая стена'),
        ('EDIT:inset_distance','Толщина'),
        ('TOGGLE:close_ends','Торцы открытого пути'),
        ('TOGGLE:close_start_end','Торец · начало'),
        ('TOGGLE:close_finish_end','Торец · конец'),
        ('SECTION', 'КРЫШКИ И МАТЕРИАЛЫ'),
        ('TOGGLE:create_top_cap','Верхняя крышка'),
        ('TOGGLE:cap_material_override','Материал верха отдельно'),
        ('RESOURCE:cap_material','Материал верха'),
        ('TOGGLE:create_bottom_cap','Нижняя крышка'),
        ('TOGGLE:bottom_cap_material_override','Материал низа отдельно'),
        ('RESOURCE:bottom_cap_material','Материал низа'),
        ('EDIT:cap_rotation','UV крышек'),
        ('SECTION', 'ВНУТРЕННЯЯ ПОВЕРХНОСТЬ'),
        ('TOGGLE:create_inner_surface','Грунт'),
        ('EDIT:inner_surface_height_offset','Высота грунта'),
        ('RESOURCE:inner_surface_material','Материал грунта'),
    ),
    'SURFACE': (
        ('SECTION', 'ПОВЕРХНОСТЬ'),
        ('EDIT:module_standard','Размер модуля'),
        ('EDIT:surface_height_offset','Смещение Z'),
        ('EDIT:surface_module_rotation','Поворот UV'),
        ('EDIT:roof_normal','Нормали'),
        ('EDIT:surface_method','Метод поверхности'),
    ),
    'CORNICE': (
        ('SECTION', 'ГЕОМЕТРИЯ КАРНИЗА'),
        ('RESOURCE:cornice_profile','Профиль'),
        ('EDIT:cornice_profile_rotation','Поворот профиля'),
        ('TOGGLE:cornice_flip_side','Обратная сторона'),
        ('TOGGLE:cornice_profile_flip_x','Отразить X'),
        ('TOGGLE:cornice_profile_flip_y','Отразить Z'),
        ('TOGGLE:cornice_reverse_path','Обратный путь'),
        ('EDIT:module_standard','Размер модуля'),
        ('EDIT:cornice_uv_mode','UV карниза'),
        ('EDIT:cornice_linear_step','Шаг вдоль пути'),
    ),
}


def dock_rect(region_width, region_height, ui_scale=1.0):
    """Return viewport-local (x, y, width, height) or a collapsed mini dock.

    Larger windows use a dedicated gizmo gutter so navigation is never
    obstructed. Narrow windows move below the gizmo safe zone instead.
    """
    rw = max(0.0, float(region_width)); rh = max(0.0, float(region_height))
    scale = max(.75, min(1.6, float(ui_scale)))
    width = min(DOCK_WIDTH*scale, max(0., rw-LEFT_TOOLBAR*scale-24.))
    if rw < 235 or rh < 170:
        return None
    gutter = GIZMO_GUTTER*scale if rw >= width + GIZMO_GUTTER*scale + LEFT_TOOLBAR*scale+28 else 12.*scale
    x = max(LEFT_TOOLBAR*scale+4., rw-width-gutter)
    top = rh-51.*scale if gutter>30.*scale else rh-285.*scale
    bottom = 65.*scale
    if top-bottom<300.*scale:
        # A narrow Viewport still gets a usable mode launcher, not an invisible
        # full-size panel intercepting mouse input or zoom.
        badge_h=min(56.*scale,max(30.,rh-16.))
        top = min(rh-8.,max(bottom+badge_h,rh-53.*scale))
        return (x, max(8.,top-badge_h), width, badge_h)
    return (x,bottom,width,top-bottom)


def dock_row_visibility(mode, definition, *, path_closed=False):
    """Filter optional fields without changing their identity/click targets."""
    rows = MODE_FIELDS.get(mode, MODE_FIELDS['WALL'])
    result=[]
    for key, label in rows:
        if key == 'EDIT:first_story_height' and not getattr(definition,'custom_first_story',False):
            continue
        if key in {'EDIT:inset_distance','EDIT:inset_side'} and not getattr(definition,'create_inner',False):
            continue
        if key in {'TOGGLE:close_ends','TOGGLE:close_start_end','TOGGLE:close_finish_end'}:
            if path_closed or not getattr(definition,'create_inner',False):continue
            if key!='TOGGLE:close_ends' and not getattr(definition,'close_ends',False):continue
        if key == 'TOGGLE:create_top_cap' and not (getattr(definition,'create_inner',False) or path_closed):
            continue
        if key == 'TOGGLE:cap_material_override' and not getattr(definition,'create_top_cap',False):
            continue
        if key == 'RESOURCE:cap_material' and not (getattr(definition,'create_top_cap',False) and getattr(definition,'cap_material_override',False)):
            continue
        if key == 'TOGGLE:create_bottom_cap' and not (getattr(definition,'create_inner',False) or path_closed):
            continue
        if key == 'TOGGLE:bottom_cap_material_override' and not getattr(definition,'create_bottom_cap',False):
            continue
        if key == 'RESOURCE:bottom_cap_material' and not (getattr(definition,'create_bottom_cap',False) and getattr(definition,'bottom_cap_material_override',False)):
            continue
        if key in {'TOGGLE:create_inner_surface','EDIT:inner_surface_height_offset','RESOURCE:inner_surface_material'}:
            if not getattr(definition,'create_inner',False) or not path_closed:continue
            if key != 'TOGGLE:create_inner_surface' and not getattr(definition,'create_inner_surface',False):continue
        result.append((key,label))
    return tuple(result)


def visible_dock_rows(rows, scroll, max_rows):
    capacity=max(0,int(max_rows)); top=max(0,len(rows)-capacity)
    start=min(max(0,int(scroll)), top)
    return tuple(rows[start:start+capacity]),start,top


# 1.16.0 content-driven geometry; the old dock_rect remains the viewport-safe
# bounding rectangle for compatibility and comparison tests.
def composed_rows(mode, definition, collapsed=(), favorites=(), *, path_closed=False):
    """Flatten only visible sections; pin favorites once, never duplicate values.

    Identity of fields is preserved so hit testing, undo and resource dispatch
    share precisely the same Definition field in both presentations.
    """
    raw = dock_row_visibility(mode, definition, path_closed=path_closed)
    collapsed = set(collapsed)
    favorites_order = tuple(favorites)
    favorites = set(favorites_order)
    valid_keys = {key for key, _ in raw if key != 'SECTION'}
    row_by_key = {key:label for key,label in raw if key!='SECTION'}
    pinned = [(key,row_by_key[key]) for key in favorites_order if key in row_by_key]
    result = ([('SECTION', 'ИЗБРАННОЕ')] + pinned) if pinned else []
    active = ''
    for key, label in raw:
        if key == 'SECTION':
            active = label
            result.append((key, label))
        elif key in favorites:
            continue
        elif active not in collapsed:
            result.append((key,label))
    # Avoid blank section labels when all their fields are pinned elsewhere.
    cleaned=[]
    for i, row in enumerate(result):
        if row[0] == 'SECTION' and (i+1 == len(result) or result[i+1][0] == 'SECTION'):
            if row[1] in collapsed:
                cleaned.append(row)  # Collapsed groups must remain reopenable.
            continue
        cleaned.append(row)
    return tuple(cleaned)


def content_dock_rect(region_width, region_height, ui_scale, rows,
                      sizing='AUTO', manual_width=342, manual_height=580):
    """Fit visible content and reserve real Blender UI. Never make a hitbox
    outside the viewport. An oversize inspector is scrollable, not clipped.
    """
    base = dock_rect(region_width,region_height,ui_scale)
    if base is None:
        return None
    bx,by,bw,bh=base
    scale=max(.75,min(1.6,float(ui_scale)))
    if bw<280*scale or bh<344*scale:
        return base
    # Pixel width includes two label/value columns, resource caption and star.
    longest=max((len(str(label)) for _key,label in rows),default=16)
    ideal=max(304., min(420., 272.+longest*4.6)) * scale
    if sizing=='MANUAL':ideal=max(280.,min(520.,float(manual_width)))*scale
    right_edge=bx+bw
    min_left=LEFT_TOOLBAR*scale+4.
    width=min(ideal,max(0.,right_edge-min_left))
    # Header/source/material toolbar 219, footer 83, row pitch 36; 8 padding.
    desired=(310.+36.*len(rows))*scale
    if sizing=='MANUAL':desired=max(344.,min(1100.,float(manual_height)))*scale
    height=min(bh,max(344.*scale,desired))
    x=right_edge-width  # same right edge; gizmo clearance remains valid
    y=by+bh-height  # same top edge; shrink upwards, not downwards
    return (x,y,width,height)


def smoothing_step(previous, target, alpha=.30):
    """Stable interpolation; usable without timers and no overshooting."""
    if previous is None or abs(float(previous)-float(target))<.65:
        return float(target)
    alpha=max(.08,min(1.,float(alpha)))
    return float(previous)+(float(target)-float(previous))*alpha
