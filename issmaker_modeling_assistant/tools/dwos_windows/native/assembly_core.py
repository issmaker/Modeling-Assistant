"""Authoritative, Blender-independent window assembly anchors and validation.

XY: frame belt centerline; Z: glass 1/2 depth; interior BOX start 1/4 depth.
The local exterior facade is z=0, local interior is toward ``interior_sign``.
"""
from __future__ import annotations
import math

EPS=1e-7

def belt_centerline(width, height, frame_width, edge_offset=0.0):
    values=(width,height,frame_width,edge_offset)
    if not all(math.isfinite(float(v)) for v in values):
        raise ValueError('Центральная линия рамы: NaN/Infinity недопустимы')
    if width<=EPS or height<=EPS or frame_width<=EPS or frame_width*2 >= min(width,height)-EPS:
        raise ValueError('Рама: ширина/высота/толщина не образуют проём')
    inset=frame_width*0.5+edge_offset
    if inset < -EPS or 2*inset >= min(width,height)-EPS:
        raise ValueError('Отступ от середины рамы выходит за допустимые границы')
    return (-width/2+inset,width/2-inset,-height/2+inset,height/2-inset)

def glass_overlap(frame_width):
    """A small real XY overlap of glazing over the centreline of the frame belt.

    This is not an artificial image offset.  Each glass edge extends outward
    by at most 10 mm, while staying within the physical frame belt.
    """
    if not math.isfinite(float(frame_width)) or frame_width <= EPS:
        raise ValueError('Недопустимая ширина рамы для стекла')
    return min(0.01, float(frame_width) * 0.125)


def glass_rectangle(width, height, frame_width, user_offset=0.0):
    """Glass uses the main frame centreline with a small outboard overlap.

    user_offset remains independent: zero means the automatic overlap; positive
    values retreat towards the opening, negative values extend towards the frame.
    """
    return belt_centerline(width, height, frame_width,
                           user_offset - glass_overlap(frame_width))


def depth_anchors(frame_depth, facade_z, interior_sign, glass_offset=0.0,
                  interior_offset=0.0):
    values=(frame_depth,facade_z,glass_offset,interior_offset)
    if not all(math.isfinite(float(v)) for v in values):
        raise ValueError('Глубина: NaN/Infinity недопустимы')
    if frame_depth < 0.005:
        raise ValueError('Глубина рамы должна быть не меньше 0,005')
    if interior_sign not in (-1,1):
        raise ValueError('Ориентация стороны помещения должна быть +1 или -1')
    interior_distance=frame_depth*0.25+interior_offset
    if interior_distance < -EPS or interior_distance > frame_depth + EPS:
        raise ValueError('Начало Interior должно оставаться в пределах глубины рамы')
    glass_distance=frame_depth*0.5+glass_offset
    if glass_distance < 0 or glass_distance > frame_depth:
        raise ValueError('Стекло выходит за глубину рамы')
    return (facade_z+interior_sign*glass_distance,
            facade_z+interior_sign*interior_distance)

def validate_glazing(frame_depth, gap, glass_offset, full, margin=0.001):
    if not all(math.isfinite(float(v)) for v in (frame_depth,gap,glass_offset)):
        raise ValueError('Стекло: NaN/Infinity недопустимы')
    if frame_depth<0.005 or gap<=0:
        raise ValueError('Стекло: некорректная глубина или зазор')
    max_offset=frame_depth*0.5-margin-(gap/2 if full else 0)
    if max_offset < -EPS or abs(glass_offset)>max_offset+EPS:
        raise ValueError('Стекло или стеклопакет выходит за границы глубины рамы')
    return True
