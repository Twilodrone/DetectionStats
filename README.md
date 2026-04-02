# DetectionStats Web UI

Веб-интерфейс для локального компьютера с Redis-видеоналитикой:
- на главной странице показывает последнее срабатывание с архивными снимками `Cam1..Cam4` и статистикой;
- отслеживает срабатывания `Signal`;
- ведёт статистику и список срабатываний в SQLite;
- вынес обзор архива срабатываний на отдельную страницу-вкладку;
- сохраняет архив изображений в момент срабатывания (`Signal: 0 -> 1`) по всем камерам;
- позволяет открыть отчет по каждому срабатыванию кликом по строке.
- добавлена вкладка `Live RTSP` с потоковым видео (MJPEG proxy через `ffmpeg`) и разворачиваемыми секциями для камер.

## Требования

- Python 3.10+
- Redis доступен на `127.0.0.1:6379`
- `ffmpeg` в PATH (для трансляции RTSP в браузерный MJPEG-поток)

## Запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

После запуска откройте: `http://127.0.0.1:8080`.

Для live-видео:
1. Скопируйте `camera_streams.json.example` в `camera_streams.json`.
2. Заполните RTSP URL для камер `Cam1..Cam4`.

## Конфигурация через переменные окружения

- `REDIS_HOST` (по умолчанию `127.0.0.1`)
- `REDIS_PORT` (по умолчанию `6379`)
- `REDIS_DB` (по умолчанию `0`)
- `POLL_INTERVAL_SECONDS` (по умолчанию `2`)
- `DB_PATH` (по умолчанию `./signal_events.db`)
- `CAMERA_PICTURES_DIR` (по умолчанию `/home/sdp/Detector/pictures`)
- `EVENT_ARCHIVE_DIR` (по умолчанию `./event_archive`)
- `WEB_HOST` (по умолчанию `0.0.0.0`)
- `WEB_PORT` (по умолчанию `8080`)
- `RTSP_CONFIG_PATH` (по умолчанию `./camera_streams.json`)

## Логика срабатываний

Срабатывание считается при переходе `Signal: 0 -> 1`.
В момент срабатывания:
- сохраняется снимок значений всех счётчиков:
  - `Cam1_count..Cam4_count`
  - `Cam1_wheelchair_cnt..Cam4_wheelchair_cnt`
- создается архив изображений камер в `EVENT_ARCHIVE_DIR/event_<id>/CamX.jpg`

## API

- `GET /api/status` — текущие значения + агрегированная статистика.
- `GET /api/events?limit=50` — последние события.
- `GET /api/events/<id>` — детальная информация по конкретному срабатыванию.
- `GET /api/camera/<cam_name>.jpg` — актуальная картинка камеры (`Cam1..Cam4`).
- `GET /api/events/<id>/camera/<cam_name>.jpg` — архивная картинка камеры для срабатывания.
- `GET /api/live/<cam_name>/mjpeg` — live-видео камеры в формате MJPEG (источник RTSP берется из `camera_streams.json`).
- `GET /events/<id>` — HTML-отчет по срабатыванию с архивными изображениями.
