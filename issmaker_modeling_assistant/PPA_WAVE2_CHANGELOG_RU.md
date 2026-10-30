# ISSMAKER PPA Wave 2 — список изменений

**База:** PPA Wave 1, **тип:** TEST candidate, **Native:** NOT VERIFIED, **релиз:** BLOCKED.

| CHG-ID | Было | Стало | Статус |
|---|---|---|---|
| PPA-W2-001 | При общей границе n-gon пара новых граней безусловно пропускалась | Проверяются положительные площади перекрытия по polygon-owned треугольникам | VERIFIED OFFLINE / NATIVE NOT VERIFIED |
| PPA-W2-002 | Две общие вершины считались достаточным признаком соседства | Проверяется фактическое общее ребро полигона; несмежные пары идут в обычный narrow phase | CODE DONE / NATIVE NOT VERIFIED |
| PPA-W2-003 | Потенциальный O(N×M) перебор triangles | Детерминированный interval sweep broad-phase перед exact narrow-phase | VERIFIED OFFLINE |
| PPA-W2-004 | Не было независимого n-gon Oracle | Добавлены Shapely, concave, vertical, CAD, stub и performance тесты | VERIFIED OFFLINE |
| PPA-W2-005 | Изменение защищённого UV Wall могло сломать manifest | Пересчитаны и проверены оба SHA256-манифеста на финальном ZIP | VERIFIED OFFLINE |
| PPA-W2-006 | Не было явного договора независимости WALL/CORNICE | Контракт зафиксирован и проверен на текущем исходнике | VERIFIED SOURCE CONTRACT / NATIVE NOT VERIFIED |

НЕ СДЕЛАНО в этой волне: универсальная проверка произвольных неплоских n-gon, новый 2D Cornice Profile Editor, полный DWOS Arbitrary Opening Solver, Blender Native и пользовательская приёмка.
