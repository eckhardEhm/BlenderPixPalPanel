"""
PixPal v0.4 — N-Panel colour palette picker for ImphenziaPixPal
=============================================================
Install:  Edit → Preferences → Add-ons → Install → select this file → enable
Panel:    3D Viewport → N key → PixPal tab

Workflow
--------
1. Select objects that already have ImphenziaPixPal material assigned.
2. Click "Sample Favourites" — reads all unique UV positions from those
   objects and creates a Favourite for each unique palette pixel found.
3. Each Favourite row shows:
     [coloured assign button]  [editable label: region+name]  [toggle]
4. Click the coloured button to assign that UV to selection/object.
5. Check the toggle on one or more Favourites, then use the
   Finetune section (Saturation / Brightness buttons) to step all
   checked Favourites together.
6. Use "Read UV" (↙) to manually capture the active face's UV into
   a Favourite at any time.

## pixpal palette from Imphenzia: https://imphenzia.com/imphenzia-pixpal   
"""

import bpy
import bmesh
import json
import os
import tempfile
import numpy as np
from bpy.props import (StringProperty, IntProperty, CollectionProperty,
                       BoolProperty)
from bpy.types import PropertyGroup, Operator, Panel
import bpy.utils.previews

bl_info = {
    "name": "PixPal Panel",
    "author": "Eckhard Ehm / PixPal Palette and Material by Imphenzia",
    "version": (0, 0, 6),
    "blender": (4, 2, 0),
    "location": "View3D › N-Panel › PixPal",
    "description": "Quick UV reading and assignments for ImphenziaPixPal palette",
    "category": "UV",
}

# ---------------------------------------------------------------------------
# GLOBAL CONSTANTS & REGIONS
# ---------------------------------------------------------------------------

TEXTURE_SIZE  = 128

REGIONS = [
    # ── Large (8×8 regions) ──────────────────────────────────────────
    {"name": "STANDARD", "x0":   1, "y0":  7, "cell_size": 1,"cols": 48, "rows": 48, "sub_size": 8, "has_gray": True},
    {"name": "GLOW",     "x0": 51, "y0":  7, "cell_size": 1,"cols": 48, "rows": 48, "sub_size": 8, "has_gray": True},
    # ── Grid Small (4×4 subregions) ──────────────────────────────────
    {"name": "MIRROR",   "x0": 79, "y0": 72, "cell_size": 1,"cols": 24, "rows": 24, "sub_size": 4, "has_gray": True},
    {"name": "DULL",     "x0":   1, "y0": 103, "cell_size": 1,"cols": 24, "rows": 24, "sub_size": 4, "has_gray": True},
    {"name": "METAL",    "x0": 27, "y0": 103, "cell_size": 1,"cols": 24, "rows": 24, "sub_size": 4, "has_gray": True},
    {"name": "SHINY",    "x0": 53, "y0": 103, "cell_size": 1,"cols": 24, "rows": 24, "sub_size": 4, "has_gray": True},
    {"name": "PLASTIC",  "x0": 79, "y0": 103, "cell_size": 1,"cols": 24, "rows": 24, "sub_size": 4, "has_gray": True},
    # ── Organic/Special (No grayscale column) ─────────────────────────
    {"name": "TREE",     "x0":   1, "y0": 62, "cell_size": 1,"cols": 15, "rows": 34, "sub_size": 1, "has_gray": False},
    {"name": "WOOD",     "x0":  17, "y0": 62, "cell_size": 1,"cols": 15, "rows": 34, "sub_size": 1, "has_gray": False},
    {"name": "ROCK",     "x0": 33, "y0": 62, "cell_size": 1,"cols": 15, "rows": 34, "sub_size": 1, "has_gray": False},
    {"name": "BUSH",     "x0": 49, "y0": 62, "cell_size": 1,"cols": 15, "rows": 34, "sub_size": 1, "has_gray": False},
    {"name": "ICE",      "x0": 65, "y0": 62, "cell_size": 1,"cols":  9, "rows": 34, "sub_size": 1, "has_gray": False},
    {"name": "SCROLL",   "x0": 105, "y0": 0, "cell_size": 1,"cols": 23, "rows": 128, "sub_size": 1, "has_gray": False},
]



# NOTE: REGIONS are approximate — tune x0/y0/cols/rows to match your texture.
# When Sample Favourites reports "Unknown", the pixel falls outside all region
# bounds. Use the MCP console output (px,py values) to identify the correct bounds.

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def get_palette_image(context):
    settings = context.scene.pixpal_settings
    return bpy.data.images.get(settings.palette_image)


def pixel_to_uv(px, py):
    """Pixel (top-left origin) → UV centre of that pixel (bottom-left origin)."""
    u = (px + 0.5) / TEXTURE_SIZE
    v = 1.0 - (py + 0.5) / TEXTURE_SIZE
    return u, v


def uv_to_pixel(u, v):
    """UV (bottom-left origin) → nearest pixel (top-left origin), clamped."""
    px = int(u * TEXTURE_SIZE)
    py = int((1.0 - v) * TEXTURE_SIZE)
    return max(0, min(TEXTURE_SIZE - 1, px)), max(0, min(TEXTURE_SIZE - 1, py))


def sample_pixel_color(px, py, cached_pixels=None):
    """Sample RGB of one pixel. Returns (r,g,b) 0–1 linear."""
    if cached_pixels is not None:
        pix = cached_pixels
    else:
        image = get_palette_image(bpy.context)
        if image is None: return (0.5, 0.5, 0.5)
        try:
            pix = image.pixels[:]
        except Exception: return (0.5, 0.5, 0.5)

    w = TEXTURE_SIZE
    h = TEXTURE_SIZE
    if len(pix) == 0: return (0.5, 0.5, 0.5)
    
    px  = max(0, min(w - 1, px))
    py  = max(0, min(h - 1, py))
    fy  = h - 1 - py
    idx = (fy * w + px) * 4
    if idx < 0 or idx + 2 >= len(pix):
        return (0.5, 0.5, 0.5)
    return (pix[idx], pix[idx + 1], pix[idx + 2])


def region_for_pixel(px, py):
    """Return (region_dict, sub_col, sub_row, is_gray) or (None, 0, 0, False)."""
    for r in REGIONS:
        cs = r["cell_size"]
        has_gray = r.get("has_gray", False)
        # Bounding box expands by 1px on the right if it has a grayscale column
        eff_cols = r["cols"] + 1 if has_gray else r["cols"]
        x1 = r["x0"] + eff_cols * cs
        y1 = r["y0"] + r["rows"] * cs
        
        if r["x0"] <= px < x1 and r["y0"] <= py < y1:
            sc = (px - r["x0"]) // cs
            sr = (py - r["y0"]) // cs
            is_gray = has_gray and sc == r["cols"]
            return r, sc, sr, is_gray
    return None, 0, 0, False


def apply_region_to_fav(fav, r, sub_col, sub_row, px, py, is_gray):
    fav.region_x0   = r["x0"]
    fav.region_y0   = r["y0"]
    fav.region_size = r["cell_size"]
    fav.region_cols = r["cols"]
    fav.region_rows = r["rows"]
    fav.sub_size    = r.get("sub_size", 1)
    fav.sub_col     = sub_col
    fav.sub_row     = sub_row
    fav.px          = px
    fav.py          = py
    fav.is_gray     = is_gray
    fav.color       = sample_pixel_color(px, py)


def default_label_for(px, py):
    """Return 'REGION_PX.PY' or 'REGION_Grey_RelY'."""
    r, sc, sr, is_gray = region_for_pixel(px, py)
    if r:
        n = r['name']
        if is_gray:
            rel_y = py - r['y0']
            return f"{n}_Grey_{rel_y}"
        else:
            return f"{n}_{px}.{py}"
    return f"Unknown_{px}.{py}"


def is_default_label(label, px, py):
    """Check if the label is one of the auto-generated ones for these coords."""
    if label == f"Unknown_{px}.{py}": return True
    r, sc, sr, is_gray = region_for_pixel(px, py)
    if not r: return False
    n = r['name']
    if is_gray:
        return label == f"{n}_Grey_{py - r['y0']}"
    return label == f"{n}_{px}.{py}"


def fav_exists(favs, px, py):
    """Return True if a favourite with this exact px,py already exists."""
    return any(f.px == px and f.py == py for f in favs)


def read_active_uv(obj):
    """Return (u,v) of active/first-selected face, or None."""
    mesh = obj.data
    is_edit = (bpy.context.mode == 'EDIT_MESH')
    
    if is_edit:
        bm = bmesh.from_edit_mesh(mesh)
    else:
        bm = bmesh.new()
        bm.from_mesh(mesh)
        
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.active
    
    if uv_layer is None:
        if not is_edit: bm.free()
        return None
        
    # Prioritize selected face
    face = next((f for f in bm.faces if f.select), None)
    if face is None and bm.faces:
        face = bm.faces[0]
        
    if face is None:
        if not is_edit: bm.free()
        return None
        
    uv = face.loops[0][uv_layer].uv.copy()
    
    if not is_edit:
        bm.free()
        
    return uv.x, uv.y


def ensure_material_on_object(context, obj):
    settings = context.scene.pixpal_settings
    mat = bpy.data.materials.get(settings.material_name)
    if mat is None:
        return False
    if not obj.material_slots:
        obj.data.materials.append(mat)
    else:
        obj.material_slots[0].material = mat
    return True


def assign_uv_to_faces(obj, u, v, selected_only):
    mesh = obj.data
    if not mesh.uv_layers:
        mesh.uv_layers.new(name="UVMap")
    uv_name = mesh.uv_layers.active.name
    in_edit  = (bpy.context.mode == 'EDIT_MESH')
    bm = bmesh.from_edit_mesh(mesh) if in_edit else bmesh.new()
    if not in_edit:
        bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.get(uv_name) or bm.loops.layers.uv.new(uv_name)
    for face in bm.faces:
        if selected_only and not face.select:
            continue
        for loop in face.loops:
            loop[uv_layer].uv = (u, v)
    if in_edit:
        bmesh.update_edit_mesh(mesh)
    else:
        bm.to_mesh(mesh)
        bm.free()
        mesh.update()


# ---------------------------------------------------------------------------
# PROPERTY GROUP
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ICON MANAGEMENT (INTERNAL IMAGES)
# ---------------------------------------------------------------------------

def update_fav_icon(fav):
    """Create/update a 1x1 coordinate-based image for this favourite."""
    # Use coordinates for the name so it's stable if indices shift
    img_name = f".pix_{fav.px}_{fav.py}"
    img = bpy.data.images.get(img_name)
    
    if not img:
        img = bpy.data.images.new(img_name, 1, 1, alpha=True)
        img.use_fake_user = True
    
    # Linear to sRGB approx
    def to_srgb(c): return pow(max(0, c), 1/2.2)
    color_data = [to_srgb(fav.color[0]), to_srgb(fav.color[1]), to_srgb(fav.color[2]), 1.0]
    
    img.pixels = color_data
    img.preview_ensure()
    # Explicitly reload preview to force UI refresh
    img.preview.reload()


def clear_fav_icons():
    # Only clear orphans or on unregister
    for img in list(bpy.data.images):
        if img.name.startswith(".pix_"):
            bpy.data.images.remove(img)


def refresh_all_fav_icons(context):
    for fav in context.scene.pixpal_favourites:
        update_fav_icon(fav)


def on_fav_update(self, context):
    update_fav_icon(self)


class PixPalSettings(PropertyGroup):
    material_name: StringProperty(
        name="Material",
        description="Name of the ImphenziaPixPal material",
        default="ImphenziaPixPal"
    )
    palette_image: StringProperty(
        name="Palette Image",
        description="Name of the palette texture image file in Blender",
        default="ImphenziaPixPal-BaseColor.png"
    )


class PixPalFavourite(PropertyGroup):
    label:        StringProperty(name="Label", default="Colour")
    px:           IntProperty(default=0, update=on_fav_update)
    py:           IntProperty(default=0, update=on_fav_update)
    region_x0:    IntProperty(default=0)
    region_y0:    IntProperty(default=0)
    region_size:  IntProperty(default=1)
    region_cols:  IntProperty(default=8)
    region_rows:  IntProperty(default=8)
    sub_size:     IntProperty(default=1) # 4 or 8 for the grid regions
    sub_col:      IntProperty(default=0)
    sub_row:      IntProperty(default=0)
    is_gray:      BoolProperty(default=False) # color in gray scale of grid region picked
    finetune:     BoolProperty(
        name="Toggle to finetune Subregion Steps in Brightness and Saturation",
        description="Include this Favourite when using the Finetune step buttons",
        default=False
    )
    color: bpy.props.FloatVectorProperty(
        name="Color", subtype='COLOR',
        size=3, min=0, max=1, default=(0.5, 0.5, 0.5),
        update=on_fav_update
    )


# ---------------------------------------------------------------------------
# SCENE STORAGE
# ---------------------------------------------------------------------------

STORAGE_KEY = "pixpal_favourites"

def save_favourites(context):
    fields = ("label","px","py","region_x0","region_y0","region_size",
              "region_cols","region_rows","sub_size","sub_col","sub_row",
              "is_gray","finetune")
    data = []
    for fav in context.scene.pixpal_favourites:
        d = {k: getattr(fav, k) for k in fields}
        d["color"] = list(fav.color)
        data.append(d)
    context.scene[STORAGE_KEY] = json.dumps(data)


def load_favourites(context):
    try:
        data = json.loads(context.scene.get(STORAGE_KEY, "[]"))
    except Exception:
        return
    context.scene.pixpal_favourites.clear()
    clear_fav_icons()
    for d in data:
        fav = context.scene.pixpal_favourites.add()
        for k, v in d.items():
            if k == "color":
                fav.color = v
            else:
                setattr(fav, k, v)
    refresh_all_fav_icons(context)


# ---------------------------------------------------------------------------
# OPERATORS
# ---------------------------------------------------------------------------

class PIXPAL_OT_sample_favourites(Operator):
    """Scan all selected objects with ImphenziaPixPal material and create
Favourites for every unique UV/palette pixel found. Skips duplicates."""
    bl_idname  = "pixpal.sample_favourites"
    bl_label   = "Sample Favourites"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        favs    = context.scene.pixpal_favourites
        
        # 1. Selection Check
        objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not objs:
            self.report({'WARNING'}, "Select at least one mesh object")
            return {'CANCELLED'}

        added   = 0
        skipped = 0
        in_edit = (context.mode == 'EDIT_MESH')
        
        # Performance: Copy palette pixels ONCE for the whole operation
        palette = get_palette_image(context)
        pixels_cache = palette.pixels[:] if palette else None

        mat_name = context.scene.pixpal_settings.material_name

        for obj in objs:
            has_mat = any(
                s.material and s.material.name == mat_name
                for s in obj.material_slots
            )
            if not has_mat:
                continue

            mesh = obj.data
            seen_pixels = set()

            if in_edit:
                bm = bmesh.from_edit_mesh(mesh)
                uv_lay = bm.loops.layers.uv.active
                if not uv_lay: continue
                
                for face in bm.faces:
                    if face.select:
                        for loop in face.loops:
                            uv = loop[uv_lay].uv
                            seen_pixels.add(uv_to_pixel(uv.x, uv.y))
            else:
                uv_layer = mesh.uv_layers.active
                if uv_layer is None: continue
                for poly in mesh.polygons:
                    for loop_idx in poly.loop_indices:
                        uv = uv_layer.data[loop_idx].uv
                        seen_pixels.add(uv_to_pixel(uv.x, uv.y))

            # Process collected pixels
            for px, py in sorted(seen_pixels):
                if fav_exists(favs, px, py):
                    skipped += 1
                    continue
                fav = favs.add()
                fav.label = default_label_for(px, py)
                r, sc, sr, is_gray = region_for_pixel(px, py)
                if r:
                    # Manually update to use cached pixels
                    fav.region_x0   = r["x0"]
                    fav.region_y0   = r["y0"]
                    fav.region_size = r["cell_size"]
                    fav.region_cols = r["cols"]
                    fav.region_rows = r["rows"]
                    fav.sub_size    = r.get("sub_size", 1)
                    fav.sub_col     = sc
                    fav.sub_row     = sr
                    fav.px          = px
                    fav.py          = py
                    fav.is_gray     = is_gray
                    fav.color       = sample_pixel_color(px, py, cached_pixels=pixels_cache)
                else:
                    fav.px    = px
                    fav.py    = py
                    fav.color = sample_pixel_color(px, py, cached_pixels=pixels_cache)
                added += 1

        refresh_all_fav_icons(context)
        save_favourites(context)
        self.report({'INFO'}, f"PixPal: {added} added, {skipped} skipped")
        return {'FINISHED'}


class PIXPAL_OT_assign(Operator):
    """Assign ImphenziaPixPal material + UV
Object mode: entire mesh  |  Edit mode: selected faces"""
    bl_idname  = "pixpal.assign"
    bl_label   = "Assign"
    bl_options = {'REGISTER', 'UNDO'}
    fav_index: IntProperty()

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "Select a mesh object first")
            return {'CANCELLED'}
        fav = context.scene.pixpal_favourites[self.fav_index]
        if not ensure_material_on_object(context, obj):
            mat_n = context.scene.pixpal_settings.material_name
            self.report({'ERROR'}, f"Material '{mat_n}' not found in file")
            return {'CANCELLED'}
        u, v = pixel_to_uv(fav.px, fav.py)
        assign_uv_to_faces(obj, u, v, selected_only=(context.mode == 'EDIT_MESH'))
        return {'FINISHED'}


class PIXPAL_OT_finetune_step(Operator):
    """Step all finetune-toggled Favourites
'Color' steps jump by subgrid size.
'Variance' steps move by 1px within the current subgrid."""
    bl_idname  = "pixpal.finetune_step"
    bl_label   = "Finetune Step"
    bl_options = {'REGISTER', 'UNDO'}
    
    mode:      StringProperty() # 'VARIANCE' or 'COLOR'
    direction: StringProperty() # 'LEFT', 'RIGHT', 'UP', 'DOWN'

    def execute(self, context):
        moved = 0
        for fav in context.scene.pixpal_favourites:
            if not fav.finetune:
                continue
            
            was_default = is_default_label(fav.label, fav.px, fav.py)
            ss = max(fav.sub_size, 1)
            
            # Current offsets relative to region start
            rel_x = fav.px - fav.region_x0
            rel_y = fav.py - fav.region_y0
            
            # Grid dimensions
            cols = max(fav.region_cols, 1)
            rows = max(fav.region_rows, 1)

            if fav.is_gray and not fav.finetune: # Safety check
                continue

            if self.mode == 'VARIANCE':
                # Move 1px, wrap within the ss*ss subregion
                if self.direction in {'LEFT', 'RIGHT'}:
                    if fav.is_gray: continue # No variance for 1px gray column
                    local_x = rel_x % ss
                    base_x  = (rel_x // ss) * ss
                    delta   = 1 if self.direction == 'RIGHT' else -1
                    rel_x   = base_x + (local_x + delta) % ss
                else:
                    local_y = rel_y % ss
                    base_y  = (rel_y // ss) * ss
                    delta   = 1 if self.direction == 'DOWN' else -1
                    rel_y   = base_y + (local_y + delta) % ss
            
            else: # mode == 'COLOR'
                # Move by sub_size, wrap within the whole region
                if self.direction in {'LEFT', 'RIGHT'}:
                    if fav.is_gray: continue
                    delta = ss if self.direction == 'RIGHT' else -ss
                    rel_x = (rel_x + delta) % cols
                else:
                    delta = ss if self.direction == 'DOWN' else -ss
                    rel_y = (rel_y + delta) % rows

            fav.px = fav.region_x0 + rel_x
            fav.py = fav.region_y0 + rel_y
            
            # Recalculate grid indices based on new position
            r, sc, sr, is_gray = region_for_pixel(fav.px, fav.py)
            if r:
                fav.sub_col = sc
                fav.sub_row = sr
                fav.is_gray = is_gray

            fav.color = sample_pixel_color(fav.px, fav.py)
            if was_default:
                fav.label = default_label_for(fav.px, fav.py)
            moved += 1
        
        if moved == 0:
            self.report({'INFO'}, "No Favourites moved")
        save_favourites(context)
        return {'FINISHED'}


class PIXPAL_OT_select_by_favourite(Operator):
    """Select geometry/objects matching this color.
Searches selected objects (if any) or entire scene. Shift-click to extend selection."""
    bl_idname  = "pixpal.select_by_favourite"
    bl_label   = "Select by Color"
    bl_options = {'REGISTER', 'UNDO'}
    
    fav_index: IntProperty()
    extend:    BoolProperty(default=False)

    def invoke(self, context, event):
        self.extend = event.shift
        return self.execute(context)

    def execute(self, context):
        fav = context.scene.pixpal_favourites[self.fav_index]
        in_edit = (context.mode == 'EDIT_MESH')
        
        if not self.extend:
            if in_edit: bpy.ops.mesh.select_all(action='DESELECT')
            else:       bpy.ops.object.select_all(action='DESELECT')

        count = 0
        if in_edit:
            # Edit mode: Select matching faces in active object
            obj = context.active_object
            if obj and obj.type == 'MESH':
                bm = bmesh.from_edit_mesh(obj.data)
                uv_lay = bm.loops.layers.uv.active
                if uv_lay:
                    for face in bm.faces:
                        # Check first loop UV
                        u, v = face.loops[0][uv_lay].uv
                        px, py = uv_to_pixel(u, v)
                        if px == fav.px and py == fav.py:
                            face.select = True
                            count += 1
                bmesh.update_edit_mesh(obj.data)
        else:
            # Object mode: 
            # 1. Decide Scope: Selected objects if any, else View Layer
            search_objs = [o for o in context.selected_objects if o.type == 'MESH']
            if not search_objs:
                search_objs = [o for o in context.view_layer.objects if o.type == 'MESH']

            mat_name = context.scene.pixpal_settings.material_name

            for obj in search_objs:
                # 2. Pre-check Material
                has_mat = any(s.material and s.material.name == mat_name 
                              for s in obj.material_slots)
                if not has_mat:
                    continue

                mesh = obj.data
                uv_layer = mesh.uv_layers.active
                if not uv_layer: continue
                
                # Fast scan
                found = False
                for poly in mesh.polygons:
                    u, v = uv_layer.data[poly.loop_indices[0]].uv
                    px, py = uv_to_pixel(u, v)
                    if px == fav.px and py == fav.py:
                        found = True
                        break
                if found:
                    obj.select_set(True)
                    count += 1

        self.report({'INFO'}, f"PixPal: Selected {count} {'faces' if in_edit else 'objects'}")
        return {'FINISHED'}


def try_sample_to_fav(context, fav):
    """Attempt to fill 'fav' with UV data from selection. Returns True if successful."""
    obj = context.active_object
    if not obj or obj.type != 'MESH':
        return False
    
    uv = read_active_uv(obj)
    if uv is None:
        return False
        
    px, py = uv_to_pixel(uv[0], uv[1])
    r, sub_col, sub_row, is_gray = region_for_pixel(px, py)
    
    if r:
        apply_region_to_fav(fav, r, sub_col, sub_row, px, py, is_gray)
        fav.label = default_label_for(px, py)
    else:
        fav.px = px
        fav.py = py
        fav.is_gray = False
        fav.color = sample_pixel_color(px, py)
        fav.label = default_label_for(px, py)
    return True


class PIXPAL_OT_read_uv(Operator):
    """Sample UV from active/selected face and store as this Favourite's palette position"""
    bl_idname  = "pixpal.read_uv"
    bl_label   = "Sample UV from Mesh"
    bl_options = {'REGISTER', 'UNDO'}
    fav_index: IntProperty()

    def execute(self, context):
        fav = context.scene.pixpal_favourites[self.fav_index]
        if try_sample_to_fav(context, fav):
            self.report({'INFO'}, f"PixPal: Sampled to {fav.label}")
        else:
            self.report({'WARNING'}, "Nothing valid to sample (select a face or mesh)")
        save_favourites(context)
        return {'FINISHED'}


class PIXPAL_OT_add_favourite(Operator):
    """Add a new Favourite slot. Automatically samples selection if possible."""
    bl_idname  = "pixpal.add_favourite"
    bl_label   = "Add Favourite"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        favs = context.scene.pixpal_favourites
        fav = favs.add()
        
        # Try to sample selection
        if not try_sample_to_fav(context, fav):
            # Fallback to default
            r = REGIONS[0]
            apply_region_to_fav(fav, r, 0, 0, r["x0"], r["y0"], False)
            fav.label = default_label_for(fav.px, fav.py)
        
        update_fav_icon(fav)
        save_favourites(context)
        return {'FINISHED'}


class PIXPAL_OT_remove_favourite(Operator):
    """Remove this Favourite"""
    bl_idname  = "pixpal.remove_favourite"
    bl_label   = "Remove Favourite"
    bl_options = {'REGISTER', 'UNDO'}
    fav_index: IntProperty()

    def execute(self, context):
        context.scene.pixpal_favourites.remove(self.fav_index)
        save_favourites(context)
        return {'FINISHED'}


# Logic moved to property updates and refresh helpers above

# ---------------------------------------------------------------------------
# N-PANEL
# ---------------------------------------------------------------------------

class PIXPAL_PT_main(Panel):
    bl_label       = "PixPal"
    bl_idname      = "PIXPAL_PT_main"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "PixPal"

    def draw(self, context):
        layout   = self.layout
        favs     = context.scene.pixpal_favourites
        settings = context.scene.pixpal_settings

        # ── Error Reporting / Warnings ────────────────────────────────────
        palette_img = get_palette_image(context)
        palette_mat = bpy.data.materials.get(settings.material_name)

        if not palette_img or not palette_mat:
            err = layout.box()
            err.alert = True
            if not palette_img:
                err.label(text=f"Image Error: '{settings.palette_image}' missing!", icon='ERROR')
            if not palette_mat:
                err.label(text=f"Material Error: '{settings.material_name}' missing!", icon='ERROR')
            layout.separator()

        # Load from scene storage on first draw if list is empty
        # Use get() with default to avoid KeyError when storage key absent
        stored = context.scene.get(STORAGE_KEY)
        if len(favs) == 0 and stored and stored != "[]":
            load_favourites(context)

        # ── SAMPLE button — full width ────────────────────────────────────
        layout.operator("pixpal.sample_favourites",
                        text="Sample Favourites", icon='EYEDROPPER')

        layout.separator()

        # ── FINETUNE section ──────────────────────────────────────────────
        box = layout.box()
        box.scale_y = 1.1 # Padding-like height
        
        # Helper to draw a d-pad row
        def draw_stepper(layout, mode, label):
            row = layout.row(align=True)
            # Left
            op = row.operator("pixpal.finetune_step", text="", icon='TRIA_LEFT')
            op.mode = mode
            op.direction = 'LEFT'
            # Right
            op = row.operator("pixpal.finetune_step", text="", icon='TRIA_RIGHT')
            op.mode = mode
            op.direction = 'RIGHT'
            # Up
            op = row.operator("pixpal.finetune_step", text="", icon='TRIA_UP')
            op.mode = mode
            op.direction = 'UP'
            # Down
            op = row.operator("pixpal.finetune_step", text="", icon='TRIA_DOWN')
            op.mode = mode
            op.direction = 'DOWN'
            
            row.separator(factor=2.0)
            row.label(text=label)

        draw_stepper(box, 'VARIANCE', "Variance")
        draw_stepper(box, 'COLOR',    "Color")

        layout.separator()

        # ── FAVOURITES list ───────────────────────────────────────────────
        if favs:
            layout.label(text="Favourites")
            for i, fav in enumerate(favs):
                row = layout.row(align=True)

                # 1. Swatch Button (Assign)
                img_name = f".pix_{fav.px}_{fav.py}"
                img = bpy.data.images.get(img_name)
                icon_val = img.preview.icon_id if (img and img.preview) else 0
                
                row.scale_x = 0.75
                op_s = row.operator("pixpal.assign", text="", icon_value=icon_val)
                op_s.fav_index = i
                row.scale_x = 1.0

                # 2. Brush Button (Small Assign)
                sub_b = row.row(align=True)
                sub_b.scale_x = 1.2
                op_b = sub_b.operator("pixpal.assign", text="", icon='BRUSH_DATA')
                op_b.fav_index = i

                # 3. Label (Flexible)
                row.prop(fav, "label", text="")

                # 4. Utilities: Select, Sample, Remove
                sub_util = row.row(align=True)
                sub_util.scale_x = 1.3 
                
                # Select (Magnify)
                op_sel = sub_util.operator("pixpal.select_by_favourite", text="", icon='VIEWZOOM')
                op_sel.fav_index = i
                
                # Sample (Import)
                op_read = sub_util.operator("pixpal.read_uv", text="", icon='IMPORT')
                op_read.fav_index = i
                
                # Remove
                sub_rem = sub_util.row(align=True)
                sub_rem.alert = True
                op_rem = sub_rem.operator("pixpal.remove_favourite", text="", icon='X')
                op_rem.fav_index = i

                # 6. Finetune toggle
                row.prop(fav, "finetune", text="",
                         icon='CHECKBOX_HLT' if fav.finetune else 'CHECKBOX_DEHLT')

        layout.separator()
        layout.operator("pixpal.add_favourite",
                        text="+ Add new Favourite", icon='ADD')
        
        # ── SETTINGS section ──────────────────────────────────────────────
        layout.separator()
        set_box = layout.box()
        set_box.scale_y = 0.9
        row = set_box.row()
        row.prop(settings, "material_name", text="", icon='MATERIAL')
        row.prop(settings, "palette_image", text="", icon='IMAGE_DATA')


# ---------------------------------------------------------------------------
# REGISTRATION
# ---------------------------------------------------------------------------

classes = [
    PixPalSettings,
    PixPalFavourite,
    PIXPAL_OT_sample_favourites,
    PIXPAL_OT_assign,
    PIXPAL_OT_select_by_favourite,
    PIXPAL_OT_finetune_step,
    PIXPAL_OT_read_uv,
    PIXPAL_OT_add_favourite,
    PIXPAL_OT_remove_favourite,
    PIXPAL_PT_main,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.pixpal_favourites = CollectionProperty(type=PixPalFavourite)
    bpy.types.Scene.pixpal_settings   = bpy.props.PointerProperty(type=PixPalSettings)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.pixpal_favourites
    del bpy.types.Scene.pixpal_settings
    clear_fav_icons()


if __name__ == "__main__":
    try:
        unregister()
    except Exception:
        pass
    register()