# ISSMAKER PPA Wave 6 — Таблица изменений (Protocol 2.0)

| CHG-ID | Было | Стало | Риск | Проверка | Статус |
|---|---|---|---|---|---|
| PPA-W6-001 | GPU handles хранились только в module global | `core/lifecycle_lease` хранит owner epoch и GPU tokens для следующей загрузки | driver_namespace очищается при load | Lease Oracle; loaded .blend cases | Offline VERIFIED |
| PPA-W6-002 | Collision после hot reload мог оставить два draw handlers и timer | Регистрация reclaims/removes old handle; timers, hooks и unregistration защищены от late old module | provider GPU handles | 7 500 randomized registration cycles | Offline VERIFIED |
| PPA-W6-003 | Сбой одного Collision teardown мог прекратить остальную очистку | Независимое best-effort освобождение и bounded errors | скрытые native errors | forced remove failure oracle | Offline VERIFIED |
| PPA-W6-004 | DWOS статический overlay имел только `_HANDLE` текущего модуля | Explicit owner lease, rebind после load, GPU depth state через `finally` | GPU API | tests overlay reload, finally | Offline VERIFIED |
| PPA-W6-005 | Отложенный DWOS Smart Interior timer мог пережить обновление модуля и обратиться к старой сцене | Generation guard + rebind после `.blend` load + late-old unregister guards | stale Blender scene | queue mock tests + lost references | Offline VERIFIED |
| PPA-W6-006 | DWOS Composer animation closures продолжали держать старого оператора | Epoch guard для двух timer closures, cleanup старого GPU handle при новой сессии, безопасный modal init | ESC/menu latency | pulse closure & static AST tests | Offline VERIFIED |
| PPA-W6-007 | В UV Wall второй `draw_handler_add` мог упасть после первого без rollback | Единый защищённый участок создания двух handles/timer/modal со снятием при отказе | inactive inspector | 4 injected-fault mocks | Offline VERIFIED |
| PPA-W6-008 | Support ZIP не включал единый GPU/Modal lifecycle state | `gpu_modal_lifecycle` status с четырьмя независимыми providers | диагностика может встретить freed RNA | snapshot isolation test | Offline VERIFIED |
| PPA-W6-009 | UV Wall защищённые контрольные суммы не соответствовали бы правке | Integrity и Runtime manifests пересчитаны на финальных исходниках | package preflight | final extracted ZIP and corruption test | Offline VERIFIED |
| PPA-W6-010 | Предыдущие Python Undo guarantees могли регрессировать | Повторный запуск 18/18 Wave 5 Python regression | cross-module changes | Wave 5 native-independent mocks | Offline VERIFIED |

**Blender Native: NOT VERIFIED. RELEASE BLOCKED.** Важно: фиксы lifecycle не гарантируют отсутствие C-level crash в Blender, особенно при GPU teardown в разных окна/областях. Релиз только после нативной приёмки и визуальной проверки.
