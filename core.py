"""Shared DB + query logic for the QEMU lifetime report.

Used by both the CLI (`qemu_lifetime_report.py`) and the web UI (`web.py`).
All OpenStack DBs live on a MariaDB replica *per region*; Keystone is shared
across regions and lives on one nominated region's replica (see
`openstack_bi.config`).

Config for connections, regions, and Keystone placement is handled by the
`openstack_bi` package. This module is concerned with domain/project/instance
queries and the lifecycle-action semantics specific to the QEMU report.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from openstack_bi.config import (
    Region,
    keystone_db,
    keystone_region,
    nova_api_db,
    parse_regions,
    resolve_regions,
)
from openstack_bi.db import query
from openstack_bi.util import annotate_ages, humanize  # re-exported for compat

__all__ = [
    "LIFECYCLE_ACTIONS",
    "COMMON_VM_STATES",
    "DEFAULT_VM_STATES",
    "annotate_ages",
    "humanize",
    "list_domains",
    "find_domain",
    "list_projects",
    "list_cells",
    "fetch_instances",
    "collect_report",
    "parse_regions",
    "resolve_regions",
    "keystone_region",
]


# Nova action names that count as a QEMU lifecycle event for this report.
# Reboot, migrate, resize, rebuild, and create are intentionally excluded.
LIFECYCLE_ACTIONS: Tuple[str, ...] = (
    "start",
    "stop",
    "shelve",
    "unshelve",
    "shelveOffload",
    "live-migration",
)


# Common Nova `instances.vm_state` values, in roughly the order users
# care about them. The web UI exposes these as the state-filter options.
COMMON_VM_STATES: Tuple[str, ...] = (
    "active",
    "stopped",
    "paused",
    "suspended",
    "shelved",
    "shelved_offloaded",
    "error",
    "building",
    "rescued",
    "resized",
    "soft-deleted",
)

# Default state filter applied when the caller doesn't specify one:
# the operational interest is in *running* instances.
DEFAULT_VM_STATES: Tuple[str, ...] = ("active",)


def list_domains() -> List[Dict[str, Any]]:
    """Domains in Keystone are rows in `project` with is_domain=1.

    Keystone is shared across regions — we hit the region configured via
    `KEYSTONE_REGION` (or the first configured region by default).
    """
    sql = """
        SELECT d.id, d.name,
               (SELECT COUNT(*) FROM project p
                WHERE p.domain_id = d.id AND p.is_domain = 0 AND p.enabled = 1
               ) AS project_count
        FROM project d
        WHERE d.is_domain = 1 AND d.enabled = 1
        ORDER BY d.name
    """
    return query(keystone_region(), keystone_db(), sql)


def find_domain(needle: str) -> Optional[Dict[str, Any]]:
    """Resolve a domain by id or name."""
    sql = """
        SELECT id, name
        FROM project
        WHERE is_domain = 1 AND enabled = 1 AND (id = %s OR name = %s)
        LIMIT 1
    """
    rows = query(keystone_region(), keystone_db(), sql, (needle, needle))
    return rows[0] if rows else None


def list_projects(domain_id: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT id, name
        FROM project
        WHERE domain_id = %s AND is_domain = 0 AND enabled = 1
        ORDER BY name
    """
    return query(keystone_region(), keystone_db(), sql, (domain_id,))


def list_cells(region: Region) -> List[str]:
    """Discover cell DB names for one region from its `nova_api.cell_mappings`."""
    from urllib.parse import urlparse

    rows = query(
        region,
        nova_api_db(),
        "SELECT name, database_connection FROM cell_mappings ORDER BY id",
    )
    cells: List[str] = []
    for r in rows:
        conn = r.get("database_connection") or ""
        # SQLAlchemy URL: dialect+driver://user:pass@host/dbname
        parsed = urlparse(conn.replace("mysql+pymysql://", "mysql://", 1))
        dbname = (parsed.path or "").lstrip("/")
        if dbname:
            cells.append(dbname)
    return cells


def fetch_instances(
    region: Region,
    cell_db: str,
    project_ids: Sequence[str],
    days: Optional[int],
    vm_states: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Instances in the given projects with their most-recent lifecycle
    action, scoped to one cell DB within one region.

    Keystone is shared across regions and may live on a different replica
    than this region's Nova data, so we do *not* cross-DB-join into
    `keystone.project` here. Callers are expected to resolve project names
    from the shared Keystone separately (see `collect_report`), which is
    also cheaper: one lookup per project, not one join per row.
    """
    if not project_ids:
        return []

    proj_ph = ",".join(["%s"] * len(project_ids))
    act_ph = ",".join(["%s"] * len(LIFECYCLE_ACTIONS))

    sql = f"""
        WITH project_instances AS (
            SELECT uuid, project_id
            FROM instances
            WHERE deleted = 0
              AND project_id IN ({proj_ph})
        ),
        ranked AS (
            SELECT ia.instance_uuid, ia.action, ia.start_time, ia.user_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY ia.instance_uuid
                       ORDER BY ia.start_time DESC
                   ) AS rn
            FROM instance_actions ia
            JOIN project_instances pi ON pi.uuid = ia.instance_uuid
            WHERE ia.deleted = 0
              AND ia.action IN ({act_ph})
        )
        SELECT
            i.uuid                                  AS uuid,
            i.display_name                          AS name,
            i.host                                  AS compute_host,
            i.vm_state                              AS vm_state,
            i.power_state                           AS power_state,
            i.created_at                            AS created_at,
            i.project_id                            AS project_id,
            r.action                                AS last_action,
            r.start_time                            AS last_action_time,
            r.user_id                               AS last_action_user,
            COALESCE(r.start_time, i.created_at)    AS effective_time
        FROM instances i
        LEFT JOIN ranked r ON r.instance_uuid = i.uuid AND r.rn = 1
        WHERE i.deleted = 0
          AND i.project_id IN ({proj_ph})
    """

    args: List[Any] = list(project_ids) + list(LIFECYCLE_ACTIONS) + list(project_ids)

    if vm_states:
        state_ph = ",".join(["%s"] * len(vm_states))
        sql += f" AND i.vm_state IN ({state_ph})"
        args.extend(vm_states)

    if days is not None:
        sql += " AND COALESCE(r.start_time, i.created_at) < (UTC_TIMESTAMP() - INTERVAL %s DAY)"
        args.append(days)

    sql += " ORDER BY i.project_id, effective_time"
    rows = query(region, cell_db, sql, args)
    # Tag every row with the region it came from so multi-region aggregates
    # can render a `region` column downstream.
    for row in rows:
        row["region"] = region.name
    return rows


def collect_report(
    domain_selector: str,
    days: Optional[int],
    vm_states: Optional[Sequence[str]] = None,
    selected_regions: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Resolve a domain by name/id, then fetch + annotate every instance in
    its projects across the selected regions (all regions by default).

    Returns a dict with keys:
        domain    — dict or None if not found
        projects  — list of project dicts (sorted by name; Keystone is shared
                    so this list is region-independent)
        rows      — list of instance dicts (each tagged with `region`;
                    annotated with `age_seconds` and `age`)
        regions   — list of Region objects queried

    `vm_states` filters by `instances.vm_state`. None/empty disables the
    filter; callers wanting "active only" should pass DEFAULT_VM_STATES.
    """
    regions = resolve_regions(list(selected_regions) if selected_regions else None)
    domain = find_domain(domain_selector)
    if domain is None:
        return {"domain": None, "projects": [], "rows": [], "regions": regions}
    projects = list_projects(domain["id"])
    project_ids = [p["id"] for p in projects]
    name_by_id = {p["id"]: p["name"] for p in projects}

    rows: List[Dict[str, Any]] = []
    for region in regions:
        for cell in list_cells(region):
            rows.extend(fetch_instances(region, cell, project_ids, days, vm_states))

    # Resolve project names from the shared Keystone map (Keystone is not
    # cross-DB-joined in the cell query; see `fetch_instances` docstring).
    for row in rows:
        row["project_name"] = name_by_id.get(row["project_id"])

    annotate_ages(rows)
    return {"domain": domain, "projects": projects, "rows": rows, "regions": regions}
