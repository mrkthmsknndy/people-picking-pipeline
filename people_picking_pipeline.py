#!/usr/bin/env python3
"""
People Picking Pipeline

Run end-to-end from a raw Qualtrics CSV download of the "People Picking"
sociometric exercise:

- Cleans the raw export (keeps the first header row, drops the two Qualtrics
  label rows; drops known metadata column ranges; excludes preview/test rows
  and, by default, unfinished submissions; shuffles rows; assigns zero-padded
  anonymous ids; renames columns to short field names)
- Builds adjacency matrices for the five pick networks: design (d), lobbying
  (l), implementation (i), friendship (f), advice (a)
    * "Respondent-only" scope (default): both picker and picked must have
      responded to the survey. Reproduces prior years' numbers exactly.
    * "Full cohort" scope (--roster path/to/roster.csv): picks toward any
      named cohort member count, even if that person hasn't (yet) responded.
      Recommended once response collection is still in progress, since
      respondent-only scope silently undercounts popularity for anyone who
      picked a not-yet-responded classmate.
- Computes indegree summaries and "popularity of my picks" scores per
  network + a design+lobby+implement (DLI) omnibus (unique picks and with
  duplicates counted)
- Rescales popularity Z-scores to an IQ-style metric (mean 100, SD 15):
    nQ(O) Design/Opportunity, nQ(I) Lobbying/Influence, nQ(C) Implementation/
    Cohesion, nQ(F) Friendship, nQ(A) Advice, nQ(*)_unique / nQ(*)_dupe (DLI
    omnibus)
- Computes nQ(V) = variability across D/L/I picks = unique / total
- Writes an id<->email map for mail-merge, per-id profile CSVs (+ zip)
- Writes cohort summary stats, a correlation table, and simple regressions
  of friendship popularity against task-pick popularity
- Draws a network diagram for each of the five pick networks (PNG), plus a
  matching interactive 3D HTML diagram (same layout, height = indegree;
  drag to rotate -- skip with --no-3d), plus an optional full chart pack /
  PDF slide deck with --cohort-pack
- Writes DATA_DICTIONARY.md describing every output file

Usage:
    python people_picking_pipeline.py --in RAW.csv --outdir OUTDIR \\
        [--seed 42] [--roster roster.csv] [--include-preview] \\
        [--include-unfinished] [--no-viz] [--no-3d] [--cohort-pack]

Dependencies: pandas, numpy, matplotlib, networkx (matplotlib/networkx only
needed for network diagrams / --cohort-pack; omit --no-viz to skip them if
not installed). The interactive 3D diagrams need no extra Python package --
Plotly.js is bundled in assets/plotly.min.js and inlined directly into each
generated HTML file, so every one is single-file and fully offline; skip
them with --no-3d if you'd rather not.
"""
import argparse
import csv
import os
import re
import sys
import zipfile

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

RENAME_MAP = {
    "RecipientLastName": "lName",
    "RecipientFirstName": "fName",
    "RecipientEmail": "email",
    "pname": "pName",
    "ptype": "pType",
    "glist1": "gType1",
    "glist2": "gType2",
    "glistmix": "gMix",
    "friendship": "fPicks",
    "advice": "aPicks",
    "design": "dPicks",
    "lobby": "lPicks",
    "implement": "iPicks",
    "picklogic": "pickLogic",
}

PICK_COLS = ["dPicks", "lPicks", "iPicks", "fPicks", "aPicks"]


def drop_range(df, start_name, end_name):
    """Drop all columns from start_name to end_name, inclusive, by position."""
    cols = list(df.columns)
    lower = [c.lower() for c in cols]
    try:
        start_idx = cols.index(start_name)
    except ValueError:
        start_idx = lower.index(start_name.lower())
    try:
        end_idx = cols.index(end_name)
    except ValueError:
        end_idx = lower.index(end_name.lower())
    if start_idx > end_idx:
        start_idx, end_idx = end_idx, start_idx
    return df.drop(columns=cols[start_idx:end_idx + 1])


def norm(s):
    """Normalise a name for matching: collapse whitespace, casefold."""
    return " ".join(str(s).strip().split()).lower()


def clean_raw(in_path, seed, include_preview, include_unfinished):
    """Load a raw Qualtrics export and return a cleaned respondent dataframe.

    Returns (df, drop_report) where drop_report is a dict of counts for
    rows dropped as preview/blank or unfinished, for transparency.
    """
    df = pd.read_csv(in_path, header=0)
    # First two data rows are Qualtrics' human-readable / ImportId label rows
    df = df.drop(index=[0, 1], errors="ignore").reset_index(drop=True)

    drop_report = {"preview_or_blank": 0, "unfinished": 0, "total_raw": len(df)}

    if not include_preview:
        is_preview = pd.Series(False, index=df.index)
        if "DistributionChannel" in df.columns:
            is_preview |= df["DistributionChannel"].astype(str).str.lower().eq("preview")
        fname = df.get("RecipientFirstName", pd.Series("", index=df.index)).fillna("")
        lname = df.get("RecipientLastName", pd.Series("", index=df.index)).fillna("")
        is_blank_name = (fname.astype(str).str.strip() == "") & (lname.astype(str).str.strip() == "")
        drop_mask = is_preview | is_blank_name
        drop_report["preview_or_blank"] = int(drop_mask.sum())
        df = df[~drop_mask].reset_index(drop=True)

    if not include_unfinished and "Finished" in df.columns:
        finished = df["Finished"].astype(str).str.strip().str.lower().isin(["true", "1", "1.0"])
        drop_report["unfinished"] = int((~finished).sum())
        df = df[finished].reset_index(drop=True)

    df = drop_range(df, "StartDate", "ResponseID")
    df = drop_range(df, "LocationLatitude", "UserLanguage")
    df = df.sample(frac=1, random_state=seed).reset_index(drop=True)

    width = max(3, len(str(len(df))))
    df.insert(0, "id", [f"{i:0{width}d}" for i in range(1, len(df) + 1)])

    if "ExternalReference" in df.columns:
        df = df.drop(columns=["ExternalReference"])

    df = df.rename(columns=RENAME_MAP)
    return df, drop_report


# ---------------------------------------------------------------------------
# Roster / node-space
# ---------------------------------------------------------------------------

def load_full_roster(path):
    """Load an optional full-cohort roster CSV. Accepts a 'full_name' or
    'name' column (case-insensitive); any other columns are ignored."""
    r = pd.read_csv(path)
    cols_lower = {c.lower(): c for c in r.columns}
    name_col = cols_lower.get("full_name") or cols_lower.get("name")
    if name_col is None:
        raise SystemExit(
            f"--roster file {path!r} needs a 'full_name' (or 'name') column; found {list(r.columns)}"
        )
    names = [str(n).strip() for n in r[name_col].dropna().tolist() if str(n).strip()]
    # de-duplicate while preserving order
    seen = set()
    out = []
    for n in names:
        k = norm(n)
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


def build_node_space(df, full_roster_names):
    """Build the full node id space.

    Respondents keep the ids clean_raw() already assigned. If a full cohort
    roster is supplied, any roster name that isn't a respondent gets a new
    id (prefixed 'nr' = non-respondent) so it can still receive picks.

    Returns (name_to_id, node_ids, node_meta) where node_meta is a dict
    id -> {"full_name": ..., "is_respondent": bool}.
    """
    df_names = df[["id", "fName", "lName"]].fillna("")
    df_names["full"] = (df_names["fName"].str.strip() + " " + df_names["lName"].str.strip()).str.strip()

    name_to_id = {}
    node_meta = {}
    for _, row in df_names.iterrows():
        if row["full"]:
            name_to_id[norm(row["full"])] = row["id"]
        node_meta[row["id"]] = {"full_name": row["full"], "is_respondent": True}

    respondent_ids = df["id"].tolist()
    node_ids = list(respondent_ids)

    if full_roster_names:
        width = max(3, len(str(len(full_roster_names) + len(respondent_ids))))
        nr_i = 1
        for name in full_roster_names:
            key = norm(name)
            if key in name_to_id:
                continue
            nid = f"nr{nr_i:0{width}d}"
            nr_i += 1
            name_to_id[key] = nid
            node_meta[nid] = {"full_name": name, "is_respondent": False}
            node_ids.append(nid)

    return name_to_id, node_ids, respondent_ids, node_meta


# ---------------------------------------------------------------------------
# Networks
# ---------------------------------------------------------------------------

def build_adjacency(df, col, name_to_id, row_ids, col_ids):
    """Rectangular adjacency: rows = respondents (pickers), cols = node space
    (targets). row_ids and col_ids may be the same list (respondent-only
    scope) or col_ids may be the larger full-cohort node space."""
    A = pd.DataFrame(0, index=row_ids, columns=col_ids, dtype=int)
    for _, row in df.iterrows():
        src = row["id"]
        if src not in A.index:
            continue
        for person in (p.strip() for p in str(row.get(col, "")).split(",") if p.strip()):
            tgt = name_to_id.get(norm(person))
            if tgt and tgt in A.columns and tgt != src:
                A.at[src, tgt] = 1
    for i in row_ids:
        if i in A.columns:
            A.at[i, i] = 0
    return A


def indegrees(A):
    return A.sum(axis=0).astype(int)


def zscore(s, ddof=0):
    s = pd.Series(s, dtype=float)
    mu = s.mean()
    sd = s.std(ddof=ddof)
    if sd == 0 or pd.isna(sd):
        return pd.Series(0.0, index=s.index)
    return (s - mu) / sd


def iq_scale(z):
    return (z * 15 + 100).round(1)


def popularity_table(A, ids, label):
    """For each picker, sum the indegree (within A's column space) of who
    they picked -- i.e. how widely-picked their own picks were."""
    indeg = indegrees(A)
    rows = []
    for i in ids:
        picks = [col for col, v in A.loc[i].items() if v == 1]
        score = int(indeg.reindex(picks).fillna(0).sum()) if picks else 0
        rows.append({"id": i, f"score_{label}": score, f"k_{label}": len(picks)})
    dfp = pd.DataFrame(rows).set_index("id").loc[ids].reset_index()
    z = zscore(dfp[f"score_{label}"])
    dfp[f"z_{label}"] = z.round(4)
    dfp[f"iq_{label}"] = iq_scale(z)
    return dfp


# ---------------------------------------------------------------------------
# Network diagrams
# ---------------------------------------------------------------------------

NETWORK_LABELS = {
    "dPicks": "Design (Opportunity)",
    "lPicks": "Lobbying (Influence)",
    "iPicks": "Implementation (Cohesion)",
    "fPicks": "Friendship",
    "aPicks": "Advice",
}


# Validated categorical/status colors (dataviz skill palette; see references/palette.md).
# Respondent uses categorical slot 1 (blue); "not yet responded" is a muted status gray,
# not a peer series -- it recedes on purpose so respondents (the people getting this
# report) read first. Both are reinforced with the legend (never color-alone identity).
COLOR_RESPONDENT = "#2a78d6"
COLOR_NOT_RESPONDED = "#898781"
COLOR_EDGE = "#54524c"
COLOR_TEXT_PRIMARY = "#0b0b0b"
COLOR_SURFACE = "#fcfcfb"

# Muted, mutually distinguishable tints for community/clique background shading.
# Kept well clear of COLOR_RESPONDENT's blue so cluster shading never reads as a
# third node category; applied at low alpha so it stays background, not a series.
CLUSTER_HULL_COLORS = [
    "#e0a838", "#4f9d6e", "#c26b8f", "#7c7cd6", "#3fa6a6",
    "#c2743f", "#8c9e3f", "#a06bd6", "#4f8fc2", "#c24f4f",
]


def build_graph_and_layout(A, node_meta):
    """Build the pick-network DiGraph and compute its 2D layout + community
    partition. Shared by the static PNG diagram and the interactive 3D HTML
    view, so both show the exact same x/y placement and clique groupings --
    only the z-axis (added by the 3D view) differs.

    Returns (G, pos, communities, indeg, show_clusters) or None if there are
    no active (non-isolate) nodes to draw.
    """
    import networkx as nx

    G = nx.DiGraph()
    indeg = indegrees(A)
    # Only draw nodes that appear in at least one edge, to keep it legible
    active_targets = set(indeg[indeg > 0].index)
    active_sources = set(A.index[A.sum(axis=1) > 0])
    active = active_targets | active_sources
    if not active:
        return None

    for n in active:
        meta = node_meta.get(n, {"is_respondent": True})
        G.add_node(n, is_respondent=meta.get("is_respondent", True))
    for src in A.index:
        if src not in active:
            continue
        for tgt, v in A.loc[src].items():
            if v == 1 and tgt in active:
                G.add_edge(src, tgt)

    n_nodes = len(G)

    # Layout, in three stages:
    #
    # 1. Detect communities (Louvain modularity) on the undirected graph -- these
    #    are the "cliques"/cliquish subgroups: sets of people who pick heavily
    #    among themselves relative to the rest of the cohort.
    # 2. Lay the communities out as a coarse graph first (one supernode per
    #    community, weighted by cross-community edge count) with generous
    #    spacing, then lay each community out internally with Kamada-Kawai
    #    (which minimizes graph-theoretic stress, pulling its own tightly-linked
    #    members together) and place it at its assigned slot. This is what
    #    actually produces visible gaps between cliques, rather than relying on
    #    a single global layout to happen to separate them.
    # 3. A pure node-repulsion "declutter" pass (no edge attraction) across ALL
    #    nodes, so nothing overlaps regardless of how tight a community is.
    import numpy as np
    UG = G.to_undirected()

    try:
        communities = list(nx.algorithms.community.louvain_communities(UG, seed=42, weight=None))
    except Exception:
        communities = [set(UG.nodes())]
    # collapse degenerate partitions (all-singletons, or just one community)
    non_trivial = [c for c in communities if len(c) > 1]
    show_clusters = 1 < len(communities) and len(non_trivial) >= 1 and n_nodes >= 6

    node_to_comm = {}
    for i, c in enumerate(communities):
        for n in c:
            node_to_comm[n] = i

    if len(communities) > 1:
        coarse = nx.Graph()
        coarse.add_nodes_from(range(len(communities)))
        for u, v in UG.edges():
            cu, cv = node_to_comm[u], node_to_comm[v]
            if cu != cv:
                w = coarse.get_edge_data(cu, cv, {"weight": 0})["weight"]
                coarse.add_edge(cu, cv, weight=w + 1)
        try:
            coarse_pos = nx.spring_layout(coarse, seed=42, weight="weight",
                                           k=5.0 / max(len(communities) ** 0.5, 1),
                                           iterations=300)
        except Exception:
            coarse_pos = {i: (np.cos(2 * np.pi * i / len(communities)),
                              np.sin(2 * np.pi * i / len(communities)))
                          for i in range(len(communities))}
    else:
        coarse_pos = {0: (0.0, 0.0)}

    # Internal layout + footprint radius per community, independent of where
    # it ends up -- needed before centroids can be packed without overlapping.
    local_scale = 0.30 if len(communities) > 1 else 1.0
    local_layouts = {}
    comm_radius = {}
    for i, comm in enumerate(communities):
        members = list(comm)
        sub = UG.subgraph(members)
        if len(members) == 1:
            local = {members[0]: (0.0, 0.0)}
        else:
            try:
                local = nx.kamada_kawai_layout(sub, weight=None)
            except Exception:
                local = nx.spring_layout(sub, seed=42, iterations=200)
        lxs = [p[0] for p in local.values()]
        lys = [p[1] for p in local.values()]
        lspan = max(max(lxs) - min(lxs), max(lys) - min(lys), 1e-6) if len(members) > 1 else 1.0
        lcx = (max(lxs) + min(lxs)) / 2 if len(members) > 1 else 0.0
        lcy = (max(lys) + min(lys)) / 2 if len(members) > 1 else 0.0
        scaled = {n: ((local[n][0] - lcx) / lspan * local_scale,
                       (local[n][1] - lcy) / lspan * local_scale) for n in members}
        local_layouts[i] = scaled
        comm_radius[i] = max((abs(x) ** 2 + abs(y) ** 2) ** 0.5 for x, y in scaled.values()) if scaled else 0.05

    # Pack community centroids apart so their footprints (radius above, plus a
    # margin for the node markers + hull padding) don't overlap -- this is what
    # actually guarantees a visible gap between cliques, rather than hoping the
    # coarse spring layout above happened to spread them out far enough on its
    # own (it starts from that layout, so cross-linked communities still end
    # up as neighbors, but no two footprints are allowed to overlap).
    if len(communities) > 1:
        idx = list(range(len(communities)))
        C = np.array([coarse_pos[i] for i in idx], dtype=float)
        R = np.array([comm_radius[i] for i in idx], dtype=float)
        margin = 0.34
        for _ in range(400):
            diff = C[:, None, :] - C[None, :, :]
            dist = np.sqrt((diff ** 2).sum(-1))
            np.fill_diagonal(dist, np.inf)
            min_gap = R[:, None] + R[None, :] + margin
            overlap = min_gap - dist
            crowded = overlap > 0
            if not crowded.any():
                break
            safe_dist = np.where(dist == 0, 1, dist)
            direction = diff / safe_dist[..., None]
            magnitude = np.where(crowded, overlap, 0.0) * 0.5
            C += (direction * magnitude[..., None]).sum(axis=1) * 0.5
        coarse_pos = {idx[k]: (C[k, 0], C[k, 1]) for k in range(len(idx))}
    else:
        coarse_pos = {0: (0.0, 0.0)}

    pos = {}
    for i, comm in enumerate(communities):
        ccx, ccy = coarse_pos[i]
        for n, (lx, ly) in local_layouts[i].items():
            pos[n] = (ccx + lx, ccy + ly)

    nodes = list(G.nodes())
    rng = np.random.default_rng(42)
    P = np.array([pos[n] for n in nodes], dtype=float)
    min_sep = 1.7 / max(n_nodes ** 0.5, 1)  # target minimum center-to-center distance
    for _ in range(250):
        diff = P[:, None, :] - P[None, :, :]
        dist = np.sqrt((diff ** 2).sum(-1))
        np.fill_diagonal(dist, np.inf)
        overlap = min_sep - dist
        crowded = overlap > 0
        if not crowded.any():
            break
        # zero-distance coincident nodes: nudge with random jitter first
        zero = (dist < 1e-9) & crowded
        if zero.any():
            jitter = rng.normal(scale=1e-3, size=P.shape)
            P += jitter
            continue
        push = np.zeros_like(P)
        safe_dist = np.where(dist == 0, 1, dist)
        direction = diff / safe_dist[..., None]
        magnitude = np.where(crowded, overlap, 0.0) * 0.5
        push = (direction * magnitude[..., None]).sum(axis=1)
        P += push * 0.5
    pos = {n: (P[i, 0], P[i, 1]) for i, n in enumerate(nodes)}

    # Center and scale uniformly (preserving aspect ratio -- a per-axis min-max
    # stretch here is what was flattening clusters into an artificial circle).
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
    pos = {n: ((x - cx) / span, (y - cy) / span) for n, (x, y) in pos.items()}

    return G, pos, communities, indeg, show_clusters, min_sep


def draw_network(A, node_meta, title, out_path, scope_label=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe
    import networkx as nx
    import numpy as np

    built = build_graph_and_layout(A, node_meta)
    if built is None:
        return None
    G, pos, communities, indeg, show_clusters, min_sep = built
    n_nodes = len(G)

    fig, ax = plt.subplots(figsize=(14, 14), facecolor=COLOR_SURFACE)
    ax.set_facecolor(COLOR_SURFACE)

    # Shade each community as a soft background blob behind its members, so
    # cliques/subgroups are visible at a glance rather than something you have
    # to infer from spacing alone. Purely decorative -- node color still carries
    # respondent status.
    if show_clusters:
        from matplotlib.patches import Polygon, Circle
        from matplotlib.colors import to_rgba
        pad = min_sep * 0.45
        hull_i = 0
        for i, comm in enumerate(communities):
            members = [n for n in comm if n in pos]
            if len(members) < 3:
                continue
            color = CLUSTER_HULL_COLORS[hull_i % len(CLUSTER_HULL_COLORS)]
            hull_i += 1
            face = to_rgba(color, 0.07)   # light fill -- overlaps stay legible, not muddy
            edge = to_rgba(color, 0.75)   # solid-reading boundary carries the grouping
            pts = np.array([pos[n] for n in members])
            drew_hull = False
            if len(members) >= 3:
                try:
                    from scipy.spatial import ConvexHull
                    hull = ConvexHull(pts)
                    hull_pts = pts[hull.vertices]
                    centroid = hull_pts.mean(axis=0)
                    expanded = centroid + (hull_pts - centroid) * 1.12 + \
                        (hull_pts - centroid) / (np.linalg.norm(hull_pts - centroid, axis=1, keepdims=True) + 1e-9) * pad
                    ax.add_patch(Polygon(expanded, closed=True, facecolor=face,
                                          edgecolor=edge, linewidth=1.6, zorder=0))
                    drew_hull = True
                except Exception:
                    drew_hull = False
            if not drew_hull:
                centroid = pts.mean(axis=0)
                radius = max(np.linalg.norm(pts - centroid, axis=1).max(), 1e-6) + pad
                ax.add_patch(Circle(centroid, radius, facecolor=face, edgecolor=edge,
                                     linewidth=1.6, zorder=0))

    # sqrt scaling keeps a handful of very popular nodes from dwarfing everyone else
    sizes = [140 + 260 * (indeg.get(n, 0) ** 0.5) for n in G.nodes()]
    colors = [COLOR_RESPONDENT if G.nodes[n].get("is_respondent", True) else COLOR_NOT_RESPONDED
              for n in G.nodes()]

    nx.draw_networkx_edges(G, pos, ax=ax, arrows=True, arrowsize=8, alpha=0.6,
                            width=1.7, edge_color=COLOR_EDGE, connectionstyle="arc3,rad=0.08",
                            node_size=sizes)
    nx.draw_networkx_nodes(G, pos, ax=ax, node_size=sizes, node_color=colors,
                            edgecolors=COLOR_SURFACE, linewidths=1.0)
    labels = {n: n for n in G.nodes()}
    txt = nx.draw_networkx_labels(G, pos, labels, ax=ax, font_size=7.5, font_color=COLOR_TEXT_PRIMARY)
    for t in txt.values():
        t.set_path_effects([pe.withStroke(linewidth=2.4, foreground=COLOR_SURFACE)])

    subtitle = f"{n_nodes} people shown (isolates hidden) · {A.shape[0]} respondents"
    if scope_label:
        subtitle += f" · {scope_label}"
    ax.set_title(title, fontsize=18, color=COLOR_TEXT_PRIMARY, pad=18, loc="left")
    ax.text(0.0, 1.0, subtitle, transform=ax.transAxes, ha="left", va="bottom",
            fontsize=10, color="#52514e")
    ax.axis("off")
    ax.margins(0.06)
    handles = [
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=COLOR_RESPONDENT,
                   markeredgecolor=COLOR_SURFACE, markersize=11, label="Respondent"),
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=COLOR_NOT_RESPONDED,
                   markeredgecolor=COLOR_SURFACE, markersize=11, label="Not yet responded"),
    ]
    if any(not G.nodes[n].get("is_respondent", True) for n in G.nodes()):
        ax.legend(handles=handles, loc="lower left", frameon=False, fontsize=10,
                  labelcolor=COLOR_TEXT_PRIMARY)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180, facecolor=COLOR_SURFACE)
    plt.close(fig)
    return out_path


# Bundled Plotly.js (MIT license), shipped in this repo under assets/ and
# inlined directly into every generated HTML file (see _load_plotly_js())
# so each diagram is a single, fully self-contained file: no companion JS
# file to keep track of, no CDN, no internet connection needed to view it.
# One-time cost: ~4.8MB added to each of the five HTML files.
_ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
PLOTLY_JS_ASSET = os.path.join(_ASSET_DIR, "plotly.min.js")
PLOTLY_JS_CDN = "https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.35.2/plotly.min.js"


# Which brand motif corresponds to each pick network, for the corner mark on
# each interactive 3D diagram. Design/Lobbying/Implementation map onto the
# Opportunity/Influence/Cohesion constructs the profiles already score;
# Friendship/Advice aren't part of that construct, so they get the plain
# network glyph instead.
MOTIF_FOR_COL = {
    "dPicks": "opportunity",
    "lPicks": "influence",
    "iPicks": "cohesion",
}


def _load_corner_svg(col):
    """Return inline SVG markup (sized to fill its wrapper div) for the
    corner mark matching this pick network, or None if the bundled asset
    is missing."""
    import re
    name = MOTIF_FOR_COL.get(col)
    fname = f"motif_{name}.svg" if name else "logo.svg"
    path = os.path.join(_ASSET_DIR, fname)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        svg = f.read()
    # Drop the XML prolog/doctype matplotlib emits and force the root <svg>
    # to fill its wrapper div rather than keep its native point dimensions.
    svg = re.sub(r"<\?xml.*?\?>\s*", "", svg, flags=re.DOTALL)
    svg = re.sub(r"<!DOCTYPE.*?>\s*", "", svg, flags=re.DOTALL)
    svg = re.sub(r'(<svg\b[^>]*?)\swidth="[^"]*"', r"\1", svg, count=1)
    svg = re.sub(r'(<svg\b[^>]*?)\sheight="[^"]*"', r"\1", svg, count=1)
    svg = re.sub(r"(<svg\b)", r'\1 width="100%" height="100%"', svg, count=1)
    return svg


def _load_plotly_js():
    """Return the bundled Plotly.js source as a string, to inline into each
    diagram's <script> tag. Returns None if the bundled asset is missing
    (e.g. a stripped-down checkout) -- callers fall back to a CDN <script
    src> reference with a printed warning in that case."""
    if os.path.exists(PLOTLY_JS_ASSET):
        with open(PLOTLY_JS_ASSET, "r", encoding="utf-8") as f:
            return f.read()
    print("  [!] Bundled plotly.min.js not found in assets/ -- falling back to "
          "loading Plotly.js from a CDN (needs an internet connection to view).")
    return None


def draw_network_3d(A, node_meta, title, out_path, scope_label="", plotly_js=None, corner_svg=None):
    """Interactive 3D companion to draw_network(): the same x/y layout
    (so cliques line up with the 2D diagram), lifted into a third dimension
    by indegree -- how often each person was picked, i.e. degree centrality
    -- so the network reads like a skyline you can rotate: popular people
    rise as towers above the 2D "floor" layout. A single self-contained
    HTML file -- Plotly.js is inlined directly into it, so it needs no
    companion file, no CDN, and no internet connection: one file, drop it
    anywhere, open it in any modern browser. Drag to rotate, scroll to
    zoom, hover for details, click legend entries to toggle.
    """
    import json

    built = build_graph_and_layout(A, node_meta)
    if built is None:
        return None
    G, pos, communities, indeg, show_clusters, min_sep = built

    node_to_comm = {}
    for i, comm in enumerate(communities):
        for n in comm:
            node_to_comm[n] = i

    def z_of(n):
        return float(indeg.get(n, 0))

    def hover(n):
        status = "Respondent" if G.nodes[n].get("is_respondent", True) else "Not yet responded"
        return (f"{n}<br>Times picked: {int(z_of(n))}<br>{status}"
                f"<br>Clique group: {node_to_comm.get(n, '-') + 1 if show_clusters else '-'}")

    # Vertical "stem" from the floor (z=0) up to each node -- what makes this
    # read as a skyline rather than a plain 3D scatter. One trace, None-separated.
    stem_x, stem_y, stem_z = [], [], []
    for n in G.nodes():
        x, y = pos[n]
        stem_x += [x, x, None]
        stem_y += [y, y, None]
        stem_z += [0, z_of(n), None]

    # Pick edges, drawn at each node's true (elevated) height.
    edge_x, edge_y, edge_z = [], [], []
    for src, tgt in G.edges():
        sx, sy = pos[src]
        tx, ty = pos[tgt]
        edge_x += [sx, tx, None]
        edge_y += [sy, ty, None]
        edge_z += [z_of(src), z_of(tgt), None]

    def marker_trace(node_list, color, label):
        return {
            "type": "scatter3d", "mode": "markers+text",
            "x": [pos[n][0] for n in node_list], "y": [pos[n][1] for n in node_list],
            "z": [z_of(n) for n in node_list],
            "text": list(node_list), "textposition": "top center",
            "textfont": {"size": 9, "color": COLOR_TEXT_PRIMARY},
            "hovertext": [hover(n) for n in node_list], "hoverinfo": "text",
            "marker": {
                "size": [6 + 3.2 * (indeg.get(n, 0) ** 0.5) for n in node_list],
                "color": color, "line": {"color": COLOR_SURFACE, "width": 1},
            },
            "name": label, "showlegend": True,
        }

    respondents = [n for n in G.nodes() if G.nodes[n].get("is_respondent", True)]
    not_responded = [n for n in G.nodes() if not G.nodes[n].get("is_respondent", True)]

    data = [
        {
            "type": "scatter3d", "mode": "lines", "x": stem_x, "y": stem_y, "z": stem_z,
            "line": {"color": "#c9c8c1", "width": 2}, "opacity": 0.5,
            "hoverinfo": "skip", "showlegend": False,
        },
        {
            "type": "scatter3d", "mode": "lines", "x": edge_x, "y": edge_y, "z": edge_z,
            "line": {"color": COLOR_EDGE, "width": 3}, "opacity": 0.35,
            "hoverinfo": "skip", "showlegend": False,
        },
        marker_trace(respondents, COLOR_RESPONDENT, "Respondent"),
    ]
    if not_responded:
        data.append(marker_trace(not_responded, COLOR_NOT_RESPONDED, "Not yet responded"))

    n_nodes = len(G)
    subtitle = f"{n_nodes} people shown (isolates hidden) · {A.shape[0]} respondents"
    if scope_label:
        subtitle += f" · {scope_label}"

    layout = {
        "title": {"text": f"{title}<br><sup>{subtitle} — height = indegree (times picked); drag to rotate</sup>",
                   "font": {"color": COLOR_TEXT_PRIMARY},
                   "x": 0.045, "xanchor": "left", "y": 0.965, "yanchor": "top",
                   "pad": {"t": 12}},
        "paper_bgcolor": COLOR_SURFACE, "plot_bgcolor": COLOR_SURFACE,
        "scene": {
            "xaxis": {"visible": False}, "yaxis": {"visible": False},
            "zaxis": {"title": "Indegree (times picked)", "color": COLOR_TEXT_PRIMARY,
                      "backgroundcolor": COLOR_SURFACE, "gridcolor": "#e3e2dc"},
            "aspectmode": "manual", "aspectratio": {"x": 1, "y": 1, "z": 0.7},
            "camera": {"eye": {"x": 1.4, "y": -1.4, "z": 0.9}},
            "bgcolor": COLOR_SURFACE,
        },
        # Horizontal legend, centered, just under the diagram (in the bottom margin).
        "legend": {"font": {"color": COLOR_TEXT_PRIMARY}, "orientation": "h",
                   "x": 0.5, "xanchor": "center", "y": 0.02, "yanchor": "bottom"},
        "margin": {"l": 0, "r": 0, "t": 110, "b": 70},
    }

    plotly_tag = (f"<script>{plotly_js}</script>" if plotly_js is not None
                  else f'<script src="{PLOTLY_JS_CDN}"></script>')

    corner_html = (f'<div id="corner-logo" '
                   f'style="position:fixed;top:16px;right:18px;width:52px;height:52px;'
                   f'opacity:0.92;pointer-events:none;z-index:10;">{corner_svg}</div>'
                   if corner_svg else "")

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{title} (3D)</title>
{plotly_tag}
<style>html,body{{margin:0;height:100%;background:{COLOR_SURFACE};font-family:-apple-system,Helvetica,Arial,sans-serif;}}
#plot{{width:100vw;height:100vh;}}</style>
</head><body>
<div id="plot"></div>
{corner_html}
<script>
Plotly.newPlot("plot", {json.dumps(data)}, {json.dumps(layout)}, {{responsive: true}});
</script>
</body></html>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="in_path", required=True, help="Raw Qualtrics CSV")
    ap.add_argument("--outdir", dest="outdir", required=True, help="Output directory")
    ap.add_argument("--seed", type=int, default=42, help="Random seed for row shuffle")
    ap.add_argument("--roster", dest="roster_path", default=None,
                     help="Optional CSV of the full cohort ('full_name' column) for full-cohort network scope")
    ap.add_argument("--include-preview", action="store_true",
                     help="Keep Qualtrics preview/blank rows (excluded by default)")
    ap.add_argument("--include-unfinished", action="store_true",
                     help="Keep unfinished (Finished=False) submissions (excluded by default)")
    ap.add_argument("--no-viz", action="store_true", help="Skip the five per-network diagrams (2D and 3D)")
    ap.add_argument("--no-3d", action="store_true",
                     help="Skip the interactive 3D HTML diagrams (the static 2D PNGs are still produced)")
    ap.add_argument("--cohort-pack", action="store_true", help="Generate additional PNG figures + PDF slide deck")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    # -----------------------------
    # 1) Clean the raw CSV
    # -----------------------------
    df, drop_report = clean_raw(args.in_path, args.seed, args.include_preview, args.include_unfinished)
    print(f"Raw rows: {drop_report['total_raw']} | "
          f"dropped preview/blank: {drop_report['preview_or_blank']} | "
          f"dropped unfinished: {drop_report['unfinished']} | "
          f"respondents kept: {len(df)}")

    roster_names = load_full_roster(args.roster_path) if args.roster_path else None
    name_to_id, node_ids, respondent_ids, node_meta = build_node_space(df, roster_names)
    if roster_names:
        n_nr = len(node_ids) - len(respondent_ids)
        print(f"Full-cohort scope: {len(respondent_ids)} respondents + {n_nr} not-yet-responded cohort members "
              f"= {len(node_ids)} nodes")
    else:
        print(f"Respondent-only scope: {len(respondent_ids)} nodes")

    # Save roster and id-email map (respondents only -- non-respondents have no email on file)
    roster = df[["id", "fName", "lName", "email"]].copy()
    roster.to_csv(os.path.join(args.outdir, "roster_respondents.csv"), index=False)
    roster[["id", "email", "fName", "lName"]].to_csv(os.path.join(args.outdir, "pp_id_email_map.csv"), index=False)

    if roster_names:
        full_roster_out = pd.DataFrame([
            {"id": nid, "full_name": meta["full_name"], "is_respondent": meta["is_respondent"]}
            for nid, meta in node_meta.items()
        ]).sort_values("id")
        full_roster_out.to_csv(os.path.join(args.outdir, "roster_full_cohort.csv"), index=False)

    # -----------------------------
    # 2) Adjacencies for all five pick types
    # -----------------------------
    ids = respondent_ids
    col_ids = node_ids  # equals respondent_ids when no --roster given
    As = {}
    for col in PICK_COLS:
        A = build_adjacency(df, col, name_to_id, ids, col_ids)
        As[col] = A
        A.to_csv(os.path.join(args.outdir, f"adjacency_{col}_respondents.csv"))
        edges = [(i, j) for i in ids for j in col_ids if A.at[i, j] == 1]
        pd.DataFrame(edges, columns=["source_id", "target_id"]).to_csv(
            os.path.join(args.outdir, f"edges_{col}_respondents.csv"), index=False)

    A_any = ((As["dPicks"] + As["lPicks"] + As["iPicks"] + As["fPicks"] + As["aPicks"]) > 0).astype(int)
    A_any.to_csv(os.path.join(args.outdir, "adjacency_anyPick_respondents.csv"))
    A_dli = ((As["dPicks"] + As["lPicks"] + As["iPicks"]) > 0).astype(int)

    # -----------------------------
    # 3) Network diagrams
    # -----------------------------
    if not args.no_viz:
        viz_dir = os.path.join(args.outdir, "network_diagrams")
        os.makedirs(viz_dir, exist_ok=True)
        scope_label = "full-cohort scope" if roster_names else "respondent-only scope"
        for col in PICK_COLS:
            out_path = os.path.join(viz_dir, f"network_{col}.png")
            draw_network(As[col], node_meta, NETWORK_LABELS[col], out_path, scope_label=scope_label)
        if not args.no_3d:
            viz3d_dir = os.path.join(args.outdir, "network_diagrams_3d")
            os.makedirs(viz3d_dir, exist_ok=True)
            plotly_js = _load_plotly_js()  # read once, inlined into all five files below
            for col in PICK_COLS:
                out_path = os.path.join(viz3d_dir, f"network_{col}_3d.html")
                draw_network_3d(As[col], node_meta, NETWORK_LABELS[col], out_path,
                                 scope_label=scope_label, plotly_js=plotly_js,
                                 corner_svg=_load_corner_svg(col))

    # -----------------------------
    # 4) Indegree summary (anonymized)
    # -----------------------------
    indeg_summary = pd.DataFrame({"id": ids})
    for col in PICK_COLS:
        indeg_summary[f"in_{col}"] = indegrees(As[col]).reindex(ids).fillna(0).astype(int).values
    indeg_summary["in_anyPick"] = indegrees(A_any).reindex(ids).fillna(0).astype(int).values
    N = len(col_ids)
    for c in [c for c in indeg_summary.columns if c.startswith("in_")]:
        indeg_summary[f"{c}_rate"] = (indeg_summary[c] / (N - 1)).round(4) if N > 1 else 0.0
    indeg_summary.to_csv(os.path.join(args.outdir, "pp_indegree_summary_anonymized.csv"), index=False)

    # -----------------------------
    # 5) Popularity-of-my-picks per network + DLI omnibus
    # -----------------------------
    ptables = {key: popularity_table(As[f"{key}Picks"], ids, key) for key in ["d", "l", "i", "f", "a"]}
    main_tbl = ptables["d"][["id", "score_d", "k_d", "z_d", "iq_d"]].copy()
    for key in ["l", "i", "f", "a"]:
        part = ptables[key]
        cols = [c for c in part.columns if c != "id"]
        main_tbl = main_tbl.merge(part[["id"] + cols], on="id", how="left")

    in_dli = indegrees(A_dli)
    rows = []
    for i in ids:
        d = [c for c, v in As["dPicks"].loc[i].items() if v == 1]
        l = [c for c, v in As["lPicks"].loc[i].items() if v == 1]
        ii = [c for c, v in As["iPicks"].loc[i].items() if v == 1]
        with_dupes = d + l + ii
        unique = sorted(set(with_dupes) - {i})
        score_dupe = int(in_dli.reindex(with_dupes).fillna(0).sum()) if with_dupes else 0
        score_unique = int(in_dli.reindex(unique).fillna(0).sum()) if unique else 0
        rows.append({
            "id": i,
            "score_dli_dupe": score_dupe, "k_dli_dupe": len(with_dupes),
            "score_dli_unique": score_unique, "k_dli_unique": len(unique),
            "k_d": len(d), "k_l": len(l), "k_i": len(ii),
        })
    dli_df = pd.DataFrame(rows).set_index("id").loc[ids]
    for col in ["score_dli_dupe", "score_dli_unique"]:
        z = zscore(dli_df[col])
        dli_df[f"z_{col}"] = z.round(4)
        dli_df[f"iq_{col}"] = iq_scale(z)
    dli_df = dli_df.reset_index()

    # k_d/k_l/k_i are already in main_tbl (from popularity_table); drop the
    # redundant copies from dli_df before merging so we don't get _x/_y pairs
    dli_df = dli_df.drop(columns=["k_d", "k_l", "k_i"])
    scored = main_tbl.merge(dli_df, on="id", how="left")

    rename_to_nq = {
        "iq_d": "nQ(O)", "iq_i": "nQ(C)", "iq_l": "nQ(I)",
        "iq_f": "nQ(F)", "iq_a": "nQ(A)",
        "iq_score_dli_dupe": "nQ(*)_dupe", "iq_score_dli_unique": "nQ(*)_unique",
    }
    scored_nq = scored.rename(columns=rename_to_nq)
    scored_nq.to_csv(os.path.join(args.outdir, "pp_pick_popularity_all_scored_nq.csv"), index=False)

    # -----------------------------
    # 6) nQ(V) variability and merged output
    # -----------------------------
    rows = []
    for i in ids:
        d = [c for c, v in As["dPicks"].loc[i].items() if v == 1]
        l = [c for c, v in As["lPicks"].loc[i].items() if v == 1]
        ii = [c for c, v in As["iPicks"].loc[i].items() if v == 1]
        total = len(d) + len(l) + len(ii)
        unique = len(set(d + l + ii))
        v = (unique / total) if total > 0 else None
        rows.append({"id": i, "k_dli_total": total,
                     "nQ(V)": round(v, 4) if v is not None else None})
    nqV = pd.DataFrame(rows)

    full = scored_nq.merge(nqV, on="id", how="left")
    full.to_csv(os.path.join(args.outdir, "pp_pick_popularity_all_scored_nq_v.csv"), index=False)

    # -----------------------------
    # 7) Per-id profiles (CSV) + zip
    # -----------------------------
    profiles_dir = os.path.join(args.outdir, "profiles")
    os.makedirs(profiles_dir, exist_ok=True)

    profiles_table = full.merge(
        indeg_summary[["id", "in_dPicks", "in_lPicks", "in_iPicks", "in_fPicks", "in_aPicks", "in_anyPick"]],
        on="id", how="left"
    )
    party_cols = [c for c in ["pName", "pType", "gType1", "gType2"] if c in df.columns]
    if party_cols:
        profiles_table = profiles_table.merge(df[["id"] + party_cols], on="id", how="left")

    # (source column, header shown in the file, one-line plain-English definition,
    # blank_after) -- Friendship/Advice popularity and the "times picked" indegree
    # counts are deliberately left out of the profile (too revealing for a
    # personal report); "Perspicacity"/"Flexibility" are Mark's preferred labels
    # for the nQ(...) constructs here, in place of the plainer descriptions used
    # elsewhere (DATA_DICTIONARY.md, the cohort-level CSVs) -- those keep the
    # nQ(...) terminology as-is.
    PROFILE_FIELDS = [
        ("nQ(O)", "Perspicacity(Opportunity)",
         "Popularity (IQ-scaled: mean 100, SD 15) of the people YOU picked for the Design/Opportunity task", False),
        ("nQ(I)", "Perspicacity(Influence)",
         "Popularity of the people YOU picked for the Lobbying/Influence task", False),
        ("nQ(C)", "Perspicacity(Cohesion)",
         "Popularity of the people YOU picked for the Implementation/Cohesion task", False),
        ("nQ(*)_unique", "Perspicacity(Overall)",
         "Combined Design+Lobbying+Implementation popularity score, counting each person you picked only once even if you picked them for more than one task role", True),
        ("nQ(V)", "Flexibility(Overall)",
         "How much you varied WHO you picked across the three task roles (1.0 = a different person every time; lower = you reused the same people)", False),
        ("k_d", None, "Number of people you picked for Design", False),
        ("k_l", None, "Number of people you picked for Lobbying", False),
        ("k_i", None, "Number of people you picked for Implementation", False),
        ("k_dli_total", None, "Total Design+Lobbying+Implementation picks you made (counts repeats)", False),
        ("k_dli_unique", None, "Number of distinct people you picked across the three task roles", False),
    ]
    fields_present = [f for f in PROFILE_FIELDS if f[0] in profiles_table.columns]

    def fmt_num(x, min_decimals=0):
        if pd.isna(x):
            return "n/a"
        xf = float(x)
        if min_decimals >= 1:
            return f"{xf:.1f}"
        return f"{xf:.0f}" if xf == round(xf) else f"{xf:.1f}"

    # nQ(V) is a 0-1 ratio -- always show one decimal place so "1" doesn't
    # read as a different kind of number than "0.9".
    FORCE_ONE_DECIMAL = {"nQ(V)"}

    # Cohort mean/SD per field, computed once over all respondents in this run,
    # so every profile line can show the person's value against the cohort norm.
    cohort_stats = {}
    for src, _, _, _ in fields_present:
        vals = pd.to_numeric(profiles_table[src], errors="coerce")
        cohort_stats[src] = (vals.mean(), vals.std(ddof=0))

    def qtext(x):
        return f'"{x}"' if x is not None and str(x) != "" else '""'

    written = []
    for _, row in profiles_table.iterrows():
        pid = row["id"]
        out_txt = os.path.join(profiles_dir, f"profile_{pid}.txt")
        lines = [
            f"Your anonymous ID for this exercise (also this file's name): --- {{val = {qtext(row['id'])}}}",
            "",
        ]
        for src, label, definition, blank_after in fields_present:
            mean, sd = cohort_stats[src]
            md = 1 if src in FORCE_ONE_DECIMAL else 0
            text = label if label else definition
            lines.append(
                f"{text}: --- {{val = {fmt_num(row[src], md)}, "
                f"mean = {fmt_num(mean, md)}, s.d. = {fmt_num(sd, md)}}}"
            )
            if blank_after:
                lines.append("")
        if party_cols:
            lines.append("")
            lines.append(f"Party Name: --- {{val = {qtext(row.get('pName', ''))}}}")
            lines.append(f"Party Style: --- {{val = {qtext(row.get('pType', ''))}}}")
            lines.append(
                f"Party Guest Preference: --- {{val = {qtext(row.get('gType1', ''))}, "
                f"{qtext(row.get('gType2', ''))}}}"
            )
        with open(out_txt, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        written.append(out_txt)

    with zipfile.ZipFile(os.path.join(args.outdir, "profiles_txt.zip"), "w", zipfile.ZIP_DEFLATED) as zf:
        for p in written:
            zf.write(p, arcname=os.path.basename(p))

    # -----------------------------
    # 8) Data dictionary
    # -----------------------------
    scope_note = (
        "Full-cohort scope: indegrees count picks from ALL cohort members named in --roster, "
        "including those who haven't yet responded (see roster_full_cohort.csv for who's who)."
        if roster_names else
        "Respondent-only scope: only picks between two survey respondents are counted. "
        "Picks toward a cohort member who hasn't responded are not reflected in indegree/popularity scores."
    )
    dd = f"""# Data Dictionary

Plain-English guide to the anonymized outputs you'll receive.

**Network scope for this run:** {scope_note}

**Row filtering for this run:** preview/blank rows {'kept' if args.include_preview else 'dropped'};
unfinished (Finished=False) submissions {'kept' if args.include_unfinished else 'dropped'}.

## File: `pp_pick_popularity_all_scored_nq_v.csv`
Per-respondent scores for how widely-picked your picks are, by network. Z-scores are centered at 0; **nQ** columns rescale Z to mean 100, SD 15.

- **id** -- Anonymized respondent id.
- **score_d / z_d / nQ(O)** -- Design (Opportunity).
- **score_l / z_l / nQ(I)** -- Lobbying (Influence).
- **score_i / z_i / nQ(C)** -- Implementation (Cohesion).
- **score_f / z_f / nQ(F)** -- Friendship (separate).
- **score_a / z_a / nQ(A)** -- Advice (separate).
- **score_dli_unique / z_score_dli_unique / nQ(*)_unique** -- D+L+I (unique alters).
- **score_dli_dupe / z_score_dli_dupe / nQ(*)_dupe** -- D+L+I (duplicates allowed).
- **k_d, k_l, k_i** -- Picks per task.
- **k_dli_total, k_dli_unique, nQ(V)** -- Variability across D/L/I where nQ(V) = unique / total.

## File: `pp_indegree_summary_anonymized.csv`
In-degree counts (and normalized rates) by network.

## File: `pp_id_email_map.csv`
- **id** -- Anonymized ID
- **email** -- Respondent email (for mail merge)
- **fName**, **lName** -- Names from roster (for personalization)

## File: `roster_full_cohort.csv` (only when --roster is used)
- **id** -- Anonymized ID (respondent ids match the main outputs; non-respondents are prefixed `nr`)
- **full_name** -- Name as given in the roster file
- **is_respondent** -- Whether this person completed the survey

## Folder: `network_diagrams/`
One PNG per pick network (design, lobbying, implementation, friendship, advice). Blue nodes are
respondents; grey nodes (full-cohort scope only) are cohort members who were picked but haven't
responded themselves. Node size scales with indegree (how often that person was picked). Shaded
outlines group people into cliques/subgroups detected from the pick pattern (Louvain communities).

## Folder: `network_diagrams_3d/` (skip with `--no-3d`)
One interactive HTML file per pick network -- the same layout as the matching PNG above, but
projected into 3D with height (z-axis) showing indegree, so popular people rise as towers above
the floor. Open any of these `.html` files in a browser: drag to rotate, scroll to zoom, hover a
node for details, click a legend entry to show/hide respondents or non-respondents. Each file is
fully self-contained -- the charting library (Plotly.js, MIT licensed) is inlined directly inside
it, not loaded from the internet -- so you can send a single `.html` file to one person and it
will open and work with no other files and no internet connection needed. (That self-sufficiency
is also why each file is a few MB: the library is duplicated inside every one of them.)

## Folder: `profiles/` (also zipped as `profiles_txt.zip`)
One plain-text file per respondent (`profile_<id>.txt`) -- this is what you mail-merge back to each
person via `pp_id_email_map.csv`. Each line is one metric, self-explanatory with no need to
cross-reference this data dictionary:

    <label>: --- {{val = <their value>, mean = <cohort mean>, s.d. = <cohort s.d.>}}

Text values (id, the party answers) are quoted and have no mean/s.d. -- those are categorical,
not scored, e.g. `Party Style: --- {{val = "MIXER. For 20-30; ..."}}`.

mean/s.d. are computed over this run's respondents. Friendship/Advice popularity and all "times
picked" indegree counts are intentionally left out of this participant-facing file (kept in the
cohort-level CSVs above, not per-person). "Perspicacity" labels the nQ(O)/nQ(I)/nQ(C)/nQ(*)_unique
popularity constructs; "Flexibility(Overall)" labels nQ(V), the pick-variability score. Each
profile also closes with that person's own launch-party answers (name, style, first/second-choice
guest preference) verbatim from the survey -- plain text, no val/mean/s.d. triplet, since these
are categorical, not scored.
"""
    with open(os.path.join(args.outdir, "DATA_DICTIONARY.md"), "w", encoding="utf-8") as f:
        f.write(dd)

    # -----------------------------
    # 9) Formatted exports for presentation
    # -----------------------------
    nq_fmt = full.copy()
    nq_cols = [c for c in nq_fmt.columns if c.startswith("nQ(")]
    for c in nq_cols:
        nq_fmt[c] = pd.to_numeric(nq_fmt[c], errors="coerce").round().astype("Int64")
    nq_fmt.to_csv(os.path.join(args.outdir, "pp_pick_popularity_all_scored_nq_v_fmt.csv"), index=False)

    vars_nq = ["nQ(C)", "nQ(I)", "nQ(O)", "nQ(V)", "nQ(*)_unique", "nQ(*)_dupe"]
    vars_deg = ["in_dPicks", "in_lPicks", "in_iPicks", "in_fPicks", "in_anyPick"]
    summary_rows = []
    for col in vars_nq + vars_deg:
        s = full[col] if col in full.columns else indeg_summary[col]
        s = pd.to_numeric(s, errors="coerce")
        summary_rows.append({
            "variable": col,
            "mean": float(s.mean()) if s.notna().any() else float("nan"),
            "std": float(s.std(ddof=0)) if s.notna().any() else float("nan"),
            "max": float(s.max()) if s.notna().any() else float("nan"),
            "n": int(s.notna().sum())
        })
    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(os.path.join(args.outdir, "cohort_summary_stats.csv"), index=False)

    s_fmt = summary_df.copy()
    for c in ["mean", "std", "max"]:
        s_fmt[c] = s_fmt[c].apply(lambda x: f"{x:.1f}" if pd.notna(x) else "")
    s_fmt.to_csv(os.path.join(args.outdir, "cohort_summary_stats_fmt.csv"), index=False)

    corr_vars = ["nQ(C)", "nQ(I)", "nQ(O)", "in_dPicks", "in_lPicks", "in_iPicks", "in_fPicks", "in_anyPick"]
    corr_df = pd.concat([full[["nQ(C)", "nQ(I)", "nQ(O)"]], indeg_summary[corr_vars[3:]]], axis=1)
    corr_df = corr_df.apply(pd.to_numeric, errors="coerce")
    corr_mat = corr_df[corr_vars].corr(method="pearson")
    corr_mat.to_csv(os.path.join(args.outdir, "cohort_correlation_table.csv"))
    corr_fmt = corr_mat.map(lambda x: f"{x:.3f}")
    corr_fmt.to_csv(os.path.join(args.outdir, "cohort_correlation_table_fmt.csv"))

    def _simple_r2_pair(x_name, y_name):
        x = pd.to_numeric(indeg_summary[x_name], errors="coerce")
        y = pd.to_numeric(indeg_summary[y_name], errors="coerce")
        m = x.notna() & y.notna()
        if m.sum() < 2:
            return float("nan"), float("nan"), float("nan")
        r = np.corrcoef(x[m], y[m])[0, 1]
        r2 = float(r * r)
        coeffs = np.polyfit(x[m], y[m], deg=1)
        return r2, float(coeffs[0]), float(coeffs[1])

    r2_rows = []
    for x_name, y_name in [("in_fPicks", "in_dPicks"), ("in_fPicks", "in_lPicks"), ("in_fPicks", "in_iPicks")]:
        r2, slope, intercept = _simple_r2_pair(x_name, y_name)
        r2_rows.append({"x": x_name, "y": y_name, "R_squared": r2, "slope": slope, "intercept": intercept})
    r2_df = pd.DataFrame(r2_rows)
    r2_df.to_csv(os.path.join(args.outdir, "cohort_regression_r2.csv"), index=False)

    r2_fmt = r2_df.copy()
    for c in ["R_squared", "slope", "intercept"]:
        r2_fmt[c] = r2_fmt[c].apply(lambda x: f"{x:.3f}" if pd.notna(x) else "")
    r2_fmt.to_csv(os.path.join(args.outdir, "cohort_regression_r2_fmt.csv"), index=False)

    # -----------------------------
    # 10) Optional: extra cohort charts + slide deck
    # -----------------------------
    if args.cohort_pack:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_pdf import PdfPages
        from mpl_toolkits.mplot3d import Axes3D  # noqa: F401

        figs = []

        fig = plt.figure(figsize=(7, 6))
        ax = fig.add_subplot(111, projection="3d")
        X = pd.to_numeric(full["nQ(C)"], errors="coerce")
        Y = pd.to_numeric(full["nQ(I)"], errors="coerce")
        Z = pd.to_numeric(full["nQ(O)"], errors="coerce")
        ax.scatter(X, Y, Z)
        ax.set_xlabel("nQ(C) — Implementation")
        ax.set_ylabel("nQ(I) — Lobbying")
        ax.set_zlabel("nQ(O) — Design")
        ax.set_title("3D scatter: nQ(C), nQ(I), nQ(O)")
        f_3d = os.path.join(args.outdir, "scatter3d_nQ_C_I_O.png")
        plt.tight_layout(); plt.savefig(f_3d, dpi=200); plt.close(fig)
        figs.append(f_3d)

        for y in ["in_dPicks", "in_lPicks", "in_iPicks"]:
            x = pd.to_numeric(indeg_summary["in_fPicks"], errors="coerce")
            yy = pd.to_numeric(indeg_summary[y], errors="coerce")
            m = x.notna() & yy.notna()
            fig = plt.figure(figsize=(6, 5))
            ax = fig.add_subplot(111)
            ax.scatter(x[m], yy[m])
            if m.sum() >= 2:
                coeffs = np.polyfit(x[m], yy[m], deg=1)
                xp = np.linspace(x[m].min(), x[m].max(), 100)
                yp = coeffs[0] * xp + coeffs[1]
                ax.plot(xp, yp)
                r = np.corrcoef(x[m], yy[m])[0, 1]
                ax.set_title(f"{y} vs in_fPicks (R²={r*r:.3f})")
            else:
                ax.set_title(f"{y} vs in_fPicks")
            ax.set_xlabel("in_fPicks (friendship popularity)")
            ax.set_ylabel(y)
            fp = os.path.join(args.outdir, f"scatter_{y}_vs_in_fPicks.png")
            plt.tight_layout(); plt.savefig(fp, dpi=200); plt.close(fig)
            figs.append(fp)

        fig = plt.figure(figsize=(7, 6))
        ax = fig.add_subplot(111)
        im = ax.imshow(corr_mat.values, aspect="auto")
        ax.set_xticks(range(len(corr_mat.columns)))
        ax.set_xticklabels(corr_mat.columns, rotation=45, ha="right")
        ax.set_yticks(range(len(corr_mat.index)))
        ax.set_yticklabels(corr_mat.index)
        ax.set_title("Correlation matrix (Pearson)")
        heatmap_path = os.path.join(args.outdir, "correlation_heatmap.png")
        plt.tight_layout(); plt.savefig(heatmap_path, dpi=200); plt.close(fig)
        figs.append(heatmap_path)

        slides_path = os.path.join(args.outdir, "cohort_slides.pdf")
        with PdfPages(slides_path) as pdf:
            fig = plt.figure(figsize=(10, 7)); plt.axis('off')
            plt.text(0.5, 0.82, "People Picking — Cohort Overview", ha="center", va="center", fontsize=22)
            plt.text(0.5, 0.64, "What you'll see", ha="center", va="center", fontsize=14)
            bullet = (
                "• nQ scores for task teams (O/I/C) + DLI omnibus\n"
                "• Variability nQ(V)\n"
                "• Where the cohort clusters in 3D (O/I/C)\n"
                "• Popularity vs task-fit (R² of in_f vs in_{d,l,i})"
            )
            plt.text(0.5, 0.48, bullet, ha="center", va="center", fontsize=12)
            pdf.savefig(fig); plt.close(fig)

            fig = plt.figure(figsize=(10, 7)); plt.axis('off')
            plt.text(0.5, 0.9, "Summary (means / max)", ha="center", va="center", fontsize=18)
            keep = ["nQ(C)", "nQ(I)", "nQ(O)", "nQ(V)", "nQ(*)_unique", "nQ(*)_dupe"]
            tbl = s_fmt[s_fmt["variable"].isin(keep)]
            y = 0.8
            for _, row in tbl.iterrows():
                plt.text(0.15, y, f"{row['variable']}", fontsize=12)
                plt.text(0.5, y, f"mean={row['mean']}, std={row['std']}", fontsize=12)
                plt.text(0.85, y, f"max={row['max']}", fontsize=12, ha="right")
                y -= 0.06
            pdf.savefig(fig); plt.close(fig)

            for p in figs:
                img = plt.imread(p)
                fig = plt.figure(figsize=(10, 7))
                ax = fig.add_subplot(111)
                ax.imshow(img); ax.axis('off')
                pdf.savefig(fig); plt.close(fig)

    print("Done. Outputs written to:", args.outdir)


if __name__ == "__main__":
    main()
