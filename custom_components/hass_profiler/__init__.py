"""HASS Profiler: standalone in-Core diagnostics."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from .runtime import Runtime, Settings

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

DOMAIN = "hass_profiler"


def _create_runtime(config_path: str, settings: Settings) -> Runtime:
    share = Path("/share")
    directory = share / "Logs" / DOMAIN.replace("_", "-") if share.is_dir() else Path(config_path) / "hass-profiler-diagnostics"
    return Runtime(directory, settings)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Set up a single native instance with administrator-only actions."""
    import voluptuous as vol

    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    from homeassistant.core import SupportsResponse
    from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
    from homeassistant.helpers.service import async_register_admin_service

    if DOMAIN in hass.data:
        raise HomeAssistantError("Only one HASS Profiler instance can run")
    settings = Settings(**{**entry.data, **entry.options})
    try:
        runtime = await hass.async_add_executor_job(_create_runtime, hass.config.config_dir, settings)
    except (OSError, ValueError) as err:
        raise ConfigEntryNotReady("Native diagnostic storage unavailable") from err
    hass.data[DOMAIN] = runtime
    entry.runtime_data = runtime

    async def profile_memory(call):
        future = await hass.async_add_executor_job(
            runtime.request_manual, call.data["seconds"], call.data["include_census"]
        )
        return await asyncio.shield(asyncio.wrap_future(future))

    async def status(call):
        return await hass.async_add_executor_job(runtime.status)

    async def shutdown(event):
        await hass.async_add_executor_job(runtime.stop)

    try:
        async_register_admin_service(
            hass, DOMAIN, "profile_memory", profile_memory,
            schema=vol.Schema({
                vol.Optional("seconds", default=settings.profile_seconds): vol.All(int, vol.Range(min=5, max=60)),
                vol.Optional("include_census", default=False): bool,
            }),
            supports_response=SupportsResponse.ONLY,
        )
        async_register_admin_service(
            hass, DOMAIN, "status", status, supports_response=SupportsResponse.ONLY,
        )
        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown))
        await hass.async_add_executor_job(runtime.start)
    except BaseException:
        await hass.async_add_executor_job(runtime.stop)
        hass.services.async_remove(DOMAIN, "profile_memory")
        hass.services.async_remove(DOMAIN, "status")
        hass.data.pop(DOMAIN, None)
        raise


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Stop monitoring and any owned capture before removing the services."""
    runtime = entry.runtime_data
    await hass.async_add_executor_job(runtime.stop)
    hass.services.async_remove(DOMAIN, "profile_memory")
    hass.services.async_remove(DOMAIN, "status")
    hass.data.pop(DOMAIN, None)
