#!/usr/bin/env python3
from __future__ import annotations

import math
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Arc, Circle, FancyArrowPatch, FancyBboxPatch, Polygon, Rectangle


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIRS = [ROOT / "docs" / "assets", ROOT / "paper" / "date" / "figures"]

INK = "#111827"
MUTED = "#4b5563"
LINE = "#6b7280"
ACCENT = "#1f4e79"
SOFT = "#f6f8fb"
WHITE = "#ffffff"


def main() -> None:
    for out_dir in OUTPUT_DIRS:
        out_dir.mkdir(parents=True, exist_ok=True)
    figures = {
        "analogdiag_architecture.png": draw_architecture(),
        "analogdiag_schema_state.png": draw_schema_state(),
        "analogdiag_optimization_loop.png": draw_optimization_loop(),
        "analogdiag_diagnosis_loop.png": draw_diagnosis_loop(),
    }
    for name, fig in figures.items():
        for out_dir in OUTPUT_DIRS:
            fig.savefig(out_dir / name, dpi=260, bbox_inches="tight")
        plt.close(fig)


def draw_architecture() -> plt.Figure:
    fig, ax = base_fig(10.2, 5.0)

    # Outer agent loop.
    loop = FancyBboxPatch(
        (0.38, 0.38),
        9.44,
        4.22,
        boxstyle="round,pad=0.10,rounding_size=0.20",
        linewidth=1.2,
        edgecolor=LINE,
        facecolor="none",
        linestyle=(0, (5, 4)),
    )
    ax.add_patch(loop)

    draw_person(ax, 1.05, 3.38, "Human\nreview")
    draw_person(ax, 1.05, 1.62, "LLM\nplanner")

    flow_node(ax, 3.20, 4.02, 2.12, 0.44, "LangGraph\nagent loop")
    draw_file(
        ax,
        3.55,
        1.70,
        1.58,
        1.92,
        "ASIR/YAML\nstate",
        [r"$G,\theta,\tau$", r"$\mathcal{D},\mathcal{E}$", "write policy"],
    )

    cycle_center = (7.38, 3.08)
    draw_cycle(ax, cycle_center, 0.92, ["gm/ID", "NSGA-II", "ngspice", "PP\nrepair"])
    ax.text(cycle_center[0], cycle_center[1], "execute", ha="center", va="center", fontsize=7.0, color=INK)

    panel(ax, 5.02, 0.62, 1.34, 0.72, "Causal graph", ["typed failures", "ranked actions"])
    panel(ax, 6.62, 0.62, 1.42, 0.72, "Action opt.", ["combo_coarse_fine", r"$C^\star,\Delta J$"])
    panel(ax, 8.30, 0.62, 1.24, 0.72, "Apply gate", ["admissibility", "skip notes"])

    arrow(ax, 1.66, 3.42, 3.16, 4.12)
    arrow(ax, 3.45, 3.28, 1.78, 3.05)
    arrow(ax, 1.66, 1.77, 3.36, 2.30)
    arrow(ax, 3.45, 2.05, 1.80, 1.40)
    arrow(ax, 4.82, 4.02, 5.98, 3.55)
    arrow(ax, 5.25, 2.86, 6.12, 2.94)
    arrow(ax, 6.55, 2.28, 5.16, 2.10, rad=-0.12)
    arrow(ax, 6.98, 2.18, 5.78, 1.36, rad=-0.06)
    arrow(ax, 6.36, 0.98, 6.58, 0.98)
    arrow(ax, 8.04, 0.98, 8.26, 0.98)
    return fig


def draw_schema_state() -> plt.Figure:
    fig, ax = base_fig(9.8, 3.7)
    draw_file(ax, 0.62, 0.95, 1.45, 1.95, "ASIR/YAML\nstate", ["roles", "knobs", "evidence"])

    items = [
        (2.45, 2.55, "Topology roles", r"$G$: devices, stages, paths"),
        (6.05, 2.55, "Writable knobs", r"$\theta$: gm/ID, W/L, bias, $C_c$"),
        (2.45, 1.45, "Targets", r"$\tau,J_{\rm spec}$: priority + loss"),
        (6.05, 1.45, "Diagnostics", r"$\mathcal{D}$: typed causal edges"),
        (2.45, 0.35, "Evidence", r"$\mathcal{E}$: ngspice + probes"),
        (6.05, 0.35, "Write policy", "bounds, symmetry, admissibility"),
    ]
    for x, y, heading, body in items:
        small_panel(ax, x, y, 2.85, 0.78, heading, body)
    return fig


def draw_optimization_loop() -> plt.Figure:
    fig, ax = base_fig(9.6, 4.9)
    center = (4.72, 2.38)
    radius = 1.68
    ax.add_patch(
        Circle(
            center,
            radius,
            edgecolor="#e2e8f0",
            facecolor="none",
            linewidth=0.55,
            linestyle=(0, (3, 5)),
            zorder=0,
        )
    )
    nodes = [
        (90, "schema\nprofile"),
        (30, "gm/ID\npriors"),
        (-30, "NSGA-II\nbounded"),
        (-90, "ngspice\nmeasure"),
        (-150, "archive\nbest"),
        (150, "PP repair\nfallback"),
    ]
    for (a1, _), (a2, _) in zip(nodes, nodes[1:] + nodes[:1]):
        circular_arrow(ax, center, radius, a1, a2, node_radius=0.43)

    for angle, label in nodes:
        rad = math.radians(angle)
        circle_node(ax, center[0] + radius * math.cos(rad), center[1] + radius * math.sin(rad), 0.43, label)

    panel(
        ax,
        3.78,
        1.64,
        1.88,
        1.02,
        "Authority",
        [r"$J_{\rm spec}=\sum w_kv_k^2$", "simulator result wins", "schema gates remain hard"],
    )
    return fig


def draw_diagnosis_loop() -> plt.Figure:
    fig, ax = base_fig(10.2, 3.65)

    stage_w = 1.18
    stage_h = 0.62
    y = 2.55
    xs = [0.42, 2.02, 3.62, 5.22, 6.82, 8.42]
    labels = [
        "failed\nspecs",
        "typed causal\ngraph",
        "local SPICE\nprobes",
        "combo_coarse\n_fine opt.",
        "formal apply\ngate",
        "schema\ncommand",
    ]
    for i, (x, label) in enumerate(zip(xs, labels)):
        flow_node(ax, x, y, stage_w, stage_h, label)
        if i < len(xs) - 1:
            arrow(ax, x + stage_w + 0.08, y + stage_h / 2, xs[i + 1] - 0.08, y + stage_h / 2)

    cards = [
        (
            0.58,
            "intervention model",
            [r"$A_{k,j}=v_k(s\oplus\delta a_j)-v_k(s)$", "small ngspice perturbations", "not a hidden global sweep"],
        ),
        (
            3.72,
            "action optimizer",
            ["bounded compatible sets", r"$C^\star=\arg\min J_{\rm act}(C)$", "duplicate writes rejected"],
        ),
        (
            6.86,
            "apply rule",
            ["optimizer_selected or", r"trusted $\Delta J<0$", r"guarded $\Rightarrow$ evidence_gate"],
        ),
    ]
    for x, heading, lines in cards:
        evidence_card(ax, x, 0.68, 2.72, 1.10, heading, lines)
    return fig


def base_fig(width: float, height: float) -> tuple[plt.Figure, plt.Axes]:
    fig, ax = plt.subplots(figsize=(width, height))
    ax.set_xlim(0, width)
    ax.set_ylim(0, height)
    ax.axis("off")
    return fig, ax


def title(ax: plt.Axes, text: str) -> None:
    ax.text(0.15, ax.get_ylim()[1] - 0.42, text, fontsize=11.6, weight="bold", color=INK)


def node(ax: plt.Axes, x: float, y: float, w: float, h: float, text: str) -> None:
    patch = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.025,rounding_size=0.035",
        linewidth=1.0,
        edgecolor=ACCENT,
        facecolor=WHITE,
    )
    ax.add_patch(patch)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=7.4, color=INK, linespacing=1.05)


def flow_node(ax: plt.Axes, x: float, y: float, w: float, h: float, text: str) -> None:
    patch = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.020,rounding_size=0.045",
        linewidth=1.05,
        edgecolor=ACCENT,
        facecolor=WHITE,
    )
    ax.add_patch(patch)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=7.15, color=INK, linespacing=1.03)


def evidence_card(ax: plt.Axes, x: float, y: float, w: float, h: float, heading: str, lines: list[str]) -> None:
    patch = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.035,rounding_size=0.045",
        linewidth=0.9,
        edgecolor="#9fb6cf",
        facecolor="#f5f8fc",
    )
    ax.add_patch(patch)
    ax.text(x + w / 2, y + h - 0.23, heading, ha="center", va="top", fontsize=7.8, weight="bold", color=ACCENT)
    ax.text(x + w / 2, y + 0.46, "\n".join(lines), ha="center", va="center", fontsize=6.85, color=INK, linespacing=1.17)


def circle_node(ax: plt.Axes, x: float, y: float, r: float, text: str) -> None:
    ax.add_patch(Circle((x, y), r, edgecolor=ACCENT, facecolor=WHITE, linewidth=1.0, zorder=3))
    ax.text(x, y, text, ha="center", va="center", fontsize=6.9, color=INK, linespacing=1.03, zorder=4)


def panel(ax: plt.Axes, x: float, y: float, w: float, h: float, heading: str, lines: list[str]) -> None:
    patch = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.045,rounding_size=0.04",
        linewidth=0.9,
        edgecolor=LINE,
        facecolor=SOFT,
    )
    ax.add_patch(patch)
    ax.text(x + 0.14, y + h - 0.18, heading, fontsize=8.2, weight="bold", color=ACCENT, va="top")
    ax.text(x + 0.14, y + h - 0.43, "\n".join(lines), fontsize=7.1, color=INK, va="top", linespacing=1.15)


def small_panel(ax: plt.Axes, x: float, y: float, w: float, h: float, heading: str, body: str) -> None:
    panel(ax, x, y, w, h, heading, [body])


def draw_file(ax: plt.Axes, x: float, y: float, w: float, h: float, heading: str, lines: list[str]) -> None:
    ax.add_patch(Rectangle((x, y), w, h, linewidth=1.15, edgecolor=ACCENT, facecolor=WHITE))
    fold = Polygon(
        [(x + w * 0.74, y + h), (x + w, y + h), (x + w, y + h * 0.78)],
        closed=True,
        edgecolor=ACCENT,
        facecolor=SOFT,
        linewidth=1.0,
    )
    ax.add_patch(fold)
    ax.plot([x + w * 0.74, x + w * 0.74], [y + h, y + h * 0.78], color=ACCENT, linewidth=1.0)
    ax.text(x + 0.16, y + h - 0.34, heading, fontsize=8.6, weight="bold", color=INK, va="top", linespacing=1.05)
    ax.text(x + 0.16, y + 0.34, "\n".join(lines), fontsize=7.2, color=MUTED, va="bottom", linespacing=1.12)


def draw_person(ax: plt.Axes, x: float, y: float, label: str) -> None:
    ax.add_patch(Circle((x, y + 0.24), 0.16, edgecolor=ACCENT, facecolor=WHITE, linewidth=1.0))
    ax.add_patch(Arc((x, y - 0.03), 0.70, 0.62, theta1=22, theta2=158, color=ACCENT, linewidth=1.0))
    ax.plot([x - 0.28, x + 0.28], [y - 0.10, y - 0.10], color=ACCENT, linewidth=1.0)
    ax.text(x, y - 0.48, label, ha="center", va="top", fontsize=7.7, color=INK, linespacing=1.05)


def draw_cycle(ax: plt.Axes, center: tuple[float, float], radius: float, labels: list[str]) -> None:
    cx, cy = center
    ax.add_patch(
        Circle(
            center,
            radius,
            edgecolor="#e2e8f0",
            facecolor="none",
            linewidth=0.55,
            linestyle=(0, (3, 5)),
            zorder=0,
        )
    )
    angles = [90, 0, -90, 180]
    arrow_angles = [90, 0, -90, -180, -270]
    for a1, a2 in zip(arrow_angles, arrow_angles[1:]):
        circular_arrow(ax, center, radius, a1, a2, node_radius=0.34, arrow_size=0.055)
    for angle, label in zip(angles, labels):
        rad = math.radians(angle)
        x = cx + radius * math.cos(rad)
        y = cy + radius * math.sin(rad)
        circle_node(ax, x, y, 0.34, label)


def circular_arrow(
    ax: plt.Axes,
    center: tuple[float, float],
    radius: float,
    start_deg: float,
    end_deg: float,
    *,
    node_radius: float,
    arrow_size: float = 0.070,
    color: str = LINE,
) -> None:
    """Draw a clockwise circular-flow arrow trimmed around round nodes."""
    cx, cy = center
    start = float(start_deg)
    end = float(end_deg)
    while end >= start:
        end -= 360.0
    trim = math.degrees(math.asin(min(0.92, node_radius / max(radius, 1e-6)))) + 2.0
    start -= trim
    end += trim
    steps = max(16, int(abs(start - end) / 3.0))
    angles = [start + (end - start) * i / steps for i in range(steps + 1)]
    xs = [cx + radius * math.cos(math.radians(a)) for a in angles]
    ys = [cy + radius * math.sin(math.radians(a)) for a in angles]
    ax.plot(xs, ys, color=color, linewidth=0.95, solid_capstyle="round", zorder=1)

    theta = math.radians(end)
    tip = (cx + radius * math.cos(theta), cy + radius * math.sin(theta))
    direction = (math.sin(theta), -math.cos(theta))
    normal = (-direction[1], direction[0])
    length = arrow_size * 1.55
    width = arrow_size * 0.86
    base = (tip[0] - direction[0] * length, tip[1] - direction[1] * length)
    head = Polygon(
        [
            tip,
            (base[0] + normal[0] * width, base[1] + normal[1] * width),
            (base[0] - normal[0] * width, base[1] - normal[1] * width),
        ],
        closed=True,
        edgecolor=color,
        facecolor=color,
        linewidth=0.0,
        zorder=2,
    )
    ax.add_patch(head)


def arrow(ax: plt.Axes, x1: float, y1: float, x2: float, y2: float, rad: float = 0.0) -> None:
    ax.add_patch(
        FancyArrowPatch(
            (x1, y1),
            (x2, y2),
            arrowstyle="-|>",
            mutation_scale=8.5,
            linewidth=0.9,
            color=LINE,
            connectionstyle=f"arc3,rad={rad}",
        )
    )


def arc_arrow(ax: plt.Axes, center: tuple[float, float], radius: float, start: float, end: float) -> None:
    cx, cy = center
    theta = math.radians(end)
    x1 = cx + radius * math.cos(math.radians(start))
    y1 = cy + radius * math.sin(math.radians(start))
    x2 = cx + radius * math.cos(theta)
    y2 = cy + radius * math.sin(theta)
    arrow(ax, x1, y1, x2, y2, rad=0.25 if start > end else -0.25)


if __name__ == "__main__":
    main()
