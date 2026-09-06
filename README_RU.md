# API & Webhook Reliability Monitor

[English](README.md) | **Русский**

Production-style приложение на FastAPI для приёма, проверки, пересылки, повторной отправки и анализа webhook-событий.

## Зачем нужен проект

Webhook-интеграции часто ломаются из-за некорректного JSON, временно недоступного API, ошибок авторизации, просроченных секретов или ответов не из диапазона 2xx. Проект даёт единый центр, где можно увидеть событие, понять причину ошибки и повторить доставку без повторного запроса к исходному сервису.

## Возможности

- создание собственных webhook URL;
- сохранение method, headers, query-параметров, content type, raw body и JSON;
- проверка JSON и опционального `X-Webhook-Secret`;
- автоматическая пересылка событий на внешний API;
- логирование статуса ответа, тела ответа, времени и числа попыток;
- retry с exponential backoff;
- ручной replay любого события;
- pause/resume endpoint'ов;
- ротация webhook secrets;
- встроенный REST API tester;
- история событий и JSON API;
- экспорт в CSV и Excel;
- SQLite persistence;
- Swagger/OpenAPI;
- Docker / Docker Compose;
- автоматические тесты.

## Стек

Python 3.12+, FastAPI, SQLAlchemy 2, HTTPX, Jinja2, SQLite, Pandas, OpenPyXL, Uvicorn, Pytest.

## Быстрый запуск

```bash
python -m venv .venv
```

Windows:

```bat
.venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

macOS / Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Открыть:

- Dashboard: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- Health: `http://localhost:8000/health`

Для готовой демонстрации:

```bash
python seed_demo.py
python run.py
```

## Retry-логика

Неудачная доставка автоматически повторяется, если endpoint разрешает авто-пересылку, событие прошло валидацию, внешний сервис вернул ошибку/недоступен и лимит попыток ещё не исчерпан. Все попытки сохраняются для аудита.

## Что демонстрирует проект

Python backend, FastAPI, REST API, webhooks, надёжные интеграции, retries, observability, SQLAlchemy, экспорт данных, Docker и тестирование.
