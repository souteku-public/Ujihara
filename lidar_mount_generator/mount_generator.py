#!/usr/bin/env python3
"""
LiDAR Mount Plate Generator
============================
Reads a STP/STEP file (e.g. Falcon K2), detects bottom-face mounting holes,
and generates a 3D-printable tripod mount plate (STL or STEP).

Falls back to a parametric mode when automatic hole detection yields
insufficient results.

Usage
-----
  # Auto mode (STP → STL)
  python mount_generator.py -i FalconK2.stp -o mount.stl

  # Parametric: square 4-hole pattern, pitch=38.5mm, M3 screws
  python mount_generator.py -p --pitch 38.5 --screw-dia 3.0 -o mount.stl

  # Parametric: explicit positions
  python mount_generator.py -p --holes "-19,0 19,0" --screw-dia 3.0 -o mount.stl

  # Override design parameters
  python mount_generator.py -i FalconK2.stp --plate-thickness 8 --boss-height 5 -o mount.stl
"""

import sys
import math
import textwrap
import logging
import argparse
from dataclasses import dataclass
from typing import List, Tuple, Optional

import cadquery as cq
from cadquery import exporters

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# ─── Design Parameters ────────────────────────────────────────────────────────

@dataclass
class Config:
    # Base plate
    plate_thickness: float = 10.0   # mm  ← change via --plate-thickness
    plate_margin:    float = 10.0   # mm  extra border around hole array
    plate_fillet:    float = 3.0    # mm  outer corner fillet

    # Standoff bosses (spacers under LiDAR)
    boss_height:     float = 3.0    # mm  ← 放熱ギャップ
    boss_od_factor:  float = 2.8    # boss OD = screw_dia × factor
    boss_od_min_wall: float = 3.0   # mm  minimum boss wall thickness

    # Screw clearance holes
    screw_clearance: float = 0.4    # mm  added to each screw hole diameter

    # Tripod screw (1/4-20 UNC)
    tripod_hole_dia: float = 6.5    # mm  through-hole for 1/4-20 bolt
    hex_nut_af:      float = 11.5   # mm  1/4-20 hex nut across-flats
    hex_nut_depth:   float = 5.0    # mm  pocket depth from bottom face

    # Cosmetic
    chamfer_top:     float = 0.5    # mm  top-face edge chamfer


@dataclass
class HoleInfo:
    x: float
    y: float
    diameter: float  # mm


# ─── STP Hole Detection ───────────────────────────────────────────────────────

def load_and_detect_holes(
    stp_path: str,
    z_ratio: float = 0.25,     # search within bottom 25 % of bounding height
    dia_min: float = 1.5,       # mm  smallest expected mounting screw
    dia_max: float = 12.0,      # mm  largest expected mounting screw
) -> Tuple[List[HoleInfo], dict]:
    """
    Parse a STP file with OpenCASCADE (via CadQuery/OCP) and return
    all circular edges found near the bottom face.

    The bottom face is defined as edges whose Z-centre ≤ z_min + height × z_ratio.
    Duplicates (same XY centre and radius) are suppressed.
    """
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        from OCP.GeomAbs import GeomAbs_Circle
        from OCP.TopAbs import TopAbs_EDGE
        from OCP.TopExp import TopExp_Explorer
    except ImportError as exc:
        raise RuntimeError(
            "OCP (OpenCASCADE) is required for STP parsing. "
            "It ships with CadQuery – install with:  pip install cadquery"
        ) from exc

    log.info(f"Loading STP: {stp_path}")
    shape = cq.importers.importStep(stp_path)

    bb = shape.val().BoundingBox()
    z_bottom = bb.zmin
    height   = bb.zmax - bb.zmin
    z_thresh = z_bottom + height * z_ratio

    log.info(
        f"BBox  X[{bb.xmin:.2f} – {bb.xmax:.2f}]  "
        f"Y[{bb.ymin:.2f} – {bb.ymax:.2f}]  "
        f"Z[{bb.zmin:.2f} – {bb.zmax:.2f}]  "
        f"(search Z ≤ {z_thresh:.2f})"
    )

    seen: dict = {}
    explorer = TopExp_Explorer(shape.val().wrapped, TopAbs_EDGE)
    while explorer.More():
        try:
            ada = BRepAdaptor_Curve(explorer.Current())
            if ada.GetType() == GeomAbs_Circle:
                circ = ada.Circle()
                loc  = circ.Location()
                cx, cy, cz = loc.X(), loc.Y(), loc.Z()
                dia = circ.Radius() * 2.0
                if cz <= z_thresh and dia_min <= dia <= dia_max:
                    key = (round(cx, 1), round(cy, 1), round(dia, 1))
                    if key not in seen:
                        seen[key] = True
                        log.info(
                            f"  Hole found: xy=({cx:+.2f}, {cy:+.2f})  "
                            f"z={cz:.2f}  dia={dia:.2f} mm"
                        )
        except Exception:
            pass
        explorer.Next()

    holes  = [HoleInfo(k[0], k[1], k[2]) for k in seen]
    bb_dict = dict(
        xmin=bb.xmin, xmax=bb.xmax,
        ymin=bb.ymin, ymax=bb.ymax,
        zmin=bb.zmin, zmax=bb.zmax,
    )
    log.info(f"Total holes detected: {len(holes)}")
    return holes, bb_dict


# ─── Geometry Helpers ─────────────────────────────────────────────────────────

def _boss_od(screw_dia: float, factor: float, min_wall: float) -> float:
    """Boss outer diameter: larger of factor×dia or dia + 2×wall."""
    return max(screw_dia * factor, screw_dia + 2.0 * min_wall)


def _hex_circumscribed(across_flats: float) -> float:
    """
    Convert hex nut across-flats (AF) to circumscribed (vertex-to-vertex) diameter.
    CadQuery polygon() expects the circumscribed diameter.
    Formula: D_circ = AF / cos(30°)
    """
    return across_flats / math.cos(math.radians(30.0))


# ─── Plate Builder ────────────────────────────────────────────────────────────

def build_mount_plate(holes: List[HoleInfo], cfg: Config) -> cq.Workplane:
    """
    Build the mount plate solid in CadQuery.

    Coordinate system:
      Z = 0              → bottom face of plate (tripod-side)
      Z = plate_thickness → top face of plate
      Z = plate_thickness + boss_height → top of bosses (LiDAR rests here)
    """

    # ── 1. Centre holes at their centroid ──────────────────────────────────
    if holes:
        cx_avg = sum(h.x for h in holes) / len(holes)
        cy_avg = sum(h.y for h in holes) / len(holes)
        holes = [HoleInfo(h.x - cx_avg, h.y - cy_avg, h.diameter) for h in holes]

    # ── 2. Plate footprint ─────────────────────────────────────────────────
    if holes:
        xs = [h.x for h in holes]
        ys = [h.y for h in holes]
        px0 = min(xs) - cfg.plate_margin
        py0 = min(ys) - cfg.plate_margin
        px1 = max(xs) + cfg.plate_margin
        py1 = max(ys) + cfg.plate_margin
    else:
        px0, py0, px1, py1 = -40.0, -40.0, 40.0, 40.0

    pw  = px1 - px0
    ph  = py1 - py0
    pcx = (px0 + px1) / 2.0   # ≈ 0 after centroid normalization
    pcy = (py0 + py1) / 2.0

    log.info(f"Plate footprint: {pw:.1f} × {ph:.1f} mm  centre=({pcx:.1f}, {pcy:.1f})")

    # ── 3. Base plate (Z: 0 → plate_thickness) ────────────────────────────
    # centered=(True,True,False): XY centred, Z starts at workplane (Z=0)
    plate = (
        cq.Workplane("XY")
        .center(pcx, pcy)
        .box(pw, ph, cfg.plate_thickness, centered=(True, True, False))
    )

    # ── 4. Standoff bosses ─────────────────────────────────────────────────
    # Solid annular cylinders on the plate top face; translated to Z = plate_thickness
    for h in holes:
        od = _boss_od(h.diameter, cfg.boss_od_factor, cfg.boss_od_min_wall)
        boss = (
            cq.Workplane("XY")
            .center(h.x, h.y)
            .circle(od / 2.0)
            .extrude(cfg.boss_height)
            .translate((0.0, 0.0, cfg.plate_thickness))
        )
        plate = plate.union(boss)

    # ── 5. Screw clearance holes (through boss + plate) ───────────────────
    # Cut from top-of-boss down through bottom of plate (+1 mm for clean boolean)
    for h in holes:
        clr_r = (h.diameter + cfg.screw_clearance) / 2.0
        total = cfg.plate_thickness + cfg.boss_height + 1.0
        cutter = (
            cq.Workplane("XY")
            .center(h.x, h.y)
            .circle(clr_r)
            .extrude(total)
        )
        plate = plate.cut(cutter)

    # ── 6. Tripod screw through-hole (1/4-20) ─────────────────────────────
    tripod_cutter = (
        cq.Workplane("XY")
        .center(pcx, pcy)
        .circle(cfg.tripod_hole_dia / 2.0)
        .extrude(cfg.plate_thickness + 1.0)
    )
    plate = plate.cut(tripod_cutter)

    # ── 7. Hex-nut capture pocket (from bottom face) ──────────────────────
    # Oriented so the nut is captive; CadQuery polygon uses circumscribed dia
    hex_cutter = (
        cq.Workplane("XY")
        .center(pcx, pcy)
        .polygon(6, _hex_circumscribed(cfg.hex_nut_af))
        .extrude(cfg.hex_nut_depth)
    )
    plate = plate.cut(hex_cutter)

    # ── 8. Cosmetic fillets & chamfers ─────────────────────────────────────
    try:
        # Round outer vertical edges
        plate = plate.edges("|Z").fillet(cfg.plate_fillet)
    except Exception as e:
        log.debug(f"Corner fillet skipped: {e}")

    try:
        # Light chamfer on top face perimeter
        plate = plate.faces(">Z").edges().chamfer(cfg.chamfer_top)
    except Exception as e:
        log.debug(f"Top chamfer skipped: {e}")

    return plate


# ─── Export ───────────────────────────────────────────────────────────────────

def export_model(model: cq.Workplane, path: str) -> None:
    """Export to STL or STEP based on file extension."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext == "stl":
        exporters.export(model, path, exporters.ExportTypes.STL)
    elif ext in ("step", "stp"):
        exporters.export(model, path, exporters.ExportTypes.STEP)
    else:
        raise ValueError(
            f"Unknown extension '.{ext}'.  Use .stl or .step"
        )
    log.info(f"Saved → {path}")


# ─── Interactive / CLI Modes ──────────────────────────────────────────────────

def _ask_holes_interactively(cfg: Config) -> List[HoleInfo]:
    """Collect hole positions from stdin when no CLI args are given."""
    print("\n── Parametric Mode ──────────────────────────────────────────")
    print("Enter mounting hole positions relative to LiDAR centre (mm).")
    print()
    raw_dia = input("Screw diameter mm [3.0 = M3, 4.0 = M4]: ").strip()
    screw_dia = float(raw_dia) if raw_dia else 3.0
    print("Enter each hole as  x,y  (blank line when done, need ≥ 2):")
    holes: List[HoleInfo] = []
    while True:
        raw = input(f"  hole {len(holes)+1}: ").strip()
        if not raw:
            if len(holes) >= 2:
                break
            print("  (need at least 2 holes – keep going)")
            continue
        try:
            x_str, y_str = raw.split(",")
            holes.append(HoleInfo(float(x_str), float(y_str), screw_dia))
        except ValueError:
            print("  Format: x,y  e.g.  -20,-20")
    return holes


def run_auto(stp_path: str, output: str, cfg: Config) -> None:
    """Auto mode: detect holes from STP, build plate, export."""
    try:
        holes, _ = load_and_detect_holes(stp_path)
    except Exception as exc:
        log.error(f"STP parsing error: {exc}")
        holes = []

    if len(holes) < 2:
        log.warning(
            f"Auto-detection found {len(holes)} hole(s) – "
            "switching to parametric mode."
        )
        holes = _ask_holes_interactively(cfg)

    model = build_mount_plate(holes, cfg)
    export_model(model, output)


def run_parametric(
    output: str,
    cfg: Config,
    pitch: Optional[float],
    screw_dia: float,
    holes_str: Optional[str],
) -> None:
    """Parametric mode: build plate from explicit hole data."""
    if holes_str:
        # Parse "x1,y1 x2,y2 ..."
        holes = []
        for token in holes_str.split():
            x_s, y_s = token.split(",")
            holes.append(HoleInfo(float(x_s), float(y_s), screw_dia))
        log.info(f"Using {len(holes)} hole(s) from --holes argument")
    elif pitch is not None:
        half = pitch / 2.0
        holes = [
            HoleInfo(-half, -half, screw_dia),
            HoleInfo( half, -half, screw_dia),
            HoleInfo( half,  half, screw_dia),
            HoleInfo(-half,  half, screw_dia),
        ]
        log.info(
            f"Square 4-hole pattern  pitch={pitch} mm  screw_dia={screw_dia} mm"
        )
    else:
        holes = _ask_holes_interactively(cfg)

    model = build_mount_plate(holes, cfg)
    export_model(model, output)


# ─── CLI Argument Parser ──────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mount_generator",
        description="LiDAR Mount Plate Generator – generates a 3D-printable "
                    "tripod adapter from a LiDAR STP file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples
            --------
            # Auto-detect from STP, output STL
            python mount_generator.py -i FalconK2.stp -o mount.stl

            # Square 4-hole, 38.5 mm pitch, M3 screws, output STEP
            python mount_generator.py -p --pitch 38.5 --screw-dia 3.0 -o mount.step

            # Two-hole pattern with exact positions
            python mount_generator.py -p --holes "-19.25,0 19.25,0" --screw-dia 3.0 -o mount.stl

            # Thinner plate, taller bosses
            python mount_generator.py -i FalconK2.stp --plate-thickness 8 --boss-height 5 -o mount.stl
        """),
    )

    p.add_argument("-i", "--input",  metavar="STP",  help="Input STP/STEP file path")
    p.add_argument("-o", "--output", metavar="FILE", default="lidar_mount.stl",
                   help="Output file (.stl or .step)  [default: lidar_mount.stl]")
    p.add_argument("-p", "--parametric", action="store_true",
                   help="Parametric mode – skip STP parsing entirely")

    pg = p.add_argument_group("parametric options (used with -p)")
    pg.add_argument("--pitch",     type=float, metavar="MM",
                    help="Pitch for square 4-hole pattern (mm)")
    pg.add_argument("--holes",     metavar='"x1,y1 x2,y2 ..."',
                    help="Explicit hole positions as quoted string")
    pg.add_argument("--screw-dia", type=float, default=3.0, metavar="MM",
                    help="Screw diameter mm  [default: 3.0 = M3]")

    dg = p.add_argument_group("design overrides")
    dg.add_argument("--plate-thickness", type=float, metavar="MM",
                    help=f"Plate thickness mm  [default: {Config.plate_thickness}]")
    dg.add_argument("--plate-margin",    type=float, metavar="MM",
                    help=f"Border margin mm    [default: {Config.plate_margin}]")
    dg.add_argument("--boss-height",     type=float, metavar="MM",
                    help=f"Boss/standoff height mm  [default: {Config.boss_height}]")
    dg.add_argument("--tripod-hole-dia", type=float, metavar="MM",
                    help=f"Tripod clearance hole dia mm  [default: {Config.tripod_hole_dia}]")
    return p


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main() -> None:
    p    = _build_parser()
    args = p.parse_args()

    # Apply design overrides
    cfg = Config()
    if args.plate_thickness: cfg.plate_thickness = args.plate_thickness
    if args.plate_margin:    cfg.plate_margin    = args.plate_margin
    if args.boss_height:     cfg.boss_height     = args.boss_height
    if args.tripod_hole_dia: cfg.tripod_hole_dia = args.tripod_hole_dia

    if args.parametric:
        run_parametric(args.output, cfg, args.pitch, args.screw_dia, args.holes)

    elif args.input:
        run_auto(args.input, args.output, cfg)

    else:
        # Interactive wizard (no arguments given)
        print("=" * 50)
        print("  LiDAR Mount Plate Generator")
        print("=" * 50)
        print()
        print("  [1] Auto mode    – parse STP file, detect holes")
        print("  [2] Parametric   – enter screw positions manually")
        print()
        choice = input("Select mode [1/2]: ").strip()
        if choice == "1":
            stp = input("STP file path: ").strip()
            out = input("Output path [lidar_mount.stl]: ").strip() or "lidar_mount.stl"
            run_auto(stp, out, cfg)
        else:
            out = input("Output path [lidar_mount.stl]: ").strip() or "lidar_mount.stl"
            run_parametric(out, cfg, None, 3.0, None)


if __name__ == "__main__":
    main()
