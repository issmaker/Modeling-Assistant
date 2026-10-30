"""Five curated non-crosshatching premium patterns for ISSMAKER panels.

Pure-python vector generator. No Blender dependencies or GPU resources. Each family
is compositionally independent: closed topo contours, laminar flow, mineral
strata, faceted prisms and atmospheric aurora ribbons. Geometry is conservative
so dense overdraw and alpha accumulation cannot overwhelm panel typography.
"""
from __future__ import annotations
from math import sin,cos,pi,isfinite

FAMILIES=('contours','flow','strata','prism','aurora')

def _clip(x0,y0,x1,y1,w,h):
    dx=x1-x0;dy=y1-y0;low=0.;high=1.
    for p,q in ((-dx,x0),(dx,w-x0),(-dy,y0),(dy,h-y0)):
        if abs(p)<1e-12:
            if q<0:return None
            continue
        t=q/p
        if p<0:low=max(low,t)
        else:high=min(high,t)
        if low>high:return None
    return (min(w,max(0.,x0+dx*low)),min(h,max(0.,y0+dy*low)),
            min(w,max(0.,x0+dx*high)),min(h,max(0.,y0+dy*high)))

def generate(family,w,h,phase,density,detail=1.):
    """Return bounded (x0,y0,x1,y1,alpha). Never add geometry for UI hitboxes."""
    if family not in FAMILIES:raise ValueError('Unsupported premium family')
    w=float(w);h=float(h);phase=float(phase);density=float(density);detail=float(detail)
    if not all(isfinite(v) for v in (w,h,phase,density,detail)) or w<2 or h<2 or w>8192 or h>8192:raise ValueError('Invalid dimensions')
    if density<.25 or density>2.5 or detail<.5 or detail>3:raise ValueError('Invalid detail')
    result=[]
    def line(a,b,c,d,alpha):
        cut=_clip(a,b,c,d,w,h)
        if cut is not None and abs(cut[0]-cut[2])+abs(cut[1]-cut[3])>.08:
            result.append((*cut,max(.01,min(.5,alpha))))
    def curve(fn,steps,alpha):
        count=min(150,max(12,int(steps*min(2.1,detail))))
        prev=fn(0.)
        for k in range(1,count+1):
            pt=fn(k/count);line(*prev,*pt,alpha);prev=pt
    # The wave envelope for each family remains at least 3x smaller than the
    # distance between main paths; unlike legacy drawing, lines don't cross.
    if family=='contours':
        rings=max(5,min(11,int(7*density)))
        for i in range(rings):
            spread=.18+i*.09
            cx=w*(.65+.006*sin(phase*.2));cy=h*(.48+.008*cos(phase*.16))
            rx=w*spread;ry=h*spread*.86
            def ring(t,i=i,rx=rx,ry=ry):
                a=t*2*pi
                r=1.+.030*sin(3*a+phase*.12+i*.25)+.015*sin(7*a+i*.41)
                return cx+cos(a)*rx*r,cy+sin(a)*ry*r
            curve(ring,52,.085+.010*(i%3))
    elif family=='flow':
        n=max(5,min(13,int(8*density)))
        for i in range(n):
            base=(i+.50)*h/n
            def flow(t,i=i,base=base):
                px=w*t
                wave=(.012*h*sin(t*2*pi*1.15+phase*.33+i*.20)+
                      .006*h*sin(t*2*pi*.50-phase*.14+i*.31))
                return px,base+wave
            curve(flow,32,.08+.012*(i%4))
    elif family=='strata':
        n=max(6,min(17,int(12*density)))
        for i in range(n):
            base=(i+.55)*h/n
            def layer(t,i=i,base=base):
                return w*t,base+.006*h*sin(t*pi*4+phase*.14+i*.20)+.002*h*sin(t*pi*11+i*.32)
            curve(layer,33,.075+.011*(i%5))
    elif family=='prism':
        # Coherent low-poly mountain facets; no tiny zig-zag triangles.
        t=phase*.12
        for i in range(3):
            x=w*(.14+i*.34)+w*.007*sin(t+i)
            apex=(x+w*.15,h*(.38+.1*(i%2)))
            left=(x-w*.11,h*.10);right=(x+w*.36,h*.12)
            foot=(x+w*.08,h*.72)
            for a,b in ((left,apex),(apex,right),(right,foot),(foot,left),(apex,foot)):
                line(*a,*b,.10 if a==apex else .07)
        line(0,h*.22,w,h*.22,.065)
    elif family=='aurora':
        n=max(5,min(12,int(8*density)))
        for i in range(n):
            base=(i+.5)*w/n
            def ribbon(t,i=i,base=base):
                yy=h*t
                bend=w*.032*sin(t*pi*1.5+phase*.25+i*.24)+w*.010*sin(t*pi*3+phase*.11)
                return base+bend,yy
            curve(ribbon,38,.052+.008*(i%3))
    return tuple(result)
