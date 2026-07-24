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
| `.github/workflows/deploy.yml` | GitHub Actions pipeline: auth → validate → pre-hook → deploy → verify. |

## Prerequisites

- **Databricks CLI v1.2.0+** (`bundle deployment bind` requires it). The workflow
  installs it via [`databricks/setup-cli`](https://github.com/databricks/setup-cli).
- A Databricks workspace with **Lakebase** enabled.
- An **OAuth service principal** (M2M) with permission to create Lakebase instances and
  manage the project.
- The target workspace must **not enforce IP access lists** for the standard
  `ubuntu-latest` runner to reach it — see [Network access](#network-access-ip-acls) below.

### Network access (IP ACLs)

The workflow runs on a standard `ubuntu-latest` GitHub-hosted runner, whose egress IP is
**dynamic** (a different address every run). This only works if the target workspace does
**not enforce IP access lists** for the control-plane APIs the pipeline calls.

If the workspace **does** enforce IP ACLs, every call (auth, `bundle validate`, the
pre-hook, `bundle deploy`) is rejected with
`Source IP address ... is blocked by Databricks IP ACL`. A GitHub-hosted runner **cannot**
self-allowlist out of this: the IP-ACL management API is itself gated by the list it would
edit, and the runner's IP changes every run so you can't pre-allowlist a `/32`. (Adding
GitHub's published [`api.github.com/meta`](https://api.github.com/meta) `.actions[]` ranges
doesn't fit either — 7,000+ CIDRs vs the API's 1,000-value cap → `400 QUOTA_EXCEEDED`.)

Pick one of:

- **Disable IP-ACL enforcement on the demo workspace** (simplest, and what the companion
  `lakebase-fleet-controller` pipeline does). Then `ubuntu-latest` just works:

  ```bash
  databricks workspace-conf set-status --json '{"enableIpAccessLists": "false"}'
  ```

- **Use a runner with a static egress IP** (a GitHub larger-runner group with a static IP
  range, or a self-hosted runner behind a NAT gateway) and allowlist that one IP/range
  once, out of band, from an already-permitted host. Then change `runs-on:` to that
  runner's label.

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
