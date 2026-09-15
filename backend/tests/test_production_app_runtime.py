"""Native production APP runtime dosyalarının statik sözleşme testleri."""

from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOT = REPO_ROOT / "deploy" / "production"


def _read(relative_path: str) -> str:
    return (PRODUCTION_ROOT / relative_path).read_text(encoding="utf-8")


def test_production_runtime_layout_is_complete():
    expected = {
        "README.md",
        "app/install.sh",
        "app/auzef-backend.service",
        "app/nginx/auzef-app.conf",
        "app/nginx/README.md",
        "config/backend.env.example",
    }

    assert all((PRODUCTION_ROOT / path).is_file() for path in expected)


def test_systemd_unit_uses_direct_loopback_uvicorn_runtime():
    unit = _read("app/auzef-backend.service")

    assert "User=auzef" in unit
    assert "Group=auzef" in unit
    assert "WorkingDirectory=/opt/auzef/current/backend" in unit
    assert "EnvironmentFile=/etc/auzef/backend.env" in unit
    assert "/opt/auzef/current/.venv/bin/uvicorn main:app" in unit
    assert "--host 127.0.0.1" in unit
    assert "--port 8000" in unit
    assert "--workers 2" in unit
    assert "--proxy-headers" in unit
    assert "--forwarded-allow-ips=127.0.0.1" in unit
    assert "Restart=on-failure" in unit
    assert "StandardOutput=journal" in unit
    assert "entrypoint.sh" not in unit
    assert "scripts.init_system" not in unit
    assert "0.0.0.0" not in unit


def test_nginx_uses_native_upstream_and_preserves_public_routes():
    nginx = _read("app/nginx/auzef-app.conf")

    assert "backend:8000" not in nginx
    assert "proxy_pass http://127.0.0.1:8000" in nginx
    assert "root /opt/auzef/current/frontend;" in nginx
    assert "location = /health {" in nginx
    assert "location = /health/live {" in nginx
    assert "location = /health/ready {" in nginx
    assert "location = /widget-chat {" in nginx
    assert "location = /api/search {" in nginx
    assert "location /api/ {" in nginx
    assert "/var/lib/auzef/flags/maintenance.flag" in nginx
    assert "error_page 502 503 504 = @maintenance;" in nginx
    assert "client_max_body_size 10m;" in nginx
    assert "limit_req_zone" in nginx
    assert 'Cache-Control "no-cache"' in nginx
    assert 'Cache-Control "public, immutable"' in nginx
    assert "ssl_certificate" not in nginx
    assert "set_real_ip_from 0.0.0.0/0" not in nginx


def test_production_environment_requires_split_databases_and_external_cache():
    env_text = _read("config/backend.env.example")
    settings = {}
    for raw_line in env_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings[key] = value

    assert "DATABASE_URL" not in settings
    assert settings["ADMIN_DATABASE_URL"] == ""
    assert settings["CHAT_DATABASE_URL"] == ""
    assert settings["MEILI_URL"]
    assert "MEILI_MASTER_KEY" in settings
    assert settings["QDRANT_HOST"]
    assert settings["QDRANT_PORT"] == "6333"
    assert settings["HF_HOME"] == "/var/cache/auzef/huggingface"
    assert settings["ADMIN_AUTH_ENFORCED"] == "true"
    assert settings["ADMIN_COOKIE_SECURE"] == "true"
    assert "OPENROUTER_API_KEY" in settings
    assert "CM_BASE_URL" in settings
    assert "CM_SERVICE_TOKEN" in settings
    assert "SC_HASH_SECRET" in settings


def test_installer_is_valid_shell_and_does_not_deploy_or_start_services():
    installer_path = PRODUCTION_ROOT / "app" / "install.sh"
    installer = installer_path.read_text(encoding="utf-8")
    syntax = subprocess.run(
        ["sh", "-n", str(installer_path)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert syntax.returncode == 0, syntax.stderr
    assert "command_name in systemctl nginx python3.11" in installer
    assert 'if ! getent group "$APP_GROUP"' in installer
    assert 'if ! id -u "$APP_USER"' in installer
    assert 'groupadd --system "$APP_GROUP"' in installer
    assert 'useradd --system --gid "$APP_GROUP"' in installer
    assert '"$APP_ROOT/releases"' in installer
    assert '"$CACHE_DIR"' in installer
    assert '"$FLAGS_DIR"' in installer
    assert '-m 0750 "$CONFIG_DIR"' in installer
    assert 'if [ -e "$BACKEND_ENV" ]; then' in installer
    assert 'chmod 0640 "$BACKEND_ENV"' in installer
    assert '"$ENV_EXAMPLE_TARGET"' in installer
    assert "install_if_absent" in installer
    assert "systemctl daemon-reload" in installer
    assert "systemctl start" not in installer
    assert "systemctl restart" not in installer
    assert "systemctl enable" not in installer
    assert "ln -s" not in installer
    assert "python -m scripts.init_system" not in installer
