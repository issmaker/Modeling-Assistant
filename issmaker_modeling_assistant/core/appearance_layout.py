"""Pass 12.1 layout geometry: pure and resolution-independent (Blender px)."""

def hero_regions(x,y,w,h):
    """Main preview divided into text and bounded pattern demonstration."""
    x=float(x);y=float(y);w=float(w);h=float(h)
    if min(w,h)<=0:raise ValueError('Invalid size')
    left=min(max(184.,w*.51),max(184.,w-158.))
    pattern=(x+left+8,y+8,max(8.,w-left-16),max(8.,h-16))
    label=(x+12,y+9,max(32.,left-20),max(8.,h-18))
    return label,pattern
