#!/usr/bin/env python3
"""Web UI for MiniMax Music 3, fronted by a hot audiocpp_server.

The model stays loaded in `audiocpp_server` (see server_config.json) and this
server only:

  - serves the page (lyrics + music description inputs)
  - validates jobs and queues them (one in-flight request per model on the
    hot server side; extra jobs wait in our queue)
  - forwards each job to the hot server's POST /v1/tasks/run
  - stores the returned WAV for playback/download

No model is loaded here, so it adds no GPU memory. The hot server does the
serializing: concurrent submissions just queue on its side.

Usage:
  python3 music3_webui/server.py
Environment:
  HOT_SERVER   base URL of audiocpp_server (default: http://127.0.0.1:8080)
  MODEL_ID     model id from server_config.json (default: music3)
  PORT         listen port (default: 8321)
  HOST         bind address (default: 0.0.0.0)
"""

import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
HOT_SERVER = os.environ.get("HOT_SERVER", "http://127.0.0.1:8080").rstrip("/")
MODEL_ID = os.environ.get("MODEL_ID", "MiniMax-Music-3")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8321"))
OUT_DIR = ROOT / "output"
OUT_DIR.mkdir(exist_ok=True)
LOG_TAIL_CHARS = 8000

PAGE = (ROOT / "index.html").read_text(encoding="utf-8")

TAG_RE = re.compile(r"^\s*\[([a-z0-9-]+)\]", re.IGNORECASE)
KNOWN_TAGS = {
    "intro", "verse", "pre-chorus", "chorus", "post-chorus",
    "bridge", "instrumental", "solo", "outro",
}


def hot_health():
    try:
        with urllib.request.urlopen(HOT_SERVER + "/health", timeout=3) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return {"status": "unreachable", "error": str(e)}


class Job:
    def __init__(self, params):
        self.id = uuid.uuid4().hex[:12]
        self.params = params
        self.status = "queued"          # queued | running | done | error | cancelled
        self.error = None
        self.log = ""
        self.wav_path = None
        self.submitted = False
        self.started_at = None
        self.finished_at = None
        self._lock = threading.Lock()

    def append_log(self, text):
        with self._lock:
            self.log = (self.log + text)[-LOG_TAIL_CHARS:]

    def public(self):
        with self._lock:
            tail = self.log[-4000:]
            return {
                "id": self.id,
                "status": self.status,
                "error": self.error,
                "log": tail,
                "logLen": len(self.log),
                "wav": f"/audio/{self.id}.wav" if self.wav_path else None,
                "params": self.params,
                "startedAt": self.started_at,
                "finishedAt": self.finished_at,
            }


JOBS = {}
JOBS_LOCK = threading.Lock()


def sanitize_lyrics(lyrics: str) -> str:
    """Put a leading section tag on its own line (tag + text on one line
    is silently dropped by the model's input contract)."""
    lines = []
    for line in lyrics.splitlines():
        m = TAG_RE.match(line)
        rest = line[m.end():] if m else ""
        if m and rest.strip():
            lines.append(m.group(0).strip())
            lines.append(rest.strip())
        else:
            lines.append(line)
    return "\n".join(lines)


def lint_caption(caption: str) -> list:
    notes = []
    low = caption.lower()
    for section in ("global metadata", "vocal details", "arrangement"):
        if section not in low:
            notes.append(f"caption is missing the '{section}' section (guide: 3 headings)")
    if len(caption) > 1200:
        notes.append("caption is long; the guide suggests ~250-450 words")
    return notes


def run_job(job: Job):
    p = job.params
    body = json.dumps({
        "model": MODEL_ID,
        "request": {
            "text": p["caption"],
            "options": {
                "lyrics": p["lyrics"],
                "duration_sec": p["duration_sec"],
                "num_inference_steps": p["num_inference_steps"],
                "guidance_scale": p["guidance_scale"],
                "ar_guidance_scale": p["ar_guidance_scale"],
                "top_k": p["top_k"],
                "seed": p["seed"],
            },
        },
    }).encode()
    wav_out = OUT_DIR / f"{job.id}.wav"

    job.status = "running"
    job.started_at = time.time()
    job.submitted = True
    job.append_log(
        f"-> {HOT_SERVER}/v1/tasks/run (model={MODEL_ID})\n"
        f"   duration={p['duration_sec']}s steps={p['num_inference_steps']} "
        f"seed={p['seed']}\n"
        "(hot server serializes requests; 'running' includes queue wait)\n\n"
    )
    try:
        req = urllib.request.Request(
            HOT_SERVER + "/v1/tasks/run", data=body,
            headers={"Content-Type": "application/json"},
        )
        # no timeout: long generations are normal; the hot server owns the work
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        # /v1/tasks/run replies with the flat task result
        # ({"audio": <base64>, "sample_rate": ..., "channels": ..., "timing": {...}});
        # also tolerate a {"result": {...}} envelope.
        result = data.get("result") if isinstance(data.get("result"), dict) else data
        audio = result.get("audio") or ""
        if audio.startswith("data:"):
            audio = audio.split(",", 1)[1] if "," in audio else ""
        if not audio:
            raise RuntimeError(
                "hot server returned no audio (check server log; "
                "result keys: " + ", ".join(result.keys()) + ")")
        wav_out.write_bytes(base64.b64decode(audio))
        job.wav_path = str(wav_out)
        job.status = "done"
        elapsed = time.time() - job.started_at
        timing = result.get("timing") or {}
        job.append_log(
            f"[webui] done in {elapsed:.1f}s "
            f"({wav_out.stat().st_size / 1e6:.2f} MB, {wav_out.name})\n"
            + (f"   timing: {timing}\n" if timing else "")
        )
    except urllib.error.HTTPError as e:
        snippet = e.read().decode(errors="replace")[:1500]
        job.status = "error"
        job.error = f"hot server HTTP {e.code}"
        job.append_log(f"\n[webui] ERROR: {job.error}\n{snippet}\n")
    except Exception as e:  # noqa: BLE001
        job.status = "error"
        job.error = str(e)
        job.append_log(f"\n[webui] ERROR: {e}\n")
    job.finished_at = time.time()


def worker_loop():
    """Pull queued jobs one at a time and submit them to the hot server.

    The hot server also serializes per model, so this keeps our queue honest
    and prevents unbounded in-flight HTTP connections.
    """
    while True:
        job = None
        with JOBS_LOCK:
            for j in JOBS.values():
                if j.status == "queued":
                    job = j
                    break
        if job is None:
            time.sleep(0.5)
            continue
        if job.status != "queued":  # cancelled while waiting
            continue
        run_job(job)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        import sys
        sys.stderr.write("[webui] " + (fmt % args) + "\n")

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _hot_health(self):
        return hot_health()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._html(200, PAGE)
        elif url.path == "/api/status":
            health = self._hot_health()
            with JOBS_LOCK:
                jobs = [j.public() for j in reversed(list(JOBS.values()))]
            self._json(200, {
                "hotServer": HOT_SERVER,
                "modelId": MODEL_ID,
                "health": health,
                "jobs": jobs[:50],
            })
        elif url.path.startswith("/api/jobs/"):
            jid = url.path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = JOBS.get(jid)
            if job is None:
                self._json(404, {"error": "no such job"})
            else:
                self._json(200, job.public())
        elif url.path.startswith("/audio/"):
            name = Path(url.path).name  # avoid traversal
            wav = OUT_DIR / name
            if wav.exists():
                data = wav.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json(404, {"error": "no such audio"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/generate":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "bad JSON"})
                return

            health = self._hot_health()
            if health.get("status") != "ok":
                self._json(503, {"error": f"hot server not reachable at {HOT_SERVER} (run ./run.sh)"})
                return

            lyrics = sanitize_lyrics(str(payload.get("lyrics") or "")).strip()
            caption = str(payload.get("caption") or "").strip()
            if not lyrics:
                self._json(400, {"error": "lyrics are required"})
                return
            if not caption:
                self._json(400, {"error": "music description (caption) is required"})
                return

            def num(key, default, lo, hi, as_int):
                v = payload.get(key, default)
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    self._json(400, {"error": f"{key} must be a number"})
                    return None
                if v < lo or v > hi:
                    self._json(400, {"error": f"{key} must be in [{lo}, {hi}]"})
                    return None
                return int(v) if as_int else v

            duration_sec = num("duration_sec", 180, 1, 600, True)
            if duration_sec is None:
                return
            steps = num("num_inference_steps", 30, 1, 200, True)
            if steps is None:
                return
            guidance = num("guidance_scale", 1.7, 0.1, 10, False)
            if guidance is None:
                return
            ar_guidance = num("ar_guidance_scale", 1.5, 0.1, 10, False)
            if ar_guidance is None:
                return
            top_k = num("top_k", 50, 1, 10000, True)
            if top_k is None:
                return
            seed = num("seed", int(time.time()) % 100000, 0, 2**31 - 1, True)
            if seed is None:
                return

            warnings = []
            tags = set()
            for line in lyrics.splitlines():
                m = TAG_RE.match(line)
                if m:
                    tags.add(m.group(1).lower())
            unknown = tags - KNOWN_TAGS
            if unknown:
                warnings.append(f"unusual section tags: {', '.join(sorted(unknown))}")
            warnings.extend(lint_caption(caption))

            job = Job({
                "lyrics": lyrics,
                "caption": caption,
                "duration_sec": duration_sec,
                "num_inference_steps": steps,
                "guidance_scale": guidance,
                "ar_guidance_scale": ar_guidance,
                "top_k": top_k,
                "seed": seed,
            })
            with JOBS_LOCK:
                JOBS[job.id] = job
            self._json(201, {"id": job.id, "warnings": warnings})
        elif url.path.startswith("/api/jobs/") and url.path.endswith("/cancel"):
            jid = url.path.split("/")[-2]
            with JOBS_LOCK:
                job = JOBS.get(jid)
            if job is None:
                self._json(404, {"error": "no such job"})
            elif not job.submitted and job.status == "queued":
                with job._lock:
                    job.status = "cancelled"
                job.append_log("\n[webui] cancelled before submission\n")
                self._json(200, {"id": jid, "status": "cancelled"})
            else:
                self._json(409, {"error": "already running on the hot server; cannot cancel"})
        else:
            self._json(404, {"error": "not found"})


def main():
    threading.Thread(target=worker_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Music 3 web UI (proxy) on http://{HOST}:{PORT}")
    print(f"  hot server: {HOT_SERVER} (model id: {MODEL_ID})")
    print(f"  health    : {json.dumps(hot_health())[:200]}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
