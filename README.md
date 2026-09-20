# Data Lineage Trust Platform

Minimal backend service used as the starting point for a data-lineage and trustworthy-computing platform.

## Requirements

- Python 3.12+

## Install and test

```bash
python -m pip install -e '.[dev]'
python -m pytest
```

## Start

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Public API

- `GET /health` returns `{"status":"ok"}` when the service is ready.

