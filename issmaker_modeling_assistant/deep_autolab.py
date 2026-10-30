"""One button, isolated second-Blender stress lab. User scene never mutated."""
from __future__ import annotations
import datetime
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import time
import traceback
import uuid

import bpy
from bpy.types import Operator
from .core import deep_lab_supervisor as sup

_JOB = None
_LAST = None

def state():
    job = _JOB
    if job is not None:
        progress = sup.load_json(Path(job['folder']) / 'worker_state.json', {})
        return {'status':'RUNNING', 'case': str(progress.get('current','STARTING'))[:95],
                'cases':int(progress.get('count', 0)), 'folder':job['folder']}
    return dict(_LAST) if _LAST else {'status':'NOT_RUN'}


def _finish(job, returncode, *, timed_out=False, error=None):
    global _JOB, _LAST
    try:
        for stream in ('stdout','stderr'):
            handle = job.get(stream)
            if handle is not None:
                handle.close()
        report = sup.final_report(job['folder'], returncode=returncode,
                                  timed_out=timed_out, launch_error=error)
        target = str(Path(job['destination']) / ('ISSMAKER_DEEP_AUTOLAB_' + job['id'] + '.zip'))
        sup.package_evidence(job['folder'], target, report)
        _LAST = {'status':report['status'], 'path':target,
                 'cases':report['recorded_cases'], 'counts':report['counts']}
        try:
            bpy.context.window_manager.clipboard = target
        except (RuntimeError, ReferenceError, AttributeError):
            pass
        print('[ISSMAKER DEEP AUTOLAB]', report['status'], target, report['counts'])
    except Exception as exc:
        _LAST = {'status':'EXPORT_ERROR','path':str(job['folder']), 'error':str(exc)[:500]}
        print('[ISSMAKER DEEP AUTOLAB] export FAILED', traceback.format_exc())
    finally:
        _JOB = None


def _tick():
    global _JOB
    job = _JOB
    if job is None:
        return None
    try:
        proc = job['proc']
        result = proc.poll()
        if result is not None:
            _finish(job, result)
            return None
        if time.monotonic() - job['started'] > sup.TIME_LIMIT_SECONDS:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            _finish(job, proc.returncode, timed_out=True)
            return None
        return 1.0
    except Exception as exc:
        _finish(job, None, error=f'{type(exc).__name__}: {exc}')
        return None


def _destination():
    if os.name == 'nt':
        base = Path(os.environ.get('USERPROFILE') or str(Path.home())) / 'Downloads'
        if not base.is_dir():
            base = Path(bpy.app.tempdir or tempfile.gettempdir())
    else:
        base = Path(bpy.app.tempdir or tempfile.gettempdir())
    path = base / 'ISSMAKER_Diagnostics'
    path.mkdir(parents=True, exist_ok=True)
    return path


class IMA_OT_DeepAutoLab(Operator):
    bl_idname = 'ima.deep_autolab'
    bl_label = 'DEEP AUTOLAB · СЛОЖНЫЕ ТЕСТЫ → ZIP'
    bl_description = ('Одна кнопка: изолированный Blender запускает реальные сложные тесты WALL, '
                      'DWOS, Collision, Reliability; сохраняет ошибки, фикстуры и ZIP, не трогая проект')
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _JOB
        if _JOB is not None:
            self.report({'WARNING'}, 'DEEP AUTOLAB уже работает')
            return {'CANCELLED'}
        binary = str(bpy.app.binary_path or '')
        if not binary or not os.path.isfile(binary):
            self.report({'ERROR'}, 'BLOCKED: исполняемый файл Blender не найден; лаборатория не запускалась')
            return {'CANCELLED'}
        ident = datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        folder = Path(tempfile.gettempdir()) / ('ISSMAKER_DEEP_AUTOLAB_WORK_' + ident)
        destination = None
        log_handles = []
        try:
            folder.mkdir(parents=True, exist_ok=False)
            (folder / 'cases').mkdir()
            (folder / 'fixtures').mkdir()
            (folder / 'logs').mkdir()
            destination = _destination()
            sup.atomic_json(folder / 'run_spec.json', {
                'schema':sup.SCHEMA,'id':ident,'source':'ONE_BUTTON',
                'blender':str(bpy.app.version_string),
                'current_project_not_loaded':True,
                'no_scene_mutation':True,
                'gui_clicks':'NOT_TESTED_GUI', 'timeout_s':sup.TIME_LIMIT_SECONDS,
            })
            (folder / 'README_RU.md').write_text(
                '# ISSMAKER DEEP AUTOLAB\n'
                'Все тесты запущены в отдельном фоновом Blender. Основная сцена не открывалась и не менялась.\n'
                'PASS означает успешную проверку данного сценария, не всего аддона.\n'
                'FAIL/ERROR/TIMEOUT/CRASH требуют анализа. BLOCKED не является PASS.\n'
                'Физические клики, реальный интерактивный Esc, DPI и Ctrl+Z в рабочем GUI не тестировались.\n'
                'В ZIP включены только синтетические тестовые данные; проверьте метаданные перед отправкой.\n',
                encoding='utf-8')
            script = Path(__file__).with_name('deep_autolab_worker.py')
            cmd = [binary, '--background', '--factory-startup', '--python', str(script),
                   '--', '--output', str(folder)]
            for channel in ('stdout','stderr'):
                log_handles.append(open(folder / 'logs' / ('blender_' + channel + '.log'), 'wb'))
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=log_handles[0],stderr=log_handles[1],
                                    creationflags=flags, cwd=str(folder))
            _JOB = {'proc':proc,'folder':str(folder),'destination':str(destination),
                    'id':ident,'started':time.monotonic(),
                    'stdout':log_handles[0], 'stderr':log_handles[1]}
            bpy.app.timers.register(_tick, first_interval=1.0)
            # Existing bounded one-click Support ZIP supplies the real user-session
            # logs/normal failures; never use the user's .blend as a child fixture.
            capture = {'status':'NOT_CAPTURED','included':False}
            try:
                result = bpy.ops.ima.diagnostics_zip('EXEC_DEFAULT')
                if 'FINISHED' in result:
                    source_path = Path(str(context.window_manager.clipboard))
                    if source_path.is_file() and source_path.suffix.lower() == '.zip':
                        if source_path.stat().st_size <= sup.MAX_CAPTURE_FILE:
                            dest = folder / 'support_context'
                            dest.mkdir(exist_ok=True)
                            shutil.copyfile(str(source_path),str(dest / 'CURRENT_SESSION_SUPPORT.zip'))
                            capture={'status':'CAPTURED','included':True,
                                     'bytes':source_path.stat().st_size,
                                     'privacy':'May contain names/paths and sampled coordinates'}
                        else:
                            capture={'status':'BLOCKED_SIZE','included':False,
                                     'bytes':source_path.stat().st_size}
                    else:
                        capture={'status':'BLOCKED_NO_ZIP','included':False}
                else:
                    capture={'status':'BLOCKED_DIAGNOSTIC_OPERATOR','included':False}
            except Exception as cap_exc:
                capture={'status':'ERROR','included':False,
                         'error':f'{type(cap_exc).__name__}: {cap_exc}'[:500]}
            sup.atomic_json(folder / 'context_capture.json',capture)
            self.report({'INFO'}, 'DEEP AUTOLAB запущен в отдельном Blender; Support ZIP включён, если доступен')
            return {'FINISHED'}
        except Exception as exc:
            if _JOB is not None and _JOB.get('folder') == str(folder):
                try:
                    _JOB['proc'].terminate()
                except (OSError, RuntimeError):
                    pass
                _JOB = None
            for handle in log_handles:
                try: handle.close()
                except OSError: pass
            # Attempt to deliver a BLOCKED diagnosis rather than claiming PASS.
            try:
                sup.atomic_json(folder / 'worker_state.json', {'completed':False,'current':'LAUNCH_FAILED'})
                report = sup.final_report(folder,returncode=None,launch_error=repr(exc))
                target=str((destination or _destination()) / ('ISSMAKER_DEEP_AUTOLAB_'+ident+'.zip'))
                sup.package_evidence(folder,target,report)
                context.window_manager.clipboard=target
            except Exception:
                pass
            self.report({'ERROR'},'DEEP AUTOLAB BLOCKED: '+str(exc)[:140])
            return {'CANCELLED'}


CLASSES = (IMA_OT_DeepAutoLab,)

def register():
    from . import runtime
    runtime.register_classes_atomic(CLASSES)

def unregister():
    global _JOB
    job = _JOB
    if job is not None:
        try:
            job['proc'].terminate()
        except (OSError,RuntimeError):
            pass
        # No user scene changed. Worker may leave checkpoint files in temp.
        _JOB = None
    from . import runtime
    runtime.unregister_classes_safe(CLASSES)
