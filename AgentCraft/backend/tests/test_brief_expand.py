"""Tests for brief expansion markdown normalization."""

from app.services.brief_expand import (
    normalize_expanded,
    seed_for_expansion,
    structure_to_markdown,
)


def test_normalize_nested_object_inside_expanded():
    payload = {
        "expanded": {
            "product": {"name": "ClinicOps", "tagline": "Telehealth ops"},
            "goals": ["Tenant isolation", "FHIR export"],
            "constraints": ["No PHI in logs"],
        }
    }
    md = normalize_expanded(payload)
    assert "## Product" in md
    assert "ClinicOps" in md
    assert "## Goals" in md
    assert "- Tenant isolation" in md
    assert "{'product'" not in md


def test_normalize_python_dict_string():
    raw = (
        "{'product': {'name': 'MediConnect Platform', 'tagline': 'Telehealth'}, "
        "'goals': ['A', 'B'], 'constraints': ['No PHI']}"
    )
    md = normalize_expanded(raw)
    assert "## Product" in md
    assert "MediConnect Platform" in md
    assert "## Goals" in md


def test_normalize_markdown_passthrough():
    src = "## Product\nClinicOps\n\n## Goals\n- Ship VAPT gate"
    assert normalize_expanded(src) == src


def test_structure_to_markdown_order():
    md = structure_to_markdown(
        {"success": "Done", "product": "X", "problem": "Y"}
    )
    assert md.index("## Product") < md.index("## Problem") < md.index("## Success")


def test_seed_for_expansion_compacts_structured_brief():
    prior = (
        "## Product\nSmart Tutor AI for enterprise SOP training.\n\n"
        "## Goals\n- Ship MVP\n\n"
        "## Stack\n- FastAPI\n"
    )
    out = seed_for_expansion(prior)
    assert "Smart Tutor AI" in out
    assert "Rebuild a FULL structured brief" in out
    assert out.count("## Goals") == 0


def test_seed_for_expansion_keeps_plain_seed():
    seed = "Build a payments API with ledger and webhooks."
    assert seed_for_expansion(seed) == seed