"""W55: one-click conventional native DWOS mullion zoning patterns.

Publishes nothing: stage7 remains the ONLY source of HP/LP geometry.
Old DWOS zone_core owns width safety and all nested split geometry.
"""
from __future__ import annotations
import bpy
from bpy.props import EnumProperty,FloatProperty
from . import surface_steps,zone_core,surface_graph,surface_step_core
PATTERNS={
 'V':(('root','V'),),
 'H':(('root','H'),),
 'CROSS':(('root','V'),('left','H'),('right','H')),
 'TWO_H':(('root','H'),('left','H')),
 'TWO_V':(('root','V'),('left','V')),
 'GRID6':(('root','V'),('left','H'),('left_left','H'),('right','H'),('right_left','H'))
}
class DWOS_OT_surface_mullion_preset(bpy.types.Operator):
 bl_idname='dwos.surface_mullion_preset'
 bl_label='DWOS W55 · Set Mullion Layout'
 bl_description='Stage native editable zone tree for Step 7; does not generate fake mullion objects'
 bl_options={'REGISTER','UNDO'}
 preset:EnumProperty(name="Layout",items=[
 ('V','1 Vertical','One continuous vertical mullion'),
 ('H','1 Horizontal','One continuous horizontal mullion'),
 ('CROSS','Cross','Vertical with two horizontal branches'),
 ('TWO_H','Two Horizontal','Two independent horizontal bars'),
 ('TWO_V','Two Vertical','Two independent vertical bars'),
 ('GRID6','Grid 2×3','Six panes in two columns and three rows')],default='CROSS')
 thickness:FloatProperty(name='Mullion Width',default=.06,min=.01,max=.25,precision=4,subtype='DISTANCE')
 def execute(self,context):
  try:
   host,s=surface_steps._get_session_wall(context)
   if int(s['step'])!=6:raise ValueError('W55_PRESET_ONLY_AT_STEP6')
   source=s['source']['points']
   axes=surface_step_core.rectangle_axes(source) or surface_graph._orthogonal_bounds(source)
   x0,x1,y0,y1=axes;width=x1-x0;height=y1-y0
   fw=float(s['parameters']['frame_width'])
   ux=s['source']['x_axis'];uy=s['source']['y_axis']
   flip=abs(ux[2])>abs(uy[2])+.25
   tree=zone_core.root_node()
   for where,axis in PATTERNS[self.preset]:
    id='root'
    if where!='root':
     pieces=where.split('_')
     cur=tree
     for part in pieces:
      if cur.get('axis') is None or len(cur.get('children',()))!=2:
       raise ValueError('W55_PRESET_PARENT_NOT_BUILT')
      cur=cur['children'][0 if part=='left' else 1]
     id=cur['id']
    real_axis=('H' if axis=='V' else 'V') if flip else axis
    tree,_=zone_core.add(tree,id,real_axis,self.thickness,width,height,fw)
   shape=surface_step_core.safe_inset(source,fw)
   from .surface_zone_next import zone_layout
   zone_layout(shape['outer'],shape['inner'],tree)
   graph=surface_graph.make_graph(shape['outer'],shape['inner'],zone_tree_next=tree)
   if not any(c['role']=='MULLION' for c in graph['cells']):
    raise ValueError('W55_PRESET_HAS_NO_PHYSICAL_MULLION_CELLS')
   s['zone_tree_pending']=zone_core.encode(tree)
   s['DWOS_W55_mullion_preset']=self.preset
   surface_steps._save_session(host,s)
   self.report({'INFO'},f'DWOS: {self.preset} — {len(graph["bars"])} реальных импостов; шаг 7')
   return {'FINISHED'}
  except Exception as ex:
   self.report({'ERROR'},str(ex)[:180])
   return {'CANCELLED'}
CLASSES=(DWOS_OT_surface_mullion_preset,)
