import bpy
import re


class ShapekeyTransferPanel(bpy.types.Panel):
    """UI panel for copying shapekeys and their animation from one or more sources to a single target."""
    bl_label = "Copy Shapekeys"
    bl_idname = "PT_ShapekeyTransfer"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Tool'

    def draw(self, context):
        layout = self.layout
        layout.label(text="Select sources then target and click the button.")
        col = layout.column(align=True)
        # Add the "Name Only" checkbox
        col.prop(context.scene, "name_only", text="Name Only")
        col.prop(context.scene, "active_only", text="Active Only")
        col.operator("object.shapekey_transfer", text="Copy Shape keys", icon="COPYDOWN")
        col.operator("object.shapekey_animation_transfer", text="Copy Animation", icon="ANIM")
        col.operator("object.vertexgroup_transfer", text="Copy Vertex Groups", icon="GROUP_VERTEX")

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

            # Topology check
            if len(src.data.vertices) != len(target.data.vertices):
                self.report({'WARNING'},
                    f"'{src.name}' skipped: vertex count mismatch ({len(src.data.vertices)} vs {len(target.data.vertices)})")
                skipped += 1
                continue

            # Copy each vertex group
            for vg_src in src.vertex_groups:

                # Ensure target has this vertex group
                if vg_src.name not in target.vertex_groups:
                    target.vertex_groups.new(name=vg_src.name)
                vg_tgt = target.vertex_groups[vg_src.name]

                # ---- FIX: CLEAR WEIGHTS MANUALLY ----
                # Remove membership from ALL vertices
                for v in target.data.vertices:
                    try:
                        vg_tgt.remove([v.index])
                    except RuntimeError:
                        pass

                # Now transfer weights
                for v in src.data.vertices:
                    try:
                        w = vg_src.weight(v.index)
                        vg_tgt.add([v.index], w, 'REPLACE')
                    except RuntimeError:
                        # Vertex has no weight → skip
                        pass

                copied += 1

        self.report({'INFO'}, f"Copied {copied} vertex groups, skipped {skipped}.")
        return {'FINISHED'}

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

        sources = [obj for obj in selected_objects if obj != target_object]
        if not sources:
            self.report({'ERROR'}, "No source objects selected.")
            return {'CANCELLED'}

        if target_object.type != 'MESH':
            self.report({'ERROR'}, "Target object must be a mesh.")
            return {'CANCELLED'}

        if target_object.data.shape_keys is None:
            target_object.shape_key_add(name="Basis", from_mix=False)

        copied_count = 0
        skipped_count = 0
        active_only = context.scene.active_only
        name_only = context.scene.name_only  # Check the "Name Only" checkbox state

        for src in sources:
            if src.data.shape_keys is None:
                self.report({'WARNING'}, f"Source '{src.name}' has no shape keys. Skipped.")
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
                    if not name_only:  # Only copy vertex deformations if "Name Only" is unchecked
                        for v_src, v_tgt in zip(key.data, new_key.data):
                            v_tgt.co = v_src.co
                    copied_count += 1
                except Exception as e:
                    self.report({'WARNING'}, f"Failed to copy shapekey '{key.name}' from '{src.name}': {str(e)}")
                    skipped_count += 1

        for key in target_object.data.shape_keys.key_blocks:
            key.value = 0.0

        self.report({'INFO'}, f"Copied {copied_count} shape keys, skipped {skipped_count} duplicates or muted.")
        return {'FINISHED'}


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
        if target is None or target.type != 'MESH':
            self.report({'ERROR'}, "Active object must be a mesh target.")
            return {'CANCELLED'}

        sources = [obj for obj in selected_objects if obj != target]
        if not sources:
            self.report({'ERROR'}, "No source objects selected.")
            return {'CANCELLED'}

        if target.data.shape_keys is None:
            target.shape_key_add(name="Basis", from_mix=False)
        key_tgt = target.data.shape_keys

        for key in key_tgt.key_blocks:
            if len(key.data) != len(target.data.vertices):
                self.report({'WARNING'}, f"Shape key '{key.name}' on target has mismatched vertex count. Animation may not work.")
                continue

        if key_tgt.animation_data is None:
            key_tgt.animation_data_create()

        if key_tgt.animation_data.action is None:
            key_tgt.animation_data.action = bpy.data.actions.new(name=f"{target.name}_ShapekeyAction")
        action_tgt = key_tgt.animation_data.action

        copied_curves = 0
        skipped_curves = 0
        active_only = context.scene.active_only

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

            num_target_keyframes = sum(len(fc.keyframe_points) for fc in key_tgt.animation_data.action.fcurves if fc.data_path.startswith('key_blocks'))
            if paste_success and num_target_keyframes >= num_source_keyframes:
                copied_curves += len(filtered_fcurves)
            else:
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

        context.area.type = original_area_type
        if original_space_mode:
            context.space_data.mode = original_space_mode
        context.scene.frame_set(original_frame)

        for key in key_tgt.key_blocks:
            if key.name == "Basis":
                continue
            original_value = key.value
            key.value = 1.0 if original_value != 1.0 else 0.0
            key.value = original_value

        target.update_tag()
        context.scene.frame_set(context.scene.frame_current)

        self.report({'INFO'}, f"Copied {copied_curves} F-curves, skipped {skipped_curves} F-curves due to errors or muted.")
        return {'FINISHED'}


def register():
    # Register the "name_only" property
    bpy.types.Scene.name_only = bpy.props.BoolProperty(
        name="Name Only",
        description="Create shape keys with names only, without copying vertex deformations",
        default=False
    )
    # Register the "active_only" property
    bpy.types.Scene.active_only = bpy.props.BoolProperty(
        name="Active Only",
        description="Only copy unmuted (active) shape keys and their animations",
        default=False
    )
    bpy.utils.register_class(ShapekeyTransferPanel)
    bpy.utils.register_class(ShapekeyTransferOperator)
    bpy.utils.register_class(ShapekeyAnimationTransferOperator)
    bpy.utils.register_class(VertexGroupTransferOperator)


def unregister():
    # Unregister the properties
    del bpy.types.Scene.name_only
    del bpy.types.Scene.active_only
    bpy.utils.unregister_class(ShapekeyTransferPanel)
    bpy.utils.unregister_class(ShapekeyTransferOperator)
    bpy.utils.unregister_class(ShapekeyAnimationTransferOperator)
    bpy.utils.unregister_class(VertexGroupTransferOperator)


if __name__ == "__main__":
    register()