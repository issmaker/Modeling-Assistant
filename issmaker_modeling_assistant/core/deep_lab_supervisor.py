"""Pure-process Deep Autolab supervisor primitives, no bpy imports."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import time
import zipfile

SCHEMA = 'issmaker.deep_autolab.1'
TIME_LIMIT_SECONDS = 420
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_CAPTURE_FILE = 50 * 1024 * 1024


def load_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as stream:
            return json.load(stream)
    except (OSError, ValueError, TypeError):
        return default


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.pending')
    with open(temporary, 'w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, default=str)
    os.replace(temporary, path)


def summarize_cases(folder):
    records = []
    for path in sorted((Path(folder) / 'cases').glob('*.json')):
        row = load_json(path)
        if isinstance(row, dict) and row.get('case_id'):
            records.append(row)
        else:
            records.append({'case_id':'CORRUPT_CHECKPOINT_'+path.stem,
                            'module':'LAB_RUNTIME','status':'ERROR',
                            'error':'Unreadable or incomplete case checkpoint: '+path.name})
    counts = {key: 0 for key in ('PASS','FAIL','ERROR','BLOCKED','TIMEOUT','CRASH','NOT_TESTED_GUI')}
    for row in records:
        status = str(row.get('status', 'ERROR'))
        counts[status] = counts.get(status, 0) + 1
    return {'counts': counts, 'cases': len(records), 'rows': records}


def final_report(folder, *, returncode, timed_out=False, launch_error=None):
    folder = Path(folder)
    summary = summarize_cases(folder)
    state = load_json(folder / 'worker_state.json', {})
    finished = bool(isinstance(state, dict) and state.get('completed'))
    if launch_error:
        status = 'BLOCKED'
    elif timed_out:
        status = 'TIMEOUT'
    elif returncode is None:
        status = 'ERROR'
    elif returncode != 0:
        status = 'CRASH'
    elif not finished or summary['cases'] == 0:
        status = 'ERROR'
    elif summary['counts'].get('FAIL', 0) or summary['counts'].get('ERROR', 0):
        status = 'FAIL'
    elif summary['counts'].get('BLOCKED', 0):
        status = 'PARTIAL'
    else:
        status = 'COMPLETE'
    # Actual GUI click and interactive Blender undo are never executed in background.
    result = {
        'schema': SCHEMA, 'status': status,
        'blender_child_returncode': returncode,
        'worker_completed': finished,
        'timeout': bool(timed_out),
        'launch_error': str(launch_error or ''),
        'counts': summary['counts'], 'recorded_cases': summary['cases'],
        'last_worker_stage': (state.get('current') if isinstance(state, dict) else None),
        'native_background': bool(finished and returncode == 0),
        'native_gui': 'NOT_TESTED_GUI',
        'production_scene': 'NOT_TESTED',
        'cases': [{k: row.get(k) for k in ('case_id','module','status','duration_ms','stage','error')}
                  for row in summary['rows']],
    }
    atomic_json(folder / 'summary.json', result)
    return result


def package_evidence(folder, target, report):
    folder = Path(folder); target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    allowed = ('cases/', 'fixtures/', 'logs/', 'environment.json',
               'worker_state.json', 'summary.json', 'README_RU.md',
               'context_capture.json', 'support_context/',
               'native_limits.json', 'run_spec.json')
    meta = []
    for path in sorted(folder.rglob('*')):
        if not path.is_file() or path.is_symlink() or path.name.endswith('.pending'):
            continue
        relative = path.relative_to(folder).as_posix()
        if not any(relative == key or relative.startswith(key) for key in allowed):
            continue
        size = path.stat().st_size
        if size > (MAX_LOG_BYTES if relative.startswith('logs/') else MAX_CAPTURE_FILE):
            meta.append({'path':relative,'included':False,'reason':'MAX_FILE_BUDGET','size':size})
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        meta.append({'path':relative,'included':True,'size':size,'sha256':digest})
    manifest = {'schema': SCHEMA, 'status': report['status'], 'files':meta}
    atomic_json(folder / 'manifest.json', manifest)
    tmp = target.with_name(target.name + '.pending')
    with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for entry in meta:
            if entry.get('included'):
                archive.write(folder / entry['path'], arcname=entry['path'])
        archive.write(folder / 'manifest.json', arcname='manifest.json')
    with zipfile.ZipFile(tmp, 'r') as archive:
        bad = archive.testzip()
        if bad is not None:
            raise RuntimeError('ZIP CRC failure: ' + bad)
    os.replace(tmp, target)
    return str(target)
