"""The graph read back from the cache: what GET /api/v1/graph/data returns and GET /api/v1/graph
draws. Never hydrates and never calls SPARQL.

Public API for library users (with dig.run_dig, graph_store.GraphStore, hydration.*): keep these
signatures and the payload shape stable, or bump the package's major version.
"""
from typing import Dict, List, Set, Tuple

from .category import wikipedia_entity_url
from .graph_store import GraphStore


def node_summary(store: GraphStore, uri: str) -> Dict[str, object]:
    attrs = store.graph.nodes[uri]

    return {
        "id": uri,
        "entity_url": wikipedia_entity_url(uri),
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
        # Raw Wikipedia intro text, NOT reviewed: consumers must not show it
        # to end users as-is. None when the page has none, is a category, or
        # hasn't been hydrated yet (abstract_hydrated tells those apart).
        "abstract": attrs.get("abstract"),
        "abstract_fetched_at": attrs.get("abstract_fetched_at"),
        "abstract_hydrated": attrs.get("abstract_hydrated", False),
    }


def build_graph_data(
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
            node_summary(store, uri)
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
