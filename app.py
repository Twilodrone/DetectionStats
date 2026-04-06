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
from flask import Flask, Response, jsonify, redirect, render_template, send_file, url_for


REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "2"))
DB_PATH = os.getenv("DB_PATH", "signal_events.db")
CAMERA_PICTURES_DIR = Path(os.getenv("CAMERA_PICTURES_DIR", "/home/sdp/Detector/pictures"))
EVENT_ARCHIVE_DIR = Path(os.getenv("EVENT_ARCHIVE_DIR", "./event_archive"))
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
RTSP_CONFIG_PATH = Path(os.getenv("RTSP_CONFIG_PATH", "camera_streams.json"))
RTSP_SNAPSHOT_TIMEOUT_SECONDS = float(os.getenv("RTSP_SNAPSHOT_TIMEOUT_SECONDS", "5"))

CAM_KEYS = ["Cam1", "Cam2", "Cam3", "Cam4"]
REDIS_KEYS = [
    "Signal",
    "Cam1_count",
    "Cam2_count",
    "Cam3_count",
    "Cam4_count",
    "Cam1_wheelchair_cnt",
    "Cam2_wheelchair_cnt",
    "Cam3_wheelchair_cnt",
    "Cam4_wheelchair_cnt",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                    payload_json TEXT NOT NULL
                )
                """
            )
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
                SELECT id, ts_utc, signal_value, payload_json
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
                }
            )
        return events

    def get_event_by_id(self, event_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, ts_utc, signal_value, payload_json
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
        }

    def get_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0]
            last = conn.execute(
                "SELECT ts_utc FROM signal_events ORDER BY id DESC LIMIT 1"
            ).fetchone()

        return {
            "total_triggers": int(total),
            "last_trigger_utc": last["ts_utc"] if last else None,
        }


class RedisWatcher:
    def __init__(self, repo: SignalRepository, rtsp_streams: dict[str, str]) -> None:
        self.repo = repo
        self.rtsp_streams = rtsp_streams
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
        self._archive_queue: Queue[int] = Queue()
        self._last_snapshot: dict[str, int] = {key: 0 for key in REDIS_KEYS}
        self._last_redis_ok: bool = False

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

    def _archive_event_images(self, event_id: int) -> None:
        event_dir = EVENT_ARCHIVE_DIR / f"event_{event_id}"
        event_dir.mkdir(parents=True, exist_ok=True)

        for cam_name in CAM_KEYS:
            dst = event_dir / f"{cam_name}.jpg"
            rtsp_url = self.rtsp_streams.get(cam_name)
            if rtsp_url and self._capture_rtsp_frame(rtsp_url, dst):
                continue

            if not rtsp_url:
                logging.warning("RTSP URL is not configured for %s. Using fallback image.", cam_name)
            else:
                logging.warning("RTSP capture failed for %s. Using fallback image.", cam_name)

            src = CAMERA_PICTURES_DIR / f"{cam_name}.jpg"
            if src.exists():
                shutil.copy2(src, dst)
            else:
                logging.warning("Fallback image for %s not found: %s", cam_name, src)

    def _archive_worker(self) -> None:
        while not self._stop_event.is_set() or not self._archive_queue.empty():
            try:
                event_id = self._archive_queue.get(timeout=0.5)
            except Empty:
                continue
            try:
                self._archive_event_images(event_id)
            finally:
                self._archive_queue.task_done()

    def _run(self) -> None:
        previous_signal = 0
        while not self._stop_event.is_set():
            try:
                values = self._fetch_values()
                self._last_snapshot = values
                self._last_redis_ok = True

                current_signal = values.get("Signal", 0)
                if previous_signal == 0 and current_signal == 1:
                    event_id = self.repo.add_event(payload=values, signal_value=current_signal)
                    self._archive_queue.put(event_id)

                previous_signal = current_signal
            except redis.RedisError:
                self._last_redis_ok = False
            time.sleep(POLL_INTERVAL_SECONDS)


app = Flask(__name__)
RTSP_STREAMS = load_rtsp_config(RTSP_CONFIG_PATH)
repo = SignalRepository(DB_PATH)
watcher = RedisWatcher(repo, RTSP_STREAMS)
watcher.start()


@app.route("/")
def index() -> str:
    return render_template("index.html", cams=CAM_KEYS)


@app.route("/archive")
def archive() -> str:
    return render_template("archive.html")


@app.route("/live")
def live() -> str:
    return render_template("live.html", cams=CAM_KEYS, streams=RTSP_STREAMS)


@app.route("/events/<int:event_id>")
def event_report(event_id: int):
    event = repo.get_event_by_id(event_id)
    if event is None:
        return jsonify({"error": "Event not found"}), 404

    return render_template("event_report.html", event=event, cams=CAM_KEYS)


@app.route("/cgi-bin/luci")
@app.route("/cgi-bin/luci/")
def luci_compat_redirect() -> Response:
    return redirect(url_for("index"), code=302)


@app.route("/api/status")
def api_status() -> Response:
    snapshot = watcher.get_snapshot()
    stats = repo.get_stats()
    return jsonify(
        {
            "timestamp_utc": utc_now_iso(),
            "redis": snapshot,
            "stats": stats,
        }
    )


@app.route("/api/events")
def api_events() -> Response:
    from flask import request

    limit = request.args.get("limit", default=50, type=int)
    wheelchair_gt_zero = request.args.get("wheelchair_gt_zero", default=0, type=int) == 1
    any_cam_count_gt = request.args.get("any_cam_count_gt", type=int)

    events = repo.get_recent_events(limit=limit)
    filtered_events: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload", {})
        total_wheelchair_cnt = sum(int(payload.get(f"{cam}_wheelchair_cnt", 0)) for cam in CAM_KEYS)
        any_cam_pedestrians = max(int(payload.get(f"{cam}_count", 0)) for cam in CAM_KEYS)

        if wheelchair_gt_zero and total_wheelchair_cnt <= 0:
            continue
        if any_cam_count_gt is not None and any_cam_pedestrians <= any_cam_count_gt:
            continue
        filtered_events.append(event)

    return jsonify({"events": filtered_events})


@app.route("/api/events/<int:event_id>")
def api_event_by_id(event_id: int) -> Response:
    event = repo.get_event_by_id(event_id)
    if event is None:
        return jsonify({"error": "Event not found"}), 404
    return jsonify(event)


@app.route("/api/camera/<cam_name>.jpg")
def camera_image(cam_name: str):
    if cam_name not in CAM_KEYS:
        return jsonify({"error": "Unknown camera"}), 404

    image_path = CAMERA_PICTURES_DIR / f"{cam_name}.jpg"
    if not image_path.exists():
        return jsonify({"error": f"Image not found: {image_path}"}), 404

    return send_file(image_path, mimetype="image/jpeg", max_age=0)


@app.route("/api/events/<int:event_id>/camera/<cam_name>.jpg")
def archived_event_camera_image(event_id: int, cam_name: str):
    if cam_name not in CAM_KEYS:
        return jsonify({"error": "Unknown camera"}), 404

    image_path = EVENT_ARCHIVE_DIR / f"event_{event_id}" / f"{cam_name}.jpg"
    if not image_path.exists():
        return jsonify({"error": f"Image not found: {image_path}"}), 404

    return send_file(image_path, mimetype="image/jpeg", max_age=0)


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
