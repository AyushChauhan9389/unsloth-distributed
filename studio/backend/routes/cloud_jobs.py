"""Cloud jobs API: launch/schedule Studio training on cloud GPUs and watch it through the usual training views.

The per-job status/metrics/progress/stop endpoints call the existing training routes with that job's
backend in place (core.training.training.training_backend_override), so they return the same shapes.
"""
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth.authentication import authenticated_via_api_key, get_current_subject
from auth.policy import OwnerOnly
from cloud_jobs import store
from cloud_jobs.service import cloud_jobs
from core.training.training import training_backend_override
from models.training import TrainingStartRequest
from routes import training as training_routes

router = APIRouter()


class Resources(BaseModel):
    launcher: Literal["local", "skypilot"] = "skypilot"
    infra: Literal["gcp", "aws", "kubernetes"] = "gcp"
    regions: list[str] = Field(default_factory = list)  # tried in order; empty = anywhere the cloud has capacity
    accelerator: str = "H100:1"
    use_spot: bool = True
    disk_size: int = Field(200, ge = 50, le = 4000)
    max_hours: float = Field(12, gt = 0, le = 72)  # hard runtime cap: the runner stops (and saves) at the deadline


class CloudJobCreate(BaseModel):
    name: Optional[str] = Field(None, max_length = 120)
    request: TrainingStartRequest
    resources: Resources = Field(default_factory = Resources)
    schedule: Optional[dict[str, Any]] = None  # Studio's schedule JSON: {"kind": "once"|"hourly"|"daily"|...}
    timezone: str = "UTC"


def _public(job: dict) -> dict:
    """A job as the UI sees it: no secrets, no worker config; plus a live progress summary while running."""
    out = {k: v for k, v in job.items() if k not in ("secrets", "config")}
    backend = cloud_jobs.backend(job["id"])
    if backend is not None:
        p = backend._progress
        out["live"] = {"step": p.step, "total_steps": p.total_steps, "loss": p.loss, "epoch": p.epoch,
                       "eta_seconds": p.eta_seconds, "status_message": p.status_message, "error": p.error}
    return out


def _job_or_404(job_id: str) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "Cloud job not found")
    return job


def _use_job_backend(job_id: str) -> None:
    """Point the existing training routes at this job for the rest of this request (incl. its SSE stream)."""
    _job_or_404(job_id)
    backend = cloud_jobs.backend(job_id)
    if backend is None:
        raise HTTPException(409, "This job is not running on this server (not started yet, or finished before a restart)")
    training_backend_override.set(backend)


@router.post("")
async def create_cloud_job(body: CloudJobCreate, current_subject: str = Depends(get_current_subject),
                           via_api_key: bool = Depends(authenticated_via_api_key)):
    try:
        await training_routes._prepare_training_request(body.request, via_api_key)
        admitted = await training_routes._admit_training_request(body.request, current_subject, via_api_key)
        job = cloud_jobs.create(name = body.name, request = body.request.model_dump(mode = "json"),
                                training_kwargs = admitted.training_kwargs, resources = body.resources.model_dump(),
                                schedule = body.schedule, timezone = body.timezone)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _public(job)


class EstimateBody(BaseModel):
    request: TrainingStartRequest
    infra: Literal["gcp", "aws", "kubernetes"] = "gcp"
    use_spot: bool = True
    gpu_count: Literal[1, 2, 4, 8] = 1


@router.post("/estimate")
async def estimate_cloud_gpu(body: EstimateBody, current_subject: str = Depends(get_current_subject)):
    """VRAM this config needs (per GPU), the cloud's machines with `gpu_count` GPUs and their prices, and the cheapest that fits."""
    import asyncio

    from cloud_jobs import gpu_advisor
    from cloud_jobs.skypilot_launcher import SkyPilotLauncher

    req = body.request.model_dump()
    required, meta = await asyncio.to_thread(gpu_advisor.required_vram_gb, req)
    options, others, catalog_error = [], {}, None
    if body.infra != "kubernetes":  # a cluster's GPUs aren't in the public catalog
        url = SkyPilotLauncher().url
        try:
            options = await asyncio.to_thread(gpu_advisor.catalog, url, body.infra, body.gpu_count)
            others = await asyncio.to_thread(gpu_advisor.other_counts, url, body.infra, body.gpu_count)
        except Exception as e:
            catalog_error = f"GPU catalog unavailable: {e}"
    ranked, pick = gpu_advisor.suggest(required, options, body.use_spot)
    return {"required_gb": required, "estimate": meta, "options": ranked, "suggested": pick,
            "other_counts": others, "headroom": gpu_advisor.HEADROOM, "catalog_error": catalog_error}


@router.get("")
async def list_cloud_jobs(current_subject: str = Depends(get_current_subject)):
    return [_public(j) for j in store.list_jobs()]


# ---- cloud accounts (owner only; secrets are write-only) and quota
class AwsConnect(BaseModel):
    access_key_id: str = Field(min_length = 16, max_length = 128)
    secret_access_key: str = Field(min_length = 16, max_length = 256)
    region: str = Field("us-east-1", pattern = r"^[a-z]{2}(-[a-z]+)+-\d$")
    role_arn: Optional[str] = Field(None, pattern = r"^(arn:aws[\w-]*:iam::\d{12}:role/.+)?$")


class GcpConnect(BaseModel):
    key_json: str = Field(min_length = 100, max_length = 20000)


@router.get("/clouds", dependencies = [OwnerOnly])
async def cloud_accounts(current_subject: str = Depends(get_current_subject)):
    from cloud_jobs import credentials
    return credentials.status()


@router.put("/clouds/aws", dependencies = [OwnerOnly])
async def connect_aws(body: AwsConnect, current_subject: str = Depends(get_current_subject)):
    import asyncio

    from cloud_jobs import credentials
    try:
        return await asyncio.to_thread(credentials.connect_aws, body.access_key_id, body.secret_access_key,
                                       body.region, body.role_arn)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/clouds/gcp", dependencies = [OwnerOnly])
async def connect_gcp(body: GcpConnect, current_subject: str = Depends(get_current_subject)):
    import asyncio

    from cloud_jobs import credentials
    try:
        return await asyncio.to_thread(credentials.connect_gcp, body.key_json)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/clouds/{cloud}", dependencies = [OwnerOnly])
async def disconnect_cloud(cloud: Literal["aws", "gcp"], current_subject: str = Depends(get_current_subject)):
    from cloud_jobs import credentials
    credentials.disconnect(cloud)
    return credentials.status()


class QuotaBody(BaseModel):
    infra: Literal["gcp", "aws"]
    accelerator: str = Field(max_length = 64)
    gpu_count: int = Field(1, ge = 1, le = 16)
    use_spot: bool = True
    instance_type: str = Field("", max_length = 64)  # AWS: quota is per instance family, in vCPUs
    vcpus: int = Field(0, ge = 0, le = 4096)
    regions: list[str] = Field(max_length = 64)


@router.post("/quota")
async def cloud_quota(body: QuotaBody, current_subject: str = Depends(get_current_subject)):
    """Per region: enough quota for this machine on the connected account? 409 when the cloud is not connected."""
    import asyncio

    from cloud_jobs import quota
    try:
        if body.infra == "aws":
            regions = await asyncio.to_thread(quota.aws, body.instance_type, body.vcpus, body.regions, body.use_spot)
            return {"regions": regions, "global": None}
        return await asyncio.to_thread(quota.gcp, body.accelerator, body.gpu_count, body.regions, body.use_spot)
    except LookupError as e:
        raise HTTPException(409, str(e))


@router.get("/{job_id}")
async def get_cloud_job(job_id: str, current_subject: str = Depends(get_current_subject)):
    return _public(_job_or_404(job_id))


@router.post("/{job_id}/cancel")
async def cancel_cloud_job(job_id: str, current_subject: str = Depends(get_current_subject)):
    _job_or_404(job_id)
    cloud_jobs.cancel(job_id)
    return _public(store.get(job_id))


@router.post("/{job_id}/rerun")
async def rerun_cloud_job(job_id: str, current_subject: str = Depends(get_current_subject)):
    _job_or_404(job_id)
    return _public(cloud_jobs.rerun(job_id))


@router.get("/{job_id}/logs")
async def cloud_job_logs(job_id: str, tail: int = 200, current_subject: str = Depends(get_current_subject)):
    job = _job_or_404(job_id)
    return {"logs": cloud_jobs.launcher(job).logs(job, cloud_jobs.folder(job), min(max(tail, 1), 5000))}


# ---- the existing training views, scoped to one cloud job
@router.get("/{job_id}/status")
async def cloud_job_status(job_id: str, current_subject: str = Depends(get_current_subject),
                           via_api_key: bool = Depends(authenticated_via_api_key)):
    _use_job_backend(job_id)
    return await training_routes.get_training_status(current_subject = current_subject, via_api_key = via_api_key)


@router.get("/{job_id}/metrics")
async def cloud_job_metrics(job_id: str, current_subject: str = Depends(get_current_subject)):
    _use_job_backend(job_id)
    return await training_routes.get_training_metrics(expected_job_id = job_id, current_subject = current_subject)


@router.post("/{job_id}/progress")
@router.get("/{job_id}/progress", include_in_schema = False)
async def cloud_job_progress(job_id: str, request: Request, current_subject: str = Depends(get_current_subject)):
    _use_job_backend(job_id)
    return await training_routes.stream_training_progress(request = request, expected_job_id = job_id,
                                                          current_subject = current_subject)


@router.post("/{job_id}/stop")
async def cloud_job_stop(job_id: str, body: training_routes.TrainingStopRequest,
                         current_subject: str = Depends(get_current_subject)):
    _use_job_backend(job_id)
    body.expected_job_id = job_id
    return await training_routes.stop_training(body = body, current_subject = current_subject)
