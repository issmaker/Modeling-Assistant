"""Safe, bpy-free COPY transfer for native Corner branch layouts.

Never interpret global Zone Tree of a non-Corner source as Corner mullions.
Branch IDs and station extents are always owned by the receiving opening.
"""
from __future__ import annotations
import copy
import json

try:
    from .copy_orientation_core import mirror_zone_tree
except ImportError:
    from copy_orientation_core import mirror_zone_tree

try:
    from .branch_core import branch_descriptors, decode_state, encode_state, new_state, layout_state, SCHEMA
    from .corner_rules import corner_posts, validate_split_rows
    from .zone_core import decode as zone_decode, layout as zone_layout
except ImportError:
    from branch_core import branch_descriptors, decode_state, encode_state, new_state, layout_state, SCHEMA
    from corner_rules import corner_posts, validate_split_rows
    from zone_core import decode as zone_decode, layout as zone_layout


class IncompatibleCopyTarget(ValueError):
    """Valid receiving window cannot accept this source layout.

    Batch COPY skips only this error; malformed source/target state must fail
    loudly instead of being silently classified as incompatibility.
    """


def partition_copy_targets(targets, planner):
    """Pure, deterministic preflight partition: (plans, skipped targets).

    ``planner`` may raise IncompatibleCopyTarget to skip a receiver. All other
    exceptions propagate and leave the entire batch unchanged.
    """
    plans, skipped = {}, []
    for target in targets:
        try:
            plans[target] = planner(target)
        except IncompatibleCopyTarget as exc:
            skipped.append((target, str(exc)))
    return plans, skipped


def _kind(topo):
    return str((topo or {}).get('classification', 'PLANAR')).upper()


def _junction_signature(topo):
    return tuple(str(j.get('kind','')).upper() for j in ((topo or {}).get('corner_junctions') or ()))


def plan_copy_layout(source_topology, target_topology, *, source_zone_tree='', source_branch_state='',
                     source_splits=(), target_width, target_height, target_frame_width,
                     target_internal_width, target_frame_depth, target_post_mode='ON', mirror_horizontal=False):
    """Compute an immutable-ish transfer plan; no Blender state is mutated.

    Rebuild and copy callers must apply only after ALL targets preflight.
    """
    sc,tc=_kind(source_topology),_kind(target_topology)
    if (sc=='CORNER') != (tc=='CORNER'):
        raise IncompatibleCopyTarget('CORNER и обычное окно имеют разные системы импостов')
    if mirror_horizontal and (sc != 'PLANAR' or tc != 'PLANAR'):
        raise IncompatibleCopyTarget('COPY: зеркальная ориентация CORNER/CURVED требует ручной проверки раскладки')
    if sc != 'CORNER':
        zone_tree = str(source_zone_tree or '')
        if zone_tree:
            tree = zone_decode(zone_tree)
            if mirror_horizontal:
                try:
                    from .zone_core import encode as zone_encode
                except ImportError:
                    from zone_core import encode as zone_encode
                tree = mirror_zone_tree(tree)
                zone_tree = zone_encode(tree)
            zone_layout(tree,float(target_width),float(target_height),float(target_frame_width))
        return {'mode':'STANDARD','zone_tree_json':zone_tree,
                'branch_state_json':'', 'splits':copy.deepcopy(list(source_splits)),
                'mirror_horizontal':bool(mirror_horizontal)}
    if mirror_horizontal:
        raise IncompatibleCopyTarget('COPY CORNER: противоположная ориентация веток — ручная проверка обязательна')
    if source_zone_tree:
        raise ValueError('COPY отклонён: исходный CORNER использует устаревшее общее дерево зон. Автоматическое копирование опасно.')
    if source_splits:
        raise ValueError('COPY отклонён: исходный CORNER содержит старые глобальные Splits. Переносите только native branch раскладку.')
    source_desc=branch_descriptors(source_topology)
    target_desc=branch_descriptors(target_topology)
    if len(source_desc)!=2 or len(target_desc)!=2:
        raise IncompatibleCopyTarget('CORNER требует две совместимые фасадные ветки')
    if _junction_signature(source_topology)!=_junction_signature(target_topology):
        raise IncompatibleCopyTarget('Геометрия угловых стыков различается')
    if source_branch_state:
        try:
            raw=json.loads(source_branch_state) if isinstance(source_branch_state,str) else source_branch_state
        except (TypeError,ValueError) as exc:
            raise ValueError('COPY отклонён: повреждено native дерево веток исходного CORNER.') from exc
        if not isinstance(raw,dict) or raw.get('schema')!=SCHEMA:
            raise ValueError('COPY отклонён: неподдерживаемая схема веток исходного CORNER.')
    src=decode_state(source_branch_state,source_topology)
    dst=new_state(target_topology)
    by_index={int(b['index']):b for b in src['branches']}
    if set(by_index)!={int(b['index']) for b in dst['branches']}:
        raise IncompatibleCopyTarget('Порядок фасадных веток CORNER несовместим')
    for target_branch in dst['branches']:
        # Copy only authored tree. All station indices, dimensions, ids and
        # corner junctions remain generated from the receiving opening.
        target_branch['tree']=copy.deepcopy(by_index[int(target_branch['index'])]['tree'])
    dst['migration_status']='NATIVE_COPY_SAFE_V1'
    try:
        from .corner_post_policy_core import require_post_allowed
    except ImportError:
        from corner_post_policy_core import require_post_allowed
    try:
        require_post_allowed(target_topology,target_post_mode)
    except ValueError as exc:
        raise IncompatibleCopyTarget(str(exc)) from exc
    posts=corner_posts(target_topology,target_post_mode,float(target_internal_width),float(target_frame_depth))
    _,bands=layout_state(dst,target_topology,float(target_height),float(target_frame_width),posts)
    conflicts=validate_split_rows([
        {'id':r[0], 'axis':r[1], 'position':r[2], 'thickness':r[3], 'rect':r[4]} for r in bands
    ],posts,0.003)
    if conflicts:
        raise IncompatibleCopyTarget('Импост пересекает угловой junction получателя')
    return {'mode':'NATIVE_CORNER','zone_tree_json':'',
            'branch_state_json':encode_state(dst),'splits':[]}
