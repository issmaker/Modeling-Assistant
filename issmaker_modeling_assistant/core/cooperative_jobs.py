"""Shared cooperative main-thread task controller for ISSMAKER.

A Blender modal can lose its event handler when invoked by parent operators or
app timers. This controller owns an ordinary bpy.app timer and advances a
generator only at explicit stage boundaries, with transactional GeneratorExit
rollback. All bpy work stays on the main thread, never in Python worker threads.

Native undo is requested only after a successful committed transaction. Failure
to push Blender undo is reported as a warning, not hidden.
"""
from __future__ import annotations
import time
import traceback

import bpy
from . import process_feedback

_JOB=None
_LOG=[]
_TICK_DELAY=.065


def _log(event,detail=""):
    _LOG.append((round(time.monotonic(),3),event,str(detail)[:250]))
    if len(_LOG)>60:del _LOG[:-60]
    try:
        from .. import log
        log.write("COOPERATIVE_JOB "+event+" "+str(detail)[:350])
    except Exception:pass


def history():
    return list(_LOG)


def active():
    return _JOB is not None and not _JOB.get("finished",False)


def status():
    if _JOB is None:return {}
    j=_JOB
    return {"token":j["token"],"tool":j["tool"],"mode":j["mode"],
            "completed":j["completed"],"started":j["started"],
            "finished":j["finished"],"result":j["result"],
            "undo":j.get("undo","NOT_APPLICABLE")}


def _valid(j):
    try:
        wm=bpy.context.window_manager
        return (any(w.as_pointer()==j["window"].as_pointer() for w in wm.windows)
                and any(a.as_pointer()==j["area"].as_pointer()
                        for a in j["window"].screen.areas)
                and j["window"].scene==j["scene"])
    except (AttributeError,ReferenceError,RuntimeError):
        return False


def _final(j,state,message):
    global _JOB
    if j.get("finished"):return
    j["finished"]=True
    j["result"]=state
    if state!="SUCCESS":
        try:j["iterator"].close()
        except Exception as exc:
            _log("ROLLBACK_ERROR",repr(exc))
            state="FAILED"
            message=str(message)+"; rollback: "+repr(exc)
    j["result"]=state
    token=j["token"]
    try:
        if state=="SUCCESS":process_feedback.finish(token,message)
        elif state=="CANCELLED":process_feedback.cancel(token,message)
        else:process_feedback.fail(token,message)
    except Exception as exc:_log("HUD_FINALIZE_ERROR",repr(exc))
    try:j["scene"]["W5829_last_build_status"]=state
    except (ReferenceError,RuntimeError):pass
    _remove_esc_shortcut(j)
    _log("FINISHED_"+state,message)
    # Invalidate old timer callbacks before a new process may begin.
    if _JOB is j:_JOB=None


def _install_esc_shortcut(j):
    """Install a temporary native ESC command while a cooperative task runs."""
    if bpy.app.background:return
    try:
        kc=bpy.context.window_manager.keyconfigs.addon
        if kc is None:return
        km=kc.keymaps.new(name="Window",space_type="EMPTY")
        kmi=km.keymap_items.new("ima.process_hud_cancel",type="ESC",value="PRESS")
        j["escape_keymap"]=(km,kmi)
        _log("ESC_KEYMAP_INSTALLED",km.name)
    except Exception as exc:_log("ESC_KEYMAP_UNAVAILABLE",repr(exc))


def _remove_esc_shortcut(j):
    item=j.pop("escape_keymap",None)
    if item is None:return
    km,kmi=item
    try:km.keymap_items.remove(kmi)
    except (ReferenceError,RuntimeError,ValueError):pass


def _push_undo(j):
    if bpy.app.background:return "HEADLESS_NO_UNDO"
    try:
        if not _valid(j):return "CONTEXT_LOST"
        w,area,region=j["window"],j["area"],j["region"]
        with bpy.context.temp_override(window=w,area=area,region=region):
            if not bpy.ops.ed.undo_push.poll():
                return "UNDO_UNAVAILABLE"
            result=bpy.ops.ed.undo_push(message="ISSMAKER · Операция")
        return "PUSHED" if "FINISHED" in result else "UNDO_REJECTED"
    except Exception as exc:
        _log("UNDO_ERROR",repr(exc))
        return "UNDO_ERROR"


def _tick(j):
    if _JOB is not j or j["finished"]:
        return None
    if not _valid(j):
        _final(j,"FAILED","Сцена или окно изменились — откат временной геометрии")
        return None
    try:
        # Explicit override matters in multi-window Blender and when a
        # toolbar / UI timer last changed bpy.context.area.
        with bpy.context.temp_override(window=j["window"],
                                       area=j["area"],region=j["region"]):
            step=next(j["iterator"])
        if "steps" in step:
            process_feedback.configure(j["token"],step["steps"])
        process_feedback.stage(j["token"],step["completed"],
                               step.get("phase"),step.get("detail"),
                               cancellable=True)
        j["completed"]=step["completed"]
        _log("STAGE",str(step.get("phase"))+": "+str(j["completed"]))
        # A few windows: show each completed milestone long enough for a
        # user to understand/cancel it. A large batch uses a shorter interval
        # to avoid hundreds of unnecessary idle UI frames.
        task=process_feedback.current()
        return .17 if task is not None and len(task.steps)<=14 else _TICK_DELAY
    except StopIteration as completed:
        windows=completed.value or []
        j["count"]=len(windows)
        j["undo"]=_push_undo(j)
        suffix="" if j["undo"]=="PUSHED" else " · Undo: "+j["undo"]
        _final(j,"SUCCESS",f"Создано {len(windows)} окон"+suffix)
        # Composer stays accessible via radial; no modal invocation inside
        # a timer, because Blender may discard the new modal handler.
        return None
    except Exception as exc:
        _log("FAILED",traceback.format_exc()[-1600:])
        _final(j,"FAILED",type(exc).__name__+": "+str(exc)[:350])
        return None




def cancel_current(reason="Операция отменена"):
    j=_JOB
    if j is None or j["finished"]:return False
    # Cancel cooperatively only when between generator advances (single main
    # thread, never re-entrant from another bpy thread).
    _final(j,"CANCELLED",reason)
    return True


def reset():
    if active():
        cancel_current("Аддон выгружен — временные объекты отменены")
