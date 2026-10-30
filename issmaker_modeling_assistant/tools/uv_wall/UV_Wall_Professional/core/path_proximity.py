"""Conservative broad-phase for Path vertex-vs-edge proximity tests.

Pure math. A point inside tolerance of an edge must lie in the 3D expanded
edge bounding box. Sweep along the axis with maximum total spread, and only
pass candidate pairs to the *unchanged* exact distance test in production.
Never change authored points or make an approximate accept/reject decision.
"""
import heapq


def vertex_edge_candidates(coords, indexed_edges, tolerance):
    """Yield (vertex_idx, edge_idx, start_idx, end_idx) in sweep-axis order.

    `coords` are xyz-like Vector objects; edges are (edge_index, a, b).
    Each qualifying geometric point-to-segment pair is guaranteed to appear.
    Worst case may still be quadratic when bounding boxes all overlap, but
    on long thin imported Paths the broad phase is close to linear.
    """
    if not coords or not indexed_edges:
        return
    tolerance=max(0.0,float(tolerance))
    points=[(float(v.x),float(v.y),float(v.z)) for v in coords]
    ranges=[max(p[i] for p in points)-min(p[i] for p in points) for i in range(3)]
    axis=max(range(3),key=lambda i:(ranges[i],-i))
    others=[a for a in range(3) if a!=axis]
    endpoints=[]
    for order,(edge_idx,a,b) in enumerate(indexed_edges):
        pa,pb=points[a],points[b]
        lower=min(pa[axis],pb[axis])-tolerance
        upper=max(pa[axis],pb[axis])+tolerance
        endpoints.append((lower,upper,order,edge_idx,a,b))
    endpoints.sort(key=lambda x:(x[0],x[2]))
    live=set(); expiration=[];cursor=0
    n=len(endpoints)
    for vertex_index in sorted(range(len(points)),key=lambda v:(points[v][axis],v)):
        xyz=points[vertex_index]
        while cursor<n and endpoints[cursor][0]<=xyz[axis]:
            entry=endpoints[cursor];live.add(entry[2]);heapq.heappush(expiration,(entry[1],entry[2]))
            cursor+=1
        while expiration and expiration[0][0]<xyz[axis]:
            _upper,order=heapq.heappop(expiration)
            live.discard(order)
        # Active entries are usually tiny; this preserves deterministic edge
        # order for failures and diagnostics. The exact narrow phase owns truth.
        for order in sorted(live):
            # index into original edge order, never into sorted interval order
            edge_idx,a,b=indexed_edges[order]
            if vertex_index in (a,b):
                continue
            pa,pb=points[a],points[b]
            if all(min(pa[k],pb[k])-tolerance<=xyz[k]<=max(pa[k],pb[k])+tolerance for k in others):
                yield vertex_index,edge_idx,a,b


def xy_segment_candidate_pairs(points, *, closed=False):
    """Conservative XY AABB sweep for non-adjacent segment intersections.

    Returns source segment index pairs. Only the exact intersection test may
    declare an actual intersection; broad-phase is never a correctness filter
    based on a guessed tolerance.
    """
    n=len(points)-1
    if n<3:return []
    extent_x=max(p.x for p in points)-min(p.x for p in points)
    extent_y=max(p.y for p in points)-min(p.y for p in points)
    axis=0 if extent_x>=extent_y else 1
    entries=[]
    for i in range(n):
        a,b=points[i],points[i+1]
        coords_a=(float(a.x),float(a.y));coords_b=(float(b.x),float(b.y))
        lo=min(coords_a[axis],coords_b[axis]);hi=max(coords_a[axis],coords_b[axis])
        other=1-axis
        low_other=min(coords_a[other],coords_b[other]);high_other=max(coords_a[other],coords_b[other])
        entries.append((lo,hi,low_other,high_other,i))
    entries.sort(key=lambda item:(item[0],item[4]))
    live={};expiration=[];pairs=[]
    for lo,hi,olo,ohi,i in entries:
        while expiration and expiration[0][0]<lo:
            _end,index=heapq.heappop(expiration)
            live.pop(index,None)
        for j,(left,right) in live.items():
            if abs(i-j)<=1 or (closed and {i,j}=={0,n-1}):
                continue
            if max(olo,left)<=min(ohi,right):
                pairs.append((min(i,j),max(i,j)))
        live[i]=(olo,ohi)
        heapq.heappush(expiration,(hi,i))
    # Source order yields stable Problem Navigator order across versions.
    return sorted(pairs)
