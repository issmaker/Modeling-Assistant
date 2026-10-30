"""Conservative UV repair of cap faces after wall/cap T-junction stitching.

Never modifies geometry. Pure tile selection is testable without Blender.
"""
from math import cos, sin, floor, isfinite


def tile_coordinates(points, module, angle_rad, tolerance=1.e-5, face_index=None):
    if not isfinite(module) or module<=0:
        raise ValueError('Invalid UV module dimension')
    rotated=[(float(p[0])*cos(angle_rad)+float(p[1])*sin(angle_rad),
              -float(p[0])*sin(angle_rad)+float(p[1])*cos(angle_rad)) for p in points]
    if not rotated:return []
    # A face is clipped to one tile before stitching.  Its centroid is safely
    # inside that tile even when some boundary vertices coincide with the grid.
    center_u=sum(p[0] for p in rotated)/len(rotated)
    center_v=sum(p[1] for p in rotated)/len(rotated)
    tile_u=floor(center_u/module)
    tile_v=floor(center_v/module)
    answer=[]
    for u,v in rotated:
        uu=(u-tile_u*module)/module
        vv=(v-tile_v*module)/module
        if not (-tolerance<=uu<=1+tolerance and -tolerance<=vv<=1+tolerance):
            raise ValueError(
                'UV-крышка пересекает границу модуля после стыковки; '
                'требуется пересечение по сетке. '
                f'face={face_index}; tile=({tile_u},{tile_v}); module={module:.6f}; '
                f'local_uv=({uu:.9f},{vv:.9f}); '
                f'range=({min(p[0] for p in rotated):.9f},'
                f'{max(p[0] for p in rotated):.9f})x'
                f'({min(p[1] for p in rotated):.9f},'
                f'{max(p[1] for p in rotated):.9f}); '
                f'xy={[(round(float(p[0]), 7), round(float(p[1]), 7)) for p in points]}'
            )
        answer.append((max(0.,min(1.,uu)),max(0.,min(1.,vv))))
    return answer


def reproject_cap_uv(mesh, source, module, angle_rad, roles, role_codes, converter):
    layer=mesh.uv_layers.active
    if layer is None:return 0
    valid={role_codes['TOP_CAP'],role_codes['BOTTOM_CAP']}
    changed=0
    for polygon in mesh.polygons:
        if roles[polygon.index] not in valid:continue
        points=[mesh.vertices[index].co for index in polygon.vertices]
        grid_uv=tile_coordinates([(p.x,p.y) for p in points],module,angle_rad,face_index=polygon.index)
        if len(grid_uv)!=len(polygon.loop_indices):
            raise ValueError('Несовпадение UV/топологии крышки после стыковки')
        for loop,uv in zip(polygon.loop_indices,grid_uv):
            dest=converter(source,uv[0],uv[1])
            old=layer.data[loop].uv
            if (old-dest).length>1e-6:changed+=1
            layer.data[loop].uv=dest
    return changed
