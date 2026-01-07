import bpy
import re


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

        # Ensure Basis exists
        if target_object.data.shape_keys is None:
            target_object.shape_key_add(name="Basis", from_mix=False)

        copied_count = 0
        skipped_count = 0
        active_only = context.scene.active_only
        name_only = context.scene.name_only

        for src in sources:
            # Require matching object type for shapekey geometry transfer
            if src.type != target_object.type:
                self.report({'WARNING'}, f"Source '{src.name}' skipped: type mismatch (need {target_object.type}).")
                skipped_count += 1
                continue

            if src.data.shape_keys is None:
                self.report({'WARNING'}, f"Source '{src.name}' has no shape keys. Skipped.")
                continue

            # Curve topology check (only needed if we will copy deformation)
            if (target_object.type == 'CURVE') and (not name_only):
                if not curves_have_matching_topology(src.data, target_object.data):
                    self.report({'WARNING'}, f"Source '{src.name}' skipped: curve topology mismatch.")
                    continue

            for key in src.data.shape_keys.key_blocks:
                if key.name == "Basis" or (active_only and key.mute):
                    skipped_count += 1
                    continue

                if key.name in target_object.data.shape_keys.key_blocks:
                    skipped_count += 1
                    continue

                try:
                    new_key = target_object.shape_key_add(name=key.name, from_mix=False)

                    if not name_only:
                        # Mesh: require identical vertex count
                        if target_object.type == 'MESH':
                            if len(key.data) != len(new_key.data):
                                self.report({'WARNING'},
                                            f"'{src.name}' key '{key.name}' skipped: vertex count mismatch.")
                                # Remove the partially created key to avoid confusing results
                                target_object.shape_key_remove(new_key)
                                skipped_count += 1
                                continue

                            for v_src, v_tgt in zip(key.data, new_key.data):
                                v_tgt.co = v_src.co

                        # Curve: data is flattened points/handles; topology match checked above
                        elif target_object.type == 'CURVE':
                            if len(key.data) != len(new_key.data):
                                self.report({'WARNING'},
                                            f"'{src.name}' key '{key.name}' skipped: curve key data size mismatch.")
                                target_object.shape_key_remove(new_key)
                                skipped_count += 1
                                continue

                            for p_src, p_tgt in zip(key.data, new_key.data):
                                p_tgt.co = p_src.co

                    copied_count += 1

                except Exception as e:
                    self.report({'WARNING'}, f"Failed to copy shapekey '{key.name}' from '{src.name}': {str(e)}")
                    skipped_count += 1

        # Reset all shapekeys on target
        for key in target_object.data.shape_keys.key_blocks:
            key.value = 0.0

        self.report({'INFO'}, f"Copied {copied_count} shape keys, skipped {skipped_count} duplicates/muted/mismatched.")
        return {'FINISHED'}


# ------------------------------------------------------------------------
# Shapekey animation transfer (Mesh + Curve)
# ------------------------------------------------------------------------
class ShapekeyAnimationTransferOperator(bpy.types.Operator):
    """Copy shapekey animation using Dope Sheet copy-paste, with a fallback to manual transfer."""
    bl_idname = "object.shapekey_animation_transfer"
    bl_label = "Transfer Shapekey Animation"
    bl_description = "Copy shapekey animation curves from selected source objects to the active target object"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT':
            self.report({'ERROR'}, "Operator must be run in Object mode.")
            return {'CANCELLED'}

        selected_objects = context.selected_objects
        if len(selected_objects) < 2:
            self.report({'ERROR'}, "Select at least one source and one target (active).")
            return {'CANCELLED'}

        target = context.view_layer.objects.active
        if target is None or target.type not in {'MESH', 'CURVE'}:
            self.report({'ERROR'}, "Active object must be a Mesh or Curve target.")
            return {'CANCELLED'}

        sources = [obj for obj in selected_objects if obj != target]
        if not sources:
            self.report({'ERROR'}, "No source objects selected.")
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

        copied_curves = 0
        skipped_curves = 0
        active_only = context.scene.active_only

        # Figure out which shapekeys are animated on sources
        shapekeys_to_animate = set()
        for src in sources:
            key_src = src.data.shape_keys
            if key_src is None or key_src.animation_data is None or key_src.animation_data.action is None:
                continue
            for fcurve in key_src.animation_data.action.fcurves:
                if not fcurve.data_path.startswith('key_blocks'):
                    continue
                match = re.match(r'key_blocks\["(.+?)"\]\.value', fcurve.data_path)
                if match:
                    key_name = match.group(1)
                    if key_name in key_src.key_blocks and (not active_only or not key_src.key_blocks[key_name].mute):
                        shapekeys_to_animate.add(key_name)

        # Ensure target has an initial keyframe channel for each animated shapekey that exists on target
        for key_name in shapekeys_to_animate:
            if key_name in key_tgt.key_blocks:
                try:
                    data_path = f'key_blocks["{key_name}"].value'
                    existing_fcurve = action_tgt.fcurves.find(data_path)
                    if existing_fcurve:
                        action_tgt.fcurves.remove(existing_fcurve)

                    current_frame = context.scene.frame_current
                    key_tgt.key_blocks[key_name].value = 0.0
                    key_tgt.key_blocks[key_name].keyframe_insert(data_path="value", frame=current_frame)
                except Exception as e:
                    self.report({'WARNING'}, f"Failed to add initial keyframe for '{key_name}' on target: {str(e)}")

        # --- Dope Sheet copy/paste setup ---
        if context.area is None:
            self.report({'ERROR'}, "No active area context. Run from a normal Blender UI area.")
            return {'CANCELLED'}

        original_area_type = context.area.type
        original_space_mode = context.space_data.mode if original_area_type == 'DOPESHEET_EDITOR' else None
        original_frame = context.scene.frame_current

        context.area.type = 'DOPESHEET_EDITOR'
        context.space_data.mode = 'DOPESHEET'
        context.space_data.ui_mode = 'SHAPEKEY'
        context.space_data.dopesheet.show_only_selected = True
        context.space_data.dopesheet.show_hidden = False

        for src in sources:
            key_src = src.data.shape_keys
            if key_src is None:
                self.report({'WARNING'}, f"Source '{src.name}' has no shapekeys. Skipped.")
                continue
            if key_src.animation_data is None or key_src.animation_data.action is None:
                self.report({'WARNING'}, f"Source '{src.name}' has no shapekey animation. Skipped.")
                continue
            if not any(fcurve.data_path.startswith('key_blocks') for fcurve in key_src.animation_data.action.fcurves):
                self.report({'WARNING'}, f"Source '{src.name}' has no shapekey F-curves. Skipped.")
                continue

            filtered_fcurves = []
            for fcurve in key_src.animation_data.action.fcurves:
                if not fcurve.data_path.startswith('key_blocks'):
                    continue
                match = re.match(r'key_blocks\["(.+?)"\]\.value', fcurve.data_path)
                if match:
                    key_name = match.group(1)
                    if key_name in key_src.key_blocks and (not active_only or not key_src.key_blocks[key_name].mute):
                        filtered_fcurves.append(fcurve)

            if not filtered_fcurves:
                self.report({'WARNING'}, f"No unmuted shapekey F-curves to copy from '{src.name}'. Skipped.")
                continue

            bpy.ops.object.select_all(action='DESELECT')
            src.select_set(True)
            context.view_layer.objects.active = src

            try:
                bpy.ops.anim.channels_select_all(action='DESELECT')
                for fcurve in filtered_fcurves:
                    fcurve.select = True
                bpy.ops.action.copy()
            except Exception as e:
                self.report({'WARNING'}, f"Failed to copy keyframes from '{src.name}': {str(e)}")
                continue

            src.select_set(False)
            target.select_set(True)
            context.view_layer.objects.active = target

            context.scene.frame_set(0)

            paste_success = False
            num_source_keyframes = sum(len(fc.keyframe_points) for fc in filtered_fcurves)
            try:
                bpy.ops.anim.channels_select_all(action='DESELECT')
                bpy.ops.action.paste(offset=0, merge="OVERWRITE")
                paste_success = True
            except Exception as e:
                self.report({'WARNING'}, f"Failed to paste keyframes to '{target.name}': {str(e)}")

            num_target_keyframes = sum(
                len(fc.keyframe_points)
                for fc in key_tgt.animation_data.action.fcurves
                if fc.data_path.startswith('key_blocks')
            )

            if paste_success and num_target_keyframes >= num_source_keyframes:
                copied_curves += len(filtered_fcurves)
            else:
                # Manual fallback per F-curve
                for fcurve in filtered_fcurves:
                    match = re.match(r'key_blocks\["(.+?)"\]\.value', fcurve.data_path)
                    if not match:
                        continue
                    key_name = match.group(1)
                    if key_name not in key_tgt.key_blocks:
                        continue

                    existing_fcurve = action_tgt.fcurves.find(fcurve.data_path, index=fcurve.array_index)
                    if existing_fcurve:
                        action_tgt.fcurves.remove(existing_fcurve)

                    try:
                        for kp in fcurve.keyframe_points:
                            frame, value = kp.co
                            key_tgt.key_blocks[key_name].value = value
                            key_tgt.key_blocks[key_name].keyframe_insert(data_path="value", frame=frame)
                        copied_curves += 1
                    except Exception as e:
                        self.report({'WARNING'}, f"Fallback failed for '{fcurve.data_path}' from '{src.name}': {str(e)}")
                        skipped_curves += 1

        # Restore area
        context.area.type = original_area_type
        if original_space_mode:
            context.space_data.mode = original_space_mode
        context.scene.frame_set(original_frame)

        # Nudge update
        for key in key_tgt.key_blocks:
            if key.name == "Basis":
                continue
            original_value = key.value
            key.value = 1.0 if original_value != 1.0 else 0.0
            key.value = original_value

        target.update_tag()
        context.scene.frame_set(context.scene.frame_current)

        self.report({'INFO'}, f"Copied {copied_curves} F-curves, skipped {skipped_curves} due to errors/muted.")
        return {'FINISHED'}

class ShapekeyZeroOperator(bpy.types.Operator):
    """Set all shapekey values on selected objects to 0"""
    bl_idname = "object.shapekey_zero"
    bl_label = "Set Keys to 0"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT':
            self.report({'ERROR'}, "Operator must be run in Object mode.")
            return {'CANCELLED'}

        selected = context.selected_objects
        if not selected:
            self.report({'ERROR'}, "Select at least one object.")
            return {'CANCELLED'}

        reset_count = 0

        for obj in selected:
            if not hasattr(obj.data, "shape_keys"):
                continue

            keys = obj.data.shape_keys
            if not keys:
                continue

            for kb in keys.key_blocks:
                # Leave Basis alone
                if kb.name == "Basis":
                    continue
                kb.value = 0.0
                reset_count += 1

        self.report({'INFO'}, f"Reset {reset_count} shapekey values to 0.")
        return {'FINISHED'}

# ------------------------------------------------------------------------
# Copy Armature Anim
# ------------------------------------------------------------------------

class ArmatureAnimationCopyOperator(bpy.types.Operator):
    bl_idname = "object.copy_armature_anim"
    bl_label = "Copy"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):

        if context.mode != 'OBJECT':
            self.report({'ERROR'}, "Run in Object Mode.")
            return {'CANCELLED'}

        armatures = [o for o in context.selected_objects if o.type == 'ARMATURE']
        if len(armatures) < 2:
            self.report({'ERROR'}, "Select sources then the active target.")
            return {'CANCELLED'}

        target = context.view_layer.objects.active
        sources = [o for o in armatures if o != target]

        print("\n===== ARMATURE VERIFICATION =====")
        
        # Verify armatures are identical
        for src in sources:
            print(f"\nComparing {src.name} -> {target.name}")
            
            src_bones = src.data.bones
            tgt_bones = target.data.bones
            
            # Check bone count
            if len(src_bones) != len(tgt_bones):
                msg = f"Bone count mismatch: {src.name} has {len(src_bones)} bones, {target.name} has {len(tgt_bones)} bones"
                print(f"  ✗ {msg}")
                self.report({'ERROR'}, msg)
                return {'CANCELLED'}
            
            print(f"  ✓ Both have {len(src_bones)} bones")
            
            # Get bone names
            src_bone_names = set(bone.name for bone in src_bones)
            tgt_bone_names = set(bone.name for bone in tgt_bones)
            
            # Check for name mismatches
            missing_in_target = src_bone_names - tgt_bone_names
            extra_in_target = tgt_bone_names - src_bone_names
            
            if missing_in_target or extra_in_target:
                print(f"  ✗ Bone name mismatch:")
                if missing_in_target:
                    print(f"    Missing in target: {', '.join(sorted(missing_in_target))}")
                if extra_in_target:
                    print(f"    Extra in target: {', '.join(sorted(extra_in_target))}")
                
                self.report({'ERROR'}, f"Bone names don't match between {src.name} and {target.name}. See console.")
                return {'CANCELLED'}
            
            print(f"  ✓ All bone names match")
            
            # Check bone hierarchy
            hierarchy_mismatch = False
            for bone_name in src_bone_names:
                src_bone = src_bones[bone_name]
                tgt_bone = tgt_bones[bone_name]
                
                src_parent_name = src_bone.parent.name if src_bone.parent else None
                tgt_parent_name = tgt_bone.parent.name if tgt_bone.parent else None
                
                if src_parent_name != tgt_parent_name:
                    print(f"  ✗ Bone '{bone_name}' parent mismatch: '{src_parent_name}' vs '{tgt_parent_name}'")
                    hierarchy_mismatch = True
            
            if hierarchy_mismatch:
                self.report({'ERROR'}, f"Bone hierarchy doesn't match between {src.name} and {target.name}. See console.")
                return {'CANCELLED'}
            
            print(f"  ✓ Bone hierarchy matches")
            
            # Check armature data names (this is often the issue!)
            print(f"  Armature data IDs:")
            print(f"    Source: {src.data.name}")
            print(f"    Target: {target.data.name}")
            
            if src.data.name != target.data.name:
                print(f"  ⚠ Warning: Armature data names differ - this may cause animation issues!")
            
        print("===== ARMATURE STRUCTURES VERIFIED =====\n")

        if target.animation_data is None:
            target.animation_data_create()
        ad_tgt = target.animation_data

        # remove existing NLA
        for t in list(ad_tgt.nla_tracks):
            ad_tgt.nla_tracks.remove(t)

        ad_tgt.action = None

        copied_strips = 0
        slot_action_assigned = False

        print("\n===== ARMATURE COPY DEBUG =====")

        for src in sources:
            print(f"\nSource: {src.name}")

            ad_src = src.animation_data
            if not ad_src:
                print("  NO animation_data")
                continue

            # Assign slot action if present (only assign the first one found)
            if ad_src.action and not slot_action_assigned:
                print("  COPYING AND ASSIGNING SLOT ACTION:", ad_src.action.name)
                src_action = ad_src.action
                
                # Diagnose the action structure (Blender 5.0+ has layers, older has fcurves)
                if hasattr(src_action, 'layers'):
                    print(f"    Action has {len(src_action.layers)} layer(s) (Blender 5.0+ animation system)")
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
