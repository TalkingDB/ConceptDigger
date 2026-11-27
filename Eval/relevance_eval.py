"""
Relevance eval for a dug category graph.

Takes the JSON produced by the graph-data endpoint (`{"root", "depth", "nodes", "edges"}`)
or by POST /api/v1/dig (`{"data": [...]}`), asks Claude whether each extracted node is
relevant to the root category's subject, and writes a timestamped report to reports/.

    python -m Eval.relevance_eval path/to/Flavors.json
    python -m Eval.relevance_eval path/to/Flavors.json --dry-run      # no API calls

The report shows the share of irrelevant nodes overall, for pages and categories,
by depth, and by top-level branch (the root's direct subcategory a node was reached
through), so you can see where the dig drifts off-topic.

Verdicts are cached in reports/.cache/, so re-running on the same data does not call
the API again for nodes already judged. The judge is an OpenAI model: put OPENAI_API_KEY
in the repo's .env (loaded automatically) or the environment, never on the command line,
and `pip install -r requirements-evals.txt`.
Pick the model with --model or OPENAI_MODEL.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"
DEFAULT_MODEL = "gpt-4.1"  # overridden by OPENAI_MODEL or --model
ABSTRACT_CHARS = 600  # first part of the abstract is enough to tell what an entity is

VERDICTS = ("relevant", "borderline", "irrelevant")


@dataclass
class Node:
    id: str
    type: str  # "page" or "category"
    abstract: Optional[str] = None
    depth: Optional[int] = None  # shortest distance from the root, in edges
    branch: Optional[str] = None  # root's direct child the shortest path goes through
    path: List[str] = field(default_factory=list)


@dataclass
class Verdict:
    verdict: str
    reason: str


Judge = Callable[[str, List[Node]], Dict[str, Verdict]]


# --------------------------------------------------------------------------- loading


def load_dotenv(path: Path) -> None:
    """Load KEY=value lines from `path` into os.environ; variables already set win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and not os.environ.get(key):
            os.environ[key] = value


def load_graph(path: Path) -> tuple[str, List[Node]]:
    """Read graph-data or /dig output and return (root, nodes) with depth/branch/path set."""
    payload = json.loads(path.read_text(encoding="utf-8"))

    if "nodes" in payload:
        root = payload["root"]
        nodes = {
            n["id"]: Node(id=n["id"], type=n.get("type", "page"), abstract=n.get("abstract"))
            for n in payload["nodes"]
        }
        edges = [(e["source"], e["target"]) for e in payload.get("edges", [])]
        _annotate_paths(root, nodes, edges)
        return root, [n for n in nodes.values() if n.id != root]

    if "data" in payload:
        # /dig output has no edges or abstracts: the judge sees only names.
        roots = {d.get("seed_category") for d in payload["data"] if d.get("seed_category")}
        if len(roots) != 1:
            raise SystemExit(f"Expected one seed_category in /dig output, found {sorted(roots)}")
        root = roots.pop()
        nodes = [
            Node(
                id=d["entity"],
                type="category" if d["entity"].startswith("Category:") else "page",
            )
            for d in payload["data"]
            if d["entity"] != root
        ]
        return root, nodes

    raise SystemExit(f"{path}: expected a graph-data ('nodes'/'edges') or /dig ('data') payload")


def _annotate_paths(root: str, nodes: Dict[str, Node], edges: Iterable[tuple[str, str]]) -> None:
    children: Dict[str, List[str]] = defaultdict(list)
    for source, target in edges:
        children[source].append(target)

    parent: Dict[str, Optional[str]] = {root: None}
    queue = deque([root])
    while queue:
        current = queue.popleft()
        for child in children.get(current, []):
            if child not in parent:
                parent[child] = current
                queue.append(child)

    for node_id, node in nodes.items():
        if node_id not in parent or node_id == root:
            continue
        path = []
        cursor: Optional[str] = node_id
        while cursor is not None:
            path.append(cursor)
            cursor = parent[cursor]
        path.reverse()  # root ... node
        node.path = path
        node.depth = len(path) - 1
        node.branch = path[1] if len(path) > 1 else None


# --------------------------------------------------------------------------- judging


def _display_name(entity_id: str) -> str:
    return entity_id.replace("_", " ")


SYSTEM_PROMPT = """You are a strict reviewer. You evaluate the output of a tool that collects Wikipedia entities for a topic by \
walking a Wikipedia category tree. Category trees drift: a few levels down, they often reach pages \
that share a word or a chemical with the topic but are not about it.

For each item, decide whether it belongs in a knowledge base about the given topic.
- "relevant": the item is an instance, kind, component, property, producer or direct study of the topic \
(for "Flavors": a flavoring compound, a sweetener, a flavor of ice cream, a condiment used for taste, \
a flavor company).
- "borderline": closely associated but a stretch (a drink or dish mainly known for one flavor, a brand \
that sells a flavored product).
- "irrelevant": a different subject reached through a shared word or ingredient (for "Flavors": \
textiles, drugs, enzymes, people, films, tanning leather, cellulose plastics).

Judge each item by what it is, using its title and abstract. Category items have no abstract: judge \
what their members would be. Return one verdict for every item id, with a short reason."""


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "reason": {"type": "string"},
                },
                "required": ["id", "verdict", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


def build_user_prompt(topic: str, batch: List[Node]) -> str:
    items = []
    for node in batch:
        item = {"id": node.id, "kind": node.type, "title": _display_name(node.id)}
        if node.abstract:
            item["abstract"] = node.abstract[:ABSTRACT_CHARS]
        items.append(item)
    return f"Topic: {topic}\n\nItems:\n{json.dumps(items, ensure_ascii=False, indent=1)}"


def make_openai_judge(model: str, reasoning_effort: Optional[str] = None) -> Judge:
    from openai import OpenAI  # imported here so --dry-run and the tests do not need the SDK

    client = OpenAI()  # reads OPENAI_API_KEY (and OPENAI_BASE_URL, if set)

    def judge(topic: str, batch: List[Node]) -> Dict[str, Verdict]:
        extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(topic, batch)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "verdicts", "schema": VERDICT_SCHEMA, "strict": True},
            },
            **extra,
        )
        choice = response.choices[0]
        if choice.message.refusal:
            raise RuntimeError(f"Model declined a batch starting at {batch[0].id}")
        if choice.finish_reason == "length":
            raise RuntimeError(f"Response truncated for a batch starting at {batch[0].id}")
        parsed = json.loads(choice.message.content)
        wanted = {n.id for n in batch}
        return {
            v["id"]: Verdict(v["verdict"], v["reason"])
            for v in parsed["verdicts"]
            if v["id"] in wanted
        }

    return judge


def judge_all(
    topic: str,
    nodes: List[Node],
    judge: Judge,
    cache: Dict[str, dict],
    batch_size: int,
    workers: int,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
) -> Dict[str, Verdict]:
    """Judge every node not already in `cache`; returns verdicts for all nodes it could judge."""
    results = {nid: Verdict(**v) for nid, v in cache.items()}
    pending = [n for n in nodes if n.id not in results]
    batches = [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(judge, topic, b): b for b in batches}
        for future in as_completed(futures):
            batch = futures[future]
            try:
                verdicts = future.result()
            except Exception as exc:  # one bad batch should not lose the rest
                print(f"  batch starting at {batch[0].id} failed: {exc}", file=sys.stderr)
                continue
            for nid, verdict in verdicts.items():
                results[nid] = verdict
                cache[nid] = {"verdict": verdict.verdict, "reason": verdict.reason}
            done += 1
            on_progress(done, len(batches))
    return results


# --------------------------------------------------------------------------- scoring


@dataclass
class Tally:
    relevant: int = 0
    borderline: int = 0
    irrelevant: int = 0
    unjudged: int = 0

    def add(self, verdict: Optional[str]) -> None:
        if verdict in VERDICTS:
            setattr(self, verdict, getattr(self, verdict) + 1)
        else:
            self.unjudged += 1

    @property
    def judged(self) -> int:
        return self.relevant + self.borderline + self.irrelevant

    def pct(self, name: str) -> float:
        return 100.0 * getattr(self, name) / self.judged if self.judged else 0.0


def summarise(nodes: List[Node], verdicts: Dict[str, Verdict]) -> dict:
    overall, by_type = Tally(), defaultdict(Tally)
    by_depth, by_branch = defaultdict(Tally), defaultdict(Tally)
    for node in nodes:
        v = verdicts[node.id].verdict if node.id in verdicts else None
        overall.add(v)
        by_type[node.type].add(v)
        by_depth[node.depth if node.depth is not None else "unreachable"].add(v)
        by_branch[node.branch or "(direct member / unknown)"].add(v)
    return {"overall": overall, "by_type": by_type, "by_depth": by_depth, "by_branch": by_branch}


# --------------------------------------------------------------------------- reporting


def _row(label: str, t: Tally) -> str:
    return (
        f"| {label} | {t.judged} | {t.relevant} | {t.borderline} | {t.irrelevant} "
        f"| {t.pct('relevant'):.1f}% | {t.pct('irrelevant'):.1f}% |"
    )


TABLE_HEAD = (
    "| Slice | Judged | Relevant | Borderline | Irrelevant | Relevant % | Irrelevant % |\n"
    "|---|---:|---:|---:|---:|---:|---:|"
)


def render_markdown(
    root: str, source: Path, model: str, timestamp: str, nodes: List[Node],
    verdicts: Dict[str, Verdict], summary: dict,
) -> str:
    overall = summary["overall"]
    lines = [
        f"# Relevance eval: {root}",
        "",
        f"- Generated: {timestamp}",
        f"- Source: `{source.name}`",
        f"- Judge: `{model}`",
        f"- Nodes extracted (excluding root): {len(nodes)}; judged: {overall.judged}; "
        f"not judged: {overall.unjudged}",
        f"- **Irrelevant: {overall.pct('irrelevant'):.1f}%** "
        f"(borderline {overall.pct('borderline'):.1f}%, relevant {overall.pct('relevant'):.1f}%)",
        "",
        "## Summary",
        "",
        TABLE_HEAD,
        _row("All nodes", overall),
    ]
    for kind in sorted(summary["by_type"]):
        lines.append(_row(f"{kind}s", summary["by_type"][kind]))

    lines += ["", "## By depth", "", TABLE_HEAD]
    for depth in sorted(summary["by_depth"], key=lambda d: (isinstance(d, str), d)):
        lines.append(_row(f"depth {depth}", summary["by_depth"][depth]))

    lines += [
        "", "## By top-level branch", "",
        "Branch = the root's direct subcategory on the shortest path to the node. "
        "Sorted by irrelevant count.", "", TABLE_HEAD,
    ]
    branches = sorted(summary["by_branch"].items(), key=lambda kv: -kv[1].irrelevant)
    for branch, tally in branches:
        lines.append(_row(branch, tally))

    irrelevant = [n for n in nodes if n.id in verdicts and verdicts[n.id].verdict == "irrelevant"]
    irrelevant.sort(key=lambda n: (n.depth or 0, " > ".join(n.path)))
    lines += [
        "", f"## Irrelevant nodes ({len(irrelevant)})", "",
        "| Node | Type | Depth | Path from root | Reason |",
        "|---|---|---:|---|---|",
    ]
    for n in irrelevant:
        path = " > ".join(_display_name(p.replace("Category:", "")) for p in n.path[1:-1]) or "-"
        reason = verdicts[n.id].reason.replace("|", "/")
        lines.append(f"| {_display_name(n.id)} | {n.type} | {n.depth} | {path} | {reason} |")
    return "\n".join(lines) + "\n"


def write_csv(path: Path, nodes: List[Node], verdicts: Dict[str, Verdict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "type", "depth", "branch", "verdict", "reason", "path"])
        for n in nodes:
            v = verdicts.get(n.id)
            writer.writerow([
                n.id, n.type, n.depth, n.branch,
                v.verdict if v else "", v.reason if v else "", " > ".join(n.path),
            ])


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text.replace("Category:", "")).strip("_") or "root"


# --------------------------------------------------------------------------- main


def main(argv: Optional[List[str]] = None) -> int:
    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("input", type=Path, help="graph-data or /dig JSON file")
    parser.add_argument("--topic", help="subject to judge against (default: root category name)")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL)
    parser.add_argument(
        "--reasoning-effort", choices=["minimal", "low", "medium", "high"],
        help="only for OpenAI reasoning models; leave unset for others",
    )
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, help="judge only the first N nodes (smoke test)")
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument("--dry-run", action="store_true", help="load and annotate only; no API calls")
    args = parser.parse_args(argv)

    root, nodes = load_graph(args.input)
    if args.limit:
        nodes = nodes[: args.limit]
    topic = args.topic or _display_name(root.replace("Category:", ""))
    pages = sum(n.type == "page" for n in nodes)
    print(f"{root}: {len(nodes)} nodes ({pages} pages, {len(nodes) - pages} categories); topic '{topic}'")

    if args.dry_run:
        batches = -(-len(nodes) // args.batch_size)
        chars = sum(len(build_user_prompt(topic, [n])) for n in nodes)
        print(f"dry run: {batches} batches, ~{chars // 4:,} input tokens of items (plus prompts)")
        return 0

    cache_dir = args.reports_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{_slug(root)}__{_slug(topic)}__{_slug(args.model)}.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    cache = {k: v for k, v in cache.items() if k in {n.id for n in nodes}}

    try:
        verdicts = judge_all(
            topic, nodes, make_openai_judge(args.model, args.reasoning_effort), cache,
            args.batch_size, args.workers,
            on_progress=lambda d, t: print(f"  {d}/{t} batches judged", end="\r"),
        )
    finally:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    print()

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    stem = f"{_slug(root)}_{now.strftime('%Y%m%dT%H%M%SZ')}"
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    summary = summarise(nodes, verdicts)

    md_path = args.reports_dir / f"{stem}.md"
    md_path.write_text(
        render_markdown(root, args.input, args.model, timestamp, nodes, verdicts, summary),
        encoding="utf-8",
    )
    write_csv(args.reports_dir / f"{stem}.csv", nodes, verdicts)

    overall = summary["overall"]
    print(f"irrelevant {overall.pct('irrelevant'):.1f}% | borderline {overall.pct('borderline'):.1f}% "
          f"| relevant {overall.pct('relevant'):.1f}% of {overall.judged} judged")
    if overall.unjudged:
        print(f"{overall.unjudged} nodes not judged (failed batches); re-run to retry them")
    print(f"report: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
