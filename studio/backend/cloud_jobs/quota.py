"""Does the connected account have quota to launch this GPU machine, region by region?

AWS: EC2 vCPU quotas per instance family (Service Quotas), minus the vCPUs already running in that family.
GCP: the region's GPU quota for this GPU (preemptible variant for spot) with its usage, plus GPUS_ALL_REGIONS.
"""
from concurrent.futures import ThreadPoolExecutor

from cloud_jobs import credentials

# EC2 quota codes, in vCPUs. Families outside these (Inferentia/Trainium) are not trainable here anyway.
AWS_QUOTA_CODES = {  # family letter -> (on-demand, spot)
    "g": ("L-DB2E81BA", "L-3819A6DF"),  # G and VT
    "p": ("L-417A185B", "L-7212CCBC"),  # P
}
GCP_GPU_METRIC = {  # SkyPilot accelerator name -> Compute Engine quota metric
    "T4": "NVIDIA_T4_GPUS", "P4": "NVIDIA_P4_GPUS", "V100": "NVIDIA_V100_GPUS", "L4": "NVIDIA_L4_GPUS",
    "A100": "NVIDIA_A100_GPUS", "A100-80GB": "NVIDIA_A100_80GB_GPUS", "H100": "NVIDIA_H100_GPUS",
    "H100-MEGA": "NVIDIA_H100_MEGA_GPUS", "H200": "NVIDIA_H200_GPUS", "B200": "NVIDIA_B200_GPUS",
    "RTXPRO6000": "NVIDIA_RTX_PRO_6000_GPUS",
}


def _aws_region(region: str, instance_type: str, need_vcpus: int, use_spot: bool) -> dict:
    from botocore.exceptions import BotoCoreError, ClientError

    family = instance_type[:1].lower()
    codes = AWS_QUOTA_CODES.get(family)
    if codes is None:
        return {"region": region, "status": "unknown", "detail": f"No quota mapping for {instance_type}"}
    code = codes[1 if use_spot else 0]
    url = f"https://{region}.console.aws.amazon.com/servicequotas/home/services/ec2/quotas/{code}"
    try:
        session = credentials.aws_session(region)
        sq = session.client("service-quotas")
        try:
            limit = sq.get_service_quota(ServiceCode = "ec2", QuotaCode = code)["Quota"]["Value"]
        except sq.exceptions.NoSuchResourceException:
            limit = sq.get_aws_default_service_quota(ServiceCode = "ec2", QuotaCode = code)["Quota"]["Value"]
        ec2 = session.client("ec2")
        used, types = 0, {}
        for page in ec2.get_paginator("describe_instances").paginate(
                Filters = [{"Name": "instance-state-name", "Values": ["pending", "running"]}]):
            for res in page["Reservations"]:
                for inst in res["Instances"]:
                    t = inst["InstanceType"]
                    if t[:1].lower() != family or (inst.get("InstanceLifecycle") == "spot") != use_spot:
                        continue
                    if t not in types:
                        types[t] = ec2.describe_instance_types(InstanceTypes = [t])["InstanceTypes"][0][
                            "VCpuInfo"]["DefaultVCpus"]
                    used += types[t]
    except (ClientError, BotoCoreError) as e:
        return {"region": region, "status": "error", "detail": str(e), "url": url}
    free = limit - used
    return {"region": region, "status": "ok" if free >= need_vcpus else "short", "unit": "vCPUs",
            "limit": limit, "used": used, "need": need_vcpus, "url": url}


def aws(instance_type: str, vcpus: int, regions: list[str], use_spot: bool) -> list[dict]:
    with ThreadPoolExecutor(max_workers = 8) as pool:
        return list(pool.map(lambda r: _aws_region(r, instance_type, vcpus, use_spot), regions))


def _norm(s: str) -> str:
    return "".join(c for c in s.upper() if c.isalnum())


def _gcp_family_limits(creds, project: str, accelerator: str, use_spot: bool) -> dict:
    """Per-region limits of Compute's per-GPU-family quota, via the Cloud Quotas API.
    {"limits": {region: n}, "default": n} or {"error": ..., "url": ...} (e.g. the API is not enabled)."""
    from google.auth.transport.requests import AuthorizedSession

    qid = ("PREEMPTIBLE-" if use_spot else "") + "GPUS-PER-GPU-FAMILY-per-project-region"
    r = AuthorizedSession(creds).get(f"https://cloudquotas.googleapis.com/v1/projects/{project}/locations/global"
                                     f"/services/compute.googleapis.com/quotaInfos/{qid}", timeout = 60)
    if r.status_code == 403 and "SERVICE_DISABLED" in r.text:
        return {"error": "Turn on the Cloud Quotas API for this project to see quotas for this GPU",
                "url": f"https://console.cloud.google.com/apis/library/cloudquotas.googleapis.com?project={project}"}
    if not r.ok:
        return {"error": f"Cloud Quotas API: {r.status_code} {r.text[:200]}"}
    return {"infos": r.json().get("dimensionsInfos", [])}


def family_limit(infos: list[dict], accelerator: str, region: str) -> int:
    """Most specific match wins: family+region, region-wide, family-wide, then the project default."""
    want, best = "NVIDIA" + _norm(accelerator), {}
    for info in infos:
        dims = info.get("dimensions", {})
        fam, reg = dims.get("gpu_family"), dims.get("region")
        if (fam is not None and _norm(fam) != want) or (reg is not None and reg != region):
            continue  # exact family: H100 must not match H100_MEGA
        best[(fam is not None, reg is not None)] = int(info.get("details", {}).get("value", 0))
    for key in ((True, True), (False, True), (True, False), (False, False)):
        if key in best:
            return best[key]
    return 0


def gcp(accelerator: str, count: int, regions: list[str], use_spot: bool) -> dict:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    creds, project = credentials.gcp_credentials()
    base = GCP_GPU_METRIC.get(accelerator) or f"NVIDIA_{accelerator.upper().replace('-', '_')}_GPUS"
    metric = f"PREEMPTIBLE_{base}" if use_spot else base
    url = f"https://console.cloud.google.com/iam-admin/quotas?project={project}"
    compute = build("compute", "v1", credentials = creds, cache_discovery = False)
    try:
        project_quotas = {q["metric"]: q for q in compute.projects().get(project = project).execute().get("quotas", [])}
        by_region, req = {}, compute.regions().list(project = project)
        while req is not None:  # one paged call returns every region's quotas
            resp = req.execute()
            for item in resp.get("items", []):
                by_region[item["name"]] = {q["metric"]: q for q in item.get("quotas", [])}
            req = compute.regions().list_next(req, resp)
        family = None  # newer GPUs (H100, H200, B200, RTX PRO 6000...) only have a per-GPU-family quota
        if not any(metric in m for m in by_region.values()):
            family = _gcp_family_limits(creds, project, accelerator, use_spot)
        out = []
        for region in regions:
            q = by_region.get(region, {}).get(metric)
            if q is not None:
                free = q["limit"] - q.get("usage", 0)
                out.append({"region": region, "status": "ok" if free >= count else "short", "unit": "GPUs",
                            "limit": q["limit"], "used": q.get("usage", 0), "need": count, "url": url})
            elif family is not None and "error" in family:
                out.append({"region": region, "status": "unknown", "detail": family["error"],
                            "url": family.get("url", url)})
            elif family is not None:
                limit = family_limit(family["infos"], accelerator, region)  # family quotas report no usage
                out.append({"region": region, "status": "ok" if limit >= count else "short", "unit": "GPUs",
                            "limit": limit, "used": None, "need": count, "url": url})
            else:
                out.append({"region": region, "status": "short", "unit": "GPUs", "limit": 0, "used": 0,
                            "need": count, "detail": f"{metric} is not available in this region", "url": url})
    except HttpError as e:
        return {"regions": [{"region": r, "status": "error", "detail": str(e), "url": url} for r in regions],
                "global": None, "metric": metric}
    g = project_quotas.get("GPUS_ALL_REGIONS")
    glob = None if g is None else {"limit": g["limit"], "used": g.get("usage", 0), "need": count,
                                   "status": "ok" if g["limit"] - g.get("usage", 0) >= count else "short"}
    return {"regions": out, "global": glob, "metric": metric}


def _selftest():
    infos = [
        {"dimensions": {}, "details": {"value": "4"}},  # project default
        {"dimensions": {"gpu_family": "NVIDIA_H100"}, "details": {"value": "8"}},
        {"dimensions": {"region": "us-east1"}, "details": {"value": "2"}},
        {"dimensions": {"region": "us-east1", "gpu_family": "NVIDIA_H100"}, "details": {"value": "16"}},
        {"dimensions": {"region": "us-west1", "gpu_family": "NVIDIA_H100_MEGA"}, "details": {"value": "99"}},
    ]
    assert family_limit(infos, "H100", "us-east1") == 16  # family + region
    assert family_limit(infos, "H100", "us-west1") == 8  # family-wide; H100_MEGA must not match H100
    assert family_limit(infos, "H100-MEGA", "us-west1") == 99
    assert family_limit(infos, "L4", "us-east1") == 2  # region-wide
    assert family_limit(infos, "L4", "asia-south1") == 4  # project default
    assert family_limit(infos, "RTXPRO6000", "x") == 4 and family_limit([], "H100", "x") == 0
    assert _norm("NVIDIA_RTX_PRO_6000") == "NVIDIA" + _norm("RTXPRO6000")


if __name__ == "__main__":
    _selftest()
    print("quota selftest ok")
