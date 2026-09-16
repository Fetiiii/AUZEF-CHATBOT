"""Hermetic acceptance tests for native production deployment tooling."""

from __future__ import annotations

import io
import os
from pathlib import Path
import stat
import subprocess
import tarfile

import pytest


REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_ROOT = REPO_ROOT / "deploy" / "production" / "scripts"
VERSION = "4.1.0"
SCRIPT_NAMES = (
    "auzef-deploy",
    "auzef-init",
    "auzef-rollback",
    "auzef-status",
    "auzef-health",
    "auzef-logs",
)


def _write(path: Path, content: str, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def _file_digest(path: Path) -> str:
    return subprocess.check_output(["sha256sum", str(path)], text=True).split()[0]


def _checksum_tree(release: Path) -> None:
    lines = []
    for path in sorted(p for p in release.rglob("*") if p.is_file()):
        if path.name == "SHA256SUMS":
            continue
        relative = path.relative_to(release).as_posix()
        lines.append(f"{_file_digest(path)}  {relative}\n")
    _write(release / "SHA256SUMS", "".join(lines))


def _payload(root: Path, version: str = VERSION, top_level: str | None = None) -> Path:
    release = root / (top_level or f"auzef-{version}")
    _write(release / "VERSION", f"{version}\n")
    _write(release / "BUILD_INFO", f"version={version}\ngit_commit={'a' * 40}\ndirty=false\n")
    _write(release / "backend/main.py", "APP = 'fixture'\n")
    _write(
        release / "backend/requirements.lock",
        "--extra-index-url https://download.pytorch.org/whl/cpu\n"
        "torch==2.14.0+cpu\nsentence-transformers==6.0.1\n",
    )
    _write(release / "frontend/index.html", "<html>fixture</html>\n")
    _write(release / "frontend/widget.js", "window.AUZEF_WIDGET = true;\n")
    _checksum_tree(release)
    return release


def _artifact(
    tmp_path: Path,
    *,
    version: str = VERSION,
    top_level: str | None = None,
    corrupt_checksum: bool = False,
    extra_member: tuple[str, bytes] | tarfile.TarInfo | None = None,
) -> Path:
    source = tmp_path / "artifact-source"
    source.mkdir(exist_ok=True)
    release = _payload(source, version=version, top_level=top_level)
    if corrupt_checksum:
        (release / "backend/main.py").write_text("CORRUPTED\n", encoding="utf-8")

    archive = tmp_path / f"{release.name}.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(release, arcname=release.name, recursive=True)
        if isinstance(extra_member, tuple):
            name, content = extra_member
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o644
            handle.addfile(member, io.BytesIO(content))
        elif extra_member is not None:
            handle.addfile(extra_member)
    return archive


def _special_member(kind: str) -> tarfile.TarInfo:
    member = tarfile.TarInfo(f"auzef-{VERSION}/forbidden-{kind}")
    member.mode = 0o644
    if kind == "symlink":
        member.type = tarfile.SYMTYPE
        member.linkname = "VERSION"
    elif kind == "hardlink":
        member.type = tarfile.LNKTYPE
        member.linkname = f"auzef-{VERSION}/VERSION"
    elif kind == "fifo":
        member.type = tarfile.FIFOTYPE
    elif kind == "device":
        member.type = tarfile.CHRTYPE
        member.devmajor = 1
        member.devminor = 3
    else:  # pragma: no cover
        raise AssertionError(kind)
    return member


def _prepared(app_root: Path, version: str) -> Path:
    release = app_root / "releases" / version
    _write(release / "VERSION", f"{version}\n")
    _write(release / "BUILD_INFO", f"version={version}\n")
    _write(release / "backend/main.py", "APP = 'prepared'\n")
    _write(release / "backend/requirements.lock", "fixture==1.0.0\n")
    _write(release / "frontend/index.html", "<html>prepared</html>\n")
    _write(release / "frontend/widget.js", "window.AUZEF_WIDGET = true;\n")
    _checksum_tree(release)
    _write(release / ".venv/bin/uvicorn", "#!/bin/sh\nexit 0\n", 0o755)
    _write(release / ".venv/bin/python", "#!/bin/sh\nexit 0\n", 0o755)
    return release


def _fixture_link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)


@pytest.fixture()
def runtime(tmp_path: Path) -> dict[str, object]:
    app_root = tmp_path / "opt/auzef"
    config_file = tmp_path / "etc/auzef/backend.env"
    cache_root = tmp_path / "var/cache/auzef"
    state_root = tmp_path / "var/lib/auzef"
    fake_bin = tmp_path / "fake-bin"
    call_log = tmp_path / "calls.log"
    app_root.joinpath("releases").mkdir(parents=True)
    cache_root.joinpath("huggingface").mkdir(parents=True)
    state_root.joinpath("flags").mkdir(parents=True)
    fake_bin.mkdir()

    _write(
        config_file,
        "\n".join(
            (
                "ADMIN_DATABASE_URL=postgresql://admin.invalid/admin",
                "CHAT_DATABASE_URL=postgresql://chat.invalid/chat",
                "MEILI_URL=http://meili.invalid:7700",
                "MEILI_MASTER_KEY=test-key-not-a-secret",
                "QDRANT_HOST=qdrant.invalid",
                "QDRANT_PORT=6333",
                f"HF_HOME={cache_root / 'huggingface'}",
                "ADMIN_AUTH_ENFORCED=true",
                "ADMIN_COOKIE_SECURE=true",
                "OPENROUTER_API_KEY=",
                "CM_BASE_URL=",
                "CM_SERVICE_TOKEN=",
                "",
            )
        ),
        0o640,
    )

    fake_python = _write(
        fake_bin / "python3.11",
        """#!/bin/sh
set -eu
printf 'python %s\n' "$*" >> "$FAKE_CALL_LOG"
if [ "${1:-}" = "-" ]; then
    exec /usr/bin/python3 "$@"
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
    [ "${FAKE_PYTHON_FAIL:-}" != "venv" ] || exit 31
    target=$3
    mkdir -p "$target/bin"
    cp "$0" "$target/bin/python"
    printf '#!/bin/sh\nprintf "uvicorn %%s\\n" "$*" >> "$FAKE_CALL_LOG"\nexit 0\n' > "$target/bin/uvicorn"
    chmod 0755 "$target/bin/python" "$target/bin/uvicorn"
    exit 0
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
    case "${3:-}" in
        install) [ "${FAKE_PYTHON_FAIL:-}" != "install" ] || exit 32 ;;
        check) [ "${FAKE_PYTHON_FAIL:-}" != "check" ] || exit 33 ;;
    esac
    exit 0
fi
if [ "${1:-}" = "-c" ]; then
    [ "${FAKE_PYTHON_FAIL:-}" != "smoke" ] || exit 34
    exit 0
fi
exit 0
""",
        0o755,
    )

    fake_systemctl = _write(
        fake_bin / "systemctl",
        """#!/bin/sh
set -eu
printf 'systemctl %s\n' "$*" >> "$FAKE_CALL_LOG"
case "${1:-}" in
    start)
        case "$*" in
            *auzef-init@*) [ "${FAKE_INIT_FAIL:-0}" != 1 ] || exit 41 ;;
        esac
        ;;
    restart) [ "${FAKE_RESTART_FAIL:-0}" != 1 ] || exit 42 ;;
    is-active) printf '%s\n' "${FAKE_ACTIVE_STATE:-active}" ;;
    is-enabled) printf '%s\n' "${FAKE_ENABLED_STATE:-enabled}" ;;
esac
exit 0
""",
        0o755,
    )

    fake_curl = _write(
        fake_bin / "curl",
        """#!/bin/sh
set -eu
printf 'curl %s\n' "$*" >> "$FAKE_CALL_LOG"
output=''
write_out=''
url=''
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o|--output) output=$2; shift 2 ;;
        -w|--write-out) write_out=$2; shift 2 ;;
        -*) shift ;;
        *) url=$1; shift ;;
    esac
done
status=${FAKE_READY_STATUS:-ready}
case "$url" in
    */health/live) status=${FAKE_LIVE_STATUS:-ready} ;;
esac
if [ -n "${FAKE_FAIL_RELEASE:-}" ] && [ -L "${FAKE_CURRENT_LINK:-/nonexistent}" ]; then
    current=$(basename "$(readlink "${FAKE_CURRENT_LINK}")")
    [ "$current" != "$FAKE_FAIL_RELEASE" ] || status=unready
fi
[ "${FAKE_FAIL_ALL:-0}" != 1 ] || status=unready
case "$status" in
    ready) code=200; body='{"status":"ready"}' ;;
    degraded) code=200; body='{"status":"degraded"}' ;;
    live) code=200; body='{"status":"ok"}' ;;
    unready) code=503; body='{"status":"unready"}' ;;
    connection) exit 7 ;;
    *) exit 8 ;;
esac
if [ -n "$output" ]; then
    printf '%s\n' "$body" > "$output"
else
    printf '%s\n' "$body"
fi
if [ -n "$write_out" ]; then
    printf '%s' "$code"
fi
""",
        0o755,
    )

    fake_journalctl = _write(
        fake_bin / "journalctl",
        """#!/bin/sh
set -eu
printf 'journalctl %s\n' "$*" >> "$FAKE_CALL_LOG"
printf '%s\n' 'fixture journal line'
""",
        0o755,
    )
    fake_chown = _write(
        fake_bin / "chown",
        """#!/bin/sh
set -eu
printf 'chown %s\n' "$*" >> "$FAKE_CALL_LOG"
""",
        0o755,
    )

    env = os.environ.copy()
    env.update(
        {
            "AUZEF_APP_ROOT": str(app_root),
            "AUZEF_CONFIG_FILE": str(config_file),
            "AUZEF_CACHE_ROOT": str(cache_root),
            "AUZEF_STATE_ROOT": str(state_root),
            "AUZEF_SYSTEMCTL": str(fake_systemctl),
            "AUZEF_CURL": str(fake_curl),
            "AUZEF_JOURNALCTL": str(fake_journalctl),
            "AUZEF_PYTHON": str(fake_python),
            "AUZEF_CHOWN": str(fake_chown),
            "AUZEF_SKIP_ROOT_CHECK": "1",
            "AUZEF_READY_URL": "http://127.0.0.1/health/ready",
            "AUZEF_LIVE_URL": "http://127.0.0.1/health/live",
            "AUZEF_READINESS_TIMEOUT": "1",
            "AUZEF_READINESS_INTERVAL": "1",
            "AUZEF_LOCK_FILE": str(state_root / "deploy.lock"),
            "FAKE_CALL_LOG": str(call_log),
            "FAKE_CURRENT_LINK": str(app_root / "current"),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    return {
        "tmp": tmp_path,
        "app_root": app_root,
        "config": config_file,
        "cache": cache_root,
        "state": state_root,
        "call_log": call_log,
        "env": env,
    }


def _run(runtime: dict[str, object], script: str, *arguments: str, **updates: str):
    env = dict(runtime["env"])
    env.update(updates)
    return subprocess.run(
        [str(SCRIPTS_ROOT / script), *arguments],
        cwd=runtime["tmp"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )


def _calls(runtime: dict[str, object]) -> str:
    path = runtime["call_log"]
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_scripts_exist_and_have_valid_posix_shell():
    paths = [SCRIPTS_ROOT / "common.sh", *(SCRIPTS_ROOT / name for name in SCRIPT_NAMES)]
    for path in paths:
        assert path.is_file(), path
        result = subprocess.run(
            ["sh", "-n", str(path)], text=True, capture_output=True, check=False
        )
        assert result.returncode == 0, f"{path}: {result.stderr}"


@pytest.mark.parametrize(
    "member",
    [
        (f"auzef-{VERSION}/../../escape", b"escape"),
        (f"/tmp/auzef-{VERSION}-absolute", b"escape"),
        _special_member("symlink"),
        _special_member("hardlink"),
        _special_member("fifo"),
        _special_member("device"),
    ],
    ids=("traversal", "absolute", "symlink", "hardlink", "fifo", "device"),
)
def test_prepare_rejects_unsafe_archive_members(runtime, member):
    artifact = _artifact(runtime["tmp"], extra_member=member)

    result = _run(runtime, "auzef-deploy", str(artifact), "--prepare-only")

    assert result.returncode != 0
    assert not (runtime["tmp"] / "escape").exists()
    assert not (runtime["app_root"] / "releases" / VERSION).exists()
    assert not (runtime["app_root"] / "current").exists()


def test_prepare_rejects_checksum_mismatch(runtime):
    artifact = _artifact(runtime["tmp"], corrupt_checksum=True)

    result = _run(runtime, "auzef-deploy", str(artifact), "--prepare-only")

    assert result.returncode != 0
    assert not (runtime["app_root"] / "releases" / VERSION).exists()
    assert "-m venv" not in _calls(runtime)


def test_prepare_rejects_multiple_top_level_directories(runtime):
    artifact = _artifact(runtime["tmp"], extra_member=("second-root/file", b"x"))

    result = _run(runtime, "auzef-deploy", str(artifact), "--prepare-only")

    assert result.returncode != 0
    assert not (runtime["app_root"] / "releases" / VERSION).exists()


@pytest.mark.parametrize(
    "extra_member",
    [
        (f"auzef-{VERSION}/backend/injected.py", b"INJECTED = True\n"),
        (f"auzef-{VERSION}/backend/main.py", b"DUPLICATE = True\n"),
    ],
    ids=("unlisted-payload", "duplicate-archive-path"),
)
def test_prepare_rejects_unchecksummed_or_duplicate_payload(runtime, extra_member):
    artifact = _artifact(runtime["tmp"], extra_member=extra_member)

    result = _run(runtime, "auzef-deploy", str(artifact), "--prepare-only")

    assert result.returncode != 0
    assert not (runtime["app_root"] / "releases" / VERSION).exists()


@pytest.mark.parametrize(
    ("artifact_version", "top_level"),
    [("4.2.0", f"auzef-{VERSION}"), (VERSION, "unexpected-top-level")],
)
def test_prepare_rejects_wrong_version_or_top_level(runtime, artifact_version, top_level):
    artifact = _artifact(
        runtime["tmp"], version=artifact_version, top_level=top_level
    )

    result = _run(runtime, "auzef-deploy", str(artifact), "--prepare-only")

    assert result.returncode != 0
    assert not any((runtime["app_root"] / "releases").iterdir())


def test_prepare_never_overwrites_existing_release(runtime):
    existing = _prepared(runtime["app_root"], VERSION)
    marker = _write(existing / "operator-marker", "keep-me\n")
    artifact = _artifact(runtime["tmp"])

    result = _run(runtime, "auzef-deploy", str(artifact), "--prepare-only")

    assert result.returncode != 0
    assert marker.read_text(encoding="utf-8") == "keep-me\n"
    assert "-m venv" not in _calls(runtime)


def test_prepare_stages_validates_and_publishes_without_activation_or_init(runtime):
    artifact = _artifact(runtime["tmp"])

    result = _run(runtime, "auzef-deploy", str(artifact), "--prepare-only")

    assert result.returncode == 0, result.stdout + result.stderr
    release = runtime["app_root"] / "releases" / VERSION
    assert (release / "backend/main.py").is_file()
    assert (release / ".venv/bin/python").is_file()
    assert (release / ".venv/bin/uvicorn").is_file()
    assert not (runtime["app_root"] / "current").exists()
    calls = _calls(runtime)
    assert f"-m venv {release / '.venv'}" in calls
    assert ".staging." not in next(
        line for line in calls.splitlines() if "-m venv" in line
    )
    assert "pip install --no-deps -r" in calls
    assert "pip check" in calls
    assert "python -c" in calls
    assert "uvicorn --version" in calls
    assert "scripts.init_system" not in calls
    assert "systemctl restart" not in calls
    for path in (release / "backend/main.py", release / ".venv/bin/python"):
        assert not path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)


@pytest.mark.parametrize("failure", ["venv", "install", "check", "smoke"])
def test_prepare_dependency_failure_does_not_activate(runtime, failure):
    old = _prepared(runtime["app_root"], "4.0.0")
    _fixture_link(runtime["app_root"] / "current", old)
    artifact = _artifact(runtime["tmp"])

    result = _run(
        runtime,
        "auzef-deploy",
        str(artifact),
        "--prepare-only",
        FAKE_PYTHON_FAIL=failure,
    )

    assert result.returncode != 0
    assert (runtime["app_root"] / "current").resolve() == old.resolve()
    assert not (runtime["app_root"] / "releases" / VERSION).exists()
    assert "systemctl restart" not in _calls(runtime)
    assert "scripts.init_system" not in _calls(runtime)


@pytest.mark.parametrize(
    "replacement",
    [
        "CHAT_DATABASE_URL=",
        "QDRANT_HOST=CHANGE_ME_QDRANT_HOST",
        "ADMIN_AUTH_ENFORCED=false",
        "ADMIN_COOKIE_SECURE=false",
        "HF_HOME=/tmp/wrong-cache",
    ],
)
def test_activation_preflight_rejects_invalid_production_environment(runtime, replacement):
    target = _prepared(runtime["app_root"], VERSION)
    config = runtime["config"]
    lines = config.read_text(encoding="utf-8").splitlines()
    key = replacement.split("=", 1)[0]
    config.write_text(
        "\n".join(replacement if line.startswith(f"{key}=") else line for line in lines)
        + "\n",
        encoding="utf-8",
    )

    result = _run(
        runtime, "auzef-deploy", "--activate", VERSION, "--confirm-drained"
    )

    assert result.returncode != 0
    assert not (runtime["app_root"] / "current").exists()
    assert "systemctl restart" not in _calls(runtime)
    assert "postgresql://" not in result.stdout + result.stderr
    assert target.is_dir()


def test_activation_requires_explicit_drain_confirmation(runtime):
    _prepared(runtime["app_root"], VERSION)

    result = _run(runtime, "auzef-deploy", "--activate", VERSION)

    assert result.returncode != 0
    assert not (runtime["app_root"] / "current").exists()
    assert "systemctl restart" not in _calls(runtime)


@pytest.mark.parametrize("ready_status", ["ready", "degraded"])
def test_successful_activation_switches_current_and_previous(runtime, ready_status):
    old = _prepared(runtime["app_root"], "4.0.0")
    new = _prepared(runtime["app_root"], VERSION)
    _fixture_link(runtime["app_root"] / "current", old)

    result = _run(
        runtime,
        "auzef-deploy",
        "--activate",
        VERSION,
        "--confirm-drained",
        FAKE_READY_STATUS=ready_status,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (runtime["app_root"] / "current").resolve() == new.resolve()
    assert (runtime["app_root"] / "previous").resolve() == old.resolve()
    calls = _calls(runtime)
    assert calls.count("systemctl restart auzef-backend.service") == 1
    assert "scripts.init_system" not in calls


def test_failed_activation_restores_healthy_previous_release(runtime):
    old = _prepared(runtime["app_root"], "4.0.0")
    _prepared(runtime["app_root"], VERSION)
    _fixture_link(runtime["app_root"] / "current", old)

    result = _run(
        runtime,
        "auzef-deploy",
        "--activate",
        VERSION,
        "--confirm-drained",
        FAKE_FAIL_RELEASE=VERSION,
    )

    assert result.returncode != 0
    assert (runtime["app_root"] / "current").resolve() == old.resolve()
    calls = _calls(runtime)
    assert calls.count("systemctl restart auzef-backend.service") == 2
    assert calls.count("/health/ready") >= 2


def test_failed_activation_reports_critical_when_previous_is_also_unready(runtime):
    old = _prepared(runtime["app_root"], "4.0.0")
    _prepared(runtime["app_root"], VERSION)
    _fixture_link(runtime["app_root"] / "current", old)

    result = _run(
        runtime,
        "auzef-deploy",
        "--activate",
        VERSION,
        "--confirm-drained",
        FAKE_FAIL_ALL="1",
    )

    assert result.returncode != 0
    assert (runtime["app_root"] / "current").resolve() == old.resolve()
    assert "CRITICAL" in result.stderr
    assert _calls(runtime).count("systemctl restart auzef-backend.service") == 2


def test_failed_first_activation_leaves_no_broken_current(runtime):
    _prepared(runtime["app_root"], VERSION)

    result = _run(
        runtime,
        "auzef-deploy",
        "--activate",
        VERSION,
        "--confirm-drained",
        FAKE_FAIL_RELEASE=VERSION,
    )

    assert result.returncode != 0
    assert not os.path.lexists(runtime["app_root"] / "current")
    assert not os.path.lexists(runtime["app_root"] / "previous")
    assert "systemctl stop auzef-backend.service" in _calls(runtime)


def test_rollback_to_previous_swaps_symlinks_and_checks_health(runtime):
    old = _prepared(runtime["app_root"], "4.0.0")
    current = _prepared(runtime["app_root"], VERSION)
    _fixture_link(runtime["app_root"] / "current", current)
    _fixture_link(runtime["app_root"] / "previous", old)

    result = _run(runtime, "auzef-rollback", "--confirm-drained")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (runtime["app_root"] / "current").resolve() == old.resolve()
    assert (runtime["app_root"] / "previous").resolve() == current.resolve()
    calls = _calls(runtime)
    assert calls.count("systemctl restart auzef-backend.service") == 1
    assert "scripts.init_system" not in calls


def test_failed_rollback_recovers_original_current(runtime):
    old = _prepared(runtime["app_root"], "4.0.0")
    current = _prepared(runtime["app_root"], VERSION)
    _fixture_link(runtime["app_root"] / "current", current)
    _fixture_link(runtime["app_root"] / "previous", old)

    result = _run(
        runtime,
        "auzef-rollback",
        "--confirm-drained",
        FAKE_FAIL_RELEASE="4.0.0",
    )

    assert result.returncode != 0
    assert (runtime["app_root"] / "current").resolve() == current.resolve()
    assert _calls(runtime).count("systemctl restart auzef-backend.service") == 2


def test_init_requires_confirmation_and_propagates_systemd_failure(runtime):
    _prepared(runtime["app_root"], VERSION)

    missing_confirmation = _run(runtime, "auzef-init", VERSION)
    assert missing_confirmation.returncode != 0
    assert "auzef-init@" not in _calls(runtime)

    failure = _run(
        runtime,
        "auzef-init",
        VERSION,
        "--confirm-shared-change",
        FAKE_INIT_FAIL="1",
    )
    assert failure.returncode != 0
    assert f"systemctl start auzef-init@{VERSION}.service" in _calls(runtime)


def test_deploy_never_runs_shared_initialization(runtime):
    artifact = _artifact(runtime["tmp"])
    result = _run(runtime, "auzef-deploy", str(artifact), "--prepare-only")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "auzef-init@" not in _calls(runtime)
    assert "scripts.init_system" not in _calls(runtime)


def test_health_reports_bodies_codes_and_uses_automation_exit_status(runtime):
    success = _run(
        runtime,
        "auzef-health",
        FAKE_LIVE_STATUS="live",
        FAKE_READY_STATUS="degraded",
    )
    assert success.returncode == 0, success.stdout + success.stderr
    assert "200" in success.stdout
    assert '"status":"ok"' in success.stdout
    assert '"status":"degraded"' in success.stdout

    failure = _run(runtime, "auzef-health", FAKE_READY_STATUS="unready")
    assert failure.returncode != 0
    assert "503" in failure.stdout
    assert '"status":"unready"' in failure.stdout

    connection_failure = _run(runtime, "auzef-health", FAKE_READY_STATUS="connection")
    assert connection_failure.returncode != 0
    assert "HTTP 000" in connection_failure.stdout


def test_status_reports_releases_services_flags_and_never_reads_secrets(runtime):
    current = _prepared(runtime["app_root"], VERSION)
    _prepared(runtime["app_root"], "4.0.0")
    _fixture_link(runtime["app_root"] / "current", current)
    (runtime["app_root"] / "previous").symlink_to(
        runtime["app_root"] / "releases" / "missing"
    )
    _write(runtime["state"] / "flags/maintenance.flag", "1\n")
    secret = "must-never-be-rendered"
    with runtime["config"].open("a", encoding="utf-8") as handle:
        handle.write(f"OPENROUTER_API_KEY={secret}\n")

    result = _run(runtime, "auzef-status")

    assert result.returncode == 0, result.stdout + result.stderr
    assert VERSION in result.stdout
    assert "4.0.0" in result.stdout
    assert "active" in result.stdout
    assert "enabled" in result.stdout
    assert "maintenance" in result.stdout.lower()
    assert "broken" in result.stdout.lower() or "kirik" in result.stdout.lower()
    assert secret not in result.stdout + result.stderr


def test_logs_routes_backend_follow_and_init_to_expected_journal_units(runtime):
    plain = _run(runtime, "auzef-logs")
    follow = _run(runtime, "auzef-logs", "-f")
    init = _run(runtime, "auzef-logs", "--init", VERSION)

    assert plain.returncode == follow.returncode == init.returncode == 0
    calls = _calls(runtime)
    assert "journalctl -u auzef-backend.service" in calls
    assert "journalctl -u auzef-backend.service -f" in calls
    assert f"journalctl -u auzef-init@{VERSION}.service" in calls
