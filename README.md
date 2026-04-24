# qemu-world-lifetime

QEMU instance lifetime reporting for large, multi-region OpenStack
deployments. Queries MariaDB replicas (one per region, plus a shared
Keystone) directly to answer: **for every instance in a given domain — in
every region or just the ones you pick — when was it last started, stopped,
shelved, unshelved, or live-migrated, and how long ago?**

Ships two interfaces that share one query layer:

- **CLI** — interactive menu or flag-driven, plain-text grouped output.
- **Web** — Flask UI with browser tables and one-click Excel export.

## Why query the DB instead of the API or virsh?

- **Centralized.** One MariaDB replica per region beats fanning SSH/virsh
  calls across every compute node.
- **Fast.** No per-instance round-trips; a single CTE returns the whole set
  per cell.
- **Zero control-plane impact.** Reads go to replicas; nothing touches Nova
  services or hypervisors.

The tradeoff: this is *user-visible* uptime (what Nova recorded), not the
underlying QEMU process lifetime. Live-migration is included as a lifecycle
event so operator-initiated moves show up.

## Requirements

- Python 3.8+.
- A MariaDB replica per OpenStack region, each holding that region's
  `nova_api` and `nova_cell*` DBs. One of them (or a separate shared
  replica) must also host the shared `keystone` DB.
- A DB user with `SELECT` on those schemas. Per-region credentials are
  supported.
- MariaDB 10.2+ (the query uses CTEs and window functions). Anything
  Ussuri-era and newer is fine.

## Install

```
git clone git@github.com:ssearles1911/qemu-world-lifetime.git
cd qemu-world-lifetime
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

All config is via environment variables. The CLI and web app auto-load a
`.env` file from the current working directory; real env vars still take
precedence, so you can override per-run.

### Multi-region (recommended)

```
OS_DB_REGIONS=dfw1,ord1
KEYSTONE_REGION=dfw1            # which region's replica reaches `keystone`

OS_DB_HOST__DFW1=replica-dfw1.internal
OS_DB_PORT__DFW1=3306
OS_DB_USER__DFW1=reporting
OS_DB_PASSWORD__DFW1=...

OS_DB_HOST__ORD1=replica-ord1.internal
OS_DB_USER__ORD1=reporting
OS_DB_PASSWORD__ORD1=...

# Optional fallbacks (used when per-region value is missing)
OS_DB_PORT=3306
OS_DB_USER=reporting
```

Per-region suffix convention: `<REGION_NAME>` uppercased, with any non-
alphanumeric character replaced by an underscore — so `dfw1` → `DFW1`,
`us-east-2` → `US_EAST_2`.

### Single-region (legacy / backwards compatible)

If `OS_DB_REGIONS` is unset but the bare `OS_DB_HOST` / `OS_DB_USER` /
`OS_DB_PASSWORD` variables are set, a single region named `default` is
synthesized. Existing deployments keep working without any `.env` changes.

### Variable reference

| Variable                 | Default     | Purpose                                                |
| ------------------------ | ----------- | ------------------------------------------------------ |
| `OS_DB_REGIONS`          | *(unset)*   | Comma-separated region names. Empty ⇒ single-region fallback. |
| `KEYSTONE_REGION`        | first listed | Region whose replica hosts shared `keystone`.         |
| `OS_DB_HOST__<REGION>`   | `127.0.0.1` | Replica host for one region.                           |
| `OS_DB_PORT__<REGION>`   | `3306`      | Replica port for one region.                           |
| `OS_DB_USER__<REGION>`   | `nova`      | DB user for one region.                                |
| `OS_DB_PASSWORD__<REGION>` | *(empty)* | DB password for one region.                            |
| `OS_DB_HOST`, etc.       | —           | Fallback values if a per-region variable is missing.   |
| `KEYSTONE_DB`            | `keystone`  | Keystone schema name.                                  |
| `NOVA_API_DB`            | `nova_api`  | Used for cell auto-discovery.                          |
| `QLR_HOST`               | `127.0.0.1` | `web.py` bind host.                                    |
| `QLR_PORT`               | `8000`      | `web.py` bind port.                                    |

### `.env` file (recommended)

```
cp .env.example .env
$EDITOR .env
```

`.env` is gitignored. Overriding for a single run:

```
OS_DB_PASSWORD__DFW1=oneoff python qemu_lifetime_report.py --list-domains
```

## CLI usage

```
# interactive — prompts for domain, then for the min-days filter
python qemu_lifetime_report.py

# fully non-interactive (defaults to all regions, vm_state=active)
python qemu_lifetime_report.py --domain heroes --days 80

# scope to specific regions
python qemu_lifetime_report.py --domain heroes --region dfw1 --region ord1

# include specific non-active states
python qemu_lifetime_report.py --domain heroes --state active --state stopped

# disable the state filter entirely
python qemu_lifetime_report.py --domain heroes --all-states

# helpers
python qemu_lifetime_report.py --list-domains
python qemu_lifetime_report.py --list-regions
python qemu_lifetime_report.py --list-cells       # groups by region
python qemu_lifetime_report.py --help
```

Output is grouped by project under the selected domain, sorted oldest-first
within each project so long-idle VMs surface at the top. Each row shows
the region the instance lives in.

**Region filter:** defaults to *all* configured regions. Pass `--region
NAME` (repeatable) to restrict, or `--all-regions` to be explicit about the
default.

**State filter:** by default only instances in `vm_state=active` are
reported — the operational use case is running VMs. Use `--state`
(repeatable) to pick other states, or `--all-states` to see everything.

**Days filter:** with `--days N`, only instances whose most-recent
lifecycle action is older than `N` days are shown. Instances with *no*
recorded lifecycle action are included and anchored to
`instances.created_at` (so a never-touched VM still shows up under any
`--days` filter — usually what you want).

## Web usage

```
python web.py
# → http://127.0.0.1:8000/
```

Pick a domain, tick the regions you want (all on by default), pick a state
(defaults to `active`; pick *— all states —* to turn the filter off),
optionally set a minimum-days filter, click **Run report**. The report
renders grouped by project with a `region` column; click **Download
Excel** to fetch the same query as an `.xlsx` with:

- Metadata header (domain, regions, state filter, days filter, action set,
  generated-at timestamp).
- Frozen table header row and auto-filter on every column.
- A `region` column for cross-region sorting/filtering.
- A numeric `age_days` column alongside the human-readable `age`, so Excel
  can sort/filter properly.

Bind elsewhere:

```
QLR_HOST=0.0.0.0 QLR_PORT=8000 python web.py
```

For a long-running deployment, use a production WSGI server instead of the
Flask dev server (no code changes needed):

```
pip install waitress
waitress-serve --host=0.0.0.0 --port=8000 web:app
```

## Lifecycle actions tracked

The report considers exactly these `nova.instance_actions.action` values:

```
start, stop, shelve, unshelve, shelveOffload, live-migration
```

Deliberately excluded: `reboot`, `migrate` (cold), `resize`, `rebuild`,
`create`. These either don't match the operational signal of interest
(reboots don't correlate with maintenance windows) or duplicate
information already captured elsewhere (`create` = instance age).

To change the set, edit `LIFECYCLE_ACTIONS` in `core.py` — the CLI, web
UI, and Excel export all read from it.

## How it works

1. Parses per-region connection details from env/`.env` into a list of
   `Region` objects (`openstack_bi.config`).
2. Resolves the shared domain/project list once from the `KEYSTONE_REGION`
   replica (`keystone.project`).
3. For each selected region, discovers its cell DBs from
   `nova_api.cell_mappings` and runs one query per cell that:
   - pre-filters `instances` to projects in the selected domain,
   - picks each instance's most recent lifecycle action via a CTE +
     `ROW_NUMBER() OVER (PARTITION BY instance_uuid ORDER BY start_time DESC)`.
4. Aggregates rows from every cell in every region in Python; tags each
   row with its region; joins project names from the Keystone map;
   computes age from `COALESCE(last_action_time, instances.created_at)`;
   renders.

The same `core.collect_report()` call powers the CLI, the web UI, and the
Excel export — the table you see in the browser and the rows in the
downloaded spreadsheet come from one query pass and are guaranteed to
match.

## Project layout

```
openstack_bi/            multi-region config, DB access layer, shared utils
  config.py              Region dataclass; parse_regions(); keystone_region()
  db.py                  connect/query against a single (region, database)
  util.py                humanize, annotate_ages
core.py                  QEMU-lifetime-specific queries and constants
qemu_lifetime_report.py  CLI entry point
web.py                   Flask app + .xlsx export
templates/index.html     single-page UI
requirements.txt         PyMySQL, Flask, openpyxl, python-dotenv
```

## Limitations and notes

- **`instance_actions` retention.** Nova can be configured to purge old
  action rows. Instances older than the retention horizon are reported
  with `last_action = (none recorded)` and age anchored to `created_at`.
- **Live migration counts as a lifecycle event — by design.** If you want
  "user-requested" events only, remove `live-migration` from
  `LIFECYCLE_ACTIONS`.
- **One domain per run.** Cross-domain aggregation isn't built in yet.
- **`cell0` is included.** It normally holds failed-to-schedule instances
  with no lifecycle data; cost is negligible.
- **Shared Keystone assumed.** Project IDs are expected to be globally
  unique across regions. The report resolves project names once from
  `KEYSTONE_REGION` rather than cross-DB-joining per cell, so Keystone
  and Nova can live on different physical replicas.
- **The web UI is unauthenticated** and binds to `127.0.0.1` by default.
  Put it behind auth (basic-auth reverse proxy, SSO) or keep it local
  before exposing widely.
- **Read-only replica assumption.** Nothing in this project writes; point
  it at replicas to keep the control plane out of the hot path.
