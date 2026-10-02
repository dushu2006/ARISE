"""Host facts that can be read without desktop-control permissions."""

from __future__ import annotations

import platform

import psutil

from arise.core.models import EnvironmentSnapshot


class EnvironmentDiscovery:
    """Collect basic runtime facts; deliberately avoids process/window scraping."""

    def collect(self) -> EnvironmentSnapshot:
        unavailable = (
            "gpu_names",
            "displays",
            "active_window",
            "running_applications",
            "installed_applications",
            "browsers",
            "terminals",
            "network_status",
        )
        memory_total: int | None
        try:
            memory_total = int(psutil.virtual_memory().total)
        except (OSError, RuntimeError):
            memory_total = None
        try:
            cpu_count = psutil.cpu_count(logical=True) or 1
        except (OSError, RuntimeError):
            cpu_count = 1
        return EnvironmentSnapshot(
            operating_system=platform.system() or "unknown",
            os_version=platform.release() or "unknown",
            architecture=platform.machine() or "unknown",
            cpu_count=max(1, int(cpu_count)),
            total_memory_bytes=memory_total,
            unavailable_fields=unavailable,
        )
