# Contributing to Ascend Core

## Development Setup

```bash
git clone https://github.com/vitamin33/ascend-core.git
cd ascend-core
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Code Style

- Python 3.12+, type hints on all functions
- No `Any` type (except where unavoidable for third-party compatibility)
- `ruff` for linting, `mypy --strict` for type checking
- Functions max 30 lines, classes max 200 lines
- Docstrings on all public functions

## Running Tests

```bash
pytest tests/ -v
ruff check .
mypy --strict ascend/
```

## Pull Requests

1. Fork the repo and create a feature branch
2. Write tests for new behavior
3. Ensure all tests pass and linting is clean
4. Submit a PR with a clear description

## Architecture Rules

- Policy-first, not prompt-first (safety in YAML, not in prompts)
- Audit JSONL from Day 1 (never skip logging)
- SQLite over Redis (simpler, enough for most workloads)
- Add contract fields only when consumed by real code
- Test real behavior, not mocks
