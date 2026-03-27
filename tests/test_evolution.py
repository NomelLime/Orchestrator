"""tests/test_evolution.py — Парсинг плана эволюции без вызова LLM."""

from __future__ import annotations

from modules import evolution


def test_parse_plan_plain_json():
    raw = '{"summary": "x", "targets": {"zones": []}}'
    plan = evolution._parse_plan(raw)
    assert plan is not None
    assert plan.get("summary") == "x"


def test_parse_plan_markdown_wrapped():
    raw = """Here is the plan:
```json
{"summary": "wrapped", "targets": {}}
```
"""
    plan = evolution._parse_plan(raw)
    assert plan is not None
    assert plan.get("summary") == "wrapped"


def test_parse_plan_invalid_returns_none():
    assert evolution._parse_plan("no json here") is None


def test_extract_files_collects_shorts_and_prelend():
    plan = {
        "targets": {
            "shorts_project": {
                "code_patches": [{"file": "pipeline/a.py", "goal": "g"}],
            },
            "prelend": {
                "code_patches": [{"file": "x.php"}],
            },
        }
    }
    files = evolution._extract_files(plan)
    assert "pipeline/a.py" in files
    assert "x.php" in files


def test_extract_files_empty():
    assert evolution._extract_files({"targets": {}}) == []
