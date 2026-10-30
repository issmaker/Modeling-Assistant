"""Contact-driven automatic DWOS Interior reconciliation.

All Blender dependency-graph callbacks only enqueue changes. Exact geometry,
validation, and transactional Mesh publication occur in a debounced main-thread
one-shot timer; never in a depsgraph/draw callback. The timer is not periodic.
"""
from __future__ import annotations

import bpy
import time
from bpy.app.handlers import persistent

from . import smart_interior_scene as smart
from .smart_interior_core import collision_graph, contact_graph, connected_components
from .dwos_log import event as log_event, exception as log_exception
from ....core import lifecycle_lease as _lease

_DELAY_SEC = 0.35
_queued = {}              # scene pointer -> {scene, changed set, all flag}
_last_world = {}          # (scene pointer, frame name) -> matrix signature
_last_inventory = {}      # scene pointer -> DWOS Interior identities
_running = False
_registered = False
_LEASE_OWNER = "dwos.smart_interior_queue"
_LEASE_TOKEN = None


def _scene_id(scene):
    return scene.as_pointer()


def _frame_of(obj):
    if obj is None or getattr(obj, 'type', None) != 'MESH':
        return None
    if obj.get('DWOS_role') == 'INTERIOR':
        obj = obj.parent
    if obj is not None and getattr(obj.dwos_window, 'is_window', False):
        return obj
    return None


def _signature(frame):
    """World matrices of the Interior, not just the frame basis, matter."""
    cavity = next((o for o in frame.children
                   if o.type == 'MESH' and o.get('DWOS_role') == 'INTERIOR'), None)
    if cavity is None:
        return None
    return tuple(round(float(v), 9) for row in cavity.matrix_world for v in row)


def schedule(scene, *, changed=(), all_contacts=False, reason='EDIT'):
    """Merge dirty contacts, registering at most one pending timer."""
    if not _registered or not _lease.alive(_LEASE_OWNER, _LEASE_TOKEN) or scene is None or not hasattr(scene, 'dwos'):
        return
    # ASK FIRST is a non-mutating navigation policy. Never schedule a later
    # timer that silently replaces geometry AFTER the user sees the conflict.
    if str(getattr(scene.dwos,"smart_interior_conflict_behavior","ASK_FIRST"))=="ASK_FIRST":
        _queued.pop(_scene_id(scene),None)
        return
    key = _scene_id(scene)
    row = _queued.setdefault(key, {'scene': scene, 'changed': set(), 'all': False, 'reasons': set(), 'last_change': 0.0})
    row['changed'].update(str(name) for name in changed)
    row['all'] |= bool(all_contacts)
    row['reasons'].add(reason)
    row['last_change'] = time.monotonic()
    if not bpy.app.timers.is_registered(_run_queue):
        bpy.app.timers.register(_run_queue, first_interval=_DELAY_SEC)


def queue_snapshot():
    '''Tiny diagnostic snapshot (no Mesh reads or Blender mutations).'''
    return {'registered':bool(_registered),'lease_valid':_lease.alive(_LEASE_OWNER,_LEASE_TOKEN),
            'running':bool(_running),
            'scenes':len(_queued),
            'tasks':[{'scene':str(getattr(row['scene'],'name','UNKNOWN')),
                      'changed':sorted(row['changed']),
                      'all_contacts':bool(row['all']),
                      'reasons':sorted(row['reasons'])}
                     for row in _queued.values()]}


def settings_changed(scene):
    """RNA callbacks: defer Blender Mesh mutation until after property editing."""
    schedule(scene, all_contacts=True, reason='SETTINGS_CHANGED')


def _contact_groups(scene, *, changed=(), all_contacts=False):
    """Return only overlapping or previously joined groups, never unrelated windows.

    A historical link also needs processing after a contact disappears: its
    canonical contour must be restored. No-contact/never-linked windows are
    intentionally excluded, and do not cause Mesh modification or Undo noise.
    """
    interiors = {}
    canonical = {}
    for obj in scene.objects:
        if (obj.type != 'MESH' or obj.get('DWOS_role') != 'INTERIOR' or
                obj.get('DWOS_W58_9_active_group') or
                obj.parent is None or not getattr(obj.parent.dwos_window, 'is_window', False) or
                getattr(obj.parent.dwos_window, 'finalized', False)):
            continue
        name = obj.parent.name
        if name in interiors:
            raise ValueError(f'Duplicate Interior controller ID: {name}')
        interiors[name] = obj
        canonical[name] = smart._world(smart.decode(obj), obj.matrix_world)
    clearance=float(scene.dwos.smart_interior_gap)
    classes={name:str(interiors[name].parent.get('DWOS_adaptive_classification','PLANAR_LEGACY')) for name in interiors}
    pairs = contact_graph(canonical,clearance=clearance,classes=classes)
    edges = {tuple(sorted(pair)) for pair in pairs}
    for name, obj in interiors.items():
        for peer in smart._contact_links(obj):
            if peer in interiors and name != peer:
                edges.add(tuple(sorted((name, peer))))
    dirty = set(changed)
    groups = []
    for group in connected_components(canonical, sorted(edges)):
        if not all_contacts and not (dirty & set(group)):
            continue
        if len(group) < 2:
            # Previously unresolved Interiors may have no valid published Mesh
            # and therefore no live collision edge. They still need an automatic
            # retry when the owner moves or clearance changes.
            name = group[0]
            obj = interiors[name]
            stale_links = any(link not in interiors for link in smart._contact_links(obj))
            if not (stale_links or obj.get('DWOS_smart_interior_pending', False) or
                    obj.parent.get('DWOS_smart_interior_pending', False)):
                continue
        groups.append(tuple(group))
    return interiors, tuple(groups), len(pairs)


def refresh(scene, *, changed=(), all_contacts=False):
    """Rebuild only contact components. Every stage is validated before publish.

    Group failure stays isolated (no mesh mutation); a subsequent group may
    still be rebuilt. Each publish has its own complete rollback contract.
    """
    if (not scene.dwos.smart_interior_enabled or
        str(getattr(scene.dwos,"smart_interior_conflict_behavior","ASK_FIRST"))=="ASK_FIRST"):
        return {'contact_groups':0,'updated':0,'pending':0,'pairs':0,
                'policy':"ASK_FIRST_NO_AUTO_MUTATION"}
    interiors, groups, detected_pairs = _contact_groups(scene, changed=changed, all_contacts=all_contacts)
    updated = pending = 0
    for group in groups:
        # A group may contain a pending window. Prefer that as anchor so retries
        # clear its state when the conflict becomes solvable.
        anchor = min(group, key=lambda name:(not bool(interiors[name].get('DWOS_smart_interior_pending', False)),name))
        obj = interiors[anchor]
        try:
            source = smart.decode(obj)
            result, updates, report = smart.stage(obj.parent, source, obj.matrix_world,
                                                  scene, force=True, allow_deferred=True)
            if report.get('active_pending'):
                pending += 1
                obj['DWOS_smart_interior_pending'] = True
                obj['DWOS_smart_interior_reason'] = report['reason']
                obj.parent['DWOS_smart_interior_pending'] = True
                obj.parent['DWOS_smart_interior_reason'] = report['reason']
                obj.parent['DWOS_smart_interior_reason_code'] = report['reason_code']
                for affected in group:
                    if affected == anchor:continue
                    peer=interiors[affected]
                    peer['DWOS_smart_interior_pending']=True
                    peer['DWOS_smart_interior_reason']=str(report['reason'])
                    peer.parent['DWOS_smart_interior_pending']=True
                    peer.parent['DWOS_smart_interior_reason']=str(report['reason'])
                    peer.parent['DWOS_smart_interior_reason_code']=report['reason_code']
                log_event('SMART_INTERIOR_AUTO_PENDING', group=group, code=report['reason_code'])
                continue
            if (not smart._equal(result, smart._mesh_data(obj)) or
                    obj.get('DWOS_smart_interior_pending', False) or
                    tuple(report.get('active_links', ())) != smart._contact_links(obj)):
                updates.insert(0, {'object': obj, 'mesh': result, 'canonical': source,
                                   'links': report.get('active_links', []),
                                   'replace_geometry': not smart._equal(result, smart._mesh_data(obj))})
            if updates:
                updated += smart.publish_neighbors(updates)
        except Exception as exc:
            pending += 1
            # The old refresh silently swallowed solver/publish errors and left
            # a visibly colliding Mesh with no warning on the window. Never
            # interpret this as successful AUTO.
            message=f'{type(exc).__name__}: {exc}'
            for affected in group:
                cavity=interiors[affected]
                cavity['DWOS_smart_interior_pending']=True
                cavity['DWOS_smart_interior_reason']=message
                cavity.parent['DWOS_smart_interior_pending']=True
                cavity.parent['DWOS_smart_interior_reason']=message
                cavity.parent['DWOS_smart_interior_reason_code']='AUTO_REBUILD_EXCEPTION'
            log_exception('SMART_INTERIOR_AUTO_REBUILD_FAILED', exc, window=anchor, group=group)
    log_event('SMART_INTERIOR_AUTO_REFRESH', groups=len(groups), pairs=detected_pairs,
              updated=updated, pending=pending, scope='CONTACT_GROUPS_ONLY')
    return {'contact_groups':len(groups),'updated':updated,'pending':pending,'pairs':detected_pairs}


def _run_queue():
    global _running
    if not _registered or not _lease.alive(_LEASE_OWNER, _LEASE_TOKEN):
        _queued.clear()
        return None
    if not _queued:
        return None
    if _running:
        return _DELAY_SEC
    # Blender must not modify Mesh during Edit Mode or an active interactive tool.
    if getattr(bpy.context, 'mode', 'OBJECT') != 'OBJECT':
        return 0.75
    if any(time.monotonic() - task['last_change'] < _DELAY_SEC for task in _queued.values()):
        return _DELAY_SEC
    tasks = tuple(_queued.values())
    _queued.clear()
    _running = True
    try:
        for task in tasks:
            scene = task['scene']
            try:
                refresh(scene, changed=task['changed'], all_contacts=task['all'])
            except Exception as exc:
                # One broken Scene cannot permanently kill the timer or block
                # a valid independent Scene. Fail once, record and continue.
                log_exception('SMART_INTERIOR_AUTO_DISPATCH_FAILED', exc,
                              trigger=','.join(sorted(task['reasons'])))
    finally:
        _running = False
    # A property may have changed while computing: leave that change queued.
    return _DELAY_SEC if _queued else None


@persistent
def _depsgraph_changed(scene, depsgraph):
    if _running or scene is None or not hasattr(scene, 'dwos'):
        return
    if not scene.dwos.smart_interior_enabled:
        return
    names = set()
    scene_key = _scene_id(scene)
    inventory = frozenset((obj.parent.name, obj.name) for obj in scene.objects
                          if obj.type == 'MESH' and obj.get('DWOS_role') == 'INTERIOR'
                          and not obj.get('DWOS_W58_9_active_group')
                          and obj.parent is not None)
    before = _last_inventory.get(scene_key)
    _last_inventory[scene_key] = inventory
    if before is not None and before != inventory:
        # An Interior/owner may have been deleted: old linked peers require
        # restoration even though the removed item no longer has a depsgraph ID.
        schedule(scene, all_contacts=True, reason='INTERIOR_ADDED_OR_REMOVED')
    for update in depsgraph.updates:
        try:
            if not getattr(update, 'is_updated_transform', False):
                continue
            obj = getattr(update, 'id', None)
            if obj is None or not isinstance(obj, bpy.types.Object):
                continue
            frame = _frame_of(obj)
            if frame is None:
                continue
            signature = _signature(frame)
            key = (_scene_id(scene), frame.name)
            if signature is not None and _last_world.get(key) != signature:
                _last_world[key] = signature
                names.add(frame.name)
        except (ReferenceError, RuntimeError, ValueError, TypeError) as exc:
            # A deleted/relinked Object in a depsgraph batch must not suppress
            # transform changes of unrelated valid windows in the same event.
            log_exception('SMART_INTERIOR_DEPSGRAPH_OBJECT_FAILED', exc)
    if names:
        schedule(scene, changed=names, reason='DWOS_TRANSFORM')


@persistent
def _file_loaded(_dummy):
    global _LEASE_TOKEN
    # Blender can clear driver_namespace during file load. Reclaim this
    # registered module's epoch only if no replacement module owns it.
    if (_registered and _LEASE_TOKEN is not None
            and not _lease.alive(_LEASE_OWNER, _LEASE_TOKEN)
            and not _lease.snapshot(_LEASE_OWNER)['leased']):
        _LEASE_TOKEN, _ = _lease.claim(_LEASE_OWNER)
    _queued.clear()
    _last_world.clear()
    _last_inventory.clear()
    from . import dwos_history
    dwos_history.clear()  # Never carry an in-memory Undo stack to a newly loaded .blend
    for scene in bpy.data.scenes:
        if getattr(getattr(scene, 'dwos', None), 'smart_interior_enabled', False):
            schedule(scene, all_contacts=True, reason='FILE_LOAD')


@persistent
def _undo_redo(_dummy):
    # Blender may restore a previous Mesh and parent transform without a
    # transform depsgraph event. Reconcile contact groups after the undo/redo
    # operation finishes, using restored canonical geometry and current settings.
    _queued.clear()
    _last_world.clear()
    _last_inventory.clear()
    for scene in bpy.data.scenes:
        if getattr(getattr(scene, 'dwos', None), 'smart_interior_enabled', False):
            schedule(scene, all_contacts=True, reason='UNDO_REDO')


def _remove_stale_hook(handlers, callback):
    """Remove identical handler names from earlier module objects."""
    for previous in tuple(handlers):
        if (getattr(previous, '__module__', None) == __name__
                and getattr(previous, '__name__', None) == callback.__name__):
            try:
                handlers.remove(previous)
            except (RuntimeError, ValueError, ReferenceError):
                pass


def register():
    global _registered, _LEASE_TOKEN
    _LEASE_TOKEN, _previous_lease = _lease.claim(_LEASE_OWNER)
    # _registered is local to a Python module incarnation. It cannot detect
    # stale callbacks that Blender kept after hot-reloading the addon.
    for handlers, callback in (
            (bpy.app.handlers.depsgraph_update_post, _depsgraph_changed),
            (bpy.app.handlers.load_post, _file_loaded),
            (bpy.app.handlers.undo_post, _undo_redo),
            (bpy.app.handlers.redo_post, _undo_redo)):
        _remove_stale_hook(handlers, callback)
        handlers.append(callback)
    _registered = True


def unregister():
    global _registered, _LEASE_TOKEN
    if not _lease.alive(_LEASE_OWNER, _LEASE_TOKEN):
        # Old Python module's unregister must not strip newer module callbacks.
        _queued.clear()
        _last_world.clear()
        _last_inventory.clear()
        _registered = False
        return
    for handler_list, callback in (
            (bpy.app.handlers.depsgraph_update_post, _depsgraph_changed),
            (bpy.app.handlers.load_post, _file_loaded),
            (bpy.app.handlers.undo_post, _undo_redo),
            (bpy.app.handlers.redo_post, _undo_redo)):
        _remove_stale_hook(handler_list, callback)
    try:
        if bpy.app.timers.is_registered(_run_queue):
            bpy.app.timers.unregister(_run_queue)
    except (ReferenceError, RuntimeError, AttributeError):
        pass
    _queued.clear()
    _last_world.clear()
    _last_inventory.clear()
    _registered = False
    _lease.retire(_LEASE_OWNER, _LEASE_TOKEN)
    _LEASE_TOKEN = None
