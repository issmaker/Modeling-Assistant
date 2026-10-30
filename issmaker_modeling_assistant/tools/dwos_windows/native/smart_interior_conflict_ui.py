"""ISSMAKER W58.8 — viewport risk zones + click-to-resolve interaction.

No source mesh is edited by scanning, drawing, picking or preview. Scan only
live scene interiors; canonical meshes are transformed into world coordinates.
Clicking a risk marker opens a native dark Blender popup whose allowed modes
depend on both windows' actual *upper AND lower* Z elevations.
"""
from __future__ import annotations
import math
import json
import bpy
import gpu
import blf
from mathutils import Vector
from bpy.props import IntProperty, EnumProperty, StringProperty
from gpu_extras.batch import batch_for_shader
from bpy_extras.view3d_utils import location_3d_to_region_2d
from . import smart_interior_scene as scene_adapt
from .smart_interior_3d_modes import classify_pair,preview_for_interior_objects
from .spatial_safety_core import bounds,aabb_overlap

_ISSUES={}
_HANDLE=None
_WORLD_HANDLE=None
_SESSION={}
_ACTIVE=False
_LAST_ERROR=""

def _interiors(scene):
    output=[]
    for o in scene.objects:
        if (o.type!="MESH" or o.get("DWOS_role")!="INTERIOR"
            or o.parent is None or
            not bool(getattr(getattr(o.parent,"dwos_window",None),"is_window",False))
            or o.get("ISSMAKER_W58_8_PREVIEW_ONLY")
            or o.get("DWOS_W58_9_active_group")):continue
        try:
            canonical=scene_adapt._mesh_data(o)
            world=scene_adapt._world(canonical,o.matrix_world)
            if world["faces"]:
                output.append((o.name,o,world,bounds(world["verts"])))
        except (ValueError,TypeError,KeyError):
            continue
    return sorted(output,key=lambda row:row[0])

def scan(scene):
    """Exact pair classification, with X-sweep + 3D clearance broad phase."""
    if scene is None:return []
    gap=float(getattr(scene.dwos,"smart_interior_gap",.03))
    rows=_interiors(scene)
    issues=[]
    sweep=sorted(rows,key=lambda row:row[3][0][0])
    for i,(na,oa,a,ba) in enumerate(sweep):
        for nb,ob,b,bb in sweep[i+1:]:
            if bb[0][0]>ba[1][0]+gap+1e-6:break
            if not all(ba[0][k]-gap<=bb[1][k] and bb[0][k]<=ba[1][k]+gap for k in range(3)):
                continue
            decision=classify_pair(a,b,clearance=gap)
            status=decision["status"]
            if status in ("NO_CONTACT_Z","NO_CONTACT_XY"):continue
            if status.startswith("UNSUPPORTED"):
                # AABB overlap alone is not evidence of a curved/corner
                # collision. Only publish these risks after the strict
                # triangle oracle proves the two shells intersect.
                from .spatial_safety_core import mesh_intersections
                if not mesh_intersections(
                    a["verts"],a["faces"],b["verts"],b["faces"],
                    max_hits=1):
                    continue
            center=[]
            for dim in range(3):
                low=max(ba[0][dim],bb[0][dim])
                high=min(ba[1][dim],bb[1][dim])
                center.append((low+high)*.5)
            point=(float(center[0]),float(center[1]),float(center[2]))
            issue={"id":len(issues),"a":na,"b":nb,"point":point,
                "status":status,"z":decision.get("z",{}),
                "policy":decision.get("heights",{}),
                "options":decision.get("mode_options",["GAP","AUTO"]),
                "gap_m":gap,
                "danger_bounds": [[max(ba[0][k],bb[0][k])-0.03 for k in range(3)],
                                  [min(ba[1][k],bb[1][k])+0.03 for k in range(3)]]}
            issues.append(issue)
    issues.sort(key=lambda x:(x["a"],x["b"]))
    for i,entry in enumerate(issues):entry["id"]=i
    _ISSUES[scene.name]=issues
    scene["W58_8_smart_interior_detected_count"]=len(issues)
    scene["W58_8_smart_interior_conflicts"]=json.dumps(issues,ensure_ascii=False)
    return issues

def _color(issue):
    return (1.,.26,.28,.92) if issue["status"]=="OVERLAPPING_3D" else (1.,.71,.24,.90)

def _lines(shader,coords,color):
    if not coords:return
    shader.bind();shader.uniform_float("color",color)
    batch=batch_for_shader(shader,"LINES",{"pos":coords})
    batch.draw(shader)

def _draw_world():
    ctx=bpy.context
    if not _ACTIVE or ctx.area is None or ctx.area.type!="VIEW_3D":
        return
    issues=_ISSUES.get(ctx.scene.name,())
    if not issues:return
    shader=gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    gpu.state.depth_test_set("LESS_EQUAL")
    try:
        for issue in issues:
            a,b=issue["danger_bounds"]
            if any(b[k]<a[k] for k in range(3)):
                p=issue["point"]
                a=[p[k]-.025 for k in range(3)]
                b=[p[k]+.025 for k in range(3)]
            corners=[(x,y,z) for z in (a[2],b[2])
                       for y in (a[1],b[1]) for x in (a[0],b[0])]
            edges=[(0,1),(1,3),(3,2),(2,0),
                   (4,5),(5,7),(7,6),(6,4),
                   (0,4),(1,5),(2,6),(3,7)]
            _lines(shader,[v for i,j in edges
                for v in (corners[i],corners[j])],_color(issue))
    finally:
        gpu.state.depth_test_set("NONE")
        gpu.state.blend_set("NONE")

def projected_marker_layout(context,issues):
    """Shared pixel layout for both GPU drawing and exact click hit tests.

    An issue is identified by its 3D danger point but close 3D projections
    must fan out to distinct 2D markers; otherwise a user cannot select the
    second conflict when three windows overlap at the same corner.
    """
    region=getattr(context,"region",None)
    rv3d=getattr(context,"region_data",None)
    if region is None or rv3d is None:return []
    placed=[]
    for issue in issues:
        pt=location_3d_to_region_2d(
            region,rv3d,Vector(issue["point"]))
        if pt is None:continue
        anchor=(float(pt.x),float(pt.y))
        if not (-25<=anchor[0]<=region.width+25 and
                -25<=anchor[1]<=region.height+25):
            continue
        target=anchor
        # Proximity of both markers AND their descriptive text matters.
        shifts=[0.,48.,-48.,96.,-96.,144.,-144.,
                192.,-192.,240.,-240.]
        for dy in shifts:
            candidate=(anchor[0],max(26.,min(float(region.height)-26.,
                                           anchor[1]+dy)))
            if all((math.hypot(candidate[0]-old[1][0],
                               candidate[1]-old[1][1])>=41. and
                    (abs(candidate[0]-old[1][0])>=230. or
                     abs(candidate[1]-old[1][1])>=27.))
                   for old in placed):
                target=candidate
                break
        placed.append((issue,target,anchor))
    return placed

def _draw():
    ctx=bpy.context
    if not _ACTIVE or ctx.area is None or ctx.area.type!="VIEW_3D" or ctx.region is None:
        return
    issues=_ISSUES.get(ctx.scene.name,())
    if not issues:return
    shader=gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    gpu.state.depth_test_set("NONE")
    try:
        for issue,center,anchor in projected_marker_layout(ctx,issues):
            col=_color(issue)
            if math.hypot(center[0]-anchor[0],center[1]-anchor[1])>2.:
                _lines(shader,[(anchor[0],anchor[1],0.),
                    (center[0],center[1],0.)],
                    (col[0],col[1],col[2],.47))
            circle=[]
            for k in range(28):
                t0=math.tau*k/28.;t1=math.tau*(k+1)/28.
                circle.extend([(center[0]+16*math.cos(t0),center[1]+16*math.sin(t0),0.),
                               (center[0]+16*math.cos(t1),center[1]+16*math.sin(t1),0.)])
            _lines(shader,circle,col)
            # Marker remains click-sized on 1K, 2K and 4K viewport.
            font=0;blf.size(font,14)
            blf.color(font,1.,.93,.90,1.)
            blf.position(font,center[0]-5,center[1]-6,0)
            blf.draw(font,"!")
            blf.size(font,12);blf.position(font,center[0]+22,center[1]+4,0)
            blf.draw(font,f"INTERIOR {issue['id']+1:02d}  •  "+
                ("COLLISION" if issue["status"]=="OVERLAPPING_3D" else "CLEARANCE"))
    finally:
        gpu.state.depth_test_set("NONE")
        gpu.state.blend_set("NONE")

def redraw():
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type=="VIEW_3D":area.tag_redraw()

def ensure_overlay():
    global _HANDLE,_WORLD_HANDLE,_ACTIVE
    if _HANDLE is None:
        _HANDLE=bpy.types.SpaceView3D.draw_handler_add(_draw,(),"WINDOW","POST_PIXEL")
    if _WORLD_HANDLE is None:
        _WORLD_HANDLE=bpy.types.SpaceView3D.draw_handler_add(_draw_world,(),"WINDOW","POST_VIEW")
    _ACTIVE=True
    redraw()

def stop_overlay():
    global _HANDLE,_WORLD_HANDLE,_ACTIVE
    _ACTIVE=False
    for handle in (_HANDLE,_WORLD_HANDLE):
        if handle is None:continue
        try:bpy.types.SpaceView3D.draw_handler_remove(handle,"WINDOW")
        except (ReferenceError,RuntimeError,ValueError):pass
    _HANDLE=None;_WORLD_HANDLE=None
    redraw()

def issue_at_pixel(context,x,y,radius=23.):
    issues=_ISSUES.get(context.scene.name,())
    best=None
    for issue,target,_anchor in projected_marker_layout(context,issues):
        distance=math.hypot(float(x)-target[0],float(y)-target[1])
        if distance<=radius and (best is None or distance<best[0]):
            best=(distance,issue["id"])
    return best[1] if best else None

class DWOS_OT_scan_interior_conflicts(bpy.types.Operator):
    bl_idname="dwos.scan_interior_conflicts"
    bl_label="Analyse Smart Interior Conflicts"
    bl_description="Find 3D intersections and visualize clickable danger markers"
    bl_options={"REGISTER"}
    def execute(self,context):
        issues=scan(context.scene)
        if issues:ensure_overlay()
        else:stop_overlay()
        self.report({"WARNING" if issues else "INFO"},
             f"Smart Interior: {len(issues)} contact risks found")
        return {"FINISHED"}

class DWOS_OT_pick_interior_conflict(bpy.types.Operator):
    bl_idname="dwos.pick_interior_conflict"
    bl_label="Choose Smart Interior Danger Zone"
    bl_description="Click a red/orange viewport marker; Esc exits without intercepting when inactive"

    def invoke(self,context,event):
        if context.area is None or context.area.type!="VIEW_3D":return {"CANCELLED"}
        if not _ISSUES.get(context.scene.name):
            scan(context.scene)
        if not _ISSUES.get(context.scene.name):
            self.report({"INFO"},"No detected Smart Interior contact risks")
            return {"CANCELLED"}
        ensure_overlay()
        self._area=context.area
        self._scene=context.scene
        self._token=object()
        _SESSION[self._scene.name]=self._token
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self,context,event):
        if _SESSION.get(self._scene.name) is not self._token:
            return {"FINISHED"}
        if context.area is not self._area or context.scene is not self._scene:
            return {"PASS_THROUGH"}
        if event.type=="ESC" and event.value=="PRESS":
            _SESSION.pop(self._scene.name,None)
            stop_overlay()
            return {"FINISHED"}
        if event.type=="LEFTMOUSE" and event.value=="PRESS":
            index=issue_at_pixel(context,event.mouse_region_x,event.mouse_region_y)
            if index is not None:
                _SESSION.pop(self._scene.name,None)
                bpy.ops.dwos.resolve_interior_contact("INVOKE_DEFAULT",index=index)
                return {"FINISHED"}
        return {"PASS_THROUGH"}

class DWOS_OT_resolve_interior_contact(bpy.types.Operator):
    bl_idname="dwos.resolve_interior_contact"
    bl_label="ISSMAKER | Smart Interior Conflict"
    bl_description="Inspect neighboring window heights and choose a non-destructive preview"
    bl_options={"REGISTER","UNDO"}
    index: IntProperty(default=0,options={"HIDDEN"})
    source_a: StringProperty(default="",options={"HIDDEN"})
    source_b: StringProperty(default="",options={"HIDDEN"})
    choice: EnumProperty(items=[
      ("GAP","Independent + Gap","Preserve both independent Interior shells"),
      ("MERGE","Unified Interior","One level shell, only if both height bounds differ by <=10mm"),
      ("AUTO","AUTO","Apply safe classification; never merge staggered heights")],
      default="AUTO")

    def invoke(self,context,event):
        issues=_ISSUES.get(context.scene.name,())
        if not (0<=self.index<len(issues)):return {"CANCELLED"}
        self.source_a=issues[self.index]["a"]
        self.source_b=issues[self.index]["b"]
        self.choice="GAP" if "MERGE" not in issues[self.index].get("options",()) else "AUTO"
        return context.window_manager.invoke_props_dialog(self,width=420,confirm_text="Create Preview")

    def draw(self,context):
        issues=_ISSUES.get(context.scene.name,())
        if not (0<=self.index<len(issues)):return
        i=issues[self.index]
        layout=self.layout
        header=layout.box()
        header.label(text="INTERIOR CONTACT • "+i["status"],icon="ERROR")
        header.label(text="Window A: "+i["a"][:37],icon="WINDOW")
        header.label(text="Window B: "+i["b"][:37],icon="WINDOW")
        box=layout.box()
        box.label(text="Height analysis",icon="EMPTY_ARROWS")
        box.label(text=i["policy"].get("relation","UNKNOWN"))
        z=i.get("z",{})
        box.label(text=f"Vertical overlap: {z.get('m',0.):.3f} m")
        if "MERGE" not in i["options"]:
            box.label(text="MERGE forbidden: different heights",icon="CANCEL")
            if self.choice=="MERGE":box.label(text="Choose GAP",icon="ERROR")
        else:
            box.label(text="MERGE eligible: height tolerance <=10 mm",icon="CHECKMARK")
        layout.prop(self,"choice",text="Resolution")
        layout.label(text="Preview only — windows, LP/HP, UV unchanged",icon="INFO")
        layout.label(text="Choose the result before committing geometry")
        try:component=connected_issue_group(context.scene,self.index)
        except ValueError:component=[]
        if len(component)>=3:
            grp=layout.box()
            grp.label(text=f"Detected connected group: {len(component)} windows",icon="MOD_BOOLEAN")
            op=grp.operator("dwos.commit_connected_interior_group",
                text="Apply Global GAP",icon="CHECKMARK")
            op.index=self.index
        row=layout.row(align=True)
        op=row.operator("dwos.commit_interior_contact",
                        text="Apply resolution",icon="CHECKMARK")
        op.index=self.index
        op.mode=self.choice
        row.enabled=(self.choice!="MERGE" or "MERGE" in i["options"])

    def execute(self,context):
        issues=_ISSUES.get(context.scene.name,())
        if not 0<=self.index<len(issues):return {"CANCELLED"}
        issue=issues[self.index]
        if self.source_a and (self.source_a,self.source_b)!=(issue["a"],issue["b"]):
            self.report({"WARNING"},"Conflict selection changed; scan again")
            return {"CANCELLED"}
        a,b=bpy.data.objects.get(issue["a"]),bpy.data.objects.get(issue["b"])
        if (a is None or b is None or
                a.name not in context.scene.objects or
                b.name not in context.scene.objects):
            self.report({"WARNING"},"Interior pair changed; run scan again")
            return {"CANCELLED"}
        if self.choice=="MERGE" and "MERGE" not in issue["options"]:
            self.report({"ERROR"},"Staggered window elevations: GAP only")
            return {"CANCELLED"}
        try:
            outcome=preview_for_interior_objects(context,a,b,self.choice)
        except (ValueError,RuntimeError,Exception) as exc:
            self.report({"WARNING"},str(exc)[:220])
            return {"CANCELLED"}
        self.report({"INFO"},f"W58.8 {outcome['policy']} created {len(outcome['objects'])} preview object(s)")
        return {"FINISHED"}

class DWOS_OT_commit_interior_contact(bpy.types.Operator):
    """Apply one user's explicitly selected mode, using W58.9 atomic kernel."""
    bl_idname="dwos.commit_interior_contact"
    bl_label="ISSMAKER | Apply Interior Resolution"
    bl_description="Commit verified GAP or MERGE; Blender Undo restores the scene"
    bl_options={"REGISTER","UNDO"}
    index: IntProperty(default=0,options={"HIDDEN"})
    source_a: StringProperty(default="",options={"HIDDEN"})
    source_b: StringProperty(default="",options={"HIDDEN"})
    mode: EnumProperty(items=[
      ("GAP","Separate GAP","Atomic synchronized update of two Interior meshes"),
      ("MERGE","Single MERGE","One shared shell; original two hidden and recoverable"),
      ("AUTO","AUTO GAP","Use GAP, never silently merge")],default="GAP")
    def invoke(self,context,event):
        issues=_ISSUES.get(context.scene.name,())
        if not 0<=self.index<len(issues):
            return {"CANCELLED"}
        self.source_a=issues[self.index]["a"]
        self.source_b=issues[self.index]["b"]
        return context.window_manager.invoke_props_dialog(
            self,width=420,confirm_text="Apply to Interior")
    def draw(self,context):
        ls=_ISSUES.get(context.scene.name,())
        if not 0<=self.index<len(ls):return
        item=ls[self.index]
        box=self.layout.box()
        box.label(text="COMMIT TO TWO WINDOWS",icon="ERROR")
        box.label(text=item["a"][:42])
        box.label(text=item["b"][:42])
        box.label(text=item["policy"].get("relation","UNKNOWN"))
        box.label(text="GAP updates both shells atomically")
        box.label(text="MERGE stores two originals for recovery")
        self.layout.prop(self,"mode",text="Policy")
        self.layout.label(text="Undo and manual Revert are available",icon="LOOP_BACK")
    def execute(self,context):
        issues=scan(context.scene)
        if not 0<=self.index<len(issues):
            self.report({"WARNING"},"Conflict changed; scan again")
            return {"CANCELLED"}
        i=issues[self.index]
        if self.source_a and (self.source_a,self.source_b)!=(i["a"],i["b"]):
            self.report({"WARNING"},"Selected pair has changed; rescan")
            return {"CANCELLED"}
        from .smart_interior_resolution_transaction import (
            gap_commit,merge_commit)
        choice="GAP" if self.mode=="AUTO" else self.mode
        if choice=="MERGE" and "MERGE" not in i["options"]:
            self.report({"WARNING"},"Different window levels: GAP only")
            return {"CANCELLED"}
        try:
            a,b=i["a"],i["b"]
            from .smart_interior_resolution_transaction import _signature
            sha=[_signature(context.scene.objects[a]),_signature(context.scene.objects[b])]
            if choice=="GAP":
                result=gap_commit(context.scene,a,b,
                    clearance=i["gap_m"],expected_sha=sha)
            else:
                result=merge_commit(context.scene,a,b,expected_sha=sha)
        except Exception as exc:
            self.report({"ERROR"},"Safe resolution refused: "+str(exc)[:180])
            return {"CANCELLED"}
        context.view_layer.update()
        scan(context.scene)
        redraw()
        self.report({"INFO"},"ISSMAKER: "+str(result["status"]))
        return {"FINISHED"}

def connected_issue_group(scene,index):
    issues=_ISSUES.get(scene.name,())
    if not 0<=index<len(issues):raise ValueError("ISSUE_INDEX_CHANGED")
    graph={}
    for issue in issues:
        a,b=issue["a"],issue["b"]
        graph.setdefault(a,set()).add(b)
        graph.setdefault(b,set()).add(a)
    seed=issues[index]["a"]
    seen=set();stack=[seed]
    while stack:
        name=stack.pop()
        if name in seen:continue
        seen.add(name)
        stack.extend(graph.get(name,set())-seen)
    return sorted(seen)

class DWOS_OT_commit_connected_interior_group(bpy.types.Operator):
    bl_idname="dwos.commit_connected_interior_group"
    bl_label="ISSMAKER | Resolve Entire Interior Group"
    bl_description="Atomic global GAP of all interior objects in a connected contact graph"
    bl_options={"REGISTER","UNDO"}
    index: IntProperty(default=0,options={"HIDDEN"})
    source_names: StringProperty(default="",options={"HIDDEN"})
    def invoke(self,context,event):
        try:names=connected_issue_group(context.scene,self.index)
        except ValueError:
            self.report({"WARNING"},"Group changed; rescan")
            return {"CANCELLED"}
        if len(names)<3:
            self.report({"WARNING"},"Only two windows; use pair GAP")
            return {"CANCELLED"}
        self.source_names=json.dumps(names,separators=(",",":"))
        return context.window_manager.invoke_props_dialog(
            self,width=420,confirm_text="Apply Global GAP")
    def draw(self,context):
        try:names=connected_issue_group(context.scene,self.index)
        except ValueError:return
        self.layout.label(text=f"Connected group: {len(names)} interiors",icon="MOD_BOOLEAN")
        for name in names[:12]:
            self.layout.label(text=name[:44],icon="MESH_CUBE")
        self.layout.label(text="One validated transaction; all-or-nothing",icon="LOOP_BACK")
        self.layout.label(text="Aligned Z: planar group; staggered Z: verified 3D",icon="INFO")
        self.layout.label(text="Incompatible mouths and contacts refuse safely",icon="ERROR")
    def execute(self,context):
        issues=scan(context.scene)
        if not 0<=self.index<len(issues):
            self.report({"WARNING"},"Group changed; scan again")
            return {"CANCELLED"}
        names=connected_issue_group(context.scene,self.index)
        if self.source_names and json.loads(self.source_names)!=names:
            self.report({"WARNING"},"Contact group changed; rescan")
            return {"CANCELLED"}
        from .smart_interior_resolution_transaction import (
            global_gap_commit,_signature)
        try:
            before=[_signature(context.scene.objects[n]) for n in names]
            result=global_gap_commit(
                context.scene,names,
                clearance=float(context.scene.dwos.smart_interior_gap),
                expected_sha=before)
        except Exception as exc:
            self.report({"WARNING"},"Safe global resolution refused: "+str(exc)[:170])
            return {"CANCELLED"}
        context.view_layer.update()
        scan(context.scene)
        redraw()
        self.report({"INFO"},str(result["status"])+f": {len(names)} windows")
        return {"FINISHED"}

class DWOS_OT_unmerge_interior_group(bpy.types.Operator):
    bl_idname="dwos.unmerge_interior_group"
    bl_label="Revert Shared Interior"
    bl_description="Restore both original Interior meshes and remove the shared group"
    bl_options={"REGISTER","UNDO"}
    name: bpy.props.StringProperty(default="",options={"HIDDEN"})
    def invoke(self,context,event):
        return context.window_manager.invoke_confirm(self,event)
    def execute(self,context):
        from .smart_interior_resolution_transaction import unmerge
        try:result=unmerge(context.scene,self.name)
        except Exception as exc:
            self.report({"WARNING"},"Unmerge refused: "+str(exc)[:175])
            return {"CANCELLED"}
        context.view_layer.update()
        scan(context.scene);redraw()
        self.report({"INFO"},result["status"])
        return {"FINISHED"}

class DWOS_PT_smart_interior_conflict_navigator(bpy.types.Panel):
    bl_label="Smart Interior Conflict Navigator"
    bl_idname="DWOS_PT_smart_interior_conflict_navigator"
    bl_space_type="VIEW_3D"
    bl_region_type="UI"
    bl_category="ISSMAKER"
    bl_options={"DEFAULT_CLOSED"}
    def draw(self,context):
        col=self.layout.column(align=True)
        col.label(text="Spatial issue navigator",icon="VIEWZOOM")
        col.prop(context.scene.dwos,"smart_interior_conflict_behavior",
                 text="Resolution policy")
        if context.scene.dwos.smart_interior_conflict_behavior=="ASK_FIRST":
            col.label(text="Detection only — no automatic cuts",icon="INFO")
        col.label(text=f"Conflicts: {len(_ISSUES.get(context.scene.name,()))}")
        row=col.row(align=True)
        row.operator("dwos.scan_interior_conflicts",text="Analyse",icon="VIEWZOOM")
        row.operator("dwos.pick_interior_conflict",text="Pick",icon="RESTRICT_SELECT_OFF")
        for issue in _ISSUES.get(context.scene.name,())[:8]:
            box=col.box()
            box.label(text=f"#{issue['id']+1} {issue['status']}",icon="ERROR")
            row=box.row(align=True)
            row.operator("dwos.resolve_interior_contact",
                        text="Preview",icon="HIDE_OFF").index=issue["id"]
            action=row.operator("dwos.commit_interior_contact",
                        text="Apply GAP",icon="CHECKMARK")
            action.index=issue["id"]
            action.mode="GAP"
            try:component=connected_issue_group(context.scene,issue["id"])
            except ValueError:component=[]
            if len(component)>=3:
                op=box.operator("dwos.commit_connected_interior_group",
                    text=f"Global GAP ({len(component)})",icon="MOD_BOOLEAN")
                op.index=issue["id"]
        groups=[o for o in context.scene.objects
            if o.get("DWOS_role")=="INTERIOR_SHARED_GROUP"]
        if groups:
            col.separator()
            col.label(text="Active shared Interiors",icon="LINKED")
            for group in groups[:8]:
                col.operator("dwos.unmerge_interior_group",
                    text="Revert "+group.name[:30],
                    icon="LOOP_BACK").name=group.name

CLASSES=(DWOS_OT_scan_interior_conflicts,DWOS_OT_pick_interior_conflict,
         DWOS_OT_resolve_interior_contact,DWOS_OT_commit_interior_contact,
         DWOS_OT_unmerge_interior_group,
         DWOS_OT_commit_connected_interior_group,
         DWOS_PT_smart_interior_conflict_navigator)
