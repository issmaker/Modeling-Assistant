# ISSMAKER PPA Wave 7 — Change Log

**Base:** PPA Wave 6. **Build:** ISSMAKER-PPA-WAVE7-20261027. **Channel:** TEST ONLY.

| CHG-ID | Было | Стало | Статус |
|---|---|---|---|
| W7-001 | Однопроходная проверка stop и отсутствие общего статуса | Строгий REQUEST→STOP→VERIFY со сбором всех ошибок, reentrancy guard и bounded ledger | VERIFIED OFFLINE |
| W7-002 | Quick Select жил вне session adapters | Добавлен `QUICK_SELECT` adapter и stop-before-preflight для direct worker | VERIFIED OFFLINE |
| W7-003 | Collision overlay off мог скрывать живой modal-picker | Учитываются picker sessions и несколько сцен; cleanup инвалидирует токены | VERIFIED OFFLINE |
| W7-004 | Запускатель дублировал handoff в четырёх ветках | Handoff вызывается один раз до target preflight | VERIFIED OFFLINE |
| W7-005 | Диагностика не содержала единого ownership status | `unified_tool_sessions` в Support ZIP; последние 100 фаз | CODE DONE |
| W7-006 | При unregister не было единого закрытия активных owners | Best-effort `UNREGISTER` handoff до unregister RNA | CODE DONE |
| W7-007 | Не было независимого Chaos verifier | 20 000 детерминированных сценариев + 11 групп; регрессии Wave5/Wave6 | VERIFIED OFFLINE |
| W7-008 | Риск конфликта с защищённым UV Wall | Итоговый ZIP+UV Wall package_preflight+negative tamper test | VERIFIED OFFLINE |

**Запреты:** исходные стены/окна/карнизы, UV, Mesh, WindowDefinition, WallRecovery и геометрические солверы не редактировались.

**Известные ограничения:** Blender не запускался. Модальные операторы Blender могут закончить фактическую обработку только на последующем событии. Нельзя утверждать устранение нативных crash. Нужна проверка в Blender 4.5.5, включая быстрые Ctrl+Z, Esc и переключение окна/файла.
