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
```

- The release artifact is built and verified before it reaches a production
  VM. Production servers should not compile Angular, resolve application
  dependencies, or perform other builds when a ready artifact can be supplied.
- APP-01 and APP-02 receive the same versioned artifact.
- Activation changes the `current` symlink to the selected release and restarts
  or reloads the relevant `systemd`/Nginx services.
- Rollback points `current` to the previously retained release and restarts or
  reloads the same services. Runtime configuration, secrets, and persistent
  state are not rolled back by changing this symlink.
- Database initialization and migration are explicit deployment operations,
  separate from APP service startup. Starting or restarting Uvicorn must not
  implicitly modify database schema.
- Deploy, rollback, status, health, and log operations will be simplified with
  standard shell and `systemd`/`journalctl` tooling. This document does not add
  those production scripts.

Rolling deployment order is fixed:

1. Drain APP-01 from the LB.
2. Deploy and activate the release on APP-01.
3. Verify APP-01 health.
4. Add APP-01 back to the LB.
5. Drain APP-02 from the LB.
6. Deploy and activate the same release on APP-02.
7. Verify APP-02 health.
8. Add APP-02 back to the LB.

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
- Any APP-local runtime cache that must survive a release switch, including a
  model cache if retained, uses a dedicated path outside the release tree.
- Release directories are treated as immutable after publication. Application
  runtime state must not be written beneath `releases/<release-id>` or
  `current`.
- Secrets are never embedded in the Angular output or release manifest and are
  not committed to the repository.

## Health and Availability

Two health contracts are planned:

- `/health/live`: process liveness only. It answers whether the FastAPI process
  can serve requests and must not require downstream dependencies.
- `/health/ready`: readiness for production traffic. It verifies the dependency
  state required to serve safely and is the endpoint intended for LB admission
  and rolling-deployment gates.

Until that split is implemented, the existing `/health` behavior is only a
current-state compatibility endpoint and is not the final production health
contract.

APP-01 and APP-02 provide application-tier redundancy through the physical LB.
The rolling procedure keeps one APP node in service while the other is being
updated. The six-VM topology does not, by itself, provide PostgreSQL,
MeiliSearch, or Qdrant replication/failover.

Maintenance mode must have one consistent multi-node state. Production must
not depend on an APP-local flag file whose value can differ between APP-01 and
APP-02. Both nodes and the LB-facing response path must observe the same
maintenance decision throughout a deployment or incident.

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
- `backend/entrypoint.sh` runs `scripts.init_system.setup()` before starting
  Uvicorn. That setup creates/updates tables, indexes, a PostgreSQL view, and
  initial configuration. Production conversion must remove this schema work
  from APP startup and expose it as a separate initialization/migration step.
- The effective Uvicorn command in `backend/entrypoint.sh` uses two workers,
  listens on `0.0.0.0:8000` inside the current container network, and trusts
  proxy headers. The Dockerfile contains a stale comment mentioning four
  workers; executable behavior is two.
- The FastAPI lifespan in `backend/main.py` calls
  `QDRANT_PROVIDER.ensure_collection()`. Lifespan runs per Uvicorn worker, so
  collection initialization is currently coupled to application startup and
  can race across workers or APP nodes. Native production preparation must
  make this operation safely idempotent or move it to an explicit setup step.
- `backend/main.py` exposes one public `/health` endpoint. It executes
  `SELECT 1` through the single application database and returns `{"ok": true}`;
  liveness and readiness are not currently separated.
- Maintenance state is currently the presence of
  `/app/flags/maintenance.flag` for the backend and
  `/etc/nginx/flags/maintenance.flag` for Nginx, connected through the Compose
  `ops_flags` volume. Nginx returns HTTP 503 for `/widget-chat` while the flag
  exists. `bakim.sh` also operates through the current Docker containers.
- `backend/core/database.py` creates one SQLAlchemy engine and one
  `SessionLocal` from `DATABASE_URL`. All chat, admin, QnA, calendar, settings,
  session, rate-limit, and logging models currently share that database; there
  is no `ADMIN_DATABASE_URL` or `CHAT_DATABASE_URL` routing yet.
- `chatbot-web/Dockerfile` currently performs an Angular production build and
  copies `dist/chatbot-web` into an Nginx image. `chatbot-web/nginx.conf` serves
  the SPA/static assets and proxies `/api/`, `/widget-chat`, and `/health` to
  FastAPI. Native production must retain this request boundary while consuming
  a prebuilt Angular artifact instead of building an image on the APP nodes.
- The Compose topology pins the Qdrant server image to `1.13.2`, while the
  Python `qdrant-client` dependency is currently unpinned. Native service and
  release dependency versions need an explicit compatibility baseline before
  rollout.
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
