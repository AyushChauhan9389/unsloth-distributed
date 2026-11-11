"""Suggest the cheapest cloud GPU that fits a training config.

VRAM need: Studio's own training estimator (utils.hardware.estimate_required_model_memory_gb), which sizes
the model from its Hugging Face config, so it works on a server without a GPU.
Options and prices: SkyPilot's catalog for the chosen cloud (single-GPU instances, cheapest region).
"""
import json
import time
import urllib.request

# GPU memory per SkyPilot accelerator name (the catalog lists prices, not memory sizes).
GPU_MEMORY_GB = {
    "P4": 8, "T4": 16, "V100": 16, "V100-32GB": 32, "L4": 24, "A10G": 24, "A10": 24, "RTX4090": 24,
    "L40": 48, "L40S": 48, "A6000": 48, "A40": 48, "A100": 40, "A100-80GB": 80, "H100": 80, "H100-MEGA": 80,
    "H200": 141, "RTXPRO6000": 96, "B200": 180, "B300": 288, "GH200": 96,
}
HEADROOM = 1.15  # leave 15% for fragmentation and activation spikes
_CACHE_SECONDS = 3600
_catalog_cache: dict[str, tuple[float, list]] = {}


def required_vram_gb(request: dict) -> tuple[float | None, dict]:
    from utils.hardware import estimate_required_model_memory_gb

    full = request.get("training_type") == "Full Finetuning"
    return estimate_required_model_memory_gb(
        request["model_name"], hf_token = request.get("hf_token") or None,
        training_type = request.get("training_type") or "LoRA/QLoRA",
        load_in_4bit = False if full else bool(request.get("load_in_4bit", True)),
        batch_size = int(request.get("batch_size") or 2), max_seq_length = int(request.get("max_seq_length") or 2048),
        lora_rank = int(request.get("lora_r") or 16), target_modules = request.get("target_modules"),
        gradient_checkpointing = request.get("gradient_checkpointing") or "unsloth",
        optimizer = request.get("optim") or "adamw_8bit")


def catalog(sky_url: str, infra: str) -> list[dict]:
    """[{accelerator, memory_gb, vcpus, ram_gb, price, spot_price, region, instance_type, regions:[...]}], 1 GPU each;
    the top-level price/region is the cheapest region, `regions` lists every region with its own prices."""
    hit = _catalog_cache.get(infra)
    if hit and time.time() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    body = json.dumps({"gpus_only": True, "clouds": [infra], "require_price": True, "quantity_filter": 1,
                       "all_regions": True}).encode()
    req = urllib.request.Request(f"{sky_url}/list_accelerators", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout = 60) as r:
        rid = r.headers.get("X-Skypilot-Request-ID")
    with urllib.request.urlopen(f"{sky_url}/api/get?request_id={rid}", timeout = 300) as r:
        rv = json.load(r)["return_value"]
    rv = json.loads(rv) if isinstance(rv, str) else rv
    out = []
    for name, rows in rv.items():
        mem = GPU_MEMORY_GB.get(name)
        # row: [cloud, instance_type, accelerator, count, vcpus, device_memory, cpu_memory, price, spot_price, region]
        rows = [r for r in rows if r[3] == 1 and r[7] is not None]
        if mem is None or not rows:
            continue
        best = min(rows, key = lambda r: r[7])
        spot = min((r[8] for r in rows if r[8] is not None), default = None)
        regions = {}  # cheapest 1-GPU machine per region
        for r in rows:
            cur = regions.get(r[9])
            if cur is None or r[7] < cur["price"]:
                regions[r[9]] = {"region": r[9], "price": r[7], "spot_price": r[8], "instance_type": r[1]}
        out.append({"accelerator": name, "memory_gb": mem, "price": best[7], "spot_price": spot,
                    "region": best[9], "instance_type": best[1], "vcpus": best[4], "ram_gb": best[6],
                    "regions": sorted(regions.values(), key = lambda x: x["price"])})
    _catalog_cache[infra] = (time.time(), out)
    return out


def suggest(required_gb: float | None, options: list[dict], use_spot: bool) -> tuple[list[dict], str | None]:
    """Options sorted by hourly price with a fits flag; the suggestion is the cheapest that fits."""
    def hourly(o):
        return (o["spot_price"] if use_spot and o["spot_price"] is not None else o["price"])

    ranked = sorted(({**o, "hourly": hourly(o), "fits": required_gb is not None and o["memory_gb"] >= required_gb * HEADROOM}
                     for o in options), key = lambda o: o["hourly"])
    pick = next((o["accelerator"] for o in ranked if o["fits"]), None)
    return ranked, pick


def _selftest():
    opts = [{"accelerator": "L4", "memory_gb": 24, "price": 0.85, "spot_price": 0.5, "region": "r", "instance_type": "g"},
            {"accelerator": "H100", "memory_gb": 80, "price": 11.0, "spot_price": 5.5, "region": "r", "instance_type": "a"},
            {"accelerator": "A100-80GB", "memory_gb": 80, "price": 5.0, "spot_price": 1.8, "region": "r", "instance_type": "a2"}]
    ranked, pick = suggest(30, opts, use_spot = True)
    assert pick == "A100-80GB" and [o["fits"] for o in ranked] == [False, True, True]
    assert suggest(None, opts, True)[1] is None and suggest(20, opts, False)[1] == "L4"


_selftest()
