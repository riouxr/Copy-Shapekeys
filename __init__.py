import bpy
import re


# ------------------------------------------------------------------------
# Blender 5.0 Compatibility Helper
# ------------------------------------------------------------------------
def get_action_fcurves(action):
    """
    Get F-Curves from an action in a version-compatible way.
    Blender 5.0+ uses a layered animation system with channelbags, while 4.x uses fcurves directly.
    """
    if action is None:
        return []
    
    # Blender 5.0+ has a layered animation system
    # Check for layers first, as 5.0 may have both 'layers' and 'fcurves' attributes
    if hasattr(action, 'layers') and len(action.layers) > 0:
        fcurves_list = []
        for layer in action.layers:
            for strip in layer.strips:
                for channelbag in strip.channelbags:
                    fcurves_list.extend(channelbag.fcurves)
        return fcurves_list
    
    # Blender 4.x - direct fcurves access
    if hasattr(action, 'fcurves'):
        return action.fcurves
    
    return []


def find_fcurve(action, data_path):
    """
    Find an F-Curve by data_path in a version-compatible way.
    """
    if action is None:
        return None
    
    # Use get_action_fcurves which already handles version detection
    fcurves = get_action_fcurves(action)
    for fcurve in fcurves:
        if fcurve.data_path == data_path:
            return fcurve
    
    return None


def remove_fcurve(action, fcurve):
    """
    Remove an F-Curve from an action in a version-compatible way.
    """
    if action is None or fcurve is None:
        return
    
    # Blender 4.x
    if hasattr(action, 'fcurves') and hasattr(action.fcurves, 'remove'):
        action.fcurves.remove(fcurve)
        return
    
    # Blender 5.0+ - need to find which strip contains this fcurve
    if hasattr(action, 'layers'):
        for layer in action.layers:
            if hasattr(layer, 'strips'):
                for strip in layer.strips:
                    if hasattr(strip, 'fcurves') and fcurve in strip.fcurves:
                        strip.fcurves.remove(fcurve)
                        return


# ------------------------------------------------------------------------
# Curve helpers
# ------------------------------------------------------------------------
def curves_have_matching_topology(src_curve, tgt_curve):
    """
    Curve shapekey data order depends on the spline layout.
    We require exact matching:
      - same number of splines
      - same spline types (BEZIER / NURBS / POLY)
      - same number of points per spline (bezier_points or points)
    """
    src_splines = src_curve.splines
    tgt_splines = tgt_curve.splines

    if len(src_splines) != len(tgt_splines):
        return False

    for s_src, s_tgt in zip(src_splines, tgt_splines):
        if s_src.type != s_tgt.type:
            return False

        if s_src.type == 'BEZIER':
            if len(s_src.bezier_points) != len(s_tgt.bezier_points):
                return False
        else:
            if len(s_src.points) != len(s_tgt.points):
                return False

    return True


# ------------------------------------------------------------------------
# UI
# ------------------------------------------------------------------------
class ShapekeyTransferPanel(bpy.types.Panel):
    bl_label = "BB Copy Shapekeys"
    bl_idname = "PT_ShapekeyTransfer"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Tool'

    def draw(self, context):
        layout = self.layout

        # ---------------------------
        # SHAPEKEY BOX
        # ---------------------------
        sk_box = layout.box()
        sk_box.label(text="Shapekey Tools")

        col = sk_box.column(align=True)
        col.prop(context.scene, "name_only", text="Name Only")
        col.prop(context.scene, "active_only", text="Active Only")
        col.operator("object.shapekey_transfer", text="Copy Shape keys", icon="COPYDOWN")
        col.operator("object.shapekey_animation_transfer", text="Copy Animation", icon="ANIM")
        col.operator("object.vertexgroup_transfer", text="Copy Vertex Groups", icon="GROUP_VERTEX")

        col.separator()
        col.operator("object.shapekey_zero", text="Set Keys to 0", icon="X")

        # ---------------------------
        # ARMATURE ANIM BOX
        # ---------------------------
        arm_box = layout.box()
        arm_box.label(text="Copy Armature Anim")
        arm_box.label(text="(Armatures must be identical)", icon="INFO")
        arm_box.operator("object.copy_armature_anim", text="Copy", icon="ANIM")

# ------------------------------------------------------------------------
# Vertex groups (Mesh only)
# ------------------------------------------------------------------------
class VertexGroupTransferOperator(bpy.types.Operator):
    """Copy vertex groups from selected sources to the active target (requires identical topology)."""
    bl_idname = "object.vertexgroup_transfer"
    bl_label = "Copy Vertex Groups"
    bl_description = "Copy vertex groups from selected objects to active target (same topology)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT':
            self.report({'ERROR'}, "Operator must be run in Object mode.")
            return {'CANCELLED'}

        selected = context.selected_objects
        if len(selected) < 2:
            self.report({'ERROR'}, "Select at least one source object and one target object.")
            return {'CANCELLED'}

        target = context.view_layer.objects.active
        sources = [o for o in selected if o != target]

        if target.type != 'MESH':
            self.report({'ERROR'}, "Target must be a mesh object.")
            return {'CANCELLED'}

        copied = 0
        skipped = 0

        for src in sources:
            if src.type != 'MESH':
                skipped += 1
                continue

            if len(src.data.vertices) != len(target.data.vertices):
                self.report({'WARNING'},
                    f"'{src.name}' skipped: vertex count mismatch ({len(src.data.vertices)} vs {len(target.data.vertices)})")
                skipped += 1
                continue

            for vg_src in src.vertex_groups:
                if vg_src.name not in target.vertex_groups:
                    target.vertex_groups.new(name=vg_src.name)
                vg_tgt = target.vertex_groups[vg_src.name]

                # Clear weights
                for v in target.data.vertices:
                    try:
                        vg_tgt.remove([v.index])
                    except RuntimeError:
                        pass

                # Transfer weights
                for v in src.data.vertices:
                    try:
                        w = vg_src.weight(v.index)
                        vg_tgt.add([v.index], w, 'REPLACE')
                    except RuntimeError:
                        pass

                copied += 1

        self.report({'INFO'}, f"Copied {copied} vertex groups, skipped {skipped}.")
        return {'FINISHED'}


# ------------------------------------------------------------------------
# Shapekey transfer (Mesh + Curve)
# ------------------------------------------------------------------------
class ShapekeyTransferOperator(bpy.types.Operator):
    """Transfer non-duplicate shapekeys from selected source objects to the active target object."""
    bl_idname = "object.shapekey_transfer"
    bl_label = "Transfer Shapekeys"
    bl_description = "Transfer shapekeys from selected source objects to active target object"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT':
            self.report({'ERROR'}, "Operator must be run in Object mode.")
            return {'CANCELLED'}

        selected_objects = context.selected_objects
        if len(selected_objects) < 2:
            self.report({'ERROR'}, "Select at least one source object and one target object (active).")
            return {'CANCELLED'}

        target_object = context.view_layer.objects.active
        if target_object is None:
            self.report({'ERROR'}, "No active (target) object selected.")
            return {'CANCELLED'}

        if target_object.type not in {'MESH', 'CURVE'}:
            self.report({'ERROR'}, "Target object must be a Mesh or Curve.")
            return {'CANCELLED'}

        sources = [obj for obj in selected_objects if obj != target_object]
        if not sources:
            self.report({'ERROR'}, "No source objects selected.")
            return {'CANCELLED'}

        name_only = context.scene.name_only
        active_only = context.scene.active_only

        is_curve = (target_object.type == 'CURVE')
        target_data = target_object.data

        # Validate curve topology if target is a curve
        if is_curve:
            for src in sources:
                if src.type != 'CURVE':
                    self.report({'ERROR'}, f"'{src.name}' is not a curve but target is a curve.")
                    return {'CANCELLED'}
                if not curves_have_matching_topology(src.data, target_data):
                    self.report({'ERROR'},
                        f"'{src.name}' does not have the same spline structure as target '{target_object.name}'.")
                    return {'CANCELLED'}
        else:
            # Mesh: vertex count check
            target_vert_count = len(target_data.vertices)
            for src in sources:
                if src.type != 'MESH':
                    self.report({'ERROR'}, f"'{src.name}' is not a mesh but target is a mesh.")
                    return {'CANCELLED'}
                if len(src.data.vertices) != target_vert_count:
                    self.report({'ERROR'},
                        f"'{src.name}' has a different vertex count ({len(src.data.vertices)} vs {target_vert_count}).")
                    return {'CANCELLED'}

        # Create Basis if missing
        if target_data.shape_keys is None:
            target_object.shape_key_add(name="Basis", from_mix=False)

        target_keys = target_data.shape_keys
        copied_count = 0

        for src_object in sources:
            src_data = src_object.data
            if src_data.shape_keys is None:
                self.report({'WARNING'}, f"'{src_object.name}' has no shapekeys.")
                continue

            src_keys = src_data.shape_keys

            for src_block in src_keys.key_blocks:
                if src_block.name == "Basis":
                    continue
                if active_only and src_block.mute:
                    continue

                # Skip if already exists
                if src_block.name in target_keys.key_blocks:
                    continue

                # Create shapekey on target
                new_block = target_object.shape_key_add(name=src_block.name, from_mix=False)

                # Copy properties
                new_block.slider_min = src_block.slider_min
                new_block.slider_max = src_block.slider_max
                new_block.value = src_block.value
                new_block.mute = src_block.mute

                # Copy deformation data if not name-only
                if not name_only:
                    if is_curve:
                        # Curve
                        for s_src, s_tgt in zip(src_data.splines, target_data.splines):
                            if s_src.type == 'BEZIER':
                                for pt_src, pt_tgt in zip(s_src.bezier_points, s_tgt.bezier_points):
                                    pt_idx = pt_src.index if hasattr(pt_src, 'index') else pt_tgt.index
                                    new_block.data[pt_idx].co = src_block.data[pt_idx].co
                                    new_block.data[pt_idx].handle_left = src_block.data[pt_idx].handle_left
                                    new_block.data[pt_idx].handle_right = src_block.data[pt_idx].handle_right
                            else:
                                for pt_src, pt_tgt in zip(s_src.points, s_tgt.points):
                                    pt_idx = pt_src.index if hasattr(pt_src, 'index') else pt_tgt.index
                                    new_block.data[pt_idx].co = src_block.data[pt_idx].co
                    else:
                        # Mesh
                        for i, vert in enumerate(src_block.data):
                            new_block.data[i].co = vert.co

                copied_count += 1

        self.report({'INFO'}, f"Copied {copied_count} shapekey(s) to '{target_object.name}'.")
        return {'FINISHED'}


# ------------------------------------------------------------------------
# Shapekey Animation Transfer
# ------------------------------------------------------------------------
class ShapekeyAnimationTransferOperator(bpy.types.Operator):
    """Copy shapekey animation from selected sources to the active target."""
    bl_idname = "object.shapekey_animation_transfer"
    bl_label = "Copy Shapekey Animation"
    bl_description = "Copy shapekey animation from selected sources to active target"
    bl_options = {'REGISTER', 'UNDO'}

    def copy_fcurve_keyframes(self, src_fcurve, tgt_fcurve):
        """Copy all keyframes from source fcurve to target fcurve."""
        # Clear existing keyframes
        while len(tgt_fcurve.keyframe_points) > 0:
            tgt_fcurve.keyframe_points.remove(tgt_fcurve.keyframe_points[0])
        
        # Copy each keyframe
        for kf in src_fcurve.keyframe_points:
            new_kf = tgt_fcurve.keyframe_points.insert(kf.co.x, kf.co.y)
            new_kf.interpolation = kf.interpolation
            new_kf.handle_left_type = kf.handle_left_type
            new_kf.handle_right_type = kf.handle_right_type
            new_kf.handle_left = kf.handle_left
            new_kf.handle_right = kf.handle_right
        
        # Copy fcurve properties
        tgt_fcurve.extrapolation = src_fcurve.extrapolation
        tgt_fcurve.color_mode = src_fcurve.color_mode
        tgt_fcurve.color = src_fcurve.color

    def get_or_create_fcurve(self, action, data_path, array_index=0, target_data=None):
        """Get existing fcurve or create a new one - compatible with Blender 4.x and 5.0+"""
        # Try to find existing fcurve
        fcurves = get_action_fcurves(action)
        for fc in fcurves:
            if fc.data_path == data_path and fc.array_index == array_index:
                return fc
        
        # Need to create new fcurve
        # Blender 5.0+ - check for layers first (5.0 has both layers and fcurves attributes)
        if hasattr(action, 'layers'):
            # Get or create a layer
            if len(action.layers) == 0:
                layer = action.layers.new(name="Layer")
            else:
                layer = action.layers[0]
            
            # Get or create a keyframe strip
            if len(layer.strips) == 0:
                strip = layer.strips.new(type='KEYFRAME')
            else:
                strip = layer.strips[0]
            
            # Get or create channelbag (requires a slot in Blender 5.0)
            if len(strip.channelbags) > 0:
                channelbag = strip.channelbags[0]
            else:
                # Need to create a slot for the channelbag
                if len(action.slots) == 0:
                    # For shape keys, use 'KEY' id_type
                    # Use target_data name if available, otherwise use a default name
                    slot_name = target_data.name if (target_data and hasattr(target_data, 'name')) else "ShapeKeys"
                    slot = action.slots.new(name=slot_name, id_type='KEY')
                else:
                    slot = action.slots[0]
                
                channelbag = strip.channelbags.new(slot)
            
            # Create the fcurve in the channelbag
            return channelbag.fcurves.new(data_path, index=array_index)
        
        # Blender 4.x - direct fcurves access
        if hasattr(action, 'fcurves'):
            return action.fcurves.new(data_path, index=array_index)
        
        return None

    def execute(self, context):
        if context.mode != 'OBJECT':
            self.report({'ERROR'}, "Operator must be run in Object mode.")
            return {'CANCELLED'}

        selected_objects = context.selected_objects
        if len(selected_objects) < 2:
            self.report({'ERROR'}, "Select at least one source object and one target object (active).")
            return {'CANCELLED'}

        target = context.view_layer.objects.active
        if target is None:
            self.report({'ERROR'}, "No active (target) object selected.")
            return {'CANCELLED'}

        if target.type not in {'MESH', 'CURVE'}:
            self.report({'ERROR'}, "Target must be a Mesh or Curve.")
            return {'CANCELLED'}

        if target.data.shape_keys is None:
            self.report({'ERROR'}, "Target has no shapekeys. Copy shapekeys first.")
            return {'CANCELLED'}

        sources = [obj for obj in selected_objects if obj != target and obj.type in {'MESH', 'CURVE'}]
        if not sources:
            self.report({'ERROR'}, "No valid source objects selected.")
            return {'CANCELLED'}

        # Ensure Basis exists
        if target.data.shape_keys is None:
            target.shape_key_add(name="Basis", from_mix=False)
        key_tgt = target.data.shape_keys

        if key_tgt.animation_data is None:
            key_tgt.animation_data_create()
        if key_tgt.animation_data.action is None:
            key_tgt.animation_data.action = bpy.data.actions.new(name=f"{target.name}_ShapekeyAction")
        action_tgt = key_tgt.animation_data.action
        
        # For Blender 5.0, ensure the action is properly assigned to the shape_keys
        # by setting the action_slot_handle if it exists
        if hasattr(action_tgt, 'slots') and len(action_tgt.slots) > 0:
            if hasattr(key_tgt.animation_data, 'action_slot_handle'):
                # Get the first slot's handle (will be created by get_or_create_fcurve if needed)
                # We'll set this after creating the first fcurve
                pass
            if hasattr(action_tgt, 'assign_id'):
                try:
                    action_tgt.assign_id(None, key_tgt)
                except:
                    pass

        copied_curves = 0
        skipped_curves = 0
        active_only = context.scene.active_only

        for src in sources:
            key_src = src.data.shape_keys
            if key_src is None:
                continue
            if key_src.animation_data is None or key_src.animation_data.action is None:
                continue

            action_src = key_src.animation_data.action
            
            # Get all fcurves from source
            src_fcurves = get_action_fcurves(action_src)

            for src_fcurve in src_fcurves:
                # Only process shapekey fcurves
                if not src_fcurve.data_path.startswith('key_blocks'):
                    continue

                # Extract shapekey name
                match = re.match(r'key_blocks\["(.+?)"\]\.value', src_fcurve.data_path)
                if not match:
                    continue

                key_name = match.group(1)

                # Check if key exists in source
                if key_name not in key_src.key_blocks:
                    skipped_curves += 1
                    continue

                # Skip if active_only and key is muted
                if active_only and key_src.key_blocks[key_name].mute:
                    skipped_curves += 1
                    continue

                # Check if key exists in target
                if key_name not in key_tgt.key_blocks:
                    skipped_curves += 1
                    continue

                # Get or create target fcurve
                data_path = f'key_blocks["{key_name}"].value'
                tgt_fcurve = self.get_or_create_fcurve(action_tgt, data_path, src_fcurve.array_index, key_tgt)
                
                if tgt_fcurve is None:
                    skipped_curves += 1
                    continue

                # Copy keyframes
                try:
                    self.copy_fcurve_keyframes(src_fcurve, tgt_fcurve)
                    copied_curves += 1
                except Exception as e:
                    skipped_curves += 1

        # For Blender 5.0, ensure the slot is properly bound to the target shape_keys
        if hasattr(action_tgt, 'slots') and len(action_tgt.slots) > 0:
            slot = action_tgt.slots[0]
            if hasattr(key_tgt.animation_data, 'action_slot_handle'):
                key_tgt.animation_data.action_slot_handle = slot.handle
        
        # Update scene
        context.view_layer.update()

        if copied_curves > 0:
            self.report({'INFO'}, f"Copied {copied_curves} animation curves, skipped {skipped_curves}.")
        else:
            self.report({'WARNING'}, f"No animation curves copied. Skipped {skipped_curves}.")
        
        return {'FINISHED'}


# ------------------------------------------------------------------------
# Zero All Shapekeys
# ------------------------------------------------------------------------
class ShapekeyZeroOperator(bpy.types.Operator):
    """Set the value of all shape keys on the selected object to 0 (except Basis)."""
    bl_idname = "object.shapekey_zero"
    bl_label = "Zero All Shapekeys"
    bl_description = "Set all shapekey values to 0 for the selected object"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT':
            self.report({'ERROR'}, "Operator must be run in Object mode.")
            return {'CANCELLED'}

        obj = context.view_layer.objects.active
        if obj is None:
            self.report({'ERROR'}, "No active object selected.")
            return {'CANCELLED'}

        if obj.type not in {'MESH', 'CURVE'}:
            self.report({'ERROR'}, "Active object must be a Mesh or Curve.")
            return {'CANCELLED'}

        if obj.data.shape_keys is None:
            self.report({'WARNING'}, "Active object has no shapekeys.")
            return {'CANCELLED'}

        count = 0
        for block in obj.data.shape_keys.key_blocks:
            if block.name == "Basis":
                continue
            block.value = 0.0
            count += 1

        self.report({'INFO'}, f"Set {count} shapekey(s) to 0.")
        return {'FINISHED'}


# ------------------------------------------------------------------------
# Copy Armature Animation
# ------------------------------------------------------------------------
class ArmatureAnimationCopyOperator(bpy.types.Operator):
    """Copy armature animation from selected sources to active target (armatures must be identical)."""
    bl_idname = "object.copy_armature_anim"
    bl_label = "Copy Armature Animation"
    bl_description = "Copy armature animation from selected sources to active target"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT':
            self.report({'ERROR'}, "Operator must be run in Object mode.")
            return {'CANCELLED'}

        selected_objects = context.selected_objects
        if len(selected_objects) < 2:
            self.report({'ERROR'}, "Select at least one source armature and one target armature (active).")
            return {'CANCELLED'}

        target = context.view_layer.objects.active
        if target is None:
            self.report({'ERROR'}, "No active (target) object selected.")
            return {'CANCELLED'}

        if target.type != 'ARMATURE':
            self.report({'ERROR'}, "Target must be an armature.")
            return {'CANCELLED'}

        sources = [obj for obj in selected_objects if obj != target and obj.type == 'ARMATURE']
        if not sources:
            self.report({'ERROR'}, "No source armature objects selected.")
            return {'CANCELLED'}

        if target.animation_data is None:
            target.animation_data_create()

        ad_tgt = target.animation_data
        copied_strips = 0
        slot_action_assigned = False

        print("\n===== BEGIN DEBUG: COPY ARMATURE ANIMATION =====")
        print(f"TARGET: {target.name}")
        print(f"SOURCES: {[s.name for s in sources]}")

        for src in sources:
            print(f"\n--- Processing source: {src.name} ---")
            
            if src.animation_data is None:
                print(f"  ✗ No animation_data on source '{src.name}'")
                continue

            ad_src = src.animation_data
            print(f"  ✓ Source has animation_data")

            # Copy the action if present
            if ad_src.action:
                src_action = ad_src.action
                print(f"  ACTION: {src_action.name}")
                
                # Debug: Show action structure
                if hasattr(src_action, 'layers'):
                    print(f"    Action has {len(src_action.layers)} layer(s) (Blender 5.0+ layered system)")
                    for i, layer in enumerate(src_action.layers):
                        print(f"      Layer {i}: {layer.name} with {len(layer.strips)} strip(s)")
                elif hasattr(src_action, 'fcurves'):
                    print(f"    Action has {len(src_action.fcurves)} F-curves (legacy animation system)")
                else:
                    print(f"    ⚠ Unknown action structure")
                
                # Check slots
                source_slot_handle = None
                source_slot_name = None
                if hasattr(src_action, 'slots'):
                    print(f"    Action has {len(src_action.slots)} slot(s)")
                    
                    # Get source's active slot handle
                    if hasattr(ad_src, 'action_slot_handle'):
                        source_slot_handle = ad_src.action_slot_handle
                        print(f"    Source is using slot handle: {source_slot_handle}")
                        
                        # Find which slot that handle corresponds to
                        for slot in src_action.slots:
                            if hasattr(slot, 'handle') and slot.handle == source_slot_handle:
                                source_slot_name = slot.name_display if hasattr(slot, 'name_display') else slot.name
                                slot_id_type = slot.id_type if hasattr(slot, 'id_type') else 'Unknown'
                                print(f"      -> This is slot '{source_slot_name}' (type: {slot_id_type})")
                                break
                    
                    # List all slots with handles
                    for slot in src_action.slots:
                        slot_name = slot.name_display if hasattr(slot, 'name_display') else slot.name
                        slot_id_type = slot.id_type if hasattr(slot, 'id_type') else 'Unknown'
                        slot_handle = slot.handle if hasattr(slot, 'handle') else 'N/A'
                        marker = " <- SOURCE ACTIVE" if slot_handle == source_slot_handle else ""
                        print(f"      - Slot: {slot_name} (type: {slot_id_type}, handle: {slot_handle}){marker}")
                
                # Make a copy of the action for the target
                new_action = src_action.copy()
                new_action.name = f"{target.name}_{src_action.name}"
                print(f"    Created action copy: {new_action.name}")
                
                # Assign the copied action to the target
                ad_tgt.action = new_action
                print(f"    Assigned copied action to target.animation_data.action")
                
                # For Blender 4.0+ with animation slots, ensure proper slot assignment
                if hasattr(new_action, 'slots'):
                    print(f"    Configuring slots for target...")
                    
                    # Find the armature slot in the copied action
                    armature_slot = None
                    armature_slot_handle = None
                    
                    for slot in new_action.slots:
                        slot_name = slot.name_display if hasattr(slot, 'name_display') else slot.name
                        slot_id_type = slot.id_type if hasattr(slot, 'id_type') else None
                        slot_handle = slot.handle if hasattr(slot, 'handle') else None
                        
                        # Look for ARMATURE type slot, or match the source's slot name
                        is_armature_type = (slot_id_type == 'ARMATURE')
                        is_source_slot = (source_slot_name and slot_name == source_slot_name)
                        
                        if is_armature_type or is_source_slot:
                            armature_slot = slot
                            armature_slot_handle = slot_handle
                            print(f"    ✓ Found armature slot: '{slot_name}' (type: {slot_id_type}, handle: {armature_slot_handle})")
                            break
                    
                    if armature_slot and armature_slot_handle:
                        # Set the target to use this slot
                        if hasattr(ad_tgt, 'action_slot_handle'):
                            old_handle = ad_tgt.action_slot_handle
                            ad_tgt.action_slot_handle = armature_slot_handle
                            print(f"    ✓ Changed target action_slot_handle: {old_handle} -> {armature_slot_handle}")
                        else:
                            print(f"    ⚠ animation_data doesn't have action_slot_handle attribute")
                        
                        # Try assign_id with the specific slot
                        if hasattr(new_action, 'assign_id'):
                            try:
                                result = new_action.assign_id(armature_slot, target.data)
                                print(f"    ✓ Called assign_id(armature_slot, target.data) - returned: {result}")
                            except Exception as e:
                                print(f"    ✗ assign_id() with slot failed: {e}")
                    else:
                        print(f"    ✗ Could not find armature slot in copied action!")
                        print(f"       Available slots:")
                        for slot in new_action.slots:
                            slot_name = slot.name_display if hasattr(slot, 'name_display') else slot.name
                            slot_id_type = slot.id_type if hasattr(slot, 'id_type') else 'Unknown'
                            slot_handle = slot.handle if hasattr(slot, 'handle') else 'N/A'
                            print(f"         - {slot_name} (type: {slot_id_type}, handle: {slot_handle})")
                    
                    # Show final configuration
                    if hasattr(ad_tgt, 'action_slot_handle'):
                        final_handle = ad_tgt.action_slot_handle
                        print(f"    Final target action_slot_handle: {final_handle}")
                        
                        # Verify which slot this corresponds to
                        for slot in new_action.slots:
                            if hasattr(slot, 'handle') and slot.handle == final_handle:
                                slot_name = slot.name_display if hasattr(slot, 'name_display') else slot.name
                                slot_id_type = slot.id_type if hasattr(slot, 'id_type') else 'Unknown'
                                print(f"      -> Target is now using slot '{slot_name}' (type: {slot_id_type}) ✓")
                                break
                
                slot_action_assigned = True
                print(f"    ✓ Action assignment complete")

            # Copy NLA tracks
            if ad_src.nla_tracks:
                for track in ad_src.nla_tracks:
                    print(f"  TRACK: {track.name}")
                    new_track = ad_tgt.nla_tracks.new()
                    new_track.name = track.name
                    for strip in track.strips:
                        print(f"    STRIP: {strip.name}  ACTION: {strip.action.name if strip.action else None}")
                        ns = new_track.strips.new(strip.name, int(strip.frame_start), strip.action)
                        ns.frame_end = strip.frame_end
                        copied_strips += 1

        print("===== END DEBUG =====\n")

        context.view_layer.update()

        if slot_action_assigned and copied_strips > 0:
            self.report({'INFO'}, f"Assigned slot action and copied {copied_strips} NLA strips. See console for details.")
        elif slot_action_assigned:
            self.report({'INFO'}, f"Assigned slot action. See console for details.")
        elif copied_strips > 0:
            self.report({'INFO'}, f"Copied {copied_strips} NLA strips. See console for details.")
        else:
            self.report({'WARNING'}, "No animation data found to copy.")
        
        return {'FINISHED'}

# ------------------------------------------------------------------------
# Register / Unregister
# ------------------------------------------------------------------------
def register():
    bpy.types.Scene.name_only = bpy.props.BoolProperty(
        name="Name Only",
        description="Create shape keys with names only, without copying deformation data",
        default=False
    )
    bpy.types.Scene.active_only = bpy.props.BoolProperty(
        name="Active Only",
        description="Only copy unmuted (active) shape keys and their animations",
        default=False
    )

    bpy.utils.register_class(ShapekeyTransferPanel)
    bpy.utils.register_class(ShapekeyTransferOperator)
    bpy.utils.register_class(ShapekeyAnimationTransferOperator)
    bpy.utils.register_class(VertexGroupTransferOperator)
    bpy.utils.register_class(ShapekeyZeroOperator)
    bpy.utils.register_class(ArmatureAnimationCopyOperator)


def unregister():
    del bpy.types.Scene.name_only
    del bpy.types.Scene.active_only

    bpy.utils.unregister_class(ShapekeyTransferPanel)
    bpy.utils.unregister_class(ShapekeyTransferOperator)
    bpy.utils.unregister_class(ShapekeyAnimationTransferOperator)
    bpy.utils.unregister_class(VertexGroupTransferOperator)
    bpy.utils.unregister_class(ShapekeyZeroOperator)
    bpy.utils.unregister_class(ArmatureAnimationCopyOperator)


if __name__ == "__main__":
    register()
