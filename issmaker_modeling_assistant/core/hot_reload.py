"""ISSMAKER no-restart update loader (Blender 4.x / 5.x).

Safe staged reload of the *ISSMAKER add-on only*. Blender scene data,
object mode, edit mesh, source UV, and file on disk are not saved/edited.
Uses bpy.app.timers so a GUI operator has returned before unloading its
own Python class. The Blender MCP add-on is never disabled or touched.
"""
from __future__ import annotations
from ..extension_compat import addon_key

import ast
import datetime
import importlib
import json
import pathlib
import sys
import traceback
import zipfile

import bpy

PACKAGE = addon_key()
STATUS_KEY = 'ISSMAKER_HOT_RELOAD_STATUS_V1'
_PENDING = False


def status():
    result = bpy.app.driver_namespace.get(STATUS_KEY)
    return dict(result) if isinstance(result, dict) else {
        'state':'IDLE','message':'Обновление ещё не запускалось'}


def _status(state, message, **details):
    record = {'state':str(state),'message':str(message)[:500],
              'timestamp':datetime.datetime.now().isoformat(timespec='seconds')}
    record.update(details)
    bpy.app.driver_namespace[STATUS_KEY] = record
    return record


def installed_root():
    module=sys.modules.get(PACKAGE)
    if module is None or not getattr(module,'__file__',None):
        raise RuntimeError('ISSMAKER_NOT_LOADED')
    root=pathlib.Path(module.__file__).resolve().parent
    if root.name != PACKAGE.rsplit('.',1)[-1] or not (root/'__init__.py').is_file():
        raise ValueError('ISSMAKER_UNEXPECTED_PACKAGE_PATH')
    return root


def preflight(root=None):
    """Read-only compile check BEFORE disabling registered tools."""
    root=pathlib.Path(root or installed_root())
    problems=[];count=0
    for f in sorted(root.rglob('*.py')):
        if '__pycache__' in f.parts:continue
        try:
            content=f.read_text(encoding='utf-8-sig')
            compile(content,str(f),'exec',dont_inherit=True)
            count+=1
        except (OSError,UnicodeError,SyntaxError,ValueError) as exc:
            problems.append(f'{f.relative_to(root)}: {type(exc).__name__}: {exc}')
            if len(problems)>12:break
    if problems:raise ValueError('UPDATE_PREFLIGHT_FAILED: '+ ' | '.join(problems)[:900])
    if count<4:raise ValueError('UPDATE_PREFLIGHT_PACKAGE_INCOMPLETE')
    data=ast.parse((root/'__init__.py').read_text(encoding='utf-8-sig'))
    version=None
    for node in data.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='bl_info' for t in node.targets):
            dictionary=ast.literal_eval(node.value)
            version=tuple(dictionary.get('version',()))
            break
    if not version or any(not isinstance(n,int) for n in version):
        raise ValueError('UPDATE_PREFLIGHT_VERSION_INVALID')
    return {'version':version,'modules_checked':count}


def active_sessions():
    """Never interrupt an in-flight drag or Composer field edit."""
    package=sys.modules.get(PACKAGE)
    if package is None:return ['ADDON_UNAVAILABLE']
    from . import active_tools
    report=active_tools.ownership_snapshot(bpy.context)
    result=[]
    for name,value in report.get('owners',{}).items():
        if value.get('active') is False:continue
        if name=='COLLISION':
            # The Collision Inspector enables a passive overlay by default;
            # this is NOT a modal operation. Only live pickers can be blocked.
            from ..tools import collision_inspector as collision
            pickers=bool(getattr(collision,'_PICKER_SESSIONS',{}))
            pickmode=any(bool(getattr(getattr(scene,'collision_inspector',None),
                                      'overlay_pick_mode',False))
                         for scene in bpy.data.scenes)
            if not (pickers or pickmode):continue
        result.append(name)
    ui=sys.modules.get(PACKAGE+'.ui')
    if ui is not None and getattr(ui,'_ACTIVE_RADIAL',None):result.append('RADIAL')
    # Never unload Python classes or GPU resources during an active staged
    # operation: provisional meshes and Undo must stay owned by that operator.
    from . import process_feedback
    task=process_feedback.current()
    if task is not None and task.state=="RUNNING":
        result.append('PROCESS_HUD:'+task.tool)
    return sorted(set(result))


def _backup(root):
    archive_dir=pathlib.Path.home()/'Documents'/'ISSMAKER_Addon_Backups'/'restore_snapshots'
    archive_dir.mkdir(parents=True,exist_ok=True)
    stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    filename=archive_dir/('before_live_reload_'+stamp+'.zip')
    with zipfile.ZipFile(filename,'x',zipfile.ZIP_DEFLATED,compresslevel=4) as out:
        for f in root.rglob('*'):
            if not f.is_file() or '__pycache__' in f.parts or f.suffix in ('.pyc','.tmp','.bak'):
                continue
            out.write(f,arcname=str(pathlib.Path(root.name)/f.relative_to(root)).replace('\\','/'))
    with zipfile.ZipFile(filename,'r') as test:
        if test.testzip() is not None:
            raise RuntimeError('UPDATE_BACKUP_ZIP_CORRUPT')
    return filename


def _scene_inventory():
    """Identity and mode proof, without iterating millions of mesh vertices."""
    objects=tuple(sorted((ob.name,ob.as_pointer(),
                          ob.data.as_pointer() if getattr(ob,'data',None) else 0)
                         for ob in bpy.data.objects))
    selected=tuple(sorted(ob.name for ob in bpy.context.selected_objects))
    active=bpy.context.view_layer.objects.active
    return {'file':bpy.data.filepath,'mode':bpy.context.mode,
            'objects':objects,'selected':selected,
            'active':active.name if active else None,
            'meshes':tuple(sorted((me.name,me.as_pointer()) for me in bpy.data.meshes))}


def _preserve_workspace_tabs():
    names=('ima_suite_tab','ima_openings_tab','ima_collision_tab','ima_wall_tab')
    records=[]
    for scene in bpy.data.scenes:
        values={prop:getattr(scene,prop) for prop in names if hasattr(scene,prop)}
        records.append((scene,values))
    return records


def _restore_workspace_tabs(records):
    for scene,values in records:
        for key,value in values.items():
            if hasattr(scene,key) and getattr(scene,key)!=value:
                setattr(scene,key,value)


def perform_reload():
    """Reload on Blender's main thread. Not safe inside an operator execute."""
    import addon_utils
    root=installed_root()
    pre=preflight(root)
    busy=active_sessions()
    if busy:
        raise RuntimeError('UPDATE_MODAL_SESSION_ACTIVE: '+', '.join(busy)+'. Закройте инструмент (Esc) и повторите.')
    before=_scene_inventory()
    tabs=_preserve_workspace_tabs()
    backup=_backup(root)
    old_module=sys.modules[PACKAGE]
    was_enabled=bool(getattr(old_module,'__addon_enabled__',True))
    if not was_enabled:raise RuntimeError('UPDATE_ADDON_NOT_ENABLED')
    _status('RELOADING','Перезагрузка Python-модулей...',
            target_version='.'.join(map(str,pre['version'])),
            backup=str(backup), checked=pre['modules_checked'])
    # The only mutable state here is the add-on's registration, Blender UI
    # handlers and ephemeral plugin settings. Never use wm.open_mainfile(),
    # script.reload(), bpy.ops.object.mode_set(), or save_as_mainfile().
    addon_utils.disable(PACKAGE,default_set=False)
    if hasattr(bpy.types.Scene,'ima_suite_tab'):
        raise RuntimeError('UPDATE_UNREGISTER_INCOMPLETE')
    for name in tuple(sys.modules):
        if name==PACKAGE or name.startswith(PACKAGE+'.'):
            sys.modules.pop(name,None)
    importlib.invalidate_caches()
    upgraded=addon_utils.enable(PACKAGE,default_set=False)
    if upgraded is None or not getattr(upgraded,'__addon_enabled__',False):
        raise RuntimeError('UPDATE_REENABLE_FAILED')
    if tuple(getattr(upgraded,'ADDON_VERSION',getattr(upgraded,'bl_info',{}).get('version',())))!=tuple(pre['version']):
        raise RuntimeError('UPDATE_VERSION_MISMATCH')
    if not hasattr(bpy.types.Scene,'ima_suite_tab'):
        raise RuntimeError('UPDATE_RNA_REGISTRATION_FAILED')
    if not bpy.ops.ima.launch_tool.get_rna_type():
        raise RuntimeError('UPDATE_RADIAL_LAUNCH_NOT_REGISTERED')
    _restore_workspace_tabs(tabs)
    after=_scene_inventory()
    if before!=after:
        changed=[key for key in before if before[key]!=after[key]]
        raise RuntimeError('UPDATE_SCENE_IDENTITY_CHANGED: '+','.join(changed))
    version='.'.join(map(str,getattr(upgraded,'ADDON_VERSION',getattr(upgraded,'bl_info',{}).get('version',()))))
    _status('SUCCESS','Обновлено без перезапуска: '+version,
            version=version,backup=str(backup),checked=pre['modules_checked'],
            scene_unchanged=True)
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type in {'VIEW_3D','IMAGE_EDITOR','NODE_EDITOR','PREFERENCES'}:
                area.tag_redraw()
    return status()


def _reload_timer():
    global _PENDING
    try:
        result=perform_reload()
        print('ISSMAKER Hot Reload:',result['message'])
    except Exception as exc:
        _status('FAILED',f'{type(exc).__name__}: {str(exc)[:440]}')
        print('ISSMAKER Hot Reload failed:',traceback.format_exc())
    finally:
        _PENDING=False
    return None


def request_reload():
    """Called in an operator; perform actual unload on the next event tick."""
    global _PENDING
    if _PENDING:raise RuntimeError('UPDATE_ALREADY_PENDING')
    pre=preflight()
    busy=active_sessions()
    if busy:
        raise RuntimeError('UPDATE_MODAL_SESSION_ACTIVE: '+', '.join(busy)+'. Закройте инструмент (Esc).')
    _PENDING=True
    _status('QUEUED','Обновление поставлено в очередь...',
            target_version='.'.join(map(str,pre['version'])),checked=pre['modules_checked'])
    try:
        bpy.app.timers.register(_reload_timer,first_interval=.12)
    except Exception:
        _PENDING=False
        raise
    return status()
