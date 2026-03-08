# Ascend Core

Trust-based agent orchestrator for building autonomous AI agent systems with safety guarantees.

## What It Is

Ascend Core provides the execution layer for AI agent systems:

- **Executor** — Runs agent tasks through policy/trust/audit pipeline
- **Trust Engine** — SQLite-backed L0-L4 trust levels with auto-promotion/demotion
- **Policy Engine** — YAML-based action validation, blast radius control, forbidden paths
- **Middleware** — Budget tracking, loop detection, secret scrubbing, verification
- **Memory** — Entity store, exemplars, blackboard, trends, garbage collection
- **Intelligence** — Error classification, recovery strategies, prompt evolution

## What It Is NOT

Ascend Core does not handle orchestration:

- No scheduling (use cron, Temporal, or your own scheduler)
- No messaging (use Telegram, Slack, or your own delivery)
- No webhooks (use your own HTTP server)
- No session management

These are handled by your orchestration layer. Ascend Core is the execution engine underneath.

## Install

```bash
pip install ascend-core
```

Or from source:

```bash
git clone https://github.com/vitamin33/ascend-core.git
cd ascend-core
pip install -e ".[dev]"
```

## Quick Start

```python
from pathlib import Path
from ascend import TrustEngine, PolicyEngine, TaskContract
from ascend.executor import AgentExecutor

# Initialize engines
trust = TrustEngine(Path("data/trust.db"))
policy = PolicyEngine("config/policies.yaml", "config/safety.yaml")
executor = AgentExecutor(trust, policy)

# Create and execute a task
task = TaskContract(
    project="my-project",
    action="code_review",
    description="Review the latest PR for security issues",
    target_files=["src/auth.py"],
    trust_level=0,
)

result = executor.execute(task, agent_id="code-reviewer")
print(f"Status: {result.status}, Output: {result.output[:200]}")
```

See `examples/minimal_daemon.py` for a full HTTP server example.

## Trust Levels

| Level | Name | Can Do | Execution |
|-------|------|--------|-----------|
| L0 | Observer | Read-only analysis | subprocess |
| L1 | Contributor | Draft content, PR comments | subprocess |
| L2 | Trusted | Auto-execute within policy | tmux |
| L3 | Senior | Cross-project workflows | tmux |
| L4 | Architect | System-level changes | tmux |

Agents auto-promote after 10 consecutive successes and auto-demote after 2 failures within 24 hours.

## Architecture

```
TaskContract
    |
    v
[Policy Engine] -- validates action, checks blast radius
    |
    v
[Trust Engine] -- checks agent trust level, decides execution mode
    |
    v
[Executor] -- runs agent via subprocess (L0-L1) or tmux (L2+)
    |
    v
[Audit Log] -- append-only JSONL, never deleted
    |
    v
AgentResult
```

## Configuration

Two YAML files control behavior:

**policies.yaml** — What agents can do
```yaml
projects:
  my-project:
    allowed_actions: [report, code_review, test]
    forbidden_paths: ["*.env", ".secrets/*"]
    blast_radius: low

agents:
  code-reviewer:
    allowed: [code_review, report]
    forbidden: [deploy, delete]
```

**safety.yaml** — Resource limits
```yaml
timeouts:
  default_task_seconds: 300
  max_task_seconds: 900
budget:
  daily_api_calls: 500
```

See `examples/` for complete configuration files.

## Project Structure

```
ascend-core/
  ascend/
    __init__.py          # Package exports
    contracts.py         # TaskContract, AgentResult, WorkflowContract
    executor.py          # Core execution pipeline
    trust_engine.py      # SQLite trust L0-L4
    policy_engine.py     # YAML policy validation
    middleware/           # Budget, loop detection, secrets, verification
    memory/              # Entity store, exemplars, blackboard, trends, GC
    intelligence/        # Error classification, recovery, prompt evolution
  examples/
    minimal_daemon.py    # Working HTTP server (~90 lines)
    policy_example.yaml  # Sample policy config
    safety_example.yaml  # Sample safety config
  tests/
  docs/
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
