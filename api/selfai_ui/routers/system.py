import os
import re
import time
import platform
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psutil
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from selfai_ui.utils.auth import get_admin_user

router = APIRouter()

# Warm up psutil cpu_percent (first call always returns 0.0)
psutil.cpu_percent()

# Try to connect to Docker and detect compose project
_COMPOSE_PROJECT = None
try:
    import docker as docker_lib

    _docker_client = docker_lib.DockerClient(
        base_url="unix:///var/run/docker.sock", timeout=5
    )
    _docker_client.ping()
    DOCKER_AVAILABLE = True

    # Detect our compose project from the selfUI container's label
    _self_containers = _docker_client.containers.list(
        filters={"name": "selfUI"}
    )
    if _self_containers:
        _COMPOSE_PROJECT = _self_containers[0].labels.get(
            "com.docker.compose.project"
        )
except Exception:
    _docker_client = None
    DOCKER_AVAILABLE = False

# Try to import pynvml for GPU monitoring
try:
    import pynvml

    pynvml.nvmlInit()
    NVML_AVAILABLE = True
except Exception:
    NVML_AVAILABLE = False


def _clean_cpu_model(name: str) -> str:
    """Strip trademark symbols and unnecessary words from CPU model name."""
    name = name.replace("(R)", "").replace("(TM)", "").replace("(tm)", "")
    name = re.sub(r"\bProcessor\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\bCPU\b", "", name)
    name = re.sub(r"\s@\s", " @ ", name)
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name


def _get_cpus() -> list[dict]:
    """Get per-physical-CPU info via /proc/cpuinfo with system-wide usage from psutil."""
    usage = psutil.cpu_percent(percpu=False)

    cpuinfo_path = Path("/proc/cpuinfo")
    if cpuinfo_path.exists():
        text = cpuinfo_path.read_text()
        physical_cpus: dict[str, dict] = {}
        current_phys_id = "0"
        current_model = ""

        for line in text.splitlines():
            if line.startswith("physical id"):
                current_phys_id = line.split(":")[1].strip()
            elif line.startswith("model name"):
                current_model = line.split(":", 1)[1].strip()
            elif line.startswith("core id"):
                if current_phys_id not in physical_cpus:
                    physical_cpus[current_phys_id] = {
                        "model": current_model,
                        "logical_cores": 0,
                    }
                physical_cpus[current_phys_id]["logical_cores"] += 1

        if physical_cpus:
            result = []
            for idx, (phys_id, info) in enumerate(sorted(physical_cpus.items())):
                result.append(
                    {
                        "index": idx,
                        "model": _clean_cpu_model(info["model"]),
                        "cores": info["logical_cores"],
                        "usage_percent": round(usage, 1),
                    }
                )
            return result

    model = _clean_cpu_model(platform.processor() or "Unknown CPU")
    return [
        {
            "index": 0,
            "model": model,
            "cores": psutil.cpu_count(logical=True) or 1,
            "usage_percent": round(usage, 1),
        }
    ]


def _get_gpus() -> list[dict]:
    """Get GPU info via pynvml."""
    if not NVML_AVAILABLE:
        return []

    gpus = []
    try:
        device_count = pynvml.nvmlDeviceGetCount()
        for i in range(device_count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8")

            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                gpu_util = float(util.gpu)
            except Exception:
                gpu_util = 0.0

            try:
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                vram_used = mem_info.used / (1024 * 1024)
                vram_total = mem_info.total / (1024 * 1024)
            except Exception:
                vram_used = 0.0
                vram_total = 0.0

            gpus.append(
                {
                    "index": i,
                    "name": name,
                    "utilization": round(gpu_util, 1),
                    "vram_used_mb": round(vram_used, 1),
                    "vram_total_mb": round(vram_total, 1),
                }
            )
    except Exception:
        pass

    return gpus


def _get_gpu_processes() -> dict[int, float]:
    """Get per-process VRAM usage (in MB) keyed by PID."""
    if not NVML_AVAILABLE:
        return {}

    pid_vram: dict[int, float] = {}
    try:
        device_count = pynvml.nvmlDeviceGetCount()
        for i in range(device_count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            try:
                procs = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
            except Exception:
                procs = []
            try:
                gfx_procs = pynvml.nvmlDeviceGetGraphicsRunningProcesses(handle)
            except Exception:
                gfx_procs = []

            for p in list(procs) + list(gfx_procs):
                vram_mb = (p.usedGpuMemory or 0) / (1024 * 1024)
                pid_vram[p.pid] = pid_vram.get(p.pid, 0) + vram_mb
    except Exception:
        pass

    return pid_vram


############################
# System Resources
############################


class CpuInfo(BaseModel):
    index: int
    model: str
    cores: int
    usage_percent: float


class GpuInfo(BaseModel):
    index: int
    name: str
    utilization: float
    vram_used_mb: float
    vram_total_mb: float


class MemoryInfo(BaseModel):
    used_mb: float
    total_mb: float
    percent: float


class SystemResources(BaseModel):
    cpus: list[CpuInfo]
    gpus: list[GpuInfo]
    memory: MemoryInfo
    gpu_vram_total_mb: float
    gpu_vram_used_mb: float
    container_time: str
    timestamp: float


@router.get("/resources", response_model=SystemResources)
async def get_system_resources(user=Depends(get_admin_user)):
    cpus = _get_cpus()
    gpus = _get_gpus()
    mem = psutil.virtual_memory()

    gpu_vram_total = sum(g["vram_total_mb"] for g in gpus)
    gpu_vram_used = sum(g["vram_used_mb"] for g in gpus)

    tz_name = os.environ.get("TIMEZONE", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
        tz_name = "UTC"

    now = datetime.now(tz)
    tz_abbr = now.strftime("%Z")  # e.g. "PDT", "PST", "UTC"

    return SystemResources(
        cpus=[CpuInfo(**c) for c in cpus],
        gpus=[GpuInfo(**g) for g in gpus],
        memory=MemoryInfo(
            used_mb=round(mem.used / (1024 * 1024), 1),
            total_mb=round(mem.total / (1024 * 1024), 1),
            percent=round(mem.percent, 1),
        ),
        gpu_vram_total_mb=round(gpu_vram_total, 1),
        gpu_vram_used_mb=round(gpu_vram_used, 1),
        container_time=now.strftime(f"%Y-%m-%d %H:%M:%S {tz_abbr}"),
        timestamp=time.time(),
    )


############################
# System Processes
############################


class ProcessInfo(BaseModel):
    pid: int
    name: str
    container: str
    cpu_percent: float
    memory_mb: float
    vram_mb: float


class ProcessList(BaseModel):
    processes: list[ProcessInfo]
    timestamp: float


def _find_col(titles: list[str], name: str):
    """Find column index by name in ps output headers."""
    for i, t in enumerate(titles):
        if name in t:
            return i
    return None


def _get_docker_processes() -> list[dict]:
    """Get processes from all running containers via docker top."""
    gpu_procs = _get_gpu_processes()
    processes = []

    try:
        filters = {}
        if _COMPOSE_PROJECT:
            filters["label"] = f"com.docker.compose.project={_COMPOSE_PROJECT}"
        for container in _docker_client.containers.list(filters=filters):
            container_name = container.name or container.short_id
            try:
                top = container.top(ps_args="aux")
                titles = [t.upper() for t in top.get("Titles", [])]
                rows = top.get("Processes", [])

                pid_idx = _find_col(titles, "PID")
                cmd_idx = _find_col(titles, "COMMAND")
                cpu_idx = _find_col(titles, "%CPU")
                rss_idx = _find_col(titles, "RSS")

                for row in rows:
                    try:
                        pid = int(row[pid_idx]) if pid_idx is not None else 0
                        name = row[cmd_idx] if cmd_idx is not None else "unknown"
                        name = (
                            name.split("/")[-1].split(" ")[0]
                            if name
                            else "unknown"
                        )
                        cpu_pct = (
                            float(row[cpu_idx]) if cpu_idx is not None else 0.0
                        )
                        memory_mb = (
                            float(row[rss_idx]) / 1024.0
                            if rss_idx is not None
                            else 0.0
                        )

                        processes.append(
                            {
                                "pid": pid,
                                "name": name,
                                "container": container_name,
                                "cpu_percent": round(cpu_pct, 1),
                                "memory_mb": round(memory_mb, 1),
                                "vram_mb": round(gpu_procs.get(pid, 0.0), 1),
                            }
                        )
                    except (ValueError, IndexError):
                        continue
            except Exception:
                continue
    except Exception:
        pass

    return processes


def _get_local_processes() -> list[dict]:
    """Fallback: get processes via psutil when Docker is not available."""
    gpu_procs = _get_gpu_processes()
    processes = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
        try:
            info = proc.info
            pid = info["pid"]
            mem_info = info.get("memory_info")
            memory_mb = (mem_info.rss / (1024 * 1024)) if mem_info else 0.0
            processes.append(
                {
                    "pid": pid,
                    "name": info.get("name") or "unknown",
                    "container": "local",
                    "cpu_percent": round(info.get("cpu_percent") or 0.0, 1),
                    "memory_mb": round(memory_mb, 1),
                    "vram_mb": round(gpu_procs.get(pid, 0.0), 1),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return processes


@router.get("/processes", response_model=ProcessList)
async def get_system_processes(user=Depends(get_admin_user)):
    if DOCKER_AVAILABLE and _docker_client:
        proc_list = _get_docker_processes()
    else:
        proc_list = _get_local_processes()

    return ProcessList(
        processes=[ProcessInfo(**p) for p in proc_list],
        timestamp=time.time(),
    )
