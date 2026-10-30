# PPA Wave 4 — Persistent Mesh Recovery · Change Ledger

Основание: `ISSMAKER_PPA_WAVE3_WALL_HISTORY_RECOVERY_TEST.zip` (SHA256 `f14a00d3b198623dc6acd2cf92c0d6f5932e27332b50bae3e85fc43fceb5a64a`).

| CHG-ID | Было | Стало | Риск / Контроль | Статус |
|---|---|---|---|---|
| PPA-W4-001 | Откат Mesh только в текущем Build; нет долговечного backup | `core/mesh_recovery.py`: один независимый persistent Mesh после успешного WALL Build | Blender ID life, память; 5k model fuzz, UID isolation | OFFLINE VERIFIED, NATIVE NOT VERIFIED |
| PPA-W4-002 | При повреждении принятого Mesh нельзя восстановить без генератора | Явная `object.uvwall_restore_mesh_snapshot` с Blender `UNDO`, проверкой владельца, идентичности и fingerprint | Deep checksum only on click; assignment rollback mocks | OFFLINE VERIFIED, NATIVE NOT VERIFIED |
| PPA-W4-003 | Повторные сохранения могли бы плодить копии | Старый backup заменяется одним актуальным, не удаляя используемые чужие ID; advisory ресурсный лимит | Blender `use_fake_user`, undo ownership | OFFLINE VERIFIED, NATIVE NOT VERIFIED |
| PPA-W4-004 | Нет кнопки восстановления geometry | `UV Wall > ВОССТАНОВЛЕНИЕ MESH` у выбранной принятой WALL | Нет доступа во время viewport tool / transaction | CODE DONE, UI NATIVE NOT VERIFIED |
| PPA-W4-005 | Support ZIP не сообщает о резервных копиях | `recovery_snapshots.json`: bounded metadata без тяжёлого хэширования | Не изменяет Mesh | CODE DONE, NATIVE NOT VERIFIED |
| PPA-W4-006 | Последний recovery не защищён от подмены по UID | Owner UID + Result UID + токен + SHA256 основных полей Mesh | Fingerprint не включает ВСЕ custom attributes | OFFLINE VERIFIED |
| PPA-W4-007 | Package преемственность могла снова нарушить установку | 2 манифеста обновлены, preflight final ZIP + tamper test | Любое изменение после pack требует пересборки | OFFLINE VERIFIED |
| PPA-W4-008 | Жизненный цикл Undo оценивался без специальных negative scenarios | 100 mock undo/redo callbacks со stale Area; no new undo handlers | Python mock не эквивалент Blender C Undo | OFFLINE VERIFIED / NATIVE BLOCKED |

Геометрические генераторы WALL/SURFACE/CORNICE, DWOS, Brick Hole, Collision Inspector, UV math: НЕ ИЗМЕНЯЛИСЬ в Wave 4. Предыдущий механизм Build rollback остаётся неизменным. Полную историю Mesh revisions и защиту от C-level crash этот проход НЕ ЗАВЕРШАЕТ.
