from runner import cli


def test_each_dataset_routes_to_its_own_gold_corpus(tmp_path, monkeypatch):
    gold = tmp_path / "gold.json"
    gold.write_text("{}")
    monkeypatch.setenv("RATBENCH_GOLD", str(gold))
    cases = {
        "datasets/rat_python50.json": "rat_python50",
        "datasets/rat_python50_new.json": "rat_python50_new",
        "datasets/envbench_python100.json": "envbench_python100",
        "datasets/something_else.json": None,
    }
    for dataset, corpus in cases.items():
        assert cli._gold_for(dataset) == (str(gold), corpus), dataset
