"""self.chat#24/#25: k8s namespace-quota-aware CPU/memory + VRAM-registry GPU.

Unit tests against the pure helper functions in selfai_ui.routers.system --
no HTTP layer, no real DB (VramLeases is monkeypatched with a stub exposing
the same read methods the router calls). Cgroup reads are redirected at a
fake tree via system.CGROUP_ROOT so no real /sys/fs/cgroup is touched.
"""

import pytest

from selfai_ui.models.vram_leases import CapacitySummary, DeviceOccupancy, VramConsumerStatus
from selfai_ui.routers import system


class _StubVramLeases:
    def __init__(self, summary: CapacitySummary):
        self._summary = summary

    def capacity_summary(self):
        return self._summary


def _consumer(consumer_id, held_bytes, is_stale=False, total_capacity_bytes=24 * 1024**3):
    return VramConsumerStatus(
        consumer_id=consumer_id,
        total_capacity_bytes=total_capacity_bytes,
        held_bytes=held_bytes,
        effective_state="stale" if is_stale else "steady",
        is_stale=is_stale,
    )


############################
# _running_in_kubernetes
############################


def test_running_in_kubernetes_env_var(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert system._running_in_kubernetes() is True


def test_not_running_in_kubernetes(tmp_path, monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(system, "KUBERNETES_SERVICEACCOUNT_PATH", tmp_path / "nonexistent")
    assert system._running_in_kubernetes() is False


############################
# cgroup cpu.max / cpu.stat / memory.current+max
############################


def test_cgroup_cpu_quota_cores_parses_quota_period(tmp_path, monkeypatch):
    (tmp_path / "cpu.max").write_text("200000 100000\n")
    monkeypatch.setattr(system, "CGROUP_ROOT", tmp_path)
    assert system._cgroup_cpu_quota_cores() == 2.0


def test_cgroup_cpu_quota_cores_none_when_unlimited(tmp_path, monkeypatch):
    (tmp_path / "cpu.max").write_text("max 100000\n")
    monkeypatch.setattr(system, "CGROUP_ROOT", tmp_path)
    assert system._cgroup_cpu_quota_cores() is None


def test_cgroup_cpu_quota_cores_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "CGROUP_ROOT", tmp_path)
    assert system._cgroup_cpu_quota_cores() is None


def test_cgroup_cpu_quota_cores_none_when_malformed(tmp_path, monkeypatch):
    (tmp_path / "cpu.max").write_text("garbage\n")
    monkeypatch.setattr(system, "CGROUP_ROOT", tmp_path)
    assert system._cgroup_cpu_quota_cores() is None


def test_cgroup_memory_usage_bytes_parses_current_and_max(tmp_path, monkeypatch):
    (tmp_path / "memory.current").write_text("1073741824\n")
    (tmp_path / "memory.max").write_text("2147483648\n")
    monkeypatch.setattr(system, "CGROUP_ROOT", tmp_path)
    assert system._cgroup_memory_usage_bytes() == (1073741824, 2147483648)


def test_cgroup_memory_usage_bytes_none_when_unlimited(tmp_path, monkeypatch):
    (tmp_path / "memory.current").write_text("1073741824\n")
    (tmp_path / "memory.max").write_text("max\n")
    monkeypatch.setattr(system, "CGROUP_ROOT", tmp_path)
    assert system._cgroup_memory_usage_bytes() is None


def test_cgroup_memory_usage_bytes_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "CGROUP_ROOT", tmp_path)
    assert system._cgroup_memory_usage_bytes() is None


############################
# _get_cpus() / _get_memory() k8s branch
############################


def test_get_cpus_uses_quota_in_kubernetes(tmp_path, monkeypatch):
    (tmp_path / "cpu.max").write_text("200000 100000\n")
    (tmp_path / "cpu.stat").write_text("usage_usec 0\n")
    monkeypatch.setattr(system, "CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(system, "IN_KUBERNETES", True)

    cpus = system._get_cpus()

    assert len(cpus) == 1
    assert cpus[0]["cores"] == 2.0
    assert "namespace quota" in cpus[0]["model"]


def test_get_cpus_falls_back_to_host_when_no_quota(tmp_path, monkeypatch):
    (tmp_path / "cpu.max").write_text("max 100000\n")
    monkeypatch.setattr(system, "CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(system, "IN_KUBERNETES", True)

    cpus = system._get_cpus()

    assert len(cpus) >= 1
    assert all("namespace quota" not in c["model"] for c in cpus)


def test_get_memory_uses_cgroup_in_kubernetes(tmp_path, monkeypatch):
    (tmp_path / "memory.current").write_text(str(1 * 1024**3) + "\n")
    (tmp_path / "memory.max").write_text(str(2 * 1024**3) + "\n")
    monkeypatch.setattr(system, "CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(system, "IN_KUBERNETES", True)

    memory = system._get_memory()

    assert memory["total_mb"] == pytest.approx(2048.0, abs=0.5)
    assert memory["percent"] == pytest.approx(50.0, abs=0.5)


def test_get_memory_falls_back_to_host_when_no_limit(tmp_path, monkeypatch):
    (tmp_path / "memory.current").write_text(str(1 * 1024**3) + "\n")
    (tmp_path / "memory.max").write_text("max\n")
    monkeypatch.setattr(system, "CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(system, "IN_KUBERNETES", True)

    memory = system._get_memory()

    # Host-wide psutil reading, not the pod's 1Gi cgroup current -- on any real
    # test host this is far more than 1Gi.
    assert memory["total_mb"] > 2048.0


############################
# _get_gpus_from_vram_registry() / _get_gpus()
############################


def test_get_gpus_from_vram_registry_empty_when_no_consumers(monkeypatch):
    summary = CapacitySummary(total_capacity_bytes=0, total_held_bytes=0, free_bytes=0)
    monkeypatch.setattr(system, "VramLeases", _StubVramLeases(summary))
    assert system._get_gpus_from_vram_registry() == []


def test_get_gpus_from_vram_registry_uses_device_occupancy_when_fresh(monkeypatch):
    total = 24 * 1024**3
    used = 6 * 1024**3
    summary = CapacitySummary(
        total_capacity_bytes=total,
        total_held_bytes=5 * 1024**3,
        free_bytes=total - 5 * 1024**3,
        device_occupancy=DeviceOccupancy(
            used_bytes=used, total_bytes=total, reported_by="self.llamolotl", reported_at=0
        ),
    )
    monkeypatch.setattr(system, "VramLeases", _StubVramLeases(summary))

    gpus = system._get_gpus_from_vram_registry()

    assert len(gpus) == 1
    assert gpus[0]["utilization"] == 0.0
    assert gpus[0]["vram_used_mb"] == round(used / (1024 * 1024), 1)
    assert gpus[0]["vram_total_mb"] == round(total / (1024 * 1024), 1)


def test_get_gpus_from_vram_registry_falls_back_to_ledger_held(monkeypatch):
    total = 24 * 1024**3
    held = 3 * 1024**3
    summary = CapacitySummary(
        total_capacity_bytes=total,
        total_held_bytes=held,
        free_bytes=total - held,
        device_occupancy=None,
    )
    monkeypatch.setattr(system, "VramLeases", _StubVramLeases(summary))

    gpus = system._get_gpus_from_vram_registry()

    assert len(gpus) == 1
    assert gpus[0]["vram_used_mb"] == round(held / (1024 * 1024), 1)


def test_get_gpus_falls_back_to_registry_when_no_nvml(monkeypatch):
    summary = CapacitySummary(total_capacity_bytes=0, total_held_bytes=0, free_bytes=0)
    monkeypatch.setattr(system, "VramLeases", _StubVramLeases(summary))
    monkeypatch.setattr(system, "NVML_AVAILABLE", False)
    assert system._get_gpus() == []


############################
# _get_vram_consumer_processes()
############################


def test_get_vram_consumer_processes_reports_held_consumers(monkeypatch):
    summary = CapacitySummary(
        total_capacity_bytes=24 * 1024**3,
        total_held_bytes=5 * 1024**3,
        free_bytes=19 * 1024**3,
        consumers=[
            _consumer("self.llamolotl", 4 * 1024**3),
            _consumer("self.speak", 1 * 1024**3, is_stale=True),
            _consumer("self.sketch", 0),  # held=0 -- must be skipped
        ],
    )
    monkeypatch.setattr(system, "VramLeases", _StubVramLeases(summary))
    monkeypatch.setattr(system, "NVML_AVAILABLE", False)

    rows = system._get_vram_consumer_processes()

    assert len(rows) == 2
    by_container = {r["container"]: r for r in rows}
    assert "self.llamolotl" in by_container
    assert "self.speak" in by_container
    assert "self.sketch" not in by_container
    assert by_container["self.speak"]["name"] == "self.speak (stale)"
    assert by_container["self.llamolotl"]["vram_mb"] == round(4 * 1024**3 / (1024 * 1024), 1)
    # pids are synthetic, negative, and unique
    pids = [r["pid"] for r in rows]
    assert all(p < 0 for p in pids)
    assert len(set(pids)) == len(pids)


def test_get_vram_consumer_processes_skipped_when_nvml_available(monkeypatch):
    monkeypatch.setattr(system, "NVML_AVAILABLE", True)
    assert system._get_vram_consumer_processes() == []
