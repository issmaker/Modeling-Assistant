"""W58.25 FRAME-LOCKED research corner-post generator.
The native DWOS frame is immutable; new post faces are appended and glass seats
are regenerated. No edits to UVWall objects, authored paths, LP geometry.
This prototype is not integrated into Composer/UndoRedo yet.
"""
import math,json,hashlib
from mathutils import Vector
import bpy

def sig_geom(mesh):
 h=hashlib.sha256()
 for v in mesh.vertices:h.update(repr(tuple(round(float(x),8) for x in v.co)).encode())
 for f in mesh.polygons:h.update(repr(tuple(int(x) for x in f.vertices)).encode())
 return h.hexdigest()

def v2(a):return Vector((float(a[0]),float(a[1])))
def cross2(a,b):return a.x*b.y-a.y*b.x
def area2(points):return .5*sum(cross2(a,b) for a,b in zip(points,points[1:]+points[:1]))
def no_self_intersection(poly):
 def intersect(a,b,c,d):
  return (cross2(b-a,c-a)*cross2(b-a,d-a)<-1e-10 and
          cross2(d-c,a-c)*cross2(d-c,b-c)<-1e-10)
 for i in range(len(poly)):
  for j in range(i+2,len(poly)):
   if i==0 and j==len(poly)-1:continue
   if intersect(poly[i],poly[(i+1)%len(poly)],poly[j],poly[(j+1)%len(poly)]):return False
 return True

def get_geometry_basis(hp,lp,glass):
 if not lp or len(lp.data.vertices)<4:raise ValueError("LP_REFERENCE_MISSING")
 host=hp.dwos_window.host_name
 if host not in bpy.data.objects:raise ValueError("AUTHORED_HOST_NOT_FOUND")
 if not hp.data.uv_layers.active:raise ValueError("FRAME_UV_REFERENCE_MISSING")
 bpy.context.view_layer.update()
 # All geometry is expressed in HP's local xz cross-section, y=height.
 # LP-generated HP is the authored frame reference; LP remains unmodified.
 hvs=list(hp.data.vertices)
 ylo=min(float(v.co.y) for v in hvs)
 yhi=max(float(v.co.y) for v in hvs)
 # The geometric apex is the authored center station, NOT Blender X=0.
 anchor_x=float(hvs[1].co.x)
 s=[v.co for v in hvs if abs(v.co.x-anchor_x)<.0005 and v.co.y<0]
 if len(s)<2:raise ValueError("LP_APEX_NOT_PRESENT_IN_HP")
 front=max(s,key=lambda v:v.z)
 back=min(s,key=lambda v:v.z)
 cF=Vector((front.x,front.z));cB=Vector((back.x,back.z))
 axis=(cB-cF)
 if axis.length<.02:raise ValueError("LP_ANGLE_TOO_SHALLOW")
 axis.normalize()
 # Glass geometry: use the actual front and back pane center and end
 # stations. These were built by DWOS from this same authored LP.
 matrix=hp.matrix_world.inverted() @ glass.matrix_world
 gv=[matrix @v.co for v in glass.data.vertices]
 ymin=min(v.y for v in gv);ymax=max(v.y for v in gv)
 bottoms=[v for v in gv if abs(v.y-ymin)<.0005]
 near=[v for v in bottoms if abs(v.x-anchor_x)<.0005]
 if len(near)<2:raise ValueError("GLASS_CORNER_STATION_MISSING")
 front_g=max(near,key=lambda v:v.z)
 back_g=min(near,key=lambda v:v.z)
 left=[v for v in bottoms if v.x<-.2]
 right=[v for v in bottoms if v.x>.2]
 if not left or not right:raise ValueError("GLASS_TWO_WINGS_MISSING")
 # Front of glass sheet is the less negative z in this test rig.
 left_f=max(left,key=lambda v:v.z if abs(v.x-min(z.x for z in left))<.001 else -999.)
 left_b=min((v for v in left if abs(v.x-left_f.x)<.005),key=lambda v:v.z)
 right_f=max(right,key=lambda v:v.z if abs(v.x-max(z.x for z in right))<.001 else -999.)
 right_b=min((v for v in right if abs(v.x-right_f.x)<.005),key=lambda v:v.z)
 ldir=(v2((left_f.x,left_f.z))-v2((front_g.x,front_g.z))).normalized()
 rdir=(v2((right_f.x,right_f.z))-v2((front_g.x,front_g.z))).normalized()
 angle=math.degrees(math.acos(max(-1.,min(1.,float(ldir.dot(rdir))))))
 if not 0<angle<179.:raise ValueError("LP_JUNCTION_UNSUPPORTED")
 return {"lp_obj":lp.name,"host":host,"front":cF,"back":cB,"axis":axis,
         "glass_center_front":v2((front_g.x,front_g.z)),
         "glass_center_back":v2((back_g.x,back_g.z)),
         "glass_left_front":v2((left_f.x,left_f.z)),
         "glass_left_back":v2((left_b.x,left_b.z)),
         "glass_right_front":v2((right_f.x,right_f.z)),
         "glass_right_back":v2((right_b.x,right_b.z)),
         "left_dir":ldir,"right_dir":rdir,
         "angle_deg":angle,"ymin":ymin,"ymax":ymax,"hp_ylo":ylo,"hp_yhi":yhi}

def plan_profiles(basis,mode,requested_seat=.055):
 if mode not in ("V_COMPACT","QUAD_ADAPTER"):raise ValueError("UNKNOWN_POST_MODE")
 ang=basis["angle_deg"]
 if ang<65.-6e-5:return {"status":"NO_POST","angle":ang}
 if ang>170.:raise ValueError("ANGLE_NEAR_FLAT_UNSUPPORTED")
 glass_c=basis["glass_center_front"]
 endL=glass_c+basis["left_dir"]*requested_seat
 endR=glass_c+basis["right_dir"]*requested_seat
 front=basis["front"];back=basis["back"];axis=basis["axis"]
 # All offsets along LP-authored inboard axis. No frame vertices change.
 head=front+axis*.010
 tail=back-axis*.008
 if mode=="V_COMPACT":
  poly=[head,endR,tail,endL]
 else:
  front_w=min(.015,abs(endR.x)*.27)
  back_w=min(.023,abs(endR.x)*.47)
  h=front+axis*.015
  t=back-axis*.010
  tangent=Vector((-axis.y,axis.x))
  poly=[h-tangent*front_w,h+tangent*front_w,endR,
        t+tangent*back_w,t-tangent*back_w,endL]
 if abs(area2(poly))<.0002:raise ValueError("POST_FOOTPRINT_TOO_SMALL")
 if not no_self_intersection(poly):raise ValueError("POST_FOOTPRINT_SELF_INTERSECT")
 return {"status":"POST_OK","cross_section":poly,"angle":ang,"mode":mode,
         "seats":[endL,endR],"glass_setback_m":requested_seat,
         "depth_m":(tail-head).length}

def metrically_project_faces(mesh,old_loop_uv):
 layer=mesh.uv_layers.active or mesh.uv_layers.new(name="UVMap")
 count=len(old_loop_uv)
 for idx,uv in enumerate(old_loop_uv):layer.data[idx].uv=uv
 for face in mesh.polygons:
  if face.loop_start<count:continue
  vv=[mesh.vertices[i].co for i in face.vertices]
  a=vv[0]
  longest=max((vv[j]-vv[k] for j in range(len(vv)) for k in range(j)),key=lambda v:v.length,default=None)
  if longest is None or longest.length<1e-7:raise ValueError("NEW_POST_UV_ZERO_EDGE")
  u=longest.normalized()
  normal=face.normal
  if normal.length<.9:raise ValueError("NEW_POST_NORMAL_INVALID")
  v=normal.cross(u).normalized()
  pts=[((z-a).dot(u),(z-a).dot(v)) for z in vv]
  minu=min(x for x,y in pts);minv=min(y for x,y in pts)
  for li,(x,y) in zip(face.loop_indices,pts):
   layer.data[li].uv=(.012+(x-minu)/4.096,.012+(y-minv)/4.096)
  for j,k in enumerate(face.loop_indices):
   nxt=face.loop_indices[(j+1)%len(vv)]
   d=(vv[(j+1)%len(vv)]-vv[j]).length
   duv=(layer.data[nxt].uv-layer.data[k].uv).length*4096
   if d<1e-7 or abs(duv/d-1000)>2.0:raise ValueError("NEW_POST_UV_DENSITY_ERROR")
 return layer

def append_post_geometry(frame,section):
 orig=frame.data
 points=[tuple(v.co) for v in orig.vertices]
 faces=[tuple(face.vertices) for face in orig.polygons]
 source_roles=json.loads(frame.get("DWOS_face_roles","[]"))
 if len(source_roles)!=len(faces):raise ValueError("SOURCE_ROLES_MISMATCH")
 original_uv=[tuple(z.uv) for z in orig.uv_layers.active.data]
 n=len(section)
 # Bottom/top of the vertical post penetrate halfway into adjacent
 # horizontal frame belts: geometrically seated; topology intentionally
 # independent to protect the authored frame.
 ymin=max(p.y for p in (Vector((0,0,0)),)) # dummy for deterministic start
 hm=frame.data.vertices
 abs_y=max(float(abs(v.co.y)) for v in hm)
 inner_y=sorted(set(round(float(abs(v.co.y)),5) for v in hm))[0]
 extent=inner_y+(abs_y-inner_y)*.5
 lo=-extent;hi=extent
 start=len(points)
 for y in (lo,hi):
  for q in section:points.append((q.x,y,q.y))
 post_faces=[]
 # opposite caps; side wall ring
 post_faces.append(tuple(start+i for i in reversed(range(n))))
 post_faces.append(tuple(start+n+i for i in range(n)))
 for i in range(n):
  j=(i+1)%n
  post_faces.append((start+i,start+j,start+n+j,start+n+i))
 new=bpy.data.meshes.new(orig.name+"_W5825_FRAME_LOCKED")
 new.from_pydata(points,[],faces+post_faces)
 new.update()
 for mat in orig.materials:new.materials.append(mat)
 for idx,f in enumerate(orig.polygons):
  new.polygons[idx].material_index=f.material_index
 new.uv_layers.new(name=orig.uv_layers.active.name)
 metrically_project_faces(new,original_uv)
 # STRICT frame invariance: no preexisting vertex or face may move.
 assert [tuple(v.co) for v in new.vertices[:len(orig.vertices)]]==[tuple(v.co) for v in orig.vertices]
 assert [tuple(f.vertices) for f in new.polygons[:len(orig.polygons)]]==faces
 for j,uv in enumerate(original_uv):
  if (new.uv_layers.active.data[j].uv-Vector(uv)).length>1e-7:
   raise ValueError("SOURCE_FRAME_UV_CHANGED")
 return new,len(points)-len(orig.vertices),len(post_faces),source_roles+["FRAME_CORNER_POST_CAP"]*2+["FRAME_CORNER_POST_BODY"]*n,extent

def split_glass_at_seats(hp,glass,basis,requested_seat=.055):
 orig=glass.data
 # Seats are measured along the original 2 glass paths from the DWOS apex.
 # No change to outer endpoints, frame, original author path, or LP geometry.
 matrix=glass.matrix_world.inverted() @hp.matrix_world
 front=basis["glass_center_front"]
 back=basis["glass_center_back"]
 ymin,ymax=basis["ymin"],basis["ymax"]
 vertices=[];faces=[];roles=[]
 for name in ("left","right"):
  outerF=basis["glass_"+name+"_front"]
  outerB=basis["glass_"+name+"_back"]
  dF=(outerF-front).normalized()
  dB=(outerB-back).normalized()
  # Add a 1 mm glass clearance beyond the post seat.
  insideF=front+dF*(requested_seat+.001)
  insideB=back+dB*(requested_seat+.001)
  if (outerF-front).length<requested_seat+.10:raise ValueError("GLASS_PANE_TOO_NARROW")
  start=len(vertices)
  for q,y in ((outerF,ymin),(outerF,ymax),(insideF,ymin),(insideF,ymax),
              (outerB,ymin),(outerB,ymax),(insideB,ymin),(insideB,ymax)):
   pos=matrix@Vector((q.x,y,q.y));vertices.append(tuple(pos))
  section=[(0,2,3,1),(6,4,5,7),(4,0,1,5),(2,6,7,3),(1,3,7,5),(4,6,2,0)]
  for k,indices in enumerate(section):
   faces.append(tuple(start+i for i in (indices if name=="left" else reversed(indices))))
   roles.append("GLASS_EXTERIOR" if k==0 else "GLASS_INTERIOR" if k==1 else "GLASS_EDGE")
 mesh=bpy.data.meshes.new(orig.name+"_W5825_SPLIT")
 mesh.from_pydata(vertices,[],faces);mesh.update()
 for mat in orig.materials:mesh.materials.append(mat)
 if len(faces)!=12 or len(vertices)!=16:raise ValueError("PANE_GEOMETRY_BAD")
 if any(f.area<1e-8 for f in mesh.polygons):raise ValueError("GLASS_ZERO_AREA")
 return mesh,roles

def do_frame_locked(frame,mode="V_COMPACT",min_angle=65.):
 if frame.get("DWOS_W58_auto_role")!="HP":raise ValueError("NOT_DWOS_HP")
 lp=next((x for x in frame.children if x.get("DWOS_W58_auto_role")=="LP"),None)
 glass=next((x for x in frame.children if x.get("DWOS_role")=="GLASS"),None)
 interior=next((x for x in frame.children if x.get("DWOS_role")=="INTERIOR"),None)
 if not all([lp,glass,interior]):raise ValueError("DEPENDENCY_MISSING")
 orig_geom=sig_geom(frame.data)
 basis=get_geometry_basis(frame,lp,glass)
 plan=plan_profiles(basis,mode)
 frame["W5825_frame_geometry_baseline_sha"]=orig_geom
 frame["W5825_LP_source"]=lp.name
 frame["W5825_post_requested_mode"]=mode
 frame["W5825_lp_angle_deg"]=float(basis["angle_deg"])
 frame["W5825_hud_min_corner_post_deg"]=65.0
 if plan["status"]=="NO_POST":
  frame["W5825_post_status"]="NO_POST"
  frame["W5825_hud_hint"]=f"Окно построено. Центральная стойка при {basis['angle_deg']:.1f}° недоступна; минимум 65°. Другие настройки доступны."
  return {"status":"NO_POST","source_hp_sha":orig_geom,"glass_rebuilt":False,"mode":mode,"angle":basis["angle_deg"]}
 new,added,post_faces,roles,extent=append_post_geometry(frame,plan["cross_section"])
 new_glass,glass_roles=split_glass_at_seats(frame,glass,basis)
 # All preparation and tests above are pure staging. Commit only now.
 oldframe=frame.data
 oldglass=glass.data
 frame.data=new
 glass.data=new_glass
 frame["DWOS_face_roles"]=json.dumps(roles)
 glass["DWOS_glass_roles"]=json.dumps(glass_roles)
 frame["W5825_post_status"]="FRAME_LOCKED"
 frame["W5825_post_profile"]=mode
 frame["W5825_post_vertex_count"]=added
 frame["W5825_post_face_count"]=post_faces
 frame["W5825_post_in_frame_mesh"]=True
 frame["W5825_post_separate_island"]=True
 frame["W5825_post_parametric_lp_link"]=lp.name
 frame["W5825_glass_cut_for_post"]=True
 frame["W5825_profile_depth_m"]=plan["depth_m"]
 frame["W5825_post_axis_xz"]=[float(x) for x in basis["axis"]]
 frame["W5825_post_clearance_m"]=.001
 frame["W5825_post_frame_belt_penetration_m"]=.040
 frame["W5825_hud_hint"]="FRAME LOCKED: импост построен от LP; рамный контур не меняется. Доступно редактирование формы узла в прототипе."
 # Original HP first N vertex and polygon structure must remain.
 n0=len(oldframe.vertices);f0=len(oldframe.polygons)
 assert [tuple(v.co) for v in frame.data.vertices[:n0]]==[tuple(v.co) for v in oldframe.vertices]
 assert [tuple(f.vertices) for f in frame.data.polygons[:f0]]==[tuple(f.vertices) for f in oldframe.polygons]
 assert sum(1 for r in glass_roles if r=="GLASS_EXTERIOR")==2
 return {"status":"FRAME_LOCKED","source_hp_sha":orig_geom,"new_hp_verts":len(frame.data.vertices),
         "new_hp_faces":len(frame.data.polygons),"source_hp_verts":n0,
         "source_hp_faces":f0,"post_verts":added,"post_faces":post_faces,
         "glass_rebuilt":True,"glass_faces":len(glass.data.polygons),
         "profile":mode,"angle":basis["angle_deg"],"LP":lp.name,
         "seat_setback_m":plan["glass_setback_m"],"axis":[float(x) for x in basis["axis"]]}
