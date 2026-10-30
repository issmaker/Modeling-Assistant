"""Shared contextual eligibility. No imports of bpy or GPU."""
def wall_tool_eligibility(*, closed, double, has_interior):
    closed=bool(closed);double=bool(double)
    return {
        'W_CAP': double or closed,
        'W_PLANT': closed and double and bool(has_interior),
        'END_CAP': double and not closed,
        'SECONDARY': double,
    }
