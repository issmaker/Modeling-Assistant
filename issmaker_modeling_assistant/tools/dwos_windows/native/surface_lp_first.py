"""LP-FIRST HP derivation: shared graph -> Brick Hole-like recess of GLASS zones.

Exactly one reveal per GLASS-to-STRUCTURE graph edge.  Structural front
surfaces, reveal strips, and recessed glass caps form one connected skin.
No independent box primitives, duplicate internal faces, or topology weld.
"""
from __future__ import annotations
import math
from .surface_glass_planes import glass_regions


def _loop(composer,cell,z,role,part,uv=None):
    composer.face([(*p,z) for p in cell['corners']],role,uv=uv,
                  group='HP_LP_FIRST_SHELL',part=part,module=cell.get('module'))


def build_hp(composer,graph,front_z,frame_depth,mullion_depth,interior_depth,stage,classic_glass=False):
    if stage<2:raise ValueError('LP_FIRST_NO_GRAPH')
    if not all(math.isfinite(x) and x>=0 for x in (frame_depth,mullion_depth,interior_depth)):
        raise ValueError('LP_FIRST_INVALID_DEPTH')
    if stage>=3 and frame_depth<=1e-8:raise ValueError('LP_FIRST_ZERO_RECESS')
    # W47 opt-in: actual glazing sits at 1/2 frame depth, independent from Interior.
    # Old session values preserve the prior LP-FIRST profile.
    total=(frame_depth if classic_glass else
           frame_depth+(interior_depth if stage>=6 else 0.))
    if stage>=7 and not classic_glass and mullion_depth>total+1e-8:
        raise ValueError('MULLION_PROFILE_DEEPER_THAN_GLASS_RECESS:'+str({'mullion':mullion_depth,'glass':total}))
    grid=graph['grid'];edges=[];reveal_count=0;interior_count=0;cap_count=0
    for cell in graph['cells']:
        if cell['role']!='GLASS':
            _loop(composer,cell,front_z,cell['role'],'FRONT',cell.get('source_uv'))
    cover_regions=glass_regions(graph)
    for region in cover_regions:
        fill=region['fill']
        if fill=='EMPTY' and stage>=5:continue
        if classic_glass and stage>=3:
            if fill not in ('GLASS',):
                raise ValueError('CLASSIC_FULL_SHEET_ONLY_GLASS_ZONES')
            # A true 4-vertex glass plane is published as an independent Mesh
            # object by the legacy glass builder; never fake with an HP cap.
            continue
        role=('TEMP_CAP' if stage<5 else 'PANEL' if fill=='PANEL' else 'GLASS')
        part=('LP_GLASS_PLACEHOLDER' if stage<3 else
              'EXTRUDE_CAP' if stage<5 else 'PANEL_OPT_CAP' if role=='PANEL' else 'PANE')
        cover={'corners':region['polygon']}
        _loop(composer,cover,front_z if stage<3 else front_z-total,role,part)
        cap_count+=1
    if stage<3:
        return {'build':'LP_ONLY_PREVIEW','reveal_edges':0,'glass_cap_cells':cap_count,
                'glass_region_count':len(cover_regions),'depth':0.}
    depths=({'FRAME':total,'MULLION':total} if classic_glass else
            {'FRAME':frame_depth,'MULLION':mullion_depth})
    for glass in graph['cells']:
        if glass['role']!='GLASS':continue
        i,j=glass['i'],glass['j'];corners=glass['corners']
        neighbors=((i,j-1),(i+1,j),(i,j+1),(i-1,j))
        for k,(a,b) in enumerate(zip(corners,corners[1:]+corners[:1])):
            neighbor=grid.get(neighbors[k])
            if neighbor is None:raise ValueError('GLASS_RECESS_TOUCHES_OUTER_BORDER')
            if neighbor['role']=='GLASS':continue
            role=neighbor['role']
            if role not in depths:raise ValueError('UNKNOWN_GLASS_NEIGHBOR:'+role)
            profile=min(depths[role],total)
            if profile<=0:raise ValueError('INVALID_GLASS_REVEAL_PROFILE')
            # Every reveal is split at ALL globally shared profile depths.
            # Otherwise FRAME/MULLION corner lines create hidden T-junctions
            # where one side has a vertex at .08m and the other at .12m.
            levels=sorted({0.,total,*[d for d in depths.values() if 1e-8<d<total-1e-8]})
            for da,db in zip(levels,levels[1:]):
                segment_role=role if (da<profile-1e-8 or role=='MULLION') else 'INTERIOR'
                composer.face([(*a,front_z-da),(*b,front_z-da),
                               (*b,front_z-db),(*a,front_z-db)],
                              segment_role,group='HP_LP_FIRST_SHELL',
                              part='GLASS_REVEAL' if segment_role!='INTERIOR' else 'GLASS_INTERIOR',
                              module=neighbor.get('module'),
                              uv_edge=(a,b) if segment_role!='INTERIOR' else None)
                if segment_role=='INTERIOR':interior_count+=1
                else:reveal_count+=1
            edges.append({'glass_cell':[i,j],'neighbor':[neighbor['i'],neighbor['j']],
                          'role':role,'from':list(a),'to':list(b),
                          'profile_depth':profile,'glass_depth':total,
                          'source_module':neighbor.get('module')})
    return {'build':'LP_FIRST_BRICK_HOLE_GLASS_ZONE','reveal_edges':reveal_count,
            'glass_cap_cells':cap_count,'glass_region_count':len(cover_regions),
             'interior_faces':interior_count,
            'depth':total,'classic_glass_midframe':bool(classic_glass),
            'interior_separate':bool(classic_glass and stage>=6),
            'reveal_edges_sample':edges[:80]}
