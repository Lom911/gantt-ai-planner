"""Live-LLM eval set: scenarios run against a deployed app over its public HTTP API.

NOT part of pytest or CI (pytest only collects `tests/`): every run talks to a real LLM,
costs tokens and uses the target's per-IP limits. See README.md.
"""
