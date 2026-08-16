from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.research_governance import (
    ExperimentManifest,
    ResearchGovernance,
    ResearchGovernanceError,
)

GOVERNANCE = Path("config/research/governance_v1.yaml")
EXPERIMENT = Path("config/research/experiment.example.yaml")
CLI = CliRunner()


def test_research_governance_isolated_from_operational_state() -> None:
    governance = ResearchGovernance.load(GOVERNANCE)

    governance.validate_boundaries(Path.cwd())

    assert governance.production_order_routing is False
    assert governance.wp14_evidence_mutation is False
    assert governance.workspace_root == Path("research")
    assert governance.state_root == Path("state/research")


def test_research_governance_rejects_operational_overlap(tmp_path: Path) -> None:
    invalid = tmp_path / "governance.yaml"
    invalid.write_text(
        GOVERNANCE.read_text(encoding="utf-8").replace(
            "workspace_root: research", "workspace_root: state/replay"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ResearchGovernanceError) as error:
        ResearchGovernance.load(invalid)

    assert error.value.code == "research_governance_invalid"


def test_research_governance_cannot_omit_a_protected_path(tmp_path: Path) -> None:
    invalid = tmp_path / "governance.yaml"
    invalid.write_text(
        GOVERNANCE.read_text(encoding="utf-8").replace("  - state/session\n", ""),
        encoding="utf-8",
    )

    with pytest.raises(ResearchGovernanceError) as error:
        ResearchGovernance.load(invalid)

    assert error.value.code == "research_governance_invalid"


def test_research_boundary_check_rejects_project_escape() -> None:
    governance = ResearchGovernance.load(GOVERNANCE).model_copy(
        update={"workspace_root": Path("../outside")}
    )

    with pytest.raises(ResearchGovernanceError) as error:
        governance.validate_boundaries(Path.cwd())

    assert error.value.code == "research_path_escape"


def test_experiment_manifest_enforces_holdout_costs_and_no_routing(tmp_path: Path) -> None:
    manifest = ExperimentManifest.load(EXPERIMENT)

    assert manifest.train.end <= manifest.validation.start
    assert manifest.validation.end <= manifest.holdout.start
    assert manifest.production_order_routing is False
    assert manifest.wp14_isolated is True

    invalid = tmp_path / "experiment.yaml"
    invalid.write_text(
        EXPERIMENT.read_text(encoding="utf-8").replace(
            "start: 2019-01-01T00:00:00+05:30", "start: 2017-01-01T00:00:00+05:30"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ResearchGovernanceError) as error:
        ExperimentManifest.load(invalid)
    assert error.value.code == "research_manifest_invalid"


def test_research_cli_checks_are_read_only() -> None:
    governance = CLI.invoke(app, ["research-governance-check"])
    manifest = CLI.invoke(
        app,
        ["research-manifest-check", "--manifest", str(EXPERIMENT)],
    )

    assert governance.exit_code == 0
    assert "WP-14 mutation: disabled" in governance.stdout
    assert "Production order routing: disabled" in governance.stdout
    assert manifest.exit_code == 0
    assert "Eligible for operational promotion: NO" in manifest.stdout


def test_research_cli_fails_closed_for_invalid_files(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("unknown: true\n", encoding="utf-8")

    governance = CLI.invoke(app, ["research-governance-check", "--config", str(invalid)])
    manifest = CLI.invoke(app, ["research-manifest-check", "--manifest", str(invalid)])

    assert governance.exit_code == 1
    assert "research_governance_invalid" in governance.stderr
    assert manifest.exit_code == 1
    assert "research_manifest_invalid" in manifest.stderr
