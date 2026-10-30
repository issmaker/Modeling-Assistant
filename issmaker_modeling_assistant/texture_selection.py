"""Image-space color sampling for the quick Select Linked delimiter."""
from array import array
from math import floor, isfinite

# Bound only the additional CPU-side float32 copy, not the existing Blender image.
MAX_TEXTURE_SNAPSHOT_BYTES = 320 * 1024 * 1024


def default_image_name(obj):
    material = getattr(obj, 'active_material', None)
    tree = getattr(material, 'node_tree', None)
    if tree is None:
        return ''
    # Prefer the actual Base Color input over unrelated normal/roughness maps.
    for node in tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            socket = node.inputs.get('Base Color')
            for link in socket.links if socket else ():
                source = link.from_node
                if source.type == 'TEX_IMAGE' and source.image:
                    return source.image.name
    active = tree.nodes.active
    if active and active.type == 'TEX_IMAGE' and active.image:
        return active.image.name
    images = {node.image.name for node in tree.nodes
              if node.type == 'TEX_IMAGE' and node.image}
    return next(iter(images)) if len(images) == 1 else ''


class TextureSampler:
    def __init__(self, image, wrap='REPEAT'):
        if image is None:
            raise RuntimeError('Выберите изображение цветового атласа')
        if image.source not in {'FILE', 'GENERATED'} or image.type == 'MULTILAYER':
            raise RuntimeError('Texture Color: нужен обычный атлас, не UDIM-набор, видео или Multilayer')
        self.width, self.height = image.size
        self.channels = image.channels
        if self.width < 1 or self.height < 1 or self.channels not in {1, 3, 4}:
            raise RuntimeError('Изображение не загружено или имеет неподдерживаемые каналы')
        size = self.width * self.height * self.channels
        if len(image.pixels) != size:
            raise RuntimeError('Не удалось получить пиксели атласа')
        if size * 4 > MAX_TEXTURE_SNAPSHOT_BYTES:
            raise RuntimeError(
                'Texture Color: атлас слишком велик для безопасного чтения; '
                'используйте 4K RGBA или более компактное изображение')
        try:
            self.pixels = array('f', [0.0]) * size
            image.pixels.foreach_get(self.pixels)
        except MemoryError as exc:
            raise RuntimeError('Texture Color: недостаточно оперативной памяти') from exc
        self.wrap = wrap

    def sample(self, u, v):
        if not (isfinite(u) and isfinite(v)):
            return None
        if self.wrap == 'REPEAT':
            u, v = u - floor(u), v - floor(v)
        else:
            u, v = min(1.0, max(0.0, u)), min(1.0, max(0.0, v))
        x = min(self.width - 1, int(u * self.width))
        y = min(self.height - 1, int(v * self.height))
        offset = (y * self.width + x) * self.channels
        if self.channels == 1:
            color = (self.pixels[offset],) * 3
        else:
            color = tuple(self.pixels[offset:offset + 3])
        return color if all(isfinite(value) for value in color) else None

    def face_color(self, face, uv_layer):
        count = len(face.loops)
        if not count:
            return None
        return self.sample(
            sum(loop[uv_layer].uv.x for loop in face.loops) / count,
            sum(loop[uv_layer].uv.y for loop in face.loops) / count)


def colors_match(color, reference, tolerance):
    return (color is not None and reference is not None and
            max(abs(a - b) for a, b in zip(color, reference)) <= tolerance)
