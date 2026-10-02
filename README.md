# Flight Delay MLOps

Учебный MLOps-проект с асинхронным FastAPI-приложением и PostgreSQL.

Проект демонстрирует полный цикл разработки и поставки приложения:

```text
Разработка
    ↓
Git
    ↓
pre-commit
    ↓
CI
    ↓
Docker
    ↓
CD
    ↓
GitHub Container Registry
```

## 1. Что представляет собой проект

Приложение предоставляет HTTP API для проверки состояния сервиса и его зависимостей.

Основные компоненты:

- **FastAPI** — веб-фреймворк для создания API;
- **PostgreSQL** — база данных;
- **asyncpg** — асинхронный драйвер PostgreSQL;
- **uv** — управление Python-зависимостями и окружением;
- **Docker** — контейнеризация приложения;
- **Docker Compose** — запуск приложения вместе с PostgreSQL;
- **pytest** — тестирование;
- **Ruff** — линтинг и форматирование;
- **pre-commit** — автоматические проверки перед commit;
- **GitHub Actions** — CI/CD;
- **GitHub Container Registry (GHCR)** — хранение Docker-образов.

Приложение является асинхронным: API реализован через `async def`, а операции с PostgreSQL выполняются через асинхронный драйвер `asyncpg`.

---

# 2. Структура проекта

```text
flight-delay-mlops/
│
├── src/
│   └── flight_delay_mlops/
│       ├── __init__.py
│       ├── main.py
│       └── database.py
│
├── tests/
│   ├── test_api.py
│   └── test_database.py
│
├── .github/
│   └── workflows/
│       ├── ci.yml
│       └── cd.yml
│
├── .pre-commit-config.yaml
├── .python-version
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── uv.lock
└── README.md
```

## Назначение основных файлов

### `src/flight_delay_mlops/main.py`

Основной модуль FastAPI-приложения.

Здесь находятся:

- создание объекта `FastAPI`;
- HTTP endpoints;
- проверка состояния приложения;
- получение версии приложения;
- end-to-end health-check;
- обработка ошибок при недоступности PostgreSQL;
- логирование HTTP-запросов.

### `src/flight_delay_mlops/database.py`

Модуль для работы с PostgreSQL.

В нём находится функция `check_database()`, которая:

1. подключается к PostgreSQL через `asyncpg`;
2. выполняет запрос `SELECT version()`;
3. возвращает версию PostgreSQL;
4. закрывает соединение после завершения операции.

Адрес базы данных берётся из переменной окружения `DATABASE_URL`.

Если переменная окружения не задана, используется локальный адрес PostgreSQL.

### `tests/test_api.py`

Тесты HTTP API.

Проверяются:

- `/healthz`;
- `/api/v1/version`;
- успешный `/api/v1/health`;
- сценарий недоступной базы данных.

### `tests/test_database.py`

Тесты функции работы с PostgreSQL.

Проверяются:

- успешное получение версии PostgreSQL;
- обработка ошибки базы данных;
- закрытие соединения.

### `Dockerfile`

Описывает сборку Docker-образа приложения.

В контейнер устанавливаются runtime-зависимости и исходный код приложения.

Приложение запускается от отдельного пользователя `appuser`, а не от `root`.

### `docker-compose.yml`

Описывает локальную инфраструктуру проекта.

Запускаются два сервиса:

```text
app
 ↓
FastAPI

db
 ↓
PostgreSQL
```

Compose также настраивает:

- сеть между контейнерами;
- PostgreSQL volume;
- healthcheck базы данных;
- порты;
- ограничения CPU и памяти;
- ограничения Docker logs;
- зависимость приложения от готовности базы данных.

### `pyproject.toml`

Основной файл конфигурации Python-проекта.

В нём находятся:

- метаданные проекта;
- версия приложения;
- зависимости;
- группы зависимостей;
- настройки Ruff;
- настройки pytest.

### `uv.lock`

Lock-файл с зафиксированными версиями зависимостей.

Он используется для воспроизводимой установки окружения.

### `.pre-commit-config.yaml`

Конфигурация автоматических проверок перед commit.

### `.github/workflows/ci.yml`

GitHub Actions workflow для Continuous Integration.

### `.github/workflows/cd.yml`

GitHub Actions workflow для Continuous Delivery.

---

# 3. Установка зависимостей

Проект использует `uv`.

Для установки зависимостей:

```bash
uv sync
```

`uv.lock` используется для воспроизводимой установки зависимостей.

---

# 4. Запуск PostgreSQL

Для локальной разработки можно запустить только базу данных:

```bash
docker compose up -d db
```

Проверить состояние:

```bash
docker compose ps
```

PostgreSQL должен иметь состояние:

```text
healthy
```

Параметры базы данных:

```text
Database: flight_delay
User: postgres
Password: postgres
Port: 5432
```

---

# 5. Запуск приложения локально

После запуска PostgreSQL:

```bash
uv run uvicorn flight_delay_mlops.main:app --reload
```

Приложение будет доступно по адресу:

```text
http://127.0.0.1:8000
```

Swagger UI:

```text
http://127.0.0.1:8000/docs
```

---

# 6. Запуск всего проекта через Docker Compose

Для запуска приложения и PostgreSQL вместе:

```bash
docker compose up -d --build
```

Проверить контейнеры:

```bash
docker compose ps
```

Ожидаемая структура:

```text
app   → FastAPI → port 8000
db    → PostgreSQL → port 5432
```

Посмотреть логи приложения:

```bash
docker compose logs app --tail=20
```

Посмотреть логи PostgreSQL:

```bash
docker compose logs db --tail=20
```

Остановить проект:

```bash
docker compose down
```

---

# 7. Docker Compose и PostgreSQL

Приложение и PostgreSQL запускаются в разных контейнерах.

```text
┌──────────────────────┐
│        app           │
│      FastAPI         │
│       :8000          │
└──────────┬───────────┘
           │
           │ Docker network
           │
┌──────────▼───────────┐
│         db           │
│     PostgreSQL       │
│       :5432          │
└──────────────────────┘
```

Внутри Docker Compose приложение обращается к PostgreSQL по имени сервиса:

```text
db
```

а не через `localhost`.

Переменная окружения:

```text
DATABASE_URL=postgresql://postgres:postgres@db:5432/flight_delay
```

---

# 8. Healthcheck PostgreSQL

PostgreSQL имеет Docker healthcheck:

```text
pg_isready -U postgres -d flight_delay
```

Он проверяет, готова ли база принимать подключения.

Приложение зависит от готовности базы:

```yaml
depends_on:
  db:
    condition: service_healthy
```

Поэтому Compose сначала ждёт, пока PostgreSQL станет `healthy`, и только после этого запускает приложение.

---

# 9. API

В приложении реализовано три основных endpoint'а.

## `GET /healthz`

Быстрая проверка того, что само приложение отвечает.

Ответ:

```json
{
  "status": "ok"
}
```

Endpoint находится вне `/api/v1`, потому что это отдельный healthcheck для проверки доступности самого приложения, а не версионируемый бизнес/API endpoint.

---

## `GET /api/v1/version`

Возвращает версию приложения.

Пример:

```json
{
  "version": "0.1.1"
}
```

Версия берётся из метаданных установленного Python-пакета через:

```python
importlib.metadata.version()
```

Версия проекта хранится в `pyproject.toml`.

Чтобы изменить версию:

1. изменить `version` в `pyproject.toml`;
2. выполнить:

```bash
uv lock
```

3. запустить тесты и проверки;
4. создать commit;
5. выполнить `git push`.

---

## `GET /api/v1/health`

End-to-end health-check.

Endpoint проверяет доступность PostgreSQL и получает версию базы данных.

Пример успешного ответа:

```json
{
  "status": "ok",
  "database": "PostgreSQL 17...",
  "response_time_ms": 35.24
}
```

`response_time_ms` показывает время выполнения проверки.

Если PostgreSQL недоступен, endpoint возвращает:

```text
HTTP 503 Service Unavailable
```

Например:

```json
{
  "status": "error",
  "database": "unavailable",
  "response_time_ms": 10.42
}
```

Это позволяет отличить работающий FastAPI-сервис от ситуации, когда его зависимость PostgreSQL недоступна.

---

# 10. Асинхронность

Приложение использует асинхронные endpoint'ы:

```python
async def
```

и `await`.

Для PostgreSQL используется асинхронный драйвер:

```text
asyncpg
```

Цепочка работы:

```text
FastAPI
   ↓
async def
   ↓
await
   ↓
asyncpg
   ↓
PostgreSQL
```

Асинхронность позволяет не блокировать event loop во время операций ввода-вывода.

---

# 11. Обработка ошибок

При обращении к PostgreSQL могут возникать ошибки.

В `/api/v1/health` обрабатываются ошибки:

```python
asyncpg.PostgresError
OSError
```

Если база недоступна, приложение не отдаёт необработанный traceback клиенту.

Вместо этого возвращается:

```text
503 Service Unavailable
```

и понятный JSON-ответ.

---

# 12. Тестирование

Запустить тесты:

```bash
uv run pytest
```

Запустить тесты с coverage:

```bash
uv run pytest --cov=flight_delay_mlops --cov-report=term-missing
```

Текущий результат:

```text
6 passed
98% coverage
```

Покрываются:

- API endpoints;
- успешный health-check;
- ошибочный health-check;
- работа с PostgreSQL;
- обработка ошибок базы;
- закрытие соединения.

---

# 13. Ruff

Для проверки качества Python-кода используется Ruff.

Проверка линтера:

```bash
uv run ruff check .
```

Проверка форматирования:

```bash
uv run ruff format --check .
```

Ruff используется для:

- поиска проблем в коде;
- проверки стиля;
- форматирования Python-файлов.

---

# 14. Pre-commit

Перед commit автоматически запускаются проверки из `.pre-commit-config.yaml`.

В проекте используются проверки:

```text
trim trailing whitespace
end-of-file-fixer
check added large files
check YAML
check TOML
Ruff check
Ruff format
```

Установка hooks:

```bash
uv run pre-commit install
```

Запуск всех проверок вручную:

```bash
uv run pre-commit run --all-files
```

Если hook изменил файл автоматически, изменения нужно добавить в Git и повторить commit.

---

# 15. Git

Основная ветка проекта:

```text
main
```

Обычный workflow:

```text
изменение кода
      ↓
git status
      ↓
git add
      ↓
git commit
      ↓
pre-commit
      ↓
git push origin main
```

Пример:

```bash
git status
git add .
git commit -m "feat: update healthcheck"
git push origin main
```

---

# 16. CI

CI означает Continuous Integration.

В проекте CI реализован через GitHub Actions.

После push в `main` запускается workflow:

```text
git push
    ↓
GitHub Actions
    ↓
uv sync
    ↓
Ruff check
    ↓
Ruff format check
    ↓
pytest
    ↓
coverage
```

CI нужен для автоматической проверки нового кода.

Если проверки не проходят, CI становится красным.

Если все проверки проходят, workflow завершается успешно.

---

# 17. CD

CD означает Continuous Delivery.

В нашем проекте CD запускается после успешного завершения CI.

Общий процесс:

```text
push в main
      ↓
     CI
      ↓
  CI success
      ↓
     CD
      ↓
 Docker build
      ↓
 Docker image
      ↓
     GHCR
```

Если CI завершился с ошибкой, CD не публикует новый Docker-образ.

---

# 18. GitHub Container Registry

Docker-образы проекта публикуются в GitHub Container Registry:

```text
ghcr.io/s3polina/flight-delay-mlops
```

Используется два типа тегов:

```text
latest
```

и

```text
<commit_sha>
```

### `latest`

Указывает на последнюю опубликованную версию из `main`.

### Commit SHA

Позволяет точно определить, из какого commit был собран конкретный Docker-образ.

Например:

```text
ghcr.io/s3polina/flight-delay-mlops:latest

ghcr.io/s3polina/flight-delay-mlops:f8c1acf...
```

SHA-тег позволяет связать Docker-образ с конкретным состоянием исходного кода.

---

# 19. Logging

Приложение логирует HTTP-запросы.

В логах сохраняются:

- HTTP method;
- request path;
- HTTP status code;
- response time.

Пример:

```text
INFO request method=GET path=/api/v1/health status_code=200 response_time_ms=35.24
```

Это позволяет понимать, какие запросы приходят в приложение и сколько времени они выполняются.

---

# 20. Docker image

Для приложения используется Docker image на базе:

```text
python:3.12-slim
```

В образ устанавливаются runtime-зависимости приложения.

Для установки зависимостей используется `uv`.

Приложение запускается от отдельного пользователя:

```text
appuser
```

а не от `root`.

Это уменьшает права процесса внутри контейнера.

---

# 21. Docker resources and logging

В Docker Compose для сервисов заданы ограничения ресурсов:

```text
CPU: 1
Memory: 512 MB
```

Также настроено ограничение Docker logs:

```text
max-size: 10m
max-file: 3
```

Это предотвращает бесконтрольный рост логов контейнеров.

---

# 22. PostgreSQL volume

PostgreSQL использует Docker volume:

```text
postgres_data
```

Volume монтируется в:

```text
/var/lib/postgresql/data
```

Это позволяет сохранять данные PostgreSQL независимо от жизненного цикла контейнера.

---

# 23. Полный workflow проекта

Итоговый процесс выглядит так:

```text
                    Developer
                        │
                        ▼
                  Code changes
                        │
                        ▼
                     Git
                        │
                        ▼
                  pre-commit
                        │
                        ▼
                  git push main
                        │
                        ▼
                 ┌─────────────┐
                 │     CI      │
                 │             │
                 │ Ruff        │
                 │ Format      │
                 │ Tests       │
                 │ Coverage    │
                 └──────┬──────┘
                        │
                    success
                        │
                        ▼
                 ┌─────────────┐
                 │     CD      │
                 │             │
                 │ Docker build│
                 │     ↓       │
                 │    GHCR     │
                 └─────────────┘
```

---

# 24. Полный запуск с нуля

Если нужно полностью запустить проект:

```bash
git clone https://github.com/s3polina/flight-delay-mlops.git
cd flight-delay-mlops
```

Установить зависимости:

```bash
uv sync
```

Запустить всё:

```bash
docker compose up -d --build
```

Проверить состояние:

```bash
docker compose ps
```

Проверить API:

```text
http://127.0.0.1:8000/docs
```

Проверить health:

```text
http://127.0.0.1:8000/healthz
```

Проверить версию:

```text
http://127.0.0.1:8000/api/v1/version
```

Проверить PostgreSQL:

```text
http://127.0.0.1:8000/api/v1/health
```

---

# 25. Остановка проекта

Остановить контейнеры:

```bash
docker compose down
```

Если нужно удалить также volume PostgreSQL:

```bash
docker compose down -v
```

Команда `down -v` удаляет сохранённые данные PostgreSQL, поэтому использовать её нужно только когда данные больше не нужны.

---

# 26. Основная идея проекта

Проект демонстрирует не только создание FastAPI-приложения, но и весь базовый процесс его подготовки к поставке:

```text
Код
 ↓
Проверка качества
 ↓
Тестирование
 ↓
Контейнеризация
 ↓
CI
 ↓
CD
 ↓
Docker Registry
```

Главная цель — сделать процесс воспроизводимым и автоматизировать проверки и публикацию приложения.
