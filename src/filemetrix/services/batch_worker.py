import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import datahugger
from datahugger import RepositoryNotSupportedError
from urllib.parse import unquote

from src.filemetrix.infra.rabbitmq import consume_jobs
from src.filemetrix.services import onedata_hugger

BASE_DIR = os.getenv("BASE_DIR", ".")
JOBS_DIR = Path(BASE_DIR) / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)


def _job_file_path(job_id: str) -> Path:
    return JOBS_DIR / f"job_{job_id}.json"


def _read_job(job_id: str) -> dict | None:
    p = _job_file_path(job_id)
    if not p.exists():
        return None
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_job(job_id: str, payload: dict):
    p = _job_file_path(job_id)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def process_pid(pid: str) -> dict[str, Any]:
    decoded = unquote(pid).replace("doi:", "")
    start_time = time.time()
    try:
        metadata = datahugger.info(decoded, {"type": "file"})
    except RepositoryNotSupportedError as e:
        metadata = onedata_hugger.info(decoded)
        if not metadata:
            return {"pid": decoded, "error": "Repository not supported", "message": str(e), "duration": time.time() - start_time}
    except Exception as e:
        return {"pid": decoded, "error": "Error fetching metadata", "message": str(e), "duration": time.time() - start_time}

    duration = time.time() - start_time
    return {"pid": decoded, "files": metadata.files, "duration": duration}


def _on_message(msg: dict[str, Any], ch, method, properties):
    logging.info(f"Worker received msg: {msg}")
    job_id = msg.get("job_id")
    pids = msg.get("pids", [])
    if not job_id or not pids:
        logging.error("Invalid job message")
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    job = _read_job(job_id) or {
        "job_id": job_id,
        "status": "running",
        "items": [{"pid": p, "state": "pending"} for p in pids],
        "total": len(pids),
        "completed": 0,
        "created_at": time.time(),
        "updated_at": time.time(),
    }

    job["status"] = "running"
    job["updated_at"] = time.time()
    _write_job(job_id, job)

    for idx, pid in enumerate(pids):
        # update item to in_progress
        for it in job["items"]:
            if it["pid"] == pid:
                it["state"] = "in_progress"
                it["updated_at"] = time.time()
                break
        _write_job(job_id, job)

        res = process_pid(pid)

        # write result into job item
        for it in job["items"]:
            if it["pid"] == pid:
                if "error" in res:
                    it["state"] = "error"
                    it["error"] = res.get("message")
                else:
                    it["state"] = "done"
                    it["result"] = res
                it["duration"] = res.get("duration")
                it["updated_at"] = time.time()
                break

        job["completed"] = sum(1 for i in job["items"] if i.get("state") == "done")
        job["updated_at"] = time.time()
        _write_job(job_id, job)

    # finished
    job["status"] = "completed"
    job["updated_at"] = time.time()
    _write_job(job_id, job)

    ch.basic_ack(delivery_tag=method.delivery_tag)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.info("Starting batch worker, connecting to RabbitMQ...")
    consume_jobs(_on_message)

