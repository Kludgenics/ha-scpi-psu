"""Config flow for SCPI Programmable Power Supply using serialx."""

from __future__ import annotations

import logging
from typing import Any

import probatio
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from serialx import SerialException

from .const import (
    CONF_BAUDRATE,
    CONF_IDN,
    CONF_MAX_CURRENT_CLAMP,
    CONF_MAX_VOLTAGE_CLAMP,
    CONF_NAME,
    CONF_PROFILE,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL_PORT,
    DEFAULT_BAUDRATE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PROFILE_AUTO,
    PROFILE_OWON,
    PROFILE_STANDARD,
    merged_entry_config,
)
from .scpi_psu_client import PsuIdentity, ScpiPsuClient

_LOGGER = logging.getLogger(__name__)

PROFILE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(value=PROFILE_AUTO, label="Auto-detect via *IDN?"),
            selector.SelectOptionDict(
                value=PROFILE_STANDARD,
                label="Standard IEEE 488.2 / SCPI (Rigol, Siglent, Keysight, etc.)",
            ),
            selector.SelectOptionDict(
                value=PROFILE_OWON,
                label="OWON & Rebrands (MEAS:ALL:INFO?, VOLT:LIM)",
            ),
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)


BAUDRATE_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=1200, max=921600, step=1, mode=selector.NumberSelectorMode.BOX
    )
)

VOLTAGE_CLAMP_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0.1,
        max=300.0,
        step=0.1,
        unit_of_measurement="V",
        mode=selector.NumberSelectorMode.BOX,
    )
)

CURRENT_CLAMP_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0.01,
        max=100.0,
        step=0.01,
        unit_of_measurement="A",
        mode=selector.NumberSelectorMode.BOX,
    )
)


async def _async_probe_psu(serial_url: str, baudrate: int) -> PsuIdentity:
    """Probe SCPI PSU identity using ScpiPsuClient."""
    client = ScpiPsuClient(serial_url=serial_url, baudrate=baudrate)
    try:
        await client.connect()
        if not client.is_connected:
            raise SerialException(f"Unable to establish serial connection to {serial_url}")
        return await client.probe_identity()
    finally:
        await client.close()


async def _async_probe_with_errors(
    serial_url: str, baudrate: int, errors: dict[str, str]
) -> PsuIdentity | None:
    """Probe SCPI PSU identity, recording cannot_connect in errors on failure."""
    try:
        return await _async_probe_psu(serial_url, baudrate)
    except (OSError, SerialException, TimeoutError) as err:
        _LOGGER.warning("Failed to probe SCPI PSU at %s: %s", serial_url, err)
        errors["base"] = "cannot_connect"
        return None


def _parse_connection_input(user_input: dict[str, Any]) -> tuple[str, int]:
    """Extract normalized serial URL and baudrate from user input."""
    return user_input[CONF_SERIAL_PORT].strip(), int(user_input[CONF_BAUDRATE])


def _profile_and_clamp_schema(
    *,
    profile_default: str = PROFILE_AUTO,
    v_clamp_suggested: float | None = None,
    i_clamp_suggested: float | None = None,
) -> dict[Any, Any]:
    v_opt = (
        probatio.Optional(
            CONF_MAX_VOLTAGE_CLAMP, description={"suggested_value": v_clamp_suggested}
        )
        if v_clamp_suggested is not None
        else probatio.Optional(CONF_MAX_VOLTAGE_CLAMP)
    )
    i_opt = (
        probatio.Optional(
            CONF_MAX_CURRENT_CLAMP, description={"suggested_value": i_clamp_suggested}
        )
        if i_clamp_suggested is not None
        else probatio.Optional(CONF_MAX_CURRENT_CLAMP)
    )
    return {
        probatio.Required(CONF_PROFILE, default=profile_default): PROFILE_SELECTOR,
        v_opt: VOLTAGE_CLAMP_SELECTOR,
        i_opt: CURRENT_CLAMP_SELECTOR,
    }


def _clean_optional_float(val: Any) -> float | None:
    """Convert optional selector value to positive float or None."""
    if val in (None, "", 0, 0.0):
        return None
    try:
        f = float(val)
        return f if f > 0 else None
    except TypeError, ValueError:
        return None


class ScpiPsuConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for an SCPI Power Supply."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow state."""
        self._serial_url: str | None = None
        self._baudrate: int = DEFAULT_BAUDRATE
        self._identity: PsuIdentity | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: Discover and select serial port and baudrate."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._serial_url, self._baudrate = _parse_connection_input(user_input)

            identity = await _async_probe_with_errors(self._serial_url, self._baudrate, errors)
            if identity is not None:
                self._identity = identity
                unique_key = (
                    f"{identity.manufacturer}_{identity.model}_{identity.serial_number}".lower()
                    if identity.serial_number
                    else f"serialx_{self._serial_url}"
                )
                await self.async_set_unique_id(unique_key)
                self._abort_if_unique_id_configured()
                return await self.async_step_confirm()

        schema = probatio.Schema(
            {
                probatio.Required(CONF_SERIAL_PORT): selector.SerialPortSelector(),
                probatio.Required(CONF_BAUDRATE, default=DEFAULT_BAUDRATE): BAUDRATE_SELECTOR,
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Confirm device options, friendly name, and safety clamps."""
        assert self._identity is not None
        assert self._serial_url is not None

        if user_input is not None:
            custom_name = (user_input.get(CONF_NAME) or "").strip()
            title = custom_name if custom_name else self._identity.default_title

            v_clamp = _clean_optional_float(user_input.get(CONF_MAX_VOLTAGE_CLAMP))
            i_clamp = _clean_optional_float(user_input.get(CONF_MAX_CURRENT_CLAMP))

            data: dict[str, Any] = {
                CONF_SERIAL_PORT: self._serial_url,
                CONF_BAUDRATE: self._baudrate,
                CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                CONF_PROFILE: user_input.get(CONF_PROFILE, PROFILE_AUTO),
                CONF_IDN: self._identity.raw_idn,
            }
            if v_clamp is not None:
                data[CONF_MAX_VOLTAGE_CLAMP] = v_clamp
            if i_clamp is not None:
                data[CONF_MAX_CURRENT_CLAMP] = i_clamp

            return self.async_create_entry(title=title, data=data)

        schema = probatio.Schema(
            {
                probatio.Optional(
                    CONF_NAME,
                    description={"suggested_value": self._identity.default_title},
                ): selector.TextSelector(),
                **_profile_and_clamp_schema(),
            }
        )

        return self.async_show_form(
            step_id="confirm",
            data_schema=schema,
            description_placeholders={
                "model": self._identity.default_title,
                "serial_number": self._identity.serial_number or "N/A",
                "firmware": self._identity.firmware or "Unknown",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ScpiPsuOptionsFlow(config_entry)


class ScpiPsuOptionsFlow(OptionsFlow):
    """Handle options for an SCPI Power Supply."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current_cfg = merged_entry_config(self._entry)
        current_port = current_cfg.get(CONF_SERIAL_PORT, "")
        current_baud = int(current_cfg.get(CONF_BAUDRATE, DEFAULT_BAUDRATE))
        if user_input is not None:
            serial_url, baudrate = _parse_connection_input(user_input)
            probe_ok = True
            if serial_url != current_port or baudrate != current_baud:
                probe_ok = await _async_probe_with_errors(serial_url, baudrate, errors) is not None
            if probe_ok:
                cleaned = {
                    CONF_SERIAL_PORT: serial_url,
                    CONF_BAUDRATE: baudrate,
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_PROFILE: user_input.get(CONF_PROFILE, PROFILE_AUTO),
                    CONF_MAX_VOLTAGE_CLAMP: _clean_optional_float(
                        user_input.get(CONF_MAX_VOLTAGE_CLAMP)
                    ),
                    CONF_MAX_CURRENT_CLAMP: _clean_optional_float(
                        user_input.get(CONF_MAX_CURRENT_CLAMP)
                    ),
                }
                return self.async_create_entry(title="", data=cleaned)

        cfg = {**current_cfg, **(user_input or {})}

        schema_dict: dict[Any, Any] = {
            probatio.Required(
                CONF_SERIAL_PORT, default=cfg.get(CONF_SERIAL_PORT, "")
            ): selector.SerialPortSelector(),
            probatio.Required(
                CONF_BAUDRATE, default=int(cfg.get(CONF_BAUDRATE, DEFAULT_BAUDRATE))
            ): BAUDRATE_SELECTOR,
            **_profile_and_clamp_schema(
                profile_default=cfg.get(CONF_PROFILE, PROFILE_AUTO),
                v_clamp_suggested=cfg.get(CONF_MAX_VOLTAGE_CLAMP),
                i_clamp_suggested=cfg.get(CONF_MAX_CURRENT_CLAMP),
            ),
            probatio.Required(
                CONF_SCAN_INTERVAL,
                default=int(cfg.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=60,
                    step=1,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }

        return self.async_show_form(
            step_id="init", data_schema=probatio.Schema(schema_dict), errors=errors
        )
