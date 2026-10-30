import bpy
from bpy.app.handlers import persistent
from bpy.types import Operator

from . import log, runtime


_EDITOR_AREAS = {'UV': None, 'SHADER': None}


def _find_area(screen, identity):
    if not identity:
        return None
    screen_pointer, area_pointer = identity
    if screen.as_pointer() != screen_pointer:
        return None
    for area in screen.areas:
        if area.as_pointer() == area_pointer:
            return area
    return None


@persistent
def _load_pre(_dummy):
    _EDITOR_AREAS['UV'] = None
    _EDITOR_AREAS['SHADER'] = None


class IMA_OT_SplitEditor(Operator):
    bl_idname = "ima.split_editor"
    bl_label = "Добавить рабочее окно"
    bl_description = "Добавить отдельный UV Editor или Shader Editor рядом с текущим Viewport"

    @classmethod
    def description(cls, context, properties):
        from . import tooltip_text
        return tooltip_text.editor_split(getattr(properties, 'editor', 'UV'))
    bl_options = {'REGISTER', 'UNDO'}

    editor: bpy.props.EnumProperty(items=(
        ('UV', "UV Editor", "Открыть UV Editor"),
        ('SHADER', "Shader Editor", "Открыть Shader Editor"),
    ))
    factor: bpy.props.FloatProperty(default=0.32, min=0.20, max=0.50)

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == 'VIEW_3D')

    def execute(self, context):
        key = f"split_editor_{self.editor}"
        if not runtime.acquire(key, cooldown=0.75):
            self.report({'WARNING'}, "Окно уже переключается")
            return {'CANCELLED'}
        try:
            return self._execute_locked(context)
        except Exception as exc:
            log.write_exception(f"Split editor {self.editor} failed", exc)
            self.report({'ERROR'}, f"Операция с окном остановлена: {exc}")
            return {'CANCELLED'}
        finally:
            runtime.release(key)

    def _execute_locked(self, context):
        screen = context.screen
        existing = _find_area(screen, _EDITOR_AREAS.get(self.editor))
        expected_type = 'IMAGE_EDITOR' if self.editor == 'UV' else 'NODE_EDITOR'
        expected_ui_type = 'UV' if self.editor == 'UV' else 'ShaderNodeTree'
        if (existing is not None and
                (existing.type != expected_type or
                 getattr(existing, "ui_type", None) != expected_ui_type)):
            existing = None
            _EDITOR_AREAS[self.editor] = None
        if existing is not None:
            try:
                with context.temp_override(window=context.window, screen=screen, area=existing):
                    bpy.ops.screen.area_close()
            except (RuntimeError, ReferenceError) as exc:
                self.report({'ERROR'}, f"Не удалось свернуть окно: {exc}")
                log.write(f"Close split editor failed: {exc}")
                return {'CANCELLED'}
            _EDITOR_AREAS[self.editor] = None
            log.write(f"Split editor closed: {self.editor}")
            self.report({'INFO'}, "Дополнительное окно закрыто")
            return {'FINISHED'}
        # A manually closed area leaves only an integer pointer. Clearing it is safe:
        # Blender RNA data from the deleted area is never dereferenced.
        _EDITOR_AREAS[self.editor] = None

        source = context.area
        source_pointer = context.area.as_pointer()
        source_bounds = (source.x, source.y, source.width, source.height)
        before = {area.as_pointer() for area in screen.areas}
        try:
            bpy.ops.screen.area_split(direction='VERTICAL', factor=self.factor)
        except RuntimeError as exc:
            self.report({'ERROR'}, f"Не удалось разделить окно: {exc}")
            log.write(f"Split editor failed: {exc}")
            return {'CANCELLED'}

        sx, sy, sw, sh = source_bounds
        halves = [area for area in screen.areas
                  if area.y == sy and area.height == sh and
                  sx <= area.x and area.x + area.width <= sx + sw]
        if len(halves) < 2:
            new_areas = [area for area in screen.areas if area.as_pointer() not in before]
            current = _find_area(screen, (screen.as_pointer(), source_pointer))
            halves = new_areas + ([current] if current else [])
        if len(halves) < 2:
            self.report({'ERROR'}, "Новое окно не найдено")
            return {'CANCELLED'}
        # Whichever RNA Area Blender considers "new", the editor is always assigned
        # to the geometrically left half. The right half remains a 3D Viewport.
        target = min(halves, key=lambda area: area.x)

        if self.editor == 'UV':
            target.type = 'IMAGE_EDITOR'
            try:
                target.ui_type = 'UV'
            except (AttributeError, TypeError):
                pass
            label = "UV Editor"
        else:
            target.type = 'NODE_EDITOR'
            try:
                target.ui_type = 'ShaderNodeTree'
                target.spaces.active.shader_type = 'OBJECT'
            except (AttributeError, TypeError):
                pass
            label = "Shader Editor"
        _EDITOR_AREAS[self.editor] = (screen.as_pointer(), target.as_pointer())
        log.write(f"Viewport split: {label}")
        self.report({'INFO'}, f"Открыт {label}")
        return {'FINISHED'}


CLASSES = (IMA_OT_SplitEditor,)


def register():
    runtime.register_classes_atomic(CLASSES)
    try:
        if _load_pre not in bpy.app.handlers.load_pre:
            bpy.app.handlers.load_pre.append(_load_pre)
    except Exception:
        unregister()
        raise


def unregister():
    _load_pre(None)
    if _load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_load_pre)
    runtime.unregister_classes_safe(CLASSES)
