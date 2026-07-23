<p align="center">
  <img src="assets/databricks-logo.png" alt="Databricks" height="60"/>
</p>

<h1 align="center">Lakebase Dual-Network CI/CD</h1>

<p align="center">
  Automate the Lakebase <b>Provisioned → Autoscaling</b> onboarding as a CI/CD pre-hook,
  then deploy the autoscaling bundle with a normal <code>databricks bundle deploy</code>.
</p>

---

## Why this exists

Onboarding a Lakebase database onto the **Autoscaling** resource model today involves
manual steps: create the instance the legacy provisioned way, then `bind` the
autoscaling resources to it before your bundle can manage it. For customers rolling
this out across many workspaces (e.g. a dual-networking migration), doing that by hand
is toil and a source of error.

This repo packages that cutover as a **CI/CD pre-hook** so it runs automatically and
repeatably. The `databricks.yml` declares the desired autoscaling end-state; the
pipeline reaches it without any manual CLI work.

## How it works

The `databricks.yml` defines the **end state**: a Lakebase project managed through the
Autoscaling resource types (`postgres_projects` / `postgres_branches` /
`postgres_endpoints`), including a `production` branch, a `feature-x` branch, and a
read/write endpoint.

The pipeline reaches that state in two phases:

```
┌─ Pre-hook (scripts/lakebase_prehook.py) ──────────────────────────────┐
│  Step 1  Read the Lakebase project from databricks.yml.               │
│          If the instance does NOT exist in the workspace, create it   │
│          the LEGACY PROVISIONED way and wait for AVAILABLE:           │
│              databricks database create-database-instance <name> \    │
│                  --capacity CU_2                                       │
│  Step 2  BIND the autoscaling resource keys to that instance          │
│          (zero data movement — adopts the existing database):         │
│              databricks bundle deployment bind pg_root    projects/<name>              │
│              databricks bundle deployment bind pg_prod    projects/<name>/branches/production            │
│              databricks bundle deployment bind pg_prod_rw projects/<name>/branches/production/endpoints/primary │
└───────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─ Natural CI/CD ───────────────────────────────────────────────────────┐
│  databricks bundle deploy                                             │
│    Reconciles the bound instance and CREATES any new branches         │
│    declared in the bundle (e.g. feature-x).                           │
└───────────────────────────────────────────────────────────────────────┘
```

**No `unbind` step is needed.** The instance is never declared as a
`database_instances` (provisioned) resource in the bundle, so the bundle only ever
tracks it through the autoscaling keys. `bind` alone is sufficient to hand management
to the bundle.

## Repository layout

| Path | Purpose |
|------|---------|
| `databricks.yml` | The Lakebase autoscaling bundle (project + branches + RW endpoint). |
| `scripts/lakebase_prehook.py` | Pre-hook: parse bundle → check exists → create legacy → bind. Stdlib + `databricks` CLI only. |
| `.github/workflows/deploy.yml` | GitHub Actions pipeline: validate → pre-hook → deploy → verify. |

## Prerequisites

- **Databricks CLI v1.2.0+** (`bundle deployment bind` requires it). The workflow
  installs it via [`databricks/setup-cli`](https://github.com/databricks/setup-cli).
- A Databricks workspace with **Lakebase** enabled.
- An **OAuth service principal** (M2M) with permission to create Lakebase instances and
  manage the project.

## Setup

Configure these as **repository secrets** (Settings → Secrets and variables → Actions):

| Secret | Value |
|--------|-------|
| `DATABRICKS_HOST` | `https://<workspace-host>` |
| `DATABRICKS_CLIENT_ID` | Service principal application (client) ID |
| `DATABRICKS_CLIENT_SECRET` | Service principal OAuth secret |

Adjust the project name and capacity in `databricks.yml`:

```yaml
variables:
  instance_name:
    default: dual-network-demo   # your Lakebase project name
  capacity:
    default: CU_2                # capacity used when first creating the instance
```

## Run it

- **In CI:** push to `main`, or trigger the **Lakebase Dual-Network Deploy** workflow
  manually (`workflow_dispatch`).
- **Locally** (using a CLI profile instead of env auth):

  ```bash
  databricks bundle validate -t dev -p <profile>
  python3 scripts/lakebase_prehook.py --target dev --profile <profile>
  databricks bundle deploy -t dev -p <profile>
  ```

Re-runs are **idempotent**: if the instance already exists the pre-hook skips creation,
the binds are no-ops, and `bundle deploy` simply reconciles (e.g. adds a new branch).

## Replicate for a customer

1. Fork/clone this repo.
2. Set `instance_name` / `capacity` (and add more branches under `postgres_branches`) in
   `databricks.yml` to match the customer's Lakebase project.
3. Add the three repository secrets for the target workspace.
4. Push. The pipeline provisions (if needed), binds, and deploys — no manual CLI steps.

## Notes

- New branches **require an expiration** — set `no_expiry: true` (or `ttl` / `expire_time`).
  Omitting it fails with `400 INVALID_PARAMETER_VALUE`.
- `bind` is a zero-data-movement adoption: a provisioned instance is already backed by an
  autoscaling project, so binding simply points the bundle at the existing database.
- Do not switch a `database` resource to `postgres` in a Databricks App config — the two
  resource types create separate Postgres roles.
