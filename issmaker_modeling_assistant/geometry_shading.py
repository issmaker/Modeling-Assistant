"""Apply Blender's bundled Smooth by Angle to one Mesh, then bake the modifier."""
import os
import bpy

def apply_angle_smoothing(context,obj,angle):
    if obj is None or obj.type!='MESH' or context.mode!='OBJECT':
        raise ValueError('Для сглаживания выберите Mesh в Object Mode')
    if obj.data.users>1:
        raise ValueError('Сначала сделайте Mesh Single User')
    uv_snapshot={layer.name:[tuple(item.uv) for item in layer.data]
                 for layer in obj.data.uv_layers}
    with context.temp_override(object=obj,active_object=obj,selected_objects=[obj],selected_editable_objects=[obj]):
        if obj.data.has_custom_normals:bpy.ops.mesh.customdata_custom_splitnormals_clear()
        for edge in obj.data.edges:edge.use_edge_sharp=False
        # Direct library loading avoids asynchronous Asset Browser operators.
        group=next((g for g in bpy.data.node_groups if g.get('issmaker_smooth_asset')),None)
        if group is None:
            asset=os.path.join(bpy.utils.system_resource('DATAFILES'),'assets','geometry_nodes','smooth_by_angle.blend')
            with bpy.data.libraries.load(asset,link=False) as (src,dst):
                dst.node_groups=[name for name in src.node_groups if name=='Smooth by Angle']
            group=next((g for g in dst.node_groups if g is not None),None)
            if group is None:raise RuntimeError('Не найден встроенный Smooth by Angle')
            group['issmaker_smooth_asset']=True
        modifier=obj.modifiers.new('ISSMAKER · Smooth by Angle','NODES');modifier.node_group=group
        modifier_name=modifier.name
        try:
            for socket in group.interface.items_tree:
                if getattr(socket,'in_out','')=='INPUT':
                    if socket.name=='Angle':modifier[socket.identifier]=float(angle)
                    elif socket.name=='Ignore Sharpness':modifier[socket.identifier]=True
            result=bpy.ops.object.modifier_apply(modifier=modifier.name)
            if 'FINISHED' not in result:raise RuntimeError('Blender не применил сглаживание')
            # Applying the native GN asset can round interpolated UV by one
            # float32 ULP. Smooth by Angle preserves loop topology: restore
            # authored loop values exactly, including non-active UV maps.
            for name,values in uv_snapshot.items():
                layer=obj.data.uv_layers.get(name)
                if layer is None or len(layer.data)!=len(values):
                    raise RuntimeError('Smooth by Angle изменил структуру UV')
                layer.data.foreach_set('uv',[v for point in values for v in point])
            obj['issmaker_smooth_angle']=float(angle)
        finally:
            remaining=obj.modifiers.get(modifier_name)
            if remaining is not None:obj.modifiers.remove(remaining)
            if group.users==0:bpy.data.node_groups.remove(group)
    obj.data.update()
