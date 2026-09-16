# AUZEF Chatbot Production Architecture

## Status and Scope

This document is the production deployment baseline for AUZEF Chatbot. The
decisions below are the production contract; implementation work must conform
to them unless this document is explicitly revised.

The existing Docker Compose setup remains the local development environment.
Production is a native Linux and `systemd` deployment. This baseline documents
the target only; it does not introduce production scripts or change application
behavior.

## Development Architecture

- Local development continues to use Docker and Docker Compose.
- The existing `docker-compose.yml` is retained as the base local development
  topology.
- The existing `docker-compose.dev.yml` remains the development override for
  HTTP-friendly settings and the Angular hot-reload service.
- Development must remain backward-compatible with one `DATABASE_URL`. A
  developer must not need two PostgreSQL instances to run the application.
- Production-specific service management must not replace or remove the local
  Compose workflow.

## Production Architecture

Production consists of six virtual machines and the institution's existing
physical Load Balancer (LB):

```text
                              +----------------------+
Users ----------------------> | Physical Load        |
                              | Balancer              |
                              +----------+-----------+
                                         |
                          +--------------+--------------+
                          |                             |
                    +-----v------+                +-----v------+
                    | APP-01     |                | APP-02     |
                    | Nginx      |                | Nginx      |
                    | Angular    |                | Angular    |
                    | FastAPI x2 |                | FastAPI x2 |
                    +-----+------+                +------+-----+
                          |                              |
                          +---------------+--------------+
                                          |
             +----------------+-----------+-----------+----------------+
             |                |                       |                |
       +-----v-----+    +-----v------+          +-----v------+   +-----v-----+
       | DB-CHAT   |    | DB-ADMIN   |          | MeiliSearch|   | Qdrant    |
       | PostgreSQL|    | PostgreSQL |          | native     |   | native    |
       +-----------+    +------------+          +------------+   +-----------+
```

- Production does not use Docker, Podman, Kubernetes, or another container
  runtime/orchestrator.
- Production services run natively on Linux and are managed by `systemd`.
- Centralized orchestration does not use Ansible.
- APP-01 and APP-02 are equivalent application nodes. Each contains Nginx, the
  production Angular build output, and the FastAPI backend.
- The physical LB distributes client traffic between APP-01 and APP-02.
- There is no separate frontend VM.
- FastAPI is not exposed directly to the internet. On each APP node, only local
  Nginx can reach that node's FastAPI listener.
- FastAPI runs under native Uvicorn with two workers, preserving the existing
  application runtime behavior and memory assumptions.
- MeiliSearch and Qdrant each run as a native service on their own VM.
- The compatibility-validated production server pins are Meilisearch `1.53.2`
  with application client `meilisearch==0.43.0`, and Qdrant `1.19.1` with
  application client `qdrant-client==1.19.0`. The machine-readable, non-secret
  contract is `deploy/production/search-services.env`.

## VM Responsibilities

| Node | Responsibilities | Service boundary |
|---|---|---|
| Physical LB | Distribute traffic across healthy APP nodes; support node drain and add for rolling releases | The only production traffic distributor in front of APP-01 and APP-02 |
| APP-01 | Nginx, immutable production Angular assets, FastAPI/Uvicorn with two workers | Nginx accepts LB traffic; FastAPI is local-only |
| APP-02 | Same software, configuration shape, release artifact, and service layout as APP-01 | Nginx accepts LB traffic; FastAPI is local-only |
| DB-CHAT | Conversations, conversation messages, and other high-volume records whose lifecycle follows chat activity | PostgreSQL, reachable only from authorized application/operations hosts |
| DB-ADMIN | QnA content, aliases/tags, admin users and sessions, application settings, academic calendar, and other management records | PostgreSQL, reachable only from authorized application/operations hosts |
| MeiliSearch | Keyword-search index used by the answer pipeline | Native MeiliSearch service on a dedicated VM |
| Qdrant | Vector index used by the semantic retrieval pipeline | Native Qdrant service on a dedicated VM |

The two PostgreSQL VMs separate workloads and record lifecycles. This split is
not an HA or replication design. It must not be represented operationally as a
primary/replica pair.

Development retains one database connection. Production will introduce:

- `ADMIN_DATABASE_URL` for DB-ADMIN.
- `CHAT_DATABASE_URL` for DB-CHAT.
- `DATABASE_URL` as the backward-compatible development path.

The future persistence implementation must make the production routing
explicit while preserving the single-URL development behavior.

## Network Flow

The permitted request and dependency flow is:

1. A client connects to the institution's physical LB.
2. The LB selects APP-01 or APP-02.
3. Nginx on that APP node serves Angular files directly or reverse-proxies API,
   widget, and health requests to the local FastAPI service.
4. FastAPI connects over the internal network to DB-CHAT, DB-ADMIN,
   MeiliSearch, and Qdrant according to the request's persistence needs.
5. FastAPI responses return through the same node's Nginx and the physical LB.

Network policy must enforce these boundaries:

- Only LB traffic is accepted on the APP nodes' public-facing Nginx ports.
- The Uvicorn listener is bound to loopback or an equivalent local-only socket;
  it is never an internet-facing endpoint.
- PostgreSQL, MeiliSearch, and Qdrant accept connections only from explicitly
  authorized APP and operations sources.
- Forwarded client address and protocol headers are trusted only along the
  physical LB -> Nginx -> Uvicorn proxy chain.

## Deployment Model

Releases use immutable, versioned release directories and a `current` symlink:

```text
<application-root>/
  releases/
    <release-id>/
      backend/
      frontend/
      manifest/checksums
  current -> releases/<release-id>
  previous -> releases/<previous-release-id>
```

- The release artifact is built and verified before it reaches a production
  VM. Production servers should not compile Angular, resolve application
  dependencies, or perform other builds when a ready artifact can be supplied.
- APP-01 and APP-02 receive the same versioned artifact.
- Deployment has explicit PREPARE, SHARED INIT, and ACTIVATE phases. PREPARE
  validates and stages the artifact, creates the release virtualenv from the
  committed lock, and does not change `current`. SHARED INIT is a separate,
  operator-confirmed DB/Qdrant operation run once on one APP node. ACTIVATE is
  performed only after the operator drains that APP node from the physical LB.
- Activation atomically changes `current`, restarts the backend service, and
  gates success on Nginx `/health/ready`. Nginx is not reloaded for an ordinary
  application release because its configuration is outside the release.
- A successful activation points `previous` at the former active release.
  Automatic or manual application rollback atomically restores `current` and
  restarts the backend. Runtime configuration, secrets, shared DB/Qdrant state,
  and persistent state are not rolled back by changing this symlink.
- Database initialization and migration are explicit deployment operations,
  separate from APP service startup. Starting or restarting Uvicorn must not
  implicitly modify database schema.
- Deploy, explicit shared initialization, rollback, status, health, and log
  operations use the standard shell tools under `deploy/production/scripts/`
  together with `systemd` and `journalctl`. Physical LB drain/add remains an
  operator-controlled institutional procedure.

Rolling deployment order is fixed:

1. Prepare the same artifact on APP-01 and APP-02 without changing `current`.
2. Run shared DB/Qdrant initialization once from one APP node when required.
3. Drain APP-01 from the LB.
4. Activate the release on APP-01 and verify readiness.
5. Add APP-01 back to the LB.
6. Drain APP-02 from the LB.
7. Activate the already-prepared release on APP-02 without repeating shared init.
8. Verify APP-02 readiness and add it back to the LB.
9. Verify that both nodes report the same release version.

Schema changes used by a rolling release must be compatible with the old and
new application versions while both may be running.

## Configuration and Persistent Data

- Runtime configuration and secrets live outside all versioned release
  directories. `systemd` units reference external environment/configuration
  files with appropriately restricted ownership and permissions.
- Nginx configuration and any TLS material managed on an APP node also live
  outside the application release directories.
- PostgreSQL storage on DB-CHAT and DB-ADMIN, the MeiliSearch database, and
  Qdrant storage live outside application release directories and survive
  application deploys and rollbacks.
- DB-ADMIN PostgreSQL QnA/alias/tag records are the search content source of
  truth. Meilisearch documents and Qdrant vectors are derived indexes. A
  Meilisearch binary upgrade must not assume an existing `data.ms` can be
  reused in place; use the server's supported backup/migration path or rebuild
  a clean index from DB-ADMIN. Qdrant rebuilds derive vectors from the same QnA
  text/aliases and embedding model. Existing helpers do not yet provide a
  complete stale-free atomic rebuild/swap runbook.
- Any APP-local runtime cache that must survive a release switch, including a
  model cache if retained, uses a dedicated path outside the release tree.
- Release directories are treated as immutable after publication. Application
  runtime state must not be written beneath `releases/<release-id>` or
  `current`.
- Secrets are never embedded in the Angular output or release manifest and are
  not committed to the repository.

## Health and Availability

Production exposes two health contracts:

- `/health/live`: process liveness only. It answers whether the FastAPI process
  can serve requests and must not require downstream dependencies.
- `/health/ready`: readiness for production traffic. DB-ADMIN and DB-CHAT gate
  admission; MeiliSearch/Qdrant failures are reported as degraded without
  withdrawing both APP nodes. It is the rolling-deployment health gate.

The existing `/health` behavior remains a development/compatibility endpoint.

APP-01 and APP-02 provide application-tier redundancy through the physical LB.
The rolling procedure keeps one APP node in service while the other is being
updated. The six-VM topology does not, by itself, provide PostgreSQL,
MeiliSearch, or Qdrant replication/failover.

Planned maintenance mode uses the `MAINTENANCE_MODE=true|false` `SystemConfig`
record in DB-ADMIN, defaulting to `false` when absent. APP-01 and APP-02 read
the same central value. An APP-local Nginx flag may remain as an emergency
single-node override, but the production-wide maintenance decision must not
depend on it.

## Explicit Non-Goals

- Replacing Docker Compose for local development.
- Running containers or Kubernetes in production.
- Introducing Ansible or another centralized orchestration layer.
- Creating a dedicated frontend VM.
- Exposing FastAPI directly to clients or the internet.
- Building release artifacts on production servers as the normal deployment
  path.
- Treating DB-CHAT and DB-ADMIN as an HA/replication pair.
- Defining database, MeiliSearch, or Qdrant replication/failover in this
  baseline.
- Changing chatbot response behavior as part of the deployment conversion.

## Open Infrastructure Questions

- TLS physical Load Balancer üzerinde mi terminate edilecek?
- Production VM'lerin internet çıkışı olacak mı?
- PostgreSQL kurulumu BİDB tarafından mı yapılacak, uygulama ekibi mi yapacak?
- Load Balancer node drain/add operasyon prosedürü nedir?

## Current State Notes

The following points were verified from the repository at the time this
baseline was written:

- `docker-compose.yml` currently defines PostgreSQL (`db`), development-only
  pgAdmin, MeiliSearch, Qdrant, FastAPI (`backend`), and the Angular/Nginx
  `frontend`. Persistent service state uses named Docker volumes; a
  repository-local content directory is bind-mounted into the backend.
- `docker-compose.dev.yml` overlays HTTP-friendly admin cookie behavior, a
  development Nginx configuration, and an Angular `ng serve` hot-reload
  service on port 4200. This confirms Compose is the established development
  workflow that must be preserved.
- `backend/entrypoint.sh` explicitly runs
  `python -m scripts.init_system all` before starting Uvicorn. This preserves
  automatic DB/config/Qdrant preparation for the Docker Compose development
  wrapper; production does not use this entrypoint.
- The effective Uvicorn command in `backend/entrypoint.sh` uses two workers,
  listens on `0.0.0.0:8000` inside the current container network, and trusts
  proxy headers. The Dockerfile contains a stale comment mentioning four
  workers; executable behavior is two.
- `backend/main.py` has no infrastructure-provisioning lifespan. Direct
  `uvicorn main:app` startup does not create or alter database schema, seed
  configuration, or provision Qdrant. Operators run
  `python -m scripts.init_system db|qdrant|all` as an explicit, separate step.
- `backend/main.py` exposes dependency-free `/health/live`, production
  `/health/ready`, and the backward-compatible `/health` endpoint. Readiness
  probes DB-ADMIN and DB-CHAT explicitly as admission gates; MeiliSearch and
  Qdrant failures produce a degraded HTTP 200 response instead of withdrawing
  both APP nodes.
- Maintenance state is centralized as `MAINTENANCE_MODE` in DB-ADMIN
  `SystemConfig`. The admin settings API reads/writes that record and
  `/widget-chat` checks it before persistent or expensive work. An Nginx file
  flag remains only as a node-local emergency override.
- `backend/core/database.py` exposes explicit admin and chat engines and routes
  owned models through a bound session. Production supplies
  `ADMIN_DATABASE_URL` and `CHAT_DATABASE_URL`; development remains compatible
  when both fall back to one `DATABASE_URL`.
- `chatbot-web/Dockerfile` currently performs an Angular production build and
  copies `dist/chatbot-web` into an Nginx image. `chatbot-web/nginx.conf` serves
  the SPA/static assets and proxies `/api/`, `/widget-chat`, and `/health` to
  FastAPI. Native production must retain this request boundary while consuming
  a prebuilt Angular artifact instead of building an image on the APP nodes.
- Development Compose remains pinned to Meilisearch `1.12` and Qdrant `1.13.2`
  and was deliberately not changed by production validation. Isolated real
  integration tests selected native production Meilisearch `1.53.2` with
  client `0.43.0`, and Qdrant `1.19.1` with client `1.19.0`.
- Meilisearch production authentication is already passed through the current
  client. Current Qdrant provider/vector-sync construction has no API-key
  wiring; secure native Qdrant rollout remains blocked on a separate
  application/config change plus private-network and TLS policy.
- Native APP operations are implemented by `deploy/production/scripts/` and
  the `auzef-init@.service` oneshot template. Release preparation, shared
  initialization, node activation, readiness-gated automatic rollback, status,
  health, and journald access remain standard shell/systemd operations.
- Existing deployment guidance in `DEPLOY.md` describes a Docker-based
  production flow. It predates and conflicts with this native-service
  production contract and must be revised during the implementation phase.


DB-ADMIN
────────
QnA
QnAQuery
Tag
QnATag
SystemConfig
AdminUser
AdminSession
AdminLoginAttempt
AcademicCalendar

DB-CHAT
───────
Conversation
ConversationMessage
QueryLog
SolutionCenterSession
SCRateLimit
