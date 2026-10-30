"""Read-only, fail-closed topology contract for Brick Hole FILLED regions.

Verifies welded cap adjacency, expected boundary reveal attachments, connectivity
of UV-split reveals and coincident distinct vertices. It does not modify BMesh.
The caller runs it inside the protected working-Mesh transaction before commit.
"""


def validate_filled_result_topology(bm, result_layer, first_new_region_id,
                                    cap_faces, expected_caps, expected_boundary_edges,
                                    expected_internal_edges, statistics=None,
                                    tolerance=1e-6):
    """Validate one connected selected FACE region after native extrusion + UV splits."""
    stats = {
        'validation_state': 'UNCERTAIN',
        'caps': 0, 'new_reveal_faces': 0,
        'cap_shared_edges': 0, 'cap_boundary_edges': 0,
        'cap_boundary_reveal_links': 0,
        'reachable_result_faces': 0,
        'coincident_unwelded_vertices': 0,
    }
    def fail(reason):
        stats['validation_state'] = 'PROVEN_INVALID'
        raise ValueError('TOPOLOGY: FILLED: ' + reason)

    try:
        caps = {f for f in cap_faces if getattr(f, 'is_valid', False)}
        stats['caps'] = len(caps)
        if expected_caps < 1 or len(caps) != expected_caps:
            fail(f'число крышек {len(caps)}, ожидалось {expected_caps}')
        if result_layer is None:
            fail('не найдены метки новых откосов')
        reveal = {f for f in bm.faces
                  if f not in caps and f[result_layer] > 0 and
                  f[result_layer] >= first_new_region_id}
        stats['new_reveal_faces'] = len(reveal)
        if not reveal:
            fail('не созданы новые откосы')
        # For one connected input region the cap graph must be connected
        # through ACTUAL shared BMEdges, not touching XYZ coordinates.
        inner_edges = set()
        outer_edges = set()
        for face in caps:
            for edge in face.edges:
                links = [f for f in edge.link_faces if f in caps]
                if len(links) == 2:
                    inner_edges.add(edge)
                elif len(links) == 1:
                    outer_edges.add(edge)
                else:
                    fail('недопустимая кратность общей границы крышек')
        stats['cap_shared_edges'] = len(inner_edges)
        stats['cap_boundary_edges'] = len(outer_edges)
        if len(inner_edges) != expected_internal_edges:
            fail(f'разорвано общее ребро полигонов: {len(inner_edges)} '
                 f'вместо {expected_internal_edges}')
        if len(outer_edges) != expected_boundary_edges:
            fail(f'граница экструдированной области изменилась: '
                 f'{len(outer_edges)} вместо {expected_boundary_edges}')
        connected = {next(iter(caps))}
        queue = list(connected)
        for face in queue:
            for edge in face.edges:
                for other in edge.link_faces:
                    if other in caps and other not in connected:
                        connected.add(other)
                        queue.append(other)
        if len(connected) != len(caps):
            fail('несколько крышек не сварены в одну связанную область')
        # Every cap-boundary edge must attach to exactly one NEW side face.
        # Other unrelated faces attached along the same edge are not accepted.
        for edge in outer_edges:
            sides = [f for f in edge.link_faces if f in reveal]
            if len(sides) != 1 or len(edge.link_faces) != 2:
                fail(f'нет единственного откоса у ребра крышки '
                     f'(откосов {len(sides)}, смежных граней {len(edge.link_faces)})')
            stats['cap_boundary_reveal_links'] += 1
        # UV bisects can split a side into several valid pieces; every piece
        # must remain connected to the cap over real BMEdges.
        touched = caps.copy()
        frontier = list(caps)
        result_faces = caps | reveal
        for face in frontier:
            for edge in face.edges:
                for other in edge.link_faces:
                    if other in result_faces and other not in touched:
                        touched.add(other)
                        frontier.append(other)
        stats['reachable_result_faces'] = len(touched)
        if touched != result_faces:
            fail(f'найдено {len(result_faces-touched)} отсоединённых '
                 'откосов/UV-фрагментов')
        # Coincident-but-distinct BMVerts are a classic sign of separately
        # extruded neighbouring faces. Check only vertices in THIS result,
        # not pre-existing authored duplicate vertices elsewhere in the mesh.
        unique_verts = {vert for face in result_faces for vert in face.verts}
        grid = {}
        for vert in unique_verts:
            key = tuple(round(float(c)/tolerance) for c in vert.co)
            for x in (-1, 0, 1):
                for y in (-1, 0, 1):
                    for z in (-1, 0, 1):
                        for other in grid.get((key[0]+x,key[1]+y,key[2]+z), ()):
                            if (vert.co-other.co).length <= tolerance:
                                stats['coincident_unwelded_vertices'] += 1
                                fail('дублирующиеся несваренные вершины '
                                     'с одинаковыми координатами')
            grid.setdefault(key, []).append(vert)
        stats['validation_state'] = 'VALID'
        return stats
    finally:
        if statistics is not None:
            statistics.update(stats)
