"""Read-only bounded search for an EDGE depth validated by the caller.

IMPORTANT: a conservative verified candidate, NOT an analytic maximal safe depth.
Every returned recommendation has actually passed the full caller's preflight.
A failure of the probe is never implicitly converted into a PASS. Geometry/UV
or direction failures are classified separately by the Blender-facing caller.
"""
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class DepthSuggestion:
    requested: float
    candidate: float
    failing_probe: float | None
    calls: int
    note: str

class DepthSearchFailure(ValueError):
    pass

def find_safe_depth(probe, requested, *, minimum=0.001, max_calls=26,
                    target_resolution=0.001, guard_fraction=0.002):
    """Search signed depths, with conservative application-safe final verification.

    probe(depth) must be side-effect-free, returning None on success and raising
    ValueError when rejected by the *production* preflight.  It need not be
    monotone; no global maximality claim is made. Returns a directly verified
    candidate in the same signed direction as the requested depth.
    """
    value=float(requested)
    if not math.isfinite(value) or abs(value)<minimum or abs(value)>50.0:
        raise DepthSearchFailure('SAFE DEPTH: неверная исходная глубина')
    if not isinstance(max_calls,int) or max_calls < 6 or max_calls > 100:
        raise DepthSearchFailure('SAFE DEPTH: неверный лимит вычислений')
    if not math.isfinite(target_resolution) or target_resolution<=0:
        raise DepthSearchFailure('SAFE DEPTH: неверная точность')
    direction = -1 if value<0 else 1
    absvalue=abs(value)
    calls=0
    def check(x):
        nonlocal calls
        if calls>=max_calls:
            raise DepthSearchFailure('SAFE DEPTH: исчерпан бюджет проверок без доказанного результата')
        calls += 1
        try:
            probe(direction*x)
        except ValueError as exc:
            return False,str(exc)
        return True,''
    accepted,reason=check(absvalue)
    if accepted:
        return DepthSuggestion(value,value,None,calls,'Запрошенная глубина прошла полную проверку')
    # Try progressively smaller signed depths. This does not assume that every
    # failure predicate is monotone; the returned value itself is rechecked.
    failing=absvalue
    passed=None
    candidate=absvalue
    while candidate>minimum*(1+1e-10):
        candidate=max(minimum,candidate*0.5)
        ok,why=check(candidate)
        if ok:
            passed=candidate
            break
        failing=candidate
    if passed is None:
        raise DepthSearchFailure('SAFE DEPTH: даже малая глубина отклонена. Причина: '+reason[:175])
    # We refine only the first detected failed/successful bracket. This yields
    # a useful conservative candidate but is not proof of a *global* supremum.
    while failing-passed>target_resolution and calls<max_calls-1:
        midpoint=(failing+passed)*0.5
        ok,_=check(midpoint)
        if ok:passed=midpoint
        else:failing=midpoint
    # Keep a small margin from a contact boundary, but never below minimum.
    margin=min(passed*guard_fraction,max(.00005,target_resolution*.35))
    planned=max(minimum,passed-margin)
    ok,_=check(planned)
    if not ok:
        ok,_=check(passed) if calls<max_calls else (False,'')
        if not ok:
            raise DepthSearchFailure('SAFE DEPTH: повторная проверка кандидата не прошла')
        planned=passed
    return DepthSuggestion(value,direction*planned,direction*failing,calls,
            'Проверенный консервативный кандидат; не гарантия глобального максимума')
