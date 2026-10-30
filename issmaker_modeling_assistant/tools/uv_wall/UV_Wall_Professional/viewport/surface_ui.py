"""Surface-specific visual feedback for the viewport controller.

No source/definition mutation is allowed in this module.
"""
from mathutils import Vector
from ..ui.numeric_input import format_value
from ..core.surface_points import original_height_for_point
import math
from .ghost_geometry import place_info_popup


def draw_surface_annotations(session, context, path, definition, analysis, region, x, y,
                             lens, draw_circle, draw_text):
    """Visual hints and hit areas use the same semantic indices as the source."""
    # Height-point tools are anchored to authored source vertices; no fake
    # scene objects or pixel sliders. All edits are tentative until commit.
    if definition.generator_type=="SURFACE" and lens=="BUILD":
        z_values=[]; point_count=0; screen_bins=set()
        for chain in analysis.get("simple_chains",[]):
            if not chain["closed"]:continue
            indices=chain["indices"][:-1]; world_points=chain["points"][:-1]
            for index, original in zip(indices, world_points):
                display_z=session._engine.surface_overrides.get(index, original.z)
                at=Vector((original.x,original.y,display_z))
                z_values.append(display_z);point_count+=1
                point=session._screen(context,at)
                if point is None or not (0<=point[0]<region.width and 0<=point[1]<region.height): continue
                # Density-aware screen-space bins: no hundreds of overdrawn
                # controls for coincident pixel positions on large paths.
                screen_bin=(int(point[0]//12),int(point[1]//12))
                if screen_bin in screen_bins and not (session._drag_kind=="SURFACE_Z" and session._drag_index==index) and index!=getattr(session,"_surface_point_selected",-1):
                    continue
                screen_bins.add(screen_bin)
                is_active=session._drag_kind=="SURFACE_Z" and session._drag_index==index
                hovered=session._hover==f"SURFACE_Z:{index}"
                selected=index==getattr(session,'_surface_point_selected',-1)
                col=(1.0,0.78,0.38,1) if is_active else ((0.42,1.0,0.84,1) if hovered else (0.19,0.80,0.71,0.95))
                draw_circle(point,7 if (is_active or hovered) else 5,col,1.5,True)
                if is_active or hovered or selected:
                    # Up/down ruler sign: point itself remains the same drag
                    # handle, arrow never adds a second invisible hit region.
                    sx,sy=point
                    session._ui_stroke((sx+12,sy-10),(sx+12,sy+12),col,1.6)
                    session._ui_stroke((sx+8,sy+8),(sx+12,sy+12),col,1.6)
                    session._ui_stroke((sx+16,sy+8),(sx+12,sy+12),col,1.6)
                    session._ui_stroke((sx+8,sy-6),(sx+12,sy-10),col,1.6)
                    session._ui_stroke((sx+16,sy-6),(sx+12,sy-10),col,1.6)
                show_labels=bool(getattr(context.window_manager,"uvwall_show_labels",True))
                if is_active or hovered or selected or (show_labels and point_count<=8):
                    draw_text(point[0]+10,point[1]+6,f"Z {format_value(display_z)}",10,col)
                if is_active or hovered or selected:
                    original_z=original_height_for_point(path,definition,index)
                    if original_z is None:
                        draw_text(point[0]+10,point[1]-13,"Исходная Z ещё не сохранена",9,(.80,.83,.84,1))
                    else:
                        delta=float(display_z)-original_z
                        draw_text(point[0]+10,point[1]-13,
                                  f"Исходная {format_value(original_z)} · Δ {format_value(delta)} м",9,(.75,.92,.88,1))
                        popup_w=min(182.,max(108.,region.width*.30))
                        blockers=[getattr(session,'_hud_rect',None),
                                  getattr(session,'_workspace_rect',None)]
                        blockers.extend(getattr(session,'_point_card_rects',()))
                        popup=place_info_popup(point,(popup_w,68.),
                            (region.width,region.height),blockers)
                        if popup is not None:
                            bx,by=popup[0],popup[1]
                            session._point_card_rects.append(popup)
                            session._button(f"FOCUS:S:{index}",bx+3,by+35,popup_w-6,26,"◎ Показать точку",False,True)
                            if not math.isclose(float(display_z),original_z,abs_tol=1.e-7):
                                session._button(f"S:RESET:{index}",bx+3,by+4,popup_w-6,26,"↶ Исходная Z",False,True)
                session._register_handle(f"SURFACE_Z:{index}",point,16.0,anchor=at,vertex_index=index)
        if z_values and bool(getattr(context.window_manager,"uvwall_show_labels",True)):
            draw_text(x+14,y-158,f"Поверхность · {format_value(analysis.get('xy_area',0))} · Z {format_value(min(z_values))} … {format_value(max(z_values))}",10,(0.55,0.83,0.77,1))

    # Diagnostics belong to the geometry: branch vertices, isolated points
    # and XY contour intersections are marked directly at their position.
    if lens=="INSPECT":
        for index in analysis.get("branches",[]):
            if index>=len(path.data.vertices):continue
            point=session._screen(context,path.matrix_world@path.data.vertices[index].co)
            if point:
                draw_circle(point,10,(1.0,0.38,0.26,0.96),2)
                draw_text(point[0]+13,point[1]+8,"BRANCH",10,(1.0,0.55,0.39,1))
        for index in analysis.get("isolated",[]):
            if index>=len(path.data.vertices):continue
            point=session._screen(context,path.matrix_world@path.data.vertices[index].co)
            if point:
                draw_circle(point,8,(0.97,0.71,0.30,0.9),2)
                draw_text(point[0]+12,point[1]+7,"ISOLATED",9,(1.0,0.77,0.45,1))
        for world in analysis.get("intersections",[])[:40]:
            point=session._screen(context,world)
            if point:
                draw_circle(point,12,(1.0,0.26,0.24,1.0),2.5)
                draw_text(point[0]+14,point[1]+9,"SELF INTERSECTION",10,(1.0,0.43,0.35,1))

