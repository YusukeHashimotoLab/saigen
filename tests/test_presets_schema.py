"""The LLM presets in src/agent/presets/ must stay valid under the flow schema.

Each preset file is a wrapper {label, description, hidden, template}; the
`template` is an ExperimentWorkflow. With extra="forbid" and the pipette
minimum volume in the schema, a stale preset would now be rejected at run time,
so this test catches it in CI instead.
"""
import glob
import json
import os

import pytest

from src.flow.schema import ExperimentWorkflow

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRESETS = sorted(glob.glob(os.path.join(REPO_ROOT, "src", "agent", "presets", "*.json")))


def test_presets_exist():
    assert PRESETS, "no preset files found"


@pytest.mark.parametrize("path", PRESETS, ids=[os.path.basename(p) for p in PRESETS])
def test_preset_template_is_a_valid_workflow(path):
    with open(path, encoding="utf-8") as f:
        preset = json.load(f)
    assert "template" in preset, "preset wrapper must carry a 'template' workflow"
    assert isinstance(preset.get("label"), str) and preset["label"]
    workflow = ExperimentWorkflow(**preset["template"])
    assert workflow.steps, "template has no steps"
