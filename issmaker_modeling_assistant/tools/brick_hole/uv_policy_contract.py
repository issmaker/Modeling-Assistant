"""BH04B.6: strict, pure, fail-closed compatibility contract for SMART UV.

No Blender, HUD or BMesh dependencies. UI and Apply share this decision.
Unsupported matrix cells remain explicit until independent native validation.
"""
POLICIES=('STRICT','ADAPTIVE','CONTINUE')

def resolve_policy(saved, old_edge='STRICT',old_bridge='STRICT'):
    if saved in POLICIES:return saved
    # Saved .blend migration: old Edge mode takes precedence; report ambiguity
    # in the sidebar if legacy Edge/Bridge selections differ.
    if old_edge in POLICIES and old_edge==old_bridge:return old_edge
    # Never silently choose one of two conflicting legacy UV preferences.
    return 'STRICT'

def needs_legacy_choice(saved,old_edge,old_bridge):
    return saved not in POLICIES and old_edge in POLICIES and old_bridge in POLICIES and old_edge!=old_bridge

def support_decision(policy,shape,count,connected=False,boundary=True,*,purpose='EDGE'):
    if policy not in POLICIES:
        return {'ok':False,'reason':'UV-режим не распознан; применение запрещено'}
    if count<=0:
        return {'ok':False,'reason':'Нет выбранных рёбер или граней'}
    if policy=='STRICT':
        return {'ok':True,'reason':'STRICT: производственный Preflight обязателен'}
    if purpose=='MITER':
        return {'ok':False,'reason':'Miter пока подтверждён только в STRICT 1000'}
    if shape!='EDGE':
        return {'ok':False,'reason':'Face Extrude: CONTINUE и ADAPTIVE ещё не проверены; выберите STRICT 1000'}
    if not boundary:
        return {'ok':False,'reason':'Smart UV: нужны граничные рёбра с одной исходной гранью'}
    if not 1<=count<=2:
        return {'ok':False,'reason':'Smart UV: поддержаны 1–2 независимых прямых ребра; остальные — пока нет'}
    if connected:
        return {'ok':False,'reason':'Smart UV: связанные рёбра требуют проверенного Corner Solver'}
    return {'ok':True,'reason':f'{policy}: ограниченный Edge Smart UV; точный Preview не подтверждён'}
