from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import asset_pipeline as ap  # noqa: E402


@pytest.fixture
def workspace() -> Path:
    path = PROJECT_ROOT / ".pytest-work" / uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_config(workspace: Path, mutate=None) -> Path:
    config = ap.load_config(PROJECT_ROOT / "config.yaml")
    config["operations"]["output_path"] = str(workspace / "output")
    config["operations"]["audit_log_path"] = str(workspace / "output/audit/audit.jsonl")
    config["operations"]["health_file_path"] = str(workspace / "output/health/health.json")
    config["operations"]["stop_flag_path"] = str(workspace / "STOP.flag")
    config["k16_concurrent_spawn_mutex"]["lock_dir"] = str(workspace / ".lock")
    if mutate:
        mutate(config)
    config_path = workspace / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config_path


@pytest.fixture
def config_path(workspace: Path) -> Path:
    return make_config(workspace)


@pytest.fixture
def pipeline(config_path: Path) -> ap.AssetPipeline:
    return ap.AssetPipeline(config_path, env={})


@pytest.fixture
def engine(config_path: Path) -> ap.AssetPipeline:
    return ap.AssetPipeline(config_path, env={})


@pytest.fixture
def completed_report(config_path: Path) -> tuple[ap.AssetPipeline, dict[str, object]]:
    pipeline = ap.AssetPipeline(config_path, env={})
    return pipeline, pipeline.run()


def test_load_config_exposes_7_hotels_and_8_personas(config_path: Path) -> None:
    config = ap.load_config(config_path)
    assert len(config["hotels"]) == 7
    assert len(config["personas"]) == 8


def test_tailwind_anchor_present_in_templates(pipeline: ap.AssetPipeline) -> None:
    hotel = pipeline.hotels[0]
    persona = pipeline.personas[0]
    html = pipeline.render("landing_page", hotel, persona)
    assert pipeline.tailwind_anchor in html


def test_mock_mode_is_default(pipeline: ap.AssetPipeline) -> None:
    assert pipeline.resolve_mode() == "mock"
    assert pipeline.real_output_enabled() is False


def test_real_output_requires_env_gate(config_path: Path) -> None:
    assert ap.AssetPipeline(config_path, env={}).resolve_mode() == "mock"
    assert ap.AssetPipeline(config_path, env={"DF_HLM_1_ENABLE_REAL_OUTPUT": "1"}).resolve_mode() == "real"


def test_stop_flag_short_circuits_run(workspace: Path) -> None:
    config_path = make_config(workspace)
    config = ap.load_config(config_path)
    Path(config["operations"]["stop_flag_path"]).write_text("stop", encoding="utf-8")
    report = ap.AssetPipeline(config_path, env={}).run()
    assert report["status"] == "stopped"
    assert report["generated_combo_count"] == 0


def test_k16_lock_blocks_second_runner(pipeline: ap.AssetPipeline) -> None:
    with pipeline.mutex():
        with pytest.raises(ap.MutexActiveError):
            with pipeline.mutex():
                pass


def test_k16_pgrep_detects_existing_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ap.K16MutexGuard, "_pgrep_other_instance", lambda self: 12345)
    with pytest.raises(ap.MutexActiveError):
        with ap.directory_mutex(Path("/does/not/matter"), 1, "asset_pipeline.py"):
            pass


def test_k11_cascade_containment_keeps_other_combos_running(config_path: Path) -> None:
    env = {"DF_HLM_1_FAIL_COMBO": "alexander-plaza:city-breaker"}
    report = ap.AssetPipeline(config_path, env=env).run()
    assert report["status"] == "completed_with_errors"
    assert report["generated_combo_count"] == 55
    assert report["error_count"] == 1


def test_k12_provenance_required_in_every_asset(completed_report: tuple[ap.AssetPipeline, dict[str, object]]) -> None:
    pipeline, report = completed_report
    bundle_root = Path(str(report["bundle_root"]))
    sample = next(bundle_root.rglob("landing_page.html")).read_text(encoding="utf-8")
    assert "provenance:" in sample
    assert pipeline.tailwind_anchor in sample


def test_k12_html_lint_rejects_missing_tailwind_anchor(pipeline: ap.AssetPipeline) -> None:
    errors = ap.lint_html_document("<html><body><!-- provenance: x --><div data-external-anchor=\"x\"></div></body></html>", pipeline.tailwind_anchor)
    assert "missing-tailwind-anchor" in errors


def test_k13_rejects_unapproved_domain(workspace: Path) -> None:
    def mutate(config):
        config["hotels"][0]["canonical_url"] = "https://invalid.example.org"

    config_path = make_config(workspace, mutate)
    with pytest.raises(ap.ValidationError):
        ap.AssetPipeline(config_path, env={}).run()


def test_k14_override_allows_domain_bypass(workspace: Path) -> None:
    def mutate(config):
        config["hotels"][0]["canonical_url"] = "https://invalid.example.org"

    config_path = make_config(workspace, mutate)
    report = ap.AssetPipeline(config_path, env={"DF_HLM_1_FORCE_OVERRIDE": "1"}).run()
    assert report["generated_combo_count"] == 56


def test_k15_entropy_budget_check_passes(pipeline: ap.AssetPipeline) -> None:
    assert pipeline.check_entropy_budget() is True


def test_lc_health_check_reports_mock_mode(pipeline: ap.AssetPipeline) -> None:
    health = pipeline.health_check()
    assert health["mode"] == "mock"
    assert health["degraded"] is True


def test_idempotent_hash_paths_are_stable(config_path: Path) -> None:
    first = ap.AssetPipeline(config_path, env={}).run()
    second = ap.AssetPipeline(config_path, env={}).run()
    assert first["bundle_root"] == second["bundle_root"]
    assert first["zip_path"] == second["zip_path"]


def test_generates_56_combo_manifests(completed_report: tuple[ap.AssetPipeline, dict[str, object]]) -> None:
    _, report = completed_report
    bundle_root = Path(str(report["bundle_root"]))
    manifests = list(bundle_root.rglob("manifest.json"))
    assert len(manifests) == 56


def test_generates_four_channel_assets_per_combo(completed_report: tuple[ap.AssetPipeline, dict[str, object]]) -> None:
    _, report = completed_report
    bundle_root = Path(str(report["bundle_root"]))
    combo_dir = next(path.parent for path in bundle_root.rglob("manifest.json"))
    assert len(list(combo_dir.glob("*.html"))) == 4


def test_zip_bundle_is_created(completed_report: tuple[ap.AssetPipeline, dict[str, object]]) -> None:
    _, report = completed_report
    zip_path = Path(str(report["zip_path"]))
    assert zip_path.exists()
    with ZipFile(zip_path) as archive:
        names = archive.namelist()
    assert any(name.endswith("run-manifest.json") for name in names)
    assert len([name for name in names if name.endswith(".html")]) == 224


def test_run_manifest_contains_channel_count(completed_report: tuple[ap.AssetPipeline, dict[str, object]]) -> None:
    _, report = completed_report
    manifest = json.loads((Path(str(report["bundle_root"])) / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["channel_file_count"] == 224


def test_audit_log_is_written(completed_report: tuple[ap.AssetPipeline, dict[str, object]]) -> None:
    pipeline, _ = completed_report
    assert pipeline.audit_log_path.exists()
    lines = pipeline.audit_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert any("mock_run_complete" in line for line in lines)


def test_pii_scrubbed_in_output_with_kemmer_name(workspace: Path) -> None:
    """Output enthaelt keinen Kemmer-Familien-Namen."""
    def mutate(config):
        config["personas"][0]["name"] = "Martin"
        config["personas"][0]["desire"] = "Imke wants a quiet stay"

    config_path = make_config(workspace, mutate)
    report = ap.AssetPipeline(config_path, env={}).run()
    bundle_root = Path(str(report["bundle_root"]))
    sample = next(bundle_root.rglob("landing_page.html")).read_text(encoding="utf-8")
    assert "Martin" not in sample
    assert "Imke" not in sample
    assert "[PII-REDACTED]" in sample


def test_k13_pre_action_verification_env_tag_block(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real-Mode mit falschem env_tag wird geblockt."""
    monkeypatch.setenv("DF_ENV_TAG", "prod")
    monkeypatch.setenv("DF_HLM_1_ENABLE_REAL_OUTPUT", "1")
    with pytest.raises(RuntimeError) as exc_info:
        ap.AssetPipeline(config_path, env={"DF_HLM_1_ENABLE_REAL_OUTPUT": "1"}).run()
    assert "K13" in str(exc_info.value)


def test_mock_provenance_explicit_in_output(engine: ap.AssetPipeline) -> None:
    """Mock-Outputs haben 'mode': 'mock' in Provenance."""
    result = engine.run()
    bundle_root = Path(str(result["bundle_root"]))
    sample = next(bundle_root.rglob("landing_page.html")).read_text(encoding="utf-8")
    manifest = json.loads((bundle_root / "run-manifest.json").read_text(encoding="utf-8"))
    assert '"mode": "mock"' in sample
    assert "MOCK-" in sample
    assert manifest["provenance"]["mode"] == "mock"


def test_k16_mutex_blocks_concurrent_spawn(engine: ap.AssetPipeline) -> None:
    """Concurrent Engine-Spawn wird geblockt."""
    with engine.mutex():
        with pytest.raises(ap.MutexActiveError) as exc_info:
            engine.run()
    assert "K16-VETO" in str(exc_info.value)


def test_lc3_circuit_breaker_opens_after_threshold_failures(
    engine: ap.AssetPipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LC3 Circuit-Breaker: 3 consecutive Failures -> break loop + circuit_open audit-event.

    Belegt LC3 Welle-D Pflicht-Test-Klasse (Phase D-2):
    - failure_streak waechst pro Exception in generate_combo
    - Bei threshold (config=3) erreicht: audit('circuit_open') + break
    - error_count == threshold (NICHT alle 56 Combos versucht)
    """
    threshold = int(
        engine.config["lose_coupling"]["LC3_circuit_breaker"][
            "circuit_breaker_open_threshold"
        ]
    )
    assert threshold == 3, "Test expects config-Default threshold=3"

    # Force every generate_combo() to raise
    def always_fail(*args, **kwargs):
        raise RuntimeError("forced-failure-for-lc3-test")
    monkeypatch.setattr(engine, "generate_combo", always_fail)

    result = engine.run()

    # Assert: Circuit-Breaker stopped iteration at threshold (not all 56 combos)
    assert result["status"] == "completed_with_errors"
    assert result["error_count"] == threshold, (
        f"Expected circuit-break after {threshold} failures, got {result['error_count']}"
    )
    assert result["generated_combo_count"] == 0

    # Assert: audit log has 'circuit_open' event
    audit_log_path = Path(engine.config["operations"]["audit_log_path"])
    audit_lines = audit_log_path.read_text(encoding="utf-8").strip().split("\n")
    circuit_events = [
        json.loads(line) for line in audit_lines if '"circuit_open"' in line
    ]
    assert len(circuit_events) >= 1, "circuit_open audit-event missing"
    assert circuit_events[0]["failure_streak"] >= threshold


def test_lc3_circuit_breaker_resets_on_success(
    engine: ap.AssetPipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LC3 Circuit-Breaker: success resets failure_streak (no premature open).

    Sequence: fail -> fail -> success -> fail -> fail -> success -> ... (no break)
    Bestaetigt: failure_streak = 0 nach success (failure_streak nicht-monoton).
    """
    threshold = int(
        engine.config["lose_coupling"]["LC3_circuit_breaker"][
            "circuit_breaker_open_threshold"
        ]
    )

    # Alternating fail-pattern: 2 fails (under threshold), then success, repeat
    original = engine.generate_combo
    call_counter = {"n": 0}

    def alternating(*args, **kwargs):
        call_counter["n"] += 1
        # Every 3rd call succeeds (so failure_streak max = 2 < threshold=3)
        if call_counter["n"] % 3 == 0:
            return original(*args, **kwargs)
        raise RuntimeError("forced-intermittent-failure")

    monkeypatch.setattr(engine, "generate_combo", alternating)

    result = engine.run()

    # Circuit-Breaker did NOT open (since streak max = 2 < threshold=3)
    audit_log_path = Path(engine.config["operations"]["audit_log_path"])
    audit_lines = audit_log_path.read_text(encoding="utf-8").strip().split("\n")
    circuit_events = [
        line for line in audit_lines if '"circuit_open"' in line
    ]
    assert len(circuit_events) == 0, (
        f"Circuit should NOT open with intermittent failures < threshold, "
        f"got {len(circuit_events)} circuit_open events"
    )
    # Some combos succeeded (every 3rd of 56 = ~18)
    assert result["generated_combo_count"] > 0
