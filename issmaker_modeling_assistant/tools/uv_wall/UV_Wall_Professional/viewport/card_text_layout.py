"""Blender-free measured text fitting for point/problem cards."""
from math import isfinite

def wrap_lines(message,limit,measure,max_lines=6):
    """Fit all words, fail with explicit overflow line rather than painting beyond bounds."""
    limit=float(limit)
    if not isfinite(limit) or limit<=0:return []
    parts=str(message).replace('\n',' ').split()
    out=[];row=''
    for part in parts:
        next_row=(row+' '+part).strip()
        if row and measure(next_row)>limit:
            out.append(row);row=part
        else:row=next_row
        if measure(row)>limit:
            # Long unbroken identifiers wrap by measured glyph widths.
            rest=row;row=''
            while rest:
                lo,hi=1,len(rest)
                while lo<hi:
                    m=(lo+hi+1)//2
                    if measure(rest[:m])<=limit:lo=m
                    else:hi=m-1
                chunk=rest[:max(1,lo)];rest=rest[len(chunk):]
                if rest:out.append(chunk)
                else:row=chunk
    if row:out.append(row)
    if len(out)<=max_lines:return out
    clipped=out[:max_lines-1]
    clipped.append('…')
    return clipped

def card_size(lines,scale=1.,min_width=220.,max_width=360.):
    if not lines:return min_width,64.*scale
    return max(min_width,min(max_width,360.*scale)),(44.+len(lines)*16.+30.)*scale
