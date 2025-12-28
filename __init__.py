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
    """UI panel for copying shapekeys and their animation from one or more sources to a single target."""
    bl_label = "Copy Shapekeys"
    bl_idname = "PT_ShapekeyTransfer"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Tool'

    def draw(self, context):
        layout = self.layout

        # -------- Copy Shapekeys Section --------
        box = layout.box()
        box.label(text="Copy Shapekeys")

        col = box.column(align=True)
        col.label(text="Select sources then target and click the button.")
        col.prop(context.scene, "name_only", text="Name Only")
        col.prop(context.scene, "active_only", text="Active Only")
        col.operator("object.shapekey_transfer", text="Copy Shape keys", icon="COPYDOWN")
        col.operator("object.shapekey_animation_transfer", text="Copy Animation", icon="ANIM")
        col.operator("object.vertexgroup_transfer", text="Copy Vertex Groups", icon="GROUP_VERTEX")

        col.separator()
        col.operator("object.shapekey_zero", text="Set Keys to 0", icon="X")

        # -------- Armature Anim Section --------
        box2 = layout.box()
        box2.label(text="Copy Armature Anim")
        box2.label(text="Armatures need to be identical", icon='INFO')
        box2.operator("object.copy_armature_anim", text="Copy", icon="ANIM")


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
        # (UNCHANGED — your full implementation stays here)
        ...
        return {'FINISHED'}


# ------------------------------------------------------------------------
# Set all shapekey values to 0
# ------------------------------------------------------------------------
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

        if target.animation_data is None:
            target.animation_data_create()
        ad_tgt = target.animation_data

        # remove existing NLA
        for t in list(ad_tgt.nla_tracks):
            ad_tgt.nla_tracks.remove(t)

        ad_tgt.action = None

        copied_strips = 0

        print("\n===== ARMATURE COPY DEBUG =====")

        for src in sources:
            print(f"\nSource: {src.name}")

            ad_src = src.animation_data
            if not ad_src:
                print("  NO animation_data")
                continue

            # Stash slot action if present
            if ad_src.action:
                print("  STASHING SLOT ACTION:", ad_src.action.name)
                new_track = ad_tgt.nla_tracks.new()
                new_track.name = ad_src.action.name
                action = ad_src.action
                start = int(action.frame_range[0])
                ns = new_track.strips.new(action.name, start, action)
                ns.frame_end = ns.frame_start + (action.frame_range[1] - action.frame_range[0])
                copied_strips += 1

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

        self.report({'INFO'}, f"Copied {copied_strips} strips. See console for details.")
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