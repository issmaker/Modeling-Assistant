"""Deterministic, bpy-free geometry for Collision Inspector's 55 diagnostic cases.

All units are meters. Intentionally invalid faces/edges MUST NOT be validated,
cleaned, welded, triangulated, or fixed by the generator.
"""
from math import cos, sin, pi

CATEGORIES = (
    ('UNCLOSED', 'Открытая оболочка'),
    ('NONMANIFOLD', 'Non-manifold'),
    ('NONCONVEX', 'Невыпуклость'),
    ('DEGENERATE', 'Вырожденная геометрия'),
    ('SELF_INTERSECTION', 'Самопересечение'),
    ('INTERSECTION', 'Пересечение оболочек'),
    ('NESTED', 'Полное вложение'),
    ('GAP', 'Малый зазор'),
    ('CONTACT', 'Касание'),
    ('COINCIDENT', 'Совпадение оболочек'),
    ('SURFACE_OVERLAP', 'Наложение поверхностей'),
)

CASE_NAMES = {
    'UNCLOSED': ('Куб без крышки', 'Цилиндр без торца', 'Открытая труба', 'Короб без стены', 'Свободный контур'),
    'NONMANIFOLD': ('Три грани на ребре', 'Четыре грани', 'Пять граней', 'Веер из шести граней', 'Ребро куба с плавником'),
    'NONCONVEX': ('L-образный объём', 'Ступень', 'U-образный объём', 'Крестовой объём', 'Z-образный объём'),
    'DEGENERATE': ('Коллинеарный треугольник', 'Нулевая грань', 'Коллинеарный четырёхугольник', 'Очень тонкий треугольник', 'Нулевое ребро'),
    'SELF_INTERSECTION': ('Перекрёстная лента', 'Широкая петля', 'Диагональный перекрёст', 'Развёрнутая петля', 'Смещённый перекрёст'),
    'INTERSECTION': ('Проникновение двух кубов', 'Угловое проникновение', 'Пересечение длинных балок', 'Куб и цилиндр', 'Треугольный призматический клин'),
    'NESTED': ('Куб внутри куба', 'Смещённый внутренний куб', 'Куб внутри цилиндра', 'Тетраэдр внутри куба', 'Призма внутри коробки'),
    'GAP': ('Зазор 0.025 мм', 'Зазор 0.065 мм', 'Зазор 0.110 мм', 'Зазор 0.150 мм', 'Зазор 0.190 мм'),
    'CONTACT': ('Касание вершинами', 'Касание ребром', 'Касание на верхнем ребре', 'Касание углами', 'Касание у кромки'),
    'COINCIDENT': ('Совпадающие кубы', 'Совпадающие призмы', 'Совпадающие цилиндры', 'Совпадающие тетраэдры', 'Совпадающие вытянутые блоки'),
    'SURFACE_OVERLAP': ('Общая часть граней', 'Смещение вдоль Y', 'Смещение вдоль Z', 'Широкая общая площадка', 'Узкая общая площадка'),
}

GAPS_MM = (0.025, 0.065, 0.110, 0.150, 0.190)


class Geometry:
    def __init__(self):
        self.vertices = []
        self.faces = []
        self.edges = []
        self.face_materials = []

    def v(self, xyz):
        self.vertices.append(tuple(float(t) for t in xyz))
        return len(self.vertices) - 1

    def face(self, indices, material=0):
        self.faces.append(tuple(indices))
        self.face_materials.append(int(material))

    def box(self, center=(0, 0, 0), size=(2, 2, 2), omit=(), material=0):
        x, y, z = center
        sx, sy, sz = (float(t) * .5 for t in size)
        corners = [(x-sx,y-sy,z-sz),(x+sx,y-sy,z-sz),(x+sx,y+sy,z-sz),
                   (x-sx,y+sy,z-sz),(x-sx,y-sy,z+sz),(x+sx,y-sy,z+sz),
                   (x+sx,y+sy,z+sz),(x-sx,y+sy,z+sz)]
        ix = [self.v(v) for v in corners]
        six = ((3,2,1,0),(4,5,6,7),(0,1,5,4),
               (1,2,6,5),(2,3,7,6),(3,0,4,7))
        for n, quad in enumerate(six):
            if n not in omit:
                self.face([ix[i] for i in quad], material)
        return ix

    def cylinder(self, sides=12, radius=1, depth=2, center=(0,0,0), omit=(), material=0):
        x, y, z = center
        low = [self.v((x+radius*cos(2*pi*k/sides),y+radius*sin(2*pi*k/sides),z-depth/2)) for k in range(sides)]
        high = [self.v((x+radius*cos(2*pi*k/sides),y+radius*sin(2*pi*k/sides),z+depth/2)) for k in range(sides)]
        if 'bottom' not in omit: self.face(tuple(reversed(low)), material)
        if 'top' not in omit: self.face(high, material)
        for k in range(sides):
            if k not in omit:
                j = (k+1)%sides
                self.face((low[k],low[j],high[j],high[k]),material)

    def tetra(self, center=(0,0,0), scale=1.0, material=0):
        x,y,z = center
        vertices = ((1,1,1),(-1,-1,1),(-1,1,-1),(1,-1,-1))
        ix = [self.v((x+scale*a,y+scale*b,z+scale*c)) for a,b,c in vertices]
        for face in ((0,1,2),(0,3,1),(0,2,3),(1,3,2)):
            a,b,c = (self.vertices[ix[k]] for k in face)
            u = tuple(b[k]-a[k] for k in range(3)); v = tuple(c[k]-a[k] for k in range(3))
            normal = (u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
            centroid = tuple(sum(self.vertices[ix[t]][k] for t in face)/3 - center[k] for k in range(3))
            self.face(tuple(ix[t] for t in (face if sum(normal[k]*centroid[k] for k in range(3))>0 else reversed(face))),material)

    def prism(self, center=(0,0,0), radius=1.0, length=2.0, sides=3, material=0):
        self.cylinder(sides=sides,radius=radius,depth=length,center=center,material=material)

    def voxels(self, occupied, unit=.78, material=0):
        """Connected face-grid solid with shared vertex indices (not touching cubes)."""
        active = set(occupied)
        points = {}
        sides = ( ((-1,0,0),(0,4,7,3)), ((1,0,0),(1,2,6,5)),
                  ((0,-1,0),(0,1,5,4)), ((0,1,0),(2,3,7,6)),
                  ((0,0,-1),(3,2,1,0)), ((0,0,1),(4,5,6,7)) )
        corners = ((0,0,0),(1,0,0),(1,1,0),(0,1,0),
                   (0,0,1),(1,0,1),(1,1,1),(0,1,1))
        for i,j,k in sorted(active):
            for delta, face in sides:
                if (i+delta[0],j+delta[1],k+delta[2]) in active: continue
                indices = []
                for corner_id in face:
                    corner = corners[corner_id]
                    coord = (i+corner[0],j+corner[1],k+corner[2])
                    if coord not in points:
                        points[coord] = self.v(tuple(unit*c for c in coord))
                    indices.append(points[coord])
                self.face(indices,material)
        # Recenter the voxel cluster for legible per-tile labels.
        count=len(self.vertices)
        if count:
            mins=[min(v[k] for v in self.vertices) for k in range(3)]
            maxs=[max(v[k] for v in self.vertices) for k in range(3)]
            off=[-.5*(a+b) for a,b in zip(mins,maxs)]
            self.vertices=[tuple(v[k]+off[k] for k in range(3)) for v in self.vertices]

    def ribbon(self, path, width=.36):
        # XY band. Nonadjacent strips of the continuous path cross each other.
        for i,(x,y) in enumerate(path):
            if i == 0: tx,ty=path[1][0]-x,path[1][1]-y
            elif i == len(path)-1: tx,ty=x-path[i-1][0],y-path[i-1][1]
            else: tx,ty=path[i+1][0]-path[i-1][0],path[i+1][1]-path[i-1][1]
            length=(tx*tx+ty*ty)**.5 or 1
            nx,ny=-ty/length,tx/length
            self.v((x+nx*width/2,y+ny*width/2,.1))
            self.v((x-nx*width/2,y-ny*width/2,.1))
        for i in range(len(path)-1):
            self.face((i*2,i*2+1,i*2+3,i*2+2))

    def degenerate_triangle(self, a,b,c):
        ids=[self.v(k) for k in (a,b,c)]
        self.face(ids)


def build_case(kind, number):
    """Return (Geometry, title). 1 <= number <= 5. All cases intentionally authored."""
    if kind not in CASE_NAMES or not 1 <= number <= 5:
        raise ValueError((kind,number))
    g=Geometry(); i=number-1
    if kind == 'UNCLOSED':
        if i==0: g.box(omit=(1,))
        elif i==1: g.cylinder(12,1,2,omit=('top',))
        elif i==2: g.cylinder(16,.9,2,omit=('top','bottom'))
        elif i==3: g.box(size=(2,1.4,2.2),omit=(3,))
        else:
            g.box(size=(1.4,1.4,1.4)); v1=g.v((1.4,-.8,0));v2=g.v((2.0,-.8,0));v3=g.v((2.5,-.5,.3))
            g.edges.extend(((v1,v2),(v2,v3)))
    elif kind=='NONMANIFOLD':
        if i<4:
            a=g.v((0,0,-1));b=g.v((0,0,1))
            for n in range(3+i):
                t=2*pi*n/(3+i)+.13*i
                c=g.v((1.1*cos(t),1.1*sin(t),.0))
                g.face((a,b,c))
        else:
            ids=g.box()
            p=g.v((1.9,-.6,.3))
            g.face((ids[1],ids[2],p))
    elif kind=='NONCONVEX':
        solids=( ((0,0,0),(1,0,0),(0,1,0)),
                 ((0,0,0),(1,0,0),(2,0,0),(2,0,1),(2,0,2)),
                 ((0,0,0),(1,0,0),(2,0,0),(0,1,0),(2,1,0),(0,2,0),(2,2,0)),
                 ((1,0,0),(0,1,0),(1,1,0),(2,1,0),(1,2,0)),
                 ((0,0,0),(1,0,0),(1,1,0),(2,1,0),(2,2,0),(3,2,0)) )
        g.voxels(solids[i])
    elif kind=='DEGENERATE':
        if i==0: g.degenerate_triangle((-1,0,0),(0,0,0),(1,0,0))
        elif i==1: g.degenerate_triangle((.2,.2,0),(.2,.2,0),(.2,.2,0))
        elif i==2:
            ids=[g.v((x,0,0)) for x in (-1,-.2,.3,1)]
            g.face(ids)
        elif i==3: g.degenerate_triangle((-1,0,0),(1,0,0),(0,1e-13,0))
        else:
            a=g.v((-1,0,0)); b=g.v((-1+1e-11,0,0)); g.edges.append((a,b))
            g.box(center=(.1,0,0),size=(1.25,1.25,1.25))
    elif kind=='SELF_INTERSECTION':
        # Different crossing arrangements, all use an edge-connected ribbon.
        paths=(
            ((1.114,-1.481),(1.101,.594),(-.575,-1.034),(1.646,-.490)),
            ((-.050,-1.292),(.938,.798),(-1.338,-.074),(.179,-.705)),
            ((.063,-1.137),(-.991,-.486),(.318,-.810),(-1.007,-1.287)),
            ((1.430,.731),(-.091,-.722),(-.910,.413),(.957,.064)),
            ((.743,-1.307),(-.333,.128),(-.303,-.879),(-.287,1.215)),
        )
        g.ribbon(paths[i],width=(.1785,.3345,.2862,.2853,.2768)[i])
    elif kind=='INTERSECTION':
        if i==0: g.box();g.box((1.05,.08,.02),material=1)
        elif i==1: g.box();g.box((1.2,1.2,.45),material=1)
        elif i==2: g.box(size=(3.2,.75,.85));g.box(size=(.75,3.2,.85),material=1)
        elif i==3: g.box();g.cylinder(16,.82,2.5,center=(.9,0,0),material=1)
        else: g.box();g.prism(center=(.75,.1,0),radius=1.25,length=2,sides=3,material=1)
    elif kind=='NESTED':
        if i==0: g.box(size=(3.4,3.4,3.4));g.box(size=(1.4,1.4,1.4),material=1)
        elif i==1: g.box(size=(4,3.3,3));g.box(center=(.6,-.3,.2),size=(1.3,1.2,1.1),material=1)
        elif i==2: g.cylinder(sides=16,radius=2,depth=3.4);g.box(size=(1.3,1.3,1.3),material=1)
        elif i==3: g.box(size=(3.5,3.5,3.5));g.tetra(scale=.63,material=1)
        else: g.box(size=(4,3.6,3.6));g.prism(sides=5,radius=.85,length=1.5,material=1)
    elif kind=='GAP':
        gap=GAPS_MM[i]/1000.0
        sizes=((1.7,1.7,1.7),(1.7,1.3,2.3),(1.6,1.8,1.6),(2.4,.9,1.6),(1.0,2.4,1.3))
        width,height,depth=sizes[i]
        g.box(center=(-width/2-gap/2,0,0),size=(width,height,depth))
        g.box(center=(width/2+gap/2,0,0),size=(width,height,depth),material=1)
    elif kind=='CONTACT':
        g.box(size=(2,2,2))
        centers=((2,2,2),(2,2,0),(2,0,2),(2,-2,-2),(-2,2,.45))
        g.box(center=centers[i],size=(2,2,2),material=1)
    elif kind=='COINCIDENT':
        for material in (0,1):
            if i==0: g.box(material=material)
            elif i==1: g.prism(sides=5,radius=1.1,length=2,material=material)
            elif i==2: g.cylinder(sides=12,radius=1,depth=2,material=material)
            elif i==3: g.tetra(scale=.9,material=material)
            else: g.box(size=(2.8,.85,1.3),material=material)
    elif kind=='SURFACE_OVERLAP':
        first=( (2,2,2),(2,2,2),(2.1,2,2.4),(2,3.0,1.2),(2,2,1.6) )[i]
        second=( (2,2,2),(2,2,2),(1.2,1.1,1.5),(1.4,.8,1),(1.2,1.2,1.2) )[i]
        lateral=( (0,.42,0),(0,1.2,0),(0,0,.8),(0,1.45,0),(0,1.35,0) )[i]
        g.box(size=first)
        g.box(center=(first[0]/2+second[0]/2,lateral[1],lateral[2]),size=second,material=1)
    return g, CASE_NAMES[kind][i]


def specs():
    for kind,_ in CATEGORIES:
        for number in range(1,6):
            yield kind,number,CASE_NAMES[kind][number-1]


def assemble_cases(case_rows, mode='ONE', column_spacing=6.8, row_spacing=7.3):
    """Concatenate independent diagnostic cases without welding or sanitizing.

    Each row is (category_row, case_column, problem_kind, 1-based_variant).
    The returned vertex/face indices stay stable per source fixture. The calling
    Blender object provides the category-row Y translation in CATEGORY mode.
    """
    if mode not in ('ONE','CATEGORY'):
        raise ValueError('Aggregate layout must be ONE or CATEGORY')
    vertices, edges, faces, materials, manifest = [], [], [], [], []
    for row, column, kind, number in case_rows:
        shape, _ = build_case(kind,number)
        if not shape.vertices or len(shape.face_materials)!=len(shape.faces):
            raise ValueError(f'Invalid case geometry {kind}/{number}')
        dx=float(column*column_spacing)
        dy=float(-row*row_spacing if mode=='ONE' else 0.0)
        offset=len(vertices)
        vertices.extend((v[0]+dx,v[1]+dy,v[2]) for v in shape.vertices)
        edges.extend((a+offset,b+offset) for a,b in shape.edges)
        faces.extend(tuple(i+offset for i in polygon) for polygon in shape.faces)
        materials.extend(shape.face_materials)
        manifest.append([kind,number,dx,float(-row*row_spacing)])
    return vertices,edges,faces,materials,manifest
