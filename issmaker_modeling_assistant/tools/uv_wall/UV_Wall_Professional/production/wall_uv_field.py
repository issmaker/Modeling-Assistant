"""Authoritative Blender-free metric U field for UV Wall facade strips.

A facade polygon may cover multiple *provably straight, forward and contiguous*
authored segments. Bent, overlapping and backward paths remain fail-closed.
Primary and secondary facades own separate XY fields; no authored coordinates
or original Mesh positions are changed. Designed for independent offline tests.
"""
from math import ceil, floor, hypot, isfinite
from bisect import bisect_left, bisect_right


def signed_loop_area(u, v):
    if len(u) != len(v) or len(u) < 3 or not all(isfinite(float(a)) and isfinite(float(b)) for a, b in zip(u, v)):
        raise ValueError('WALL_U_PARITY: invalid face loop')
    return 0.5 * sum(float(a)*float(v[(i+1) % len(v)]) -
                     float(u[(i+1) % len(u)])*float(b)
                     for i,(a,b) in enumerate(zip(u,v)))


def expected_facade_sign(stations, world_heights, *, face_index=-1, area_epsilon=1e-10):
    """Expected UV sign under U=physical arclength, V=increasing world Z.

    Uses no existing UV coordinates and no winding assumptions. Independent
    of mesh normal policy or face triangulation, which may reverse loop order.
    A degenerate face must not bypass validation.
    """
    area=signed_loop_area(stations, world_heights)
    if abs(area) <= area_epsilon:
        raise ValueError(f'WALL_U_PARITY: degenerate metric face {face_index}; area={area:.12g}')
    return area > 0.0


class UVFieldError(ValueError):
    """Unresolvable face provenance: never replace stable Mesh on this error."""


class FacadeField:
    def __init__(self, points, tile_length, *, clearance=0.002,
                 max_index_samples=1_000_000):
        self.tile_length = float(tile_length)
        self.clearance = float(clearance)
        if not isfinite(self.tile_length) or self.tile_length <= 0:
            raise UVFieldError('WALL_U_FIELD: invalid module length')
        if not isfinite(self.clearance) or not 0 <= self.clearance < self.tile_length / 4:
            raise UVFieldError('WALL_U_FIELD: invalid seam clearance')
        self.cell = self.tile_length
        self.points = tuple((float(p[0]), float(p[1])) if hasattr(p, '__getitem__')
                            else (float(p.x), float(p.y)) for p in points)
        if len(self.points) < 2 or not all(isfinite(a) and isfinite(b)
                                           for a, b in self.points):
            raise UVFieldError('WALL_U_FIELD: bad path coordinates')
        self.lengths = [0.0]
        self.segments = []
        self.grid = {}
        self._neighbor_cache = {}  # bounded per-build repeated face-cell lookups
        samples = 0
        for i, ((x0,y0),(x1,y1)) in enumerate(zip(self.points,self.points[1:])):
            dx,dy = x1-x0,y1-y0
            length = hypot(dx,dy)
            if length <= 1e-9 or not isfinite(length):
                raise UVFieldError(f'WALL_U_FIELD: degenerate segment {i}')
            self.segments.append((x0,y0,dx,dy,length*length,self.lengths[-1],length))
            self.lengths.append(self.lengths[-1]+length)
            # Small sampled spatial grid, O(path length / module), not
            # O(faces * authored points). Samples <= half a grid cell apart,
            # then a 3x3 query guarantees candidates near any on-segment face.
            n=max(1,int(ceil(length/(self.cell*0.5))))
            samples += n + 1
            if samples > max_index_samples:
                raise UVFieldError('WALL_U_FIELD: spatial index budget exceeded; '
                                   'split the job or increase available budget')
            for j in range(n+1):
                t=j/n
                key=(floor((x0+dx*t)/self.cell),floor((y0+dy*t)/self.cell))
                self.grid.setdefault(key,set()).add(i)
        # Group authored segments into provably straight forward runs, without
        # using the FIRST (possibly very short) segment as an infinite ray.
        # A 0.37 m float32 segment can have microradian direction quantisation:
        # extrapolating that ray for 8 m wrongly declares a straight wall bent.
        # First preserve every local authored corner/reversal, then prove each
        # candidate run against its full endpoint chord.  This has linear cost
        # for straight paths, and only subdivides where actual drift exceeds
        # the same 10 micron protection threshold used by wall_topology.
        self.runs = []
        self._segment_to_run = [0] * len(self.segments)
        _straight_tolerance = 1.e-5

        def _lateral(a, b, c):
            dx = c[0] - a[0]
            dy = c[1] - a[1]
            span = hypot(dx, dy)
            if span <= 1.e-12:
                return float('inf')
            return abs((b[0]-a[0])*dy - (b[1]-a[1])*dx) / span

        # Section end indices are *point* indices, not segment indices.
        # Preserve genuine angles even when a very long near-straight region
        # contains large numbers of subdivided authored vertices.
        sections = []
        start = 0
        for point_index in range(1, len(self.points) - 1):
            a, b, c = (self.points[point_index-1],
                       self.points[point_index],
                       self.points[point_index+1])
            ux, uy = b[0]-a[0], b[1]-a[1]
            vx, vy = c[0]-b[0], c[1]-b[1]
            reversal = ux*vx + uy*vy <= 0
            if reversal or _lateral(a, b, c) > _straight_tolerance:
                sections.append((start, point_index))
                start = point_index
        sections.append((start, len(self.points) - 1))

        verified = []
        for first, last in sections:
            pending = [(first, last)]
            while pending:
                left, right = pending.pop()
                if right == left + 1:
                    verified.append((left, right))
                    continue
                a, c = self.points[left], self.points[right]
                dx, dy = c[0]-a[0], c[1]-a[1]
                span = hypot(dx, dy)
                if span <= 1.e-12:
                    # A folded chain cannot be approximated by a straight run.
                    # Splitting keeps the original geometry and fail-closed
                    # per-face provenance semantics intact.
                    cut = (left + right) // 2
                    pending.extend(((cut, right), (left, cut)))
                    continue
                inv_span = 1.0 / span
                max_drift = 0.0
                split_at = None
                for index in range(left + 1, right):
                    px, py = self.points[index]
                    dist = abs((px-a[0])*dy-(py-a[1])*dx)*inv_span
                    if dist > max_drift:
                        max_drift = dist
                        split_at = index
                # Protect also against a nonforward zigzag that happens to
                # remain inside the thin line tolerance corridor.
                forward = True
                for index in range(left, right):
                    p, q = self.points[index], self.points[index+1]
                    if (q[0]-p[0])*dx+(q[1]-p[1])*dy <= 0:
                        forward = False
                        split_at = index + 1 if index + 1 < right else index
                        break
                if max_drift > _straight_tolerance or not forward:
                    if split_at is None or split_at <= left or split_at >= right:
                        raise UVFieldError('WALL_U_FIELD: cannot resolve nonforward run')
                    pending.extend(((split_at, right), (left, split_at)))
                else:
                    verified.append((left, right))

        for first, last in sorted(verified):
            x0, y0 = self.points[first]
            xe, ye = self.points[last]
            dx, dy = xe-x0, ye-y0
            length2 = dx*dx+dy*dy
            if length2 <= 1.e-18 or not isfinite(length2):
                raise UVFieldError('WALL_U_FIELD: invalid proven straight run')
            idx = len(self.runs)
            self.runs.append((x0, y0, dx, dy, length2,
                              self.lengths[first],
                              self.lengths[last]-self.lengths[first],
                              first, last-1))
            for segment_index in range(first, last):
                self._segment_to_run[segment_index] = idx

    def stationed_face(self, vertices, *, face_index=-1, tolerance=0.0025):
        """Map a face onto a single *straight run*, not necessarily one segment.

        A run can span authored collinear subdivisions with strictly forward
        motion and sub-10-micron drift only. Never interpolate over a real
        authored corner or select a neighboring loop based on centroid alone.
        """
        xy = tuple((float(p[0]),float(p[1])) if hasattr(p,'__getitem__')
                   else (float(p.x),float(p.y)) for p in vertices)
        if len(xy)<3 or not all(isfinite(x) and isfinite(y) for x,y in xy):
            raise UVFieldError(f'WALL_U_FIELD: face {face_index} has invalid coordinates')
        cx=sum(x for x,_ in xy)/len(xy)
        cy=sum(y for _,y in xy)/len(xy)
        key=(floor(cx/self.cell),floor(cy/self.cell))
        candidates=self._neighbor_cache.get(key)
        if candidates is None:
            found=set()
            for ox in (-1,0,1):
                for oy in (-1,0,1):
                    found.update(self.grid.get((key[0]+ox,key[1]+oy),()))
            candidates=tuple(sorted(found))
            if len(self._neighbor_cache)<4096:
                self._neighbor_cache[key]=candidates
        matches=[]
        for run_index in sorted({self._segment_to_run[i] for i in candidates}):
            x0,y0,dx,dy,length2,s0,length,first,last=self.runs[run_index]
            stations=[];max_perp=0.;max_extension=0.
            for x,y in xy:
                t=((x-x0)*dx+(y-y0)*dy)/length2
                projection_x=x0+dx*t
                projection_y=y0+dy*t
                perp=hypot(x-projection_x,y-projection_y)
                extension=max(0.,-t*length,(t-1)*length)
                max_perp=max(max_perp,perp)
                max_extension=max(max_extension,extension)
                stations.append(s0+max(0.,min(1.,t))*length)
            if max_perp <= tolerance and max_extension <= tolerance:
                matches.append((max(max_perp,max_extension),first,tuple(stations),last))
        if not matches:
            # Diagnostic data are intentionally bounded and carry BOTH source
            # segment candidates and face XY vertices for independent replay.
            raise UVFieldError(
                f'WALL_U_FIELD: face {face_index}: no proven straight path run; '
                f'centroid=({cx:.6f},{cy:.6f}); '
                f'candidate_segments={candidates[:16]}; '
                f'face_xy={tuple((round(x,6),round(y,6)) for x,y in xy[:16])}; '
                'real authored corner, module cut or off-path geometry')
        matches.sort(key=lambda m:(m[0],m[1]))
        best=matches[0]
        # Multiple geometrically indistinguishable contours may have DIFFERENT
        # metric positions. Do not accidentally take the U from another lap.
        for alt in matches[1:]:
            if alt[0] > best[0]+1e-8:
                break
            if max(abs(a-b) for a,b in zip(best[2],alt[2])) > max(0.0001,tolerance):
                raise UVFieldError(f'WALL_U_FIELD: face {face_index}: ambiguous '
                                   f'source segments {best[1]}, {alt[1]}')
        stations=best[2]
        mid=0.5*(min(stations)+max(stations))
        origin=floor(mid/self.tile_length)*self.tile_length
        vals=[(s-origin)/self.tile_length for s in stations]
        deviation=max([0.]+[-v*self.tile_length for v in vals]
                      +[(v-1.)*self.tile_length for v in vals])
        if deviation > self.clearance + 1e-8:
            raise UVFieldError(f'WALL_U_FIELD: face {face_index} crosses module '
                               f'boundary; segment={best[1]}..{best[3]}; interval='
                               f'{min(stations):.6f}..{max(stations):.6f}m; '
                               f'excess={deviation:.6f}m')
        # Correction only to near-module corners, with an explicit bound.
        # A module boundary snapped onto an authored corner represents the
        # SAME physical seam on either side. Without this 1->0 snap, the two
        # modules sample 1 mm apart (even with correct global stationing).
        # Tiny sub-clearance facets are not silently collapsed to zero UV area.
        physical_span=max(stations)-min(stations)
        if physical_span <= self.clearance and any(
                abs(s-round(s/self.tile_length)*self.tile_length) <= self.clearance
                for s in stations):
            raise UVFieldError(f'WALL_U_FIELD: face {face_index} too narrow '
                               'near module seam for safe metric UV')
        mapped=[]
        for station,value in zip(stations,vals):
            v=max(0.,min(1.,value))
            if abs(station-origin) <= self.clearance+1e-10:
                v=0.
            elif abs(station-(origin+self.tile_length)) <= self.clearance+1e-10:
                v=1.
            mapped.append(v)
        vals=tuple(mapped)
        smin,smax=min(stations),max(stations)
        # The run can contain thousands of authored subdivisions; report only
        # the segments ACTUALLY covered by this face, in O(log n), and never
        # allocate a huge segment tuple inside the per-face UV hot loop.
        first_seg=max(best[1],min(best[3],bisect_right(self.lengths,smin+1.e-9)-1))
        last_seg=max(first_seg,min(best[3],bisect_left(self.lengths,smax-1.e-9)-1))
        count=last_seg-first_seg+1
        return vals, {"face":face_index,"segment":first_seg,
                      "segment_last":last_seg,
                      "source_segment_count":count,
                      "source_segments":(tuple(range(first_seg,last_seg+1))
                                         if count<=32 else None),
                      "station_min":smin,"station_max":smax,
                      "module_origin":origin,"near_cut_correction_m":deviation}


def rephase_uv_for_facade(mesh, outer_points, inner_points, tile_length,
                          source, role_codes, role_constants, *, audit_limit=8,
                          expected_winding_by_face=None, positive_winding=False):
    """Rephase only semantic WALL facades; call *after* topology and UV flips.

    No changes to geometry, V, caps, endcaps, other UV layers or materials. All
    projection operations finish in a pure preparation phase before changing a
    single UV loop. Exceptions leave the mesh UV data exactly as supplied.
    """
    layer=mesh.uv_layers.active
    if layer is None:
        raise UVFieldError('WALL_U_FIELD: active UV layer missing')
    fields={role_constants['WALL_OUTER']:FacadeField(outer_points,tile_length)}
    if inner_points is not None:
        fields[role_constants['WALL_INNER']]=FacadeField(inner_points,tile_length)
    safe_min=float(source['bottom_left_uv'].x)
    safe_max=float(source['bottom_right_uv'].x)
    span=safe_max-safe_min
    if not 0.<span<2.:
        raise UVFieldError('WALL_U_FIELD: invalid source U interval')
    changes=[];counts=0;max_delta=0.;snapped=0;examples=[]
    predicted_by_vertex={}; shared_checks=0
    # Compute expected UV orientation from *physical* (arclength, Z), not
    # from already mirrored UV islands. Commit the map only after all faces
    # have passed. This is crucial for single WALL PATH_LEFT and reverse
    # directions: a negative signed UV area can be geometrically correct.
    expected_winding_local={}
    positive_facades=negative_facades=0
    for poly in mesh.polygons:
        role=role_codes[poly.index]
        field=fields.get(role)
        if field is None:
            continue
        indices=list(poly.loop_indices)
        xyz=[mesh.vertices[mesh.loops[i].vertex_index].co for i in indices]
        values,info=field.stationed_face(xyz,face_index=poly.index)
        expected_sign=expected_facade_sign(values, [float(v[2]) for v in xyz],
                                           face_index=poly.index)
        physical_sign = expected_sign
        # Choose the U direction from the final outward-facing loop order.
        # Reflect the entire module interval, never an island bounding box:
        # adjacent fragments keep the same physical phase and texel density.
        if positive_winding and not expected_sign:
            values = tuple(1.0-u for u in values)
            expected_sign = True
        expected_winding_local[poly.index]=expected_sign
        if expected_sign:
            positive_facades+=1
        else:
            negative_facades+=1
        counts+=1
        if info['near_cut_correction_m'] > 1e-8:snapped+=1
        for loop_idx,u in zip(indices,values):
            expected=safe_min+span*u
            vertex_id=int(mesh.loops[loop_idx].vertex_index)
            # Opposite-facing surfaces can meet at a vertex of a corner.
            # They need opposite U directions to both remain unmirrored.
            key=(role,vertex_id, physical_sign)
            other=predicted_by_vertex.get(key)
            if other is not None:
                shared_checks+=1
                gap=abs(expected-other)
                # Closed contour reuses the physical start vertex after a
                # full perimeter. Its one permitted nonperiodic seam is there.
                if min(gap,abs(gap-span))>5e-6:
                    # Correct UV is overwhelmingly continuous; only evaluate
                    # exceptional loop-start geometry for an actual mismatch.
                    co=mesh.vertices[vertex_id].co
                    is_closed=(hypot(field.points[0][0]-field.points[-1][0],
                                     field.points[0][1]-field.points[-1][1])<1e-7)
                    at_start=(hypot(float(co[0])-field.points[0][0],
                                    float(co[1])-field.points[0][1])<1e-5)
                    if not (is_closed and at_start):
                        raise UVFieldError(f'WALL_U_FIELD: nonperiodic internal UV '
                                       f'seam at face {poly.index}, vertex {vertex_id}; '
                                       f'gap={gap:.8f} UV')
            else:
                predicted_by_vertex[key]=expected
            actual=float(layer.data[loop_idx].uv.x)
            diff=abs(expected-actual)
            max_delta=max(max_delta,diff)
            if diff>5e-7:
                changes.append((loop_idx,expected))
                if len(examples)<audit_limit:
                    examples.append((poly.index,info['segment'],round(diff,7),
                                     round(info['station_min'],6)))
    # Commit after *all* facade faces passed: no partially corrected result.
    for idx,u in changes:
        layer.data[idx].uv.x=u
    if expected_winding_by_face is not None:
        expected_winding_by_face.update(expected_winding_local)
    return {'facade_faces':counts,'corrected_loops':len(changes),
            'expected_positive_facades':positive_facades,
            'expected_negative_facades':negative_facades,
            'max_source_u_delta':max_delta,'snapped_faces':snapped,
            'shared_vertex_checks':shared_checks,
            'examples':examples,'policy':'ABSOLUTE_ARCLENGTH_AFTER_TOPOLOGY'}
