"""Read-only, bounded, semantic WALL/CAP/UV forensic evidence.

No bpy/bmesh import or mesh mutation. Works on Blender RNA collections and
minimal offline fake meshes.  Counts are observational; no automatic deletion.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from math import hypot, sqrt, isfinite

ROLES = {0: 'UNKNOWN', 10: 'WALL_OUTER', 11: 'WALL_INNER', 12: 'END_CAP',
         20: 'TOP_CAP', 21: 'BOTTOM_CAP', 30: 'SURFACE', 40: 'CORNICE'}
WALL = {10, 11}
CAP = {20, 21}
EPS_UV = 1e-6


def _xyz(v):
    co = v.co
    return (float(co.x), float(co.y), float(co.z))


def _area3(a, b, c):
    dx, dy, dz = (b[i] - a[i] for i in range(3))
    ex, ey, ez = (c[i] - a[i] for i in range(3))
    return .5 * sqrt((dy*ez-dz*ey)**2 + (dz*ex-dx*ez)**2 + (dx*ey-dy*ex)**2)


def _area2(a, b, c):
    return abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))*.5


def _role_codes(mesh, nf):
    try:
        attr = mesh.attributes.get('uvwall_face_role')
        if attr is not None and getattr(attr, 'domain', None) == 'FACE' and len(attr.data) == nf:
            return [int(v.value) for v in attr.data], True
    except (AttributeError, KeyError, TypeError):
        pass
    return [0] * nf, False


def _sample(container, value, limit=120):
    if len(container) < limit:
        container.append(value)


def inspect_mesh(obj, tile_length=4.0, max_faces=250_000, max_edges=500_000):
    """Analyse ALL faces/edges only below limits; return useful bounded evidence.

    Uses face-edge adjacency by shared vertex indices, never nearest XY alone.
    A coplanar edge is only a *candidate*, not safe deletion authorization.
    """
    mesh = obj.data
    verts, edges, polygons = mesh.vertices, mesh.edges, mesh.polygons
    nv, ne, nf = len(verts), len(edges), len(polygons)
    report = {
        'object': obj.name, 'mesh': mesh.name, 'mode': str(obj.get('uv_wall_mode', 'UNSPECIFIED')),
        'counts': {'vertices': nv, 'edges': ne, 'faces': nf, 'euler_characteristic': nv-ne+nf},
        'coordinate_basis': 'mesh local coordinates; use geometry_snapshot.json matrix_world for world reconstruction',
        'audit_policy': 'read-only; candidates are NOT permission to delete edges',
        'status': 'OK',
    }
    try:
        import json as _json
        payload = obj.get('uvwall_topology_optimization', '')
        report['optimizer_evidence'] = (_json.loads(payload) if isinstance(payload, str) and payload else
                                         {'status': 'NOT_RECORDED'})
    except (ValueError, TypeError):
        report['optimizer_evidence'] = {'status': 'MALFORMED'}
    try:
        import json as _json
        payload = obj.get('uvwall_topology_first', '')
        report['topology_first_evidence'] = (_json.loads(payload) if isinstance(payload, str) and payload else
                                            {'status': 'NOT_RECORDED'})
    except (ValueError, TypeError):
        report['topology_first_evidence'] = {'status': 'MALFORMED'}
    try:
        import json as _json
        evidence = obj.get('uvwall_boundary_first', '')
        report['boundary_first_evidence'] = (
            _json.loads(evidence) if isinstance(evidence, str) and evidence else
            {'status': 'NOT_RECORDED'}
        )
    except (ValueError, TypeError):
        report['boundary_first_evidence'] = {'status': 'MALFORMED'}
    try:
        import json as _json
        evidence = obj.get('uvwall_vertex_minimal', '')
        report['vertex_minimal_evidence'] = (
            _json.loads(evidence) if isinstance(evidence, str) and evidence else
            {'status': 'NOT_RECORDED'}
        )
    except (ValueError, TypeError):
        report['vertex_minimal_evidence'] = {'status': 'MALFORMED'}
    if report['mode'] != 'WALL':
        # A reused UV_Wall Object may retain historical WALL metadata when
        # inspected before its next normal metadata reset. Never mislabel
        # CORNICE diagnostics with an old WALL optimization report.
        for key in ('optimizer_evidence','topology_first_evidence','boundary_first_evidence','vertex_minimal_evidence'):
            report[key] = {'status':'NOT_APPLICABLE_TO_MODE'}
    if nf > max_faces or ne > max_edges:
        report.update(status='BOUNDED_SKIP', reason='Mesh exceeds safe forensic limits',
                      limits={'faces': max_faces, 'edges': max_edges})
        return report
    roles, has_roles = _role_codes(mesh, nf)
    report['semantic_roles'] = {
        'available': has_roles,
        'counts': dict(Counter(ROLES.get(r, f'CODE_{r}') for r in roles)),
        'interpretation': 'UNKNOWN if face role attribute missing; cannot prove CAP↔WALL contact',
    }
    position = [_xyz(v) for v in verts]
    adjacency = defaultdict(list)
    uv_ends = defaultdict(list)
    uv_layer = getattr(getattr(mesh, 'uv_layers', None), 'active', None)
    uv_data = getattr(uv_layer, 'data', None)
    face_area = 0.0
    uv_area = 0.0
    triangles = 0
    degenerate_xyz, degenerate_uv = 0, 0
    invalid_uv = 0
    density_px_per_m = []
    density_anomalies = []
    for fi, face in enumerate(polygons):
        loop_idx = list(face.loop_indices)
        if not loop_idx:
            continue
        xyz = [position[int(mesh.loops[i].vertex_index)] for i in loop_idx]
        uv = None
        if uv_data is not None:
            try:
                uv = [tuple(map(float, uv_data[i].uv)) for i in loop_idx]
                invalid_uv += sum(not all(isfinite(v) for v in val) for val in uv)
            except (IndexError, AttributeError, TypeError, ValueError):
                uv = None
        incident_in_this_face = set()
        for i, loop_id in enumerate(loop_idx):
            loop = mesh.loops[loop_id]
            e = int(loop.edge_index)
            if e not in incident_in_this_face:
                adjacency[e].append(fi)
                incident_in_this_face.add(e)
            if uv is not None:
                a, b = int(loop.vertex_index), int(mesh.loops[loop_idx[(i+1) % len(loop_idx)]].vertex_index)
                uv_ends[e].append({a: uv[i], b: uv[(i+1) % len(loop_idx)]})
        for j in range(1, len(xyz)-1):
            a = _area3(xyz[0], xyz[j], xyz[j+1])
            face_area += a
            triangles += 1
            if a <= 1e-12:
                degenerate_xyz += 1
            if uv is not None:
                u = _area2(uv[0], uv[j], uv[j+1])
                uv_area += u
                if u <= 1e-13:
                    degenerate_uv += 1
                if a > 1e-12 and u > 1e-13:
                    density = sqrt(u/a) * round(float(tile_length)*1024)
                    if isfinite(density):
                        density_px_per_m.append(density)
                        if abs(density-1000.) > 50.:
                            _sample(density_anomalies, {'face': fi, 'triangle': j-1,
                                                      'estimated_px_per_m': round(density, 3)}, 90)
    valence = Counter(); pairs = Counter(); seam_pairs = Counter()
    caps_border = []; wall_cap_examples = []; candidates = []
    nonmanifold = []; boundary = []; isolated = []
    horiz = Counter(); horiz_near = []
    cap_contact = 0
    cap_border_count = 0
    for edge in edges:
        i = int(edge.index)
        incident = adjacency.get(i, [])
        n = len(incident)
        valence[str(n)] += 1
        r = tuple(sorted(roles[fi] for fi in incident))
        role_label = '+'.join(ROLES.get(x, f'CODE_{x}') for x in r) or 'WIRE'
        pairs[role_label] += 1
        if n == 0: _sample(isolated, i)
        if n == 1: _sample(boundary, i)
        if n > 2: _sample(nonmanifold, {'edge': i, 'face_count': n, 'roles': role_label})
        if n == 1 and r and r[0] in CAP:
            cap_border_count += 1
            _sample(caps_border, i)
        if n == 2 and r[0] in WALL and r[1] in CAP or n == 2 and r[0] in CAP and r[1] in WALL:
            cap_contact += 1
            _sample(wall_cap_examples, {'edge': i, 'roles': role_label})
        seam = False
        if n == 2 and uv_data is not None:
            endpoint = uv_ends.get(i, ())
            if len(endpoint) >= 2:
                a, b = endpoint[:2]
                seam = set(a) != set(b) or any(
                    any(abs(a[k][d]-b[k][d]) > EPS_UV for d in (0, 1))
                    for k in set(a).intersection(b))
                if seam: seam_pairs[role_label] += 1
        va, vb = (int(k) for k in edge.vertices)
        pa, pb = position[va], position[vb]
        if r and all(role in WALL for role in r) and abs(pa[2]-pb[2]) < 1e-5:
            horiz['total'] += 1
            z = round((pa[2]+pb[2])*.5, 5)
            horiz[str(z)] += 1
            _sample(horiz_near, {'edge': i, 'z_local_m': z, 'length_m': round(hypot(pb[0]-pa[0], pb[1]-pa[1]), 7)}, 80)
        if n == 2 and r[0] == r[1] and not seam:
            f0, f1 = polygons[incident[0]], polygons[incident[1]]
            n0, n1 = f0.normal, f1.normal
            dot = sum(float(n0[k])*float(n1[k]) for k in (0, 1, 2))
            if int(f0.material_index) == int(f1.material_index) and dot > 0.999999:
                _sample(candidates, {'edge': i, 'role': ROLES.get(r[0], 'UNKNOWN'),
                                     'normal_dot': round(dot, 8), 'uv_seam': False}, 150)
    # Face components by shared geometric *edge*, not coincident coordinates.
    neighbors = [[] for _ in range(nf)]
    for face_ids in adjacency.values():
        if len(face_ids) == 2:
            x, y = face_ids
            neighbors[x].append(y); neighbors[y].append(x)
    seen = bytearray(nf)
    components = []
    components_count = 0
    for start in range(nf):
        if seen[start]: continue
        seen[start] = 1; queue = deque([start]); count = 0; roles_seen = Counter()
        while queue:
            f = queue.popleft(); count += 1; roles_seen[ROLES.get(roles[f], 'UNKNOWN')] += 1
            for adj in neighbors[f]:
                if not seen[adj]: seen[adj] = 1; queue.append(adj)
        components_count += 1
        if len(components) < 100:
            components.append({'face_count': count, 'roles': dict(roles_seen)})
    report['topology'] = {
        'edge_face_valence': dict(valence), 'role_pairs': dict(pairs),
        'boundary_edges_count': valence.get('1', 0), 'boundary_edge_samples': boundary,
        'wire_edges_count': valence.get('0', 0), 'wire_edge_samples': isolated,
        'nonmanifold_edges_over_2_faces_count': sum(v for k, v in valence.items() if int(k) > 2),
        'nonmanifold_samples': nonmanifold,
        'face_connected_components_count': components_count, 'components_sample': components,
        'disconnected_components_caution': 'Disconnected shells can be intentional; inspect per-role and per-object',
    }
    report['wall_cap_stitch'] = {
        'status': 'MEASURED' if has_roles and any(r in CAP for r in roles) else 'UNKNOWN_OR_NO_CAP',
        'shared_topological_edges': cap_contact,
        'contact_samples': wall_cap_examples,
        'cap_boundary_missing_second_face_count': cap_border_count,
        'cap_boundary_samples': caps_border,
        'note': 'Counts require actual shared edge indices; co-located positions are insufficient',
    }
    densities_sorted = sorted(density_px_per_m)
    report['uv'] = {
        'layer': getattr(uv_layer, 'name', None),
        'uv_seams_by_adjacent_role': dict(seam_pairs),
        'uv_seams_total': sum(seam_pairs.values()),
        'degenerate_uv_triangles': degenerate_uv,
        'triangle_density_estimates_px_per_m': {
            'count': len(densities_sorted),
            'min': round(densities_sorted[0], 3) if densities_sorted else None,
            'p10': round(densities_sorted[int((len(densities_sorted)-1)*.1)], 3) if densities_sorted else None,
            'median': round(densities_sorted[(len(densities_sorted)-1)//2], 3) if densities_sorted else None,
            'p90': round(densities_sorted[int((len(densities_sorted)-1)*.9)], 3) if densities_sorted else None,
            'max': round(densities_sorted[-1], 3) if densities_sorted else None,
            'outside_950_1050_count': sum(1 for d in density_px_per_m if abs(d-1000.)>50.),
            'samples': density_anomalies,
            'warning': 'Nominal resolution and triangle-area estimate; not UV affine/tile correctness proof',
        },
        'invalid_uv_coordinates': invalid_uv,
        'triangle_area_sum_local_m2': round(face_area, 6),
        'triangle_area_sum_uv2': round(uv_area, 6),
        'nominal_module_size_m': float(tile_length),
        'estimated_global_area_density_px_per_m': round(sqrt(uv_area / face_area) * round(float(tile_length) * 1024), 4) if face_area > 1e-12 and uv_area > 0 else None,
        'density_note': 'Area-weighted rough metric only, NOT a 1000 px/m guarantee. Needs per-face/material/UV-island checks.',
        'module_seams_not_equivalent_to_mesh_edges': True,
    }
    report['horizontal_wall_edges'] = {
        'counts_by_local_z': dict((k, v) for k, v in sorted(horiz.items()) if k != 'total'),
        'total': horiz['total'], 'samples': horiz_near,
        'interpretation': 'A horizontal edge may be a mandatory UV tile, story or geometric seam; classify, do not delete blindly',
    }
    report['possible_redundant_edges'] = {
        'same_role_same_material_near_coplanar_uv_continuous_sample_count': len(candidates),
        'examples': candidates,
        'count_is_sample_only': len(candidates) >= 150,
        'not_a_removal_authorization': True,
        'required_extra_checks': ['UV module ownership', 'story boundaries', 'topological convexity', 'non-planarity', 'authored path point', 'protected seam', 'face identity'],
    }
    report['degenerate_mesh_triangles'] = degenerate_xyz
    report['processing'] = {'triangles_checked': triangles, 'faces_checked': nf, 'edges_checked': ne}
    return report


def height_hypothesis_snapshot(path_obj, mesh_audit, settings):
    """Read-only hypothesis diagnostics, never rebuild the user's wall."""
    data = {'status': 'NO_SOURCE_PATH', 'hypotheses': [
        'H-1: global Z UV anchoring may cause nonlocal wall rows for a local height change',
        'H-2: 2 mm weld may collapse extremely thin rows introduced by UV grid shifts',
        'H-3: CAP↔WALL must share edge indices, not merely identical XYZ locations',
        'H-4: local rising/falling path segments need no full-wall height belt unless geometry/material/UV requires it',
    ]}
    if path_obj is None or getattr(path_obj, 'type', None) != 'MESH':
        return data
    try:
        points = [tuple(float(v) for v in path_obj.matrix_world @ vert.co) for vert in path_obj.data.vertices]
    except (AttributeError, TypeError):
        points = [_xyz(v) for v in path_obj.data.vertices]
        data['coordinate_warning'] = 'World transform unavailable; local XYZ used'
    levels = sorted(set(round(p[2], 7) for p in points))
    data.update(status='MEASURED_SOURCE_PATH', authored_vertex_count=len(points),
                authored_world_xyz=[list(p) for p in points[:120]],
                source_z_unique_levels=levels[:120], source_z_range_m=(max(levels)-min(levels)) if levels else None,
                observed_wall_horizontal_levels_local=mesh_audit.get('horizontal_wall_edges', {}).get('counts_by_local_z', {}),
                story_direction=settings.get('story_direction'),
                uv_origin_mode=settings.get('wall_uv_origin_mode'),
                module_standard=settings.get('module_standard'))
    if len(points)>1:
        try:
            edges = [tuple(int(i) for i in edge.vertices) for edge in path_obj.data.edges]
        except (AttributeError, TypeError):
            edges = [(i, i+1) for i in range(len(points)-1)]
            data['edges_warning'] = 'Mesh edges unavailable; consecutive vertex order assumed'
        data['source_edge_count'] = len(edges)
        data['authored_source_edges'] = [list(edge) for edge in edges[:220]]
        graph = defaultdict(set)
        for a,b in edges:
            if 0 <= a < len(points) and 0 <= b < len(points) and a != b:
                graph[a].add(b);graph[b].add(a)
        degrees = Counter(len(graph.get(i, set())) for i in range(len(points)))
        data['source_graph_degree_counts'] = dict(degrees)
        data['source_graph_closed'] = bool(len(points)>2 and all(len(graph.get(i,()))==2 for i in range(len(points))))
        # Rebuild path order by true mesh edge connectivity, never by index sequence.
        endpoints = [i for i in range(len(points)) if len(graph.get(i, ())) == 1]
        usable = len(graph)==len(points) and all(len(graph.get(i,())) in (1,2) for i in range(len(points))) and len(endpoints) in (0,2)
        if usable:
            start = min(endpoints) if endpoints else min(graph)
            order=[start]; visited={start}; prev=None;cur=start
            for _ in range(len(points)-1):
                nxt = sorted(j for j in graph[cur] if j != prev and j not in visited)
                if not nxt:break
                prev,cur=cur,nxt[0]
                order.append(cur);visited.add(cur)
            if len(order)!=len(points): usable=False
        if not usable:
            data['source_graph_status'] = 'INVALID_OR_AMBIGUOUS; segments reported by stored edge direction'
            ordered_edges=edges
        else:
            data['source_graph_status'] = 'ORDER_RECONSTRUCTED_FROM_EDGES'
            data['inferred_vertex_order'] = order[:220]
            ordered_edges=list(zip(order,order[1:]))
            if data['source_graph_closed']:ordered_edges.append((order[-1],order[0]))
        data['segment_direction_note'] = 'ASCENDING/DESCENDING relative to inferred traversal; winding/reverse path may invert labels'
        data['segments'] = []
        for i,(a,b) in enumerate(ordered_edges[:220]):
            if not (0 <= a < len(points) and 0 <= b < len(points)):
                continue
            z=points[b][2]-points[a][2]
            data['segments'].append({
                'edge_index': i, 'source_vertex_a': a, 'source_vertex_b': b,
                'delta_z_m': round(z,8),
                'horizontal_length_m': round(hypot(points[b][0]-points[a][0], points[b][1]-points[a][1]),7),
                'classification': ('ASCENDING' if z>1e-7 else 'DESCENDING' if z<-1e-7 else 'FLAT'),
            })
    return data
