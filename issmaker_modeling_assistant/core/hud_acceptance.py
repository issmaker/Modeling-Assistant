"""W18 deterministic, bounded HUD acceptance checks; NEVER touch Blender Mesh.

PASS_OFFLINE checks the math implementation, PASS_CONTEXT only means a
VIEW_3D context was actually available. Neither proves physical Gizmo clicks.
"""
from __future__ import annotations
import math
import random
from . import bottom_issue_layout as layout
from . import hud_scope
from .. import nav_hud_geometry

SCHEMA = 'issmaker.ppa.hud_acceptance.2'
SEED = 20261027


def run_lab(context=None, *, expected_scope=None, enabled=False, cases=512):
    """Side-effect-free, deterministic data. No GPU/handlers/mesh access."""
    rows = []

    def group(name, action):
        try:
            count = action()
            rows.append({'id': name, 'status': 'PASS', 'cases': count})
        except Exception as exc:
            rows.append({'id': name, 'status': 'FAIL',
                         'witness': ('%s: %s' % (type(exc).__name__, str(exc)))[:300]})

    def scope_case():
        from types import SimpleNamespace
        class Ptr:
            def __init__(self, ptr): self.ptr = ptr
            def as_pointer(self): return self.ptr
        for i in range(1, 257):
            owner = SimpleNamespace(window=Ptr(i), area=Ptr(10000+i))
            owner.area.type = 'VIEW_3D'
            other = SimpleNamespace(window=Ptr(i+1), area=Ptr(10000+i))
            other.area.type = 'VIEW_3D'
            assert hud_scope.matches((i, 10000+i), owner)
            assert not hud_scope.matches((i, 10000+i), other)
            assert hud_scope.context_scope(SimpleNamespace(window=Ptr(i), area=SimpleNamespace(type='TEXT_EDITOR'))) is None
        return 256

    def layouts():
        modes = ('CENTER', 'LEFT', 'RIGHT', 'CUSTOM')
        widths = ((960, 540), (1920, 1080), (2560, 1440), (3840, 2160))
        count = 0
        for w, h in widths:
            for scale in (.75, 1.0, 1.5, 2.0):
                for mode in modes:
                    raw = nav_hud_geometry.layout(w*.5, 100.*scale, scale)
                    dest = layout.shift_layout(raw, w, h, mode, custom_x=.42, custom_y=.19)
                    if dest is None:
                        # Validly hiding a too-tall panel is not a FAIL.
                        continue
                    assert len(dest['buttons']) == 4
                    gx, gy, gw, gh = dest['grip']
                    assert layout.hit_grip(dest, gx, gy)
                    for i, (x,y,size) in enumerate(dest['buttons']):
                        assert layout.hit_button(dest, x, y) == i
                        assert not layout.hit_grip(dest, x, y)
                        assert abs(x-gx)*2 >= gw+size or abs(y-gy)*2 >= gh+size
                    x0,y0,x1,y1=dest['panel']
                    assert 0<=x0<x1<=w and 0<=y0<y1<=h
                    count+=1
        assert count >= 40, 'Insufficient valid DPI cases'
        return count

    def seeded_fuzz():
        rng = random.Random(SEED)
        n = max(1, min(int(cases), 2048))
        checked = 0
        for _ in range(n):
            w = rng.uniform(320, 4500)
            h = rng.uniform(170, 2500)
            scale = rng.uniform(.5, 2.)
            raw = nav_hud_geometry.layout(w*.5, max(90., scale*90), scale)
            mode = rng.choice(('CENTER', 'LEFT', 'RIGHT', 'CUSTOM'))
            dest = layout.shift_layout(raw, w, h, mode,
                                       custom_x=rng.random(), custom_y=rng.random())
            if dest is None:
                continue
            gx, gy, gw, gh = dest['grip']
            # Independent rectangular hit oracle (no call to production test).
            for x, y in ((gx,gy), (gx+gw,gy), (gx,gy+gh)):
                expected = (abs(x-gx)<=gw*.5 and abs(y-gy)<=gh*.5 and
                            all((x-bx)**2+(y-by)**2 > (s*.5)**2
                                for bx,by,s in dest['buttons']))
                assert layout.hit_grip(dest,x,y) == expected
            rx,ry=rng.uniform(0,250),rng.uniform(0,250)
            cursor=layout.normalized_cursor(rx+gx,ry+gy,rx,ry,w,h)
            assert cursor is not None
            assert abs(cursor[0]-gx/w)<1.e-9 and abs(cursor[1]-gy/h)<1.e-9
            checked += 1
        assert checked > n//2, 'Unexpectedly many infeasible layouts'
        return checked

    def invalid_inputs():
        raw=nav_hud_geometry.layout(500, 100, 1)
        for value in (float('nan'), float('inf'), -float('inf')):
            assert layout.shift_layout(raw, 1000, 700,'CUSTOM',custom_x=value) is None
            assert layout.normalized_cursor(value,0,0,0,100,100) is None
        assert layout.shift_layout(raw, 60, 50) is None
        assert hud_scope.matches(None, context) is False
        return 4

    def owner_generation_lifecycle():
        # Independent truth table for owner scope and lifecycle generation.
        from types import SimpleNamespace
        class Ptr:
            def __init__(self, ptr, broken=False):
                self.ptr, self.broken = ptr, broken
            def as_pointer(self):
                if self.broken:
                    raise ReferenceError('area was destroyed')
                return self.ptr
        rng = random.Random(SEED + 20)
        checked = 0
        for _ in range(12000):
            owner_w, owner_a = rng.randint(1, 100000), rng.randint(1, 100000)
            generation = rng.randint(1, 100000)
            # Positive cases, same pointer tuple and generation.
            valid = SimpleNamespace(window=Ptr(owner_w), area=Ptr(owner_a, False))
            valid.area.type = 'VIEW_3D'
            assert hud_scope.modal_owner_valid((owner_w, owner_a), valid, generation, generation)
            # Distinct window/area, a dead RNA pointer, stale/invalid generation
            wrong = SimpleNamespace(window=Ptr(owner_w+1), area=Ptr(owner_a))
            wrong.area.type = 'VIEW_3D'
            assert not hud_scope.modal_owner_valid((owner_w, owner_a), wrong, generation, generation)
            wrong_area = SimpleNamespace(window=Ptr(owner_w), area=Ptr(owner_a+1))
            wrong_area.area.type = 'VIEW_3D'
            assert not hud_scope.modal_owner_valid((owner_w, owner_a), wrong_area, generation, generation)
            dead = SimpleNamespace(window=Ptr(owner_w), area=Ptr(owner_a, True))
            dead.area.type = 'VIEW_3D'
            assert not hud_scope.modal_owner_valid((owner_w, owner_a), dead, generation, generation)
            assert not hud_scope.modal_owner_valid((owner_w, owner_a), valid, generation, generation+1)
            assert not hud_scope.modal_owner_valid((owner_w, owner_a), valid, 0, generation)
            checked += 1
        assert not hud_scope.modal_owner_valid((1, 2), context, True, True)
        return checked

    group('SCOPE_OWNER_ORACLE', scope_case)
    group('DPI_LAYOUT_AND_HIT', layouts)
    group('SEEDED_INTERACTION_FUZZ', seeded_fuzz)
    group('FAIL_CLOSED_INVALID_INPUT', invalid_inputs)
    group('OWNER_GENERATION_LIFECYCLE', owner_generation_lifecycle)
    failed = [r for r in rows if r['status']=='FAIL']
    try:
        observed_scope = hud_scope.context_scope(context)
    except Exception:
        observed_scope = None
    gui_state = 'NOT_TESTED_GUI'
    return {'schema': SCHEMA, 'seed': SEED,
            'status': 'FAIL' if failed else 'PASS_OFFLINE',
            'test_groups': rows, 'group_pass': len(rows)-len(failed),
            'group_total': len(rows),
            'context_status': 'PASS_CONTEXT' if observed_scope is not None else 'BLOCKED_NO_VIEW3D',
            'context_scope': list(observed_scope) if observed_scope else None,
            'hud_enabled': bool(enabled),
            'hud_matches_context': hud_scope.matches(expected_scope, context) if enabled else None,
            'native_gui': gui_state,
            'owner_contract': 'IMMUTABLE_WINDOW_AREA_SCOPE_PLUS_GENERATION',
            'limits': ['Physical mouse clicks, Esc, 1K/2K/4K displays, and Undo/Redo were not tested by this lab',
                       'No Mesh/UV scan, no GPU draw, no modal callbacks invoked']}
