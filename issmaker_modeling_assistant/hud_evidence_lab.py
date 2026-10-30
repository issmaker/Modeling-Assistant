"""HUD Pass 06 — in-Blender, explicitly observed UI evidence lab.

Safe by design: creates a SEPARATE scene, never alters existing Mesh data,
never fabricates click events, and never marks manual scenarios PASS itself.
"""
from __future__ import annotations

import ast
import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import tempfile
import traceback
import zipfile

import bpy
from bpy.types import Operator
from .core import hud_evidence_core as _core
from .core import window_visibility

LAB_VERSION = 'HUD_PASS06_4_SCREENSHOT_HOTFIX_1.3'
_SESSION = None
_PENDING_CAPTURE = None
_ERRORS = []
_MAX_SHOTS = 80
_AUTO_JOB = None
_LAST_AUTO = None
_AUTO_KEYS = ('UV_WALL_PATH', 'BRICK_FACE', 'DWOS_WALL', 'COLLISION_SHELLS', 'GENERAL_MESH', 'UV_CHECK')


def _timestamp():
    return _dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _safe_json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _inventory():
    path = Path(__file__).parent / 'assets' / 'hud_lab_inventory.json'
    return json.loads(path.read_text(encoding='utf-8'))


def _fixture_for(name):
    text = str(name).lower()
    if any(s in text for s in ('uv wall', 'стена', 'карниз', 'поверхност', 'studio dock')):
        return 'UV_WALL_PATH'
    if any(s in text for s in ('dwos', 'окна', 'импост', 'стекл')):
        return 'DWOS_WALL'
    if any(s in text for s in ('brick', 'проём')):
        return 'BRICK_FACE'
    if any(s in text for s in ('collision', 'коллиз', 'пересечен')):
        return 'COLLISION_SHELLS'
    if any(s in text for s in ('uv', 'weld', 'сварк')):
        return 'UV_CHECK'
    return 'GENERAL_MESH'


def _build_steps():
    steps = []
    for code in ('1K', '2K', '4K'):
        w, h = _core.PROFILES[code]
        steps.append({'id': 'DISPLAY_' + code, 'name': f'{code} · {w}×{h}',
                      'group': 'RESOLUTION', 'fixture': 'GENERAL_MESH', 'profile': code,
                      'instruction': ('На реальном дисплее с этим разрешением проверьте UI scale, текст, '
                                      'ширину Viewport, кнопки и края окон; снимите экран без изменения масштаба изображения.')})
    for item in _inventory():
        steps.append({'id': item['id'], 'name': item['name'], 'group': 'HUD',
                      'fixture': _fixture_for(item['name']),
                      'source': item['source'],
                      'instruction': item['acceptance'] + '. Откройте указанный HUD, проверьте hover, нажатия и читаемость; снимите экран.'})
    design = [
      ('DESIGN_01','Читаемость текстов в состоянии normal и hover'),
      ('DESIGN_02','Размеры кнопок, полей и межэлементные отступы'),
      ('DESIGN_03','Единая типографика и визуальная иерархия'),
      ('DESIGN_04','Дорогой Dark Architectural стиль без визуального шума'),
      ('DESIGN_05','Читаемость предупреждений и disabled без нарушения цветов ошибок'),
      ('DESIGN_06','Проверка компактной и развёрнутой компоновки'),
    ]
    for code,title in design:
        steps.append({'id':code,'name':title,'group':'DESIGN','fixture':'GENERAL_MESH',
                      'instruction':'Оцените визуал на настоящем экране Blender; приложите скриншот и конкретный комментарий, особенно при FAIL.'})
    chaos = [
      ('CHAOS_01','Открыть и закрыть каждое окно несколько раз','GENERAL_MESH'),
      ('CHAOS_02','Быстрое переключение UV Wall → DWOS → Brick Hole → Collision','GENERAL_MESH'),
      ('CHAOS_03','Esc при редактировании числового значения','BRICK_FACE'),
      ('CHAOS_04','Ctrl+Z / Ctrl+Shift+Z и сохранность активных HUD','UV_WALL_PATH'),
      ('CHAOS_05','Нажатия на край кнопки и за её границами','GENERAL_MESH'),
      ('CHAOS_06','Hover по соседним кнопкам на высокой скорости','GENERAL_MESH'),
      ('CHAOS_07','Многократное открытие dropdown и клик вне окна','DWOS_WALL'),
      ('CHAOS_08','Перетаскивание HUD к краям и углам Viewport','UV_WALL_PATH'),
      ('CHAOS_09','Узкий Viewport и разделение рабочей области','GENERAL_MESH'),
      ('CHAOS_10','Изменение масштаба шрифта и окна HUD','GENERAL_MESH'),
      ('CHAOS_11','Переключение цветовых схем и accent','GENERAL_MESH'),
      ('CHAOS_12','Приоритет Hitbox, перекрытие нескольких HUD','GENERAL_MESH'),
      ('CHAOS_13','Edit/Object: запуск и отмена неподходящего инструмента','BRICK_FACE'),
      ('CHAOS_14','UV Wall: WALL/SURFACE/CORNICE и закрытие окон','UV_WALL_PATH'),
      ('CHAOS_15','DWOS: ONE/SEL/SAME/ALL/COPY и скрытые панели','DWOS_WALL'),
      ('CHAOS_16','Collision: переход между проблемами, ESC и переоткрытие','COLLISION_SHELLS'),
      ('CHAOS_17','Brick Hole: Preview, числовой ввод и Esc','BRICK_FACE'),
      ('CHAOS_18','Открыть одновременно два окна Blender / изменить рабочую область','GENERAL_MESH'),
    ]
    for code, title, fixture in chaos:
        steps.append({'id': code, 'name': title, 'group': 'INTERACTION', 'fixture': fixture,
                      'instruction': 'Выполните сценарий вручную; убедитесь, что каждый клик соответствует рисунку, модель не повреждена, а выход/Undo корректны.'})
    return steps


def _new_mesh(scene, group, name, verts, faces=(), edges=(), location=(0, 0, 0), uv=False):
    me = bpy.data.meshes.new('IMA_LAB_' + name)
    me.from_pydata(verts, edges, faces)
    me.update()
    obj = bpy.data.objects.new('IMA_LAB_' + name, me)
    group.objects.link(obj)
    obj.location = location
    obj['ima_hud_lab_fixture'] = name
    if uv and me.polygons:
        layer = me.uv_layers.new(name='IMA_LAB_UV_1001')
        for poly in me.polygons:
            for loop_idx in poly.loop_indices:
                v = me.vertices[me.loops[loop_idx].vertex_index].co
                layer.data[loop_idx].uv = (float(v.x)*.25, float(v.z)*.25)
    return obj


def _cube(cx, cy, cz, radius):
    verts = [(cx+sx*radius, cy+sy*radius, cz+sz*radius)
             for sx, sy, sz in ((-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),
                                 (-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1))]
    faces = [(0,3,2,1), (4,5,6,7), (0,1,5,4),
             (1,2,6,5), (2,3,7,6), (3,0,4,7)]
    return verts, faces


def _fixtures(scene):
    col = bpy.data.collections.new('ISSMAKER HUD LAB · TEST ONLY')
    scene.collection.children.link(col)
    col['ima_hud_lab_collection'] = str(scene.get('ima_hud_lab_id', ''))
    created = {}
    # Edge-only authored route, including nonuniform Z, with exact original vertices.
    created['UV_WALL_PATH'] = _new_mesh(scene, col, 'UV_WALL_PATH',
        [(0,0,0),(4,0,.25),(4,4,1.0),(8,4,.5)], edges=[(0,1),(1,2),(2,3)], location=(-12,0,0))
    created['BRICK_FACE'] = _new_mesh(scene, col, 'BRICK_FACE',
        [(-1,0,0),(1,0,0),(1,0,2),(-1,0,2)],faces=[(0,1,2,3)],location=(-4,0,0),uv=True)
    # This is a visual opening fixture, not a certified DWOS auto-detection fixture.
    xx=(-2,-.8,.8,2); zz=(0,.8,2.2,3.2)
    verts=[(x,0,z) for z in zz for x in xx]
    faces=[]
    for j in range(3):
        for i in range(3):
            if i==1 and j==1:
                continue
            faces.append((j*4+i,j*4+i+1,(j+1)*4+i+1,(j+1)*4+i))
    created['DWOS_WALL'] = _new_mesh(scene,col,'DWOS_WALL',verts,faces=faces,location=(4,0,0),uv=True)
    # Two intersecting independent closed shells in a single Mesh object.
    v1,f1=_cube(0,0,1,.9)
    v2,f2=_cube(.6,.3,1,.9)
    created['COLLISION_SHELLS'] = _new_mesh(scene,col,'COLLISION_SHELLS',v1+v2,faces=f1+[tuple(8+i for i in f) for f in f2],location=(12,0,0))
    v3,f3=_cube(0,0,1,1)
    created['GENERAL_MESH'] = _new_mesh(scene,col,'GENERAL_MESH',v3,faces=f3,location=(0,7,0),uv=True)
    v4,f4=_cube(0,0,1,1)
    created['UV_CHECK'] = _new_mesh(scene,col,'UV_CHECK',v4,faces=f4,location=(7,7,0),uv=True)
    for key,obj in created.items():
        obj['ima_hud_lab_warning'] = ('UI-only visual fixture, not a validated production case' if key=='DWOS_WALL'
            else 'Non-destructive independent HUD laboratory fixture')
    return created


def _active(context):
    global _SESSION
    if not _SESSION:
        return None
    scene=context.scene
    if scene is None or scene.get('ima_hud_lab_id') != _SESSION['id']:
        return None
    return _SESSION


def state(context):
    session = _active(context)
    if session is None:
        return None
    idx = int(session['index'])
    return {'id': session['id'], 'index': idx, 'current': session['steps'][idx],
            'summary': _core.evidence_summary(session['steps'],session['results']),
            'last_error': session['errors'][-1] if session['errors'] else '',
            'pending_capture': _PENDING_CAPTURE is not None,
            'root': session['root']}


def step_result(context):
    session=_active(context)
    if not session:
        return 'NOT_TESTED'
    current=session['steps'][session['index']]
    return session['results'].get(current['id'],{}).get('status','NOT_TESTED')


def _write_live(session):
    # Best-effort persistent source for reports, never overwrites the source blend.
    try:
        path=Path(session['root']) / 'lab_live.json'
        content={'schema':'ISSMAKER_HUD_LAB_1','id':session['id'],'steps':session['steps'],
                 'results':session['results'],'events':session['events'][-1200:],
                 'summary':_core.evidence_summary(session['steps'],session['results'])}
        temp=path.with_suffix('.tmp')
        temp.write_text(_safe_json(content),encoding='utf-8')
        temp.replace(path)
    except (OSError, ValueError, TypeError) as exc:
        session['errors'].append('LIVE_SAVE: '+repr(exc))


def _event(session, kind, **details):
    session['events'].append({'at':_dt.datetime.now(_dt.timezone.utc).isoformat(), 'type':kind, **details})
    if len(session['events'])>1500:
        del session['events'][:-1500]
    _write_live(session)


def _metrics(context):
    out = {'blender_version':bpy.app.version_string,
           'blender_version_tuple':list(bpy.app.version),
           'python':platform.python_version(), 'os':platform.platform(),
           'blender_background':bool(bpy.app.background),
           'scene':str(getattr(context.scene,'name','')),
           'window':{},'areas':[],'appearance':{}}
    try:
        wnd=context.window
        out['window']={key:int(getattr(wnd,key)) for key in ('width','height') if hasattr(wnd,key)}
        out['screen']=wnd.screen.name
        for a in wnd.screen.areas:
            out['areas'].append({'type':a.type,'x':a.x,'y':a.y,'width':a.width,'height':a.height,
                'regions':[{'type':r.type,'x':r.x,'y':r.y,'width':r.width,'height':r.height} for r in a.regions]})
    except (AttributeError,ReferenceError,RuntimeError) as exc:
        out['window_error']=repr(exc)
    try:
        system=context.preferences.system
        out['dpi']={'ui_scale':getattr(system,'ui_scale',None),
                    'pixel_size':getattr(system,'pixel_size',None),
                    'dpi':getattr(system,'dpi',None)}
        addon=context.preferences.addons.get('issmaker_modeling_assistant')
        pref=addon.preferences if addon else None
        if pref:
            for key in ('ui_scheme','ui_surface','accent','font_scale','window_scale',
                        'button_scale','hud_display_preset','ui_reduced_motion','ui_motion_quality'):
                out['appearance'][key]=getattr(pref,key,None)
    except (AttributeError,ReferenceError,RuntimeError) as exc:
        out['appearance_error']=repr(exc)
    return out


def _window_snapshot(context):
    visible=window_visibility.snapshot_all(active_within=15)
    fixed=window_visibility.fixed_snapshot(active_within=15)
    return {'visible':visible,'fixed':fixed,'candidate_overlaps':_core.potential_collisions(visible,fixed),
            'metrics':_metrics(context)}


def _fixture_geometry_snapshot(session):
    """Small reproducible mesh payload; copies only objects created by the lab."""
    payload={}
    for key,name in session['fixture_names'].items():
        obj=bpy.data.objects.get(name)
        if obj is None or obj.type!='MESH':
            payload[key]={'error':'missing'}
            continue
        mesh=obj.data
        payload[key]={'name':obj.name,'location':list(obj.location),
            'vertices':[list(v.co) for v in mesh.vertices[:1000]],
            'edges':[list(e.vertices) for e in mesh.edges[:1000]],
            'polygons':[list(p.vertices) for p in mesh.polygons[:1000]],
            'uv_layer_names':[layer.name for layer in mesh.uv_layers]}
    return payload


def _auto_checks(context,session):
    """Verifies fixtures and current layout data; does not simulate user clicks."""
    checks=[]
    def check(code, condition, explanation):
        checks.append({'id':code,'status':'PASS' if condition else 'FAIL', 'detail':explanation})
    check('LAB_SCENE_ISOLATED',session['scene']!=session['original_scene']
          and context.scene.get('ima_hud_lab_id')==session['id'],
          'Current scene must be separate from the original scene')
    meshes={key:bpy.data.objects.get(name) for key,name in session['fixture_names'].items()}
    check('ALL_FIXTURES_PRESENT',len(meshes)==6 and all(obj is not None for obj in meshes.values()),
          'Six independent test objects should exist')
    wall=meshes.get('UV_WALL_PATH')
    check('EDGE_PATH',bool(wall and len(wall.data.vertices)==4
          and len(wall.data.edges)==3 and len(wall.data.polygons)==0),
          'Authored path: four points, three edges, no faces')
    dwos=meshes.get('DWOS_WALL')
    check('DWOS_VISUAL_HOLE',bool(dwos and len(dwos.data.polygons)==8),
          'Eight side quads around a central opening; visual example only')
    collision=meshes.get('COLLISION_SHELLS')
    check('COLLISION_TWO_SHELLS',bool(collision and len(collision.data.vertices)==16
          and len(collision.data.polygons)==12),
          'Two closed eight-vertex cube shells combined into one object')
    for key in ('BRICK_FACE','DWOS_WALL','GENERAL_MESH','UV_CHECK'):
        obj=meshes.get(key)
        check('UV_'+key,bool(obj and len(obj.data.uv_layers)>0),
              'UV layer exists; this does not prove texel density or correctness')
    return checks


def _screen_capture(session, step_id):
    if bpy.app.background:
        raise RuntimeError('Blender работает в background; снимок GUI невозможен')
    if len(session['screenshots']) >= _MAX_SHOTS:
        raise RuntimeError('Достигнут лимит снимков лаборатории')
    outdir=Path(session['root']) / 'screenshots'
    outdir.mkdir(parents=True,exist_ok=True)
    name = f'{step_id}_{len(session["screenshots"])+1:03d}.png'
    dest=outdir/name
    try:
        window=next((w for w in bpy.context.window_manager.windows
                     if w.scene and w.scene.get('ima_hud_lab_id')==session['id']),None)
        if window is None:
            raise RuntimeError('Окно тестовой сцены закрыто')
        # Timer callbacks may not have an active Window context.
        with bpy.context.temp_override(window=window,screen=window.screen):
            result=bpy.ops.screen.screenshot('EXEC_DEFAULT',filepath=str(dest),check_existing=False)
    except (AttributeError,RuntimeError,TypeError,ValueError) as exc:
        raise RuntimeError('Blender screenshot failed: '+repr(exc)) from exc
    if 'FINISHED' not in result or not dest.is_file() or dest.stat().st_size < 100:
        raise RuntimeError('Blender screenshot did not produce an image')
    session['screenshots'].append(name)
    result=session['results'].setdefault(step_id,{})
    result.setdefault('screenshots',[]).append(name)
    result.setdefault('screenshot_sha256',{})[name]=hashlib.sha256(dest.read_bytes()).hexdigest()
    # Keep HUD coordinates as observed AT the screenshot time, not after UI closes.
    try:
        with bpy.context.temp_override(window=window,screen=window.screen):
            result['capture_windows']=_window_snapshot(bpy.context)
    except (AttributeError,RuntimeError,ReferenceError,ValueError) as exc:
        session['errors'].append('CAPTURE_WINDOW_SNAPSHOT: '+repr(exc))
    return name


def _delayed_capture(generation, step_id):
    global _PENDING_CAPTURE
    if _PENDING_CAPTURE != (generation,step_id):
        return None
    _PENDING_CAPTURE = None
    session = _SESSION
    if not session or session['id'] != generation:
        return None
    try:
        name = _screen_capture(session,step_id)
        _event(session,'SCREENSHOT',step_id=step_id,file=name)
    except Exception as exc:
        session['errors'].append('CAPTURE: '+repr(exc))
        _event(session,'SCREENSHOT_FAILED',step_id=step_id,error=repr(exc))
    return None


def _select_fixture(context,session,step):
    if context.scene.get('ima_hud_lab_id') != session['id']:
        raise RuntimeError('Сначала переключитесь на сцену HUD Lab')
    target=bpy.data.objects.get(session['fixture_names'][step['fixture']])
    if not target or target.name not in context.scene.objects:
        raise RuntimeError('Тестовый объект отсутствует; исходный проект не изменён')
    if context.mode!='OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for obj in context.view_layer.objects:
        if obj.select_get():obj.select_set(False)
    target.select_set(True)
    context.view_layer.objects.active=target
    _event(session,'FIXTURE_SELECTED',step_id=step['id'],object=target.name)
    return target


class IMA_OT_HudLabResume(Operator):
    bl_idname='ima.hud_lab_resume'
    bl_label='Продолжить HUD Lab'
    bl_description='Вернуться в созданную тестовую сцену без повторной генерации'
    bl_options={'REGISTER'}

    def execute(self,context):
        session=_SESSION
        if not session or context.window is None:
            return {'CANCELLED'}
        scene=bpy.data.scenes.get(session['scene'])
        if scene is None:
            self.report({'ERROR'},'Сцена HUD Lab была удалена')
            return {'CANCELLED'}
        context.window.scene=scene
        scene.ima_suite_tab='DIAGNOSTICS'
        self.report({'INFO'},'HUD Lab: сессия восстановлена')
        return {'FINISHED'}


def pending_session():
    return bool(_SESSION)


def _prepare_lab_handoff(context):
    """Disarm active ISSMAKER inspectors through their native, verified stop paths.

    A user pressing NEW TEST authorizes ending an active inspector, but NOT
    committing or altering geometry, quitting Edit Mode or swallowing failed
    cleanup. The existing switch coordinator owns rollback, timers and HUDs.
    Returns (ok, active_before, diagnostic) for the lab's audit trail.
    """
    from .core import active_tools, action_trace

    def owners():
        return {
            'BRICK_HOLE': bool(active_tools._brick_active()),
            'COLLISION': bool(active_tools._collision_active(context)),
            'UV_WALL': bool(active_tools._wall_active(context)),
            'DWOS': bool(active_tools._dwos_active()),
        }

    try:
        before = owners()
        active_names = tuple(name for name, running in before.items() if running)
        action_trace.record('HUD_LAB_HANDOFF_REQUEST', 'HUD_LAB', ', '.join(active_names) or 'idle')
        ok, reason = active_tools.switch_to('HUD_LAB', context)
        after = owners()
        lingering = tuple(name for name, running in after.items() if running)
        if not ok or lingering:
            detail = (reason or 'Инспектор всё ещё активен')
            if lingering:
                detail += ' | остались: ' + ', '.join(lingering)
            action_trace.record('HUD_LAB_HANDOFF_BLOCKED', 'HUD_LAB', detail)
            return False, active_names, detail
        if context.mode != 'OBJECT':
            detail = 'После закрытия инспектора режим изменился; NEW TEST отменён'
            action_trace.record('HUD_LAB_HANDOFF_BLOCKED', 'HUD_LAB', detail)
            return False, active_names, detail
        action_trace.record('HUD_LAB_HANDOFF_OK', 'HUD_LAB', ', '.join(active_names) or 'idle')
        return True, active_names, ''
    except Exception as exc:
        detail = 'Ошибка штатного закрытия: ' + type(exc).__name__ + ': ' + str(exc)
        action_trace.record('HUD_LAB_HANDOFF_BLOCKED', 'HUD_LAB', detail)
        return False, (), detail


class IMA_OT_HudLabNew(Operator):
    bl_idname='ima.hud_lab_new'
    bl_label='NEW TEST · создать HUD лабораторию'
    bl_description='Создать отдельную тестовую сцену и сценарии, не меняя исходный проект'
    bl_options={'REGISTER'}

    def execute(self,context):
        global _SESSION,_PENDING_CAPTURE
        if bpy.app.background or context.window is None:
            self.report({'ERROR'},'Для HUD Lab нужен Blender с графическим окном')
            return {'CANCELLED'}
        if _SESSION is not None:
            self.report({'WARNING'},'HUD Lab уже существует. Используйте Продолжить HUD Lab.')
            return {'CANCELLED'}
        # Non-destructive guarantee: never leave Edit Mode or commit a preview.
        if context.mode != 'OBJECT':
            self.report({'WARNING'},'Перед NEW TEST завершите текущую операцию и перейдите в Object Mode')
            return {'CANCELLED'}
        # Explicit NEW TEST uses the SAME cleanup contract as switching tools.
        # Merely hiding the GPU draw handler would leave live modal ownership.
        ready, closed_inspectors, reason = _prepare_lab_handoff(context)
        if not ready:
            self.report({'WARNING'},'HUD Lab: '+reason[:160])
            return {'CANCELLED'}
        original=context.window.scene
        scene=None
        try:
            scene=bpy.data.scenes.new('ISSMAKER_HUD_LAB_'+_timestamp())
            scene['ima_hud_lab_id']=_timestamp()+'_'+str(os.getpid())
            scene['ima_hud_lab_original_scene']=original.name
            fixtures=_fixtures(scene)
            root=Path(tempfile.mkdtemp(prefix='ISSMAKER_HUD_LAB_'))
            steps=_build_steps()
            session={'id':scene['ima_hud_lab_id'],'scene':scene.name,'original_scene':original.name,
                     'root':str(root),'steps':steps,'index':0,'fixture_names':{k:v.name for k,v in fixtures.items()},
                     'fixture_collection':next((c.name for c in scene.collection.children if c.get('ima_hud_lab_collection')==scene['ima_hud_lab_id']),None),
                     'results':{},'screenshots':[],'events':[],'errors':[], 'start_metrics':_metrics(context)}
            context.window.scene=scene
            scene.ima_suite_tab='DIAGNOSTICS'
            _SESSION=session
            _PENDING_CAPTURE=None
            _event(session,'LAB_CREATED',scene=scene.name,original_scene=original.name,
                   fixtures=list(session['fixture_names']),manual_steps=len(steps),
                   auto_closed_inspectors=list(closed_inspectors))
            _select_fixture(context,session,steps[0])
            checks=_auto_checks(context,session)
            session['snapshots']=[{'automatic_fixture_checks':checks,'start_window_snapshot':_window_snapshot(context)}]
            _event(session,'AUTOMATED_PREFLIGHT',passes=sum(x['status']=='PASS' for x in checks),
                   failures=[x['id'] for x in checks if x['status']!='PASS'])
            self.report({'INFO'},f'HUD Lab: {len(steps)} сценария, {sum(x["status"]=="PASS" for x in checks)}/{len(checks)} fixture-check PASS; ручные тесты ещё не пройдены')
            return {'FINISHED'}
        except Exception as exc:
            if scene is not None:
                try:
                    if context.window.scene==scene:context.window.scene=original
                    bpy.data.scenes.remove(scene)
                except (ReferenceError,RuntimeError,TypeError):pass
            _SESSION=None
            self.report({'ERROR'},'HUD Lab: '+str(exc)[:180])
            return {'CANCELLED'}


class IMA_OT_HudLabStep(Operator):
    bl_idname='ima.hud_lab_step'
    bl_label='Перейти к тесту HUD'
    bl_options={'INTERNAL'}
    direction:bpy.props.IntProperty(default=1,min=-1,max=1)

    def execute(self,context):
        if _AUTO_JOB is not None:
            self.report({'WARNING'},'AUTO TEST ALL выполняется; дождитесь ZIP')
            return {'CANCELLED'}
        session=_active(context)
        if not session:return {'CANCELLED'}
        session['index']=max(0,min(len(session['steps'])-1, session['index']+int(self.direction)))
        step=session['steps'][session['index']]
        _event(session,'STEP_SELECTED',step_id=step['id'])
        self.report({'INFO'},f'{session["index"]+1}/{len(session["steps"])} · {step["id"]}')
        return {'FINISHED'}


class IMA_OT_HudLabFixture(Operator):
    bl_idname='ima.hud_lab_fixture'
    bl_label='Подготовить выбранный тест'
    bl_description='Выбрать подготовленный объект; не строить стены или окна автоматически'
    bl_options={'REGISTER'}

    def execute(self,context):
        if _AUTO_JOB is not None:
            self.report({'WARNING'},'AUTO TEST ALL выполняется; дождитесь ZIP')
            return {'CANCELLED'}
        session=_active(context)
        if not session:return {'CANCELLED'}
        try:
            _select_fixture(context,session,session['steps'][session['index']])
            return {'FINISHED'}
        except Exception as exc:
            session['errors'].append('FIXTURE: '+repr(exc))
            self.report({'ERROR'},str(exc)[:180]);return {'CANCELLED'}


class IMA_OT_HudLabCapture(Operator):
    bl_idname='ima.hud_lab_capture'
    bl_label='Снимок HUD через 4 секунды'
    bl_description='Даёт время открыть HUD, затем снимает реальный интерфейс Blender'
    bl_options={'REGISTER'}

    def execute(self,context):
        if _AUTO_JOB is not None:
            self.report({'WARNING'},'AUTO TEST ALL выполняется; дождитесь ZIP')
            return {'CANCELLED'}
        global _PENDING_CAPTURE
        session=_active(context)
        if not session:return {'CANCELLED'}
        if _PENDING_CAPTURE is not None:
            self.report({'WARNING'},'Снимок уже запланирован')
            return {'CANCELLED'}
        step=session['steps'][session['index']]
        key=(session['id'],step['id'])
        _PENDING_CAPTURE=key
        try:
            bpy.app.timers.register(lambda: _delayed_capture(*key),first_interval=4.0)
        except (ValueError,RuntimeError) as exc:
            _PENDING_CAPTURE=None
            session['errors'].append('TIMER: '+repr(exc))
            self.report({'ERROR'},'Не удалось запустить таймер снимка')
            return {'CANCELLED'}
        _event(session,'SCREENSHOT_SCHEDULED',step_id=step['id'])
        self.report({'INFO'},'Откройте HUD в Viewport: снимок экрана через 4 секунды')
        return {'FINISHED'}


class IMA_OT_HudLabMark(Operator):
    bl_idname='ima.hud_lab_mark'
    bl_label='Зафиксировать результат теста'
    bl_options={'REGISTER'}
    status:bpy.props.EnumProperty(items=(('PASS','PASS',''),('FAIL','FAIL',''),
                                    ('BLOCKED','BLOCKED',''),('NOT_TESTED','Не проверено','')))

    def execute(self,context):
        if _AUTO_JOB is not None:
            self.report({'WARNING'},'AUTO TEST ALL выполняется; дождитесь ZIP')
            return {'CANCELLED'}
        session=_active(context)
        if not session:return {'CANCELLED'}
        step=session['steps'][session['index']]
        evidence=session['results'].setdefault(step['id'],{})
        new_status=self.status
        if new_status=='PASS' and not evidence.get('screenshots'):
            self.report({'WARNING'},'Для PASS сначала снимите реальный HUD; без снимка статус не изменён')
            return {'CANCELLED'}
        if new_status=='PASS' and step.get('profile'):
            try:
                metrics=_metrics(context)['window']
                if not _core.profile_fit(metrics.get('width'),metrics.get('height'),step['profile']):
                    self.report({'WARNING'},'Размер Blender окна не соответствует профилю; нельзя поставить PASS')
                    return {'CANCELLED'}
            except Exception as exc:
                self.report({'WARNING'},'Не удалось подтвердить размер Blender окна: '+str(exc)[:70])
                return {'CANCELLED'}
        evidence['status']=new_status
        evidence['note']=str(getattr(context.scene,'ima_hud_lab_note',''))[:1000]
        context.scene.ima_hud_lab_note=''
        evidence['at']=_dt.datetime.now(_dt.timezone.utc).isoformat()
        evidence['metrics']=_metrics(context)
        evidence['windows']=_window_snapshot(context)
        try:
            from .core import action_trace
            events=action_trace.snapshot()
            evidence['action_trace']=list(events)[-100:] if isinstance(events,(list,tuple)) else events
        except (AttributeError,RuntimeError,TypeError,ValueError) as exc:
            session['errors'].append('ACTION_TRACE: '+repr(exc))
        _event(session,'MANUAL_RESULT',step_id=step['id'],status=new_status)
        self.report({'INFO'},f'{step["id"]}: {new_status} (подтверждено пользователем, не автотест)')
        return {'FINISHED'}


class IMA_OT_HudLabAudit(Operator):
    bl_idname='ima.hud_lab_audit'
    bl_label='Снять состояние всех HUD'
    bl_description='Снимок реальных размеров областей и зарегистрированных окон; без эмуляции кликов'
    bl_options={'REGISTER'}

    def execute(self,context):
        if _AUTO_JOB is not None:
            self.report({'WARNING'},'AUTO TEST ALL выполняется; дождитесь ZIP')
            return {'CANCELLED'}
        session=_active(context)
        if not session:return {'CANCELLED'}
        try:
            snap=_window_snapshot(context)
            snap['automatic_fixture_checks']=_auto_checks(context,session)
            session.setdefault('snapshots',[]).append(snap)
            if len(session['snapshots'])>120:session['snapshots'].pop(0)
            _event(session,'WINDOW_AUDIT',candidate_overlaps=len(snap['candidate_overlaps']))
            self.report({'INFO'},f'Аудит: {len(snap["candidate_overlaps"])} возможных наложений (проверить по скриншоту)')
            return {'FINISHED'}
        except Exception as exc:
            session['errors'].append('WINDOW_AUDIT: '+repr(exc))
            return {'CANCELLED'}



def _write_evidence_zip(context, session, dest):
    """Shared real exporter for manual and one-click flows; never invents GUI PASS."""
    _event(session,'EXPORT_STARTED')
    report={'lab_version':LAB_VERSION,'created':session['id'],'lab_scene':session['scene'],
            'fixture_names':session['fixture_names'],
            'start_metrics':session['start_metrics'],'current_metrics':_metrics(context),
            'manual_results':session['results'],
            'summary':_core.evidence_summary(session['steps'],session['results']),
            'screenshots':session['screenshots'], 'errors':session['errors'],
            'window_audits':session.get('snapshots',[]),
            'final_window_snapshot':_window_snapshot(context),
            'automatic_fixture_checks':_auto_checks(context,session),
            'auto_test':session.get('auto_test'),
            'auto_provenance':('AUTO checks inspect the laboratory fixtures, installed source and Blender runtime, '
              'not genuine tool button events, tool correctness or all physical display profiles.'),
            'provenance':('Manual PASS reflects the operator selected by the user and available screenshot; '
                          'it is NOT automated proof that pointer clicks or mesh operations succeeded. '
                          'Tests left untouched stay NOT_TESTED.'),
            'blender_runtime_source':'Captured inside Blender GUI; visual acceptance still requires review.'}
    with zipfile.ZipFile(dest,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        z.writestr('README.txt',
            'ISSMAKER HUD Pass06 Evidence Lab\n'
            'Tests were performed in a user-controlled Blender GUI, not by ChatGPT.\n'
            'MANUAL PASS = observer assessment, independent review required.\n'
            'Please review screenshots and audit_logs before accepting a release.\n'
            'Bundle may contain system paths and screenshots. Review before sharing.\n')
        z.writestr('evidence/report.json',_safe_json(report))
        z.writestr('evidence/scenarios.json',_safe_json(session['steps']))
        if session.get('auto_test'):
            z.writestr('evidence/AUTO_TEST_ALL.json',_safe_json(session['auto_test']))
        z.writestr('evidence/events.json',_safe_json(session['events']))
        z.writestr('evidence/overlay_owners.json',_safe_json(_inventory()))
        try:
            from .core import action_trace
            z.writestr('evidence/action_trace.json',_safe_json(action_trace.snapshot()))
        except Exception as exc:
            z.writestr('evidence/action_trace_error.txt',repr(exc))
        z.writestr('evidence/test_fixture_geometry.json',_safe_json(_fixture_geometry_snapshot(session)))
        try:
            from .diagnostics import collect_snapshot
            z.writestr('evidence/issmaker_diagnostics.json',_safe_json(collect_snapshot(context)))
        except Exception as exc:
            session['errors'].append('DIAGNOSTICS: '+repr(exc))
            z.writestr('evidence/diagnostics_error.txt',traceback.format_exc()[-3000:])
        for name in session['screenshots'][:_MAX_SHOTS]:
            f=Path(session['root'])/'screenshots'/name
            if f.is_file() and f.stat().st_size <= 32*1024*1024:
                z.write(f,'screenshots/'+name)
            elif not f.is_file():
                z.writestr('missing_'+name+'.txt','File missing; screenshot NOT VERIFIED')
            else:
                z.writestr('omitted_'+name+'.txt','Screenshot exceeds 32 MiB bundle limit; excluded and NOT VERIFIED')
    # Integrity self-check: actual archive members must be readable.
    with zipfile.ZipFile(dest) as z:
        invalid=z.testzip()
        if invalid:raise RuntimeError('CRC mismatch in '+invalid)
    return dest

class IMA_OT_HudLabExport(Operator):
    bl_idname='ima.hud_lab_export'
    bl_label='Собрать HUD Evidence ZIP'
    bl_description='ZIP со скриншотами, действиями, фактическим DPI, результатами тестов и диагностикой ISSMAKER'
    bl_options={'REGISTER'}

    def execute(self,context):
        if _AUTO_JOB is not None:
            self.report({'WARNING'},'AUTO TEST ALL выполняется; дождитесь ZIP')
            return {'CANCELLED'}
        session=_active(context)
        if not session:return {'CANCELLED'}
        dest=Path(session['root'])/f'ISSMAKER_HUD_EVIDENCE_{session["id"]}.zip'
        try:
            _write_evidence_zip(context,session,dest)
            context.window_manager.clipboard=str(dest)
            self.report({'INFO'},'ZIP готов, путь скопирован: '+str(dest)[-150:])
            return {'FINISHED'}
        except Exception as exc:
            session['errors'].append('EXPORT: '+repr(exc))
            self.report({'ERROR'},'HUD ZIP: '+repr(exc)[:180])
            return {'CANCELLED'}




def _auto_record(report, code, status, detail, *, domain='RUNTIME'):
    if status not in ('PASS', 'FAIL', 'WARN', 'NOT_TESTED'):
        raise ValueError('Invalid automatic check status')
    report['checks'].append({'id':code,'domain':domain,'status':status,'detail':detail})


def _auto_probe(context, session):
    """Observe installed package and actual Blender scene without touching user meshes.

    Assertions here establish only the named narrow invariant.  They do not
    invoke dangerous modal modeling operators or synthesize pointer clicks.
    """
    from .core.overlay_placement import StrictOverlayAllocator, overlap_area
    report={'schema':'ISSMAKER_HUD_AUTO_1','source':'IN_BLENDER_LIVE_AUTO_AUDIT',
            'started':_dt.datetime.now(_dt.timezone.utc).isoformat(),
            'checks':[],'fixture_observations':[],'virtual_profile_tests':[],
            'actual_screen_observation':None,
            'gui_interactions':'NOT_TESTED', 'real_gpu_visual_quality':'NOT_TESTED',
            'mesh_builders':'NOT_TESTED', 'overall_release':'NOT_VERIFIED',
            'limitations':[
                'No fake GUI mouse clicks or fake Hover/Undo/Redo PASS',
                'Only current Blender window resolution is observed: 1K/2K/4K are not changed on the operating system',
                'Six fixture screenshots do not prove 27 tool HUDs were opened or visually approved',
                'No production UV Wall, DWOS, Brick Hole or Collision operations are executed',
            ]}
    for row in _auto_checks(context,session):
        _auto_record(report,row['id'],row['status'],row['detail'],domain='FIXTURE')
    metrics=_metrics(context)
    report['actual_screen_observation']=metrics
    for profile in _core.PROFILES:
        w,h=_core.PROFILES[profile]
        _auto_record(report,'DISPLAY_'+profile+'_REAL',
            'PASS' if _core.profile_fit(metrics['window'].get('width'),metrics['window'].get('height'),profile) else 'NOT_TESTED',
            'Matches current Blender-window bounds; does not prove monitor native resolution or visual quality' if
            _core.profile_fit(metrics['window'].get('width'),metrics['window'].get('height'),profile) else
            'Different physical resolution was not actually tested',domain='REAL_SCREEN')
    regions=[(a['width'],a['height']) for a in metrics['areas'] if a['type']=='VIEW_3D' and a['width']>0 and a['height']>0]
    _auto_record(report,'VIEW3D_AREA_PRESENT','PASS' if regions else 'FAIL',
        {'view3d_area_sizes':regions},domain='BLENDER_GUI')
    snap=_window_snapshot(context)
    session.setdefault('snapshots',[]).append(snap)
    report['registered_window_snapshot']=snap
    candidates=snap['candidate_overlaps']
    visible_count=len(snap.get('visible') or {})
    overlap_status = ('NOT_TESTED' if visible_count == 0 else 'WARN' if candidates else 'PASS')
    _auto_record(report,'REGISTERED_PEER_OVERLAPS',overlap_status,
        {'visible_hud_count':visible_count,'candidate_overlaps':len(candidates),
         'interpretation':('No registered HUD visible; cannot confirm non-overlap' if visible_count == 0
            else 'Checked visible registered HUD only; candidates require independent review; other HUD NOT TESTED')},
        domain='BLENDER_GUI')
    # Actual filesystem / source checks: source is present, but this is not HUD execution.
    pkg=Path(__file__).resolve().parent
    parse_cache={}
    entries=_inventory()
    for item in entries:
        filename=item['source']
        filepath=(pkg/filename).resolve()
        try:
            if not filepath.is_relative_to(pkg):
                raise RuntimeError('Source outside installed addon root')
            if filename not in parse_cache:
                ast.parse(filepath.read_text(encoding='utf8'),filename=str(filepath))
                parse_cache[filename]='OK'
            result='PASS'
            detail={'source':filename,'source_ast':'PARSED',
                    'hud_rendered':'NOT_TESTED','clicks':'NOT_TESTED'}
        except Exception as exc:
            result='FAIL';detail={'source':filename,'error':repr(exc)}
        _auto_record(report,'HUD_SOURCE_'+item['id'],result,detail,domain='SOURCE_ONLY')
    # Use the installed integrity manifest, not values invented at build time.
    wall=pkg/'tools'/'uv_wall'/'UV_Wall_Professional'
    try:
        manifest=json.loads((wall/'release_integrity_200.json').read_text(encoding='utf8'))
        entries=manifest['sha256']
        mismatches=[]
        for rel,expected in sorted(entries.items()):
            path=(wall/rel).resolve()
            if not path.is_relative_to(wall.resolve()) or not path.is_file():
                mismatches.append({'file':rel,'error':'MISSING_OR_UNSAFE_PATH'})
            elif hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                mismatches.append({'file':rel,'error':'SHA256_MISMATCH'})
        _auto_record(report,'UV_WALL_INTEGRITY','PASS' if not mismatches else 'FAIL',
            {'checked_files':len(entries),'mismatches':mismatches},domain='INTEGRITY')
    except Exception as exc:
        _auto_record(report,'UV_WALL_INTEGRITY','FAIL',repr(exc),domain='INTEGRITY')
    # Strict allocator simulator: real library, synthetic dimensions. Explicitly NOT actual screenshots.
    for name,(w,h) in list(_core.PROFILES.items())+[('SMALL',(900,540)),('TINY',(640,360))]:
        alloc=StrictOverlayAllocator(w,h,margin=8,gap=6)
        allocated=[];failure=None
        sizes=((310,210),(400,300),(240,150),(180,90),(350,240),(190,70))
        for i,(pw,ph) in enumerate(sizes):
            rect=alloc.place('test_'+str(i),(pw,ph),(w-pw-8,h-ph-8))
            if rect is None:
                allocated.append({'key':str(i),'status':'NO_ROOM'})
                continue
            if not (rect[0]>=8 and rect[1]>=8 and rect[2]<=w-8+1e-5 and rect[3]<=h-8+1e-5):
                failure='OUT_OF_BOUNDS'
            if any(overlap_area(rect,prev['rect'])>0 for prev in allocated if prev.get('rect') is not None):
                failure='OVERLAP'
            allocated.append({'key':str(i),'rect':list(rect),'status':'PLACED'})
        report['virtual_profile_tests'].append({'profile':name,'viewport':[w,h],
            'placed':sum(x['status']=='PLACED' for x in allocated),
            'no_room':sum(x['status']=='NO_ROOM' for x in allocated),
            'failure':failure,'result':'FAIL' if failure else 'PASS',
            'scope':'ALGORITHM_SIMULATION_NOT_REAL_MONITOR'})
        _auto_record(report,'VIRTUAL_LAYOUT_'+name,'FAIL' if failure else 'PASS',
            {'simulation':True,'failure':failure,'no_room':sum(x['status']=='NO_ROOM' for x in allocated)},
            domain='SIMULATED_LAYOUT')
    return report


def _auto_output_destination(session):
    """Use the user's Downloads directory if possible; never overwrite existing evidence."""
    base=Path.home()/'Downloads'/'ISSMAKER_Diagnostics'
    try:
        base.mkdir(parents=True,exist_ok=True)
    except OSError:
        base=Path(session['root'])
    digest=hashlib.sha256(session['id'].encode('utf8')).hexdigest()[:8]
    filename=f'ISSMAKER_HUD_AUTO_{_timestamp()}_{digest}.zip'
    candidate=base/filename
    index=1
    while candidate.exists():
        candidate=base/(filename[:-4]+f'_{index:02d}.zip')
        index+=1
    return candidate


def _auto_cleanup_session(job, session):
    """Restore the original Blender scene; delete *only* lab-owned, tagged data.

    Report errors rather than hiding cleanup problems. Avoid touching the original
    scene's objects or mesh data even if the lab was interrupted.
    """
    global _SESSION, _PENDING_CAPTURE
    if not job.get('created_lab'):
        # Existing manually created lab remains available for further review.
        return 'OK'
    win=job['window']
    original=bpy.data.scenes.get(session['original_scene'])
    lab=bpy.data.scenes.get(session['scene'])
    if original is None:
        return 'ORIGINAL_SCENE_MISSING: lab left in place'
    try:
        if lab is not None:
            for w in bpy.context.window_manager.windows:
                if w.scene==lab:
                    w.scene=original
    except Exception as exc:
        return 'RESTORE_FAILED: '+repr(exc)
    issues=[]
    if job.get('created_lab') and lab is not None:
        try:
            # Any data removed here has been tagged and created by this lab only.
            meshes=[]
            for key,name in session['fixture_names'].items():
                obj=bpy.data.objects.get(name)
                if obj is None:
                    continue
                if obj.get('ima_hud_lab_fixture')!=key:
                    issues.append('UNEXPECTED_TAG: '+name)
                    continue
                mesh=obj.data
                bpy.data.objects.remove(obj,do_unlink=True)
                if mesh is not None and mesh.name.startswith('IMA_LAB_') and mesh.users==0:
                    meshes.append(mesh)
            bpy.data.scenes.remove(lab)
            col_name=session.get('fixture_collection')
            if col_name:
                col=bpy.data.collections.get(col_name)
                if (col is not None and
                    col.get('ima_hud_lab_collection')==session['id'] and col.users==0):
                    bpy.data.collections.remove(col)
            for mesh in meshes:
                if mesh.users==0:
                    bpy.data.meshes.remove(mesh)
        except Exception as exc:
            issues.append('LAB_CLEANUP_FAILED: '+repr(exc))
    _SESSION=None
    _PENDING_CAPTURE=None
    return 'OK' if not issues else ' | '.join(issues)


def auto_state():
    """Transient user-visible progress/result; a new run does not erase old ZIP."""
    if _AUTO_JOB is not None:
        return {'status':'RUNNING','progress':_AUTO_JOB['index'],
                'total':len(_AUTO_KEYS),'detail':_AUTO_JOB.get('detail','')}
    return _LAST_AUTO




def _auto_snapshot_views(window):
    """Store original View3D framing; restoring avoids disturbing the user's UI."""
    views=[]
    for area in window.screen.areas:
        if area.type!='VIEW_3D':
            continue
        try:
            rv=area.spaces.active.region_3d
            views.append((area,tuple(rv.view_location),tuple(rv.view_rotation),
                          float(rv.view_distance),str(rv.view_perspective)))
        except (AttributeError,ReferenceError,RuntimeError):
            continue
    return views


def _auto_restore_views(job):
    failures=[]
    for area,loc,rot,dist,persp in job.get('saved_views',()):
        try:
            rv=area.spaces.active.region_3d
            rv.view_location=loc
            rv.view_rotation=rot
            rv.view_distance=dist
            rv.view_perspective=persp
            area.tag_redraw()
        except (AttributeError,ReferenceError,RuntimeError,TypeError) as exc:
            failures.append(repr(exc))
    return failures


def _auto_frame_selected(window):
    """Use Blender's actual view operator, but never claim it tested a tool HUD."""
    for area in window.screen.areas:
        if area.type!='VIEW_3D':
            continue
        region=next((r for r in area.regions if r.type=='WINDOW'),None)
        if region is None:
            continue
        try:
            with bpy.context.temp_override(window=window,screen=window.screen,
                                           area=area,region=region):
                result=bpy.ops.view3d.view_selected('EXEC_DEFAULT',use_all_regions=False)
                return 'FRAMED' if 'FINISHED' in result else 'NOT_AVAILABLE'
        except Exception as exc:
            return 'ERROR: '+repr(exc)
    return 'NO_VIEW3D'

def _auto_finish(job, fatal=None):
    """Always attempt an evidence ZIP, even when a screenshot or audit failed."""
    global _AUTO_JOB, _LAST_AUTO
    session=_SESSION
    if session is None or session['id']!=job['id']:
        _AUTO_JOB=None
        _LAST_AUTO={'status':'FAIL','detail':'Сессия была закрыта до завершения автотеста'}
        return None
    report=session.get('auto_test') or {'schema':'ISSMAKER_HUD_AUTO_1','checks':[],
        'fixture_observations':[],'gui_interactions':'NOT_TESTED',
        'real_gpu_visual_quality':'NOT_TESTED','overall_release':'NOT_VERIFIED'}
    session['auto_test']=report
    if fatal:
        session['errors'].append('AUTO_TEST_FATAL: '+str(fatal))
        _auto_record(report,'AUTO_PIPELINE','FAIL',str(fatal),domain='LAB_RUNTIME')
    report['finished']=_dt.datetime.now(_dt.timezone.utc).isoformat()
    report['summary']={status:sum(1 for row in report['checks'] if row['status']==status)
                       for status in ('PASS','FAIL','WARN','NOT_TESTED')}
    report['manual_scenarios_not_verified']=_core.evidence_summary(session['steps'],session['results'])['counts']['NOT_TESTED']
    dest=_auto_output_destination(session)
    try:
        with bpy.context.temp_override(window=job['window'],screen=job['window'].screen):
            _event(session,'AUTO_FINALIZE',report_summary=report['summary'],destination=str(dest))
            # Record scene data before it is deleted and with the lab still active.
            _write_evidence_zip(bpy.context,session,dest)
        job['output']=str(dest)
    except Exception as exc:
        session['errors'].append('AUTO_EXPORT_FAILED: '+repr(exc))
        job['output']=''
        fatal='ZIP_ERROR: '+repr(exc)
    restore_errors=_auto_restore_views(job)
    if restore_errors:
        fatal=(str(fatal)+' | ' if fatal else '')+'VIEW_RESTORE: '+repr(restore_errors)
    status=_auto_cleanup_session(job,session)
    if status!='OK':
        fatal=(str(fatal)+' | ' if fatal else '')+status
    try:
        if job.get('output'):
            bpy.context.window_manager.clipboard=job['output']
    except Exception as exc:
        fatal=(str(fatal)+' | ' if fatal else '')+'CLIPBOARD: '+repr(exc)
    if fatal:
        out_status='FAIL'
    elif report['summary'].get('FAIL'):
        out_status='PARTIAL'
    else:
        out_status='COMPLETE_WITH_UNVERIFIED_GUI'
    _LAST_AUTO={'status':out_status,'detail':str(fatal or 'ZIP готов; путь скопирован в буфер обмена'),
                'path':job.get('output',''),'summary':report['summary'],
                'screenshots':len(session['screenshots']),
                'not_tested':report['manual_scenarios_not_verified']}
    _AUTO_JOB=None
    try:
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                if a.type=='VIEW_3D':
                    a.tag_redraw()
    except (RuntimeError,ReferenceError,AttributeError):
        pass
    print('[ISSMAKER HUD AUTO]',out_status,'ZIP',job.get('output',''), 'detail',fatal or '')
    return None


def _auto_tick(generation):
    """Blender main-thread incremental UI observation; no synthetic mouse events."""
    job=_AUTO_JOB
    if not job or job['id']!=generation:
        return None
    session=_SESSION
    if session is None or session['id']!=generation:
        return _auto_finish(job,'SESSION_CHANGED')
    if bpy.app.background:
        return _auto_finish(job,'BACKGROUND_MODE')
    try:
        win=job['window']
        if not any(w == win for w in bpy.context.window_manager.windows):
            return _auto_finish(job,'BLENDER_WINDOW_CLOSED')
        lab_scene=bpy.data.scenes.get(session['scene'])
        if lab_scene is None or win.scene!=lab_scene:
            return _auto_finish(job,'LAB_SCENE_CHANGED_BY_USER')
        with bpy.context.temp_override(window=win,screen=win.screen):
            if job['phase']=='PREFLIGHT':
                report=_auto_probe(bpy.context,session)
                session['auto_test']=report
                _event(session,'AUTO_PREFLIGHT_DONE',checks=len(report['checks']))
                job['phase']='CAPTURES'
                job['detail']='Подготовка скриншотов Blender'
                return 0.5
            if job['phase']=='CAPTURES':
                i=job['index']
                if i >= len(_AUTO_KEYS):
                    return _auto_finish(job)
                key=_AUTO_KEYS[i]
                if job.get('prepared') != key:
                    _select_fixture(bpy.context,session,{'id':'AUTO_'+key,'fixture':key})
                    framing=_auto_frame_selected(win)
                    session['auto_test']['fixture_observations'].append(
                        {'fixture':key,'stage':'FRAME_VIEW','result':framing})
                    job['prepared']=key
                    job['detail']=f'Реальный скриншот: {key} ({i+1}/{len(_AUTO_KEYS)})'
                    return 0.4
                observation={'fixture':key,'at':_dt.datetime.now(_dt.timezone.utc).isoformat(),
                             'source':'ACTUAL_BLENDER_GUI_SCREENSHOT',
                             'tool_specific_hud_opened':'NOT_TESTED',
                             'mouse_hover_click':'NOT_TESTED'}
                try:
                    name=_screen_capture(session,'AUTO_'+key)
                    observation['screenshot']=name
                    observation['status']='CAPTURED'
                    _auto_record(session['auto_test'],'SCREENSHOT_'+key,'PASS',
                        {'screenshot':name,'scope':'CURRENT_BLEND_GUI_NOT_TOOL_INTERACTIONS'},
                        domain='GUI_SCREENSHOT')
                    _event(session,'AUTO_SCREENSHOT',fixture=key,file=name)
                except Exception as exc:
                    observation['status']='CAPTURE_FAILED'
                    observation['error']=repr(exc)
                    _auto_record(session['auto_test'],'SCREENSHOT_'+key,'FAIL',repr(exc),domain='GUI_SCREENSHOT')
                    session['errors'].append('AUTO_CAPTURE_'+key+': '+repr(exc))
                session['auto_test']['fixture_observations'].append(observation)
                job['index']+=1
                job['prepared']=None
                return 0.3
    except Exception as exc:
        return _auto_finish(job,'AUTO_TICK: '+traceback.format_exc()[-1200:])
    return _auto_finish(job,'UNKNOWN_PHASE')


class IMA_OT_HudLabAutoAll(Operator):
    bl_idname='ima.hud_lab_auto_all'
    bl_label='AUTO TEST ALL · один клик → ZIP'
    bl_description='Создаёт сцену, автоматически проверяет всё безопасно доступное, снимает GUI и формирует ZIP в Загрузках; реальные клики и дизайн помечает NOT TESTED'
    bl_options={'REGISTER'}

    def execute(self,context):
        global _AUTO_JOB,_LAST_AUTO
        if _AUTO_JOB is not None:
            self.report({'WARNING'},'AUTO TEST ALL уже выполняется')
            return {'CANCELLED'}
        if _PENDING_CAPTURE is not None:
            self.report({'WARNING'},'Дождитесь завершения ручного снимка HUD')
            return {'CANCELLED'}
        if bpy.app.background or context.window is None or context.mode!='OBJECT':
            self.report({'ERROR'},'Нужен Blender с окном, Object Mode и завершённым редактированием')
            return {'CANCELLED'}
        created=False
        if _SESSION is None:
            try:
                response=bpy.ops.ima.hud_lab_new('EXEC_DEFAULT')
            except Exception as exc:
                self.report({'ERROR'},'Не удалось создать лабораторию: '+str(exc)[:130])
                return {'CANCELLED'}
            if 'FINISHED' not in response:
                self.report({'ERROR'},'NEW TEST не завершился; см. сообщение Blender')
                return {'CANCELLED'}
            created=True
        else:
            if _active(context) is None:
                self.report({'WARNING'},'Лаборатория уже создана в другой сцене. Нажмите Продолжить HUD Lab.')
                return {'CANCELLED'}
        session=_active(context)
        if session is None:
            self.report({'ERROR'},'Созданная лаборатория не активна')
            return {'CANCELLED'}
        job={'id':session['id'],'window':context.window,'created_lab':created,
             'phase':'PREFLIGHT','index':0,'prepared':None,'detail':'Автоматический аудит среды',
            'saved_views':_auto_snapshot_views(context.window)}
        _AUTO_JOB=job
        _LAST_AUTO=None
        try:
            _event(session,'AUTO_TEST_ALL_STARTED',one_click=True)
            bpy.app.timers.register(lambda: _auto_tick(job['id']),first_interval=0.25)
        except Exception as exc:
            _AUTO_JOB=None
            if created:
                cleanup=_auto_cleanup_session(job,session)
                if cleanup!='OK':
                    print('[ISSMAKER HUD AUTO] cleanup after timer registration failed:',cleanup)
            self.report({'ERROR'},'Не удалось запустить AUTO TEST: '+repr(exc)[:120])
            return {'CANCELLED'}
        self.report({'INFO'},'AUTO TEST ALL запущен: ZIP появится в Загрузки/ISSMAKER_Diagnostics; Blender должен оставаться открытым')
        return {'FINISHED'}

class IMA_OT_HudLabExit(Operator):
    bl_idname='ima.hud_lab_exit'
    bl_label='Вернуться к рабочей сцене'
    bl_description='Вернуть исходную сцену, оставив лабораторию и собранные материалы без удаления'
    bl_options={'REGISTER'}

    def execute(self,context):
        if _AUTO_JOB is not None:
            self.report({'WARNING'},'AUTO TEST ALL выполняется; дождитесь ZIP')
            return {'CANCELLED'}
        global _SESSION,_PENDING_CAPTURE
        session=_active(context)
        if not session:return {'CANCELLED'}
        original=bpy.data.scenes.get(session['original_scene'])
        if original is None:
            self.report({'WARNING'},'Исходная сцена не найдена; лаборатория остаётся открытой')
            return {'CANCELLED'}
        _event(session,'EXIT_LAB')
        _PENDING_CAPTURE=None
        context.window.scene=original
        _SESSION=None
        self.report({'INFO'},'Исходная сцена восстановлена. Тестовая сцена и ZIP сохранены.')
        return {'FINISHED'}


CLASSES=(IMA_OT_HudLabResume,IMA_OT_HudLabNew,IMA_OT_HudLabAutoAll,IMA_OT_HudLabStep,IMA_OT_HudLabFixture,
         IMA_OT_HudLabCapture,IMA_OT_HudLabMark,IMA_OT_HudLabAudit,
         IMA_OT_HudLabExport,IMA_OT_HudLabExit)


def register():
    from . import runtime
    runtime.register_classes_atomic(CLASSES)
    try:
        if hasattr(bpy.types.Scene,'ima_hud_lab_note'):
            raise RuntimeError('Scene.ima_hud_lab_note already registered')
        bpy.types.Scene.ima_hud_lab_note=bpy.props.StringProperty(name='Проверка HUD: комментарий',default='',options={'SKIP_SAVE'})
    except Exception:
        runtime.unregister_classes_safe(CLASSES)
        raise


def unregister():
    global _SESSION,_PENDING_CAPTURE,_AUTO_JOB
    _PENDING_CAPTURE=None
    _AUTO_JOB=None
    if _SESSION:
        old=bpy.data.scenes.get(_SESSION.get('original_scene',''))
        lab=bpy.data.scenes.get(_SESSION.get('scene',''))
        if old is not None and lab is not None:
            try:
                for window in bpy.context.window_manager.windows:
                    if window.scene == lab:
                        window.scene = old
            except (ReferenceError,RuntimeError,AttributeError):
                pass
    _SESSION=None
    from . import runtime
    if hasattr(bpy.types.Scene,'ima_hud_lab_note'):
        del bpy.types.Scene.ima_hud_lab_note
    runtime.unregister_classes_safe(CLASSES)
