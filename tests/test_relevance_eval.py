"""Offline tests for Eval/relevance_eval.py: a fake judge stands in for the LLM."""
import json

from Eval import relevance_eval as ev

GRAPH = {
    "root": "Category:Flavors",
    "depth": 3,
    "nodes": [
        {"id": "Category:Flavors", "type": "category"},
        {"id": "Category:Sweeteners", "type": "category"},
        {"id": "Category:Cellulose", "type": "category"},
        {"id": "Vanillin", "type": "page", "abstract": "Vanillin is a flavoring."},
        {"id": "Aspartame", "type": "page", "abstract": "Aspartame is a sweetener."},
        {"id": "Rayon", "type": "page", "abstract": "Rayon is a fiber."},
    ],
    "edges": [
        {"source": "Category:Flavors", "target": "Category:Sweeteners"},
        {"source": "Category:Flavors", "target": "Vanillin"},
        {"source": "Category:Sweeteners", "target": "Aspartame"},
        {"source": "Category:Sweeteners", "target": "Category:Cellulose"},
        {"source": "Category:Cellulose", "target": "Rayon"},
    ],
}

IRRELEVANT = {"Rayon", "Category:Cellulose"}


def fake_judge(topic, batch):
    return {
        n.id: ev.Verdict("irrelevant" if n.id in IRRELEVANT else "relevant", "fake")
        for n in batch
    }


def _load(tmp_path):
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(GRAPH), encoding="utf-8")
    return path, *ev.load_graph(path)


def test_load_graph_sets_depth_branch_and_path(tmp_path):
    _, root, nodes = _load(tmp_path)
    by_id = {n.id: n for n in nodes}

    assert root == "Category:Flavors"
    assert "Category:Flavors" not in by_id
    assert by_id["Rayon"].depth == 3
    assert by_id["Rayon"].branch == "Category:Sweeteners"
    assert by_id["Rayon"].path == [
        "Category:Flavors", "Category:Sweeteners", "Category:Cellulose", "Rayon",
    ]
    assert by_id["Vanillin"].depth == 1 and by_id["Vanillin"].branch == "Vanillin"


def test_summary_counts_irrelevant_share(tmp_path):
    _, root, nodes = _load(tmp_path)
    verdicts = ev.judge_all("Flavors", nodes, fake_judge, {}, batch_size=2, workers=1)
    summary = ev.summarise(nodes, verdicts)

    assert summary["overall"].judged == 5
    assert summary["overall"].irrelevant == 2
    assert round(summary["overall"].pct("irrelevant"), 1) == 40.0
    assert summary["by_depth"][3].irrelevant == 1
    assert summary["by_branch"]["Category:Sweeteners"].irrelevant == 2


def test_cached_verdicts_are_not_rejudged(tmp_path):
    _, root, nodes = _load(tmp_path)
    seen = []

    def counting_judge(topic, batch):
        seen.extend(n.id for n in batch)
        return fake_judge(topic, batch)

    cache = {"Vanillin": {"verdict": "relevant", "reason": "cached"}}
    verdicts = ev.judge_all("Flavors", nodes, counting_judge, cache, batch_size=10, workers=1)

    assert "Vanillin" not in seen
    assert verdicts["Vanillin"].reason == "cached"
    assert set(cache) == {n.id for n in nodes}


def test_failed_batch_leaves_nodes_unjudged(tmp_path):
    _, root, nodes = _load(tmp_path)

    def flaky_judge(topic, batch):
        if any(n.id == "Rayon" for n in batch):
            raise RuntimeError("boom")
        return fake_judge(topic, batch)

    verdicts = ev.judge_all("Flavors", nodes, flaky_judge, {}, batch_size=1, workers=1)
    summary = ev.summarise(nodes, verdicts)

    assert "Rayon" not in verdicts
    assert summary["overall"].unjudged == 1


def test_main_writes_timestamped_report(tmp_path, monkeypatch):
    path, _, _ = _load(tmp_path)
    monkeypatch.setattr(ev, "make_openai_judge", lambda model, effort=None: fake_judge)
    reports = tmp_path / "reports"

    assert ev.main([str(path), "--reports-dir", str(reports), "--workers", "1"]) == 0

    md = next(reports.glob("Flavors_*.md")).read_text(encoding="utf-8")
    assert "**Irrelevant: 40.0%**" in md
    assert "| Rayon | page | 3 | Sweeteners > Cellulose |" in md
    assert next(reports.glob("Flavors_*.csv"))
    assert next((reports / ".cache").glob("*.json"))


def test_dig_payload_is_supported(tmp_path):
    path = tmp_path / "dig.json"
    path.write_text(json.dumps({"data": [
        {"entity": "Vanillin", "seed_category": "Category:Flavors"},
        {"entity": "Category:Sweeteners", "seed_category": "Category:Flavors"},
    ]}), encoding="utf-8")

    root, nodes = ev.load_graph(path)

    assert root == "Category:Flavors"
    assert {n.id: n.type for n in nodes} == {"Vanillin": "page", "Category:Sweeteners": "category"}
