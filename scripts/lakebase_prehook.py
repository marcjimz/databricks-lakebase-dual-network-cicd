#!/usr/bin/env python3
"""
Lakebase dual-network CI/CD pre-hook.

Runs the manual "provision legacy -> bind" cutover as an automated step BEFORE
the normal `databricks bundle deploy`. Concretely:

  Step 1  Read the Lakebase project defined in databricks.yml.
          If the instance does NOT already exist in the workspace, create it the
          LEGACY PROVISIONED way (databricks database create-database-instance)
          and wait for it to reach AVAILABLE.
  Step 2  BIND the bundle's autoscaling resource keys (postgres_projects /
          postgres_branches / postgres_endpoints) to that existing instance, so
          the bundle adopts it with zero data movement.

No unbind is required: the instance is never declared as a `database_instances`
resource, so the bundle only ever tracks it through the autoscaling keys.

After this hook succeeds, the workflow runs `databricks bundle deploy` as a
normal CI/CD step — which reconciles the bundle and creates any new branches.

Only depends on the Python standard library and the `databricks` CLI (v1.2.0+),
which must be on PATH and authenticated (OAuth service principal in CI:
DATABRICKS_HOST + DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def log(msg: str) -> None:
    print(f"[lakebase-prehook] {msg}", flush=True)


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a databricks CLI command, echoing it first."""
    log("$ " + " ".join(cmd))
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


# --------------------------------------------------------------------------- #
# Bundle config resolution
# --------------------------------------------------------------------------- #
def load_bundle_config(cli: str, target: str, profile: str | None) -> dict:
    """
    Resolve the fully-interpolated bundle config via `databricks bundle validate
    -o json`. This expands ${var.*} references so we read real values, not
    templates. Falls back to raw YAML parsing only if the CLI output isn't JSON.
    """
    cmd = [cli, "bundle", "validate", "-t", target, "-o", "json"]
    if profile:
        cmd += ["-p", profile]
    proc = run(cmd, capture=True)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        log("Could not parse `bundle validate -o json`; output was:")
        log(proc.stdout)
        raise


def resolve_vars(value: str, config: dict) -> str:
    """
    Resolve ${var.NAME} references against the bundle's variables map.

    NOTE: `bundle validate -o json` does NOT interpolate ${var.*} / ${resources.*}
    references — it echoes them verbatim — so we resolve the ones we need here.
    A variable's effective value is `value` (if the CLI recorded an override) else
    `default`.
    """
    variables = config.get("variables", {})

    def repl(m: re.Match) -> str:
        name = m.group(1)
        var = variables.get(name, {})
        resolved = var.get("value", var.get("default"))
        if resolved is None:
            raise SystemExit(f"Cannot resolve ${{var.{name}}} — no value/default in databricks.yml.")
        return str(resolved)

    return re.sub(r"\$\{var\.([A-Za-z_][A-Za-z0-9_]*)\}", repl, value)


def extract_lakebase(config: dict) -> tuple[str, list[str], str | None]:
    """
    Pull the Lakebase project id, branch ids, and the RW endpoint's branch from
    the bundle config, resolving any ${var.*} / ${resources.*} references.

    Returns (project_id, [branch_ids], rw_endpoint_branch_id or None).
    """
    resources = config.get("resources", {})
    projects = resources.get("postgres_projects", {})
    if not projects:
        raise SystemExit("No postgres_projects defined in databricks.yml — nothing to provision.")

    # Simple single-project model (this demo defines exactly one).
    _, project = next(iter(projects.items()))
    project_id = resolve_vars(str(project["project_id"]), config)

    branches = resources.get("postgres_branches", {})
    # branch_id values are literal strings in the bundle.
    branch_ids = [b["branch_id"] for b in branches.values()]

    # Which branch does the RW endpoint hang off? Its `parent` is one of:
    #   ${resources.postgres_branches.<key>.id}   (unresolved reference), or
    #   projects/<name>/branches/<branch_id>       (already-resolved path).
    rw_branch = None
    endpoints = resources.get("postgres_endpoints", {})
    for ep in endpoints.values():
        parent = str(ep.get("parent", ""))
        ref = re.search(r"\$\{resources\.postgres_branches\.([^.}]+)\.", parent)
        if ref:
            branch_key = ref.group(1)
            rw_branch = branches.get(branch_key, {}).get("branch_id")
        else:
            m = re.search(r"branches/([^/]+)", parent)
            if m:
                rw_branch = m.group(1)
        if rw_branch:
            break

    return project_id, branch_ids, rw_branch


# --------------------------------------------------------------------------- #
# Step 1 — check existence, create legacy-provisioned if missing
# --------------------------------------------------------------------------- #
def instance_exists(cli: str, name: str, profile: str | None) -> bool:
    cmd = [cli, "database", "get-database-instance", name]
    if profile:
        cmd += ["-p", profile]
    proc = run(cmd, check=False, capture=True)
    if proc.returncode == 0:
        try:
            state = json.loads(proc.stdout).get("state")
            log(f"Instance '{name}' exists (state={state}).")
        except json.JSONDecodeError:
            log(f"Instance '{name}' exists.")
        return True
    log(f"Instance '{name}' not found — will create it the legacy provisioned way.")
    return False


def create_legacy_instance(cli: str, name: str, capacity: str, profile: str | None) -> None:
    """Create the instance via the legacy provisioned API and wait for AVAILABLE."""
    cmd = [cli, "database", "create-database-instance", name, "--capacity", capacity]
    if profile:
        cmd += ["-p", profile]
    # CLI waits for AVAILABLE by default (--timeout 20m); we keep that behavior.
    run(cmd)
    log(f"Instance '{name}' created and AVAILABLE (capacity={capacity}).")


# --------------------------------------------------------------------------- #
# Step 2 — bind autoscaling keys to the existing instance
# --------------------------------------------------------------------------- #
def bind(cli: str, key: str, resource_path: str, target: str, profile: str | None) -> None:
    """
    Bind one bundle resource key to its backend resource path. Idempotent: if the
    key is already bound, the CLI reports so and we treat it as success.
    """
    cmd = [cli, "bundle", "deployment", "bind", key, resource_path, "--auto-approve", "-t", target]
    if profile:
        cmd += ["-p", profile]
    proc = run(cmd, check=False, capture=True)
    print(proc.stdout, end="")
    if proc.returncode == 0:
        return
    lowered = (proc.stdout or "").lower()
    if "already" in lowered and "bound" in lowered:
        log(f"'{key}' already bound — no-op.")
        return
    raise SystemExit(f"bind failed for '{key}' -> {resource_path} (exit {proc.returncode})")


def bind_all(cli: str, project_id: str, rw_branch: str | None, target: str, profile: str | None) -> None:
    """
    Bind the three autoscaling keys used by this bundle. Keys are the resource
    names declared in databricks.yml (pg_root / pg_prod / pg_prod_rw).
    """
    bind(cli, "pg_root", f"projects/{project_id}", target, profile)
    bind(cli, "pg_prod", f"projects/{project_id}/branches/production", target, profile)
    if rw_branch:
        bind(
            cli,
            "pg_prod_rw",
            f"projects/{project_id}/branches/{rw_branch}/endpoints/primary",
            target,
            profile,
        )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Lakebase dual-network CI/CD pre-hook (create-legacy + bind).")
    parser.add_argument("--bundle-dir", default=".", help="Directory containing databricks.yml (default: cwd).")
    parser.add_argument("--target", default="dev", help="Bundle target (default: dev).")
    parser.add_argument("--profile", default=None, help="Databricks CLI profile (omit in CI; use env auth).")
    parser.add_argument("--cli", default="databricks", help="Path to the databricks CLI (default: databricks on PATH).")
    args = parser.parse_args()

    bundle_dir = Path(args.bundle_dir).resolve()
    if not (bundle_dir / "databricks.yml").exists():
        raise SystemExit(f"No databricks.yml in {bundle_dir}")

    # The CLI resolves the bundle from cwd; operate inside the bundle dir.
    import os

    os.chdir(bundle_dir)

    log(f"CLI: {args.cli} | target: {args.target} | dir: {bundle_dir}")
    run([args.cli, "--version"], check=False, capture=True)

    config = load_bundle_config(args.cli, args.target, args.profile)
    project_id, branch_ids, rw_branch = extract_lakebase(config)
    log(f"Lakebase project: {project_id} | branches: {branch_ids} | rw endpoint branch: {rw_branch}")

    # Step 1 — ensure the instance exists (create legacy-provisioned if not).
    cap_var = config.get("variables", {}).get("capacity", {})
    capacity = cap_var.get("value") or cap_var.get("default") or "CU_2"
    if not instance_exists(args.cli, project_id, args.profile):
        create_legacy_instance(args.cli, project_id, capacity, args.profile)

    # Step 2 — bind the autoscaling keys to the existing instance.
    log("Binding autoscaling resource keys to the existing instance...")
    bind_all(args.cli, project_id, rw_branch, args.target, args.profile)

    log("Pre-hook complete. The pipeline can now run `databricks bundle deploy`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
