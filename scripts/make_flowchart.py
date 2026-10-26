"""Draw the pipeline flowchart for the report.

Run from the repo root:  uv run python scripts/make_flowchart.py
Writes outputs/pipeline_flowchart.png (300 dpi).
"""
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

C = {"in": ("#e8eef6", "#4a6fa5"), "pre": ("#eef4e4", "#6b8e23"), "cl": ("#f6f0dc", "#b8860b"),
     "pm": ("#f3e6ea", "#a0526d"), "out": ("#ececec", "#555555"), "fin": ("#93a532", "#5c6b1e")}
W, H = 130, 124
fig, ax = plt.subplots(figsize=(11, 11 * H / W))
ax.set_xlim(0, W); ax.set_ylim(0, H); ax.axis("off")
fig.canvas.draw(); R = fig.canvas.get_renderer()

def box(x, y, text, kind, fs=9.5, bold=False, minw=0, round_=0.9):
    t = ax.text(x, y, text, ha="center", va="center", fontsize=fs, zorder=3, linespacing=1.35,
                color="white" if kind == "fin" else "#1a1a1a", fontweight="bold" if bold else "normal")
    bb = t.get_window_extent(R).transformed(ax.transData.inverted())
    w, h = max(bb.width + 3.2, minw), bb.height + 2.6
    f, e = C[kind]
    ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h, boxstyle=f"round,pad=0,rounding_size={round_}",
                                fc=f, ec=e, lw=1.1, zorder=2))
    return dict(x=x, y=y, w=w, h=h)

T = lambda b, dx=0: (b["x"] + dx, b["y"] + b["h"] / 2)
B = lambda b, dx=0: (b["x"] + dx, b["y"] - b["h"] / 2)
L = lambda b: (b["x"] - b["w"] / 2, b["y"])
Rt = lambda b: (b["x"] + b["w"] / 2, b["y"])

def arrow(p, q, dashed=False, rad=0.0):
    ax.annotate("", xy=q, xytext=p, zorder=1, arrowprops=dict(
        arrowstyle="-|>", color="#444444", lw=1.1, shrinkA=1.5, shrinkB=1.5, mutation_scale=11,
        ls=(0, (3, 2)) if dashed else "-", connectionstyle=f"arc3,rad={rad}"))

for y, t in [(116, "Inputs"), (96, "1  Pre-\n    processing"), (69, "2  Flood\n    detection"),
             (47, "3  Post-masks"), (34, "Product"), (10, "4  Outputs")]:
    ax.text(1, y, t, ha="left", va="center", fontsize=10.5, fontweight="bold", color="#333333")

# Inputs
s1e = box(40, 116, "Sentinel-1 GRD (VV, VH)\nevent pass, 14 Feb 2023", "in", minw=28)
s1h = box(72, 116, "Sentinel-1 GRD (VV, VH)\n1-year baseline, same track", "in", minw=28)
anc = box(108, 116, "FABDEM bare-earth DEM\nESA WorldCover, JRC water", "in", minw=28)
# Preprocessing
trk = box(56, 105, "Select one orbit track (covers AOI, nearest to flood peak)", "pre")
lee = box(40, 95, "Lee speckle filter\n(3×3, each scene)", "pre", minw=26)
tfl = box(74, 95, "Terrain flattening to γ⁰\n+ layover / shadow mask", "pre", minw=26)
med = box(56, 85, "Per-pixel median: event composite and baseline composite", "pre")
# Detection
fea = box(40, 73, "Features per pixel\nWI = VV + VH (dB)\nΔWI = WI$_{baseline}$ − WI$_{event}$", "cl", minw=30)
gmm = box(82, 73, "2-D Gaussian mixture model\n(4 components, unsupervised)", "cl", minw=30)
km = box(117, 73, "k-means\n(comparison)", "cl", fs=9)
pck = box(40, 60, "Flood component:\ndark and newly darker", "cl", minw=30)
# Post-masks
pw = box(36, 47, "Remove permanent water\nand sea (WorldCover, JRC)", "pm", minw=28)
ter = box(72, 47, "Remove steep / high ground\n(FABDEM slope, elevation)", "pm", minw=28)
mmu = box(108, 47, "Minimum mapping unit\n+ morphological opening", "pm", minw=26)
# Product
fin = box(72, 34, "Final flood extent\nraster + polygons", "fin", fs=11, bold=True, minw=30, round_=2.5)
# Outputs
ref = box(24, 22, "Dragonfly flood extent\n14 Feb 2023 (reference)", "in", fs=9)
vec = box(116, 22, "LINZ building outlines\nOSM road network", "in", fs=9)
acc = box(44, 10, "Accuracy assessment\nprecision, recall, F1, IoU, kappa", "out", fs=9, minw=30)
s2 = box(76, 10, "Sentinel-2 U-Net\noptical cross-check", "out", fs=9, minw=22)
exp = box(106, 10, "Exposure\nbuildings, roads, detours", "out", fs=9, minw=26)

# Arrows
arrow(B(s1e), T(trk, -10)); arrow(B(s1h), T(trk, 10))
arrow(B(trk, -10), T(lee)); arrow(B(trk, 10), T(tfl))
arrow(Rt(lee), L(tfl))
arrow(B(anc), Rt(tfl), rad=-0.3)
arrow(B(lee), T(med, -10)); arrow(B(tfl), T(med, 10))
arrow(B(med, -16), T(fea))
arrow(Rt(fea), L(gmm))
arrow(Rt(gmm), L(km), dashed=True)
arrow(B(gmm), Rt(pck), rad=-0.25)
arrow(B(pck), T(pw, 4))
arrow(Rt(pw), L(ter)); arrow(Rt(ter), L(mmu))
arrow(B(mmu), Rt(fin), rad=-0.3)
arrow(B(fin, -8), T(acc, 4), rad=0.1)
arrow(B(fin), T(s2, -4), dashed=True)
arrow(B(fin, 8), T(exp, -4), rad=-0.1)
arrow(B(ref), T(acc, -10), rad=0.15); arrow(B(vec), T(exp, 8), rad=-0.15)

from pathlib import Path
out = Path(__file__).resolve().parents[1] / "outputs" / "pipeline_flowchart.png"
fig.savefig(out, dpi=300, bbox_inches="tight", facecolor="white")
print("Saved", out)
