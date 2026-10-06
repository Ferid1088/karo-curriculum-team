"""Vollstaendigkeit und Freigabetor (PART 67-69)."""
from __future__ import annotations

from kcteam.completeness import manifest, release_gate, CORE_REQUIRED
from kcteam.journey import compile_policy
from tests.test_package_journey import demo_pkg


def test_manifest_markiert_alle_28_schichten():
    pkg = demo_pkg()
    pkg.journey_policy = compile_policy(pkg)
    m = manifest(pkg)
    states = {i.component: i.state for i in m.items}
    assert len(m.items) == 28
    assert states["concept_identity"] == "COMPLETE"
    assert states["mastery_checks"] == "COMPLETE"
    # kein visueller Bedarf deklariert -> ehrlich markiert, nicht still
    assert states["visual_assets"] in ("MISSING", "PARTIAL")
    assert states["illustrative_images"] in ("MISSING", "PARTIAL")


def test_tor_blockiert_bei_fehlendem_kern():
    pkg = demo_pkg()
    pkg.tasks = [t for t in pkg.tasks if t.role != "MASTERY_CHECK"]
    pkg.coverage_manifest = manifest(pkg)
    status, blocking = release_gate(pkg)
    assert status == "PARTIAL"
    assert any("mastery_checks" in b for b in blocking)


def test_tor_gibt_ready_bei_vollem_nachweis():
    pkg = demo_pkg()
    pkg.journey_policy = compile_policy(pkg)
    qe = pkg.quality_evidence
    for f in ("dead_end_check", "template_check", "return_paths",
              "prerequisite_cycles", "subject_validation", "validator",
              "rubric_adversarial", "visual_qa"):
        setattr(qe, f, "PASS")
    pkg.coverage_manifest = manifest(pkg)
    status, blocking = release_gate(pkg)
    assert status in ("READY_CORE", "READY_COMPLETE"), blocking
    assert status != "PARTIAL"


def test_tor_fail_nachweis_fuehrt_zu_partial():
    pkg = demo_pkg()
    pkg.journey_policy = compile_policy(pkg)
    pkg.quality_evidence.dead_end_check = "FAIL"
    pkg.coverage_manifest = manifest(pkg)
    status, _ = release_gate(pkg)
    assert status == "PARTIAL"
