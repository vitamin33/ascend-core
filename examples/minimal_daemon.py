#!/usr/bin/env python3
"""Minimal Ascend daemon — shows core orchestration in ~60 lines.

Starts an HTTP server with:
  POST /execute  — run a task through policy/trust/audit pipeline
  GET  /agents   — list registered agents with trust levels
  GET  /health   — liveness probe

Prerequisites:
  1. pip install ascend-core aiohttp
  2. Create config/policies.yaml and config/safety.yaml (see examples/)
  3. python examples/minimal_daemon.py
"""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from ascend import PolicyEngine, TaskContract, TrustEngine
from ascend.executor import AgentExecutor

# -- Setup -----------------------------------------------------------------

DB_PATH = Path("data/trust.db")
POLICIES_PATH = "config/policies.yaml"
SAFETY_PATH = "config/safety.yaml"
AUDIT_PATH = Path("logs/audit.jsonl")


def create_app() -> web.Application:
    """Wire up the Ascend core and return an aiohttp app."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    trust = TrustEngine(DB_PATH)
    policy = PolicyEngine(POLICIES_PATH, SAFETY_PATH)
    executor = AgentExecutor(trust, policy, audit_log=AUDIT_PATH)

    app = web.Application()
    app["trust"] = trust
    app["executor"] = executor

    app.router.add_post("/execute", handle_execute)
    app.router.add_get("/agents", handle_agents)
    app.router.add_get("/health", handle_health)

    return app


# -- Handlers --------------------------------------------------------------

async def handle_execute(request: web.Request) -> web.Response:
    """POST /execute — run a task through the full pipeline."""
    body = await request.json()

    task = TaskContract(
        project=body["project"],
        action=body["action"],
        description=body["description"],
        target_files=body.get("target_files", []),
        trust_level=body.get("trust_level", 0),
        timeout_seconds=body.get("timeout_seconds", 300),
        agent_type=body.get("agent_type", ""),
    )

    agent_id = body.get("agent_id", task.agent_type or "default")
    executor: AgentExecutor = request.app["executor"]
    result = executor.execute(task, agent_id=agent_id)

    return web.json_response({
        "task_id": result.task_id,
        "status": result.status,
        "output": result.output[:1000],
        "error": result.error,
        "duration_seconds": result.duration_seconds,
    })


async def handle_agents(request: web.Request) -> web.Response:
    """GET /agents — list all registered agents."""
    trust: TrustEngine = request.app["trust"]
    return web.json_response({"agents": trust.list_agents()})


async def handle_health(_request: web.Request) -> web.Response:
    """GET /health — liveness probe."""
    return web.json_response({"status": "ok"})


# -- Entry point -----------------------------------------------------------

if __name__ == "__main__":
    app = create_app()
    print("Ascend daemon starting on http://localhost:8321")
    web.run_app(app, host="localhost", port=8321)
