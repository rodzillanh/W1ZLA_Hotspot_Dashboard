"""Bridges camera feeds (generic RTSP, Bambu Labs A1) to plain MJPEG
multipart streams, so the frontend only ever needs an <img> tag -- it
doesn't know or care which camera type is behind it.

One background worker thread per *actively viewed* camera, each producing
JPEG frame bytes into a shared buffer that any number of HTTP viewers read
from (a "one producer, many consumers" broadcaster) -- not one ffmpeg/Bambu
connection per browser tab. Workers start lazily on first view and stop
themselves after CAMERA_IDLE_STOP_SEC with no viewers, so an enabled-but-
unwatched camera doesn't run ffmpeg or hold a printer connection forever.

RTSP: spawns `ffmpeg -f mjpeg ...` and splits its stdout on JPEG SOI/EOI
markers (the -f mjpeg muxer just concatenates whole JPEGs with no other
framing). Requires the ffmpeg binary (see install.sh/Dockerfile).

Bambu Labs A1: uses the bambulabs_api package (its camera protocol is a
proprietary TLS/port-6000 scheme, not RTSP -- a real dependency here, not a
hand-rolled reimplementation, same reasoning as paho-mqtt/aprslib elsewhere
in this project) and polls printer.get_camera_image() on an interval.

Both worker types degrade the same way every other integration in this
project does: connection failures are caught, retried with a backoff, and
surfaced via get_status() rather than raised -- a flaky camera should never
take down the poll loop or the Flask process.
"""
import io
import subprocess
import threading
import time

import config

try:
    import bambulabs_api as bl
except ImportError:
    bl = None


class _CameraWorker:
    """Base class: owns the background thread, the latest-frame buffer, and
    viewer refcounting. Subclasses implement _run_once() to produce frames."""

    def __init__(self, camera: dict):
        self.camera_id = camera["id"]
        self._camera   = camera
        self._lock     = threading.Condition()
        self._frame:      bytes | None = None
        self._frame_seq   = 0
        self._state       = "connecting"   # connecting | connected | reconnecting | offline
        self._detail       = ""
        self._last_frame_at = 0.0
        self._viewers      = 0
        self._last_viewer_at = time.time()
        self._stop_event   = threading.Event()
        self._thread        = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._teardown()
        with self._lock:
            self._lock.notify_all()

    def add_viewer(self) -> None:
        with self._lock:
            self._viewers += 1

    def remove_viewer(self) -> None:
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            self._last_viewer_at = time.time()

    def idle_seconds(self) -> float:
        with self._lock:
            return 0.0 if self._viewers > 0 else time.time() - self._last_viewer_at

    def status(self) -> dict:
        with self._lock:
            return {
                "state":  self._state,
                "detail": self._detail,
                "last_frame_at": self._last_frame_at,
            }

    def wait_for_frame(self, last_seq: int, timeout: float) -> tuple[bytes | None, int]:
        """Blocks until a frame newer than last_seq is available, or timeout.
        Returns (frame_bytes_or_None, new_seq). Checking self._state here
        (not just frame_seq) matters: _set_state()'s notify_all() can fire
        before this method starts waiting (e.g. an immediately-offline
        misconfigured camera), and a Condition's notify only wakes threads
        already waiting -- without this check, that notification is missed
        and the caller stalls for the full timeout instead of failing fast."""
        with self._lock:
            if self._stop_event.is_set():
                return None, last_seq
            if self._frame_seq == last_seq and self._state not in ("offline",):
                self._lock.wait(timeout)
            if self._stop_event.is_set():
                return None, last_seq
            return self._frame, self._frame_seq

    def _set_frame(self, data: bytes) -> None:
        with self._lock:
            self._frame = data
            self._frame_seq += 1
            self._last_frame_at = time.time()
            self._state = "connected"
            self._detail = ""
            self._lock.notify_all()

    def _set_state(self, state: str, detail: str = "") -> None:
        with self._lock:
            self._state = state
            self._detail = detail
            self._lock.notify_all()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_once()
            except Exception as e:
                self._set_state("reconnecting", str(e))
            if not self._stop_event.is_set():
                self._stop_event.wait(config.CAMERA_RECONNECT_BACKOFF)

    def _run_once(self) -> None:
        raise NotImplementedError

    def _teardown(self) -> None:
        pass


class _RtspWorker(_CameraWorker):
    def __init__(self, camera: dict):
        super().__init__(camera)
        self._proc: subprocess.Popen | None = None

    def _run_once(self) -> None:
        url = self._camera.get("rtsp_url", "")
        if not url:
            self._set_state("offline", "No RTSP URL configured")
            return

        cmd = [
            "ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp",
            "-i", url,
            "-f", "mjpeg", "-q:v", "5", "-r", str(config.CAMERA_RTSP_FPS),
            "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self._set_state("offline", "ffmpeg not found -- see README.md camera setup")
            return

        buf = b""
        last_data_at = time.time()
        try:
            while not self._stop_event.is_set():
                chunk = self._proc.stdout.read(4096)
                if not chunk:
                    if self._proc.poll() is not None:
                        raise RuntimeError("ffmpeg exited")
                    if time.time() - last_data_at > config.CAMERA_FFMPEG_TIMEOUT:
                        raise RuntimeError("ffmpeg stream timed out")
                    continue
                last_data_at = time.time()
                buf += chunk
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start == -1:
                        buf = b""
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end == -1:
                        buf = buf[start:]
                        break
                    self._set_frame(buf[start:end + 2])
                    buf = buf[end + 2:]
        finally:
            self._kill_proc()

    def _kill_proc(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.kill()
            self._proc.wait(timeout=2)
        except Exception:
            pass
        self._proc = None

    def _teardown(self) -> None:
        self._kill_proc()


class _BambuWorker(_CameraWorker):
    def __init__(self, camera: dict):
        super().__init__(camera)
        self._printer = None

    def _run_once(self) -> None:
        if bl is None:
            self._set_state("offline", "bambulabs_api not installed -- see README.md camera setup")
            return

        ip          = self._camera.get("ip", "")
        serial      = self._camera.get("serial", "")
        access_code = self._camera.get("access_code", "")
        if not (ip and serial and access_code):
            self._set_state("offline", "Missing printer IP, serial, or access code")
            return

        self._printer = bl.Printer(ip, access_code, serial)
        try:
            self._printer.connect()
            while not self._stop_event.is_set():
                image = self._printer.get_camera_image()
                if image is None:
                    raise RuntimeError("No camera frame returned")
                buf = io.BytesIO()
                image.save(buf, format="JPEG")
                self._set_frame(buf.getvalue())
                self._stop_event.wait(config.CAMERA_BAMBU_POLL_SEC)
        finally:
            self._disconnect()

    def _disconnect(self) -> None:
        if self._printer is not None:
            try:
                self._printer.disconnect()
            except Exception:
                pass
            self._printer = None

    def _teardown(self) -> None:
        self._disconnect()


class CameraStreamManager:
    """One entry point the Flask routes talk to. Workers are created lazily
    (first viewer) and torn down after CAMERA_IDLE_STOP_SEC with none."""

    def __init__(self):
        self._workers: dict[str, _CameraWorker] = {}
        self._lock = threading.Lock()
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def _get_or_start(self, camera: dict) -> _CameraWorker:
        with self._lock:
            worker = self._workers.get(camera["id"])
            if worker is None:
                worker_cls = _BambuWorker if camera.get("type") == "bambu_a1" else _RtspWorker
                worker = worker_cls(camera)
                worker.start()
                self._workers[camera["id"]] = worker
            return worker

    def stream(self, camera: dict):
        """Generator yielding multipart/x-mixed-replace frame chunks --
        caller wraps this in a Flask Response. Registers/unregisters itself
        as a viewer so the idle-cleanup loop knows when it's safe to stop
        the underlying ffmpeg process / printer connection."""
        worker = self._get_or_start(camera)
        worker.add_viewer()
        seq = 0
        try:
            while True:
                frame, seq = worker.wait_for_frame(seq, timeout=config.CAMERA_FFMPEG_TIMEOUT)
                if frame is None:
                    if worker.status()["state"] == "offline":
                        break
                    continue
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        finally:
            worker.remove_viewer()

    def status(self, camera: dict) -> dict:
        with self._lock:
            worker = self._workers.get(camera["id"])
        if worker is None:
            return {"state": "stopped", "detail": "", "last_frame_at": 0.0}
        return worker.status()

    def remove(self, camera_id: str) -> None:
        """Called when a camera is deleted/renamed-away from Settings, so a
        stale worker doesn't keep running against config that no longer
        exists."""
        with self._lock:
            worker = self._workers.pop(camera_id, None)
        if worker is not None:
            worker.stop()

    def _cleanup_loop(self) -> None:
        while True:
            time.sleep(5)
            with self._lock:
                stale_ids = [
                    cam_id for cam_id, w in self._workers.items()
                    if w.idle_seconds() > config.CAMERA_IDLE_STOP_SEC
                ]
                stale_workers = [self._workers.pop(cam_id) for cam_id in stale_ids]
            # Stop the removed workers outside the lock (avoid blocking other
            # cameras' status/stream calls on a slow ffmpeg/printer teardown).
            for worker in stale_workers:
                worker.stop()
