# Flight Delay MLOps

FastAPI сервис для демонстрации MLOps workflow: контейнеризация приложения, работа с PostgreSQL, автоматические проверки качества и публикация Docker-образа.

## Stack

- Python 3.12
- FastAPI
- PostgreSQL 17
- asyncpg
- Docker / Docker Compose
- uv
- Ruff
- pytest
- GitHub Actions
- GitHub Container Registry

---

## Project structure

```
flight-delay-mlops/
├── src/
│   └── flight_delay_mlops/
│       ├── main.py
│       ├── database.py
│       └── __init__.py
├── tests/
│   ├── test_api.py
│   └── test_database.py
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── uv.lock
└── .github/
    └── workflows/
        ├── ci.yml
        └── cd.yml
```

---

## Local development

### Install dependencies

```bash
uv sync
```

### Run PostgreSQL

```bash
docker compose up -d db
```

### Run application

```bash
uv run uvicorn flight_delay_mlops.main:app --reload
```

Application will be available at:

```
http://127.0.0.1:8000
```

Swagger documentation:

```
http://127.0.0.1:8000/docs
```

---

## Docker Compose

Full application startup:

```bash
docker compose up -d --build
```

Services:

| Service | Description | Port |
|---|---|---|
| app | FastAPI application | 8000 |
| db | PostgreSQL database | 5432 |

PostgreSQL uses a persistent Docker volume.

The database healthcheck is used to ensure that the application starts only after PostgreSQL becomes available.

---

## API endpoints

### Healthcheck

```
GET /healthz
```

Simple application availability check.

Example response:

```json
{
  "status": "ok"
}
```

---

### Application version

```
GET /api/v1/version
```

Returns the current application version.

Example response:

```json
{
  "version": "0.1.0"
}
```

---

### Full health check

```
GET /api/v1/health
```

Checks third-party components and returns response time.

Example response:

```json
{
  "status": "ok",
  "database": "PostgreSQL version",
  "response_time_ms": 20.15
}
```

---

## Testing

Run tests:

```bash
uv run pytest
```

Run tests with coverage:

```bash
uv run pytest --cov=flight_delay_mlops --cov-report=term-missing
```

Current coverage:

```
98%
```

---

## Code quality

The project uses Ruff for linting and formatting.

Run checks:

```bash
uv run ruff check .
```

```bash
uv run ruff format --check .
```

Pre-commit hooks:

```bash
uv run pre-commit install
```

Run manually:

```bash
uv run pre-commit run --all-files
```

Checks include:

- Ruff lint
- Ruff format
- trailing whitespace
- end-of-file formatting
- YAML validation
- large file detection

---

## CI/CD

### CI

GitHub Actions runs automatically on push to `main`.

Pipeline steps:

1. Install dependencies
2. Run Ruff lint
3. Check formatting
4. Run tests with coverage

---

### CD

CD starts only after successful CI completion.

Pipeline:

```
Push to main
      |
      v
      CI
      |
      v
Docker build
      |
      v
Push image to GHCR
```

Docker images are published to:

```
ghcr.io/s3polina/flight-delay-mlops
```

Image tags:

- `latest` — latest successful build from main
- `<commit_sha>` — exact version linked to source commit

---

## Logging

Application logs contain:

- HTTP method
- request path
- response status
- response time

Example:

```
INFO request method=GET path=/api/v1/health status_code=200 response_time_ms=35.24
```
