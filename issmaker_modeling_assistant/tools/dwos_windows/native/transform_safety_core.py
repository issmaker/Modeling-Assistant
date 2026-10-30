"""Pure host-transform contract for DWOS 3.5.3.

Opening detection currently reads base-mesh coordinates.  Until a full evaluated
world-metric detection pipeline lands, only rigid positive-determinant host
transforms are safe.  This module makes that limitation explicit and testable.
"""
from __future__ import annotations
import math


def _dot(a,b):return sum(float(x)*float(y) for x,y in zip(a,b))
def _length(a):return math.sqrt(max(0.0,_dot(a,a)))


def _det3(r):
    return (r[0][0]*(r[1][1]*r[2][2]-r[1][2]*r[2][1])
           -r[0][1]*(r[1][0]*r[2][2]-r[1][2]*r[2][0])
           +r[0][2]*(r[1][0]*r[2][1]-r[1][1]*r[2][0]))


def analyze_linear_transform(rows,tolerance=1.0e-5):
    r=[[float(rows[i][j]) for j in range(3)] for i in range(3)]
    cols=[(r[0][i],r[1][i],r[2][i]) for i in range(3)]
    scales=tuple(_length(c) for c in cols)
    reasons=[]
    if any(s<=1.0e-12 for s in scales):
        reasons.append('SINGULAR_TRANSFORM')
        return {'rigid':False,'reasons':reasons,'scales':scales,'determinant':_det3(r),'orthogonality_error':1.0}
    unit=[tuple(x/s for x in c) for c,s in zip(cols,scales)]
    orth=max(abs(_dot(unit[i],unit[j])) for i in range(3) for j in range(i+1,3))
    det=_det3(r)
    if any(abs(s-1.0)>tolerance for s in scales):reasons.append('NON_UNIT_SCALE')
    if orth>tolerance:reasons.append('SHEAR_OR_NON_ORTHOGONAL')
    if det<=0.0:reasons.append('NEGATIVE_OR_REFLECTED')
    # A positive orthonormal matrix must have determinant +1 (within tolerance).
    if det>0.0 and abs(det-1.0)>max(tolerance*4.0,1.0e-6) and 'NON_UNIT_SCALE' not in reasons:
        reasons.append('NON_RIGID_DETERMINANT')
    return {'rigid':not reasons,'reasons':reasons,'scales':scales,'determinant':det,'orthogonality_error':orth}
