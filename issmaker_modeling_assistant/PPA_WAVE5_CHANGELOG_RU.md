# ISSMAKER PPA Wave 5 — Что было → что стало

| CHG-ID | Было | Стало | Область | Status |
|---|---|---|---|---|
| PPA-W5-001 | `undo_audit` полагался только на `_REGISTERED`, старые hot-reload callbacks могли остаться | Вычищаются обработчики прошлых инкарнаций по module/function name, выполняется повторная точная регистрация | Core / Global Undo | VERIFIED OFFLINE |
| PPA-W5-002 | Один `action_trace` / DWOS disarm / redraw exception мог прервать остальные этапы | Стадии PRE/POST изолированы, сбои отдельно фиксируются в bounded fault ledger | Core / Global Undo | VERIFIED OFFLINE |
| PPA-W5-003 | UV Wall Undo cleanup выполнялся последовательно без stage isolation | Даже при exception в result cleanup работают performance invalidation и build guard reset | UV Wall | VERIFIED OFFLINE |
| PPA-W5-004 | Статус Undo lifecycle отсутствовал в Support ZIP | Добавлен `global_undo_lifecycle`, JSON-safe callback counts, faults, guards | Diagnostics | CODE DONE |
| PPA-W5-005 | HUD GPU `load_post` не удалял callbacks старого Python-модуля | Dedup и removal по module/name при повторной установке/выключении | HUD GPU | VERIFIED OFFLINE |
| PPA-W5-006 | Smart Interior DWOS `_registered` не замечал callbacks предыдущей инкарнации | Безопасная повторная регистрация / очистка callbacks | DWOS | VERIFIED OFFLINE |
| PPA-W5-007 | UV Wall core undo handlers отсутствовали в Runtime Identity | Добавлены в 58 защищённых runtime sources; Integrity 106 записей обновлён | Package | VERIFIED OFFLINE |
| PPA-W5-008 | Не было stage fault adversarial tests | Добавлены 18+7 offline групп и native Chaos test plan | QA | VERIFIED OFFLINE / NATIVE NOT RUN |

**Не выполнено:** доказательное устранение Blender C-crashes; полная регенерация GPU modal lifecycle в Collision Inspector и stale pending DWOS `_run_queue`; невыполненный Blender Native acceptance блокирует RELEASE. Генераторы WALL / SURFACE / CORNICE / DWOS Mesh и UV не менялись.
