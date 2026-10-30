from __future__ import annotations
from dataclasses import dataclass, field

DEFAULT_TOLERANCE = 0.01  # 1 cm in the add-on's meter-only UI.

@dataclass
class OpeningSizeGroup:
    width: float
    height: float
    indices: list[int] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.indices)


def group_sizes(sizes, tolerance: float = DEFAULT_TOLERANCE):
    """Greedy stable clustering by width/height.

    `sizes` is an iterable of (index, width, height). Values within `tolerance`
    on both axes share one group. Group representative is the running mean so
    small modeling noise does not create dozens of nearly-identical rows.
    """
    tol = max(float(tolerance), 1e-9)
    groups: list[OpeningSizeGroup] = []
    for idx, width, height in sizes:
        width = float(width); height = float(height)
        match = None
        for group in groups:
            if abs(width - group.width) <= tol and abs(height - group.height) <= tol:
                match = group
                break
        if match is None:
            groups.append(OpeningSizeGroup(width, height, [int(idx)]))
            continue
        n = match.count
        match.width = (match.width * n + width) / (n + 1)
        match.height = (match.height * n + height) / (n + 1)
        match.indices.append(int(idx))
    groups.sort(key=lambda g: (-g.count, g.width, g.height))
    return groups
