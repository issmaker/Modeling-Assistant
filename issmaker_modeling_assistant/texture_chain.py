"""Deterministic Blender-independent matching for Diffuse/ERM/Normal UDIM sets.

Never infer a second material from a shared UDIM tile: both asset key and
per-tile numbers must match.  No I/O is performed beyond directory listing.
"""
import os
import re
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTENSIONS={'.png','.tif','.tiff','.jpg','.jpeg','.exr','.tga','.bmp','.webp'}
ROLE_ALIASES={
    'DIFFUSE':{'diffuse','albedo','basecolor','base_color','diff','color'},
    'ERM':{'erm'},
    'NORMAL':{'normal','normals','nrm','nor'},
}
# Match long tokens first, preserving the asset name on both sides of role/UDIM.
_TOKEN=re.compile(r'(?i)(?<![a-z0-9])(?:base[_-]?color|diffuse|albedo|diff|color|erm|normals|normal|nrm|nor)(?![a-z0-9])')
_UDIM=re.compile(r'(?<!\d)(1\d{3})(?!\d)')

class TextureChainError(ValueError): pass

@dataclass(frozen=True)
class TileSet:
    role: str
    asset: str
    tiles: tuple
    files: tuple
    def as_map(self): return dict(self.files)


def parse_name(path):
    p=Path(path)
    if p.suffix.lower() not in IMAGE_EXTENSIONS: return None
    stem=p.stem
    matches=list(_TOKEN.finditer(stem))
    if len(matches)!=1: return None  # fail-closed for ambiguous roles
    match=matches[0]
    token=re.sub(r'[_-]','',match.group(0)).lower()
    role=next((r for r,aliases in ROLE_ALIASES.items()
               if token in {re.sub('[_-]','',a) for a in aliases}),None)
    if role is None:return None
    um=list(_UDIM.finditer(stem))
    if len(um)>1:return None
    udim=int(um[0].group(1)) if um else None
    base=stem[:match.start()]+'_'+stem[match.end():]
    if um:
        base=_UDIM.sub('_',base,count=1)
    asset=re.sub(r'[_\-.\s]+','_',base).strip('_').casefold()
    return role,asset,udim


def scan(directory, reference=None):
    """Return strictly matched role -> TileSet or raise actionable error.

    reference: optional example file of any role; a folder alone works only if
    it contains exactly one unambiguous asset. Both tiled and single files work.
    """
    folder=Path(directory)
    if not folder.is_dir():raise TextureChainError('Папка с текстурами не найдена')
    grouped={}
    for p in sorted(folder.iterdir(),key=lambda x:x.name.casefold()):
        if not p.is_file():continue
        parsed=parse_name(p.name)
        if parsed is None:continue
        role,asset,udim=parsed
        key=(asset,role)
        tiles=grouped.setdefault(key,{})
        slot=udim if udim is not None else 0
        if slot in tiles:
            raise TextureChainError(f'Два файла для {role} / {slot}: {tiles[slot].name}, {p.name}')
        tiles[slot]=p
    if not grouped:raise TextureChainError('Файлы diffuse / erm / normal не найдены')
    if reference:
        r=parse_name(Path(reference).name)
        if r is None:raise TextureChainError('У выбранного файла не распознан тип Diffuse/ERM/Normal')
        asset=r[1]
        if (asset,r[0]) not in grouped:
            raise TextureChainError('Выбранного файла нет в указанной папке')
    else:
        assets=sorted({k[0] for k in grouped})
        if len(assets)!=1:
            raise TextureChainError('В папке несколько наборов: '+', '.join(assets[:8])+'. Выберите один пример файла')
        asset=assets[0]
    roles={}
    for role in ('DIFFUSE','ERM','NORMAL'):
        tiles=grouped.get((asset,role),{})
        if not tiles:raise TextureChainError(f'Для набора «{asset}» не найден {role}')
        nums=tuple(sorted(tiles))
        roles[role]=TileSet(role,asset,nums,tuple((num,str(tiles[num])) for num in nums))
    tiled=any(0 not in roles[r].tiles for r in roles)
    if tiled:
        for role,s in roles.items():
            if 0 in s.tiles:raise TextureChainError(f'{role} — одиночная текстура, остальные — UDIM')
        required=set(roles['DIFFUSE'].tiles)
        if not required or min(required)<1001 or max(required)>1999:
            raise TextureChainError('Неподдерживаемые номера UDIM')
        for role,s in roles.items():
            if set(s.tiles)!=required:
                missing=sorted(required-set(s.tiles))
                extra=sorted(set(s.tiles)-required)
                raise TextureChainError(f'Несовпадение UDIM {role}: нет {missing}, лишние {extra}')
    else:
        for role,s in roles.items():
            if s.tiles!=(0,):raise TextureChainError('Смешаны одиночные и UDIM текстуры')
    return roles


def udim_filepath(tile_set):
    """Get path template with Blender's <UDIM> placeholder."""
    p=tile_set.files[0][1]
    if tile_set.tiles==(0,):return p
    token=str(tile_set.tiles[0])
    stem=Path(p).stem
    matches=[m for m in _UDIM.finditer(stem) if m.group(1)==token]
    if len(matches)!=1:raise TextureChainError(f'Неоднозначный номер UDIM: {p}')
    m=matches[0]
    return str(Path(p).with_name(stem[:m.start()]+'<UDIM>'+stem[m.end():]+Path(p).suffix))
