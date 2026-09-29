"""
GET /api/v1/graph

Returns a self-contained HTML visualization of the currently cached graph.

This endpoint:
- NEVER hydrates the graph
- NEVER calls SPARQL
- ONLY reads the GraphStore currently in memory
- Returns standalone HTML that can be copied into an online HTML editor

Query params:
    root  - category/entity local name, e.g.
            "Category:Food_ingredients"

    depth - number of cached child levels to walk.
            Default: 6.
            Clamped to MAX_DEPTH_HARD_CAP.

The generated visualization is a hierarchical tree.

Features:
- Category/page distinction
- Expand/collapse nodes
- Search
- Zoom
- Pan
- Node metadata tooltip
- Parent -> child relationships
"""

from typing import Dict, List, Set, Tuple
from html import escape
import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from ..config import settings
from ..graph_store import GraphStore


router = APIRouter(prefix="/api/v1", tags=["graph"])


def _node_summary(store: GraphStore, uri: str) -> Dict[str, object]:
    attrs = store.graph.nodes[uri]

    return {
        "id": uri,
        "type": "category" if uri.startswith("Category:") else "page",
        "structure_hydrated": attrs.get(
            "structure_hydrated",
            False,
        ),
        "synonyms_hydrated": attrs.get(
            "synonyms_hydrated",
            False,
        ),
        # [[text, source_tag], ...] e.g. [["chicken", "rdfs:label"], ...] —
        # store.synonyms() already defaults to [] for a missing node/attr.
        "synonyms": store.synonyms(uri),
    }


def _build_graph_data(
    store: GraphStore,
    root: str,
    depth: int,
) -> Dict[str, object]:

    if not store.has_node(root):
        return {
            "root": root,
            "depth": depth,
            "nodes": [],
            "edges": [],
            "note": (
                "Root not found in the currently hydrated graph. "
                "Try /hydrate-category first."
            ),
        }

    visited: Set[str] = {root}

    edges: List[Tuple[str, str]] = []

    frontier = [root]

    for _level in range(depth):

        next_frontier: List[str] = []

        for node in frontier:

            for child in store.children(node):

                edges.append(
                    (
                        node,
                        child,
                    )
                )

                if child not in visited:

                    visited.add(child)

                    next_frontier.append(child)

        frontier = next_frontier

        if not frontier:
            break

    return {
        "root": root,
        "depth": depth,
        "nodes": [
            _node_summary(store, uri)
            for uri in visited
        ],
        "edges": [
            {
                "source": source,
                "target": target,
            }
            for source, target in edges
        ],
    }


def _build_html(graph_data: Dict[str, object]) -> str:
    """
    Create a completely standalone HTML document.

    No external libraries or CDN dependencies are used.
    """

    graph_json = json.dumps(
        graph_data,
        ensure_ascii=False,
    ).replace("</", "<\\/")

    root = escape(
        str(graph_data.get("root", ""))
    )

    node_count = len(
        graph_data.get("nodes", [])
    )

    edge_count = len(
        graph_data.get("edges", [])
    )

    depth = graph_data.get(
        "depth",
        0,
    )

    return f"""<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
/>

<title>Graph Viewer - {root}</title>

<style>

* {{
    box-sizing: border-box;
}}

html,
body {{
    margin: 0;
    width: 100%;
    height: 100%;
    overflow: hidden;
    font-family:
        Arial,
        Helvetica,
        sans-serif;
    background: #f5f6f8;
}}

#toolbar {{

    height: 64px;

    display: flex;

    align-items: center;

    gap: 14px;

    padding: 0 18px;

    background: #ffffff;

    border-bottom:
        1px solid #dcdfe4;

    box-shadow:
        0 1px 5px
        rgba(0, 0, 0, 0.08);

    position: relative;

    z-index: 100;
}}

#title {{

    font-size: 16px;

    font-weight: 600;

    white-space: nowrap;

    max-width: 320px;

    overflow: hidden;

    text-overflow: ellipsis;
}}

#stats {{

    color: #666;

    font-size: 13px;

    white-space: nowrap;
}}

#search {{

    margin-left: auto;

    width: 280px;

    height: 36px;

    padding:
        0 12px;

    border:
        1px solid #cfd3d8;

    border-radius: 6px;

    outline: none;

    font-size: 13px;
}}

#search:focus {{

    border-color: #4f81bd;

    box-shadow:
        0 0 0 2px
        rgba(79, 129, 189, 0.12);
}}

button {{

    height: 36px;

    padding:
        0 12px;

    border:
        1px solid #cfd3d8;

    background: white;

    border-radius: 6px;

    cursor: pointer;

    font-size: 13px;
}}

button:hover {{

    background: #f1f3f5;
}}

#graph-container {{

    position: relative;

    width: 100%;

    height:
        calc(100vh - 64px);

    overflow: hidden;

    background:
        radial-gradient(
            circle at 1px 1px,
            #dfe2e6 1px,
            transparent 1px
        );

    background-size:
        20px 20px;
}}

#graph {{

    width: 100%;

    height: 100%;

    cursor: grab;

    user-select: none;
}}

#graph:active {{

    cursor: grabbing;
}}

.edge {{

    stroke: #aeb4bb;

    stroke-width: 1.3;

    fill: none;

    marker-end:
        url(#arrow);
}}

.category-node {{

    fill: #4f81bd;

    stroke: #28527a;

    stroke-width: 2;

    cursor: pointer;
}}

.page-node {{

    fill: #70ad47;

    stroke: #477a2d;

    stroke-width: 2;

    cursor: pointer;
}}

.node-label {{

    font-size: 11px;

    fill: #222;

    pointer-events: none;
}}

.node-label.root {{

    font-weight: bold;

    font-size: 13px;
}}

#legend {{

    position: absolute;

    right: 18px;

    top: 18px;

    background: white;

    border:
        1px solid #ddd;

    border-radius: 6px;

    padding:
        10px 13px;

    box-shadow:
        0 2px 8px
        rgba(0,0,0,0.10);

    font-size: 12px;
}}

.legend-item {{

    display: flex;

    align-items: center;

    gap: 7px;

    margin-bottom: 6px;
}}

.legend-item:last-child {{

    margin-bottom: 0;
}}

.legend-circle {{

    width: 12px;

    height: 12px;

    border-radius: 50%;
}}

.category-legend {{

    background: #4f81bd;
}}

.page-legend {{

    background: #70ad47;
}}

#empty {{

    position: absolute;

    inset: 0;

    display: flex;

    align-items: center;

    justify-content: center;

    color: #666;

    font-size: 15px;
}}

.search-highlight {{

    stroke: #e67e22 !important;

    stroke-width: 4 !important;
}}

</style>

</head>


<body>


<div id="toolbar">

    <div id="title">
        Graph: {root}
    </div>

    <div id="stats">
        Nodes: {node_count}
        &nbsp; | &nbsp;
        Edges: {edge_count}
        &nbsp; | &nbsp;
        Depth: {depth}
    </div>

    <input
        id="search"
        type="text"
        placeholder="Search nodes..."
    />

    <button id="expandAll">
        Expand All
    </button>

    <button id="collapseAll">
        Collapse
    </button>

    <button id="resetView">
        Reset
    </button>

</div>


<div id="graph-container">

    <svg
        id="graph"
        viewBox="0 0 1600 900"
    >

        <defs>

            <marker
                id="arrow"
                viewBox="0 0 10 10"
                refX="10"
                refY="5"
                markerWidth="7"
                markerHeight="7"
                orient="auto"
            >

                <path
                    d="
                        M 0 0
                        L 10 5
                        L 0 10
                        Z
                    "
                    fill="#aeb4bb"
                />

            </marker>

        </defs>


        <g id="viewport">

            <g id="edges"></g>

            <g id="nodes"></g>

        </g>

    </svg>


    <div id="legend">

        <div class="legend-item">

            <div
                class="legend-circle category-legend"
            ></div>

            Category

        </div>


        <div class="legend-item">

            <div
                class="legend-circle page-legend"
            ></div>

            Page / Article

        </div>

    </div>


    <div
        id="empty"
        style="display:none;"
    >
        No graph data available.
    </div>

</div>


<script>

const graphData = {graph_json};


const svg =
    document.getElementById("graph");

const viewport =
    document.getElementById("viewport");

const edgesGroup =
    document.getElementById("edges");

const nodesGroup =
    document.getElementById("nodes");


const WIDTH = 1600;

const HEIGHT = 900;

const nodeMap =
    new Map(
        graphData.nodes.map(
            node => [node.id, node]
        )
    );


const childrenMap =
    new Map();


const parentMap =
    new Map();


graphData.nodes.forEach(
    node => {{
        childrenMap.set(
            node.id,
            []
        );
    }}
);


graphData.edges.forEach(
    edge => {{

        if (!childrenMap.has(edge.source)) {{

            childrenMap.set(
                edge.source,
                []
            );
        }}

        childrenMap
            .get(edge.source)
            .push(edge.target);


        parentMap.set(
            edge.target,
            edge.source
        );
    }}
);


let collapsed =
    new Set();


let positions =
    new Map();


let scale = 1;

let translateX = 0;

let translateY = 0;

if (
    !graphData.nodes ||
    graphData.nodes.length === 0
) {{

    document
        .getElementById("empty")
        .style.display = "flex";

}} else {{

    initialize();

}}


function initialize() {{

    /*
     * Initially collapse very large branches.
     *
     * The root remains expanded.
     */

    const root =
        graphData.root;

    initializeCollapsedState(
        root
    );

    renderTree();

}}

function initializeCollapsedState(
    root
) {{

    collapsed.clear();


    /*
     * Keep the first two levels visible.
     *
     * Deeper nodes can be expanded
     * by clicking their parent.
     */

    function walk(
        nodeId,
        level
    ) {{

        const children =
            childrenMap.get(nodeId) || [];


        if (
            level >= 2 &&
            children.length > 0
        ) {{

            collapsed.add(
                nodeId
            );

            return;
        }}


        children.forEach(
            child => {{
                walk(
                    child,
                    level + 1
                );
            }}
        );
    }}


    walk(
        root,
        0
    );
}}

function getVisibleNodes() {{

    const visible = [];

    function walk(
        nodeId,
        depth,
        parent
    ) {{

        const node =
            nodeMap.get(nodeId);

        if (!node) {{
            return;
        }}


        visible.push({{
            ...node,
            depth,
            parent
        }});


        if (
            collapsed.has(nodeId)
        ) {{
            return;
        }}


        const children =
            childrenMap.get(nodeId) || [];


        children.forEach(
            child => {{
                walk(
                    child,
                    depth + 1,
                    nodeId
                );
            }}
        );
    }}


    walk(
        graphData.root,
        0,
        null
    );


    return visible;
}}

function calculateLayout(
    visibleNodes
) {{

    const levels =
        new Map();


    visibleNodes.forEach(
        node => {{

            if (!levels.has(node.depth)) {{

                levels.set(
                    node.depth,
                    []
                );
            }}

            levels
                .get(node.depth)
                .push(node);
        }}
    );


    const horizontalGap = 230;

    const verticalGap = 55;


    levels.forEach(
        (nodes, depth) => {{

            nodes.forEach(
                (node, index) => {{

                    const x =
                        120 +
                        depth *
                        horizontalGap;


                    const y =
                        100 +
                        index *
                        verticalGap;


                    positions.set(
                        node.id,
                        {{
                            x,
                            y
                        }}
                    );
                }}
            );
        }}
    );


    /*
     * Center each parent relative
     * to its visible children.
     *
     * Run several lightweight passes.
     */

    for (
        let pass = 0;
        pass < 3;
        pass++
    ) {{

        for (
            let i =
                visibleNodes.length - 1;
            i >= 0;
            i--
        ) {{

            const node =
                visibleNodes[i];


            const children =
                childrenMap
                    .get(node.id) || [];


            const visibleChildren =
                children
                    .filter(
                        child =>
                            positions.has(child)
                    );


            if (
                visibleChildren.length === 0
            ) {{
                continue;
            }}


            const childPositions =
                visibleChildren.map(
                    child =>
                        positions.get(child)
                );


            const averageY =
                childPositions.reduce(
                    (sum, pos) =>
                        sum + pos.y,
                    0
                ) /
                childPositions.length;


            const current =
                positions.get(
                    node.id
                );


            if (current) {{

                current.y =
                    averageY;
            }}
        }}
    }}
}}


function renderTree() {{

    edgesGroup.innerHTML = "";

    nodesGroup.innerHTML = "";

    positions.clear();


    const visibleNodes =
        getVisibleNodes();


    calculateLayout(
        visibleNodes
    );


    const visibleIds =
        new Set(
            visibleNodes.map(
                node => node.id
            )
        );


    /*
     * Edges
     */

    graphData.edges.forEach(
        edge => {{

            if (
                !visibleIds.has(
                    edge.source
                ) ||
                !visibleIds.has(
                    edge.target
                )
            ) {{
                return;
            }}


            const source =
                positions.get(
                    edge.source
                );

            const target =
                positions.get(
                    edge.target
                );


            if (
                !source ||
                !target
            ) {{
                return;
            }}


            const line =
                document.createElementNS(
                    "http://www.w3.org/2000/svg",
                    "line"
                );


            line.classList.add(
                "edge"
            );


            line.setAttribute(
                "x1",
                source.x
            );

            line.setAttribute(
                "y1",
                source.y
            );

            line.setAttribute(
                "x2",
                target.x
            );

            line.setAttribute(
                "y2",
                target.y
            );


            edgesGroup.appendChild(
                line
            );
        }}
    );


    /*
     * Nodes
     */

    visibleNodes.forEach(
        node => {{

            const position =
                positions.get(
                    node.id
                );


            if (!position) {{
                return;
            }}


            const group =
                document.createElementNS(
                    "http://www.w3.org/2000/svg",
                    "g"
                );


            group.dataset.id =
                node.id;


            group.setAttribute(
                "transform",
                `translate(
                    ${{position.x}},
                    ${{position.y}}
                )`
            );


            /*
             * Node circle
             */

            const circle =
                document.createElementNS(
                    "http://www.w3.org/2000/svg",
                    "circle"
                );


            const isRoot =
                node.id ===
                graphData.root;


            const hasChildren =
                (
                    childrenMap
                        .get(node.id) || []
                ).length > 0;


            circle.setAttribute(
                "r",
                isRoot
                    ? "17"
                    : "12"
            );


            circle.classList.add(
                node.type === "category"
                    ? "category-node"
                    : "page-node"
            );


            /*
             * Highlight root.
             */

            if (isRoot) {{

                circle.classList.add(
                    "root"
                );
            }}


            /*
             * Tooltip
             */

            const title =
                document.createElementNS(
                    "http://www.w3.org/2000/svg",
                    "title"
                );


            const synonymsText =
                (node.synonyms || [])
                    .map(pair => pair[0] + " (" + pair[1] + ")")
                    .join(", ") ||
                "(none yet)";

            title.textContent =
                node.id +
                "\\nType: " +
                node.type +
                "\\nStructure hydrated: " +
                node.structure_hydrated +
                "\\nSynonyms hydrated: " +
                node.synonyms_hydrated +
                "\\nSynonyms: " +
                synonymsText +
                "\\nChildren: " +
                (
                    childrenMap
                        .get(node.id) || []
                ).length;


            circle.appendChild(
                title
            );


            /*
             * Label
             */

            const label =
                document.createElementNS(
                    "http://www.w3.org/2000/svg",
                    "text"
                );


            label.classList.add(
                "node-label"
            );


            if (isRoot) {{

                label.classList.add(
                    "root"
                );
            }}


            label.setAttribute(
                "x",
                "20"
            );


            label.setAttribute(
                "y",
                "4"
            );


            label.textContent =
                cleanLabel(
                    node.id
                );


            /*
             * Expand/collapse indicator.
             */

            if (hasChildren) {{

                const indicator =
                    document.createElementNS(
                        "http://www.w3.org/2000/svg",
                        "text"
                    );


                indicator.setAttribute(
                    "x",
                    "-5"
                );


                indicator.setAttribute(
                    "y",
                    "4"
                );


                indicator.setAttribute(
                    "text-anchor",
                    "middle"
                );


                indicator.setAttribute(
                    "font-size",
                    "10"
                );


                indicator.textContent =
                    collapsed.has(node.id)
                        ? "+"
                        : "−";


                group.appendChild(
                    indicator
                );
            }}


            group.appendChild(
                circle
            );


            group.appendChild(
                label
            );


            /*
             * Clicking a node toggles
             * its children.
             */

            group.addEventListener(
                "click",
                event => {{

                    event.stopPropagation();

                    if (hasChildren) {{

                        toggleNode(
                            node.id
                        );
                    }}
                }}
            );


            nodesGroup.appendChild(
                group
            );
        }}
    );
}}

function toggleNode(
    nodeId
) {{

    if (
        collapsed.has(nodeId)
    ) {{

        collapsed.delete(
            nodeId
        );

    }} else {{

        collapsed.add(
            nodeId
        );
    }}


    renderTree();
}}


document
    .getElementById("expandAll")
    .addEventListener(
        "click",
        () => {{

            collapsed.clear();

            renderTree();
        }}
    );

document
    .getElementById("collapseAll")
    .addEventListener(
        "click",
        () => {{

            collapsed.clear();


            graphData.nodes.forEach(
                node => {{

                    const children =
                        childrenMap
                            .get(node.id) || [];


                    if (
                        children.length > 0
                    ) {{

                        collapsed.add(
                            node.id
                        );
                    }}
                }}
            );


            /*
             * Always keep root visible.
             */

            collapsed.delete(
                graphData.root
            );


            renderTree();
        }}
    );

document
    .getElementById("search")
    .addEventListener(
        "input",
        event => {{

            const query =
                event.target.value
                    .trim()
                    .toLowerCase();


            document
                .querySelectorAll(
                    "#nodes circle"
                )
                .forEach(
                    circle => {{

                        circle.classList
                            .remove(
                                "search-highlight"
                            );
                    }}
                );


            if (!query) {{
                return;
            }}


            graphData.nodes.forEach(
                node => {{

                    if (
                        node.id
                            .toLowerCase()
                            .includes(query)
                    ) {{

                        const circle =
                            document.querySelector(
                                `g[data-id="${{
                                    cssEscape(
                                        node.id
                                    )
                                }}"] circle`
                            );


                        if (circle) {{

                            circle.classList
                                .add(
                                    "search-highlight"
                                );
                        }}
                    }}
                }}
            );
        }}
    );

function cssEscape(
    value
) {{

    if (
        window.CSS &&
        CSS.escape
    ) {{

        return CSS.escape(
            value
        );
    }}


    return value.replace(
        /["\\\\]/g,
        "\\\\$&"
    );
}}

function cleanLabel(
    id
) {{

    return id
        .replace(
            /^Category:/,
            ""
        )
        .replace(
            /_/g,
            " "
        );
}}


svg.addEventListener(
    "wheel",
    event => {{

        event.preventDefault();


        const factor =
            event.deltaY < 0
                ? 1.12
                : 0.89;


        scale *= factor;


        scale =
            Math.max(
                0.15,
                Math.min(
                    5,
                    scale
                )
            );


        updateTransform();
    }},
    {{
        passive: false
    }}
);


let panning = false;

let panStartX = 0;

let panStartY = 0;


svg.addEventListener(
    "mousedown",
    event => {{

        /*
         * Only start panning when
         * clicking empty SVG space.
         */

        if (
            event.target !== svg
        ) {{
            return;
        }}


        panning = true;


        panStartX =
            event.clientX;

        panStartY =
            event.clientY;
    }}
);


window.addEventListener(
    "mousemove",
    event => {{

        if (!panning) {{
            return;
        }}


        translateX +=
            event.clientX -
            panStartX;


        translateY +=
            event.clientY -
            panStartY;


        panStartX =
            event.clientX;

        panStartY =
            event.clientY;


        updateTransform();
    }}
);


window.addEventListener(
    "mouseup",
    () => {{

        panning = false;
    }}
);

document
    .getElementById("resetView")
    .addEventListener(
        "click",
        () => {{

            scale = 1;

            translateX = 0;

            translateY = 0;

            initializeCollapsedState(
                graphData.root
            );

            updateTransform();

            renderTree();
        }}
    );

function updateTransform() {{

    viewport.setAttribute(
        "transform",
        `
            translate(
                ${{translateX}},
                ${{translateY}}
            )
            scale(${{scale}})
        `
    );
}}

</script>

</body>

</html>
"""


@router.get(
    "/graph",
    response_class=HTMLResponse,
)
async def get_graph(
    request: Request,
    root: str = Query(
        ...,
        description=(
            "Category/entity local name to start from, "
            "e.g. 'Category:Food_ingredients'"
        ),
    ),
    depth: int = Query(
        6,
        ge=0,
        description=(
            "How many levels of cached children to walk"
        ),
    ),
):

    store: GraphStore = (
        request.app.state.store
    )


    depth = min(
        depth,
        settings.max_depth_hard_cap,
    )


    graph_data = _build_graph_data(
        store=store,
        root=root,
        depth=depth,
    )


    html = _build_html(
        graph_data
    )


    return HTMLResponse(
        content=html,
        media_type="text/html",
    )