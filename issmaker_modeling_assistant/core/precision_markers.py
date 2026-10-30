"""Blender-free marker interaction contracts for ISSMAKER Pass 15.3.

No modeling writes. Runtime controller owns safe commit/Undo and GPU rendering.
"""
from dataclasses import dataclass, field
from math import isfinite
import ast
import re

MARKER_CHOICES = {
    'W_THICKNESS': (('SINGLE', 'Одинарная'), ('DOUBLE', 'Двойная')),
    'W_WALL_DOUBLE': (('INSIDE', 'Внутрь'), ('CENTER', 'Центр'), ('OUTSIDE', 'Наружу')),
    'W_WALL_SINGLE': (('OUTWARD', 'Наружу'), ('INWARD', 'Внутрь'),
                      ('PATH_LEFT', 'Влево от пути'), ('PATH_RIGHT', 'Вправо от пути')),
}

def choices_for(marker, is_double=False):
    if marker == 'W_WALL':
        return MARKER_CHOICES['W_WALL_DOUBLE' if is_double else 'W_WALL_SINGLE']
    return MARKER_CHOICES.get(marker, ())

def marker_presentation(marker, *, hovered=False, double=False, cap=False, plant=False):
    """Return exactly the visible text for marker mode, with numeric styling flags.

    Non-hover CAP/PLANT *always* include their on/off state; disabled secondary
    is non-existent, not a visually hidden clickable rectangle.
    """
    if marker in ('W_CAP','W_PLANT'):
        title='Крышка' if marker=='W_CAP' else 'Клумба'
        enabled=cap if marker=='W_CAP' else plant
        state='Вкл' if enabled else 'Выкл'
        return (f'{title} · {state}', 'Настройки' if hovered and enabled else None,False)
    if marker=='W_THICKNESS':
        return ('Конструкция', 'Толщина' if hovered and double else None,hovered and double)
    if marker=='W_WALL':return ('Положение', 'Смещение' if hovered else None,hovered)
    return (marker,None,False)

def popup_rect(card, area, count, width=196, row_height=29):
    """Anchored dropdown entirely inside view and away from card hitboxes."""
    x0,y0,x1,y1=[float(x) for x in card];rw,rh=[float(x) for x in area]
    count=int(count)
    if not all(isfinite(v) for v in (x0,y0,x1,y1,rw,rh,width,row_height)) or count<=0 or rw<110 or rh<110: return None
    w=min(float(width),max(100,rw-16));h=count*float(row_height)+12.
    if h>rh-16:return None
    x=max(8,min(x0,rw-w-8));y=y0-h-5
    if y<8:y=y1+5
    if y+h>rh-8:y=max(8,rh-h-8)
    return x,y,x+w,y+h

def numeric_spin(initial, pixel_delta, *, kind='HEIGHT', shift=False, ctrl=False):
    """Predictable, continuous displacement; no hidden geometry coupling."""
    a=float(initial);p=float(pixel_delta)
    if not isfinite(a) or not isfinite(p):raise ValueError('Invalid spin input')
    step=.25 if kind in ('SURFACE_ROT','CORNICE_ROT') else .005
    if shift:step*=.1
    if ctrl:step*=10
    value=a + p*step
    if kind in ('HEIGHT','THICKNESS'):value=max(.001,value)
    return value

@dataclass
class InlineNumericBuffer:
    """Caret/selection/clipboard/local Undo, independent of Blender geometry."""
    text: str
    caret: int = 0
    anchor: int = 0
    history: list = field(default_factory=list)
    redo: list = field(default_factory=list)

    def __post_init__(self):
        self.text=str(self.text);self.caret=max(0,min(len(self.text),self.caret));self.anchor=max(0,min(len(self.text),self.anchor))
    @classmethod
    def selected_all(cls, text):return cls(str(text),len(str(text)),0)
    @property
    def span(self):return min(self.anchor,self.caret),max(self.anchor,self.caret)
    @property
    def selected(self):return self.caret != self.anchor
    def snapshot(self):return self.text,self.caret,self.anchor
    def select_all(self):self.anchor=0;self.caret=len(self.text)
    def move(self, direction, extend=False):
        target=0 if direction=='HOME' else len(self.text) if direction=='END' else max(0,min(len(self.text),self.caret+(-1 if direction=='LEFT' else 1)))
        self.caret=target
        if not extend:self.anchor=target
    def _push(self):
        self.history.append(self.snapshot());self.history=self.history[-48:];self.redo.clear()
    def replace(self, value):
        value=str(value)
        if (not value or len(self.text)+len(value)>96 or
                any(x not in '0123456789.,-+*/() cm' for x in value.lower())):return False
        self._push();a,b=self.span;self.text=self.text[:a]+value+self.text[b:]
        self.caret=self.anchor=a+len(value)
        return True
    def erase(self, back=False):
        a,b=self.span
        if a==b:
            if back:a=max(0,a-1)
            else:b=min(len(self.text),b+1)
        if a==b:return False
        self._push();self.text=self.text[:a]+self.text[b:];self.caret=self.anchor=a
        return True
    def copy(self):
        a,b=self.span;return self.text[a:b] if a!=b else self.text
    def cut(self):
        v=self.copy();self.erase(back=False) if self.selected else self.select_all() or self.erase(back=False)
        return v
    def undo(self, redo=False):
        src=self.redo if redo else self.history;dst=self.history if redo else self.redo
        if not src:return False
        dst.append(self.snapshot());self.text,self.caret,self.anchor=src.pop();return True
    def click(self, caret, extend=False):
        self.caret=max(0,min(len(self.text),int(caret)))
        if not extend:self.anchor=self.caret
    def valid_number(self):
        return parse_numeric_expression(self.text)


def parse_numeric_expression(value):
    """Blender-like numeric expressions and m/cm/mm; AST whitelist, never eval."""
    value=str(value).strip().lower().replace(',','.')
    if not value or len(value)>96:return None
    value=re.sub(r'(?<![a-z_0-9])(\d+(?:\.\d+)?)\s*(mm|cm|m)\b',
        lambda match:'('+match.group(1)+'*'+{'mm':'0.001','cm':'0.01','m':'1'}[match.group(2)]+')',value)
    try:node=ast.parse(value,mode='eval').body
    except (ValueError,SyntaxError,TypeError):return None
    def walk(n,depth=0):
        if depth>12:raise ValueError('Expression too deep')
        if isinstance(n,ast.Constant) and type(n.value) in (float,int):return float(n.value)
        if isinstance(n,ast.UnaryOp) and isinstance(n.op,(ast.UAdd,ast.USub)):
            v=walk(n.operand,depth+1)
            return v if isinstance(n.op,ast.UAdd) else -v
        if isinstance(n,ast.BinOp) and isinstance(n.op,(ast.Add,ast.Sub,ast.Mult,ast.Div)):
            a,b=walk(n.left,depth+1),walk(n.right,depth+1)
            if isinstance(n.op,ast.Add):return a+b
            if isinstance(n.op,ast.Sub):return a-b
            if isinstance(n.op,ast.Mult):return a*b
            return a/b
        raise ValueError('Unsupported expression')
    try:
        number=walk(node)
        return number if isfinite(number) and abs(number)<1e12 else None
    except (ValueError,TypeError,OverflowError,ZeroDivisionError):return None
