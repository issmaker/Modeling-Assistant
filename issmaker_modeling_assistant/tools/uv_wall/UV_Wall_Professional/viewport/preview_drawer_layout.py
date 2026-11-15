"""Responsive, bounded read-only WALL preview drawer geometry."""

def preview_drawer_rect(rail, width, height, scale=1.0):
    """Return compact drawer above/next to the rail, never outside viewport."""
    if rail is None or width < 215 or height < 210:
        return None
    scale=max(.8,min(2.,float(scale)))
    x0,y0,x1,y1=map(float,rail)
    w=min(272.*scale,float(width)-20.)
    h=144.*scale
    if y1 + h + 8.*scale <= float(height)-8.*scale:
        x=min(max(10.,x1-w),float(width)-w-10.)
        y=y1+6.*scale
    elif y0 - h - 8.*scale >= 8.*scale:
        x=min(max(10.,x1-w),float(width)-w-10.)
        y=y0-h-6.*scale
    else:
        # Tiny editors cannot show all controls without hiding model content.
        return None
    return (x,y,x+w,y+h)


def preview_drawer_rows(rect,scale=1.0):
    if rect is None:
        return ()
    x,y,x1,y1=rect
    scale=max(.8,min(2.,float(scale)))
    gap=6.*scale
    outer=10.*scale
    row_h=28.*scale
    result=[]
    top=y1-36.*scale
    for i in range(3):
        upper=top-i*(row_h+gap)
        lower=upper-row_h
        if lower<y+4.*scale:
            break
        result.append((x+outer,lower,x1-outer,upper))
    return tuple(result)
