<p align="center">
  <img src="assets/banner.jpeg" alt="Databricks" width="100%"/>
</p>

<h1 align="center">Lakebase Dual-Network CI/CD</h1>

<p align="center">
  Migrate a <b>Provisioned</b> Lakebase instance — the only path to the
  <b>Dual Networking</b> feature — into an <b>Autoscaling</b> Asset Bundle via a CI/CD
  pre-hook, then manage it with a normal <code>databricks bundle deploy</code>.
</p>

---

## Why this exists

New Lakebase databases can be onboarded onto the **Autoscaling** resource model
directly — Autoscaling is the default going forward. This repo is **not** about that.

It is specifically for customers who need the **Dual Networking** feature, which is
exposed **only through the Provisioned Lakebase APIs**. That capability is available
only to customers **migrating from a Provisioned instance** — you cannot get Dual
Networking by starting fresh on Autoscaling. So the required sequence is:

1. Create the instance the **legacy Provisioned** way (this is what unlocks Dual
   Networking on that instance).
2. **Bind** the Autoscaling resource keys to that existing instance so a Databricks
   Asset Bundle can manage it — with zero data movement.
3. From then on, manage it purely through normal `databricks bundle deploy` CI/CD.

This repo packages steps 1–2 as a **one-time CI/CD pre-hook**. Once the instance exists
and is bound, the pre-hook is a **no-op** — the DAB owns the resource and every
subsequent push just runs `bundle deploy`.

## How it works

The `databricks.yml` defines the **end state**: a Lakebase project managed through the
Autoscaling resource types (`postgres_projects` / `postgres_branches` /
`postgres_endpoints`), including a `production` branch, a `feature-x` branch, and a
read/write endpoint.

The pipeline reaches that state in two phases:

```
┌─ Pre-hook (scripts/lakebase_prehook.py) — ONE-TIME cutover ───────────┐
│  Read the Lakebase project from databricks.yml, then:                 │
│                                                                       │
│  • If the instance ALREADY EXISTS → NO-OP.                            │
│      It has already been created + bound and is DAB-managed.          │
│      The hook does nothing and returns.                               │
│                                                                       │
│  • If the instance does NOT exist → run the cutover:                  │
│    Step 1  create it the LEGACY PROVISIONED way (this is what         │
│            unlocks Dual Networking) and wait for AVAILABLE:           │
│              databricks database create-database-instance <name> \    │
│                  --capacity CU_2                                       │
│    Step 2  BIND the autoscaling resource keys to it                   │
│            (zero data movement — adopts the new database):            │
│              databricks bundle deployment bind pg_root    projects/<name>              │
│              databricks bundle deployment bind pg_prod    projects/<name>/branches/production            │
│              databricks bundle deployment bind pg_prod_rw projects/<name>/branches/production/endpoints/primary │
└───────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─ Natural CI/CD (every run) ────────────────────────────────────────────┐
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
| `scripts/lakebase_prehook.py` | Pre-hook: parse bundle → if instance missing, create legacy + bind; else no-op. Stdlib + `databricks` CLI only. |
| `.github/workflows/deploy.yml` | GitHub Actions pipeline: allowlist runner IPs → validate → pre-hook → deploy → verify. |

## Prerequisites

- **Databricks CLI v1.2.0+** (`bundle deployment bind` requires it). The workflow
  installs it via [`databricks/setup-cli`](https://github.com/databricks/setup-cli).
- A Databricks workspace with **Lakebase** enabled.
- An **OAuth service principal** (M2M) with permission to create Lakebase instances,
  manage the project, **and manage IP access lists** (needed for the allowlist step).

### Network access (IP ACLs)

If the target workspace enforces **IP access lists**, the GitHub-hosted runner must be
allowlisted or every workspace API call (pre-hook + `bundle deploy`) will be rejected.
The workflow handles this **just-in-time**:

1. **Allowlist this runner's IP** — detects the runner's public IP (`api.ipify.org`),
   enables IP access lists on the workspace, and creates a `gha-<run-id>` ALLOW list
   containing only that single `/32`.
2. Runs validate → pre-hook → deploy → verify.
3. **Remove this runner's IP allowlist** — an `always()` cleanup step deletes that ALLOW
   list even if an earlier step failed, so no stale entries accumulate.

Why a single `/32` and not GitHub's published ranges: GitHub's
[`api.github.com/meta`](https://api.github.com/meta) `.actions[]` list is **7,000+ rotating
CIDRs**, but the Databricks IP ACL API caps at **1,000 values combined**
(`400 QUOTA_EXCEEDED`) — so the full meta set cannot be added. The just-in-time `/32` is
one value, minimal exposure, and self-cleaning.

Caveats:

- The service principal needs **admin rights to manage IP access lists**.
- If your workspace does **not** enforce IP ACLs, delete the allowlist + cleanup steps.
- For a tighter, persistent posture, use a **self-hosted runner** with a static egress IP
  and allowlist just that one address once (outside CI).

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
    default: dual-network-demo-2   # your Lakebase project name
  capacity:
    default: CU_2                  # capacity used when first creating the instance
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

Re-runs are **idempotent**: once the instance exists the pre-hook is a **complete
no-op** (it neither creates nor re-binds — the DAB already owns the resource), and
`bundle deploy` simply reconciles (e.g. adds a new branch).

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
