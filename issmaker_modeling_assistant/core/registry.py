"""Blender-free registry for ISSMAKER tools, safe to import in headless tests."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    title: str
    version: str
    entry_operator: str | None
    module: str
    enabled: bool = True
    icon: str = "GRID"


TOOLS = (
    ToolSpec('BRICK_HOLE', 'Проёмы', '1.0.17', 'view3d.brick_hole_interactive', 'tools.brick_hole', True, 'MOD_UVPROJECT'),
    ToolSpec('DWOS', 'Окна и двери', '3.7.37 RC38', 'dwos.viewport_composer', 'tools.dwos_windows', True, 'NODE_MATERIAL'),
    ToolSpec('COLLISION', 'Коллизии', '3.5.2', 'collision_inspector.check', 'tools.collision_inspector', True, 'MOD_PHYSICS'),
    ToolSpec('UV_WALL', 'Стены', '1.18.17', 'uvwall.viewport_inspector', 'tools.uv_wall', True, 'MOD_WIREFRAME'),
)


MENU_TOOLS = TOOLS


def validate_registry(specs=TOOLS):
    specs = tuple(specs)
    if len(specs) != 4 or any(not spec.enabled for spec in specs):
        raise ValueError("Tools radial menu requires exactly four enabled slots")
    used_ids, used_operators = set(), set()
    for spec in specs:
        if not spec.tool_id or spec.tool_id in used_ids:
            raise ValueError('Duplicate/empty tool ID: %r' % spec.tool_id)
        used_ids.add(spec.tool_id)
        if spec.enabled and not spec.entry_operator:
            raise ValueError('Enabled tool without an entry point: %s' % spec.tool_id)
        if spec.entry_operator:
            if spec.entry_operator in used_operators:
                raise ValueError('Duplicate entry point: %s' % spec.entry_operator)
            used_operators.add(spec.entry_operator)
    if {spec.tool_id for spec in specs} != {spec.tool_id for spec in TOOLS}:
        raise ValueError('Unexpected tool IDs in radial menu')
    return True


def find(tool_id):
    return next((spec for spec in TOOLS if spec.tool_id == tool_id), None)
