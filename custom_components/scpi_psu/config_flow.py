"""Config flow for SCPI Programmable Power Supply using serialx."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
import voluptuous as vol

from serialx import Parity, SerialException, StopBits, open_serial_connection

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

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
)
from .profiles import PsuIdentity, parse_idn

_LOGGER = logging.getLogger(__name__)


async def _async_probe_psu(serial_url: str, baudrate: int) -> PsuIdentity:
    """Open serialx connection, query *IDN? (or MEAS:VOLT?), and return parsed PsuIdentity."""
    reader, writer = await asyncio.wait_for(
        open_serial_connection(
            url=serial_url,
            baudrate=baudrate,
            bytesize=8,
            parity=Parity.NONE,
            stopbits=StopBits.ONE,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        ),
        timeout=4.0,
    )
    try:
        await asyncio.sleep(0.15)
        try:
            while await asyncio.wait_for(reader.read(512), timeout=0.03):
                pass
        except TimeoutError:
            pass

        writer.write(b"*IDN?\n")
        await asyncio.wait_for(writer.drain(), timeout=1.5)
        raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
        idn_str = raw.decode("utf-8", errors="ignore").strip() if raw else ""
        return parse_idn(idn_str)
    finally:
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=1.5)
        except Exception:  # noqa: BLE001
            pass


def _clean_optional_float(val: Any) -> float | None:
    """Convert optional selector value to positive float or None."""
    if val in (None, "", 0, 0.0):
        return None
    try:
        f = float(val)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


class ScpiPsuConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for an SCPI Power Supply."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            serial_url = user_input[CONF_SERIAL_PORT].strip()
            baudrate = int(user_input[CONF_BAUDRATE])

            try:
                identity = await _async_probe_psu(serial_url, baudrate)
            except (OSError, SerialException, TimeoutError) as err:
                _LOGGER.warning("Failed to probe SCPI PSU at %s: %s", serial_url, err)
                errors["base"] = "cannot_connect"
            else:
                unique_key = (
                    f"{identity.manufacturer}_{identity.model}_{identity.serial_number}".lower()
                    if identity.serial_number
                    else f"serialx_{serial_url}"
                )
                await self.async_set_unique_id(unique_key)
                self._abort_if_unique_id_configured()

                custom_name = (user_input.get(CONF_NAME) or "").strip()
                title = custom_name if custom_name else identity.default_title

                v_clamp = _clean_optional_float(user_input.get(CONF_MAX_VOLTAGE_CLAMP))
                i_clamp = _clean_optional_float(user_input.get(CONF_MAX_CURRENT_CLAMP))

                data: dict[str, Any] = {
                    CONF_SERIAL_PORT: serial_url,
                    CONF_BAUDRATE: baudrate,
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_PROFILE: user_input.get(CONF_PROFILE, PROFILE_AUTO),
                    CONF_IDN: identity.raw_idn,
                }
                if v_clamp is not None:
                    data[CONF_MAX_VOLTAGE_CLAMP] = v_clamp
                if i_clamp is not None:
                    data[CONF_MAX_CURRENT_CLAMP] = i_clamp

                return self.async_create_entry(title=title, data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_SERIAL_PORT): selector.SerialPortSelector(),
                vol.Optional(CONF_NAME): selector.TextSelector(),
                vol.Required(
                    CONF_BAUDRATE, default=DEFAULT_BAUDRATE
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1200, max=921600, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_PROFILE, default=PROFILE_AUTO
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=PROFILE_AUTO, label="Auto-detect via *IDN?"
                            ),
                            selector.SelectOptionDict(
                                value=PROFILE_OWON,
                                label="OWON / Abestop / Kiprim (MEAS:ALL:INFO?, VOLT:LIM)",
                            ),
                            selector.SelectOptionDict(
                                value=PROFILE_STANDARD,
                                label="Standard IEEE 488.2 / SCPI (MEAS:VOLT?, VOLT:PROT)",
                            ),
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_MAX_VOLTAGE_CLAMP): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.1,
                        max=300.0,
                        step=0.1,
                        unit_of_measurement="V",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CONF_MAX_CURRENT_CLAMP): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.01,
                        max=100.0,
                        step=0.01,
                        unit_of_measurement="A",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
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
        )

        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return ScpiPsuOptionsFlow(config_entry)


class ScpiPsuOptionsFlow(OptionsFlow):
    """Handle options for an SCPI Power Supply."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage options."""
        errors: dict[str, str] = {}
        current_cfg = {**self._entry.data, **self._entry.options}
        current_port = current_cfg.get(CONF_SERIAL_PORT) or current_cfg.get("serial_url", "")
        if user_input is not None:
            serial_url = user_input[CONF_SERIAL_PORT].strip()
            baudrate = int(user_input[CONF_BAUDRATE])
            try:
                if serial_url != current_port:
                    await _async_probe_psu(serial_url, baudrate)
            except (OSError, SerialException, TimeoutError) as err:
                _LOGGER.warning("Failed to probe SCPI PSU at %s: %s", serial_url, err)
                errors["base"] = "cannot_connect"
            else:
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

        cfg = {**self._entry.data, **self._entry.options, **(user_input or {})}
        v_clamp_default = cfg.get(CONF_MAX_VOLTAGE_CLAMP)
        i_clamp_default = cfg.get(CONF_MAX_CURRENT_CLAMP)

        schema_dict: dict[Any, Any] = {
            vol.Required(
                CONF_SERIAL_PORT, default=cfg.get(CONF_SERIAL_PORT) or cfg.get("serial_url", "")
            ): selector.SerialPortSelector(),
            vol.Required(
                CONF_BAUDRATE, default=int(cfg.get(CONF_BAUDRATE, DEFAULT_BAUDRATE))
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1200, max=921600, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_PROFILE, default=cfg.get(CONF_PROFILE, PROFILE_AUTO)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=PROFILE_AUTO, label="Auto-detect via *IDN?"
                        ),
                        selector.SelectOptionDict(
                            value=PROFILE_OWON,
                            label="OWON / Abestop / Kiprim (MEAS:ALL:INFO?, VOLT:LIM)",
                        ),
                        selector.SelectOptionDict(
                            value=PROFILE_STANDARD,
                            label="Standard IEEE 488.2 / SCPI (MEAS:VOLT?, VOLT:PROT)",
                        ),
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_MAX_VOLTAGE_CLAMP,
                description={"suggested_value": v_clamp_default},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1,
                    max=300.0,
                    step=0.1,
                    unit_of_measurement="V",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MAX_CURRENT_CLAMP,
                description={"suggested_value": i_clamp_default},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.01,
                    max=100.0,
                    step=0.01,
                    unit_of_measurement="A",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
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
            step_id="init", data_schema=vol.Schema(schema_dict), errors=errors
        )
