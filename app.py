import json
import logging
import os
from queue import Empty, Queue
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import redis
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, session, url_for


CONFIG_PATH = Path(os.getenv("APP_CONFIG_PATH", "config.yaml"))


def parse_yaml_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"''", '""'}:
        return ""
    if (value.startswith('\"') and value.endswith('\"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def parse_simple_yaml_mapping(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any] | list[Any]]] = [(-1, root)]
    pending_key: tuple[int, dict[str, Any], str] | None = None

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1]
        if line.startswith("- "):
            if not isinstance(parent, list):
                if pending_key is None or indent <= pending_key[0]:
                    raise ValueError(f"Unexpected list item at config line {line_number}")
                _, pending_parent, key = pending_key
                parent = []
                pending_parent[key] = parent
                stack.append((indent - 1, parent))
                pending_key = None
            parent.append(parse_yaml_scalar(line[2:]))
            continue

        if not isinstance(parent, dict):
            raise ValueError(f"Unexpected mapping item at config line {line_number}")
        if ":" not in line:
            raise ValueError(f"Expected key/value pair at config line {line_number}")

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Empty key at config line {line_number}")
        if value:
            parent[key] = parse_yaml_scalar(value)
            pending_key = None
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
            pending_key = (indent, parent, key)

    return root


def load_yaml_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"Application config not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as fh:
        raw = parse_simple_yaml_mapping(fh.read())

    if not isinstance(raw, dict):
        raise ValueError(f"Application config must be a YAML mapping: {config_path}")

    return raw


def config_section(config: dict[str, Any], section_name: str) -> dict[str, Any]:
    section = config.get(section_name, {})
    if not isinstance(section, dict):
        raise ValueError(f"Config section '{section_name}' must be a mapping")
    return section


def string_config(section: dict[str, Any], key: str) -> str:
    value = section.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Config value '{key}' must be a string")
    return value


def int_config(section: dict[str, Any], key: str) -> int:
    try:
        return int(section[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Config value '{key}' must be an integer") from exc


def float_config(section: dict[str, Any], key: str) -> float:
    try:
        return float(section[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Config value '{key}' must be a number") from exc


def path_config(section: dict[str, Any], key: str) -> Path:
    return Path(string_config(section, key))


def string_list_config(section: dict[str, Any], key: str) -> list[str]:
    value = section.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Config value '{key}' must be a list of strings")
    return value


def string_dict_config(section: dict[str, Any], key: str) -> dict[str, str]:
    value = section.get(key)
    if not isinstance(value, dict) or not all(
        isinstance(item_key, str) and isinstance(item_value, str)
        for item_key, item_value in value.items()
    ):
        raise ValueError(f"Config value '{key}' must be a mapping of strings")
    return dict(value)


APP_CONFIG = load_yaml_config(CONFIG_PATH)
REDIS_CONFIG = config_section(APP_CONFIG, "redis")
PATHS_CONFIG = config_section(APP_CONFIG, "paths")
WEB_CONFIG = config_section(APP_CONFIG, "web")
RTSP_CONFIG = config_section(APP_CONFIG, "rtsp")
CAMERAS_CONFIG = config_section(APP_CONFIG, "cameras")
DETECTION_CONFIG = config_section(APP_CONFIG, "detection")
THRESHOLDS_CONFIG = config_section(DETECTION_CONFIG, "threshold_defaults")

REDIS_HOST = string_config(REDIS_CONFIG, "host")
REDIS_PORT = int_config(REDIS_CONFIG, "port")
REDIS_DB = int_config(REDIS_CONFIG, "db")
POLL_INTERVAL_SECONDS = float_config(REDIS_CONFIG, "poll_interval_seconds")
DB_PATH = string_config(PATHS_CONFIG, "db_path")
PEDESTRIAN_PICTURES_DIR = path_config(PATHS_CONFIG, "pedestrian_pictures_dir")
WHEELCHAIR_PICTURES_DIR = path_config(PATHS_CONFIG, "wheelchair_pictures_dir")
CHILD_PICTURES_DIR = path_config(PATHS_CONFIG, "child_pictures_dir")
ERROR_IMAGE_PATH = path_config(PATHS_CONFIG, "error_image_path")
DETECTOR_CONFIG_PATH = path_config(PATHS_CONFIG, "detector_config_path")
EVENT_ARCHIVE_DIR = path_config(PATHS_CONFIG, "event_archive_dir")
WEB_HOST = string_config(WEB_CONFIG, "host")
WEB_PORT = int_config(WEB_CONFIG, "port")
ARCHIVE_PASSWORD = string_config(WEB_CONFIG, "archive_password")
SESSION_SECRET_KEY = string_config(WEB_CONFIG, "session_secret_key")
RTSP_CONFIG_PATH = path_config(RTSP_CONFIG, "config_path")
RTSP_SNAPSHOT_TIMEOUT_SECONDS = float_config(RTSP_CONFIG, "snapshot_timeout_seconds")
CAM_KEYS = string_list_config(CAMERAS_CONFIG, "keys")
CAM_IMAGE_FILENAMES = string_dict_config(CAMERAS_CONFIG, "image_filenames")
REDIS_KEYS = string_list_config(DETECTION_CONFIG, "redis_keys")
PHASE_MIN_THRESHOLD_DEFAULT = int_config(THRESHOLDS_CONFIG, "min")
PHASE_MAX_THRESHOLD_DEFAULT = int_config(THRESHOLDS_CONFIG, "max")

IMAGE_SOURCES = {
    "pedestrian": PEDESTRIAN_PICTURES_DIR,
    "wheelchair": WHEELCHAIR_PICTURES_DIR,
    "child": CHILD_PICTURES_DIR,
}

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def send_image_with_fallback(image_path: Path):
    if image_path.exists():
        return send_file(image_path, mimetype="image/jpeg", max_age=0)

    if ERROR_IMAGE_PATH.exists():
        logging.warning("Image not found, returning fallback image: %s", image_path)
        return send_file(ERROR_IMAGE_PATH, mimetype="image/jpeg", max_age=0)

    return jsonify({"error": f"Image not found: {image_path}"}), 404


def load_rtsp_config(config_path: Path) -> dict[str, str]:
    if not config_path.exists():
        return {}

    with config_path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)

    if not isinstance(raw, dict):
        return {}

    streams: dict[str, str] = {}
    for key in CAM_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            streams[key] = value.strip()
    return streams


def load_detector_thresholds(config_path: Path) -> tuple[int, int]:
    default_min = PHASE_MIN_THRESHOLD_DEFAULT
    default_max = PHASE_MAX_THRESHOLD_DEFAULT
    if not config_path.exists():
        logging.warning(
            "Detector config not found at %s; using defaults min=%s max=%s",
            config_path,
            default_min,
            default_max,
        )
        return default_min, default_max

    try:
        with config_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        min_threshold = int(raw.get("min", default_min))
        max_threshold = int(raw.get("max", default_max))
        return min_threshold, max_threshold
    except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
        logging.warning(
            "Failed to read detector config %s (%s); using defaults min=%s max=%s",
            config_path,
            exc,
            default_min,
            default_max,
        )
        return default_min, default_max


class SignalRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    signal_value INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    detection_accurate INTEGER
                )
                """
            )
            columns = [row[1] for row in conn.execute("PRAGMA table_info(signal_events)").fetchall()]
            if "detection_accurate" not in columns:
                conn.execute("ALTER TABLE signal_events ADD COLUMN detection_accurate INTEGER")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_signal_events_ts
                ON signal_events(ts_utc DESC)
                """
            )
            conn.commit()

    def add_event(self, payload: dict[str, Any], signal_value: int) -> int:
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO signal_events(ts_utc, signal_value, payload_json) VALUES (?, ?, ?)",
                    (utc_now_iso(), signal_value, json.dumps(payload, ensure_ascii=False)),
                )
                conn.commit()
                return int(cur.lastrowid)

    def get_recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, ts_utc, signal_value, payload_json, detection_accurate
                FROM signal_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()

        events: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            events.append(
                {
                    "id": row["id"],
                    "ts_utc": row["ts_utc"],
                    "signal_value": row["signal_value"],
                    "payload": payload,
                    "detection_accurate": row["detection_accurate"],
                }
            )
        return events

    def get_event_by_id(self, event_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, ts_utc, signal_value, payload_json, detection_accurate
                FROM signal_events
                WHERE id = ?
                LIMIT 1
                """,
                (event_id,),
            ).fetchone()

        if row is None:
            return None

        return {
            "id": row["id"],
            "ts_utc": row["ts_utc"],
            "signal_value": row["signal_value"],
            "payload": json.loads(row["payload_json"]),
            "detection_accurate": row["detection_accurate"],
        }

    def set_detection_accurate(self, event_id: int, detection_accurate: bool) -> bool:
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE signal_events SET detection_accurate = ? WHERE id = ?",
                    (1 if detection_accurate else 0, event_id),
                )
                conn.commit()
                return cur.rowcount > 0

    def get_stats(self, trigger_source: str | None = None) -> dict[str, Any]:
        where_clause = ""
        params: tuple[Any, ...] = ()
        if trigger_source:
            where_clause = " WHERE payload_json LIKE ?"
            params = (f'%"trigger_source": "{trigger_source}"%',)

        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM signal_events{where_clause}",
                params,
            ).fetchone()[0]
            last = conn.execute(
                f"SELECT ts_utc FROM signal_events{where_clause} ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()

        return {
            "total_triggers": int(total),
            "last_trigger_utc": last["ts_utc"] if last else None,
        }


class RedisWatcher:
    def __init__(
        self,
        repo: SignalRepository,
        rtsp_streams: dict[str, str],
        min_threshold: int,
        max_threshold: int,
    ) -> None:
        self.repo = repo
        self.rtsp_streams = rtsp_streams
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self.redis_client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
            decode_responses=True,
            socket_timeout=2,
        )
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._archive_thread = threading.Thread(target=self._archive_worker, daemon=True)
        self._archive_queue: Queue[tuple[int, str, str]] = Queue()
        self._last_snapshot: dict[str, int] = {key: 0 for key in REDIS_KEYS}
        self._last_redis_ok: bool = False
        self._previous_people_overflow_by_cam: dict[str, bool] = {cam: False for cam in CAM_KEYS}
        self._previous_wheelchair_present_by_cam: dict[str, bool] = {
            cam: False for cam in CAM_KEYS
        }
        self._previous_child_present_by_cam: dict[str, bool] = {cam: False for cam in CAM_KEYS}

    def start(self) -> None:
        self._thread.start()
        self._archive_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2)
        self._archive_thread.join(timeout=2)

    def get_snapshot(self) -> dict[str, Any]:
        return {
            "redis_ok": self._last_redis_ok,
            "values": self._last_snapshot,
        }

    def _fetch_values(self) -> dict[str, int]:
        raw = self.redis_client.mget(REDIS_KEYS)
        values: dict[str, int] = {}
        for key, value in zip(REDIS_KEYS, raw):
            try:
                values[key] = int(value) if value is not None else 0
            except ValueError:
                values[key] = 0
        return values

    def _capture_rtsp_frame(self, rtsp_url: str, dst_path: Path) -> bool:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg",
            "-y",
            "-rtsp_transport",
            "tcp",
            "-i",
            rtsp_url,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(dst_path),
        ]
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=RTSP_SNAPSHOT_TIMEOUT_SECONDS,
                check=True,
            )
            return dst_path.exists()
        except subprocess.TimeoutExpired:
            logging.warning("Timeout while capturing RTSP frame: %s", rtsp_url)
        except subprocess.CalledProcessError:
            logging.warning("ffmpeg failed while capturing RTSP frame: %s", rtsp_url)

        if dst_path.exists():
            dst_path.unlink(missing_ok=True)
        return False

    def _archive_event_image(self, event_id: int, source_name: str, cam_name: str) -> None:
        event_dir = EVENT_ARCHIVE_DIR / f"event_{event_id}" / source_name
        event_dir.mkdir(parents=True, exist_ok=True)
        src = IMAGE_SOURCES[source_name] / CAM_IMAGE_FILENAMES.get(cam_name, f"{cam_name}.jpg")
        dst = event_dir / CAM_IMAGE_FILENAMES.get(cam_name, f"{cam_name}.jpg")
        if src.exists():
            shutil.copy2(src, dst)
        else:
            logging.warning(
                "Image for %s source and %s camera not found: %s",
                source_name,
                cam_name,
                src,
            )

    def _archive_worker(self) -> None:
        while not self._stop_event.is_set() or not self._archive_queue.empty():
            try:
                event_id, source_name, cam_name = self._archive_queue.get(timeout=0.5)
            except Empty:
                continue
            try:
                self._archive_event_image(event_id, source_name, cam_name)
            finally:
                self._archive_queue.task_done()

    def _create_single_camera_event(
        self, values: dict[str, int], cam_name: str, source_name: str
    ) -> None:
        people_count = values.get(f"{cam_name}_count", 0)
        wheelchair_count = values.get(f"{cam_name}_wheelchair_cnt", 0)
        child_count = values.get(f"{cam_name}_child_count", 0)
        event_payload = {
            "min_threshold": self.min_threshold,
            "max_threshold": self.max_threshold,
            "trigger_source": source_name,
            "trigger_cam": cam_name,
            "Signal": values.get("Signal", 0),
            f"{cam_name}_count": people_count,
            f"{cam_name}_wheelchair_cnt": wheelchair_count,
            f"{cam_name}_child_count": child_count,
            "trigger_value": (
                people_count
                if source_name == "pedestrian"
                else wheelchair_count if source_name == "wheelchair" else child_count
            ),
        }
        event_id = self.repo.add_event(
            payload=event_payload,
            signal_value=values.get("Signal", 0),
        )
        self._archive_queue.put((event_id, source_name, cam_name))

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                values = self._fetch_values()
                self._last_snapshot = values
                self._last_redis_ok = True

                for cam_name in CAM_KEYS:
                    people_count = values.get(f"{cam_name}_count", 0)
                    wheelchair_count = values.get(f"{cam_name}_wheelchair_cnt", 0)
                    child_count = values.get(f"{cam_name}_child_count", 0)
                    people_overflow = people_count > self.max_threshold
                    wheelchair_present = wheelchair_count > 0
                    child_present = child_count > 0

                    if people_overflow and not self._previous_people_overflow_by_cam[cam_name]:
                        self._create_single_camera_event(values, cam_name, "pedestrian")

                    if (
                        wheelchair_present
                        and not self._previous_wheelchair_present_by_cam[cam_name]
                    ):
                        self._create_single_camera_event(values, cam_name, "wheelchair")
                    if child_present and not self._previous_child_present_by_cam[cam_name]:
                        self._create_single_camera_event(values, cam_name, "child")

                    self._previous_people_overflow_by_cam[cam_name] = people_overflow
                    self._previous_wheelchair_present_by_cam[cam_name] = wheelchair_present
                    self._previous_child_present_by_cam[cam_name] = child_present
            except redis.RedisError:
                self._last_redis_ok = False
            time.sleep(POLL_INTERVAL_SECONDS)


app = Flask(__name__)
app.secret_key = SESSION_SECRET_KEY
RTSP_STREAMS = load_rtsp_config(RTSP_CONFIG_PATH)
MIN_THRESHOLD, MAX_THRESHOLD = load_detector_thresholds(DETECTOR_CONFIG_PATH)
repo = SignalRepository(DB_PATH)
watcher = RedisWatcher(repo, RTSP_STREAMS, MIN_THRESHOLD, MAX_THRESHOLD)
watcher.start()

def is_archive_authenticated() -> bool:
    return bool(session.get("archive_authenticated", False))


def event_is_pedestrian(event: dict[str, Any]) -> bool:
    payload = event.get("payload", {})
    return payload.get("trigger_source") == "pedestrian"


def require_archive_auth_for_event(event: dict[str, Any]) -> bool:
    return is_archive_authenticated() or event_is_pedestrian(event)


@app.route("/")
def index() -> str:
    return render_template("index.html", cams=CAM_KEYS, max=MAX_THRESHOLD)


@app.route("/archive")
def archive() -> str:
    return render_template("archive.html", archive_authenticated=is_archive_authenticated())


@app.route("/api/archive-auth", methods=["POST"])
def api_archive_auth() -> Response:
    body = request.get_json(silent=True) or {}
    password = body.get("password")
    if not isinstance(password, str):
        return jsonify({"error": "Field password must be string"}), 400

    if not ARCHIVE_PASSWORD or password != ARCHIVE_PASSWORD:
        session["archive_authenticated"] = False
        return jsonify({"ok": False}), 401

    session["archive_authenticated"] = True
    return jsonify({"ok": True})


@app.route("/api/archive-auth/logout", methods=["POST"])
def api_archive_logout() -> Response:
    session["archive_authenticated"] = False
    return jsonify({"ok": True})


@app.route("/api/archive-auth/status")
def api_archive_auth_status() -> Response:
    return jsonify({"authenticated": is_archive_authenticated()})


@app.route("/live")
def live() -> str:
    return render_template("live.html", cams=CAM_KEYS, streams=RTSP_STREAMS)


@app.route("/events/<int:event_id>")
def event_report(event_id: int):
    event = repo.get_event_by_id(event_id)
    if event is None:
        return jsonify({"error": "Event not found"}), 404
    if not require_archive_auth_for_event(event):
        return jsonify({"error": "Forbidden"}), 403

    return render_template("event_report.html", event=event, cams=CAM_KEYS)


@app.route("/cgi-bin/luci")
@app.route("/cgi-bin/luci/")
def luci_compat_redirect() -> Response:
    return redirect(url_for("index"), code=302)


@app.route("/api/status")
def api_status() -> Response:
    snapshot = watcher.get_snapshot()
    stats = repo.get_stats(trigger_source="pedestrian")
    return jsonify(
        {
            "timestamp_utc": utc_now_iso(),
            "thresholds": {"min": MIN_THRESHOLD, "max": MAX_THRESHOLD},
            "redis": snapshot,
            "stats": stats,
        }
    )


@app.route("/api/events")
def api_events() -> Response:
    limit = request.args.get("limit", default=50, type=int)
    wheelchair_gt_zero = request.args.get("wheelchair_gt_zero", default=0, type=int) == 1
    any_cam_count_gt = request.args.get("any_cam_count_gt", type=int)
    child_gt_zero = request.args.get("child_gt_zero", default=0, type=int) == 1
    trigger_source = request.args.get("trigger_source", type=str)
    if not is_archive_authenticated():
        trigger_source = "pedestrian"
    detection_accurate = request.args.get("detection_accurate", type=int)

    safe_limit = max(1, min(limit, 500))
    has_filters = any(
        [
            wheelchair_gt_zero,
            any_cam_count_gt is not None,
            child_gt_zero,
            bool(trigger_source),
            detection_accurate in (0, 1),
        ]
    )
    scan_limit = 500 if has_filters else safe_limit

    events = repo.get_recent_events(limit=scan_limit)
    filtered_events: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload", {})
        total_wheelchair_cnt = sum(int(payload.get(f"{cam}_wheelchair_cnt", 0)) for cam in CAM_KEYS)
        total_child_cnt = sum(int(payload.get(f"{cam}_child_count", 0)) for cam in CAM_KEYS)
        any_cam_pedestrians = max(int(payload.get(f"{cam}_count", 0)) for cam in CAM_KEYS)

        if wheelchair_gt_zero and total_wheelchair_cnt <= 0:
            continue
        if any_cam_count_gt is not None and any_cam_pedestrians <= any_cam_count_gt:
            continue
        if child_gt_zero and total_child_cnt <= 0:
            continue
        if trigger_source and payload.get("trigger_source") != trigger_source:
            continue
        if detection_accurate in (0, 1) and event.get("detection_accurate") != detection_accurate:
            continue
        filtered_events.append(event)
        if len(filtered_events) >= safe_limit:
            break

    return jsonify({"events": filtered_events})


@app.route("/api/events/<int:event_id>")
def api_event_by_id(event_id: int) -> Response:
    event = repo.get_event_by_id(event_id)
    if event is None:
        return jsonify({"error": "Event not found"}), 404
    if not require_archive_auth_for_event(event):
        return jsonify({"error": "Forbidden"}), 403
    return jsonify(event)


@app.route("/api/events/<int:event_id>/accuracy", methods=["POST"])
def api_set_event_accuracy(event_id: int) -> Response:
    from flask import request

    body = request.get_json(silent=True) or {}
    detection_accurate = body.get("detection_accurate")
    if not isinstance(detection_accurate, bool):
        return jsonify({"error": "Field detection_accurate must be boolean"}), 400

    updated = repo.set_detection_accurate(event_id, detection_accurate)
    if not updated:
        return jsonify({"error": "Event not found"}), 404
    return jsonify({"ok": True, "detection_accurate": 1 if detection_accurate else 0})


@app.route("/api/camera/<source_name>/<cam_name>.jpg")
def camera_image(source_name: str, cam_name: str):
    if cam_name not in CAM_KEYS:
        return jsonify({"error": "Unknown camera"}), 404
    if source_name not in IMAGE_SOURCES:
        return jsonify({"error": "Unknown image source"}), 404

    image_path = IMAGE_SOURCES[source_name] / CAM_IMAGE_FILENAMES.get(cam_name, f"{cam_name}.jpg")
    return send_image_with_fallback(image_path)


@app.route("/api/camera/<cam_name>.jpg")
def camera_image_legacy(cam_name: str):
    return camera_image("pedestrian", cam_name)


@app.route("/api/events/<int:event_id>/camera/<source_name>/<cam_name>.jpg")
def archived_event_camera_image(event_id: int, source_name: str, cam_name: str):
    if cam_name not in CAM_KEYS:
        return jsonify({"error": "Unknown camera"}), 404
    if source_name not in IMAGE_SOURCES:
        return jsonify({"error": "Unknown image source"}), 404

    event = repo.get_event_by_id(event_id)
    if event is None:
        return jsonify({"error": "Event not found"}), 404
    if not require_archive_auth_for_event(event):
        return jsonify({"error": "Forbidden"}), 403

    image_path = EVENT_ARCHIVE_DIR / f"event_{event_id}" / source_name / CAM_IMAGE_FILENAMES.get(cam_name, f"{cam_name}.jpg")
    return send_image_with_fallback(image_path)


@app.route("/api/events/<int:event_id>/camera/<cam_name>.jpg")
def archived_event_camera_image_legacy(event_id: int, cam_name: str):
    return archived_event_camera_image(event_id, "pedestrian", cam_name)


@app.route("/api/live/<cam_name>/mjpeg")
def live_stream(cam_name: str):
    if cam_name not in CAM_KEYS:
        return jsonify({"error": "Unknown camera"}), 404

    rtsp_url = RTSP_STREAMS.get(cam_name)
    if not rtsp_url:
        return jsonify({"error": f"RTSP stream for {cam_name} is not configured"}), 404

    def generate():
        cmd = [
            "ffmpeg",
            "-rtsp_transport",
            "tcp",
            "-i",
            rtsp_url,
            "-f",
            "mjpeg",
            "-q:v",
            "5",
            "-vf",
            "fps=10,scale=960:-1",
            "pipe:1",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        try:
            assert proc.stdout is not None
            buffer = b""
            for chunk in iter(lambda: proc.stdout.read(4096), b""):
                buffer += chunk
                while True:
                    start = buffer.find(b"\xff\xd8")
                    end = buffer.find(b"\xff\xd9")
                    if start != -1 and end != -1 and end > start:
                        frame = buffer[start : end + 2]
                        buffer = buffer[end + 2 :]
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                        )
                    else:
                        break
        finally:
            proc.kill()
            proc.wait()

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False)
