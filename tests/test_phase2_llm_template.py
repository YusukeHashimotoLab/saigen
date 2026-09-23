"""src/agent/llm_service.py: template structure check and prompt wording.

The LLM itself is never called: ``_generate`` is replaced by a stub.
"""
import copy

import pytest

from src.agent import llm_service

TEMPLATE = {
    "name": "t",
    "description": "d",
    "steps": [
        {"action": "move_z", "robot_id": 1, "distance": -50.0},
        {"action": "aspirate", "robot_id": 1, "volume": 5.0, "speed": 5},
        {"action": "loop_start", "loop_id": "a", "count": 2},
        {"action": "wait", "robot_id": 1, "seconds": 1.0},
        {"action": "loop_end", "loop_id": "a"},
        {"action": "go_home", "robot_id": 1},
    ],
}


def _stub(monkeypatch, result):
    import json
    monkeypatch.setattr(llm_service, "_generate", lambda prompt, mode: json.dumps(result))


def test_parameter_changes_are_accepted(monkeypatch):
    changed = copy.deepcopy(TEMPLATE)
    changed["steps"][1]["volume"] = 3.0
    changed["steps"][1]["speed"] = 1
    changed["steps"][2]["count"] = 5
    changed["name"] = "renamed"
    _stub(monkeypatch, changed)
    assert llm_service.generate_workflow_from_prompt("3 mL", template=TEMPLATE) == changed


@pytest.mark.parametrize("mutate, needle", [
    (lambda s: s.pop(), "ステップ数"),                                   # step removed
    (lambda s: s.append({"action": "go_home", "robot_id": 1}), "ステップ数"),  # step added
    (lambda s: s.__setitem__(slice(0, 2), [s[1], s[0]]), "ステップ 1"),  # reordered
    (lambda s: s[0].__setitem__("action", "move_radial"), "action"),     # action changed
    (lambda s: s[1].__setitem__("robot_id", 2), "robot_id"),             # other robot
    (lambda s: s[2].__setitem__("loop_id", "b"), "loop_id"),
])
def test_structural_changes_are_rejected(monkeypatch, mutate, needle):
    changed = copy.deepcopy(TEMPLATE)
    mutate(changed["steps"])
    _stub(monkeypatch, changed)
    with pytest.raises(llm_service.TemplateStructureError) as exc:
        llm_service.generate_workflow_from_prompt("x", template=TEMPLATE)
    assert needle in str(exc.value)
    assert "テンプレートの構造を変えた" in str(exc.value)


def test_missing_steps_is_rejected():
    with pytest.raises(llm_service.TemplateStructureError):
        llm_service.check_template_structure(TEMPLATE, {"name": "x"})


def test_without_template_no_structure_check(monkeypatch):
    free = {"name": "f", "steps": [{"action": "go_home", "robot_id": 1}]}
    _stub(monkeypatch, free)
    assert llm_service.generate_workflow_from_prompt("home") == free


def test_generation_failure_raises_instead_of_returning_the_template(monkeypatch):
    def boom(prompt, mode):
        raise RuntimeError("endpoint down")
    monkeypatch.setattr(llm_service, "_generate", boom)
    with pytest.raises(RuntimeError):
        llm_service.generate_workflow_from_prompt("x", template=TEMPLATE)


def test_structure_error_is_a_value_error():
    # the GUI catches Exception and shows it; callers catching ValueError still work
    assert issubclass(llm_service.TemplateStructureError, ValueError)


def test_rotation_prompt_names_the_viewpoint():
    prompt = llm_service.SYSTEM_PROMPT
    assert "operator facing the robot" in prompt
    assert "正=反時計回り（上から見て、+Y方向）、負=時計回り" in prompt


def test_prompt_volume_range_matches_the_schema():
    from src.flow.schema import PIPETTE_MIN_VOLUME_ML
    assert "0.01-10.0 mL" not in llm_service.SYSTEM_PROMPT
    assert f"volume: {PIPETTE_MIN_VOLUME_ML}-10.0 mL" in llm_service.SYSTEM_PROMPT


def test_template_prompt_states_the_rule():
    prompt = llm_service._build_prompt("x", TEMPLATE)
    assert "robot_id" in prompt and "loop_id" in prompt and "採用されません" in prompt
