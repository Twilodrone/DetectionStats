# DetectionStats Web UI

Веб-интерфейс для локального компьютера с Redis-видеоналитикой:
- показывает снимки `Cam1..Cam4`;
- отображает текущие значения ключей Redis;
- отслеживает срабатывания `Signal`;
- ведёт статистику и список последних срабатываний в SQLite.

## Требования

- Python 3.10+
- Redis доступен на `127.0.0.1:6379`

## Запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

После запуска откройте: `http://127.0.0.1:8080`.

## Конфигурация через переменные окружения

- `REDIS_HOST` (по умолчанию `127.0.0.1`)
- `REDIS_PORT` (по умолчанию `6379`)
- `REDIS_DB` (по умолчанию `0`)
- `POLL_INTERVAL_SECONDS` (по умолчанию `2`)
- `DB_PATH` (по умолчанию `./signal_events.db`)
- `CAMERA_PICTURES_DIR` (по умолчанию `/home/sdp/Detector/pictures`)
- `WEB_HOST` (по умолчанию `0.0.0.0`)
- `WEB_PORT` (по умолчанию `8080`)

## Логика срабатываний

Срабатывание считается при переходе `Signal: 0 -> 1`.
В момент срабатывания сохраняется снимок значений всех счётчиков:
- `Cam1_count..Cam4_count`
- `Cam1_wheelchair_cnt..Cam4_wheelchair_cnt`

## API

- `GET /api/status` — текущие значения + агрегированная статистика.
- `GET /api/events?limit=50` — последние события.
- `GET /api/camera/<cam_name>.jpg` — картинка камеры (`Cam1..Cam4`).
