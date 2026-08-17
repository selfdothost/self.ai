import os
import platform
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import psutil
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from selfai_ui.models.vram_leases import VramLeases
from selfai_ui.utils.auth import get_admin_user

router = APIRouter()

# Warm up psutil cpu_percent (first call always returns 0.0)
psutil.cpu_percent()

# Try to connect to Docker and detect compose project
_COMPOSE_PROJECT = None
try:
    import docker as docker_lib

    _docker_client = docker_lib.DockerClient(base_url="unix:///var/run/docker.sock", timeout=5)
    _docker_client.ping()
    DOCKER_AVAILABLE = True

    # Detect our compose project from the selfUI container's label
    _self_containers = _docker_client.containers.list(filters={"name": "selfUI"})
    if _self_containers:
        _COMPOSE_PROJECT = _self_containers[0].labels.get("com.docker.compose.project")
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


############################
# Kubernetes namespace-quota awareness (self.chat#24)
############################
#
# Reading /proc/cpuinfo or psutil.virtual_memory() inside a k8s pod describes
# the whole NODE, not what this pod is actually allotted. On the yard, selfai-api
# runs with a 2-core / 2Gi limit on a many-core host -- the Admin System page
# was reporting the host's full thread count, which is not what an admin looking
# at "their" pod's headroom wants to know. When running in Kubernetes, prefer
# the pod's own cgroup v2 quota/usage; fall back to the host-wide reading
# (docker-compose / bare-metal dev) when no quota is set or cgroup v1 is in use.

# Overridable so tests can point this at a fake cgroup tree.
CGROUP_ROOT = Path(os.environ.get("SELFAI_CGROUP_ROOT", "/sys/fs/cgroup"))


# Overridable so tests can point this at a path that deliberately doesn't exist.
KUBERNETES_SERVICEACCOUNT_PATH = Path(
    os.environ.get(
        "SELFAI_K8S_SERVICEACCOUNT_PATH", "/var/run/secrets/kubernetes.io/serviceaccount"
    )
)


def _running_in_kubernetes() -> bool:
    """True when this process is running inside a Kubernetes pod."""
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    return KUBERNETES_SERVICEACCOUNT_PATH.exists()


IN_KUBERNETES = _running_in_kubernetes()


def _cgroup_cpu_quota_cores() -> Optional[float]:
    """This pod's CPU budget in cores, from cgroup v2 ``cpu.max`` ("<quota> <period>"
    in microseconds, or "max <period>" for no limit). ``None`` when unreadable, not two
    fields, non-integer, or unlimited -- the caller falls back to the host-wide reading
    rather than reporting nothing."""
    try:
        text = (CGROUP_ROOT / "cpu.max").read_text().strip()
    except OSError:
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    quota_str, period_str = parts
    if quota_str == "max":
        return None
    try:
        quota = int(quota_str)
        period = int(period_str)
    except ValueError:
        return None
    if period <= 0:
        return None
    return quota / period


def _cgroup_cpu_usage_usec() -> Optional[int]:
    """Cumulative CPU time (microseconds, summed across all cores) this cgroup has
    consumed, from cgroup v2 ``cpu.stat``'s ``usage_usec`` line. ``None`` when
    unreadable or absent."""
    try:
        text = (CGROUP_ROOT / "cpu.stat").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("usage_usec"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


# Warm-up baseline for the cgroup usage delta, mirroring psutil.cpu_percent()'s own
# warm-up above -- the first real reading needs a prior sample to diff against.
_cgroup_cpu_state = {"usage_usec": _cgroup_cpu_usage_usec(), "at": time.monotonic()}


def _cgroup_cpu_usage_percent(quota_cores: float) -> float:
    """This pod's CPU usage as a percent of ITS OWN quota (100% = using the full
    ``quota_cores`` budget), not the host's. Never negative, never above 100."""
    now = time.monotonic()
    usage = _cgroup_cpu_usage_usec()
    prev_usage = _cgroup_cpu_state["usage_usec"]
    prev_at = _cgroup_cpu_state["at"]
    _cgroup_cpu_state["usage_usec"] = usage
    _cgroup_cpu_state["at"] = now

    if usage is None or prev_usage is None or quota_cores <= 0:
        return 0.0
    elapsed = now - prev_at
    if elapsed <= 0:
        return 0.0

    delta_seconds = (usage - prev_usage) / 1_000_000
    percent = (delta_seconds / (elapsed * quota_cores)) * 100
    return max(0.0, min(100.0, round(percent, 1)))


def _cgroup_memory_usage_bytes() -> Optional[tuple]:
    """``(used_bytes, limit_bytes)`` from cgroup v2 ``memory.current``/``memory.max``,
    or ``None`` when unreadable or no limit is set (``memory.max`` reads "max")."""
    try:
        current = int((CGROUP_ROOT / "memory.current").read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        max_text = (CGROUP_ROOT / "memory.max").read_text().strip()
    except OSError:
        return None
    if max_text == "max":
        return None
    try:
        limit = int(max_text)
    except ValueError:
        return None
    return (current, limit)


def _clean_cpu_model(name: str) -> str:
    """Strip trademark symbols and unnecessary words from CPU model name."""
    name = name.replace("(R)", "").replace("(TM)", "").replace("(tm)", "")
    name = re.sub(r"\bProcessor\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\bCPU\b", "", name)
    name = re.sub(r"\s@\s", " @ ", name)
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name


def _get_cpus() -> list[dict]:
    """Get per-physical-CPU info via /proc/cpuinfo with system-wide usage from psutil.

    In Kubernetes, report the pod's OWN cgroup CPU quota and usage instead
    (self.chat#24) -- /proc/cpuinfo and psutil.cpu_percent() describe the whole
    node, not what this pod is actually allotted. Falls through to the
    host-wide reading below when no quota is set (cgroup v1, or an
    unconstrained pod)."""
    if IN_KUBERNETES:
        quota_cores = _cgroup_cpu_quota_cores()
        if quota_cores is not None:
            model = _clean_cpu_model(platform.processor() or "Unknown CPU")
            return [
                {
                    "index": 0,
                    "model": f"{model} (namespace quota)",
                    "cores": round(quota_cores, 2),
                    "usage_percent": _cgroup_cpu_usage_percent(quota_cores),
                }
            ]

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


def _get_gpus_from_vram_registry() -> list[dict]:
    """GPU info derived from the VRAM lease registry (self.chat#25).

    selfai-api has no GPU of its own in Kubernetes -- the actual GPU-holding
    pods (self.llamolotl, self.speak, self.sketch, self.curator) self-report
    their VRAM into the shared lease registry, which is the only place this
    pod can see the card from. This project runs exactly one shared GPU
    (gpu-single-factory-scheduling), so the whole registry describes one
    device (index 0), matching VramLeases.total_capacity()'s own "one physical
    card" modeling choice.

    Compute utilization is NOT tracked here -- only VRAM is self-reported by
    consumers -- so it is reported as 0.0 rather than a fabricated estimate.
    Returns [] when no consumer has ever registered (nothing to report yet,
    e.g. a fresh dev environment)."""
    try:
        summary = VramLeases.capacity_summary()
    except Exception:
        return []

    total_bytes = summary.total_capacity_bytes
    if total_bytes <= 0:
        return []

    occ = summary.device_occupancy
    if occ is not None:
        used_bytes = occ.used_bytes
        total_bytes = occ.total_bytes or total_bytes
    else:
        # No fresh card-level reading -- fall back to the ledger's own held
        # sum. Still real, self-reported data; it just can't see CUDA-context/
        # non-consumer overhead the way a device reading can.
        used_bytes = summary.total_held_bytes

    return [
        {
            "index": 0,
            "name": "Shared GPU (self.ai VRAM lease pool)",
            "utilization": 0.0,
            "vram_used_mb": round(used_bytes / (1024 * 1024), 1),
            "vram_total_mb": round(total_bytes / (1024 * 1024), 1),
        }
    ]


def _get_gpus() -> list[dict]:
    """Get GPU info via pynvml, or from the VRAM lease registry when this pod
    has no direct GPU access (self.chat#25)."""
    if not NVML_AVAILABLE:
        return _get_gpus_from_vram_registry()

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
    # float, not int: a k8s CPU quota can be fractional (e.g. "1500m" -> 1.5).
    cores: float
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


def _get_memory() -> dict:
    """This pod's memory usage. In Kubernetes, prefer cgroup v2
    memory.current/memory.max (self.chat#24's same namespace-quota fix,
    applied to memory) over psutil.virtual_memory(), which reports the whole
    node. Falls back to the host-wide reading when no memory limit is set."""
    if IN_KUBERNETES:
        cg = _cgroup_memory_usage_bytes()
        if cg is not None:
            used, total = cg
            percent = (used / total * 100) if total > 0 else 0.0
            return {
                "used_mb": round(used / (1024 * 1024), 1),
                "total_mb": round(total / (1024 * 1024), 1),
                "percent": round(percent, 1),
            }

    mem = psutil.virtual_memory()
    return {
        "used_mb": round(mem.used / (1024 * 1024), 1),
        "total_mb": round(mem.total / (1024 * 1024), 1),
        "percent": round(mem.percent, 1),
    }


@router.get("/resources", response_model=SystemResources)
async def get_system_resources(user=Depends(get_admin_user)):
    cpus = _get_cpus()
    gpus = _get_gpus()
    memory = _get_memory()

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
        memory=MemoryInfo(**memory),
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
                        name = name.split("/")[-1].split(" ")[0] if name else "unknown"
                        cpu_pct = float(row[cpu_idx]) if cpu_idx is not None else 0.0
                        memory_mb = float(row[rss_idx]) / 1024.0 if rss_idx is not None else 0.0

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


def _get_vram_consumer_processes() -> list[dict]:
    """Synthetic process rows for the shared GPU's registered consumers
    (self.chat#25) -- self.llamolotl/self.speak/self.sketch/self.curator run
    in their own pods, entirely invisible to this pod's Docker socket or
    psutil.process_iter(). Sourced from the VRAM lease registry each consumer
    self-reports into, the only place their live VRAM is visible from here.

    Only vram_mb is real data; cpu_percent/memory_mb can't be observed
    cross-pod from this process and are reported as 0 rather than guessed --
    matching what self.chat#25 actually asked for (fill in the existing VRAM
    column, which is the one this pod CAN answer for other pods).

    Skipped when this pod has direct NVML access: in that case its own GPU
    processes are already covered by _get_gpu_processes(), and re-adding the
    same consumers from the registry would double them up."""
    if NVML_AVAILABLE:
        return []
    try:
        consumers = VramLeases.capacity_summary().consumers
    except Exception:
        return []

    rows = []
    for idx, consumer in enumerate(consumers):
        held = consumer.held_bytes or 0
        if held <= 0:
            continue
        name = consumer.consumer_id
        if consumer.is_stale:
            name = f"{name} (stale)"
        rows.append(
            {
                # Negative + never reused as a real host PID.
                "pid": -(idx + 1),
                "name": name,
                "container": consumer.consumer_id,
                "cpu_percent": 0.0,
                "memory_mb": 0.0,
                "vram_mb": round(held / (1024 * 1024), 1),
            }
        )
    return rows


@router.get("/processes", response_model=ProcessList)
async def get_system_processes(user=Depends(get_admin_user)):
    if DOCKER_AVAILABLE and _docker_client:
        proc_list = _get_docker_processes()
    else:
        proc_list = _get_local_processes()

    proc_list = proc_list + _get_vram_consumer_processes()

    return ProcessList(
        processes=[ProcessInfo(**p) for p in proc_list],
        timestamp=time.time(),
    )
