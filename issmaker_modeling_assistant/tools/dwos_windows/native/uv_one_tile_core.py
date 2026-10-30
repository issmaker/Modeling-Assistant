import math,json,collections
APPLY=True
eps=0.000001
def pack_frame(ob):
 me=ob.data
 lay=me.uv_layers.active
 roles=json.loads(ob.get("DWOS_face_roles","[]"))
 if len(roles)!=len(me.polygons):
  raise ValueError("FRAME_ROLE_MAPPING_MISSING:"+ob.name)
 original=[tuple(l.uv) for l in lay.data]
 # W58.21: Valid in-tile reveals must remain locked to their authored
 # front contact; moving them used to create 182 unnecessary visible seams.
 # Only inherently out-of-tile reveal faces may move. Interior/back faces
 # can be independently re-islanded as required, retaining 1000 px/m.
 def _needs_reveal_relocation(face):
  return any(not (eps <= lay.data[li].uv.x <= 1.-eps and
                  eps <= lay.data[li].uv.y <= 1.-eps) for li in face.loop_indices)
 allowed={f.index for f in me.polygons
          if roles[f.index].endswith("_BACK") or
          (roles[f.index]=="FRAME_REVEAL" and _needs_reveal_relocation(f))}
 fronts={f.index for f in me.polygons if roles[f.index]=="FRAME_EXTERIOR"}
 # undirected UV edges, store both matching UV endpoints for each mesh edge
 emap=collections.defaultdict(list)
 intervals={}
 for f in me.polygons:
  ll=list(f.loop_indices)
  uv=[lay.data[i].uv for i in ll]
  if f.index in allowed:
   u0=min(pt.x for pt in uv);u1=max(pt.x for pt in uv);v0=min(pt.y for pt in uv);v1=max(pt.y for pt in uv)
   intervals[f.index]=(eps-u0,1-eps-u1,eps-v0,1-eps-v1)
  for j,li in enumerate(ll):
   lj=ll[(j+1)%len(ll)]
   va=me.loops[li].vertex_index;vb=me.loops[lj].vertex_index
   edge=tuple(sorted((va,vb)))
   epts={va:tuple(uv[j]),vb:tuple(uv[(j+1)%len(ll)])}
   emap[edge].append((f.index,epts))
 neighbors=collections.defaultdict(set)
 old_seams=0;old_links=0
 for edge,items in emap.items():
  if len(items)!=2:continue
  (a,au),(b,bu)=items
  was_linked=all(abs(au[v][q]-bu[v][q])<=0.00001 for v in edge for q in (0,1))
  if was_linked:old_links+=1
  else:old_seams+=1
  if a in allowed and b in allowed and was_linked:
   neighbors[a].add(b);neighbors[b].add(a)
 for face,iv in intervals.items():
  if iv[0]>iv[1] or iv[2]>iv[3]:
   raise RuntimeError("FACE_TOO_LARGE_FOR_ONE_TILE:"+ob.name+":"+str(face))
 def choose(lo,hi):
  return lo if lo>0 else hi if hi<0 else 0.
 # Priority: first outliers, then remaining faces, to minimize bad-face fragmentation
 priority=sorted(allowed,key=lambda face:(all(intervals[face][j]<=0<=intervals[face][j+1] for j in (0,2)),face))
 unseen=set(allowed);groups=[]
 while unseen:
  seed=next(f for f in priority if f in unseen)
  unseen.remove(seed);group=[seed];Iv=list(intervals[seed]);queue=collections.deque([seed])
  while queue:
   node=queue.popleft()
   for nxt in sorted(neighbors[node]):
    if nxt not in unseen:continue
    candidate=intervals[nxt]
    bounds=[max(Iv[0],candidate[0]),min(Iv[1],candidate[1]),max(Iv[2],candidate[2]),min(Iv[3],candidate[3])]
    if bounds[0]>bounds[1]+1e-9 or bounds[2]>bounds[3]+1e-9:continue
    unseen.remove(nxt);group.append(nxt);Iv=bounds
    queue.append(nxt)
  groups.append((group,choose(Iv[0],Iv[1]),choose(Iv[2],Iv[3])))
 proposed=list(original)
 shifts=0;shifted_faces=0
 for faces,du,dv in groups:
  if abs(du)+abs(dv)<1e-10:continue
  shifts+=1;shifted_faces+=len(faces)
  for fi in faces:
   for loop_id in me.polygons[fi].loop_indices:
    u,v=original[loop_id]
    proposed[loop_id]=(u+du,v+dv)
 # Verify all faces in 0-1, unchanged fronts and unchanged density per edge
 outside=0
 for f in me.polygons:
  for i in f.loop_indices:
   u,v=proposed[i]
   if u<-1e-7 or v<-1e-7 or u>1+1e-7 or v>1+1e-7:
    outside+=1
    if outside<3:print("OUTSIDE",ob.name,f.index,u,v)
 assert not outside,("OUTSIDE_AFTER_PLAN",ob.name,outside)
 for f in me.polygons:
  if f.index not in allowed:
   for i in f.loop_indices:assert proposed[i]==original[i],("LOCKED_UV_CHANGED",ob.name,f.index)
 for f in me.polygons:
  for j in range(f.loop_total):
   a=f.loop_start+j;b=f.loop_start+(j+1)%f.loop_total
   d0=math.dist(original[a],original[b]);d1=math.dist(proposed[a],proposed[b])
   assert abs(d0-d1)<1e-7,("METRIC_CHANGED",ob.name,f.index)
 new_links=0;new_seams=0;newly_broken=0
 for edge,items in emap.items():
  if len(items)!=2:continue
  (a,old_a),(b,old_b)=items
  def faceuv(f,v):
   poly=me.polygons[f]
   for i in poly.loop_indices:
    if me.loops[i].vertex_index==v:return proposed[i]
   raise RuntimeError("VERT_NOT_IN_FACE")
  was=all(abs(old_a[v][q]-old_b[v][q])<.00001 for v in edge for q in (0,1))
  now=all(abs(faceuv(a,v)[q]-faceuv(b,v)[q])<.00001 for v in edge for q in (0,1))
  if now:new_links+=1
  else:new_seams+=1
  if was and not now:newly_broken+=1
 if APPLY:
  for i,(u,v) in enumerate(proposed):lay.data[i].uv=(u,v)
  me["W5820_uv_one_tile_planner"]="ROLE_LOCKED_CONNECTED_COMPONENT_TRANSITIVE_INTERVAL_V1"
  me["W5820_uv_front_preserved"]=True
  me["W5820_uv_newly_broken_original_uv_links"]=newly_broken
 return {"name":ob.name,"class":ob.get("DWOS_W58_topology_class"),"roles_allowed_faces":len(allowed),"groups":len(groups),"shifted_groups":shifts,"shifted_faces":shifted_faces,"new_seams":new_seams,"original_seams":old_seams,"newly_broken_links":newly_broken,"original_uv_links":old_links,"front_faces_locked":len(fronts)}
