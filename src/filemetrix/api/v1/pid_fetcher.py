import asyncio
import json
import logging
import os
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List
from urllib.parse import unquote

import datahugger
import requests
from datahugger import RepositoryNotSupportedError
from datahugger.utils import get_datapublisher_from_doi, get_re3data_repositories
from fastapi import APIRouter, HTTPException, Query, Body
from fastapi.responses import JSONResponse
from fastapi.responses import Response

from src.filemetrix.infra.commons import app_settings, API_PREFIX
from src.filemetrix.services import onedata_hugger
from src.filemetrix.infra.rabbitmq import publish_job

router = APIRouter(prefix=API_PREFIX)

# Ensure jobs dir exists
BASE_DIR = os.getenv("BASE_DIR", ".")
JOBS_DIR = Path(BASE_DIR) / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)


def _job_file_path(job_id: str) -> Path:
    return JOBS_DIR / f"job_{job_id}.json"


def _write_job(job_id: str, payload: dict):
    p = _job_file_path(job_id)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _read_job(job_id: str) -> dict | None:
    p = _job_file_path(job_id)
    if not p.exists():
        return None
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# All discovery-related endpoints:
# - /repositories (re3data cache)
# - /repository-collections/{r3id}
# - /repository-info/{pid:path}
# - /extensions/{pid:path}
# - /{pid:path} (main PID fetcher)


# This endpoint retrieves repository information for a given PID
# and fetches OAI data if available from the repository. It uses re3data
# to find the repository based on the publisher extracted from the PID (DOI).
# It then transforms the re3data XML response to JSON and attempts to fetch OAI metadata.
# If OAI data is found, it extracts the title, identifier, and collections. 
# If no OAI data is available, it returns basic repository information.
@router.get("/repository-info/{pid:path}",tags=["PID Fetcher"],
    summary="Retrieve repository information for a given PID",
    description="Fetches repository information from re3data based on the provided persistent identifier (PID).")
async def retrieve_repo_info(pid: str):

    decoded_pid = unquote(pid).replace("doi:", "")
    try:
        publisher = get_datapublisher_from_doi(decoded_pid)
        if not publisher:
            raise HTTPException(status_code=404, detail="Publisher not found for PID")
        data_repos = get_re3data_repositories()
        for repo in data_repos:
            if publisher.lower() == repo["name"].lower():
                repo_data =requests.get(f"https://www.re3data.org/api/v1/repository/{repo['id']}")
                url = "http://transformer.labs.dansdemo.nl/transform/r3data-xml-to-json.xsl"
                headers = {
                    "Content-Type": "application/xml",
                    "Authorization": f"Bearer {app_settings.METADATA_TRANSFORMER_SERVICE_API_KEY}"
                }
                transformer_response = requests.post(url, headers=headers, data=repo_data.text)
                result = json.loads(transformer_response.text)
                r = result["result"]
                rst = json.loads(r)
                oai = rst["repository"].get("oai", {})
                if oai:
                    oai_url = f"{oai.split('?')[0]}?verb=GetRecord&identifier={pid}&metadataPrefix=oai_dc"
                    logging.info(oai_url)
                    response = requests.get(oai_url)
                    if response.status_code != 200:
                        raise HTTPException(status_code=response.status_code, detail="Failed to fetch OAI data")

                    logging.info(f"Fetched OAI data from {oai_url}")
                    xml_data = response.text
                    ns = {  'dc': 'http://purl.org/dc/elements/1.1/',
                            'oai_dc': 'http://www.openarchives.org/OAI/2.0/oai_dc/',
                            'oai': 'http://www.openarchives.org/OAI/2.0/'}
                    root = ET.fromstring(xml_data)
                    title = root.find('.//dc:title', ns)
                    identifier = root.find('.//dc:identifier', ns)
                    rst["title"] = title.text if title is not None else None
                    rst["identifier"] = identifier.text if identifier is not None else None

                    set_specs = [elem.text for elem in root.findall('.//oai:header/oai:setSpec', ns)]

                    rst["collections"] = set_specs
                else:
                    logging.warning("No OAI information found in repository data")
                    rst["title"] = ""
                    rst["identifier"] = decoded_pid
                    rst["collections"] = []

                return Response(content=json.dumps(rst), media_type="application/json")
        raise HTTPException(status_code=404, detail="Repository not found in re3data")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/extensions/{pid:path}", tags=["PID Fetcher"])
async def get_extensions(pid: str):
    decoded_pid = unquote(pid).replace("doi:", "")
    try:
        metadata = await asyncio.to_thread(datahugger.info, decoded_pid, {"type": "file"})
        extensions_map = {}

        for file in metadata.files:
            raw = file.get("raw_metadata", {}) or {}
            content_type = raw.get("contentType") or raw.get("content_type") or file.get("contentType") or file.get("content_type")
            if not content_type:
                continue

            type_verified = raw.get("typeVerified", raw.get("type_verified"))
            type_verified = "TRUE" if type_verified is True else "FALSE" if type_verified is False else str(type_verified).upper() or "FALSE"

            filesize = raw.get("filesize") or file.get("size") or file.get("filesize") or 0
            filesize = int(filesize) if isinstance(filesize, (int, str)) and str(filesize).isdigit() else 0

            extensions_map.setdefault(content_type, {
                "contentType": content_type,
                "friendlyType": raw.get("friendlyType") or raw.get("friendly_type") or "",
                "typeVerified": type_verified,
                "filesize": filesize,
            })

        return JSONResponse(status_code=200, content={"extensions": list(extensions_map.values())})

    except RepositoryNotSupportedError as e:
        logging.error(f"Repository not supported: {e}")
        raise HTTPException(status_code=400, detail="Repository not supported")
    except Exception as e:
        logging.error(f"Error fetching metadata: {e}")
        raise HTTPException(status_code=500, detail="Error fetching metadata")


# New: POST /batch/pids -> create a job and publish to RabbitMQ
@router.post(
    "/batch/pids",
    response_class=JSONResponse,
    summary="Create a batch job for a list of PIDs",
    description="Accepts a JSON array of PIDs and schedules their processing (async) using RabbitMQ.",
    tags=["PID Fetcher"],
)
async def create_batch_job(pids: List[str] = Body(..., embed=False, description="Array of PIDs")):
    if not isinstance(pids, list) or len(pids) == 0:
        raise HTTPException(status_code=400, detail="Provide a non-empty JSON array of PIDs in the request body")

    job_id = uuid.uuid4().hex
    now = time.time()
    decoded_pids = [unquote(p).replace("doi:", "") for p in pids]

    job = {
        "job_id": job_id,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "total": len(decoded_pids),
        "completed": 0,
        "items": [{"pid": pid, "state": "pending"} for pid in decoded_pids],
    }

    _write_job(job_id, job)

    # publish to RabbitMQ; message contains job_id and pids
    try:
        publish_job({"job_id": job_id, "pids": decoded_pids})
    except Exception as e:
        logging.exception("Failed to publish job to RabbitMQ")
        job["status"] = "error"
        job["error"] = str(e)
        job["updated_at"] = time.time()
        _write_job(job_id, job)
        raise HTTPException(status_code=500, detail="Failed to enqueue job")

    return JSONResponse(status_code=202, content={"job_id": job_id, "location": f"/api/v1/jobs/{job_id}"})


# New: GET job status
@router.get(
    "/jobs/{job_id}",
    response_class=JSONResponse,
    summary="Get batch job status",
    description="Returns status and progress for a previously created batch job.",
    tags=["PID Fetcher"],
)
async def get_job_status(job_id: str):
    job = _read_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(status_code=200, content=job)


# New: GET job results (all items that are done)
@router.get(
    "/jobs/{job_id}/results",
    response_class=JSONResponse,
    summary="Get batch job results",
    description="Returns collected results for a completed or in-progress job.",
    tags=["PID Fetcher"],
)
async def get_job_results(job_id: str):
    job = _read_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # collect done items' results (worker writes item.result)
    done = [it for it in job.get("items", []) if it.get("state") == "done" and it.get("result")]
    return JSONResponse(status_code=200, content={"job_id": job_id, "done_count": len(done), "results": done})


@router.get(
    "/batch/pids",
    response_class=JSONResponse,
    summary="Fetch metadata files for a batch of PIDs",
    description="Accepts multiple `pid` query parameters and returns metadata files per PID.",
    tags=["PID Fetcher"],
)
async def batch_get_pids(pids: List[str] = Query(..., alias="pid",
                                        description="List of PIDs, e.g. /batch/pids?pid=doi:10...&pid=doi:20...")):
    # function body remains the same
    results = []
    for raw_pid in pids:
        start_time = time.perf_counter()
        decoded = unquote(raw_pid).replace("doi:", "")
        try:
            metadata = await asyncio.to_thread(datahugger.info, decoded, {"type": "file"})
        except RepositoryNotSupportedError as e:
            metadata = onedata_hugger.info(decoded)
            if not metadata:
                duration = time.perf_counter() - start_time
                logging.error(f"Repository not supported for PID {decoded}: {e}")
                results.append({
                    "pid": decoded,
                    "error": "Repository not supported",
                    "message": str(e),
                    "duration": duration
                })
                continue
        except Exception as e:
            duration = time.perf_counter() - start_time
            logging.error(f"Error fetching metadata for PID {decoded}: {e}")
            results.append({
                "pid": decoded,
                "error": "Error fetching metadata",
                "message": str(e),
                "duration": duration
            })
            continue

        duration = time.perf_counter() - start_time
        if duration > 30:
            logging.warning(f"Request for PID {decoded} exceeded 30s: {duration:.4f}s")
        results.append({"pid": decoded, "files": metadata.files, "duration": duration})

    return JSONResponse(status_code=200, content=results)

@router.get(
    "/extensions/batch/pids",
    response_class=JSONResponse,
    summary="Fetch extensions for a batch of PIDs",
    description="Accepts multiple `pid` query parameters and returns extensions per PID.",
    tags=["PID Fetcher"],
)
async def batch_get_extensions(pids: List[str] = Query(..., alias="pid", description="List of PIDs, e.g. /extensions/batch/pids?pid=doi:...&pid=doi:...")):
    results = []

    for raw_pid in pids:
        start_time = time.perf_counter()
        decoded = unquote(raw_pid).replace("doi:", "")

        try:
            metadata = await asyncio.to_thread(datahugger.info, decoded, {"type": "file"})
        except RepositoryNotSupportedError as e:
            # fallback to onedata
            metadata = onedata_hugger.info(decoded)
            if not metadata:
                duration = time.perf_counter() - start_time
                logging.error(f"Repository not supported for PID {decoded}: {e}")
                results.append({
                    "pid": decoded,
                    "error": "Repository not supported",
                    "message": str(e),
                    "duration": duration,
                    "extensions": [],
                })
                continue
        except Exception as e:
            duration = time.perf_counter() - start_time
            logging.error(f"Error fetching metadata for PID {decoded}: {e}")
            results.append({
                "pid": decoded,
                "error": "Error fetching metadata",
                "message": str(e),
                "duration": duration,
                "extensions": [],
            })
            continue

        # compute extensions using same logic as get_extensions
        extensions_map = {}
        for file in getattr(metadata, "files", []) or []:
            raw = file.get("raw_metadata", {}) or {}
            content_type = raw.get("contentType") or raw.get("content_type") or file.get("contentType") or file.get("content_type")
            if not content_type:
                continue

            type_verified = raw.get("typeVerified", raw.get("type_verified"))
            type_verified = "TRUE" if type_verified is True else "FALSE" if type_verified is False else str(type_verified).upper() or "FALSE"

            filesize = raw.get("filesize") or file.get("size") or file.get("filesize") or 0
            try:
                filesize = int(filesize)
            except Exception:
                filesize = 0

            extensions_map.setdefault(content_type, {
                "contentType": content_type,
                "friendlyType": raw.get("friendlyType") or raw.get("friendly_type") or "",
                "typeVerified": type_verified,
                "filesize": filesize,
            })

        duration = time.perf_counter() - start_time
        results.append({"pid": decoded, "extensions": list(extensions_map.values()), "duration": duration})

    return JSONResponse(status_code=200, content=results)


@router.get(
    "/extension/batch/pids",
    response_class=JSONResponse,
    summary="Fetch extensions for a batch of PIDs (singular path)",
    description="Accepts multiple `pid` query parameters and returns extensions per PID (singular 'extension' path).",
    tags=["PID Fetcher"],
)
async def batch_get_extensions_singular(pids: List[str] = Query(..., alias="pid", description="List of PIDs, e.g. /extension/batch/pids?pid=doi:...&pid=doi:...")):
    """Return a list of objects: {"pid": "<pid>", "extensions": [ ... ]}
    Keeps the same resolution logic (datahugger then onedata fallback).
    """
    results = []

    for raw_pid in pids:
        start_time = time.perf_counter()
        decoded = unquote(raw_pid).replace("doi:", "")

        try:
            metadata = await asyncio.to_thread(datahugger.info, decoded, {"type": "file"})
        except RepositoryNotSupportedError as e:
            metadata = onedata_hugger.info(decoded)
            if not metadata:
                duration = time.perf_counter() - start_time
                logging.error(f"Repository not supported for PID {decoded}: {e}")
                results.append({"pid": decoded, "extensions": [], "error": "Repository not supported", "message": str(e), "duration": duration})
                continue
        except Exception as e:
            duration = time.perf_counter() - start_time
            logging.error(f"Error fetching metadata for PID {decoded}: {e}")
            results.append({"pid": decoded, "extensions": [], "error": "Error fetching metadata", "message": str(e), "duration": duration})
            continue

        # compute extensions using same logic as get_extensions
        extensions_map = {}
        for file in getattr(metadata, "files", []) or []:
            raw = file.get("raw_metadata", {}) or {}
            content_type = raw.get("contentType") or raw.get("content_type") or file.get("contentType") or file.get("content_type")
            if not content_type:
                continue

            type_verified = raw.get("typeVerified", raw.get("type_verified"))
            type_verified = "TRUE" if type_verified is True else "FALSE" if type_verified is False else (str(type_verified).upper() if type_verified is not None else "FALSE")

            filesize = raw.get("filesize") or file.get("size") or file.get("filesize") or 0
            try:
                filesize = int(filesize)
            except Exception:
                filesize = 0

            extensions_map.setdefault(content_type, {
                "contentType": content_type,
                "friendlyType": raw.get("friendlyType") or raw.get("friendly_type") or "",
                "typeVerified": type_verified,
                "filesize": filesize,
            })

        duration = time.perf_counter() - start_time
        results.append({"pid": decoded, "extensions": list(extensions_map.values()), "duration": duration})

    return JSONResponse(status_code=200, content=results)


@router.get(
    "/{pid:path}",
    response_class=JSONResponse,
    summary="Fetch metadata files for a given PID",
    description="Retrieves metadata files for the provided persistent identifier (PID). Optionally allows downloading the files.",
    tags=["PID Fetcher"])
async def get_pid(pid: str):
    start_time = time.perf_counter()
    logging.info("get doi")
    decoded_doi = unquote(pid)
    print(f"Received DOI: {decoded_doi}")
    logging.info(f"Received DOI: {decoded_doi}")
    try:
        metadata = await asyncio.to_thread(datahugger.info, decoded_doi, {"type": "file"})
        # metadata =datahugger.info(decoded_doi)
    except RepositoryNotSupportedError as e:
        # fall-back and try to resolve the identifier as Onedata dataset
        metadata = onedata_hugger.info(decoded_doi)
        if not metadata:
            duration = time.perf_counter() - start_time
            if duration > 30:
                logging.warning(f"Request duration exceeded 30 seconds: {duration:.4f} seconds")
                print(f"WARNING: Request duration exceeded 30 seconds: {duration:.4f} seconds")
            logging.error(f"Repository not supported: {e}")
            logging.info(f"Request duration: {duration:.4f} seconds")
            return JSONResponse(
                status_code=400,
                content={"error": "Repository not supported", "message": str(e), "duration": duration}
            )
    except Exception as e:
        duration = time.perf_counter() - start_time
        if duration > 30:
            logging.warning(f"Request duration exceeded 30 seconds: {duration:.4f} seconds")
            print(f"WARNING: Request duration exceeded 30 seconds: {duration:.4f} seconds")
        logging.error(f"Error fetching metadata: {e}")
        logging.info(f"Request duration: {duration:.4f} seconds")
        return JSONResponse(
            status_code=500,
            content={"error": "Repository not supported", "message": str(e), "duration": duration}
        )
    logging.info(f"Return metadata files for {decoded_doi}")
    duration = time.perf_counter() - start_time
    if duration > 30:
        logging.warning(f"Request duration exceeded 30 seconds: {duration:.4f} seconds")
        print(f"WARNING: Request duration exceeded 30 seconds: {duration:.4f} seconds")
    logging.info(f"Request duration: {duration:.4f} seconds")
    print(f"Request duration: {duration:.4f} seconds")
    return {"files": metadata.files}
