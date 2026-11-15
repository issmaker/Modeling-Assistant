"""ISSMAKER Process HUD — shared, truthful viewport progress protocol.

Every tool can call begin / stage / finish / fail. UI percentages reflect only
confirmed milestones and never promise an ETA. Tools yield cooperatively
between heavy stages so Blender gets real frames; a blocking native function
cannot paint until it returns (this limitation is intentionally explicit).
"""
from __future__ import annotations
from collections import deque
import math
import time

try:
    import bpy
except ImportError:
    bpy=None

_HANDL=None
_TASK=None
_HISTORY=deque(maxlen=32)
_SEQ=0
_DRAW_FAILURE_LAST=0.
_DRAW_FAILURE_TOTAL=0
_ORPHAN_GUARD_AFTER=2.5
_STALL_CANCEL_AFTER=25.0

PANEL=(0.017,0.025,0.043,0.966)
SURFACE=(0.055,0.081,0.115,0.985)
WHITE=(0.94,0.97,1.,1.)
MUTED=(0.61,0.70,0.79,1.)
CYAN=(0.30,0.92,0.85,1.)
COPPER=(1.,0.73,0.43,1.)
PINK=(1.,0.47,0.52,1.)
BLUE=(0.37,0.62,1.,1.)

def _clamp(x,a,b):
    try:
        v=float(x)
        return min(float(b),max(float(a),v)) if math.isfinite(v) else float(a)
    except (TypeError,ValueError,OverflowError):
        return float(a)


def layout(width,height,scale=1.):
    """Pure, deterministic viewport layout for Blender 4.x and 5.x."""
    width=max(240,int(width));height=max(240,int(height))
    s=_clamp(scale,.75,1.6)
    w=min(530*s,max(235.,width-32.))
    h=min(252*s,max(175.,height-58.))
    x=(width-w)*.5
    y=max(22.,height-53.*s-h)
    return x,y,w,h,s


class ProcessTask:
    """Main-thread lifecycle; tool code owns Mesh mutations and rollback."""
    def __init__(self,token,tool,title,steps,area_id=0):
        if not steps or len(steps)>500:
            raise ValueError("PROCESS_STEPS_MUST_HAVE_1_TO_500_ITEMS")
        self.token=token
        self.tool=str(tool)[:42]
        self.title=str(title)[:95]
        self.steps=tuple(str(s)[:54] for s in steps)
        self.area_id=int(area_id or 0)
        self.completed=0
        self.phase=self.steps[0]
        self.detail="Подготавливаем операцию"
        self.state="RUNNING"
        self.started=time.monotonic()
        self.last_updated=self.started
        self.finished=None
        self.error=""
        self.cancellable=True

    def configure(self,steps):
        if self.state!="RUNNING" or self.completed:
            raise ValueError("PROCESS_STEPS_CANNOT_BE_RECONFIGURED_NOW")
        if not steps or len(steps)>500:
            raise ValueError("PROCESS_STEPS_MUST_HAVE_1_TO_500_ITEMS")
        self.steps=tuple(str(s)[:54] for s in steps)
        self.phase=self.steps[0]

    def update(self,completed,phase=None,detail=None,cancellable=True):
        if self.state!="RUNNING":return
        number=int(completed)
        if number<self.completed or number>len(self.steps):
            raise ValueError("PROCESS_PROGRESS_MUST_BE_MONOTONIC")
        self.completed=number
        self.last_updated=time.monotonic()
        if phase is not None:self.phase=str(phase)[:90]
        elif number<len(self.steps):self.phase=self.steps[number]
        if detail is not None:self.detail=str(detail)[:150]
        self.cancellable=bool(cancellable)

    def conclude(self,state,message=""):
        if state not in ("SUCCESS","FAILED","CANCELLED"):
            raise ValueError("PROCESS_INVALID_FINAL_STATE")
        self.state=state
        self.finished=time.monotonic()
        self.last_updated=self.finished
        if state=="SUCCESS":
            self.completed=len(self.steps)
            self.phase="Завершено"
            self.detail=str(message or "Все этапы выполнены")[:150]
        elif state=="CANCELLED":
            self.phase="Отменено"
            self.detail=str(message or "Исходная геометрия сохранена")[:150]
        else:
            self.error=str(message)[:500]
            self.phase="Не удалось завершить"
            self.detail=self.error[:150]

    @property
    def fraction(self):
        return self.completed/len(self.steps)

    def receipt(self):
        return dict(token=self.token,tool=self.tool,title=self.title,
                    steps=self.steps,completed=self.completed,total=len(self.steps),
                    state=self.state,phase=self.phase,detail=self.detail,
                    seconds=round((self.finished or time.monotonic())-self.started,3),
                    error=self.error,cancellable=self.cancellable,
                    seconds_since_update=round(
                        (self.finished or time.monotonic())-self.last_updated,3))


def history():
    return [dict(r) for r in _HISTORY]


def current():
    return _TASK


def snapshot():
    return _TASK.receipt() if _TASK is not None else None


def _redraw():
    if bpy is None or bpy.app.background:return
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type=='VIEW_3D':
                    area.tag_redraw()
    except (ReferenceError,AttributeError,RuntimeError):pass


def _tick():
    """Redraw on actual UI frames; no fake progress, no scene changes."""
    global _TASK
    task=_TASK
    if task is None:return None
    now=time.monotonic()
    if task.finished is not None and now-task.finished>4.0:
        _TASK=None
        _remove()
        _redraw()
        return None
    _redraw()
    return .06 if task.state=="RUNNING" else .16


def begin(tool,title,steps,context=None):
    global _TASK,_SEQ
    if _TASK is not None and _TASK.state=="RUNNING":
        raise RuntimeError("ISSMAKER_PROCESS_ALREADY_RUNNING: "+_TASK.tool)
    _SEQ+=1
    area=getattr(context,'area',None) if context else None
    area_id=area.as_pointer() if area is not None and area.type=="VIEW_3D" else 0
    _TASK=ProcessTask(_SEQ,tool,title,steps,area_id=area_id)
    if bpy is not None and not bpy.app.background:
        _install()
        if not bpy.app.timers.is_registered(_tick):
            bpy.app.timers.register(_tick,first_interval=.025)
    _redraw()
    return _TASK.token


def configure(token,steps):
    if _TASK is None or _TASK.token!=token:
        raise RuntimeError("ISSMAKER_PROCESS_TOKEN_MISMATCH")
    _TASK.configure(steps)
    _redraw()

def stage(token,completed,phase=None,detail=None,cancellable=True):
    if _TASK is None or _TASK.token!=token:
        raise RuntimeError("ISSMAKER_PROCESS_TOKEN_MISMATCH")
    _TASK.update(completed,phase,detail,cancellable)
    _redraw()
    return _TASK.receipt()


def _conclude(token,state,message):
    if _TASK is None or _TASK.token!=token:
        raise RuntimeError("ISSMAKER_PROCESS_TOKEN_MISMATCH")
    _TASK.conclude(state,message)
    record=_TASK.receipt()
    _HISTORY.append(record)
    _redraw()
    return record


def finish(token,message=""):
    return _conclude(token,"SUCCESS",message)


def fail(token,message=""):
    return _conclude(token,"FAILED",message)


def cancel(token,message=""):
    return _conclude(token,"CANCELLED",message)


def reset():
    """Unregister/hot reload safe; never edits any Blender scene object."""
    global _TASK
    _TASK=None
    if bpy is not None:
        try:
            if bpy.app.timers.is_registered(_tick):bpy.app.timers.unregister(_tick)
        except (ReferenceError,RuntimeError):pass
    _remove()
    _redraw()


def _install():
    global _HANDL
    if _HANDL is None and bpy is not None and not bpy.app.background:
        _HANDL=bpy.types.SpaceView3D.draw_handler_add(_draw,(),'WINDOW','POST_PIXEL')


def _remove():
    global _HANDL
    if _HANDL is None or bpy is None:return
    try:bpy.types.SpaceView3D.draw_handler_remove(_HANDL,'WINDOW')
    except (RuntimeError,ValueError,ReferenceError):pass
    _HANDL=None


def _label(x,y,sz,value,color,max_w=None,weight=400):
    import blf
    f=0
    size=max(9,int(sz))
    blf.size(f,size)
    value=str(value)
    if max_w is not None:
        if hasattr(blf,'dimensions'):
            while size>9 and blf.dimensions(f,value)[0]>max_w:
                size-=1
                blf.size(f,size)
        if blf.dimensions(f,value)[0]>max_w:
            while len(value)>4 and blf.dimensions(f,value+'…')[0]>max_w:
                value=value[:-1]
            value+='…'
    blf.color(f,*color)
    blf.position(f,int(x),int(y),0)
    blf.draw(f,value)


def _fill(x,y,w,h,color,radius=0.):
    if w<1 or h<1:return
    from .studio_gpu_components import fill_rect
    fill_rect(x,y,x+w,y+h,color,radius=max(0.,radius),steps=5)


def _log_draw_error(exc,source="DRAW"):
    global _DRAW_FAILURE_LAST,_DRAW_FAILURE_TOTAL
    _DRAW_FAILURE_TOTAL+=1
    now=time.monotonic()
    if now-_DRAW_FAILURE_LAST<6.0:return
    _DRAW_FAILURE_LAST=now
    try:
        from .. import log
        log.write_exception("PROCESS_HUD_"+source+"_ERROR",exc)
        from .action_trace import record
        record("PROCESS_HUD_"+source+"_ERROR","CORE",
               type(exc).__name__+": "+str(exc)[:340])
    except Exception:
        pass


def _draw():
    task=_TASK
    if task is None or bpy is None:return
    ctx=bpy.context
    area=getattr(ctx,'area',None)
    region=getattr(ctx,'region',None)
    if area is None or area.type!='VIEW_3D' or region is None:return
    if task.area_id and area.as_pointer()!=task.area_id:return
    import gpu
    try:
        x,y,w,h,s=layout(region.width,region.height,
                           getattr(ctx.preferences.system,'ui_scale',1.))
        side=21*s
        accent=CYAN if task.state in ('RUNNING','SUCCESS') else (
               COPPER if task.state=='CANCELLED' else PINK)
        _fill(x+4*s,y-7*s,w,h,(0,0,0,.37),20*s)
        _fill(x,y,w,h,PANEL,20*s)
        _fill(x+1*s,y+1*s,w-2*s,h-2*s,SURFACE,19*s)
        _fill(x,y+h-5*s,w,5*s,(.10,.22,.28,.96),2*s)
        _fill(x,y+h-5*s,max(5*s,w*task.fraction),5*s,accent,2*s)
        _label(x+side,y+h-32*s,10*s,"ISSMAKER   /   PROCESS ENGINE",accent,w-2*side)
        elapsed=(task.finished or time.monotonic())-task.started
        _label(x+w-102*s,y+h-32*s,10*s,f"{elapsed:.1f} с",MUTED,82*s)
        _label(x+side,y+h-62*s,18*s,task.title,WHITE,w-2*side)
        _label(x+side,y+h-84*s,11*s,task.tool.upper()+"   ·   "+task.phase,MUTED,w-2*side)
        py=y+h-115*s
        _fill(x+side,py,w-2*side,11*s,(.12,.18,.23,1.),5*s)
        if task.completed:
            _fill(x+side,py,max(1.,(w-2*side)*task.fraction),11*s,accent,5*s)
        # A subtle running marker animates *only* within the current
        # confirmed segment; it never changes the reported stage percentage.
        if task.state=="RUNNING" and task.completed<len(task.steps):
            seg=(w-2*side)/len(task.steps)
            pulse=(math.sin(time.monotonic()*3.2)+1.)*.5
            lx=x+side+seg*task.completed+seg*.1
            _fill(lx,py+2*s,max(3*s,(seg*.1+seg*.3*pulse)),7*s,
                  (.4,.72,.75,.37),3*s)
        _label(x+side,y+h-138*s,10*s,
               f"ГОТОВО ЭТАПОВ   {task.completed} / {len(task.steps)}",
               accent,w-2*side)
        if task.state=="RUNNING" and task.cancellable:
            _label(x+w-210*s,y+h-138*s,9*s,
                   "ESC  ·  отмена после этапа",
                   MUTED,188*s)
        # Compact horizontal timeline: milestones are checked only after
        # a real yield from the tool, never estimated from elapsed seconds.
        step_gap=7*s
        indexes=(list(range(len(task.steps))) if len(task.steps)<=6 else
                 sorted(set((0,max(0,task.completed-2),
                            max(0,task.completed-1),
                            min(len(task.steps)-1,task.completed),
                            min(len(task.steps)-1,task.completed+1),
                            len(task.steps)-1))))
        count=len(indexes)
        sw=(w-2*side-step_gap*(count-1))/count
        if sw>12:
            for slot,i in enumerate(indexes):
                name=task.steps[i]
                sx=x+side+slot*(sw+step_gap)
                done=i<task.completed
                active=i==task.completed and task.state=="RUNNING"
                fg=CYAN if done else WHITE if active else MUTED
                _fill(sx,y+54*s,sw,3*s,accent if done else
                      (accent[0],accent[1],accent[2],.55) if active else
                      (.20,.27,.33,.75),1.5*s)
                _label(sx,y+38*s,9*s,('✓ ' if done else '')+name,fg,sw)
        detail=task.detail
        _label(x+side,y+16*s,10*s,detail,
               WHITE if task.state=="SUCCESS" else MUTED,
               w-2*side)
    except Exception as exc:
        _log_draw_error(exc)
    finally:
        try:gpu.state.blend_set('NONE')
        except (RuntimeError,AttributeError):pass


class IMA_OT_process_hud_preview(bpy.types.Operator):
    """UI-only preview driven by bpy.app.timers; never owns a modal input slot."""
    bl_idname="ima.process_hud_preview"
    bl_label="ISSMAKER · Демонстрация прогресса во Viewport"
    bl_description="Демонстрация реальных визуальных состояний, без изменений сцены"
    bl_options={"REGISTER"}

    def invoke(self,context,event):
        if not context.area or context.area.type!="VIEW_3D":
            self.report({"WARNING"},"Откройте 3D Viewport")
            return {"CANCELLED"}
        try:
            token=begin(
                "ДЕМОНСТРАЦИЯ · БЕЗ ИЗМЕНЕНИЙ",
                "Архитектурное окно",
                ("Контур","LP","HP","Стекло","UV-контроль","Готово"),
                context)
        except Exception as exc:
            self.report({"WARNING"},str(exc)[:190])
            return {"CANCELLED"}
        # TIMER events are not reliable for operators launched by automation
        # or parent modals. A pure visual demo has no Undo ownership and can
        # safely advance by an app timer on Blender's main thread.
        names=("Контур проверен","LP построен","HP профиль собран",
               "Стекло готово","UV проверены")
        state={"completed":0}
        def next_demo():
            task=current()
            if task is None or task.token!=token or task.state!="RUNNING":
                return None
            state["completed"]+=1
            done=state["completed"]
            if done>=6:
                finish(token,"Демонстрация завершена без изменения сцены")
                return None
            try:
                stage(token,done,names[done-1],
                      "Только интерфейс · геометрия не изменяется")
            except Exception as exc:
                fail(token,str(exc))
                _log_draw_error(exc,"DEMO")
                return None
            return .65
        bpy.app.timers.register(next_demo,first_interval=.45)
        return {"FINISHED"}


class IMA_OT_process_hud_cancel(bpy.types.Operator):
    """Emergency cancel command for process HUD (no native scene undo)."""
    bl_idname="ima.process_hud_cancel"
    bl_label="ISSMAKER · Отменить текущий процесс"
    bl_description="Безопасно отменить поддерживаемый процесс"
    bl_options={"REGISTER"}

    def execute(self,context):
        task=current()
        if task is None or task.state!="RUNNING":
            self.report({"INFO"},"Активных процессов нет")
            return {"CANCELLED"}
        if task.tool.startswith("ДЕМОНСТРАЦИЯ"):
            cancel(task.token,"Демонстрация остановлена")
            return {"FINISHED"}
        self.report({"WARNING"},"Невозможно безопасно отменить этот процесс")
        return {"CANCELLED"}


CLASSES=(IMA_OT_process_hud_preview,IMA_OT_process_hud_cancel)

def register():
    if bpy is not None:
        for klass in CLASSES:bpy.utils.register_class(klass)


def unregister():
    reset()
    if bpy is not None:
        for klass in reversed(CLASSES):
            try:bpy.utils.unregister_class(klass)
            except (RuntimeError,ValueError):pass
