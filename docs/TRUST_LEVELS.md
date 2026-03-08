# Trust Levels

Ascend uses a 5-level trust system (L0-L4) to control what agents can do.

## Levels

### L0 — Observer
- **Can do:** Read-only analysis, reporting
- **Execution:** subprocess (fast, <120s)
- **Delivery:** Results sent to human for review

### L1 — Contributor
- **Can do:** Draft content, PR comments, code review
- **Execution:** subprocess (fast, <120s)
- **Delivery:** Human approves/rejects before action

### L2 — Trusted
- **Can do:** Auto-execute within policy boundaries
- **Execution:** tmux (long-running, >120s supported)
- **Delivery:** Executes automatically, notifies human

### L3 — Senior
- **Can do:** Cross-project workflows
- **Execution:** tmux
- **Delivery:** Executes automatically with audit trail

### L4 — Architect
- **Can do:** System-level changes, infrastructure
- **Execution:** tmux
- **Delivery:** Full autonomy with audit trail

## Promotion

An agent is promoted one level when it completes **10 consecutive successful runs**:

```
L0 (10 successes) -> L1 (10 more) -> L2 -> L3 -> L4
```

Promotion can also be scoped to a specific project — an agent might be L2 in one project but still L0 in another.

## Demotion

An agent is demoted one level when it accumulates **2 failures within 24 hours**:

```
L3 (2 failures in 24h) -> L2
```

L0 agents cannot be demoted further.

## Manual Override

Trust levels can be set manually:

```python
trust.set_trust_level("my-agent", TrustLevel.L2)
```

This logs the change in the trust_log table for auditability.

## SQLite Schema

```sql
-- Agents table
CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    trust_level INTEGER NOT NULL DEFAULT 0,
    registered_at TEXT NOT NULL
);

-- Run history (used for promotion/demotion)
CREATE TABLE agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL
);

-- Trust level change log (audit trail)
CREATE TABLE trust_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    from_level INTEGER NOT NULL,
    to_level INTEGER NOT NULL,
    reason TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
```
