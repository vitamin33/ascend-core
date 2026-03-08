# Getting Started

## Installation

```bash
pip install ascend-core
```

Or from source:

```bash
git clone https://github.com/vitamin33/ascend-core.git
cd ascend-core
pip install -e ".[dev]"
```

## Step 1: Create Policy Config

Create `config/policies.yaml`:

```yaml
projects:
  my-project:
    allowed_actions:
      - report
      - code_review
    forbidden_paths:
      - "*.env"
      - ".secrets/*"
    blast_radius: low

blast_radius_levels:
  low:
    auto_approve_min_trust: 0
  high:
    auto_approve_min_trust: 4

agents:
  my-agent:
    allowed: [report, code_review]
    forbidden: [deploy, delete]
```

## Step 2: Create Safety Config

Create `config/safety.yaml`:

```yaml
timeouts:
  default_task_seconds: 300
  max_task_seconds: 900

limits:
  max_retries: 2
  max_concurrent_tasks: 3

budget:
  daily_api_calls: 500
  monthly_api_calls: 10000
```

## Step 3: Run Your First Agent

```python
from pathlib import Path
from ascend import TrustEngine, PolicyEngine, TaskContract
from ascend.executor import AgentExecutor

# Initialize
trust = TrustEngine(Path("data/trust.db"))
policy = PolicyEngine("config/policies.yaml", "config/safety.yaml")
executor = AgentExecutor(trust, policy)

# Execute a task
task = TaskContract(
    project="my-project",
    action="report",
    description="Analyze the codebase and list all TODO comments",
    target_files=[],
    trust_level=0,
    agent_type="my-agent",
)

result = executor.execute(task, agent_id="my-agent")
print(f"Status: {result.status}")
print(f"Output: {result.output[:500]}")
```

## Step 4: Build an HTTP Server

See `examples/minimal_daemon.py` for a complete HTTP server using aiohttp.

```bash
pip install aiohttp
python examples/minimal_daemon.py
```

Then test with curl:

```bash
curl -X POST http://localhost:8321/execute \
  -H "Content-Type: application/json" \
  -d '{
    "project": "my-project",
    "action": "report",
    "description": "List all files in src/",
    "agent_id": "my-agent"
  }'
```

## Next Steps

- Read [Trust Levels](TRUST_LEVELS.md) to understand L0-L4
- Customize `policies.yaml` for your projects and agents
- Extend `AgentExecutor._run_subprocess()` for your CLI tools
- Add middleware (budget tracking, loop detection) via `MiddlewarePipeline`
