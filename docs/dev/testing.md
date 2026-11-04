# Testing & Benchmarking Suite

The codebase maintains strict quality gates across unit, integration, performance, and end-to-end test suites (`tests/`).

---

## Test Hierarchy & Scope

```text
tests/
├── unit/           # Offline isolated unit tests (timing budget enforced)
├── integration/    # Cross-component integration tests (Redis & DB mocks)
├── performance/    # Concurrency stress benchmarks & rate-limiting load checks
└── e2e/            # Full end-to-end user scheduling workflow tests
```

---

## Execution Commands

### 1. Run Unit Tests (Enforced Fast Execution)

```bash
pytest -q tests/unit -m unit
```

### 2. Run Integration Tests

```bash
pytest -q tests/integration
```

### 3. Run Performance Stress Benchmarks

```bash
pytest -q tests/performance
```

### 4. Run End-to-End Workflow Tests

```bash
pytest -q tests/e2e
```

### 5. Generate Test Coverage Report

```bash
pytest --cov=. --cov-report=html
```
