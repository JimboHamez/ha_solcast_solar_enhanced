# Python Virtual Environment — Test Setup

## Initial setup

Run once from the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements_test.txt
```

This creates `.venv/` at the repo root and installs pytest plus the
`pytest-homeassistant-custom-component` harness.

### Optional dependencies (for scipy/DB tests)

```bash
pip install aiomysql>=0.2.0
pip install numpy>=1.21.0 scipy>=1.7.0
```

Without these, the aiomysql tests are skipped and the scipy tuning test
is skipped automatically.

---

## Reconnecting to an existing venv

Each new terminal session requires reactivating the venv:

```bash
source .venv/bin/activate
```

Your prompt will change to show `(.venv)`. To deactivate:

```bash
deactivate
```

---

## Running the tests

```bash
# All tests
pytest

# Single file
pytest tests/test_coordinator.py

# With coverage report
pytest --cov=custom_components/solcast_solar_enhanced --cov-report=term-missing

# Verbose output
pytest -v
```

---

## Checking the venv is active

```bash
which python   # should point to .venv/bin/python
which pytest   # should point to .venv/bin/pytest
```

---

## Recreating the venv

If the venv becomes broken or you need a clean slate:

```bash
deactivate          # if currently active
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements_test.txt
```
