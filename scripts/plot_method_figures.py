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
    fig, ax = base_fig(9.9, 5.2)
    title(ax, "Diagnosis-centered analog optimization flow")

    # Outer diagnosis loop.
    loop = FancyBboxPatch(
        (0.45, 0.52),
        9.02,
        3.92,
        boxstyle="round,pad=0.10,rounding_size=0.20",
        linewidth=1.2,
        edgecolor=LINE,
        facecolor="none",
        linestyle=(0, (5, 4)),
    )
    ax.add_patch(loop)
    ax.text(0.72, 4.25, "typed diagnosis envelope", fontsize=8.5, color=MUTED, weight="bold")
    ax.text(
        0.72,
        4.03,
        "evidence -> causal edges -> admissible edits",
        fontsize=6.9,
        color=MUTED,
    )

    draw_person(ax, 1.12, 3.14, "Human")
    draw_person(ax, 1.12, 1.46, "LLM")

    draw_file(ax, 4.35, 2.05, 1.28, 1.66, "Schema\nstate", [r"$G,\theta,\tau$", r"$\mathcal{D},\mathcal{E}$"])

    cycle_center = (7.55, 2.78)
    draw_cycle(ax, cycle_center, 0.92, ["surrogate", "search", "SPICE", "repair"])
    ax.text(cycle_center[0], cycle_center[1], "execute", ha="center", va="center", fontsize=7.5, color=INK)

    arrow(ax, 1.78, 3.22, 4.06, 3.25)
    arrow(ax, 4.05, 3.05, 1.85, 2.98)
    arrow(ax, 1.78, 1.62, 4.05, 2.45)
    arrow(ax, 4.05, 2.25, 1.88, 1.37)
    arrow(ax, 5.72, 2.86, 6.18, 2.86)
    arrow(ax, 6.35, 2.00, 5.68, 2.08, rad=-0.10)

    ax.text(2.35, 3.34, "review", fontsize=6.9, color=MUTED)
    ax.text(2.35, 1.72, "select", fontsize=6.9, color=MUTED)
    ax.text(5.90, 3.08, "candidate", fontsize=7.2, color=MUTED)
    ax.text(5.82, 1.72, "metrics", fontsize=6.9, color=MUTED)
    return fig


def draw_schema_state() -> plt.Figure:
    fig, ax = base_fig(9.8, 4.65)
    title(ax, "Schema as the shared executable state")
    draw_file(ax, 0.62, 1.42, 1.45, 1.95, "design\nstate", ["YAML", "schema edits", "evidence refs"])

    items = [
        (2.60, 3.00, "Topology", r"$G$"),
        (6.10, 3.00, "Variables", r"$\theta$"),
        (2.60, 1.87, "Targets", r"$\tau,J$"),
        (6.10, 1.87, "Dependencies", r"$\mathcal{D}$"),
        (4.28, 0.76, "Evidence", r"$\mathcal{E}$"),
    ]
    for x, y, heading, body in items:
        small_panel(ax, x, y, 2.85, 0.78, heading, body)

    ax.text(
        0.62,
        0.53,
        "All applied edits are typed schema commands; detailed traces stay in JSON artifacts.",
        fontsize=7.4,
        color=MUTED,
    )
    return fig


def draw_optimization_loop() -> plt.Figure:
    fig, ax = base_fig(9.6, 4.9)
    title(ax, "Optimization and validation execution loop")
    center = (4.72, 2.38)
    radius = 1.58
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
        (90, "gm/ID\nsurrogate"),
        (18, "NSGA-II"),
        (-54, "SPICE"),
        (-126, "archive"),
        (162, "bounds"),
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
        "Objective",
        [r"$\sum w_kv_k^2$", r"$+\,P_{\rm phys}$", "SPICE gate"],
    )
    ax.text(
        0.62,
        0.32,
        "Explicit repair handles OP balance, headroom, and MIM compensation;\naccepted diagnosis edits trigger short re-optimization.",
        fontsize=7.2,
        color=MUTED,
        linespacing=1.12,
    )
    return fig


def draw_diagnosis_loop() -> plt.Figure:
    fig, ax = base_fig(10.2, 4.75)
    title(ax, "Causal diagnosis and evidence-gated action selection")

    stages = [
        (0.35, "failed\nspecs"),
        (1.95, "typed\nedges"),
        (3.55, "cause\nrank"),
        (5.18, "SPICE\nprobe"),
        (6.98, "action\noptimizer"),
        (8.35, "apply /\nskip note"),
    ]
    for i, (x, label) in enumerate(stages):
        node(ax, x, 2.95, 1.05, 0.58, label)
        if i < len(stages) - 1:
            arrow(ax, x + 1.08, 3.24, stages[i + 1][0] - 0.05, 3.24)

    panel(
        ax,
        0.66,
        1.14,
        4.18,
        1.16,
        "Typed diagnosis",
        [
            r"$e=(u,v,t,\rho,\omega,\epsilon)$",
            "classes: gain, bias, poles,\nsymmetry, headroom, compensation",
        ],
    )
    panel(
        ax,
        5.34,
        1.14,
        4.18,
        1.16,
        "Admissibility",
        [
            r"$a\in\mathcal{A}_{adm}$ only if",
            r"$g_{\rm phys}(a)=1$ and",
            r"$selected(a)$ or $\Delta J(a)<0$",
        ],
    )
    ax.text(
        0.66,
        0.58,
        "The LLM or human explains and selects; the executor applies only admissible schema edits.",
        fontsize=7.4,
        color=MUTED,
    )
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
