# ADR — S/V-to-world конструктивный профиль (2.0.54)

## Решение
Держать перед стадией Blender Mesh независимую и тестируемую последовательность:

`source Host contact graph → S/V boundary chart → profile_join_solver → lifted true 3D surface → independent topology/contact/world-width/collision/Float32/UV gates → conditional future assembly`.

Сначала строим только **одну топологически корректную кольцевую полосу** вокруг одного opening — не всю оконную сборку. Внешние станционные вершины S/V должны отображаться ровно на их авторские Host coordinates через `map_svd_host(d=0)`. Внутренние точки размещаются на исходной фасадной поверхности, без flatten source и без изменения Host.

## Не подменять метрику
- `profile_join_solver` доказывает точность ширины в S/V. `svd_ribbon_lab_bridge` вторично измеряет *world-space* перпендикулярную ширину по каждому непоглощённому наружному ребру. Превышение `max(1mm, 1% requested_width)` даёт `WORLD_WIDTH_UNCERTAIN`, а не `CANDIDATE_VALIDATED`.
- Нельзя принимать гладкую param UV как доказательство отсутствия 3D самопересечения или совпадения исходных точек: независимый `spatial_safety_core`, `surface_graph`, `mesh_consistency_core` Float32 и `generation_uv_quality` запускаются на поднятой геометрии.
- Source vertex count/контакт не изменяются; координаты и выбор пользователя не пишутся в диагностический summary.

## Безопасность жизненного цикла
- Старые HP/LP/Glass/Interior полностью авторитетны. Новая лаборатория сохраняет только запись и самодостаточную тестовую сцену; не добавляет «скрытых» объектов и не изменяет текущий `.blend` пользователя.
- Диагностика перезаписывается только после **успешного** Composer rebuild; при отказе исходные Mesh и записанный отчёт идентичны старому состоянию. На начальной сборке валидный SVD QA отчёт добавляется после выбора production-победителя, но до host cut.
- Не публиковать реальный новый профиль вместо HP, пока не интегрированы depth/frame extrusion, glass, interior, material, source LP/HP UV, mullion/junction graph, external/interior seams, обратимый commit.

## Тесты
Плоский, угол, много-сегментный зигзаг, исходный 18-фасетный curved Host patch, reversed winding, altered contact, wrong world scale и invalid width. Выпуск ZIP после smoke/performance/regression/hot reload; фактически проверенная платформа Windows Blender 5.2.3 LTS.
