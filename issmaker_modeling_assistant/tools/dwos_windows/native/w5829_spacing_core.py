"""W58.29 collision-aware mullion spacing, tested independent of Blender UI."""
import math
CLEARANCE_M=0.010
def min_center_gap(width_a,width_b,clearance=CLEARANCE_M):
    a,b=float(width_a),float(width_b)
    if not all(math.isfinite(x) and x>0 for x in (a,b)):raise ValueError('POST_WIDTH_INVALID')
    if not math.isfinite(clearance) or clearance<0:raise ValueError('CLEARANCE_INVALID')
    return .5*(a+b)+clearance
def assert_center_allowed(candidate,width,existing,*,clearance=CLEARANCE_M):
    if not math.isfinite(candidate):raise ValueError('POST_CENTER_NONFINITE')
    for row in existing:
        pos=float(row['center'])
        other=float(row['width'])
        minimum=min_center_gap(width,other,clearance)
        if abs(candidate-pos)<minimum-1e-7:
            raise ValueError('MULLION_TOO_CLOSE: %.4f m < %.4f m required'%(abs(candidate-pos),minimum))
    return True
def assert_frame_allowed(center,width,left,right,frame_width,*,clearance=CLEARANCE_M):
    if center-width*.5<left+frame_width+clearance-1e-8:
        raise ValueError('MULLION_TOO_CLOSE_TO_FRAME_START')
    if center+width*.5>right-frame_width-clearance+1e-8:
        raise ValueError('MULLION_TOO_CLOSE_TO_FRAME_END')
    return True
