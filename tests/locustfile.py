import os
import time
import urllib.parse
from typing import List

from locust import HttpUser, task, between

# Config via env
API_PREFIX = os.getenv("API_PREFIX", "/api/v1")
SAMPLE_PIDS_ENV = os.getenv("SAMPLE_PIDS", "doi:10.17026/SS/C9KXL3")
SAMPLE_PIDS = [p.strip() for p in SAMPLE_PIDS_ENV.split(",") if p.strip()]
API_KEY = os.getenv("FILEMETRIX_API_KEY")
SAMPLE_REPO_ID = int(os.getenv("SAMPLE_REPO_ID", "1"))
SAMPLE_METADATA_PREFIX = os.getenv("SAMPLE_METADATA_PREFIX", "oai_dc")
SAMPLE_REPO_URL = os.getenv("SAMPLE_REPO_URL", "http://example.org/oai")
SAMPLE_HARVEST_STATUSES = os.getenv("SAMPLE_HARVEST_STATUSES", "IN_PROGRESS,COMPLETED")
HARVEST_STATUSES = [s.strip() for s in SAMPLE_HARVEST_STATUSES.split(",") if s.strip()]


def build_headers(content_type_json: bool = False):
    headers = {"Accept": "application/json"}
    if content_type_json:
        headers["Content-Type"] = "application/json"
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


class FullCoverageUser(HttpUser):
    """Locust user exercising all API v1 endpoints."""
    wait_time = between(0.5, 1.5)

    def on_start(self):
        self.headers = build_headers()
        self.json_headers = build_headers(True)
        self.sample_pids: List[str] = SAMPLE_PIDS
        self.repo_id = SAMPLE_REPO_ID
        self.prefix = SAMPLE_METADATA_PREFIX
        self.repo_url = SAMPLE_REPO_URL

    @task(1)
    def root_and_health(self):
        self.client.get("/", headers=self.headers, name="GET /")
        self.client.get(f"{API_PREFIX}/health", headers=self.headers, name="GET /health")

    @task(2)
    def repo_discovery(self):
        # /repositories and repository-collections
        self.client.get(f"{API_PREFIX}/repositories", headers=self.headers, name="GET /repositories")
        self.client.get(f"{API_PREFIX}/repository-collections/{self.repo_id}", headers=self.headers, name="GET /repository-collections/{id}")

    @task(3)
    def repo_metrics_all(self):
        base = API_PREFIX
        # repo endpoints
        self.client.get(f"{base}/repo/{self.repo_id}", headers=self.headers, name="GET /repo/{id}")
        self.client.get(f"{base}/repos", headers=self.headers, name="GET /repos")

        # counts
        self.client.get(f"{base}/dataset/count", headers=self.headers, name="GET /dataset/count")
        self.client.get(f"{base}/file-metadata/count", headers=self.headers, name="GET /file-metadata/count")
        self.client.get(f"{base}/dataset/count/{self.repo_id}", headers=self.headers, name="GET /dataset/count/{id}")
        self.client.get(f"{base}/file-metadata/count/{self.repo_id}", headers=self.headers, name="GET /file-metadata/count/{id}")

        # status endpoints (use sample statuses)
        for status in HARVEST_STATUSES:
            self.client.get(f"{base}/dataset/count/{self.repo_id}/status/{status}", headers=self.headers, name=f"GET /dataset/count/{self.repo_id}/status/{status}")
            self.client.get(f"{base}/dataset/count/{self.repo_id}/file-metadata/{status}", headers=self.headers, name=f"GET /dataset/count/{self.repo_id}/file-metadata/{status}")

        # grouped endpoints
        self.client.get(f"{base}/file-metadata/count/grouped/mime_type", headers=self.headers, name="GET /file-metadata/count/grouped/mime_type")
        self.client.get(f"{base}/file-metadata/count/grouped/mime_type/{self.repo_id}", headers=self.headers, name="GET /file-metadata/count/grouped/mime_type/{id}")
        self.client.get(f"{base}/dataset/count/grouped-by-publication/month/{self.repo_id}", headers=self.headers, name="GET /dataset/count/grouped-by-publication/month/{id}")
        self.client.get(f"{base}/dataset/count/grouped/repo", headers=self.headers, name="GET /dataset/count/grouped/repo")
        self.client.get(f"{base}/file-metadata/count/grouped/repo", headers=self.headers, name="GET /file-metadata/count/grouped/repo")
        self.client.get(f"{base}/file-metadata/total-size/{self.repo_id}", headers=self.headers, name="GET /file-metadata/total-size/{id}")
        self.client.get(f"{base}/file-metadata/total-size/grouped/repo", headers=self.headers, name="GET /file-metadata/total-size/grouped/repo")

    @task(2)
    def repo_workflow_controller(self):
        base = API_PREFIX
        # add-repo (may be protected)
        payload = {"name": "locust-test-repo", "url": self.repo_url, "metadata_prefix": self.prefix}
        self.client.post(f"{base}/add-repo", json=payload, headers=self.json_headers, name="POST /add-repo")

        # harvest by repo_id
        self.client.post(f"{base}/harvest/{self.repo_id}", headers=self.headers, name="POST /harvest/{repo_id}")
        # harvest by prefix+url
        encoded_url = urllib.parse.quote(self.repo_url, safe="")
        self.client.post(f"{base}/harvest/{self.prefix}/{encoded_url}", headers=self.headers, name="POST /harvest/{prefix}/{url}")

        # harvest-filemetadata (
        self.client.post(f"{base}/harvest-filemetadata/{self.repo_id}", headers=self.headers, name="POST /harvest-filemetadata/{repo_id}")
        self.client.post(f"{base}/harvest-filemetadata/{self.prefix}/{encoded_url}", headers=self.headers, name="POST /harvest-filemetadata/{prefix}/{url}")

    @task(4)
    def pid_fetcher_all(self):
        if not self.sample_pids:
            return
        base = API_PREFIX
        # single pid, use first sample
        pid = self.sample_pids[0]
        encoded = urllib.parse.quote(pid, safe="")

        self.client.get(f"{base}/{encoded}", headers=self.headers, name="GET /{pid}")
        self.client.get(f"{base}/extensions/{encoded}", headers=self.headers, name="GET /extensions/{pid}")
        self.client.get(f"{base}/repository-info/{encoded}", headers=self.headers, name="GET /repository-info/{pid}")

        # batch GET
        params = [("pid", p) for p in self.sample_pids[:8]]
        qs = "&".join([f"{urllib.parse.quote(k)}={urllib.parse.quote(v, safe='')}" for k, v in params])
        self.client.get(f"{base}/batch/pids?{qs}", headers=self.headers, name="GET /batch/pids")

        # batch POST and poll
        pids = self.sample_pids[:10]
        with self.client.post(f"{base}/batch/pids", json=pids, headers=self.json_headers, name="POST /batch/pids", catch_response=True) as resp:
            if resp.status_code in (200, 202):
                try:
                    data = resp.json()
                except Exception:
                    data = {}
                job_id = data.get("job_id")
                if job_id:
                    # poll a few times
                    for _ in range(3):
                        time.sleep(0.5)
                        self.client.get(f"{base}/jobs/{job_id}", headers=self.headers, name="GET /jobs/{job_id}")
                    self.client.get(f"{base}/jobs/{job_id}/results", headers=self.headers, name="GET /jobs/{job_id}/results")

        # batch extensions endpoints (both plural and singular)
        qs2 = "&".join([f"{urllib.parse.quote('pid')}={urllib.parse.quote(p, safe='')}" for p in self.sample_pids[:6]])
        self.client.get(f"{base}/extensions/batch/pids?{qs2}", headers=self.headers, name="GET /extensions/batch/pids")
        self.client.get(f"{base}/extension/batch/pids?{qs2}", headers=self.headers, name="GET /extension/batch/pids")

    @task(1)
    def not_found(self):
        self.client.get(f"{API_PREFIX}/this-does-not-exist", headers=self.headers, name="GET /404")
