"""W57.2: Surface DWOS adapter within the EXISTING viewport_composer modal/HUD.
No new modal, no duplicate drawing owner, no change to the legacy DWOS geometry.
"""
from __future__ import annotations
import bpy
import json
import math
from . import surface_steps as surface

DRAFT_KEY = "DWOS_W57_composer_draft"
FIELDS = {
    "frame_width": ("Рама / inset", .001, 2.0, .005),
    "frame_depth": ("Глубина рамы", .001, 3.0, .005),
    "window_offset": ("Посадка окна", -5.0, 5.0, .005),
    "interior_depth": ("Глубина Interior", .8, 1.5, .01),
    "mullion_width": ("Ширина импоста", .001, 1.0, .005),
    "mullion_depth": ("Глубина импоста", .001, 3.0, .005),
    "zone_split_thickness": ("Толщина зон", .001, 1.0, .005),
    "revised_frame_width": ("Новая рама", .001, 2.0, .005),
    "revised_mullion_width": ("Новый импост", .001, 1.0, .005),
}
PRESETS = ("V", "H", "CROSS", "TWO_H", "TWO_V", "GRID6")
PRESET_NAMES = ("1 V", "1 H", "CROSS", "2 H", "2 V", "GRID6")


def sessions(context):
    """Authoritative host sessions; never count LP/HP as distinct windows."""
    result = []
    for obj in context.scene.objects:
        if obj.type == "MESH" and obj.get(surface.SESSION_KEY):
            try:
                state = json.loads(str(obj[surface.SESSION_KEY]))
                if isinstance(state, dict) and state.get("host") == obj.name:
                    result.append((obj, state))
            except (ValueError, TypeError, KeyError):
                continue
    result.sort(key=lambda pair: pair[0].name)
    return result


def resolve_host(context):
    obj = context.active_object
    if obj is None or obj.type != "MESH":
        return None
    if obj.get(surface.SESSION_KEY):
        return obj
    name = obj.get(surface.HOST_NAME_KEY)
    if name:
        host = bpy.data.objects.get(str(name))
        if host is not None and host.get(surface.SESSION_KEY):
            return host
    # The same Composer can initialize the native surface session directly
    # from user-selected source polygons, without an extra HUD.
    if context.mode == "EDIT_MESH" and obj.mode == "EDIT":
        import bmesh
        bm = bmesh.from_edit_mesh(obj.data)
        if any(f.select for f in bm.faces):
            return obj
    return None


def current(op, context):
    host = bpy.data.objects.get(str(op._obj_name))
    if host is None or context.scene.objects.get(host.name) is not host:
        raise ValueError("Исходная стена удалена из сцены.")
    value = host.get(surface.SESSION_KEY)
    if not value:
        return host, None, 0
    state = json.loads(str(value))
    if not isinstance(state, dict):
        raise ValueError("Повреждена сессия DWOS.")
    return host, state, int(state.get("step", 0))


def draft(host, context):
    defaults = context.scene.dwos_surface_lab
    try:
        stored = json.loads(str(host.get(DRAFT_KEY, "{}")))
        if not isinstance(stored, dict):
            stored = {}
    except (ValueError, TypeError):
        stored = {}
    # Every existing window owns its authored params. Never inherit values
    # from the last edited OTHER window through shared Scene properties.
    authored = {}
    if host.get(surface.SESSION_KEY):
        try:
            authored = json.loads(str(host[surface.SESSION_KEY])).get("parameters", {})
        except (ValueError, TypeError, KeyError, AttributeError):
            authored = {}
    return {name: float(stored.get(name,authored.get(name,
                 defaults.bl_rna.properties[name].default if authored else getattr(defaults,name))))
            for name in FIELDS}


def set_draft(host, context, name, value):
    if name not in FIELDS:
        raise ValueError("Неизвестный параметр DWOS.")
    minimum, maximum = FIELDS[name][1:3]
    value = float(value)
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ValueError(f"{FIELDS[name][0]}: допустимо {minimum:g}–{maximum:g} м.")
    changes = draft(host, context)
    changes[name] = round(value, 5)
    host[DRAFT_KEY] = json.dumps(changes, sort_keys=True)
    setattr(context.scene.dwos_surface_lab, name, changes[name])


def sync_draft(host, context):
    values = draft(host, context)
    props = context.scene.dwos_surface_lab
    for name, value in values.items():
        setattr(props, name, value)


def fields_for(stage):
    if stage < 2:
        return ("frame_width",)
    if stage < 3:
        return ("frame_depth", "window_offset")
    if stage < 6:
        return ("interior_depth",) if stage == 5 else ()
    if stage == 6:
        return ("mullion_width", "mullion_depth", "zone_split_thickness")
    if stage == 7:
        return ("revised_frame_width", "revised_mullion_width")
    return ()


def zone_ids(session):
    from . import zone_core, surface_graph, surface_step_core
    if session is None or int(session.get("step", 0)) < 2:
        return []
    points = session["source"]["points"]
    axes = surface_step_core.rectangle_axes(points) or surface_graph._orthogonal_bounds(points)
    if axes is None:
        return []
    x0, x1, y0, y1 = axes
    tree = zone_core.decode(session.get("zone_tree_pending", ""))
    leaves, _bands = zone_core.layout(
        tree, x1-x0, y1-y0, float(session["parameters"]["frame_width"]))
    return [str(zid) for zid, _rect in leaves]



def _point_in_poly(x, y, points):
    inside=False
    for a, b in zip(points, points[1:]+points[:1]):
        if (a[1]>y)!=(b[1]>y):
            at=a[0]+(b[0]-a[0])*(y-a[1])/(b[1]-a[1])
            if x<at:
                inside=not inside
    return inside


def _draw_viewport_zone_guide(op, context, host, state, stage):
    """Only a HUD overlay: source boundary/face UV remain completely unchanged."""
    op._surface_zone_screen_hits=[]
    if not state or stage<2 or not context.region_data:
        return
    from . import composer as ui, zone_core, surface_graph, surface_step_core
    from bpy_extras import view3d_utils
    from mathutils import Vector
    try:
        points=state["source"]["points"]
        axes=surface_step_core.rectangle_axes(points) or surface_graph._orthogonal_bounds(points)
        x0,x1,y0,y1=axes
        tree=zone_core.decode(state.get("zone_tree_pending","") or
                              state.get("parameters",{}).get("zone_tree_next",""))
        leaves,bands=zone_core.layout(tree,x1-x0,y1-y0,float(state["parameters"]["frame_width"]))
        origin=Vector(state["source"]["origin"])
        ux=Vector(state["source"]["x_axis"]);uy=Vector(state["source"]["y_axis"])
        n=Vector(state["source"]["normal"])
        offset=float(state.get("parameters",{}).get("window_offset",0))
        cx=(x0+x1)*.5;cy=(y0+y1)*.5
        def project(a,b):
            local=origin+ux*(cx+a)+uy*(cy+b)+n*offset
            return view3d_utils.location_3d_to_region_2d(
                context.region,context.region_data,host.matrix_world@local)
        current_index=getattr(op,"_surface_zone_index",0)
        op._surface_zone_index=current_index%len(leaves) if leaves else 0
        for idx,(zid,(l,r,b,t)) in enumerate(leaves[:40]):
            poly=[project(l,b),project(r,b),project(r,t),project(l,t)]
            if any(pt is None for pt in poly):continue
            corners=[(float(pt.x),float(pt.y)) for pt in poly]
            op._surface_zone_screen_hits.append((str(zid),idx,corners))
            if idx==op._surface_zone_index:
                ui._line(corners+[corners[0]],ui.MINT,2.2)
                mx=sum(pt[0] for pt in corners)/4;my=sum(pt[1] for pt in corners)/4
                ui._soft_rect(mx-24,my-13,mx+24,my+13,(.02,.08,.09,.82),6)
                ui._text(mx-17,my-4,f"ZONE {idx+1}",9,ui.MINT)
        for sid,axis,center,width,rect,band in bands[:60]:
            l,r,b,t=band
            a,bp=(project((l+r)*.5,b),project((l+r)*.5,t)) if axis=="V" else (
                  project(l,(b+t)*.5),project(r,(b+t)*.5))
            if a is not None and bp is not None:
                ui._line([(float(a.x),float(a.y)),(float(bp.x),float(bp.y))],
                         ui.ORANGE if stage==6 else ui.MUTED,2.4 if stage==6 else 1.2)
    except (ValueError,TypeError,KeyError,IndexError,ZeroDivisionError):
        op._surface_zone_screen_hits=[]

def draw(op, context):
    from . import composer as ui
    if getattr(op, "_disabled", False) or context.area is None or context.area.type != "VIEW_3D":
        return
    try:
        host, session, stage = current(op, context)
    except (ValueError, TypeError, KeyError) as ex:
        op._surface_message = str(ex)
        return
    ui._soft_rect  # Use existing compositor renderer/palette and HIT registration.
    op._frame_theme = ui._dwos_ui_snapshot()
    op._hits = []
    op._layout_container = None
    op._layout_skips = []
    op._layout_issues = []
    op._floating_rect = None
    op._footer_rect = None
    theme = op._frame_theme
    w, h = 330, 424
    addon = context.preferences.addons.get("issmaker_modeling_assistant")
    prof = ui._ui_profile(addon.preferences if addon else None)
    scale = float(prof.safe_window)
    op._hud_pixel_scale = scale
    op._hud_font_scale = float(prof.font)
    vw, vh = context.region.width, context.region.height
    sidebar_width = 0
    if context.area.spaces.active.show_region_ui:
        sidebar_width=max((reg.width for reg in context.area.regions if reg.type=='UI' and reg.width>2),default=0)
    usable_right=vw-sidebar_width
    if usable_right < w*scale+26 or vh < h*scale+26:
        ui.gpu.state.blend_set("ALPHA")
        try:
            ui._soft_rect(12, vh-49, min(vw-12,245),vh-12,ui.PANEL_BG,6)
            ui._text(22,vh-35,"DWOS · увеличьте Viewport",10,ui.ORANGE)
            op._panel_rect=None
        finally:
            ui.gpu.state.blend_set("NONE")
        return
    if getattr(op, "_hud_custom", False):
        origin = op._hud_position(context,w*scale,h*scale)
    else:
        origin=(max(12,usable_right-w*scale-24),max(12,vh-h*scale-24))
    if origin is None:
        return
    x, y = origin
    x=min(x,max(12,usable_right-w*scale-12))
    op._panel_rect=(x,y,w*scale,h*scale)
    op._layout_container=(x,y,w,h)
    op._surface_page=getattr(op,"_surface_page","BUILD")
    top=y+h
    ui.gpu.state.blend_set("ALPHA")
    try:
        _draw_viewport_zone_guide(op,context,host,session,stage)
        with ui._composer_pixel_space((x,y),scale,prof.font):
            palette=ui._shell.surface(theme)
            ui._soft_rect(x+4,y-4,x+w+4,top-4,ui.SHADOW,11)
            ui._soft_rect(x,y,x+w,top,palette["panel"],10)
            from issmaker_modeling_assistant.appearance_gpu import draw_shared_background
            rx0,ry0,rx1,ry1=ui._mapped_xyxy(x,y,x+w,top)
            draw_shared_background(theme,(rx0,ry0,rx1-rx0,ry1-ry0))
            ui._soft_outline(x,y,x+w,top,palette["border"],10,1)
            ui._text(x+12,top-23,"DWOS / STUDIO · LP / HP",11,ui.MINT)
            op._hit("HUD_DRAG",x+8,top-30,215,28)
            op._button("HUD_PIN",x+230,top-34,28,20,"P" if op._hud_pinned else "F",op._hud_pinned)
            op._button("HUD_RESET",x+261,top-34,32,20,"RST")
            op._button("CLOSE",x+297,top-34,23,20,"X")
            all_sessions=sessions(context)
            idx=next((i for i,(ob,_state) in enumerate(all_sessions) if ob is host),-1)
            op._button("SURF_PREV",x+12,top-64,30,25,"<")
            ui._text_fitted(x+50,top-56,host.name,224,10,ui.TEXT)
            op._button("SURF_NEXT",x+288,top-64,30,25,">")
            ui._text(x+12,top-86,f"ОКНО {idx+1 if idx>=0 else 'NEW'} / {len(all_sessions)}   ·   ЭТАП {stage} / 8",9,ui.MUTED)
            for i,(key, label) in enumerate((("BUILD","BUILD"),("MULLIONS","ИМПОСТЫ"),("VIEW","VIEW"))):
                op._button("SURF_PAGE_"+key,x+12+i*103,top-118,98,25,label,op._surface_page==key)
            if session is not None and session.get("schema")!=surface.SCHEMA:
                ui._text_fitted(x+12,top-159,"! Старая схема — только диагностика/копия.",w-24,10,ui.ORANGE)
            elif op._surface_page=="BUILD":
                ui._text_fitted(x+12,top-150,surface.STEPS.get(stage+1,"Все этапы выполнены"),w-24,10,ui.MINT)
                pending=fields_for(stage)
                values=draft(host,context)
                yy=top-188
                if not pending:
                    ui._text_fitted(x+12,yy,"Параметры этого этапа не изменяются.",w-24,9,ui.MUTED)
                for name in pending:
                    label,lo,hi,step=FIELDS[name]
                    ui._text_fitted(x+12,yy+9,label,147,9,ui.TEXT)
                    op._button("SURF_MINUS:"+name,x+167,yy-3,30,24,"−")
                    number=(op._surface_buffer if getattr(op,"_surface_input",None)==name
                            else f"{values[name]:.3f}")
                    op._button("SURF_EDIT:"+name,x+202,yy-3,75,24,number,
                               getattr(op,"_surface_input",None)==name,ui.BLUE)
                    op._button("SURF_PLUS:"+name,x+282,yy-3,33,24,"+")
                    yy-=38
                if stage<8:
                    op._button("SURF_STEP",x+13,y+73,303,32,
                               f"ВЫПОЛНИТЬ ШАГ {stage+1}",False,ui.MINT)
                else:
                    ui._text(x+13,y+83,"HP / LP: все этапы опубликованы",10,ui.MINT)
                ui._text_fitted(x+14,y+48,"Шаг 1,4: Edit Mode · остальные: сессия стены",w-28,9,ui.MUTED)
                op._button("SURF_EDIT_MODE",x+14,y+15,140,25,"EDIT MODE")
                op._button("SURF_OBJECT_MODE",x+163,y+15,152,25,"OBJECT MODE")
            elif op._surface_page=="MULLIONS":
                ui._text(x+13,top-152,"РЕАЛЬНЫЕ ПРЕСЕТЫ ИМПОСТОВ",10,ui.MINT)
                if stage==6:
                    for i,(preset,name) in enumerate(zip(PRESETS,PRESET_NAMES)):
                        col=i%3;row=i//3
                        active=(session.get("DWOS_W55_mullion_preset")==preset if session else False)
                        op._button("SURF_PRESET:"+preset,x+13+col*103,top-189-row*35,97,28,name,active)
                    ids=zone_ids(session)
                    if ids:
                        zi=getattr(op,"_surface_zone_index",0)%len(ids)
                        op._surface_zone_index=zi
                        ui._text_fitted(x+13,top-274,f"ЗОНА {zi+1}/{len(ids)}  ·  {ids[zi][:14]}",w-28,10,ui.TEXT)
                        op._button("SURF_ZONE_PREV",x+13,top-305,42,27,"<")
                        op._button("SURF_ZONE_NEXT",x+61,top-305,43,27,">")
                        op._button("SURF_SPLIT_V",x+117,top-305,93,27,"+ V")
                        op._button("SURF_SPLIT_H",x+218,top-305,97,27,"+ H")
                    op._button("SURF_FILL:GLASS",x+13,top-342,96,25,"GLASS")
                    op._button("SURF_FILL:PANEL",x+117,top-342,96,25,"PANEL")
                    op._button("SURF_FILL:EMPTY",x+221,top-342,94,25,"EMPTY")
                    op._button("SURF_RESET_ZONES",x+13,y+18,148,27,"СБРОС ЗОН")
                    op._button("SURF_STEP",x+169,y+18,146,27,"ШАГ 7 · СОЗДАТЬ")
                elif stage < 6:
                    ui._text_fitted(x+13,top-206,"Импосты настраиваются после шага 6.",w-26,10,ui.MUTED)
                else:
                    ui._text_fitted(x+13,top-207,"Импосты уже опубликованы в HP/LP.",w-26,10,ui.MINT)
                    ui._text_fitted(x+13,top-238,"Повторное построение: требуется проверка.",w-26,9,ui.ORANGE)
            else:
                ui._text(x+13,top-152,"ВИДИМОСТЬ ГЕОМЕТРИИ",10,ui.MINT)
                if stage>=2:
                    for i,(mode,label) in enumerate((("HP","HP"),("LP","LP"),("BOTH","ОБА"),("NONE","СКРЫТЬ"))):
                        op._button("SURF_VIEW:"+mode,x+13+(i%2)*154,top-195-(i//2)*40,147,31,label)
                else:
                    ui._text_fitted(x+13,top-206,"Создайте LP — шаг 2.",w-28,10,ui.MUTED)
            message=str(getattr(op,"_surface_message","") or "")
            if message:
                message_y=y+50 if op._surface_page=="MULLIONS" and stage==6 else y+42
                ui._text_fitted(x+14,message_y,"! "+message,w-28,9,ui.ORANGE)
            ui._text_fitted(x+13,y+5,"ESC · закрыть   |   существующий DWOS Composer",w-26,8,ui.MUTED)
    finally:
        op._layout_container=None
        ui.gpu.state.blend_set("NONE")


def _activate(host, context):
    if context.active_object is not host:
        if context.mode != "OBJECT":
            raise ValueError("Для смены окна перейдите в Object Mode.")
        context.view_layer.objects.active=host
        host.select_set(True)


def _handle_action(op, context, key):
    host, session, stage=current(op,context)
    if key=="CLOSE":
        return op._finish(context)
    if key=="HUD_PIN":
        if not op._hud_pinned and op._panel_rect:
            op._hud_custom=True
            op._hud_custom_position=(op._panel_rect[0]/max(1,context.region.width),
                                     op._panel_rect[1]/max(1,context.region.height))
        op._hud_pinned=not op._hud_pinned
        op._save_hud_position(context)
        return {"RUNNING_MODAL"}
    if key=="HUD_RESET":
        op._hud_custom=False
        op._hud_pinned=False
        op._hud_drag=None
        op._save_hud_position(context)
        return {"RUNNING_MODAL"}
    if key.startswith("SURF_PAGE_"):
        op._surface_page=key.split("_",2)[2]
        return {"RUNNING_MODAL"}
    if key in ("SURF_PREV","SURF_NEXT"):
        items=sessions(context)
        if not items:
            return {"RUNNING_MODAL"}
        i=next((j for j,(obj,_state) in enumerate(items) if obj is host),0)
        target=items[(i+(-1 if key=="SURF_PREV" else 1))%len(items)][0]
        _activate(target,context)
        op._obj_name=target.name
        op._surface_zone_index=0
        op._surface_input=None
        return {"RUNNING_MODAL"}
    if key in ("SURF_ZONE_PREV","SURF_ZONE_NEXT"):
        ids=zone_ids(session)
        if ids:
            op._surface_zone_index=(getattr(op,"_surface_zone_index",0)+(-1 if key.endswith("PREV") else 1))%len(ids)
        return {"RUNNING_MODAL"}
    if key=="SURF_EDIT_MODE" or key=="SURF_OBJECT_MODE":
        _activate(host,context)
        mode="EDIT" if key=="SURF_EDIT_MODE" else "OBJECT"
        if host.mode!=mode:
            bpy.ops.object.mode_set(mode=mode)
        return {"RUNNING_MODAL"}
    if session is not None and session.get("schema")!=surface.SCHEMA:
        raise ValueError("Старая DWOS Surface-схема: восстановите копию.")
    if key.startswith("SURF_EDIT:"):
        name=key.split(":",1)[1]
        if name not in fields_for(stage):raise ValueError("Параметр уже опубликован.")
        op._surface_input=name
        op._surface_buffer=f"{draft(host,context)[name]:.3f}"
        return {"RUNNING_MODAL"}
    if key.startswith("SURF_MINUS:") or key.startswith("SURF_PLUS:"):
        direction=1 if key.startswith("SURF_PLUS:") else -1
        name=key.split(":",1)[1]
        if name not in fields_for(stage):raise ValueError("Параметр уже опубликован.")
        set_draft(host,context,name,round(draft(host,context)[name]+direction*FIELDS[name][3],6))
        return {"RUNNING_MODAL"}
    _activate(host,context)
    if key=="SURF_STEP":
        if stage>=8:return {"RUNNING_MODAL"}
        if stage in (0,3) and context.mode!="EDIT_MESH":
            raise ValueError(f"Шаг {stage+1}: необходимо перейти в Edit Mode.")
        sync_draft(host,context)
        result=bpy.ops.dwos.surface_step("EXEC_DEFAULT",number=stage+1)
        if "FINISHED" not in result:raise ValueError("Оператор DWOS отклонил шаг — откройте диагностику.")
        op._surface_message=f"Шаг {stage+1} завершён."
        return {"RUNNING_MODAL"}
    if key.startswith("SURF_PRESET:"):
        if stage!=6:raise ValueError("Пресеты доступны только на этапе 6.")
        sync_draft(host,context)
        result=bpy.ops.dwos.surface_mullion_preset("EXEC_DEFAULT",
                preset=key.split(":",1)[1],
                thickness=draft(host,context)["zone_split_thickness"])
        if "FINISHED" not in result:raise ValueError("Пресет импостов не принят.")
        op._surface_message="Пресет импостов сохранён для этапа 7."
        return {"RUNNING_MODAL"}
    if key.startswith("SURF_FILL:"):
        if stage!=6:raise ValueError("Заполнение зон редактируется до этапа 7.")
        ids=zone_ids(session)
        if not ids:raise ValueError("Нет зон для заполнения.")
        fill=key.split(":",1)[1]
        result=bpy.ops.dwos.surface_zone_fill("EXEC_DEFAULT",
                 zone_id=ids[getattr(op,"_surface_zone_index",0)%len(ids)],fill=fill)
        if "FINISHED" not in result:raise ValueError("Не удалось изменить заполнение.")
        op._surface_message=f"Зона: {fill}; опубликовано будет на шаге 7."
        return {"RUNNING_MODAL"}
    if key in ("SURF_SPLIT_V","SURF_SPLIT_H"):
        if stage!=6:raise ValueError("Зоны редактируются до этапа 7.")
        ids=zone_ids(session)
        if not ids:raise ValueError("В сессии нет редактируемых зон.")
        result=bpy.ops.dwos.surface_zone_split("EXEC_DEFAULT",
                 zone_id=ids[getattr(op,"_surface_zone_index",0)%len(ids)],
                 axis="V" if key.endswith("_V") else "H")
        if "FINISHED" not in result:raise ValueError("Невозможно разделить выбранную зону.")
        op._surface_message="Зона обновлена. Импосты будут созданы на шаге 7."
        return {"RUNNING_MODAL"}
    if key=="SURF_RESET_ZONES":
        if stage!=6:raise ValueError("Сброс доступен только до этапа 7.")
        result=bpy.ops.dwos.surface_zone_reset("EXEC_DEFAULT")
        if "FINISHED" not in result:raise ValueError("Сброс зон отклонён.")
        return {"RUNNING_MODAL"}
    if key.startswith("SURF_VIEW:"):
        if stage<2:raise ValueError("LP/HP ещё не построены.")
        result=bpy.ops.dwos.surface_view("EXEC_DEFAULT",mode=key.split(":",1)[1])
        if "FINISHED" not in result:raise ValueError("Не удалось переключить LP/HP.")
        return {"RUNNING_MODAL"}
    return {"RUNNING_MODAL"}


def modal(op, context, event):
    if getattr(op,"_disabled",False):
        return op._finish(context)
    if context.area is None or context.area.type!="VIEW_3D" or context.area.as_pointer()!=getattr(op,"_area_pointer",None):
        return op._finish(context)
    try:
        current(op,context)
    except (ValueError, TypeError, KeyError):
        return op._finish(context)
    if event.type=="ESC" and event.value=="PRESS":
        if getattr(op,"_surface_input",None):
            op._surface_input=None
            op._surface_buffer=""
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if getattr(op,"_hud_drag",None) is not None:
            op._hud_drag=None
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        return op._finish(context)
    if getattr(op,"_surface_input",None):
        if event.value=="PRESS" and event.type in ("RET","NUMPAD_ENTER"):
            name=op._surface_input
            host,session,stage=current(op,context)
            try:
                if name not in fields_for(stage):
                    raise ValueError("Этап изменён, ввод отменён.")
                set_draft(host,context,name,float(op._surface_buffer.replace(",",".")))
                op._surface_message=f"{FIELDS[name][0]}: {op._surface_buffer} м"
            finally:
                op._surface_input=None
                op._surface_buffer=""
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if event.value=="PRESS" and event.type=="BACK_SPACE":
            op._surface_buffer=op._surface_buffer[:-1]
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if event.value=="PRESS" and event.unicode in "0123456789.,-" and event.unicode and len(op._surface_buffer)<24:
            op._surface_buffer+=event.unicode
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if event.type!="LEFTMOUSE":return {"RUNNING_MODAL"}
        # A click outside the field cancels an uncommitted edit.
        op._surface_input=None
        op._surface_buffer=""
    if op._hud_drag is not None:
        if event.type=="MOUSEMOVE":
            mx,my=event.mouse_region_x,event.mouse_region_y
            sx,sy,ox,oy=op._hud_drag
            from ....core.hud_floating_contract import clamp_to_viewport
            rect=op._panel_rect
            if rect:
                at=clamp_to_viewport((context.region.width,context.region.height),
                                     (rect[2],rect[3]),(ox+mx-sx,oy+my-sy),12)
                if at is not None:
                    op._hud_custom=True
                    op._hud_custom_position=(at[0]/context.region.width,at[1]/context.region.height)
                    context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if event.type=="LEFTMOUSE" and event.value=="RELEASE":
            op._hud_drag=None
            op._save_hud_position(context)
            return {"RUNNING_MODAL"}
        return {"RUNNING_MODAL"}
    if event.type=="MOUSEMOVE":
        key,_payload=op._find_hit(event.mouse_region_x,event.mouse_region_y)
        if key!=getattr(op,"_hover_ui_key",""):
            op._hover_ui_key=key or ""
            context.area.tag_redraw()
        return {"PASS_THROUGH"} if not op._inside_ui(event.mouse_region_x,event.mouse_region_y) else {"RUNNING_MODAL"}
    if event.type=="LEFTMOUSE" and event.value=="PRESS":
        key,_payload=op._find_hit(event.mouse_region_x,event.mouse_region_y)
        if key=="HUD_DRAG":
            if op._hud_pinned:return {"RUNNING_MODAL"}
            op._hud_drag=(event.mouse_region_x,event.mouse_region_y,op._panel_rect[0],op._panel_rect[1])
            return {"RUNNING_MODAL"}
        if key:
            try:
                result=_handle_action(op,context,key)
                if key not in ("CLOSE",):
                    op._surface_message=getattr(op,"_surface_message","")
            except (ValueError,KeyError,TypeError,RuntimeError,AttributeError) as exc:
                op._surface_message=str(exc)[:150]
                op.report({"WARNING"},op._surface_message)
                result={"RUNNING_MODAL"}
            if context.area:context.area.tag_redraw()
            return result
        if op._inside_ui(event.mouse_region_x,event.mouse_region_y):
            return {"RUNNING_MODAL"}
    if event.type=="LEFTMOUSE" and event.value=="RELEASE" and op._inside_ui(event.mouse_region_x,event.mouse_region_y):
        return {"RUNNING_MODAL"}
    if (event.type=="LEFTMOUSE" and event.value=="PRESS"
            and getattr(op,"_surface_page","BUILD")=="MULLIONS"):
        for zid,idx,poly in getattr(op,"_surface_zone_screen_hits",[]):
            if _point_in_poly(event.mouse_region_x,event.mouse_region_y,poly):
                op._surface_zone_index=idx
                context.area.tag_redraw()
                return {"RUNNING_MODAL"}
    return {"PASS_THROUGH"}
