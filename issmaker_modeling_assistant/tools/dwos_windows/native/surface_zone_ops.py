"""Wave42 experimental dynamic LP-FIRST zoning actions.

Only pre-publish editing: stage>=7 refuses modifications because the system
must not lie about already published mesh state. All geometry is tested by
zone_core before settings are committed.
"""
from __future__ import annotations
import bpy
from bpy.props import EnumProperty,StringProperty,FloatProperty
from . import zone_core, surface_graph,surface_step_core

class DWOS_OT_surface_zone_split(bpy.types.Operator):
    bl_idname="dwos.surface_zone_split"
    bl_label="DWOS NEXT · разделить зону"
    bl_description="Добавить локальный разделитель в LP-FIRST план; геометрия будет построена на этапе импостов"
    bl_options={"REGISTER","UNDO"}

    zone_id: StringProperty(name="ID зоны",default="root")
    axis: EnumProperty(name="Ориентация",items=[("V","Вертикально",""),("H","Горизонтально","")],default="V")

    def execute(self,context):
        from . import surface_steps
        props=context.scene.dwos_surface_lab
        try:
            host,session=surface_steps._get_session_wall(context)
            step=int(session["step"])
            if not 2<=step<7:
                raise ValueError("Изменяйте зоны после LP и до публикации импостов (этап 7).")
            axes=surface_step_core.rectangle_axes(session["source"]["points"]) or surface_graph._orthogonal_bounds(session["source"]["points"])
            if axes is None:
                raise ValueError("Контур не является корректным ортогональным полигоном.")
            x0,x1,y0,y1=axes
            width=x1-x0;height=y1-y0
            fw=float(session["parameters"]["frame_width"])
            tree=zone_core.decode(session.get("zone_tree_pending",""))
            parent=zone_core.find(tree,self.zone_id)
            inherited_fill=parent.get("fill","GLASS") if parent else "GLASS"
            inherited_mode=parent.get("panel_mode","OPT") if parent else "OPT"
            # V/H mean visually vertical/horizontal on the actual facade,
            # not arbitrary source face edge-loop order.  World-up projected
            # to the facade picks which author-space coordinate is vertical.
            ux=session["source"]["x_axis"]
            uy=session["source"]["y_axis"]
            up_x=abs(float(ux[2]));up_y=abs(float(uy[2]))
            effective_axis=self.axis
            if max(up_x,up_y)>.5 and abs(up_x-up_y)>.25:
                local_y_is_vertical=up_y>up_x
                effective_axis=(self.axis if local_y_is_vertical else
                                ("H" if self.axis=="V" else "V"))
            tree,_=zone_core.add(tree,self.zone_id,effective_axis,
                                 float(props.zone_split_thickness),width,height,fw)
            new_parent=zone_core.find(tree,self.zone_id)
            # When a panel is divided, both new leaves remain panels. Never
            # silently turn user-authored opaque material back into glass.
            if inherited_fill in ("PANEL","EMPTY"):
                for child in new_parent["children"]:
                    child["fill"]=inherited_fill
                    if inherited_fill=="PANEL":
                        child["panel_mode"]=inherited_mode
                    else:
                        child.pop("panel_mode",None)
            new_parent.pop("fill",None);new_parent.pop("panel_mode",None)
            zone_core.layout(tree,width,height,fw)
            from .surface_zone_next import zone_layout
            inset=surface_step_core.safe_inset(session["source"]["points"],fw)
            zone_layout(inset["outer"],inset["inner"],tree)
            surface_graph.make_graph(inset["outer"],inset["inner"],zone_tree_next=tree)
            session["zone_tree_pending"]=zone_core.encode(tree)
            surface_steps._save_session(host,session)
            count=len(zone_core.layout(tree,width,height,fw)[0])
            self.report({"INFO"},f"План зон обновлён: {count}. Геометрия появится на этапе 7.")
            return {"FINISHED"}
        except (ValueError,KeyError,TypeError) as exc:
            self.report({"WARNING"},str(exc))
            return {"CANCELLED"}

class DWOS_OT_surface_zone_reset(bpy.types.Operator):
    bl_idname="dwos.surface_zone_reset"
    bl_label="DWOS NEXT · сбросить локальные зоны"
    bl_options={"REGISTER","UNDO"}

    def execute(self,context):
        from . import surface_steps
        props=context.scene.dwos_surface_lab
        try:
            host,session=surface_steps._get_session_wall(context)
            if not 2<=int(session["step"])<7:
                raise ValueError("Сброс зон разрешён после LP, до публикации этапа 7.")
            session.pop("zone_tree_pending",None)
            surface_steps._save_session(host,session)
            self.report({"INFO"},"План зон сброшен. При построении будут стандартные импосты.")
            return {"FINISHED"}
        except (ValueError,KeyError,TypeError) as exc:
            self.report({"WARNING"},str(exc))
            return {"CANCELLED"}

class DWOS_OT_surface_zone_fill(bpy.types.Operator):
    bl_idname="dwos.surface_zone_fill"
    bl_label="DWOS NEXT · заполнение зоны"
    bl_description="Установить стекло или одностороннюю панель в выбранной зоне"
    bl_options={"REGISTER","UNDO"}

    zone_id: StringProperty(name="ID зоны",default="root")
    fill: EnumProperty(name="Заполнение",items=[
        ("GLASS","Стекло","Прозрачное заполнение"),
        ("PANEL","Панель OPT","Односторонняя непрозрачная панель"),
        ("EMPTY","Пусто","Настоящий открытый контур, без стеклянной заглушки")],default="GLASS")
    panel_mode: EnumProperty(name="Панель",items=[
        ("OPT","OPT","Одна поверхность"),
        ("FULL","FULL","Две поверхности, пока не реализовано")],default="OPT")

    def execute(self,context):
        from . import surface_steps
        try:
            host,session=surface_steps._get_session_wall(context)
            if not 2<=int(session['step'])<7:
                raise ValueError("Зоны доступны только до публикации импостов (этап 7).")
            if self.fill=="PANEL" and self.panel_mode!="OPT":
                raise ValueError("FULL-панель ещё не реализована: нужна двухсторонняя топология.")
            axes=surface_step_core.rectangle_axes(session["source"]["points"]) or surface_graph._orthogonal_bounds(session["source"]["points"])
            if axes is None:raise ValueError("Контур не поддерживает ортогональное разбиение.")
            x0,x1,y0,y1=axes
            tree=zone_core.decode(session.get("zone_tree_pending",""))
            width=x1-x0;height=y1-y0
            fw=float(session["parameters"]["frame_width"])
            leaves,_=zone_core.layout(tree,width,height,fw)
            if self.zone_id not in {zid for zid,_ in leaves}:
                raise ValueError("Эта зона больше не является конечной. Выберите актуальную зону.")
            node=zone_core.find(tree,self.zone_id)
            if self.fill=="PANEL":
                node["fill"]="PANEL";node["panel_mode"]="OPT"
            elif self.fill=="EMPTY":
                node["fill"]="EMPTY";node.pop("panel_mode",None)
            else:
                node.pop("fill",None);node.pop("panel_mode",None)
            # Strong preflight: full facade partition + geometry budget before saving.
            from .surface_zone_next import zone_layout
            from . import surface_step_core as core
            source=session["source"]["points"]
            shape=core.safe_inset(source,fw)
            zone_layout(shape["outer"],shape["inner"],tree)
            surface_graph.make_graph(shape["outer"],shape["inner"],zone_tree_next=tree)
            session["zone_tree_pending"]=zone_core.encode(tree)
            surface_steps._save_session(host,session)
            self.report({"INFO"},f"Зона {self.zone_id[:8]}: {self.fill} — публикация на этапе 7.")
            return {"FINISHED"}
        except (ValueError,TypeError,KeyError) as exc:
            self.report({"WARNING"},str(exc))
            return {"CANCELLED"}

class DWOS_OT_surface_zone_resize_split(bpy.types.Operator):
    """Change only one authored mullion band, without rebuilding the wall."""
    bl_idname="dwos.surface_zone_resize_split"
    bl_label="DWOS NEXT · локальная толщина импоста"
    bl_description="Увеличить/уменьшить выбранный импост; применится при построении этапа 7"
    bl_options={"REGISTER","UNDO"}

    split_id: StringProperty(name="ID импоста",default="root")
    delta: FloatProperty(name="Изменение, м",default=.005,min=-1.,max=1.,precision=4,subtype="DISTANCE")

    def execute(self,context):
        from . import surface_steps
        from .surface_zone_next import zone_layout
        try:
            host,session=surface_steps._get_session_wall(context)
            if not 2<=int(session["step"])<7:
                raise ValueError("Только после LP и до этапа 7; готовые HP/LP не меняются.")
            axes=surface_step_core.rectangle_axes(session["source"]["points"]) or surface_graph._orthogonal_bounds(session["source"]["points"])
            if axes is None:
                raise ValueError("Для этого контура локальная толщина недоступна.")
            x0,x1,y0,y1=axes;fw=float(session["parameters"]["frame_width"])
            tree=zone_core.decode(session.get("zone_tree_pending",""))
            node=zone_core.find(tree,self.split_id)
            if node is None or node.get("axis") not in ("V","H"):
                raise ValueError("Выбранный импост не найден.")
            old=float(node["thickness"]);new=round(old+float(self.delta),7)
            if new<=.001:
                raise ValueError("Толщина импоста должна быть более 1 мм.")
            updated=zone_core.update(tree,self.split_id,x1-x0,y1-y0,fw,thickness=new)
            shape=surface_step_core.safe_inset(session["source"]["points"],fw)
            zone_layout(shape["outer"],shape["inner"],updated)
            surface_graph.make_graph(shape["outer"],shape["inner"],zone_tree_next=updated)
            session["zone_tree_pending"]=zone_core.encode(updated)
            surface_steps._save_session(host,session)
            self.report({"INFO"},f"Импост {self.split_id[:8]}: {old:.3f} → {new:.3f} м")
            return {"FINISHED"}
        except (ValueError,TypeError,KeyError) as exc:
            self.report({"WARNING"},str(exc))
            return {"CANCELLED"}


CLASSES=(DWOS_OT_surface_zone_split,DWOS_OT_surface_zone_reset,DWOS_OT_surface_zone_fill,DWOS_OT_surface_zone_resize_split)
