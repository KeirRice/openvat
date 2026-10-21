# OpenVAT Utility Functions

import bpy
import json
import math
import numpy as np
import os

NODE_GROUPS_BLEND_FILE = os.path.join(os.path.dirname(__file__), "vat_node_groups.blend")

def append_node_group(group_name):
    with bpy.data.libraries.load(NODE_GROUPS_BLEND_FILE, link=False) as (data_from, data_to):
        if group_name in data_from.node_groups:
            data_to.node_groups.append(group_name)

def ensure_node_group(group_name):
    if group_name not in bpy.data.node_groups:
        append_node_group(group_name)

def set_modifier_input(modifier, socket_id, value, is_attribute_name=False):
    """
    Version-safe setter for a Geometry Nodes modifier's input socket value.

    - Blender <= 4.x: uses modifier[socket_id] (flat ID-property access), or
                      modifier[f"{socket_id}_attribute_name"] for the attribute-name variant.
    - Blender 5.x+:   uses modifier.properties.inputs.<socket_id>.value, or
                      .attribute_name for the attribute-name variant - the modifier input
                      storage was restructured into a nested, typed properties.inputs group
                      (see Blender commit 1561c1ea4ab6). Detected via hasattr rather than a
                      hardcoded bpy.app.version check, matching this addon's own existing
                      get_scene_compositor_tree() convention - robust to the exact version this
                      landed in without needing to pin it down.
    """
    if hasattr(modifier, "properties") and hasattr(modifier.properties, "inputs"):
        socket = getattr(modifier.properties.inputs, socket_id)
        if is_attribute_name:
            socket.attribute_name = value
        else:
            socket.value = value
    else:
        key = f"{socket_id}_attribute_name" if is_attribute_name else socket_id
        modifier[key] = value

def make_custom_data(obj_name, attr_names, frame_start, frame_end, output_filepath, remap_output_filepath, framerate=30, looping=True):
    obj = bpy.data.objects.get(obj_name)
    if obj is None:
        print(f"Object '{obj_name}' not found")
        return

    frames = frame_end - frame_start + 1
    channel_remap_data = {}

    for attr in attr_names:
        if not attr or attr.upper() == "NONE":
            channel_remap_data["None"] = {
                "Min": 0.0,
                "Max": 0.0,
                "Frames": frames
            }
            continue

        frame_data = {}
        for frame in range(frame_start, frame_end + 1):
            bpy.context.scene.frame_set(frame)
            values = get_geometry_nodes_data(obj, attr)
            frame_data[frame] = values

        attr_min, attr_max = find_scalar_max_min(frame_data)
        channel_remap_data[attr] = {
            "Min": attr_min,
            "Max": attr_max,
            "Frames": frames
        }

    # Write or return the remap info
    with open(remap_output_filepath, 'w') as f:
        json.dump(channel_remap_data, f, indent=4)

    # --- NEW: Add animation data section to main output ---
    try:
        # Load existing data (if any)
        if os.path.exists(output_filepath):
            with open(output_filepath, 'r') as f:
                output_data = json.load(f)
        else:
            output_data = {}

        # Add/overwrite animations section
        anim_entry = {
            "startFrame": frame_start,
            "endFrame": frame_end,
            "framerate": framerate,
            "looping": looping
        }
        # If already has an animations list, append; else, create new
        if "animations" not in output_data:
            output_data["animations"] = []
        output_data["animations"].append(anim_entry)

        # Write back to file
        with open(output_filepath, 'w') as f:
            json.dump(output_data, f, indent=4)

    except Exception as e:
        print(f"Error updating animation data: {e}")

def make_remap_data(obj_name, attribute_name, frame_start, frame_end, output_filepath, remap_output_filepath,animations, scalar_value=""):
    import os
    import json

    obj = bpy.data.objects.get(obj_name)
    if obj is None:
        print(f"Object '{obj_name}' not found")
        return
    
    # Remap data for vector properties
    all_frames_data = {}
    scalar_data = {}
    frames = frame_end - frame_start + 1

    for frame in range(frame_start, frame_end + 1):
        bpy.context.scene.frame_set(frame)
        frame_data = get_geometry_nodes_data(obj, attribute_name)
        all_frames_data[frame] = frame_data
    overall_max, overall_min = find_max_min_values(all_frames_data)
    
    if attribute_name == "colPos":
        attribute_name = "os-remap"
        
    # Scalar value for alpha data
    if scalar_value:
        for frame in range(frame_start, frame_end + 1):
            bpy.context.scene.frame_set(frame)
            frame_data = get_geometry_nodes_data(obj, scalar_value)
            scalar_data[frame] = frame_data
        
        scalar_max, scalar_min = find_scalar_max_min(scalar_data)
        
        remap_info = {
            attribute_name: {
                "Min": overall_min,
                "Max": overall_max,
                "Frames": frames
            },
            scalar_value: {
                "Min": scalar_min,
                "Max": scalar_max,
            }
        }
    else:
        remap_info = {
            attribute_name: {
                "Min": overall_min,
                "Max": overall_max,
                "Frames": frames,
            }
    }
    
    # Coordinate frame of the data ("blender" or "unity"), so importers know what to expect
    remap_info["space"] = bpy.context.scene.vat_settings.engine_space.lower()

    # Write the remap info
    write_json(remap_info, remap_output_filepath)
    print(f"Remap information saved to {remap_output_filepath}")

    # --- NEW: Add animation data section to main output ---
    try:
        # Load existing data (if any)
        if os.path.exists(output_filepath):
            with open(output_filepath, 'r') as f:
                output_data = json.load(f)
        else:
            output_data = {}

        # Add/overwrite animations section
        anim_dict = {}
        for anim in animations:
            anim_dict[anim.name] = {
                "startFrame": anim.start_frame,
                "endFrame": anim.end_frame,
                "framerate": anim.framerate,
                "looping": anim.looping
            }

        output_data["animations"] = anim_dict

        # Write back to file
        with open(output_filepath, 'w') as f:
            json.dump(output_data, f, indent=4)

    except Exception as e:
        print(f"Error updating animation data: {e}")


# Get data from dependency graph
def get_geometry_nodes_data(obj, attribute_name):
    data = []
    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval = obj.evaluated_get(depsgraph)
    mesh = obj_eval.data

    if attribute_name in mesh.attributes:
        attr = mesh.attributes[attribute_name]
        attr_data = attr.data

        if attr.domain not in {'POINT'}:
            print(f"Warning: Unsupported domain '{attr.domain}' for attribute '{attribute_name}'")

        for item in attr_data:
            if hasattr(item, 'vector'):
                data.append([round(val, 8) for val in item.vector])
            elif hasattr(item, 'value'):
                data.append(round(item.value, 8))
            else:
                print(f"Warning: Unknown attribute type for '{attribute_name}'")
    else:
        print(f"Attribute '{attribute_name}' not found in '{obj.name}'")

    return data


def write_json(data, filepath):
    class CustomEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, float):
                return format(obj, ".8f")
            return json.JSONEncoder.default(self, obj)

    with open(filepath, 'w') as f:
        json.dump(data, f, indent=4, cls=CustomEncoder)

def apply_modifier(obj, modifier):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval = obj.evaluated_get(depsgraph)
    mesh_from_eval = bpy.data.meshes.new_from_object(obj_eval)

    obj.modifiers.remove(modifier)
    obj.data = mesh_from_eval

def find_scalar_max_min(all_frames_data):
    max_value = float('-inf')
    min_value = float('inf')
    
    for frame, data in all_frames_data.items():
        for value in data:
            max_value = max(max_value, value)
            min_value = min(min_value, value)
    if max_value == float('-inf'):
        max_value = None
    if min_value == float('inf'):
        min_value = None

    if max_value is not None:
        max_value = round_to_nearest_ten(max_value, math.ceil)
    if min_value is not None:
        min_value = round_to_nearest_ten(min_value, math.floor)

    return min_value, max_value
            
# Function to find x, y, z min/max from JSON
def find_max_min_values(all_frames_data):
    max_values = [float('-inf'), float('-inf'), float('-inf')]
    min_values = [float('inf'), float('inf'), float('inf')]

    for frame, data in all_frames_data.items():
        for vector in data:
            for i in range(3):
                max_values[i] = max(max_values[i], vector[i])
                min_values[i] = min(min_values[i], vector[i])

    max_values = [val if val != float('-inf') else None for val in max_values]
    min_values = [val if val != float('inf') else None for val in min_values]

    overall_max = [round_to_nearest_ten(val, math.ceil) if val is not None else None for val in max_values]
    overall_min = [round_to_nearest_ten(val, math.floor) if val is not None else None for val in min_values]

    return overall_max, overall_min

def round_to_nearest_ten(val, func):
    return func(val * 10) / 10

def read_remap_info(filepath, attribute):
    with open(filepath, 'r') as f:
        data = json.load(f)
    
    if attribute == "colPos":
        attribute = "os-remap"
    min_values = data[attribute]["Min"]
    max_values = data[attribute]["Max"]

    min_x, min_y, min_z = min_values
    max_x, max_y, max_z = max_values

    return min_x, min_y, min_z, max_x, max_y, max_z

def read_custom_info(filepath, attr_names):
    """
    Returns a flat list of min/max per attribute: [min_r, min_g, min_b, max_r, max_g, max_b]
    """
    with open(filepath, 'r') as f:
        data = json.load(f)

    min_vals = []
    max_vals = []

    for attr in attr_names:
        if not attr or attr.upper() == "NONE":
            min_vals.append(0.0)
            max_vals.append(0.0)
            continue
        
        if attr not in data:
            raise ValueError(f"Attribute '{attr}' not found in remap file.")

        min_vals.append(data[attr]["Min"])
        max_vals.append(data[attr]["Max"])


    return (*min_vals, *max_vals)

def clean_mesh_data(obj):

    if obj and obj.type == 'MESH':
        mesh = bpy.data.meshes.get(obj.data.name)
        color_attrs = [attr for attr in mesh.attributes if attr.data_type in {'FLOAT_COLOR', 'BYTE_COLOR'}]
        for attr in color_attrs:
            mesh.attributes.remove(attr)
        if mesh.shape_keys:
            key = mesh.shape_keys
            for block in list(key.key_blocks):
                key.key_blocks.remove(block)
            obj.shape_key_clear()
        while obj.vertex_groups:
            obj.vertex_groups.active_index = 0  # Set a valid active index
            obj.vertex_groups.remove(obj.vertex_groups[0])

# Calculation for estimating max amount of verts if all edges are ripped
def get_virtual_ripped_vertex_count(obj):
    if not obj or obj.type != 'MESH':
        return 0
    
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph)
    eval_mesh = eval_obj.to_mesh()
    count = len(eval_mesh.loops)
    eval_obj.to_mesh_clear()
    
    return count

# Best working approximations for output size based on realized data available

# VAT_UV addresses one texel per vertex. Engines may store UVs as 16-bit floats
# (Unreal without Full Precision UVs, Unity with Vertex Compression); above this
# size a half float can no longer hold texel centres safely and vertices read
# their neighbour's data.
HALF_PRECISION_UV_MAX_SIZE = 1024

def needs_full_precision_uvs(width, height):
    return max(width, height) > HALF_PRECISION_UV_MAX_SIZE

def calculate_optimal_vat_resolution(num_vertices, num_frames):
    def next_power_of_2(x):
        return 1 if x <= 1 else 2**math.ceil(math.log2(x))

    def powers_of_two(min_val=64, max_val=8192):
        val = min_val
        while val <= max_val:
            yield val
            val *= 2

    best = None
    best_area = float('inf')
    # Few vertices (common with shared columns) may have no near-square size
    fallback = None
    fallback_area = float('inf')

    for width in powers_of_two():
        num_wraps = math.ceil(num_vertices / width)
        height_unrounded = num_frames * num_wraps
        height = next_power_of_2(height_unrounded)

        log2_width = math.log2(width)
        log2_height = math.log2(height)

        area = width * height
        if area < fallback_area:
            fallback = (width, height, num_wraps)
            fallback_area = area

        if abs(log2_width - log2_height) > 1:
            continue  # Skip if too far from square

        if area < best_area:
            best = (width, height, num_wraps)
            best_area = area

    return best or fallback


def calculate_packed_vat_resolution(num_vertices, num_frames):
    def next_pow2(n):
        return 2 ** math.ceil(math.log2(n))

    def powers_of_two(min_val=64, max_val=8192):
        v = min_val
        while v <= max_val:
            yield v
            v *= 2

    best = None
    best_area = float('inf')
    # Few vertices (common with shared columns) may have no near-square size
    fallback = None
    fallback_area = float('inf')

    for width in powers_of_two():
        num_wraps = math.ceil(num_vertices / width)

        pos_height = num_wraps * num_frames
        norm_height = num_wraps * num_frames
        total_height = next_pow2(pos_height + norm_height)

        log_w = math.log2(width)
        log_h = math.log2(total_height)

        area = width * total_height
        if area < fallback_area:
            fallback = (width, total_height, num_wraps)
            fallback_area = area

        if abs(log_w - log_h) > 1:
            continue

        if area < best_area:
            best = (width, total_height, num_wraps)
            best_area = area

    return best or fallback

 
 
def get_point_attributes_filtered(self, context, data_type_filter=None):
    items = []
    obj = context.active_object
    if obj and obj.type == 'MESH':
        depsgraph = context.evaluated_depsgraph_get()
        obj_eval = obj.evaluated_get(depsgraph)
        mesh = obj_eval.data
        
        for attr in mesh.attributes:
            if attr.domain != 'POINT':
                continue
            if attr.name.startswith('.') or attr.name.startswith('_') or len(attr.name) > 32:
                continue
            if data_type_filter and attr.data_type != data_type_filter:
                continue
            items.append((attr.name, attr.name, ""))
                
    if not items:
        items.append(('None', 'None', 'No attributes found'))
        
    return items

def get_evaluated_point_float_attributes(context):
    depsgraph = context.evaluated_depsgraph_get()
    obj = context.active_object
    if not obj or obj.type != 'MESH':
        return []

    eval_obj = obj.evaluated_get(depsgraph)
    eval_mesh = eval_obj.to_mesh()
    try:
        return [
            (attr.name, attr.name, "")
            for attr in eval_mesh.attributes
            if attr.domain == 'POINT' and attr.data_type == 'FLOAT'
        ]
    finally:
        eval_obj.to_mesh_clear()

_openvat_enum_cache = {
    "1": [("NONE", "None", "")],
    "2": [("NONE", "None", "")],
    "3": [("NONE", "None", "")]
}


# Codes per channel for the integer image formats
QUANTIZE_LEVELS = {'PNG8': 255, 'PNG16': 65535}

def quantize_encoded(values, image_format):
    """Quantize 0-1 encoded values to what the image stores, so equal results mean equal texels"""
    values = np.clip(values, 0.0, 1.0)
    if image_format in QUANTIZE_LEVELS:
        return np.rint(values * QUANTIZE_LEVELS[image_format]).astype(np.int64)
    # Adding zero folds -0.0 into 0.0 so both compare equal
    if image_format == 'EXR16':
        return (values.astype(np.float16) + np.float16(0)).view(np.uint16).astype(np.int64)
    return (values.astype(np.float32) + np.float32(0)).view(np.uint32).astype(np.int64)

def remap_to_unit(values, range_min, range_max):
    """Match the encoder's Map Range: (v - min) / (max - min), zero where the range is empty"""
    range_min = np.asarray(range_min, dtype=np.float64)
    span = np.asarray(range_max, dtype=np.float64) - range_min
    safe_span = np.where(span == 0.0, 1.0, span)
    return np.where(span == 0.0, 0.0, (values - range_min) / safe_span)

def relative_normal_range(image_format):
    """Remap range for relative normals (offsets within -2..2) that stores a zero offset exactly.

    Static vertices encode zero, so it must land on a code rather than between two; the range
    is widened slightly on the positive side to put zero at 127/255. PNG8 stores that exactly.
    PNG16 ends within a half-float step of it (the VAT renders through a half-float buffer),
    which still rounds to 127/255 when an engine imports it at 8 bits per channel (Unity does).
    """
    if image_format in QUANTIZE_LEVELS:
        return -2.0, -2.0 + 2.0 * 255 / 127
    return -2.0, 2.0

def read_point_vectors(collection, count):
    values = np.empty(count * 3, dtype=np.float32)
    collection.foreach_get("vector", values)
    return values.reshape(count, 3).astype(np.float64)

def read_point_floats(mesh, attribute_name):
    attr = mesh.attributes.get(attribute_name)
    values = np.zeros(len(mesh.vertices), dtype=np.float32)
    if attr is not None and attr.domain == 'POINT':
        attr.data.foreach_get("value", values)
    return values.astype(np.float64)

def mirrors_x(settings):
    """Unity space writes position offsets and normals with X negated, in Unity's left-handed
    mesh frame, so the engine samples them without flipping"""
    return settings.engine_space == 'UNITY' and settings.encode_type == 'DEFAULT'

def mirror_remap_x(filepath, key):
    """Mirror a remap entry's X range to match X-mirrored data"""
    with open(filepath, 'r') as f:
        data = json.load(f)
    entry = data[key]
    entry["Min"][0], entry["Max"][0] = -entry["Max"][0], -entry["Min"][0]
    write_json(data, filepath)

def read_evaluated_frame(obj, settings, relative_basis):
    """One frame of the data the encoder writes for each vertex, remapped to 0-1 per channel"""
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    mesh = obj.evaluated_get(depsgraph).data
    count = len(mesh.vertices)

    if settings.encode_type == 'CUSTOM':
        custom = np.column_stack([
            read_point_floats(mesh, name) for name in
            (settings.custom_attr_1, settings.custom_attr_2, settings.custom_attr_3)
        ])
        return remap_to_unit(custom, (scene['min_x'], scene['min_y'], scene['min_z']),
                             (scene['max_x'], scene['max_y'], scene['max_z']))

    mirror = np.array([-1.0 if mirrors_x(settings) else 1.0, 1.0, 1.0])
    offsets = read_point_vectors(mesh.attributes["colPos"].data, count) * mirror
    channels = [remap_to_unit(offsets, (scene['min_x'], scene['min_y'], scene['min_z']),
                              (scene['max_x'], scene['max_y'], scene['max_z']))]
    if settings.vat_normal_encoding != 'NONE':
        normals = read_point_vectors(mesh.vertex_normals, count)
        if relative_basis is not None:
            low, high = relative_normal_range(settings.image_format)
            channels.append(remap_to_unit((normals - relative_basis) * mirror, (low,) * 3, (high,) * 3))
        else:
            channels.append(remap_to_unit(normals * mirror, (-1.0,) * 3, (1.0,) * 3))
    return np.column_stack(channels)

def compute_shared_columns(obj, proxy_obj, frame_start, frame_end, settings):
    """Group vertices whose encoded texels match in every frame.

    Returns one group id per vertex and the group count. Each frame refines the grouping, so
    only one frame of data is held at a time, and the pass stops once every vertex is unique.
    """
    relative_basis = None
    if settings.relative_normals and settings.encode_type == 'DEFAULT' and settings.vat_normal_encoding != 'NONE':
        depsgraph = bpy.context.evaluated_depsgraph_get()
        proxy_mesh = proxy_obj.evaluated_get(depsgraph).data
        relative_basis = read_point_vectors(proxy_mesh.vertex_normals, len(proxy_mesh.vertices))

    groups = None
    num_groups = 1
    for frame in range(frame_start, frame_end + 1):
        bpy.context.scene.frame_set(frame)
        texels = quantize_encoded(read_evaluated_frame(obj, settings, relative_basis), settings.image_format)
        if groups is None:
            count = len(texels)
            groups = np.zeros(count, dtype=np.int64)
        elif len(texels) != count:
            raise ValueError(f"Vertex count changes at frame {frame}; VAT needs constant topology")
        _, groups = np.unique(np.column_stack((groups, texels)), axis=0, return_inverse=True)
        groups = groups.reshape(-1)
        num_groups = int(groups.max()) + 1
        if num_groups == count:
            break
    return groups, num_groups

def layout_shared_columns(groups, num_groups):
    """Assign each group a column and build the vertex sort that feeds the encoder.

    The encoder writes vertex index (count - 1 - slot) into slot, so sorting by the returned
    keys puts one representative per group at the top indices, in column order, and moves the
    vertices that reuse a column below them, where they land in slots no vertex reads.
    """
    count = len(groups)
    representative = np.full(num_groups, -1, dtype=np.int64)
    np.maximum.at(representative, groups, np.arange(count))
    # Highest representative first keeps the layout close to the unshared one
    column_of_group = np.empty(num_groups, dtype=np.int64)
    column_of_group[np.argsort(-representative, kind='stable')] = np.arange(num_groups)

    sort_keys = np.arange(count, dtype=np.float64) - count
    sort_keys[representative] = num_groups - 1 - column_of_group
    return column_of_group[groups], sort_keys

def create_column_table(name, columns, sort_keys):
    """Point cloud mesh holding each vertex's column and sort key, read by ov_sort-by-column"""
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(columns))
    mesh.attributes.new("ov_column", 'INT', 'POINT').data.foreach_set("value", columns.astype(np.int32))
    mesh.attributes.new("ov_sort", 'FLOAT', 'POINT').data.foreach_set("value", sort_keys.astype(np.float32))
    return bpy.data.objects.new(name, mesh)

def update_remap_info(filepath, entries):
    """Merge extra top-level entries into an existing remap_info JSON"""
    with open(filepath, 'r') as f:
        data = json.load(f)
    data.update(entries)
    write_json(data, filepath)
