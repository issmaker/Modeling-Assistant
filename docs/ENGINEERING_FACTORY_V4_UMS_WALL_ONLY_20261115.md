# Engineering Factory V4 — UMS source synchronization and Wall-only UI

Дата: 2026-11-15  
Проект: ISSMAKER Unified Modeling Suite  
Источник: локально установленное Blender Extension `bl_ext.user_default.issmaker_modeling_assistant`  
Версия пакета: `2.0.84`; Blender: `5.2.4 LTS`  
Исходный GitHub SHA: `de3e5f2c00e54e4eb36f807c0eb59d9236ef924a` (устаревший initial import 2026-10-30)

## Архитектурный проход (Architect)

Целевая модель: оставить `UV_WALL` и `COLLISION`; единственная UI-компоновка Wall — `FLOATING_PRO`. Полностью исключить исходники, регистрации, операторы и старые runtime-модули удалённых инструментов и Studio Dock. Сохранять working scene и установленный аддон без изменений.

## Developer: исходное состояние локальной копии

- Каталоги `tools/dwos_windows`, `tools/brick_hole` отсутствуют.
- `ui/studio_dock.py`, `ui/dock_workspace_state.py`, `viewport/studio_dock_layout.py` отсутствуют.
- `core.registry.TOOLS` содержит только `COLLISION` и `UV_WALL`, `validate_registry()` прошёл.
- `active_ui_mode()` возвращает `FLOATING_PRO`; устаревшие операторы `dwos.viewport_composer` и `view3d.brick_hole_interactive` не зарегистрированы.
- Оператор `uvwall.viewport_inspector` зарегистрирован, устаревшие Python-модули не загружены.

## QA / Performance / Evidence Guardian

- Static: `compile()` — 268/268 Python-файлов PASS.
- In-process unit tests: 60/60 PASS, 0 ошибок/пропусков. Состав объектов, активный объект, режим и текущий кадр рабочей сцены не изменились.
- Isolated Blender `--background --factory-startup`: регистрация и снятие регистрации UMS PASS; 207/207 тестов PASS (0 FAIL, 0 ERROR, 0 SKIP).
- Первый фоновый прогон без регистрации UMS дал 6 ошибок подготовки окружения; повтор после корректного `addon.register()` — 207/207 PASS.
- GUI mouse interaction, визуальная приёмка Floating Pro, производительность GPU: **NOT_TESTED**. Не объявлять RELEASE PASS.
- GitHub перенос: исходный публичный репозиторий сильно отставал от локальной установки. Синхронизированный контент загружен в отдельную ветку через Git-объекты; `main` не изменён.
- Публикация GitHub-ветки и итоговый commit SHA подтверждаются отдельно после проверки дерева.

## NEXT_ACTION

Провести интерактивное тестирование Floating Pro в изолированном проекте Blender без изменения пользовательской сцены, проверить GPU draw и hot-reload, выполнить финальный Evidence Guardian и принять решение о выпуске. Не утверждать, что автономные инженерные агенты выполняли работу: все ролевые проходы — методология GPT-6.

Задача: https://github.com/issmaker/Modeling-Assistant/issues/2
