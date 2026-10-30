from __future__ import annotations

def side_targets(width,height,x_values,y_values):
    hx=width*0.5; hy=height*0.5
    return {
        'bottom':[(x,-hy) for x in x_values],
        'top':[(x,hy) for x in x_values],
        'left':[(-hx,y) for y in y_values],
        'right':[(hx,y) for y in y_values],
    }

def classify_side(x,y,width,height,tol=1e-5):
    hx=width*0.5; hy=height*0.5
    if abs(y+hy)<=tol and -hx-tol<=x<=hx+tol:return 'bottom'
    if abs(y-hy)<=tol and -hx-tol<=x<=hx+tol:return 'top'
    if abs(x+hx)<=tol and -hy-tol<=y<=hy+tol:return 'left'
    if abs(x-hx)<=tol and -hy-tol<=y<=hy+tol:return 'right'
    return None

def validate_face_counts(edge_face_counts, allow_boundary=True):
    for n in edge_face_counts:
        if n>2:return False
        if not allow_boundary and n!=2:return False
    return True
