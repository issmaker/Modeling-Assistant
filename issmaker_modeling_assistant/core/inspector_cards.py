"""Blender-independent bounded diagnostics cards for auxiliary HUD inspectors.

No mesh references, Python draw handlers, or GPU contexts are retained.
"""
from math import isfinite


def card_bounds(width, height, *, scale=1., preferred=(34., 34.), size=(436., 104.)):
    try:
        width,height,scale=map(float,(width,height,scale))
    except (TypeError,ValueError,OverflowError):return None
    if not all(isfinite(v) for v in (width,height,scale)) or scale<=0:return None
    # Never place a partially displayed card. No invisible active regions.
    margin=12.*scale
    w=min(size[0]*scale,width-2*margin)
    h=min(size[1]*scale,height-2*margin)
    if w<230.*scale or h<90.*scale:return None
    x=max(margin,min(preferred[0]*scale,width-w-margin))
    y=max(margin,min(preferred[1]*scale,height-h-margin))
    return (x,y,w,h)


def wrap_for_width(message, max_width, measure, *, max_lines=2):
    """Greedy measured word wrapping, with visible ellipsis on last row."""
    msg=' '.join(str(message).split())
    if not msg:return ()
    if max_width<=0 or not callable(measure) or max_lines<1:return ()
    words=msg.split(' ')
    out=[];line='';i=0
    while i<len(words):
        word=words[i]
        test=(line+' '+word).strip()
        if measure(test)<=max_width:
            line=test;i+=1;continue
        if line:
            out.append(line);line=''
            if len(out)>=max_lines:break
            continue
        # One long token, keep longest visible prefix with ellipsis.
        lo,hi=0,len(word)
        while lo<hi:
            mid=(lo+hi+1)//2
            if measure(word[:mid]+'…')<=max_width:lo=mid
            else:hi=mid-1
        out.append(word[:lo]+'…' if lo else '…');i+=1
        if len(out)>=max_lines:break
    if line and len(out)<max_lines:out.append(line)
    if i<len(words) or (i==len(words) and line and len(out)==max_lines and out[-1]!=line):
        value=out[-1] if out else ''
        while value and measure(value+'…')>max_width:value=value[:-1]
        if out:out[-1]=value.rstrip()+'…'
    return tuple(out[:max_lines])
