"""Pass 10: pure, fail-closed navigation/selection plans for the fixture gallery.

The functions below never access bpy, select anything, or change Mesh.
"""
from __future__ import annotations
import hashlib
import struct
from collections import Counter
from math import isfinite

FIELDS = ('id','kind','type','case_index','vertex_start','vertex_count',
          'face_start','face_count','opening_start_station','opening_end_station')


def canonical_case(source, provided):
    """Reject user-edited/untrusted manifest topology instead of indexing wrong Mesh."""
    if not isinstance(provided, dict):
        raise ValueError('Invalid fixture manifest case')
    for key in FIELDS:
        if provided.get(key) != source.get(key):
            raise ValueError(f'Fixture case manifest mismatch: {key}')
    return source


def validate_manifest(cases, expected):
    if not isinstance(cases, list) or len(cases)!=len(expected) or len(cases)!=30:
        raise ValueError('Expected 30 valid fixture cases')
    for spec, actual in zip(expected, cases):
        canonical_case(spec,actual)
    return True


def selection_target(gallery, case_index):
    """Only selected opening rim edges OR its closing patch, not surrounding wall."""
    cases=gallery['cases']
    if type(case_index) is not int or not 0 <= case_index < len(cases):
        raise ValueError('Fixture case index out of bounds')
    case=cases[case_index]
    low=int(case['opening_start_station']); high=int(case['opening_end_station'])
    first=int(case['vertex_start']); fs=int(case['face_start']); fe=fs+int(case['face_count'])
    if case['kind']=='OPEN':
        edges=[]
        vi=lambda st,lv: first+st*4+lv
        for st in range(low,high):
            edges.append((vi(st,1),vi(st+1,1)))
            edges.append((vi(st,2),vi(st+1,2)))
        edges.extend([(vi(low,1),vi(low,2)),(vi(high,1),vi(high,2))])
        if len(edges)!=2*(high-low)+2:
            raise ValueError('Invalid opening rim')
        return {'kind':'OPEN','edges':tuple(sorted(tuple(sorted(e)) for e in edges)),
                'faces':(), 'id':case['id']}
    if case['kind']=='FILLED':
        found=[]
        for fi in range(fs,fe):
            face=gallery['faces'][fi]
            stations=tuple((i-first)//4 for i in face)
            bands=tuple((i-first)%4 for i in face)
            if min(stations)>=low and max(stations)<=high and set(bands)=={1,2}:
                found.append(fi)
        if not found or len(found)!=(high-low):
            raise ValueError('Missing/ambiguous closing polygons')
        return {'kind':'FILLED','edges':(), 'faces':tuple(found), 'id':case['id']}
    raise ValueError('Invalid opening kind')


def geometry_fingerprint(vertices, faces, case_ids):
    """SHA256 of authored topology, local vertex coordinates, and case ownership.

    Vertices are quantized to float32 exactly as Blender Mesh stores them. This
    detects structural edits but intentionally does not inspect UV coordinates.
    """
    if len(faces)!=len(case_ids) or len(vertices)>200000 or len(faces)>200000:
        raise ValueError('Corrupt or too large fixture geometry')
    h=hashlib.sha256()
    h.update(struct.pack('<II',len(vertices),len(faces)))
    for v in vertices:
        if len(v)!=3 or not all(isfinite(float(x)) and abs(float(x))<1e8 for x in v):
            raise ValueError('Non-finite fixture vertex')
        h.update(struct.pack('<fff',*(float(x) for x in v)))
    for f,cid in zip(faces,case_ids):
        if len(f)<3 or len(f)>256 or any(not isinstance(i,int) or i<0 or i>=len(vertices) for i in f):
            raise ValueError('Corrupt fixture face')
        h.update(struct.pack('<I',len(f)))
        h.update(struct.pack('<'+'I'*len(f),*f))
        if type(cid) is not int or not 0 <= cid <= 30:
            raise ValueError('Invalid fixture face case ID')
        h.update(struct.pack('<I',cid))
    return h.hexdigest()


def fixture_expected_fingerprint(gallery):
    return geometry_fingerprint(gallery['vertices'],gallery['faces'],gallery['face_case_ids'])


def assert_original_fixture(actual_vertices, actual_faces, actual_case_ids, gallery):
    expected=fixture_expected_fingerprint(gallery)
    actual=geometry_fingerprint(actual_vertices,actual_faces,actual_case_ids)
    if actual!=expected:
        raise ValueError('Тестовый Mesh изменён; выбор по исходным индексам отключён')
    return actual


def gallery_summary(gallery):
    return dict(total=len(gallery['cases']),
                kinds=dict(Counter(c['kind'] for c in gallery['cases'])),
                shapes=dict(Counter(c['type'] for c in gallery['cases'])),
                fingerprint=fixture_expected_fingerprint(gallery),
                targets={c['id']:dict(kind=selection_target(gallery,i)['kind'],
                                      edges=len(selection_target(gallery,i)['edges']),
                                      faces=len(selection_target(gallery,i)['faces']))
                         for i,c in enumerate(gallery['cases'])})


def fixture_hud_layout(width, height):
    """Compact bottom-center controls in WINDOW-region pixel coordinates."""
    if not all(isfinite(float(x)) for x in (width,height)) or width<320 or height<185:
        return None
    width=float(width);height=float(height)
    w=min(390.,width-32.)
    x=(width-w)/2.;y=32.
    panel=(x,y,x+w,y+60.)
    left=(x+9,y+12,x+49,y+48)
    right=(x+w-49,y+12,x+w-9,y+48)
    select=(x+w-139,y+12,x+w-56,y+48)
    title=(x+58,y+33)
    hint=(x+58,y+17)
    if not all(0<=v<=width for v in (panel[0],panel[2],left[0],right[2],select[2])):
        return None
    if panel[3]>height-8.:
        return None
    return dict(panel=panel,left=left,right=right,select=select,title=title,hint=hint)


def in_rect(x,y,rect):
    return rect[0]<=x<=rect[2] and rect[1]<=y<=rect[3]
