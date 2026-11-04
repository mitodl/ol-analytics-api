"""bin/collect-changelog, run against a copy of the repo's real scriv config.

The release job runs this from bump-my-version's pre_commit_hooks, so a failure
here fails the release. Both branches matter: a release with fragments has to
fold them into CHANGELOG.md, and a release with none (e.g. only Renovate
updates) has to succeed without touching it.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "bin" / "collect-changelog"
CHANGELOG_HEADER = "# Changelog\n\n<!-- scriv-insert-here -->\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    shutil.copy(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG_HEADER)
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / ".gitkeep").touch()
    return tmp_path


def run_collect(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_collects_fragments_under_the_project_version(project):
    fragment = project / "changelog.d" / "20260101_000000_someone_change.md"
    fragment.write_text("### Fixed\n\n- A fixed thing.\n")

    result = run_collect(project)

    assert result.returncode == 0, result.stderr
    changelog = (project / "CHANGELOG.md").read_text()
    version = next(
        line.split('"')[1]
        for line in (project / "pyproject.toml").read_text().splitlines()
        if line.startswith("version = ")
    )
    assert f"## {version} (" in changelog
    assert "### Fixed\n\n- A fixed thing." in changelog
    assert not fragment.exists()
    assert (project / "changelog.d" / ".gitkeep").exists()


def test_no_fragments_is_a_no_op(project):
    result = run_collect(project)

    assert result.returncode == 0, result.stderr
    assert "No changelog fragments to collect" in result.stdout
    assert (project / "CHANGELOG.md").read_text() == CHANGELOG_HEADER
