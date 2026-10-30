"""Pure navigation decisions; never reads Blender state or changes selection.

This small module is deliberately testable without Blender. Reading BMesh/UV
happens once per click in view_tools; this only classifies verified results.
"""


def verify_issues(issues, verifier):
    """Return (status, survivors, examined, reason).

    A verifier must return exactly True/False. Any exception or ambiguous return
    is a fail-closed UNREADABLE result, not evidence of repaired UV.
    """
    if not issues:
        return ('NO_SESSION', [], 0, 'Нет сохранённых результатов анализа')
    survivors = []
    count = 0
    for entry in issues:
        try:
            status = verifier(entry)
            if type(status) is not bool:
                raise ValueError('verification did not return a boolean')
        except Exception as exc:
            return ('UNREADABLE', list(issues), count,
                    '%s: %s' % (type(exc).__name__, str(exc)[:240]))
        count += 1
        if status:
            survivors.append(entry)
    return ('VALID' if survivors else 'RESOLVED', survivors, count, '')


def next_issue(original, remaining, current, direction):
    """Wrap deterministically, even when the current issue was corrected."""
    if not remaining:
        return None
    step = 1 if direction >= 0 else -1
    if current is None:
        return remaining[0] if step > 0 else remaining[-1]
    if current in remaining:
        offset = remaining.index(current)
        return remaining[(offset + step) % len(remaining)]
    try:
        old_pos = original.index(current)
    except ValueError:
        return remaining[0] if step > 0 else remaining[-1]
    try:
        survivors = set(remaining)
    except TypeError:
        # Non-hashable wrappers must not turn navigation into an exception.
        survivors = None
    for jump in range(1, len(original) + 1):
        entry = original[(old_pos + step * jump) % len(original)]
        if (entry in survivors) if survivors is not None else (entry in remaining):
            return entry
    return remaining[0] if step > 0 else remaining[-1]
