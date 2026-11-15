"""Regression of actual 3D SAT witness versus mere AABB and coplanar contact."""
import math
import unittest
from itertools import product

from mathutils import Matrix,Vector
from issmaker_modeling_assistant.tools import collision_inspector as inspector


def convex_cube(center=(0,0,0), size=2.0, azimuth=0.0):
    rotate=Matrix.Rotation(float(azimuth),4,'Z')
    shift=Vector(center)
    half=size*.5
    coords=list(product((-half,half),repeat=3))
    vert_map={value:index for index,value in enumerate(coords)}
    verts=[(rotate @ Vector((x,y,z,1.0))).to_3d()+shift for x,y,z in coords]
    faces=[]
    for axis in range(3):
        for fixed in (-half,half):
            keys=[coord for coord in coords if coord[axis]==fixed]
            ids=tuple(vert_map[key] for key in keys)
            faces.append(ids)
    # Geometry face normals may have either winding; the SAT is insensitive.
    polys=[]
    for face in faces:
        a,b,c=[verts[i] for i in face[:3]]
        normal=(b-a).cross(c-a)
        if normal.length_squared < 1e-20:
            # Sort around face center if product enumeration yielded collinearity
            normal=Vector((1.0,0.0,0.0))
        else:normal.normalize()
        point=sum((verts[i] for i in face),Vector())/len(face)
        polys.append((point,normal))
    edge_map={}
    for face in faces:
        for i in range(len(face)):
            a,b=sorted((face[i],face[(i+1)%len(face)]))
            edge_map[(a,b)]={"p0":verts[a],"p1":verts[b]}
    bbox_min=Vector(tuple(min(v[i] for v in verts) for i in range(3)))
    bbox_max=Vector(tuple(max(v[i] for v in verts) for i in range(3)))
    return {"verts":verts,"polys":polys,"face_verts":faces,
            "edge_segments":list(edge_map.values()),
            "bbox_min":bbox_min,"bbox_max":bbox_max}


class PositiveSharedVolumeSATTests(unittest.TestCase):
    def check(self,shift,expected,rotation=0.0,other_rotation=0.0):
        first=convex_cube(azimuth=rotation)
        other=convex_cube(center=shift,azimuth=other_rotation)
        result=inspector._convex_shells_have_positive_shared_volume(first,other)
        self.assertIs(result,expected,(shift,rotation,other_rotation))
        return first,other

    def test_missed_one_axis_positive_volume(self):
        for shift in (.5,1.0,1.8):
            with self.subTest(shift=shift):
                self.check((shift,0,0),True)

    def test_zero_volume_contacts_are_not_penetration(self):
        for center in ((2,0,0),(2,2,0),(2,2,2),(-2,0,0)):
            with self.subTest(center=center):
                self.check(center,False)

    def test_gaps_do_not_become_intersections(self):
        for x in (2.0000001,2.0001,2.001,20):
            with self.subTest(x=x):
                self.check((x,0,0),False)

    def test_positive_volume_two_and_three_axes(self):
        self.check((1,1,0),True)
        self.check((.5,.5,.5),True)
        self.check((1,1,1),True)

    def test_nested_is_positive_volume_at_math_level(self):
        outer=convex_cube(size=4)
        inner=convex_cube(size=1)
        self.assertTrue(inspector._convex_shells_have_positive_shared_volume(outer,inner))

    def test_rotated_aabb_overlap_without_volume(self):
        angle=math.pi/4
        a,b=self.check((2,2,0),False,angle,angle)
        self.assertTrue(inspector._aabb_has_positive_shared_volume(a,b),
                        "AABB is a necessary but insufficient condition")

    def test_rotated_positive_volume(self):
        angle=math.pi/4
        self.check((1,1,0),True,angle,angle)

    def test_rotated_exact_contacts_are_not_penetration(self):
        for degrees in (15,45,80):
            angle=math.radians(degrees)
            transform=Matrix.Rotation(angle,3,'Z')
            for offset in ((2,0,0),(2,1,0),(2,2,0)):
                shift=transform @ Vector(offset)
                with self.subTest(angle=degrees,offset=offset):
                    self.check(tuple(shift),False,angle,angle)

    def test_reflected_face_normals_do_not_change_result(self):
        a,b=self.check((.5,0,0),True)
        for d in (a,b):
            d["polys"]=[(point,-normal) for point,normal in d["polys"]]
        self.assertTrue(inspector._convex_shells_have_positive_shared_volume(a,b))

    def test_nonconvex_candidate_is_never_proven(self):
        a=convex_cube()
        b=convex_cube(center=(.5,0,0))
        # An inward face violates the convex supporting-plane condition.
        a["verts"][0]=Vector((0.0,0.0,0.0))
        self.assertIsNone(inspector._convex_shells_have_positive_shared_volume(a,b))


if __name__=="__main__":
    unittest.main()
