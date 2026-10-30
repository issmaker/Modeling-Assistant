"""Blender-independent procedural panel patterns, ISSMAKER Pass 11B.

All 40 patterns are mathematically generated, deterministic, bounded to their
sample area, and intentionally subdued behind UI text. Lines: x0,y0,x1,y1,a.
No bpy/gpu dependencies: the same engine feeds gallery and live panel.
"""
from __future__ import annotations
from functools import lru_cache
from math import sin, cos, pi, exp, hypot, floor, sqrt, atan2, isfinite

PATTERN_CATALOG = (
    ('contours','Топография'), ('flow','Поток'), ('strata','Страты'),
    ('prism','Призма'), ('aurora','Аврора'), ('particles','Частицы'),
    ('isobars','Изобары'), ('grid','Сетка'), ('terraces','Террасы'),
    ('lattice','Решётка'), ('halo','Ореол'), ('caustics','Каустика'),
    ('facets','Грани'), ('survey','Геодезия'), ('ridges','Гребни'),
    ('nodes','Узлы'), ('dunes','Дюны'), ('waves','Волны'),
    ('pulse','Пульс'), ('circuit','Схема'), ('nebula','Туманность'),
    ('veins','Прожилки'), ('fiber','Волокна'), ('marble_flow','Мрамор'),
    ('glacier','Ледник'), ('magnetic_field','Магнитное поле'),
    ('ripple_lines','Рябь'), ('fractal_noise','Фрактал'),
    ('hex_weave','Шестиугольники'), ('crystal_mesh','Кристаллы'),
    ('liquid_metal','Жидкий металл'), ('sand_drift','Песчаный ветер'),
    ('rain_map','Дождь'), ('thermal_scan','Термокарта'),
    ('cloud_bands','Облака'), ('topo_pulse','Топопульс'),
    ('vector_storm','Векторный шторм'), ('soft_plasma','Плазма'),
    ('blueprint_field','Чертёжное поле'), ('strata_mineral','Минеральные слои'),
)
LEGACY_CATALOG = PATTERN_CATALOG
FEATURED_IDS = ('contours','flow','strata','prism','aurora')
PATTERN_CATALOG = tuple(item for item in LEGACY_CATALOG if item[0] in FEATURED_IDS)
IDS = tuple(x[0] for x in LEGACY_CATALOG)  # archived generators kept for migration
NAMES = dict(LEGACY_CATALOG)
VISIBLE_IDS = FEATURED_IDS + ('none',)
ALL = IDS + ('none',)  # accepted legacy IDs only; not exposed by current UI



def _rand(i, j=0):
    a = sin(i * 78.233 + j * 11.713 + 5.2) * 43758.5453
    return a - floor(a)


def _clip(x0,y0,x1,y1,w,h):
    dx=x1-x0; dy=y1-y0; t0=0.; t1=1.
    for p,q in ((-dx,x0),(dx,w-x0),(-dy,y0),(dy,h-y0)):
        if abs(p)<1.e-10:
            if q<0:return None
            continue
        r=q/p
        if p<0:t0=max(t0,r)
        else:t1=min(t1,r)
        if t0>t1:return None
    return x0+dx*t0,y0+dy*t0,x0+dx*t1,y0+dy*t1


def _build(pattern,w,h,phase,density,detail=1.):
    lines=[]
    def line(x0,y0,x1,y1,alpha=.3):
        if len(lines)>=2400:return
        clipped=_clip(x0,y0,x1,y1,w,h)
        if clipped and abs(clipped[0]-clipped[2])+abs(clipped[1]-clipped[3])>.08:
            lines.append((*clipped,max(.015,min(1.,alpha))))
    def path(fn,n,alpha=.3):
        if pattern in FEATURED_IDS:n=max(2,min(256,round(n*detail)))
        last=fn(0.)
        for i in range(1,n+1):
            cur=fn(i/n)
            line(*last,*cur,alpha)
            last=cur
    def circle(cx,cy,rx,ry,alpha=.3,steps=44,warp=0.,shift=0.):
        path(lambda t:(cx+rx*cos(t*2*pi)*(1.+warp*sin(t*12*pi+phase+shift)),
                        cy+ry*sin(t*2*pi)*(1.+warp*sin(t*12*pi+phase+shift))),steps,alpha)
    def stream(idx,steps=24,amp=13.,freq=.018,alpha=.32,phase_mul=1.):
        by=(idx+.45)*h/10
        path(lambda t: (w*t,by+sin(w*t*freq+phase*phase_mul+idx*.42)*amp+
                         sin(w*t*.006-phase*.54+idx)*amp*.45),steps,alpha)
    def mesh(step=28.,angled=False,alpha=.24):
        s=max(12.,step)
        for i in range(int(w/s)+2):
            x=i*s
            line(x,0., x+(h*.35 if angled else 0.),h,alpha)
        for j in range(int(h/s)+2):
            y=j*s
            line(0,y,w,y,alpha)
    def points(count,style='dots',shift=0.):
        for i in range(count):
            px=_rand(i,8)*w; py=_rand(i,31)*h
            drift=(phase*4.+shift)*(.6+_rand(i,19)*.5)
            px=(px+drift)%w
            r=.8+_rand(i,4)*2.1
            if style=='star':
                line(px-r,py,px+r,py,.15+.3*_rand(i,3))
                line(px,py-r,px,py+r,.12+.25*_rand(i,1))
            elif style=='rain':
                line(px,py,px-r*.35,py-4-r*4,.13+.36*_rand(i,3))
            else:
                line(px-r*.45,py,px+r*.45,py,.15+.35*_rand(i,3))
    def zig(cx,cy,r,arms=5,a=.3):
        xy=[]
        for i in range(arms*2+1):
            ang=2*pi*i/(arms*2);rr=r*(1. if i%2==0 else .36)
            xy.append((cx+rr*cos(ang),cy+rr*sin(ang)))
        for A,B in zip(xy,xy[1:]):line(*A,*B,a)

    # Sixteen Studia-original families: follow their visual concept, not copied QML code.
    if pattern=='contours':
        for i in range(max(4,int(10*density))):
            y=h*(i+.5)/max(4,int(10*density))
            path(lambda t:(w*t,y+sin(w*t*.023+i*.4+phase)*10+sin(w*t*.009-i*.3)*9),28,.21+i*.012)
    elif pattern=='flow':
        for i in range(max(5,int(11*density))):stream(i,32,9+i*.8,.012+i*.0008,.16+i*.013)
    elif pattern in ('strata','strata_mineral'):
        for i in range(max(6,int(18*density))):
            y=h*(i+.2)/max(6,int(18*density))
            amp=(4. if pattern=='strata' else 10.)*(1.+i%4*.12)
            path(lambda t:(w*t,y+sin(w*t*.021+phase*.3+i*.4)*amp+sin(w*t*.063+i)*1.7),26,.12+(i%5)*.045)
            if pattern=='strata_mineral' and i%4==0:
                for j in range(5):line(j*w/5,y-3,j*w/5+13,y+4,.32)
    elif pattern in ('prism','facets','crystal_mesh'):
        step=62 if pattern=='prism' else 49 if pattern=='facets' else 40
        for row in range(-1,int(h/step)+2):
            for col in range(-1,int(w/step)+2):
                x=col*step+(row%2)*step*.5; y=row*step
                q=step*(.34+.22*_rand(row,col))
                if pattern=='prism':
                    line(x,y,x+q,y+step*.7,.27);line(x+q,y+step*.7,x+step,y+step*.04,.30)
                elif pattern=='facets':
                    line(x,y,x+step,y+step*.5,.24);line(x+step,y+step*.5,x+step*.15,y+step,.18)
                    line(x+step*.15,y+step,x,y,.13)
                else:
                    zig(x+step*.5,y+step*.5,step*.42,5,.25)
                    line(x+step*.5,y+step*.5,x+q,y,.21)
    elif pattern=='aurora':
        for i in range(16):
            x=(i+.3)*w/16
            path(lambda t:(x+sin(t*pi*1.6+phase+i*.25)*10+t*22,h*t),22,.10+i*.012)
    elif pattern in ('particles','nebula','sand_drift'):
        points(int((165 if pattern=='nebula' else 110 if pattern=='sand_drift' else 100)*density),
               style='star' if pattern=='nebula' else 'rain' if pattern=='sand_drift' else 'dots')
        if pattern=='nebula':
            for i in range(6):circle(w*.6,h*.5,(i+1)*22,(i+1)*9,.12,42,.19,i)
    elif pattern in ('isobars','halo','ripple_lines','pulse','topo_pulse'):
        for i in range(4,14):
            if pattern=='isobars':circle(w*.56,h*.5,i*17,i*9,.11+i*.011,44,.045)
            elif pattern=='halo':circle(w*.68,h*.38,i*11,i*11,.24,44)
            elif pattern=='ripple_lines':circle(w*.36,h*.48,i*14,i*11,.22,44,.03)
            elif pattern=='pulse':circle(w*.5,h*.5,(i+phase*.25)*12,(i+phase*.25)*12,.12+.13*(i%3==0),44)
            else:circle(w*.58,h*.55,i*16,i*10,.17,48,.14,i)
    elif pattern in ('grid','lattice','survey','blueprint_field'):
        mesh((26. if pattern in ('grid','blueprint_field') else 39.)/density,pattern=='lattice',.10 if pattern=='blueprint_field' else .2)
        if pattern in ('survey','blueprint_field'):
            for j in range(2,int(h/42)+1):
                for i in range(2,int(w/55)+1):
                    x=i*55.;y=j*42.
                    line(x-5,y,x+5,y,.31);line(x,y-5,x,y+5,.31)
        if pattern=='blueprint_field':
            line(12,12,w-12,12,.5);line(12,12,12,h-12,.5)
    elif pattern in ('terraces','ridges','dunes'):
        for i in range(11):
            by=h*(i+.25)/11
            if pattern=='terraces':
                path(lambda t:(w*t,by+round(sin(w*t*.025+i+phase)*3)*3),34,.24)
            elif pattern=='ridges':
                path(lambda t:(w*t,by+abs(sin(w*t*.018+phase+i*.26))*16),36,.19+i*.01)
            else:
                path(lambda t:(w*t,by+sin(w*t*.008+i*.9+phase*.4)*20+sin(w*t*.04+i)*3),32,.18)
    elif pattern in ('caustics','waves','marble_flow','liquid_metal','soft_plasma'):
        for i in range(15):
            by=h*(i+.25)/15
            amp=8. if pattern=='waves' else 16. if pattern=='caustics' else 23.
            freq=.012 if pattern=='liquid_metal' else .023
            path(lambda t:(w*t,by+sin(w*t*freq+phase+i*.43)*amp+
                           cos(w*t*.051-phase*.33+i*.15)*(3 if pattern=='waves' else 7)),36,
                           (.09+i*.007) if pattern=='soft_plasma' else .13+i*.011)
        if pattern=='caustics':
            for i in range(12):line(i*w/12,0,(i*w/12)+h*.29,h,.10)
    elif pattern=='nodes':
        coords=[(_rand(i,9)*w,_rand(i,21)*h) for i in range(30)]
        for i,(x,y) in enumerate(coords):
            for j in range(i+1,min(30,i+6)):
                xx,yy=coords[j]
                if hypot(x-xx,y-yy)<w*.26:line(x,y,xx,yy,.1+.11*_rand(i,j))
            zig(x,y,2.+_rand(i,7)*2,4,.34)
    elif pattern=='circuit':
        for i in range(40):
            sx=_rand(i,1)*w;sy=_rand(i,2)*h
            xx=sx+(_rand(i,4)-.5)*95;yy=sy+(_rand(i,5)-.5)*110
            line(sx,sy,xx,sy,.29);line(xx,sy,xx,yy,.28)
            line(xx-2,yy,xx+2,yy,.32)
    elif pattern=='veins':
        for i in range(16):
            x=_rand(i,1)*w;y=_rand(i,2)*h
            for k in range(7):
                nx=x+7+_rand(i,k)*13;ny=y+(k%2*2-1)*(5+_rand(k,i)*11)
                line(x,y,nx,ny,.18+_rand(k,i)*.11)
                if k%2==0:line(x,y,x+9,y-16,.20)
                x,y=nx,ny
    elif pattern=='fiber':
        for i in range(max(6,int(24*density))):
            xx=(i+.3)*w/max(6,int(24*density))
            path(lambda t:(xx+sin(t*11+phase+i*.47)*3+h*t*.09,h*t),17,.1+i*.008)
    elif pattern=='glacier':
        for i in range(16):
            x=i*w/16
            line(x,0,x+h*.42,h,.18+(i%3)*.07)
            line(x,-10,x+h*.1,h*.7,.12)
    elif pattern=='magnetic_field':
        for i in range(12):
            a=(i+.5)/12*2*pi
            path(lambda t:(w*.5+(24+90*t)*cos(a+sin(t*pi)*.46),
                           h*.5+(10+70*t)*sin(a+sin(t*pi)*.46)),34,.19+i*.008)
    elif pattern=='fractal_noise':
        for i in range(30):
            x=_rand(i,4)*w;y=_rand(i,5)*h;r=5+_rand(i,3)*28
            for j in range(3):
                line(x+j*r*.2,y,x+r*.7,y+r*.6,.16+j*.06)
                x+=r*.15;y+=r*.15;r*=.72
    elif pattern=='hex_weave':
        s=23.
        for j in range(-1,int(h/(s*1.5))+3):
            for i in range(-1,int(w/(s*1.7))+3):
                x=i*s*1.73+(j%2)*s*.86;y=j*s*1.5
                pts=[(x+s*cos(2*pi*k/6),y+s*sin(2*pi*k/6)) for k in range(7)]
                for A,B in zip(pts,pts[1:]):line(*A,*B,.16)
    elif pattern=='rain_map':
        for i in range(6):line(i*w/6,0,i*w/6+12,h,.15)
        points(int(130*density),'rain')
        for i in range(12):line(i*w/12,h*.1,i*w/12-7,h*.8,.09)
    elif pattern=='thermal_scan':
        for i in range(15):
            cy=h*(i+.25)/15
            path(lambda t:(w*t,cy+15*sin(2*pi*t+phase+i*.33)+5*sin(12*pi*t+i)),40,.17+(i%4)*.025)
    elif pattern=='cloud_bands':
        for i in range(16):
            by=h*(i+.25)/16
            path(lambda t:(w*t,by+sin(w*t*.01+i*.5+phase*.34)*16+
                           cos(w*t*.025+i*.75)*8),35,.10+i*.007)
    elif pattern=='vector_storm':
        for i in range(52):
            x=_rand(i,2)*w;y=_rand(i,9)*h
            r=7+_rand(i,7)*36
            line(x,y,x+r,y+r*.45,.12+.24*_rand(i,5))
    else:
        raise ValueError('Unknown pattern: '+str(pattern))
    return tuple(lines)


@lru_cache(maxsize=256)
def segments(pattern, width, height, phase=0., density=1., detail=1.):
    """Cached geometry as local pixel-space segments, all clipped and finite."""
    if pattern=='none':return ()
    if pattern not in IDS:raise ValueError('Unsupported pattern: '+str(pattern))
    w=float(width);h=float(height);phase=float(phase);density=float(density);detail=float(detail)
    if not all(isfinite(v) for v in (w,h,phase,density,detail)) or not 2<=w<=8192 or not 2<=h<=8192:
        raise ValueError('Invalid sample dimensions/phase')
    if not .25<=density<=2.5 or not .5<=detail<=3.:raise ValueError('Invalid density/detail')
    if pattern in FEATURED_IDS:
        from .premium_patterns import generate
        return generate(pattern,w,h,phase,density,detail)
    lines=_build(pattern,w,h,phase,density,detail)
    # A tiny deterministic drift also animates geometrically static families
    # (prism, hex, crystal), without changing the logical panel hit rectangle.
    dx=2.5*sin(phase*.47);dy=1.6*(cos(phase*.29)-1.)
    moved=[]
    for x0,y0,x1,y1,alpha in lines:
        seg=_clip(x0+dx,y0+dy,x1+dx,y1+dy,w,h)
        if seg and abs(seg[0]-seg[2])+abs(seg[1]-seg[3])>.08:
            moved.append((*seg,alpha))
    assert len(moved)<=2400
    return tuple(moved)
