"""Shared helpers for offline, repeatable scheduler performance tests."""
from __future__ import annotations

import ast
import importlib
import os
import time
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Modules in this repository. A missing name in this list usually indicates an app bug,
# while missing third-party packages can be reported as an explicit skip.
PROJECT_IMPORT_ROOTS = {
    "config", "flags", "messaging", "middleware", "migrations", "models", "routes",
    "services", "tasks", "telemetry", "utilities", "auth", "celery_app", "dlq",
    "engine", "main", "schemas",
}


def source_path(relative_path: str) -> Path:
    path = (PROJECT_ROOT / relative_path).resolve()
    if PROJECT_ROOT not in path.parents and path != PROJECT_ROOT:
        raise ValueError(f"Path escapes project root: {relative_path}")
    return path


def assert_source_scan_is_fast(relative_path: str, *, iterations: int = 12) -> None:
    """Repeatedly parse/compile the target file as a cheap regression guard for tooling."""
    path = source_path(relative_path)
    assert path.is_file(), f"Expected project file does not exist: {path}"
    source = path.read_text(encoding="utf-8", errors="replace")
    started = time.perf_counter()
    for _ in range(iterations):
        tree = ast.parse(source, filename=str(path))
        compile(tree, str(path), "exec")
        # Walk the AST so the test catches unusually expensive source-analysis regressions.
        sum(1 for _node in ast.walk(tree))
    elapsed = time.perf_counter() - started
    budget = float(os.getenv("PERF_SOURCE_SCAN_BUDGET_SECONDS", "5.0"))
    assert elapsed < budget, (
        f"Source analysis for {relative_path} took {elapsed:.3f}s; budget is {budget:.3f}s"
    )


def _block_huggingface_model_downloads() -> None:
    """Set offline flags without importing heavy model libraries just to patch them."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def import_module_for_performance(module_name: str, *, budget_seconds: float | None = None):
    """Import a module with external clients isolated by conftest and enforce an import budget."""
    if module_name in {"utilities.tokenizer", "routes.meeting_scheduler", "main"}:
        _block_huggingface_model_downloads()
    started = time.perf_counter()
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = (exc.name or "").split(".", 1)[0]
        if missing not in PROJECT_IMPORT_ROOTS:
            pytest.skip(f"Optional runtime dependency {exc.name!r} is not installed")
        raise
    elapsed = time.perf_counter() - started
    default_budget = float(os.getenv("PERF_IMPORT_BUDGET_SECONDS", "20.0"))
    budget = budget_seconds if budget_seconds is not None else default_budget
    assert elapsed < budget, (
        f"Importing {module_name} took {elapsed:.3f}s; budget is {budget:.3f}s. "
        "The test environment uses fake Redis clients and blocks model downloads."
    )
    return module


def assert_text_scan_is_fast(relative_path: str, *, iterations: int = 80) -> None:
    """Measure repeated reads/line scans of a configuration or documentation file."""
    path = source_path(relative_path)
    assert path.is_file(), f"Expected project file does not exist: {path}"
    started = time.perf_counter()
    for _ in range(iterations):
        content = path.read_text(encoding="utf-8", errors="replace")
        _ = sum(1 for line in content.splitlines() if line.strip())
    elapsed = time.perf_counter() - started
    budget = float(os.getenv("PERF_TEXT_SCAN_BUDGET_SECONDS", "3.0"))
    assert elapsed < budget, (
        f"Repeated read/scan of {relative_path} took {elapsed:.3f}s; "
        f"budget is {budget:.3f}s"
    )
