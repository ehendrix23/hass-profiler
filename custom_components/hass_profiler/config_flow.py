"""Configuration and options flow for a single native profiler."""
from __future__ import annotations

from dataclasses import asdict

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from . import DOMAIN
from .runtime import Settings


def schema(values: dict) -> vol.Schema:
    defaults = {**asdict(Settings()), **values}
    return vol.Schema({
        vol.Required("automatic_memory_profiling", default=defaults["automatic_memory_profiling"]): bool,
        vol.Required("profile_seconds", default=defaults["profile_seconds"]): vol.All(int, vol.Range(min=5, max=60)),
        vol.Required("rapid_mib", default=defaults["rapid_mib"]): vol.All(int, vol.Range(min=20, max=1000)),
        vol.Required("rapid_5min_mib", default=defaults["rapid_5min_mib"]): vol.All(int, vol.Range(min=50, max=2000)),
        vol.Required("rising_high_mib", default=defaults["rising_high_mib"]): vol.All(int, vol.Range(min=500, max=16000)),
        vol.Required("absolute_mib", default=defaults["absolute_mib"]): vol.All(int, vol.Range(min=500, max=16000)),
        vol.Required("cooldown_seconds", default=defaults["cooldown_seconds"]): vol.All(int, vol.Range(min=60, max=3600)),
    })


class HassProfilerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure the native runtime from the integration UI."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        errors = {}
        if user_input is not None:
            try:
                settings = Settings(**user_input)
            except (TypeError, ValueError):
                errors["base"] = "invalid_thresholds"
            else:
                return self.async_create_entry(title="HASS Profiler", data=asdict(settings))
        return self.async_show_form(step_id="user", data_schema=schema(user_input or {}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HassProfilerOptionsFlow()


class HassProfilerOptionsFlow(config_entries.OptionsFlowWithReload):
    """Validate changes and let Core reload the integration."""

    async def async_step_init(self, user_input=None):
        errors = {}
        values = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            try:
                settings = Settings(**user_input)
            except (TypeError, ValueError):
                errors["base"] = "invalid_thresholds"
            else:
                return self.async_create_entry(title="", data=asdict(settings))
        return self.async_show_form(step_id="init", data_schema=schema(user_input or values), errors=errors)
