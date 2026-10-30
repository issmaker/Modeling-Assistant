"""Module-aware material / texture validation for UV Wall NEXTGEN 1.6.2.

This module intentionally keeps user materials read-only.  It inspects node trees,
selects an unambiguous image when possible and reports why manual selection is
needed otherwise.  2×2 and 4×4 standards are strict production contracts.
"""
from __future__ import annotations


def expected_pixels(module_standard):
    return 2048 if str(module_standard) == "2" else 4096


def module_label(module_standard):
    return "2×2 м / 2K" if str(module_standard) == "2" else "4×4 м / 4K"


def image_resolution(image):
    if image is None:
        return (0, 0)
    try:
        return int(image.size[0]), int(image.size[1])
    except Exception:
        return (0, 0)


def material_image_candidates(material):
    """Return unique Image datablocks referenced by Image Texture nodes."""
    if material is None or not getattr(material, "use_nodes", False):
        return []
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return []
    result = []
    seen = set()
    for node in getattr(tree, "nodes", ()):  # works with Blender RNA and offline fakes
        if getattr(node, "type", None) != "TEX_IMAGE":
            continue
        image = getattr(node, "image", None)
        if image is None:
            continue
        key = getattr(image, "name_full", None) or getattr(image, "name", None) or id(image)
        if key in seen:
            continue
        seen.add(key)
        result.append(image)
    return result


def _upstream_images(socket, visited=None):
    """Find image sources connected to Base Color, including reroutes and groups.

    Conservative: only connected unambiguous color-image graphs are accepted.
    Multiple PBR images (normal/ORM/etc) do not become random UV references.
    """
    visited = visited if visited is not None else set()
    result = []
    for link in getattr(socket, "links", ()):
        node = getattr(link, "from_node", None)
        if node is None or id(node) in visited:
            continue
        visited.add(id(node))
        if getattr(node, "type", "") == "TEX_IMAGE" and getattr(node, "image", None) is not None:
            result.append(node.image)
        elif getattr(node, "type", "") in {"REROUTE", "MIX_RGB", "MIX", "HUE_SAT", "GAMMA", "BRIGHTCONTRAST"}:
            for ins in getattr(node, "inputs", ()):
                result.extend(_upstream_images(ins, visited))
    return result


def material_primary_image(material):
    """Use Material's Base Color texture, never an independent texture pointer.

    A material with a single image is supported even if it is not connected.
    Multi-texture materials require an unambiguous Base Color connection.
    """
    if material is None:
        return None, "Не выбран материал"
    candidates = material_image_candidates(material)
    if not candidates:
        return None, "В материале нет Image Texture с изображением"
    tree = getattr(material, "node_tree", None)
    sources = []
    if tree is not None:
        for node in getattr(tree, "nodes", ()):
            if getattr(node, "type", "") != "BSDF_PRINCIPLED":
                continue
            base = getattr(node, "inputs", None)
            if hasattr(base, "get"):
                base = base.get("Base Color")
            else:
                base = next((x for x in base or () if getattr(x, "name", "") == "Base Color"), None)
            if base is not None:
                sources.extend(_upstream_images(base))
    unique = []
    for image in sources:
        if image not in unique:
            unique.append(image)
    if len(unique) == 1:
        return unique[0], ""
    if len(unique) > 1:
        return None, "К Base Color подключено несколько изображений: укажите одну основную цветовую текстуру в материале"
    if len(candidates) == 1:
        return candidates[0], ""
    return None, "В материале несколько текстур: подключите основную цветовую Image Texture к Base Color"


def material_module(material):
    """Return (module '2'|'4'|None, image, reason) from the material ONLY."""
    image, reason = material_primary_image(material)
    if image is None:
        return None, None, reason
    res = image_resolution(image)
    if res == (2048, 2048):
        return "2", image, "2×2 м / 2K · 1000 px/m"
    if res == (4096, 4096):
        return "4", image, "4×4 м / 4K · 1000 px/m"
    return None, image, (f"Текстура материала имеет {res[0]}×{res[1]}px; "
                         "поддерживаются только 2048×2048 и 4096×4096")


def inspect_material_asset(material, explicit_image, module_standard):
    """Validate the MATERIAL ONLY. Legacy explicit_image is intentionally ignored."""
    module, image, reason = material_module(material)
    expected = expected_pixels(module_standard)
    if material is None:
        status = "MISSING"
    elif image is None:
        status = "AMBIGUOUS" if len(material_image_candidates(material)) > 1 else "NO_IMAGE"
    elif module is None:
        status = "WRONG_RESOLUTION"
    elif expected_pixels(module) != expected:
        status = "WRONG_MODULE"
        reason = f"Материал задаёт {module}K, активный модуль {module_standard}K: обновите материал для синхронизации"
    else:
        status = "VALID"
    return {"status": status, "expected": expected, "material": material,
            "image": image, "candidates": material_image_candidates(material),
            "resolution": image_resolution(image), "source": "MATERIAL",
            "message": reason}

def active_definition_asset(definition):
    if definition is None:
        return None, None
    is_2k = str(getattr(definition, "module_standard", "4")) == "2"
    return (
        getattr(definition, "virtual_material_2k" if is_2k else "virtual_material_4k", None),
        getattr(definition, "virtual_image_2k" if is_2k else "virtual_image_4k", None),
    )


def set_active_definition_asset(definition, *, material=None, image=None):
    """Assign material in its resolution slot, synchronizing the module size.

    `image` only remains in the signature for backwards compatibility; it is
    deliberately ignored. User-authored image slots are never used as input.
    """
    if definition is None or material is None:
        return False
    module, actual_image, reason = material_module(material)
    if module is None:
        return False
    definition.module_standard = module
    setattr(definition, "virtual_material_2k" if module == "2" else "virtual_material_4k", material)
    # Clear deprecated independent image pointers on the current material.
    setattr(definition, "virtual_image_2k" if module == "2" else "virtual_image_4k", None)
    try:
        definition.revision += 1
    except Exception:
        pass
    return True
