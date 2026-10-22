# OpenVAT core functions

import bpy
import bmesh
import math
import mathutils
import os
from . import utils

# Rotation from Blender's Z-up frame into the FBX data frame for Unity space:
# (x, y, z) -> (-x, z, y). Unity negates X on FBX import, so the asset lands in Unity as
# (x, z, y): Blender's front view (looking along +Y) matches Unity's default view (along +Z).
UNITY_SPACE_ROTATION = (math.pi / 2, 0.0, math.pi)
# Object rotation that cancels the FBX exporter's -Z forward / Y up root rotation, so the
# Unity root imports with an identity rotation
UNITY_SPACE_FBX_ROOT_ROTATION = (math.pi / 2, 0.0, 0.0)

def get_scene_compositor_tree(scene: bpy.types.Scene, ensure: bool = True, name: str | None = None):
    

    #ensure=True will create a CompositorNodeTree and wire it up if missing.

    if scene is None:
        raise ValueError("Scene is None")

    # Blender 5.0+ path: scene.node_tree removed, use compositing_node_group instead
    if hasattr(scene, "compositing_node_group"):
        tree = scene.compositing_node_group

        if tree is None and ensure:
            base_name = name or f"{scene.name}_Compositor"
            tree = bpy.data.node_groups.new(base_name, "CompositorNodeTree")
            scene.compositing_node_group = tree  # also auto-sets use_nodes in 5.x. :contentReference[oaicite:1]{index=1}

        return tree

    # Legacy 4.x path
    if ensure and not getattr(scene, "use_nodes", False):
        scene.use_nodes = True

    return scene.node_tree

def get_compositor_output_socket(tree: bpy.types.NodeTree,
                                 ensure_interface: bool = True):

    # 5.x path: the "output" is a NodeGroupOutput node + interface socket
    if tree.bl_idname == "CompositorNodeTree" and hasattr(bpy.context.scene, "compositing_node_group"):
        out_node = None
        for n in tree.nodes:
            if n.bl_idname == "NodeGroupOutput":
                out_node = n
                break
        if out_node is None:
            out_node = tree.nodes.new("NodeGroupOutput")
            out_node.is_active_output = True

        if ensure_interface:
            tree.interface.new_socket(
                name="Image",
                in_out='OUTPUT',
                socket_type='NodeSocketColor'
            )

        return out_node, out_node.inputs[0]

    # 4.x path: regular Composite node
    comp = None
    for n in tree.nodes:
        if n.bl_idname == "CompositorNodeComposite":
            comp = n
            break
    if comp is None:
        comp = tree.nodes.new("CompositorNodeComposite")
    return comp, comp.inputs[0]

def setup_alpha_over_node(alpha_over_node,
                          image_node,
                          render_layers_node,
                          output_socket,
                          links):

    # --- Alpha handling: 5.x "Straight Alpha" vs 4.x "use_premultiply" ---
    straight_input = None
    for inp in alpha_over_node.inputs:
        if getattr(inp, "identifier", "") == "straight_alpha" or inp.name == "Straight Alpha":
            straight_input = inp
            break
    
    # Blender 5.x path
    if straight_input is not None:
        straight_input.default_value = True
    
    # Blender 4.x path
    elif hasattr(alpha_over_node, "use_premultiply"):
        alpha_over_node.use_premultiply = False

    # Factor
    fac_input = alpha_over_node.inputs.get("Fac", None)
    if fac_input is None and len(alpha_over_node.inputs) > 0:
        fac_input = alpha_over_node.inputs[0]
    if fac_input is not None:
        fac_input.default_value = 1.0

    # --- BG / FG sockets: names in 5.x, indices in 4.x ---
    bg_socket = alpha_over_node.inputs.get("Background", None)
    fg_socket = alpha_over_node.inputs.get("Foreground", None)

    if bg_socket is None or fg_socket is None:
        # 4.x Path
        if len(alpha_over_node.inputs) >= 3:
            bg_socket = alpha_over_node.inputs[1]
            fg_socket = alpha_over_node.inputs[2]
        else:
            raise RuntimeError("Alpha Over node does not have expected BG/FG inputs")

    # Outputs: use "Image" by name, else index 0
    img_out = image_node.outputs.get("Image", None) or image_node.outputs[0]
    rl_out  = render_layers_node.outputs.get("Image", None) or render_layers_node.outputs[0]
    alpha_out = alpha_over_node.outputs.get("Image", None) or alpha_over_node.outputs[0]

    # Wire BG/FG
    links.new(img_out, bg_socket)   # BG
    links.new(rl_out,  fg_socket)   # FG

    # Wire Alpha Over into final output
    links.new(alpha_out, output_socket)

    return alpha_over_node


# Create VAT UV map with bmesh
def create_uv_map(obj, screen_width, screen_height, frames):
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(obj.data)
    uv_layer = bm.loops.layers.uv.new("VAT_UV")
    index_layer = bm.verts.layers.int.new("index_orig")

    for i, vert in enumerate(bm.verts):
        vert[index_layer] = i

    sorted_verts = sorted(bm.verts, key=lambda v: v[index_layer], reverse=True)
    num_verts = len(sorted_verts)
    pixel_size_x = 1.0 / screen_width
    pixel_size_y = 1.0 / screen_height
    # Written by ov_sort-by-column when vertices share columns
    column_layer = bm.verts.layers.int.get("ov_column")

    for i, vert in enumerate(sorted_verts):
        slot = vert[column_layer] if column_layer is not None else i
        uv_x = (slot % screen_width) / screen_width + pixel_size_x / 2
        uv_y = 1.0 - ((slot // screen_width) * frames) / screen_height - pixel_size_y / 2

        for loop in vert.link_loops:
            loop[uv_layer].uv = (uv_x, uv_y)

    if column_layer is not None:
        bm.verts.layers.int.remove(column_layer)

    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode='OBJECT')
    obj.data.uv_layers.active_index = len(obj.data.uv_layers) - 1

def get_max_y(res_x, res_y, ortho_scale=10.0):
    aspect = res_x / res_y
    if aspect >= 1:
        return ortho_scale / aspect / 2
    else:
        return ortho_scale / 2

COLUMN_SORT_GROUP = "ov_sort-by-column"

def get_socket_identifier(node_group, name):
    return node_group.interface.items_tree[name].identifier

def get_socket(sockets, identifier):
    return next(socket for socket in sockets if socket.identifier == identifier)

def ensure_column_sort_group():
    # Reorders vertices so one representative per shared column sits where the encoder reads
    # it, and tags every vertex with its column for create_uv_map
    group = bpy.data.node_groups.get(COLUMN_SORT_GROUP)
    if group:
        return group

    group = bpy.data.node_groups.new(COLUMN_SORT_GROUP, 'GeometryNodeTree')
    group.interface.new_socket(name="Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    group.interface.new_socket(name="Column Table", in_out='INPUT', socket_type='NodeSocketObject')
    group.interface.new_socket(name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    nodes = group.nodes
    links = group.links

    group_in = nodes.new("NodeGroupInput")
    group_out = nodes.new("NodeGroupOutput")
    group_out.location = (1000, 0)
    table = nodes.new("GeometryNodeObjectInfo")
    table.location = (0, -300)
    links.new(group_in.outputs["Column Table"], table.inputs["Object"])
    index = nodes.new("GeometryNodeInputIndex")
    index.location = (200, -500)

    def sample_table(attribute_name, data_type, y):
        attribute = nodes.new("GeometryNodeInputNamedAttribute")
        attribute.data_type = data_type
        attribute.inputs["Name"].default_value = attribute_name
        attribute.location = (200, y)
        sample = nodes.new("GeometryNodeSampleIndex")
        sample.data_type = data_type
        sample.domain = 'POINT'
        sample.location = (400, y)
        links.new(table.outputs["Geometry"], sample.inputs["Geometry"])
        links.new(attribute.outputs["Attribute"], sample.inputs["Value"])
        links.new(index.outputs["Index"], sample.inputs["Index"])
        return sample.outputs["Value"]

    store = nodes.new("GeometryNodeStoreNamedAttribute")
    store.data_type = 'INT'
    store.domain = 'POINT'
    store.inputs["Name"].default_value = "ov_column"
    store.location = (600, 0)
    links.new(group_in.outputs["Geometry"], store.inputs["Geometry"])
    links.new(sample_table("ov_column", 'INT', -200), store.inputs["Value"])

    sort = nodes.new("GeometryNodeSortElements")
    sort.domain = 'POINT'
    sort.location = (800, 0)
    links.new(store.outputs["Geometry"], sort.inputs["Geometry"])
    links.new(sample_table("ov_sort", 'FLOAT', -400), sort.inputs["Sort Weight"])
    links.new(sort.outputs["Geometry"], group_out.inputs["Geometry"])
    return group

def add_column_sort(obj, column_table):
    group = ensure_column_sort_group()
    mod = obj.modifiers.new(name="ov_column_sort", type='NODES')
    mod.node_group = group
    utils.set_modifier_input(mod, get_socket_identifier(group, "Column Table"), column_table)
    return mod

def ensure_slot_limit(group):
    # Patches the appended encoder once per file: drop the faces for slots at or past "Slots"
    # (0 keeps all). With shared columns the vertices that reuse a column sit in those slots,
    # and their faces would otherwise wrap into rows below, over packed normals
    if "Slots" in group.interface.items_tree:
        return

    nodes = group.nodes
    links = group.links
    slot_store = nodes.get("Store Named Attribute.003")
    if slot_store is None or slot_store.inputs["Name"].default_value != "vertFrame":
        raise RuntimeError("ov_calculate-position-vs does not match the expected layout; cannot limit slots")

    group.interface.new_socket(name="Slots", in_out='INPUT', socket_type='NodeSocketInt')

    x, y = slot_store.location
    group_in = nodes.new("NodeGroupInput")
    group_in.location = (x, y - 500)
    slot = nodes.new("GeometryNodeInputNamedAttribute")
    slot.data_type = 'FLOAT'
    slot.inputs["Name"].default_value = "vertFrame"
    slot.location = (x, y - 350)
    past_end = nodes.new("FunctionNodeCompare")
    past_end.data_type = 'FLOAT'
    past_end.operation = 'GREATER_EQUAL'
    past_end.location = (x + 200, y - 350)
    links.new(slot.outputs["Attribute"], past_end.inputs["A"])
    links.new(group_in.outputs["Slots"], past_end.inputs["B"])
    limited = nodes.new("FunctionNodeCompare")
    limited.data_type = 'INT'
    limited.operation = 'GREATER_THAN'
    limited.location = (x + 200, y - 550)
    links.new(group_in.outputs["Slots"], limited.inputs["A"])
    drop = nodes.new("FunctionNodeBooleanMath")
    drop.operation = 'AND'
    drop.location = (x + 400, y - 400)
    links.new(past_end.outputs["Result"], drop.inputs[0])
    links.new(limited.outputs["Result"], drop.inputs[1])

    delete = nodes.new("GeometryNodeDeleteGeometry")
    delete.domain = 'FACE'
    delete.location = (x + 600, y - 200)
    for link in list(slot_store.outputs["Geometry"].links):
        links.new(delete.outputs["Geometry"], link.to_socket)
    links.new(slot_store.outputs["Geometry"], delete.inputs["Geometry"])
    links.new(drop.outputs["Boolean"], delete.inputs["Selection"])

def ensure_relative_normal_encoding(group):
    # Patches the appended encoder once per file: optionally subtract the proxy's normal from the
    # target's, and expose the normal remap range (defaults keep absolute normals unchanged)
    if "RelativeNormals" in group.interface.items_tree:
        return

    nodes = group.nodes
    links = group.links
    target_normal = nodes.get("Sample Index.002")
    remap = nodes.get("Map Range.001")
    proxy_position = nodes.get("Object Info.002")
    if target_normal is None or remap is None or proxy_position is None:
        raise RuntimeError("ov_calculate-position-vs does not match the expected layout; cannot add relative normals")

    group.interface.new_socket(name="RelativeNormals", in_out='INPUT', socket_type='NodeSocketBool')
    normal_min = group.interface.new_socket(name="nrm_min", in_out='INPUT', socket_type='NodeSocketVector')
    normal_min.default_value = (-1.0, -1.0, -1.0)
    normal_max = group.interface.new_socket(name="nrm_max", in_out='INPUT', socket_type='NodeSocketVector')
    normal_max.default_value = (1.0, 1.0, 1.0)

    x, y = target_normal.location
    group_in = nodes.new("NodeGroupInput")
    group_in.location = (x - 400, y - 300)
    proxy = nodes.new("GeometryNodeObjectInfo")
    proxy.transform_space = proxy_position.transform_space
    proxy.location = (x - 200, y - 300)
    links.new(group_in.outputs["Proxy"], proxy.inputs["Object"])

    normal = nodes.new("GeometryNodeInputNormal")
    normal.location = (x - 200, y - 500)
    proxy_normal = nodes.new("GeometryNodeSampleIndex")
    proxy_normal.data_type = 'FLOAT_VECTOR'
    proxy_normal.domain = 'POINT'
    proxy_normal.location = (x, y - 300)
    links.new(proxy.outputs["Geometry"], proxy_normal.inputs["Geometry"])
    links.new(normal.outputs["Normal"], proxy_normal.inputs["Value"])
    links.new(target_normal.inputs["Index"].links[0].from_socket, proxy_normal.inputs["Index"])

    offset = nodes.new("ShaderNodeVectorMath")
    offset.operation = 'SUBTRACT'
    offset.location = (x + 200, y - 200)
    links.new(target_normal.outputs["Value"], offset.inputs[0])
    links.new(proxy_normal.outputs["Value"], offset.inputs[1])

    switch = nodes.new("GeometryNodeSwitch")
    switch.name = "ov_normal_basis"
    switch.input_type = 'VECTOR'
    switch.location = (x + 400, y - 100)
    links.new(group_in.outputs["RelativeNormals"], switch.inputs["Switch"])
    links.new(offset.outputs["Vector"], switch.inputs["True"])

    for link in list(target_normal.outputs["Value"].links):
        if link.to_node != offset:
            links.new(switch.outputs["Output"], link.to_socket)
    links.new(target_normal.outputs["Value"], switch.inputs["False"])

    links.new(group_in.outputs["nrm_min"], get_socket(remap.inputs, "From_Min_FLOAT3"))
    links.new(group_in.outputs["nrm_max"], get_socket(remap.inputs, "From_Max_FLOAT3"))

def add_mirror_x_input(group, location):
    # Group input "MirrorX" and a vector that is (-1, 1, 1) when it is set, else (1, 1, 1)
    group.interface.new_socket(name="MirrorX", in_out='INPUT', socket_type='NodeSocketBool')
    group_in = group.nodes.new("NodeGroupInput")
    group_in.location = location
    mirror = group.nodes.new("GeometryNodeSwitch")
    mirror.input_type = 'VECTOR'
    mirror.inputs["False"].default_value = (1.0, 1.0, 1.0)
    mirror.inputs["True"].default_value = (-1.0, 1.0, 1.0)
    mirror.location = (location[0] + 200, location[1])
    group.links.new(group_in.outputs["MirrorX"], mirror.inputs["Switch"])
    return mirror.outputs["Output"]

def mirror_consumers(group, socket, mirror):
    # Route everything reading socket through socket * mirror
    multiply = group.nodes.new("ShaderNodeVectorMath")
    multiply.operation = 'MULTIPLY'
    multiply.location = (socket.node.location[0] + 200, socket.node.location[1] + 150)
    for link in list(socket.links):
        group.links.new(multiply.outputs["Vector"], link.to_socket)
    group.links.new(socket, multiply.inputs[0])
    group.links.new(mirror, multiply.inputs[1])

def ensure_mirror_x_encoding(group):
    # Patches the appended encoder once per file: optionally negate X of position offsets and
    # normals. Unity space uses it to write VAT data directly in Unity's left-handed mesh frame
    # (Unity mirrors the FBX mesh on import, but samples the VAT as stored)
    if "MirrorX" in group.interface.items_tree:
        return

    offset = group.nodes.get("Vector Math")
    normal = group.nodes.get("ov_normal_basis")
    if offset is None or offset.operation != 'SUBTRACT' or normal is None:
        raise RuntimeError("ov_calculate-position-vs does not match the expected layout; cannot mirror X")

    mirror = add_mirror_x_input(group, (offset.location[0], offset.location[1] + 400))
    mirror_consumers(group, offset.outputs["Vector"], mirror)
    mirror_consumers(group, normal.outputs["Output"], mirror)

def ensure_mirror_x_preview(group):
    # Patches the appended preview decoder once per file: undo the encoder's MirrorX so the
    # preview, which keeps the exported mesh's frame, decodes Unity-space data correctly
    if "MirrorX" in group.interface.items_tree:
        return

    set_position = group.nodes.get("Set Position.001")
    if set_position is None or not set_position.inputs["Offset"].is_linked:
        raise RuntimeError("ov_vat-decoder-vs does not match the expected layout; cannot mirror X")

    x, y = set_position.location
    mirror = add_mirror_x_input(group, (x - 600, y + 400))
    mirror_consumers(group, set_position.inputs["Offset"].links[0].from_socket, mirror)
    normal = group.nodes.get("ov_normal_decode")
    if normal is not None:
        mirror_consumers(group, get_socket(normal.outputs, "Vector"), mirror)

def set_normal_encoding_inputs(mod, settings):
    group = mod.node_group
    relative = settings.relative_normals
    low, high = utils.relative_normal_range(settings.image_format) if relative else (-1.0, 1.0)
    utils.set_modifier_input(mod, get_socket_identifier(group, "RelativeNormals"), relative)
    utils.set_modifier_input(mod, get_socket_identifier(group, "nrm_min"), (low,) * 3)
    utils.set_modifier_input(mod, get_socket_identifier(group, "nrm_max"), (high,) * 3)

def ensure_normal_preview(group):
    # Patches the appended preview decoder once per file to decode VAT normals onto the mesh.
    # Needs the Set Mesh Normal node; older Blender keeps the position-only preview
    if "Decode Normals" in group.interface.items_tree or not hasattr(bpy.types, "GeometryNodeSetMeshNormal"):
        return

    nodes = group.nodes
    links = group.links
    set_position = nodes.get("Set Position.001")
    position_sample = nodes.get("Image Texture")
    if set_position is None or position_sample is None:
        raise RuntimeError("ov_vat-decoder-vs does not match the expected layout; cannot add normal preview")
    frame_uv = position_sample.inputs["Vector"].links[0].from_socket
    geometry_link = set_position.inputs["Geometry"].links[0]
    source_geometry = geometry_link.from_socket

    group.interface.new_socket(name="Decode Normals", in_out='INPUT', socket_type='NodeSocketBool')
    group.interface.new_socket(name="Normal Image", in_out='INPUT', socket_type='NodeSocketImage')
    group.interface.new_socket(name="Normal V Offset", in_out='INPUT', socket_type='NodeSocketFloat')
    group.interface.new_socket(name="RelativeNormals", in_out='INPUT', socket_type='NodeSocketBool')
    normal_min = group.interface.new_socket(name="nrm_min", in_out='INPUT', socket_type='NodeSocketVector')
    normal_min.default_value = (-1.0, -1.0, -1.0)
    normal_max = group.interface.new_socket(name="nrm_max", in_out='INPUT', socket_type='NodeSocketVector')
    normal_max.default_value = (1.0, 1.0, 1.0)

    x, y = set_position.location
    x -= 1400
    y -= 600
    group_in = nodes.new("NodeGroupInput")
    group_in.location = (x, y)

    # Packed normals sit below the positions in the same image
    offset = nodes.new("ShaderNodeCombineXYZ")
    offset.location = (x + 200, y - 200)
    links.new(group_in.outputs["Normal V Offset"], offset.inputs["Y"])
    normal_uv = nodes.new("ShaderNodeVectorMath")
    normal_uv.operation = 'ADD'
    normal_uv.location = (x + 400, y - 100)
    links.new(frame_uv, normal_uv.inputs[0])
    links.new(offset.outputs["Vector"], normal_uv.inputs[1])

    sample = nodes.new("GeometryNodeImageTexture")
    sample.interpolation = position_sample.interpolation
    sample.extension = position_sample.extension
    sample.location = (x + 600, y)
    links.new(group_in.outputs["Normal Image"], sample.inputs["Image"])
    links.new(normal_uv.outputs["Vector"], sample.inputs["Vector"])

    decode = nodes.new("ShaderNodeMapRange")
    decode.name = "ov_normal_decode"
    decode.data_type = 'FLOAT_VECTOR'
    decode.clamp = False
    decode.location = (x + 800, y)
    links.new(sample.outputs["Color"], get_socket(decode.inputs, "Vector"))
    links.new(group_in.outputs["nrm_min"], get_socket(decode.inputs, "To_Min_FLOAT3"))
    links.new(group_in.outputs["nrm_max"], get_socket(decode.inputs, "To_Max_FLOAT3"))

    # Relative normals are offsets from the mesh's own normal, read before Set Position moves it
    rest_normal = nodes.new("GeometryNodeInputNormal")
    rest_normal.location = (x + 800, y - 300)
    restored = nodes.new("ShaderNodeVectorMath")
    restored.operation = 'ADD'
    restored.location = (x + 1000, y - 200)
    links.new(get_socket(decode.outputs, "Vector"), restored.inputs[0])
    links.new(rest_normal.outputs["Normal"], restored.inputs[1])
    basis = nodes.new("GeometryNodeSwitch")
    basis.input_type = 'VECTOR'
    basis.location = (x + 1200, y - 100)
    links.new(group_in.outputs["RelativeNormals"], basis.inputs["Switch"])
    links.new(get_socket(decode.outputs, "Vector"), basis.inputs["False"])
    links.new(restored.outputs["Vector"], basis.inputs["True"])
    normalized = nodes.new("ShaderNodeVectorMath")
    normalized.operation = 'NORMALIZE'
    normalized.location = (x + 1400, y - 100)
    links.new(basis.outputs["Output"], normalized.inputs[0])

    set_normal = nodes.new("GeometryNodeSetMeshNormal")
    set_normal.mode = 'FREE'
    set_normal.domain = 'POINT'
    set_normal.location = (x + 1600, y + 100)
    links.new(source_geometry, set_normal.inputs["Mesh"])
    links.new(normalized.outputs["Vector"], set_normal.inputs["Custom Normal"])

    use_normals = nodes.new("GeometryNodeSwitch")
    use_normals.input_type = 'GEOMETRY'
    use_normals.location = (x + 1800, y + 200)
    links.new(group_in.outputs["Decode Normals"], use_normals.inputs["Switch"])
    links.new(source_geometry, use_normals.inputs["False"])
    links.new(set_normal.outputs["Mesh"], use_normals.inputs["True"])
    links.new(use_normals.outputs["Output"], set_position.inputs["Geometry"])

def set_normal_preview_inputs(mod, settings, position_image, normal_image):
    group = mod.node_group
    if "Decode Normals" not in group.interface.items_tree:
        return
    decode = settings.encode_type == 'DEFAULT' and settings.vat_normal_encoding != 'NONE' and normal_image is not None
    relative = decode and settings.relative_normals
    low, high = utils.relative_normal_range(settings.image_format) if relative else (-1.0, 1.0)
    utils.set_modifier_input(mod, get_socket_identifier(group, "Decode Normals"), decode)
    if decode:
        utils.set_modifier_input(mod, get_socket_identifier(group, "Normal Image"), normal_image)
    utils.set_modifier_input(mod, get_socket_identifier(group, "Normal V Offset"),
                             -0.5 if normal_image is position_image else 0.0)
    utils.set_modifier_input(mod, get_socket_identifier(group, "RelativeNormals"), relative)
    utils.set_modifier_input(mod, get_socket_identifier(group, "nrm_min"), (low,) * 3)
    utils.set_modifier_input(mod, get_socket_identifier(group, "nrm_max"), (high,) * 3)

# The bulk of the encoding computation happens within an appended geometry node group - The following code defines properties to set up the geometry nodes to capture data
def setup_vat_tracker(vat_scene, obj_name, num_frames, width, height, num_wraps, proxy_name, original_scene, nodegroup_method, pack_normals, use_custom, custom_attribute, custom_remap):
    ypos = get_max_y(width, height)
    bpy.ops.mesh.primitive_plane_add(location=(0, ypos, 0))
    tracker_plane = bpy.context.object
    tracker_plane.name = f"{obj_name}_vat-tracking"
    
    bpy.ops.object.modifier_add(type='NODES')
    mod = tracker_plane.modifiers[-1]  # Always the last added modifier
    mod.name = "ov_tracking"  # Rename to a fixed, known string

    # Now safely reference it by name
    # *Note to harden custom socket naming on template*
    mod.node_group = bpy.data.node_groups[nodegroup_method]

    utils.set_modifier_input(mod, "Socket_4", bpy.data.objects[obj_name])
    utils.set_modifier_input(mod, "Socket_2", num_frames)
    utils.set_modifier_input(mod, "Socket_10", num_wraps)
    utils.set_modifier_input(mod, "Socket_8", width)
    utils.set_modifier_input(mod, "Socket_9", height)
    utils.set_modifier_input(mod, "Socket_11", bpy.data.objects[proxy_name])
    utils.set_modifier_input(mod, "Socket_6", original_scene['min_x'])
    utils.set_modifier_input(mod, "Socket_7", original_scene['max_x'])
    utils.set_modifier_input(mod, "Socket_12", original_scene['min_y'])
    utils.set_modifier_input(mod, "Socket_13", original_scene['max_y'])
    utils.set_modifier_input(mod, "Socket_14", original_scene['min_z'])
    utils.set_modifier_input(mod, "Socket_15", original_scene['max_z'])
    utils.set_modifier_input(mod, "Socket_19", vat_scene.frame_start)
    utils.set_modifier_input(mod, get_socket_identifier(mod.node_group, "Slots"), original_scene.get('ov_columns', 0))
    utils.set_modifier_input(mod, get_socket_identifier(mod.node_group, "MirrorX"), utils.mirrors_x(original_scene.vat_settings))
    if not use_custom:
        set_normal_encoding_inputs(mod, original_scene.vat_settings)


    if use_custom:
        utils.set_modifier_input(mod, "Socket_23", True)
        utils.set_modifier_input(mod, "Socket_20", original_scene.vat_settings.custom_attr_1)
        utils.set_modifier_input(mod, "Socket_26", original_scene.vat_settings.custom_attr_2)
        utils.set_modifier_input(mod, "Socket_27", original_scene.vat_settings.custom_attr_3)
        if custom_remap:
            utils.set_modifier_input(mod, "Socket_22", True)
    else:
        if bpy.data.scenes[original_scene.name].vat_settings.vat_normal_encoding == 'PACKED':
            normal_tracker = tracker_plane.copy()
            normal_tracker.data = tracker_plane.data.copy()
            vat_scene.collection.objects.link(normal_tracker)
            normal_tracker.location[1] = 0
            normal_mod = normal_tracker.modifiers.get("ov_tracking")
            if normal_mod:
                utils.set_modifier_input(normal_mod, "Socket_17", True)
        
        
def setup_proxy_scene(obj, num_frames, width, height, num_wraps, temp_obj, pack_normals, framestart, column_table=None):
    original_scene = bpy.context.scene
    settings = original_scene.vat_settings
    
    bpy.ops.scene.new(type='NEW')
    proxy_scene = bpy.context.scene
    proxy_scene.name = f"{obj.name}_proxy_scene"

    proxy_scene.frame_end = framestart + num_frames - 1
    
    if settings.proxy_method == 'START_FRAME':
        proxy_scene.frame_current = framestart
    proxy_scene.frame_start = framestart

    proxy_obj = temp_obj.copy()
    proxy_obj.data = temp_obj.data.copy()
    proxy_scene.collection.objects.link(proxy_obj)
    
    bpy.context.view_layer.objects.active = proxy_obj
    proxy_obj.select_set(True)

    for modifier in proxy_obj.modifiers[:]:
        utils.apply_modifier(proxy_obj, modifier)
    if column_table is not None:
        # Applied on its own: apply_modifier evaluates the whole remaining stack
        utils.apply_modifier(proxy_obj, add_column_sort(proxy_obj, column_table))
    
    bpy.ops.object.editmode_toggle()
    bpy.ops.object.editmode_toggle()

    bpy.ops.object.modifier_add(type='NODES')
    proxy_obj.modifiers[-1].node_group = bpy.data.node_groups["ov_generated-pos"]
    create_uv_map(proxy_obj, width, height, num_frames)
        
    # Main VAT render
    setup_vat_scene(proxy_obj, obj.name, original_scene.name, num_frames, width, height, num_wraps, pack_normals)
    
    # Get VAT Result
    base_format = ''.join(filter(str.isalpha, original_scene.vat_settings.image_format))
    image_extension = '.' + base_format.lower()
    image_result = bpy.data.images[obj.name.replace("_ovbake", "") + "_vat" + image_extension]
    
    
    bpy.context.window.scene = proxy_scene
    vat_obj = proxy_obj.copy()
    vat_obj.data = proxy_obj.data.copy()
    
    original_scene.collection.objects.link(vat_obj)
    bpy.context.window.scene = original_scene
    bpy.ops.object.select_all(action='DESELECT')
    bpy.context.view_layer.objects.active = vat_obj
    vat_obj.select_set(True)
    
    for modifier in vat_obj.modifiers[:]:
        vat_obj.modifiers.remove(modifier)
    bpy.ops.object.modifier_add(type='NODES')
    mod = vat_obj.modifiers[-1]
    mod.node_group = bpy.data.node_groups["ov_vat-decoder-vs"]
    
    utils.set_modifier_input(mod, "Socket_2", "VAT_UV", is_attribute_name=True)
    utils.set_modifier_input(mod, "Socket_6", num_frames)
    utils.set_modifier_input(mod, "Socket_7", height)
    utils.set_modifier_input(mod, "Socket_9", image_result)
    utils.set_modifier_input(mod, "Socket_3", original_scene['min_x'])
    utils.set_modifier_input(mod, "Socket_4", original_scene['max_x'])
    utils.set_modifier_input(mod, "Socket_10", original_scene['min_y'])
    utils.set_modifier_input(mod, "Socket_11", original_scene['max_y'])
    utils.set_modifier_input(mod, "Socket_12", original_scene['min_z'])
    utils.set_modifier_input(mod, "Socket_13", original_scene['max_z'])
    utils.set_modifier_input(mod, "Socket_14", original_scene.frame_start)
    if settings.vat_normal_encoding == 'PACKED':
        normal_image = image_result
    else:
        normal_image = bpy.data.images.get(obj.name.replace("_ovbake", "") + "_vnrm" + image_extension)
    set_normal_preview_inputs(mod, settings, image_result, normal_image)
    utils.set_modifier_input(mod, get_socket_identifier(mod.node_group, "MirrorX"), utils.mirrors_x(settings))

    bpy.ops.object.editmode_toggle()
    bpy.ops.object.editmode_toggle()
    vat_obj.name = obj.name.replace("_ovbake", "_vat")
    if settings.engine_space == 'UNITY':
        # The mesh is in Unity space; undo that rotation so the preview matches the source
        vat_obj.rotation_euler = mathutils.Euler(UNITY_SPACE_ROTATION).to_matrix().inverted().to_euler()

    # Move to OpenVATPreview collection
    vat_coll = bpy.data.collections.get("OpenVATPreview")
    if not vat_coll:
        vat_coll = bpy.data.collections.new("OpenVATPreview")
        #original_scene.collection.children.link(vat_coll)
    
    original_scene.collection.objects.unlink(vat_obj)
    vat_coll.objects.link(vat_obj)
    tempmain = vat_obj.name.replace("_vat", "_ovbake")
    bpy.ops.object.select_all(action='DESELECT')
    vat_obj.select_set(True)
    
    bpy.context.view_layer.objects.active = vat_obj
    if settings.clean_mesh:
        utils.clean_mesh_data(vat_obj)
    
    vat_obj.data.name=vat_obj.name.replace("_vat", "_mesh")
    bpy.data.objects.remove(obj)
    
    if settings.export_mesh:
        export_vat_model(settings.mesh_format, include_materials=False, include_tangents=True)


def setup_vat_scene(proxy_obj, obj_name, original_scene_name, num_frames, width, height, num_wraps, pack_normals):
    bpy.ops.scene.new(type='NEW')
    vat_scene = bpy.context.scene
    vat_scene.name = f"{obj_name}_vat"
    original_scene = bpy.data.scenes[original_scene_name]
    output_dir = bpy.path.abspath(original_scene.vat_settings.vat_output_directory)
    
    vat_scene.frame_start = bpy.data.scenes[original_scene_name].frame_start
    vat_scene.frame_end = bpy.data.scenes[original_scene_name].frame_end
    camera = bpy.data.cameras.new("Camera")
    camera_obj = bpy.data.objects.new("Camera", camera)
    vat_scene.collection.objects.link(camera_obj)
    vat_scene.camera = camera_obj
    camera_obj.location = (0, 0, 10)
    camera_obj.data.type = 'ORTHO'
    camera_obj.data.ortho_scale = 10
    
    vat_scene.render.resolution_x = width
    vat_scene.render.resolution_y = height
    vat_scene.display_settings.display_device = 'sRGB'
    vat_scene.view_settings.view_transform = 'Raw'
    vat_scene.render.film_transparent = True
    
    fmt = original_scene.vat_settings.image_format
    img_settings = vat_scene.render.image_settings

    if fmt in {'PNG8', 'PNG16'}:
        img_settings.file_format = 'PNG'
        img_settings.color_mode = 'RGBA'
        img_settings.color_depth = '16'
        img_settings.compression = 0

    elif fmt in {'EXR16', 'EXR32'}:
        img_settings.file_format = 'OPEN_EXR'
        img_settings.color_mode = 'RGBA'
        img_settings.color_depth = '32'
        img_settings.exr_codec = 'NONE'
        
    # Extension-Agnostic
    vat_scene.render.image_settings.compression = 0
    vat_scene.render.dither_intensity = 0
    vat_scene.render.use_compositing = False
    vat_scene.render.use_sequencer = False
    vat_scene.eevee.taa_render_samples = 1
    
    base_format = ''.join(filter(str.isalpha, fmt))
    image_format = '.' + base_format.lower()
        
    original_scene = bpy.data.scenes[original_scene_name]
    os.makedirs(output_dir, exist_ok=True)
    render_vat_scene(vat_scene, 0, output_dir, image_format, fmt)
    vat_scene.render.use_compositing = True
    
    # Set compositing nodes in VAT scene
    setup_compositing(vat_scene, output_dir, vat_scene.name, proxy_obj, image_format, fmt)

    nodegroup_method = "ov_calculate-position-vs"
    
    encode_settings = original_scene.vat_settings
    use_custom = False
    custom_attribute = ""
    custom_remap = False
    if encode_settings.encode_type == 'CUSTOM':
        use_custom = True
        custom_attribute = encode_settings.user_attribute # Custom Vector
        custom_remap = encode_settings.custom_remap
        pack_normals = False
         
    setup_vat_tracker(vat_scene, obj_name, num_frames, width, height, num_wraps, proxy_obj.name, original_scene, nodegroup_method, pack_normals, use_custom, custom_attribute, custom_remap)
    print ("VAT Tracker Created")
    print ("Starting render process...")
    
    
    # Render VAT   
    render_vat_scene(vat_scene, num_frames, output_dir, image_format, fmt)

    output_name = vat_scene.name.replace("_ovbake", "")
    output_path = os.path.join(output_dir, f"{output_name}", f"{output_name}{image_format}")
    vat_scene.render.filepath = output_path

    #un-normalize
    if original_scene.vat_settings.image_format == "EXR32":
        if original_scene.vat_settings.no_remap:
            setup_unnormalize(vat_scene,original_scene,"os-remap")
            bpy.ops.render.render(write_still=True)
            bpy.data.images[output_name+".exr"].reload()
    
    # Render VNRM
    if not pack_normals: 
        if bpy.data.scenes[original_scene_name].vat_settings.encode_type == 'DEFAULT':
            if bpy.data.scenes[original_scene_name].vat_settings.vat_normal_encoding != 'NONE':
                print ("Starting normals render process...")
                utils.set_modifier_input(bpy.context.object.modifiers[-1], "Socket_17", True
)
                rendername = vat_scene.name.replace("_ovbake_vat", "_vnrm")
                vat_scene.render.use_compositing = False
            
                # Prep
                render_vat_nrml(vat_scene, 0, output_dir, image_format, fmt)
                vat_scene.render.use_compositing = True

                tree = get_scene_compositor_tree(vat_scene, ensure=True, name=f"{vat_scene.name}_vnrm")

                
                image_node = tree.nodes["Image"]
                image = bpy.data.images.get(rendername + image_format)
                image_node.image = image
                image.colorspace_settings.name = 'Non-Color'
                
                #Render
                render_vat_nrml(vat_scene, num_frames, output_dir, image_format, fmt)   
    
# Set up compositing for the per frame capture overlay in the vat scene
def setup_compositing(vat_scene, output_dir, scene_name, proxy_obj, image_format, raw_format):
    tree = get_scene_compositor_tree(vat_scene, ensure=True, name=f"{vat_scene.name}_Compositor")
    if tree is None:
        print("❌ No compositor tree available on scene; skipping compositing setup.")
        return

    links = tree.links

    # Clear all existing nodes
    for node in list(tree.nodes):
        tree.nodes.remove(node)

    # Create nodes
    render_layers_node = tree.nodes.new("CompositorNodeRLayers")
    image_node         = tree.nodes.new("CompositorNodeImage")
    alpha_over_node    = tree.nodes.new("CompositorNodeAlphaOver")

    # Get the correct "final output" socket for this Blender version
    output_node, output_socket = get_compositor_output_socket(tree, ensure_interface=True)

    # Load previously composited image
    image_name = vat_scene.name.replace("_ovbake", "") + image_format
    image = bpy.data.images.get(image_name)
    if not image:
        print(f"Image not found: {image_name}")
        return

    image_node.image = image
    image.colorspace_settings.name = 'Non-Color'
    
    #setup and link this node to composite output - backwards compatible 4.x-5.0
    setup_alpha_over_node(alpha_over_node, image_node, render_layers_node, output_socket, links)



def setup_unnormalize(vat_scene, original_scene, attribute_name):
    tree = get_scene_compositor_tree(vat_scene, ensure=True, name=f"{vat_scene.name}_unnorm")
    if tree is None:
        print("❌ No compositor node tree available.")
        return

    links = tree.links

    # Clear existing nodes
    for node in list(tree.nodes):
        tree.nodes.remove(node)

    # Load per-channel remap min/max from original_scene custom properties
    min_x = original_scene['min_x']
    min_y = original_scene['min_y']
    min_z = original_scene['min_z']
    max_x = original_scene['max_x']
    max_y = original_scene['max_y']
    max_z = original_scene['max_z']

    # Load baked EXR
    image_name = vat_scene.name.replace("_ovbake", "") + ".exr"
    image = bpy.data.images.get(image_name)
    if not image:
        print(f"Image not found: {image_name}")
        return
    image.colorspace_settings.name = 'Non-Color'
    image.use_half_precision = False

    # Create nodes
    image_node    = tree.nodes.new("CompositorNodeImage")
    separate_node = tree.nodes.new("CompositorNodeSepRGBA")
    combine_node  = tree.nodes.new("CompositorNodeCombRGBA")

    image_node.image = image
    links.new(image_node.outputs[0], separate_node.inputs[0])

    for i, (min_val, max_val) in enumerate([(min_x, max_x), (min_y, max_y), (min_z, max_z)]):
        map_range = tree.nodes.new("CompositorNodeMapRange")
        map_range.inputs['From Min'].default_value = 0.0
        map_range.inputs['From Max'].default_value = 1.0
        map_range.inputs['To Min'].default_value = min_val
        map_range.inputs['To Max'].default_value = max_val
        map_range.use_clamp = False
        map_range.location = (300, -120 * i)

        links.new(separate_node.outputs[i], map_range.inputs[0])
        links.new(map_range.outputs[0], combine_node.inputs[i])

    # Pass alpha through directly
    links.new(separate_node.outputs[3], combine_node.inputs[3])

    # 5.0-safe: use Group Output (or Composite in 4.x) via helper
    output_node, output_socket = get_compositor_output_socket(tree, ensure_interface=True)
    links.new(combine_node.outputs[0], output_socket)

    print("✅ Unnormalize-only compositing setup complete using Map Range nodes.")


# Called to render temporary frames to first prime the compositor, then through sequence for vat and optionally vnrm    
def render_vat_scene(vat_scene, num_frames, output_dir, image_format, raw_format):
    start_frame = vat_scene.frame_start
    end_frame = vat_scene.frame_start + num_frames
    output_name = vat_scene.name.replace("_ovbake", "")
    output_path = os.path.join(output_dir, f"{output_name}", f"{output_name}{image_format}")
    img_settings = vat_scene.render.image_settings
    
    if raw_format == 'PNG8':
        img_settings.color_mode = 'RGB'
        img_settings.color_depth = '8'
        img_settings.compression = 0

    elif raw_format == 'PNG16':
        img_settings.color_mode = 'RGB'
        img_settings.color_depth = '16'
        img_settings.compression = 0

    elif raw_format == 'EXR16':
        img_settings.color_mode = 'RGB'
        img_settings.color_depth = '16'
        img_settings.exr_codec = 'ZIP'

    elif raw_format == 'EXR32':
        img_settings.color_mode = 'RGBA'
        img_settings.color_depth = '32'
        img_settings.exr_codec = 'NONE'

    nrmoutput_path = os.path.join(output_dir, f"{output_name}", vat_scene.name.replace("_vat", "_vnrm") + image_format)
    if os.path.exists(output_path):
        bpy.data.images.remove(bpy.data.images.load(output_path))
    for frame in range(start_frame -1, end_frame):
        vat_scene.frame_set(frame)
        vat_scene.render.filepath = output_path
        bpy.ops.render.render(write_still=True)
        img = bpy.data.images.get(output_name + image_format)
        if img is not None:
            img.reload()
        else:
            img = bpy.data.images.load(output_path)
            print(f"Rendered template frame for compositing {frame}")
        if 'EXR' in raw_format:
            img.use_half_precision = False

    img = bpy.data.images.load(output_path)
    if img is not None:
        img.reload()
        if 'EXR' in raw_format:
            img.use_half_precision = False
        vat_scene.render.filepath = output_path
        bpy.ops.render.render(write_still=True)

    print(f"VAT Encoding finished, exported to {output_dir}")
    
# Similar to render_vat_scene above, but to a new texture name
def render_vat_nrml(vat_scene, num_frames, output_dir, image_format, raw_format):
    start_frame = vat_scene.frame_start
    end_frame = vat_scene.frame_start + num_frames
    output_name = vat_scene.name.replace("_ovbake", "")
    rendername = output_name.replace("_vat", "_vnrm")
    output_path = os.path.join(output_dir, f"{output_name}", f"{rendername}{image_format}")
    img_settings = vat_scene.render.image_settings

    if raw_format == 'PNG8':
        img_settings.color_mode = 'RGB'
        img_settings.color_depth = '8'
        img_settings.compression = 0

    elif raw_format == 'PNG16':
        img_settings.color_mode = 'RGB'
        img_settings.color_depth = '16'
        img_settings.compression = 0

    elif raw_format == 'EXR16':
        img_settings.color_mode = 'RGB'
        img_settings.color_depth = '16'
        img_settings.exr_codec = 'ZIP'

    elif raw_format == 'EXR32':
        img_settings.color_mode = 'RGB'
        img_settings.color_depth = '32'
        img_settings.exr_codec = 'NONE'


    if os.path.exists(output_path):
        bpy.data.images.remove(bpy.data.images.load(output_path)) 
    for frame in range(start_frame -1, end_frame):
        vat_scene.frame_set(frame)
        vat_scene.render.filepath = output_path
        bpy.ops.render.render(write_still=True)
        img = bpy.data.images.get(rendername + image_format)
        if img is not None:
            img.reload()
        else:
            img = bpy.data.images.load(output_path)
            print(f"Rendered template frame for normals compositing {frame}")
   

    img = bpy.data.images.load(output_path)
    if img is not None:
        img.reload()
        vat_scene.render.filepath = output_path
        bpy.ops.render.render(write_still=True)
        
        #Reset Color Mode
        bpy.context.scene.render.image_settings.color_mode = 'RGBA'
    print(f"VNRM Encoding finished, exported to {output_dir}")

# Export the selected object with default export settings for openVAT
def export_vat_model(file_format='FBX', include_materials=False, include_tangents=True):
    obj = bpy.context.active_object

    if obj is None or obj.type != 'MESH':
        raise Exception("Active object must be a mesh")

    bpy.ops.mesh.customdata_custom_splitnormals_add()
    # The exporters write selected objects only; an unselectable object would export an empty file
    if not obj.select_get():
        raise RuntimeError(f"{obj.name} is not selectable, so its model export would be empty")

    export_directory = bpy.path.abspath(bpy.context.scene.vat_settings.vat_output_directory)
    object_directory = os.path.join(export_directory, obj.name)
    os.makedirs(object_directory, exist_ok=True)

    export_name = obj.name.replace("_vat", "")
    export_ext = 'fbx' if file_format.upper() == 'FBX' else 'glb' if file_format.upper() == 'GLB' else 'gltf'
    export_path = os.path.join(object_directory, f"{export_name}.{export_ext}")

    unity_space = bpy.context.scene.vat_settings.engine_space == 'UNITY'
    preview_rotation = obj.rotation_euler.copy()
    if unity_space and file_format.upper() == 'FBX':
        obj.rotation_euler = UNITY_SPACE_FBX_ROOT_ROTATION

    if file_format.upper() == 'FBX':
        bpy.ops.export_scene.fbx(
            filepath=export_path,
            use_selection=True,
            use_active_collection=False,
            global_scale=1.0,
            apply_unit_scale=True,
            # Unity space bakes units into the data so the root imports at scale 1
            apply_scale_options='FBX_SCALE_ALL' if unity_space else 'FBX_SCALE_NONE',
            bake_space_transform=False,
            object_types={'MESH'},
            use_mesh_modifiers=False,
            use_mesh_modifiers_render=False,
            mesh_smooth_type='OFF',
            use_subsurf=False,
            use_mesh_edges=False,
            use_tspace=include_tangents,
            add_leaf_bones=False,
            bake_anim=False,
            path_mode='COPY' if include_materials else 'STRIP',
            embed_textures=include_materials
        )
        obj.rotation_euler = preview_rotation

    elif file_format.upper() in {'GLB', 'GLTF'}:
        bpy.ops.export_scene.gltf(
            filepath=export_path,
            export_format='GLB' if file_format.upper() == 'GLB' else 'GLTF_SEPARATE',
            use_selection=True,
            export_apply=True,
            export_texcoords=True,
            export_normals=True,
            export_tangents=include_tangents,
            export_materials='EXPORT' if include_materials else 'NONE',
            export_yup=True,
            export_cameras=False,
            export_lights=False,
            export_extras=False,
            will_save_settings=False,
            use_active_scene=True 
        )

    else:
        raise ValueError(f"Unsupported export format: {file_format}. Use 'FBX', 'GLB', or 'GLTF'.")

    print(f"Exported {obj.name} to {export_path} as {file_format.upper()}")


def add_normal_safe_split(nodes, links):
    # Split edges that are marked sharp or border a flat-shaded face
    edge_smooth = nodes.new("GeometryNodeInputEdgeSmooth")
    edge_smooth.location = (400, -200)

    edge_sharp = nodes.new("FunctionNodeBooleanMath")
    edge_sharp.operation = 'NOT'
    edge_sharp.location = (600, -200)
    links.new(edge_smooth.outputs[0], edge_sharp.inputs[0])

    face_smooth = nodes.new("GeometryNodeInputShadeSmooth")
    face_smooth.location = (0, -400)

    face_flat = nodes.new("FunctionNodeBooleanMath")
    face_flat.operation = 'NOT'
    face_flat.location = (200, -400)
    links.new(face_smooth.outputs[0], face_flat.inputs[0])

    # Average of adjacent faces' flatness > 0 means at least one is flat
    flat_on_edge = nodes.new("GeometryNodeFieldOnDomain")
    flat_on_edge.domain = 'EDGE'
    flat_on_edge.data_type = 'FLOAT'
    flat_on_edge.location = (400, -400)
    links.new(face_flat.outputs[0], flat_on_edge.inputs[0])

    borders_flat = nodes.new("FunctionNodeCompare")
    borders_flat.data_type = 'FLOAT'
    borders_flat.operation = 'GREATER_THAN'
    borders_flat.inputs[1].default_value = 0.0
    borders_flat.location = (600, -400)
    links.new(flat_on_edge.outputs[0], borders_flat.inputs[0])

    selection = nodes.new("FunctionNodeBooleanMath")
    selection.operation = 'OR'
    selection.location = (800, -300)
    links.new(edge_sharp.outputs[0], selection.inputs[0])
    links.new(borders_flat.outputs[0], selection.inputs[1])

    split_node = nodes.new("GeometryNodeSplitEdges")
    split_node.location = (1000, 0)
    links.new(selection.outputs[0], split_node.inputs["Selection"])
    return split_node


def create_geo_nodes_bake(use_collection=False, collection_name=""):
    context = bpy.context
    scene = context.scene
    vat_settings = scene.vat_settings
    
    active_obj = context.active_object
    if not use_collection:
        if not active_obj:
            raise RuntimeError("No active object selected")

    base_name = collection_name if use_collection else active_obj.name
    bake_name = f"{base_name}_ovbake"

    # Create empty mesh + object
    mesh = bpy.data.meshes.new(f"{bake_name}_mesh")
    obj = bpy.data.objects.new(bake_name, mesh)
    scene.collection.objects.link(obj)

    # Add Geometry Nodes modifier and fresh group
    node_group = bpy.data.node_groups.new(f"{bake_name}_group", 'GeometryNodeTree')
    mod = obj.modifiers.new(name="GeometryNodes", type='NODES')
    mod.node_group = node_group

    # Setup nodes
    nodes = node_group.nodes
    links = node_group.links
    nodes.clear()

    # Add output socket via interface
    # Create socket via 4.4 API
    node_group.interface.new_socket(
        name="Geometry",
        in_out='OUTPUT',
        socket_type='NodeSocketGeometry'
    )

    output_node = nodes.new("NodeGroupOutput")
    output_node.location = (1200, 0)
    output_node.is_active_output = True

    realize_node = nodes.new("GeometryNodeRealizeInstances")
    realize_node.location = (400, 0)

    if use_collection:
        if collection_name not in bpy.data.collections:
            raise ValueError(f"Collection '{collection_name}' not found")
        source_node = nodes.new("GeometryNodeCollectionInfo")
        collection = bpy.data.collections[collection_name]
        if collection.name not in scene.collection.children:
            scene.collection.children.link(collection)
        source_node.inputs["Collection"].default_value = collection
        source_node.inputs["Separate Children"].default_value = False
        source_node.inputs["Reset Children"].default_value = False
        source_node.transform_space = 'RELATIVE'
        links.new(source_node.outputs["Instances"], realize_node.inputs["Geometry"])
    else:
        source_node = nodes.new("GeometryNodeObjectInfo")
        source_node.inputs["Object"].default_value = active_obj
        source_node.transform_space = vat_settings.vat_transform

        source_node.inputs["As Instance"].default_value = False
        links.new(source_node.outputs["Geometry"], realize_node.inputs["Geometry"])

    source_node.location = (0, 0)

    # Link nodes
    geometry_out = realize_node.outputs["Geometry"]
    if vat_settings.vat_normal_encoding != 'NONE' and vat_settings.rip_edges:
        # Normal-Safe Edges: split inside the tree so the live output, the baked
        # mesh and the proxy all share the same split topology
        split_node = add_normal_safe_split(nodes, links)
        links.new(geometry_out, split_node.inputs["Mesh"])
        geometry_out = split_node.outputs["Mesh"]

    if vat_settings.engine_space == 'UNITY':
        # Convert to Unity space here so positions, normals, the remap range and
        # the exported mesh are all written in the same Y-up frame
        space_node = nodes.new("GeometryNodeTransform")
        space_node.location = (1100, 200)
        space_node.inputs["Rotation"].default_value = UNITY_SPACE_ROTATION
        links.new(geometry_out, space_node.inputs["Geometry"])
        geometry_out = space_node.outputs["Geometry"]

    links.new(geometry_out, output_node.inputs["Geometry"])

    # Deselect all, activate new object
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    context.view_layer.objects.active = obj

    # Duplicate and apply modifiers
    bpy.ops.object.duplicate()
    dup = context.active_object
    bpy.ops.object.convert(target='MESH')

    # Join back into original
    obj.select_set(True)
    dup.select_set(True)
    context.view_layer.objects.active = obj
    bpy.ops.object.join()
    
    if vat_settings.proxy_method == 'COLLAPSE':
        
        # ops to collapse mesh to center (tbd replace with more stable data calls)
        bpy.context.scene.cursor.location[0] = 0
        bpy.context.scene.cursor.location[1] = 0
        bpy.context.scene.cursor.location[2] = 0
        bpy.ops.object.editmode_toggle()
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.merge(type='CURSOR')
        bpy.ops.object.editmode_toggle()

    scene = bpy.context.scene  # Make sure 'scene' is defined

    # Ensure OpenVATPreview collection exists
    vat_coll = bpy.data.collections.get("OpenVATPreview")
    if not vat_coll:
        vat_coll = bpy.data.collections.new("OpenVATPreview")
        scene.collection.children.link(vat_coll)
    # If it exists but isn't linked into this scene, link it
    elif vat_coll.name not in {c.name for c in scene.collection.children}:
        scene.collection.children.link(vat_coll)

    # Move object into OpenVATPreview
    for c in obj.users_collection:
        c.objects.unlink(obj)
    vat_coll.objects.link(obj)

    # Final selection state
    obj.select_set(True)
    context.view_layer.objects.active = obj
    if active_obj:
        active_obj.select_set(False)
