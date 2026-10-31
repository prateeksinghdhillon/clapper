"""CPU / RAM / GPU / VRAM / temperature readings for the Director page."""
from __future__ import annotations

import threading

import psutil

_nvml = None
_nvml_lock = threading.Lock()


def _gpu_handle():
    global _nvml
    with _nvml_lock:
        if _nvml is None:
            try:
                import warnings

                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    import pynvml
                pynvml.nvmlInit()
                _nvml = (pynvml, pynvml.nvmlDeviceGetHandleByIndex(0))
            except Exception:  # noqa: BLE001 — no NVIDIA GPU / driver
                _nvml = False
        return _nvml or None


def _cpu_temp() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
    except Exception:  # noqa: BLE001
        return None
    for chip in ("k10temp", "coretemp", "zenpower", "cpu_thermal"):
        for entry in temps.get(chip, []):
            if entry.label in ("Tctl", "Tdie", "Package id 0", "") or chip == "cpu_thermal":
                return round(entry.current, 1)
    return None


def read() -> dict:
    vm = psutil.virtual_memory()
    out = {
        "cpu": {"percent": psutil.cpu_percent(interval=None), "temp": _cpu_temp(), "cores": psutil.cpu_count()},
        "ram": {"used": vm.total - vm.available, "total": vm.total, "percent": vm.percent},
        "gpu": None,
    }
    handle = _gpu_handle()
    if handle:
        nv, h = handle
        try:
            mem = nv.nvmlDeviceGetMemoryInfo(h)
            name = nv.nvmlDeviceGetName(h)
            out["gpu"] = {
                "name": name.decode() if isinstance(name, bytes) else name,
                "percent": nv.nvmlDeviceGetUtilizationRates(h).gpu,
                "vram_used": mem.used, "vram_total": mem.total,
                "temp": nv.nvmlDeviceGetTemperature(h, 0),
                "power": round(nv.nvmlDeviceGetPowerUsage(h) / 1000, 1),
                "power_limit": round(nv.nvmlDeviceGetEnforcedPowerLimit(h) / 1000, 1),
            }
        except Exception:  # noqa: BLE001
            out["gpu"] = None
    return out


psutil.cpu_percent(interval=None)  # prime the CPU counter so the first reading is meaningful
