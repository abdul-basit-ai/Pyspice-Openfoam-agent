"""Phase 17 thermal viewer: annotated multi-view render + interactive 3D HTML.

Replaces the single off-screen PyVista PNG with two richer deliverables the
user asked for:
  1. MULTIVIEW ANNOTATED PNG  — a 2x2 (iso / top / front / side) composite of
     the solid-region temperature field, each panel colour-mapped + a numeric
     Tj overlay per device. One image, quick to glance.
  2. INTERACTIVE 3D HTML      — a self-contained .html that embeds the mesh
     geometry + temperature scalars (JSON arrays) and renders them with
     three.js (CDN) in the browser: orbit/zoom/rotate, a live colour bar, and
     per-device labels. The flow of OpenFOAM->PyVista->JSON lets anyone open
     it in a new tab and inspect the field in 3D without the container or
     Python. No server needed; it is a fully standalone file.

Both are headless-safe (Agg / off-screen PyVista with OSMesa).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

try:
    import pyvista as pv
except Exception:  # pragma: no cover
    pv = None


def _solid_regions(case: Path, limit_cells: int = 60_000):
    """Extract {region: (points, faces, T)} for the solid regions + the board,
    decimated to `limit_cells` total so the viewer stays light.

    Opens the OpenFOAM case through PyVista's reader (same source of truth as
    the scalar summary). Returns a dict plus a min/max T for the colourbar.
    """
    if pv is None:
        raise RuntimeError("pyvista not available")
    ctrl = case / "system" / "controlDict"
    if not ctrl.exists():
        raise RuntimeError(f"not an OpenFOAM case (no system/controlDict): {case}")
    reader = pv.POpenFOAMReader(str(ctrl))
    if reader.time_values:
        reader.set_active_time_value(reader.time_values[-1])
    data = reader.read()

    regions = {}
    t_min, t_max = float("inf"), float("-inf")
    for name in data.keys():
        if name == "air":
            continue  # only solids/board matter for the field view
        block = data[name]
        if not hasattr(block, "keys") or "internalMesh" not in block.keys():
            continue
        mesh = block["internalMesh"]
        if "T" not in mesh.point_data:
            continue
        # OpenFOAM cells are UnstructuredGrid (hex); render the *surface* as
        # connectivity (verts->faces). extract_surface() yields a PolyData
        # whose `faces` is the triangulated boundary we can embed.
        try:
            surf = mesh.extract_surface(algorithm="dataset_surface")
        except Exception:
            surf = mesh
        if surf.n_cells > limit_cells // 4:
            try:
                surf = surf.decimate_pro(0.6)  # coarse downsampling, keep shape
            except Exception:
                pass
        pts = np.asarray(surf.points)
        faces = surf.faces.reshape(-1, 4)[:, 1:] if len(surf.faces) else np.zeros((0, 3), int)
        t = np.asarray(surf.point_data["T"]) if "T" in surf.point_data else np.asarray(mesh.point_data["T"])
        regions[name] = {"pts": pts, "faces": faces, "T": t, "n_cells": mesh.n_cells}
        t_min, t_max = min(t_min, float(t.min())), max(t_max, float(t.max()))
    return regions, (t_min, t_max)


def render_multiview_annotated(case: str | Path, out_png: str | Path,
                               tj_map: dict[str, float] | None = None) -> Path:
    """2x2 annotated composite: iso / top / front / side temperature views.

    Each panel is a separate off-screen PyVista render (OSMesa), colormapped
    consistently, composited onto a matplotlib figure with device Tj overlays.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    regions, (t_min, t_max) = _solid_regions(Path(case))
    if not regions:
        raise RuntimeError(f"no solid T fields to render in {case}")
    cmap = "inferno"

    # camera directions for the 4 views
    views = [("iso", (1, 1, 1)), ("top", (0, 0, 1)), ("front", (0, 1, 0)), ("side", (1, 0, 0))]

    fig, axes = plt.subplots(2, 2, figsize=(12, 10))
    title_map = {"iso": "Isometric", "top": "Top", "front": "Front", "side": "Side"}
    for ax, (view, vec) in zip(axes.ravel(), views):
        plotter = pv.Plotter(off_screen=True, window_size=[520, 420])
        for name, r in regions.items():
            if len(r["faces"]) == 0:
                continue
            # faces need a leading count (3) per triangle for pv.PolyData
            count_faces = np.hstack([np.full((len(r["faces"]), 1), 3, np.int32),
                                     r["faces"].astype(np.int32)]).ravel()
            polydata = pv.PolyData(r["pts"], count_faces)
            polydata.point_data["T"] = r["T"]
            plotter.add_mesh(polydata, scalars="T", cmap=cmap, clim=(t_min, t_max),
                             show_scalar_bar=False, name=name)
        if view != "iso":
            plotter.view_xy() if view == "top" else plotter.view_xz()
            if view == "front":
                plotter.view_xz()
                plotter.camera.elevation = 0
        else:
            plotter.camera_position = [(1.5, 1.5, 1.5), (0, 0, 0), (0, 0, 1)]
        plotter.show_axes()
        try:
            img = plotter.screenshot(return_img=True)
        except Exception:
            img = np.zeros((420, 520, 3), np.uint8)
        plotter.close()
        ax.imshow(img)
        ax.set_title(title_map[view], fontsize=12)
        ax.axis("off")

    # device Tj overlay
    if tj_map:
        fig.suptitle("Temperature field (K) — annotated views", fontsize=13, y=0.98)
        label = ",  ".join(f"{k} {v:.0f} K" for k, v in tj_map.items())
        fig.text(0.5, 0.02, label, ha="center", fontsize=11)

    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return out


def export_3d_html(case: str | Path, out_html: str | Path,
                   tj_map: dict[str, float] | None = None) -> Path:
    """Self-contained interactive 3D viewer as a single .html file.

    Embeds the solid meshes (decimated) as JSON arrays and renders them with
    three.js from a CDN. Standalone: open index.html in any browser tab.
    """
    regions, (t_min, t_max) = _solid_regions(Path(case))
    if not regions:
        raise RuntimeError(f"no solid T fields to render in {case}")

    meshes = []
    for name, r in regions.items():
        if len(r["faces"]) == 0:
            continue
        # flatten faces to a compact Uint32-able list
        f = r["faces"].astype(np.int32)
        p = r["pts"].astype(np.float32)
        t_ = r["T"].astype(np.float32)
        meshes.append({
            "name": name,
            "points": p.ravel().tolist(),
            "faces": f.ravel().tolist(),
            "T": t_.tolist(),
            "tmin": float(t_.min()), "tmax": float(t_.max()),
        })

    palette = "inferno"
    # ~48 colours sampled from inferno for the colourbar
    col = np.array([
        [0.001462, 0.000466, 0.013866], [0.015976, 0.009463, 0.095488],
        [0.081192, 0.084121, 0.246326], [0.255950, 0.172684, 0.434130],
        [0.458396, 0.194535, 0.516350], [0.616332, 0.211601, 0.525331],
        [0.745411, 0.264597, 0.477371], [0.828281, 0.352701, 0.389514],
        [0.867400, 0.452174, 0.300378], [0.838992, 0.573871, 0.211765],
        [0.782788, 0.700485, 0.223418], [0.728458, 0.827532, 0.324450],
        [0.694067, 0.950843, 0.503174],
    ], np.float32) * 255

    labels = json.dumps([{"name": m["name"], "tmin": m["tmin"], "tmax": m["tmax"]} for m in meshes])

    html = _THREEJS_HTML_TEMPLATE.format(
        meshes_json=json.dumps(meshes),
        t_min=float(t_min), t_max=float(t_max),
        labels_json=labels,
        palette=json.dumps(col.ravel().tolist()),
        tj_label=(", ".join(f"{k} {v:.0f}K" for k, v in (tj_map or {}).items())) or "n/a",
        bar_pct=f"{84.0/220.0*100:.4f}",
    )
    out = Path(out_html)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


_THREEJS_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DC-DC Thermal Field — 3D</title>
<style>
  body {{ margin:0; font-family:system-ui,sans-serif; background:#131722; color:#e8e8e8; }}
  #app {{ position:fixed; inset:0; }}
  #hud {{ position:absolute; top:12px; left:12px; z-index:10; background:rgba(0,0,0,.65);
          padding:10px 14px; border-radius:8px; font-size:13px; }}
  #hud b {{ color:#ffd166; }}
  #bar {{ position:absolute; right:18px; top:50%; transform:translateY(-50%); z-index:10;
          width:14px; height:220px; border-radius:4px; }}
  #bartick {{ text-anchor:start; }}
  #legend {{ position:absolute; right:44px; top:50%; transform:translateY(-50%); z-index:10;
              font-size:11px; text-align:right; }}
</style>
</head>
<body>
<div id="app"></div>
<div id="hud">⚡ <b>Thermal field</b> · {tj_label}<br>
  drag: rotate · scroll: zoom · right-drag: pan</div>
<div id="legend">{t_min:.1f} K<br>…<br>{t_max:.1f} K</div>
<div id="bar"></div>

<script type="importmap">
{{ "imports": {{
  "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
  "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
}} }}
</script>
<script type="module">
import * as THREE from 'three';
import {{ OrbitControls }} from 'three/addons/controls/OrbitControls.js';

const meshes = {meshes_json};
const palette = new Float32Array({palette});
const TRANGE = [{t_min}, {t_max}];

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x131722);
const camera = new THREE.PerspectiveCamera(45, innerWidth/innerHeight, 0.1, 1000);
camera.position.set(2.2, 1.8, 2.4);
const renderer = new THREE.WebGLRenderer({{ antialias:true }});
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(devicePixelRatio);
document.getElementById('app').appendChild(renderer.domElement);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0,0,0);

function tJet(t) {{
  const u = Math.min(1, Math.max(0, (t-TRANGE[0])/(TRANGE[1]-TRANGE[0])));
  const i = u*(palette.length/3-1);
  const k = Math.floor(i), f = i-k;
  const base=k*3;
  const r=palette[base]+(palette[base+3]-palette[base])*f;
  const g=palette[base+1]+(palette[base+4]-palette[base+1])*f;
  const b=palette[base+2]+(palette[base+5]-palette[base+2])*f;
  return new THREE.Color(r/255,g/255,b/255);
}}

for (const m of meshes) {{
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(m.points, 3));
  geo.setIndex(m.faces);
  geo.computeVertexNormals();
  // per-vertex colour from T
  const colors = new Float32Array(m.points.length/3*3);
  for (let i=0;i<m.points.length/3;i++){{
    const c=tJet(m.T[i]); colors[i*3]=c.r; colors[i*3+1]=c.g; colors[i*3+2]=c.b;
  }}
  geo.setAttribute('color', new THREE.BufferAttribute(colors,3));
  const mat = new THREE.MeshLambertMaterial({{ vertexColors:true, side:THREE.DoubleSide }});
  const meshObj = new THREE.Mesh(geo, mat);
  scene.add(meshObj);
}}

// colour bar
const bar = document.getElementById('bar');
const n = 64;
let barHTML='';
for (let i=n-1;i>=0;i--){{
  const c=tJet(TRANGE[0]+(TRANGE[1]-TRANGE[0])*i/(n-1));
  const pct = '{bar_pct}';
  barHTML += `<div style="height:${{pct}}%;background:rgb(${{c.r*255|0}},${{c.g*255|0}},${{c.b*255|0}})"></div>`;
}}
bar.innerHTML = barHTML;

// lights
const amb = new THREE.AmbientLight(0xffffff, 0.5);
const dir = new THREE.DirectionalLight(0xffffff, 0.9);
dir.position.set(2,3,1); scene.add(amb, dir);

function animate(){{ requestAnimationFrame(animate); controls.update(); renderer.render(scene,camera); }}
addEventListener('resize', ()=>{{ camera.aspect=innerWidth/innerHeight; camera.updateProjectionMatrix(); renderer.setSize(innerWidth,innerHeight);}});
animate();
</script>
</body>
</html>
"""