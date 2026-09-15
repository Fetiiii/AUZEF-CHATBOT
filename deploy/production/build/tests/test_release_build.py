"""Dependency lock ve production release builder sözleşme testleri."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

import pytest


REPO_ROOT = Path(__file__).resolve().parents[4]
BUILD_DIR = REPO_ROOT / "deploy" / "production" / "build"
LOCK_FILE = REPO_ROOT / "backend" / "requirements.lock"
DIRECT_REQUIREMENTS = REPO_ROOT / "backend" / "requirements.txt"
CPU_INDEX_DIRECTIVE = "--extra-index-url https://download.pytorch.org/whl/cpu"


def _normalized_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.split("[", 1)[0]).lower()


def _requirement_names(path: Path) -> set[str]:
    names = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        names.add(_normalized_package_name(line))
    return names


def _lock_entries(path: Path) -> dict[str, str]:
    entries = {}
    exact_line = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[^\s=]+$")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--"):
            assert line == CPU_INDEX_DIRECTIVE
            continue
        assert exact_line.fullmatch(line), f"exact pin olmayan lock satırı: {line}"
        package, version = line.split("==", 1)
        normalized = _normalized_package_name(package)
        assert normalized not in entries, f"duplicate lock package: {package}"
        entries[normalized] = version
    return entries


def _file_digest(path: Path) -> str:
    return subprocess.check_output(["sha256sum", str(path)], text=True).split()[0]


def test_lock_is_exact_and_contains_every_direct_requirement():
    direct = _requirement_names(DIRECT_REQUIREMENTS)
    locked = _lock_entries(LOCK_FILE)
    lock_lines = LOCK_FILE.read_text(encoding="utf-8").splitlines()

    assert lock_lines.count(CPU_INDEX_DIRECTIVE) == 1
    assert direct <= locked.keys()
    assert len(locked) > len(direct)
    assert locked["torch"] == "2.14.0+cpu"
    assert not any(name.startswith(("cuda-", "nvidia-")) for name in locked)
    assert "triton" not in locked


def test_build_scripts_have_valid_shell_and_expected_lifecycle():
    release_script = BUILD_DIR / "build-release.sh"
    refresh_script = BUILD_DIR / "refresh-backend-lock.sh"
    lock_dockerfile = BUILD_DIR / "backend-lock.Dockerfile"

    for script in (release_script, refresh_script):
        result = subprocess.run(
            ["sh", "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr

    release_text = release_script.read_text(encoding="utf-8")
    refresh_text = refresh_script.read_text(encoding="utf-8")
    dockerfile_text = lock_dockerfile.read_text(encoding="utf-8")
    assert "--allow-dirty" in release_text
    assert "requirements.lock" in release_text
    assert "--target build" in release_text
    assert "/app/dist/chatbot-web/." in release_text
    assert "sha256sum -c SHA256SUMS" in release_text
    assert "systemctl" not in release_text
    assert "current" not in release_text
    assert "cuda-*" in release_text and "nvidia-*" in release_text
    assert "triton" in release_text
    assert "--no-cache --target resolver" in refresh_text
    assert "--no-cache --target validation" in refresh_text
    assert "download.pytorch.org/whl/cpu" in refresh_text
    assert "-m pip freeze" in refresh_text
    assert "torch.cuda.is_available() is False" in dockerfile_text
    assert "--no-deps -r requirements.lock" in dockerfile_text


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def release_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    script_target = repo / "deploy/production/build/build-release.sh"
    script_target.parent.mkdir(parents=True)
    shutil.copy2(BUILD_DIR / "build-release.sh", script_target)

    _write(repo / ".gitignore", "dist/\nbackend/.env\nbackend/.venv/\nchatbot-web/node_modules/\n")
    _write(repo / "backend/main.py", "APP = 'fixture'\n")
    _write(repo / "backend/requirements.txt", "fastapi\n")
    _write(
        repo / "backend/requirements.lock",
        f"{CPU_INDEX_DIRECTIVE}\nfastapi==1.0.0\nstarlette==1.0.0\ntorch==2.14.0+cpu\n",
    )
    for package in ("admin", "core", "integrations", "routers", "scripts", "services"):
        _write(repo / f"backend/{package}/__init__.py", "")
    _write(repo / "backend/tests/test_excluded.py", "raise AssertionError\n")
    _write(repo / "backend/.env", "SECRET=must-not-ship\n")
    _write(repo / "backend/.venv/marker", "must-not-ship\n")
    _write(repo / "backend/core/__pycache__/marker.pyc", "must-not-ship\n")
    _write(repo / "chatbot-web/Dockerfile", "FROM scratch AS build\n")
    _write(repo / "chatbot-web/node_modules/marker", "must-not-ship\n")

    fake_bin = tmp_path / "bin"
    fake_docker = fake_bin / "docker"
    _write(
        fake_docker,
        """#!/bin/sh
set -eu
case "$1" in
    build) exit 0 ;;
    create) printf '%s\n' fake-frontend-container ;;
    cp)
        destination=$3
        mkdir -p "$destination/assets"
        printf '%s\n' '<html>production</html>' > "$destination/index.html"
        printf '%s\n' 'window.AUZEF_WIDGET=true;' > "$destination/widget.js"
        printf '%s\n' 'asset' > "$destination/assets/main.js"
        ;;
    rm) exit 0 ;;
    image) exit 0 ;;
    *) printf 'unexpected fake docker command: %s\n' "$1" >&2; exit 1 ;;
esac
""",
    )
    fake_docker.chmod(0o755)

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "release-test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Release Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repo, check=True)

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    return repo, environment


def _run_builder(
    repo: Path, environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / "deploy/production/build/build-release.sh"), *arguments],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_builder_rejects_missing_and_unsafe_versions(release_repo):
    repo, environment = release_repo

    missing = _run_builder(repo, environment)
    unsafe = _run_builder(repo, environment, "../../etc")

    assert missing.returncode != 0
    assert "Version argument" in missing.stderr
    assert unsafe.returncode != 0
    assert "Guvenli bir version" in unsafe.stderr


def test_builder_rejects_dirty_tree_by_default(release_repo):
    repo, environment = release_repo
    _write(repo / "dirty-marker", "intentional\n")

    result = _run_builder(repo, environment, "4.1.0")

    assert result.returncode != 0
    assert "worktree dirty" in result.stderr
    assert not (repo / "dist/releases/auzef-4.1.0.tar.gz").exists()


@pytest.mark.parametrize("gpu_package", ["nvidia-cublas", "cuda-runtime", "triton"])
def test_builder_rejects_gpu_packages_in_production_lock(release_repo, gpu_package):
    repo, environment = release_repo
    lock_file = repo / "backend/requirements.lock"
    lock_file.write_text(
        lock_file.read_text(encoding="utf-8") + f"{gpu_package}==1.0.0\n",
        encoding="utf-8",
    )

    result = _run_builder(repo, environment, "4.1.0", "--allow-dirty")

    assert result.returncode != 0
    assert "cuda-*, nvidia-* veya triton" in result.stderr


def test_clean_builder_records_dirty_false(release_repo, tmp_path):
    repo, environment = release_repo

    result = _run_builder(repo, environment, "4.1.0")

    assert result.returncode == 0, result.stdout + result.stderr
    artifact = repo / "dist/releases/auzef-4.1.0.tar.gz"
    with tarfile.open(artifact, "r:gz") as archive:
        archive.extract("auzef-4.1.0/BUILD_INFO", tmp_path)
    build_info = (tmp_path / "auzef-4.1.0/BUILD_INFO").read_text(encoding="utf-8")
    assert "dirty=false" in build_info


def test_allow_dirty_artifact_contract_checksums_and_no_overwrite(release_repo, tmp_path):
    repo, environment = release_repo
    _write(repo / "dirty-marker", "intentional\n")
    version = "4.1.0-rc1"

    first = _run_builder(repo, environment, version, "--allow-dirty")
    assert first.returncode == 0, first.stdout + first.stderr

    artifact = repo / f"dist/releases/auzef-{version}.tar.gz"
    assert artifact.is_file()
    original_digest = _file_digest(artifact)

    with tarfile.open(artifact, "r:gz") as archive:
        members = archive.getnames()
        assert {name.split("/", 1)[0] for name in members} == {f"auzef-{version}"}
        archive.extractall(tmp_path / "extracted")

    release = tmp_path / "extracted" / f"auzef-{version}"
    assert (release / "backend/main.py").is_file()
    assert (release / "backend/requirements.lock").is_file()
    assert (release / "frontend/index.html").is_file()
    assert (release / "frontend/widget.js").is_file()
    assert (release / "VERSION").read_text(encoding="utf-8") == f"{version}\n"

    build_info = (release / "BUILD_INFO").read_text(encoding="utf-8")
    expected_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    assert f"version={version}" in build_info
    assert f"git_commit={expected_commit}" in build_info
    assert "git_ref=" in build_info
    assert "built_at_utc=" in build_info
    assert "dirty=true" in build_info

    checksum = subprocess.run(
        ["sha256sum", "-c", "SHA256SUMS"],
        cwd=release,
        capture_output=True,
        text=True,
        check=False,
    )
    assert checksum.returncode == 0, checksum.stdout + checksum.stderr

    forbidden_parts = {".env", ".venv", "tests", "__pycache__", ".pytest_cache", "node_modules"}
    assert not any(forbidden_parts.intersection(Path(name).parts) for name in members)

    second = _run_builder(repo, environment, version, "--allow-dirty")
    assert second.returncode != 0
    assert "overwrite edilmeyecek" in second.stderr
    assert _file_digest(artifact) == original_digest
