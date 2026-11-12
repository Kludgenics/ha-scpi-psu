"""Tests for SCPI PSU platforms (sensor, binary_sensor, switch, number, button)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scpi_psu import (
    binary_sensor,
    button,
    number,
    sensor,
    switch,
)
from custom_components.scpi_psu.const import CONF_SERIAL_PORT, DOMAIN
from custom_components.scpi_psu.coordinator import ScpiPsuCoordinator, ScpiPsuData
from custom_components.scpi_psu.scpi_psu_client import (
    FIELD_CURRENT,
    FIELD_DMM,
    FIELD_FAULT_ALARM,
    FIELD_OCP_TRIPPED,
    FIELD_OPP_TRIPPED,
    FIELD_OUTPUT_ENABLED,
    FIELD_OVP_TRIPPED,
    FIELD_POWER,
    FIELD_STATUS_TEXT,
    FIELD_VOLTAGE,
    ChannelTelemetry,
)


async def test_sensor_entities(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test PSU",
        data={CONF_SERIAL_PORT: "socket://127.0.0.1:5025"},
        entry_id="test_sensor_entry",
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    coordinator.data = ScpiPsuData(
        system_error="0,No error",
        channels={
            1: ChannelTelemetry(
                channel=1,
                voltage=12.34,
                current=1.5,
                power=18.51,
                status_text="Active",
            )
        },
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    added: list[sensor.ScpiPsuSensor] = []
    await sensor.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: added.extend(ents))
    )
    # Default device has no DMM, so all sensors except dmm are registered
    assert len(added) == 5

    for ent in added:
        ent.hass = hass
        await ent.async_added_to_hass()

    assert coordinator.active_fields_by_channel == {
        1: {FIELD_VOLTAGE, FIELD_CURRENT, FIELD_POWER, FIELD_STATUS_TEXT}
    }

    sensor_map = {ent.entity_description.key: ent for ent in added}
    assert sensor_map["voltage"].available is True
    assert sensor_map["voltage"].native_value == 12.34
    assert sensor_map["current"].native_value == 1.5
    assert sensor_map["power"].native_value == 18.51
    assert sensor_map["status"].native_value == "Active"
    assert sensor_map["error_register"].native_value == "0,No error"

    for ent in added:
        await ent.async_will_remove_from_hass()
    assert coordinator.active_fields_by_channel == coordinator.client.default_fields_by_channel()

    # Test when device has DMM capability (e.g. SPM6103)
    from custom_components.scpi_psu.scpi_psu_client.models import PsuIdentity

    coordinator.client.identity = PsuIdentity(
        manufacturer="OWON",
        model="SPM6103",
        serial_number="123",
        firmware="1.0",
        raw_idn="OWON,SPM6103,123,1.0",
        profile_key="owon",
        hw_max_voltage=60.0,
        hw_max_current=10.0,
        has_dmm=True,
    )
    coordinator.data.dmm_reading = 3.3
    dmm_added: list[sensor.ScpiPsuSensor] = []
    await sensor.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: dmm_added.extend(ents))
    )
    assert len(dmm_added) == 6
    dmm_map = {ent.entity_description.key: ent for ent in dmm_added}
    dmm_map["dmm"].hass = hass
    await dmm_map["dmm"].async_added_to_hass()
    assert FIELD_DMM in coordinator.active_fields_by_channel[1]
    assert dmm_map["dmm"].native_value == 3.3
    await dmm_map["dmm"].async_will_remove_from_hass()


async def test_binary_sensor_entities(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test PSU",
        data={CONF_SERIAL_PORT: "socket://127.0.0.1:5025"},
        entry_id="test_bin_sensor_entry",
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    coordinator.data = ScpiPsuData(
        connected=True,
        responding=True,
        channels={
            1: ChannelTelemetry(
                channel=1,
                fault_alarm=False,
                ovp_tripped=False,
                ocp_tripped=True,
                opp_tripped=False,
            )
        },
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Standard profile: 'connected', 'ovp_tripped', and 'ocp_tripped' are supported
    std_added: list[binary_sensor.ScpiPsuBinarySensor] = []
    await binary_sensor.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: std_added.extend(ents))
    )
    assert len(std_added) == 3
    std_keys = {b.entity_description.key for b in std_added}
    assert std_keys == {"connected", "ovp_tripped", "ocp_tripped"}
    std_map = {b.entity_description.key: b for b in std_added}
    assert std_map["connected"].is_on is True
    assert std_map["ovp_tripped"].is_on is False
    assert std_map["ocp_tripped"].is_on is True

    # OWON profile: all 5 binary sensors registered
    from custom_components.scpi_psu.scpi_psu_client.models import parse_idn

    coordinator.client.identity = parse_idn("OWON,SPE6103,123,1.0")
    added: list[binary_sensor.ScpiPsuBinarySensor] = []
    await binary_sensor.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: added.extend(ents))
    )
    assert {e.entity_description.key for e in added} == {
        "fault",
        "connected",
        "ovp_tripped",
        "ocp_tripped",
        "opp_tripped",
    }

    for ent in added:
        ent.hass = hass
        await ent.async_added_to_hass()

    assert coordinator.active_fields_by_channel == {
        1: {FIELD_FAULT_ALARM, FIELD_OVP_TRIPPED, FIELD_OCP_TRIPPED, FIELD_OPP_TRIPPED}
    }

    bin_map = {ent.entity_description.key: ent for ent in added}
    assert bin_map["connected"].is_on is True
    assert bin_map["fault"].is_on is False
    assert bin_map["ocp_tripped"].is_on is True
    assert bin_map["opp_tripped"].name == "Over-temperature Protection (OTP)"
    assert bin_map["opp_tripped"].unique_id == f"{entry.entry_id}_opp_tripped"

    for ent in added:
        await ent.async_will_remove_from_hass()
    assert coordinator.active_fields_by_channel == coordinator.client.default_fields_by_channel()


async def test_switch_entity(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test PSU",
        data={CONF_SERIAL_PORT: "socket://127.0.0.1:5025"},
        entry_id="test_switch_entry",
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    coordinator.data = ScpiPsuData(channels={1: ChannelTelemetry(channel=1, output_enabled=True)})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    added: list[switch.ScpiPsuOutputSwitch] = []
    await switch.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: added.extend(ents))
    )
    assert len(added) == 1

    sw = added[0]
    sw.hass = hass
    await sw.async_added_to_hass()
    assert coordinator.active_fields_by_channel == {1: {FIELD_OUTPUT_ENABLED}}
    assert sw.is_on is True

    with patch.object(coordinator, "async_set_output", new_callable=AsyncMock) as mock_set:
        await sw.async_turn_off()
        mock_set.assert_awaited_once_with(False, channel=1)

        await sw.async_turn_on()
        mock_set.assert_awaited_with(True, channel=1)

    await sw.async_will_remove_from_hass()
    assert coordinator.active_fields_by_channel == coordinator.client.default_fields_by_channel()


async def test_multichannel_switch_entities(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Rigol DP832",
        data={CONF_SERIAL_PORT: "socket://127.0.0.1:5025"},
        entry_id="test_multi_switch_entry",
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    coordinator.client.identity = coordinator.identity.__class__(
        manufacturer="RIGOL",
        model="DP832",
        serial_number="DP80000001",
        firmware="00.01.16",
        raw_idn="RIGOL TECHNOLOGIES,DP832,DP80000001,00.01.16",
        profile_key="standard",
        hw_max_voltage=30.0,
        hw_max_current=3.0,
        channels=3,
    )
    coordinator.data = ScpiPsuData(
        channel_count=3,
        channels={
            1: ChannelTelemetry(channel=1, output_enabled=True),
            2: ChannelTelemetry(channel=2, output_enabled=False),
            3: ChannelTelemetry(channel=3),
        },
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    added: list[switch.SwitchEntity] = []
    await switch.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: added.extend(ents))
    )
    assert len(added) == 4
    for ent in added:
        ent.hass = hass
        await ent.async_added_to_hass()

    assert coordinator.active_fields_by_channel == {
        1: {FIELD_OUTPUT_ENABLED},
        2: {FIELD_OUTPUT_ENABLED},
        3: {FIELD_OUTPUT_ENABLED},
    }

    master_sw = added[0]
    ch1_sw = added[1]
    ch2_sw = added[2]
    ch3_sw = added[3]

    assert isinstance(master_sw, switch.ScpiPsuMasterOutputSwitch)
    assert master_sw.is_on is True

    assert isinstance(ch1_sw, switch.ScpiPsuOutputSwitch)
    assert ch1_sw.is_on is True
    assert ch2_sw.is_on is False
    assert ch3_sw.is_on is None

    with patch.object(coordinator, "async_set_all_outputs", new_callable=AsyncMock) as mock_set_all:
        await master_sw.async_turn_on()
        mock_set_all.assert_awaited_once_with(True)
        await master_sw.async_turn_off()
        mock_set_all.assert_awaited_with(False)

    with patch.object(coordinator, "async_set_output", new_callable=AsyncMock) as mock_set_ch:
        await ch2_sw.async_turn_on()
        mock_set_ch.assert_awaited_once_with(True, channel=2)
        await ch2_sw.async_turn_off()
        mock_set_ch.assert_awaited_with(False, channel=2)

    for ent in added:
        await ent.async_will_remove_from_hass()
    assert coordinator.active_fields_by_channel == coordinator.client.default_fields_by_channel()


async def test_button_entity(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test PSU",
        data={CONF_SERIAL_PORT: "socket://127.0.0.1:5025"},
        entry_id="test_button_entry",
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    added: list[button.ScpiPsuResetButton] = []
    await button.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: added.extend(ents))
    )
    assert len(added) == 1

    btn = added[0]
    with patch.object(coordinator, "async_reset_errors", new_callable=AsyncMock) as mock_reset:
        await btn.async_press()
        mock_reset.assert_awaited_once()


async def test_number_entities(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test PSU",
        data={CONF_SERIAL_PORT: "socket://127.0.0.1:5025"},
        entry_id="test_number_entry",
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    coordinator.data = ScpiPsuData(
        voltage_setpoint=5.0,
        current_setpoint=2.0,
        voltage_limit=30.0,
        current_limit=5.0,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    added: list[number.ScpiPsuNumber] = []
    await number.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: added.extend(ents))
    )
    assert len(added) == 4

    num_map = {ent.entity_description.key: ent for ent in added}
    assert num_map["voltage_setpoint"].native_value == 5.0
    assert num_map["current_setpoint"].native_value == 2.0
    assert num_map["voltage_setpoint"].native_max_value == 60.0

    with patch.object(coordinator, "async_set_voltage", new_callable=AsyncMock) as mock_v:
        await num_map["voltage_setpoint"].async_set_native_value(12.0)
        mock_v.assert_awaited_once_with(12.0)

    # When hardware does not report protection limits, only setpoints are registered
    coordinator.data = ScpiPsuData(
        voltage_setpoint=5.0,
        current_setpoint=2.0,
        voltage_limit=None,
        current_limit=None,
    )
    partial_added: list[number.ScpiPsuNumber] = []
    await number.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: partial_added.extend(ents))
    )
    assert len(partial_added) == 2
    assert {e.entity_description.key for e in partial_added} == {
        "voltage_setpoint",
        "current_setpoint",
    }


async def test_number_entities_discovered_after_initial_failed_read(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test PSU",
        data={CONF_SERIAL_PORT: "socket://127.0.0.1:5025"},
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    coordinator.data = ScpiPsuData(voltage_setpoint=5.0, current_setpoint=2.0)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    added: list[number.ScpiPsuNumber] = []
    await number.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda entities: added.extend(entities))
    )
    assert {entity.entity_description.key for entity in added} == {
        "voltage_setpoint",
        "current_setpoint",
    }

    coordinator.data.voltage_limit = 30.0
    coordinator.async_set_updated_data(coordinator.data)
    assert [entity.entity_description.key for entity in added].count("overvoltage_limit") == 1
    assert "overcurrent_limit" not in {entity.entity_description.key for entity in added}

    coordinator.data.current_limit = 5.0
    coordinator.async_set_updated_data(coordinator.data)
    coordinator.async_set_updated_data(coordinator.data)
    assert {entity.entity_description.key for entity in added} == {
        "voltage_setpoint",
        "current_setpoint",
        "overvoltage_limit",
        "overcurrent_limit",
    }
    assert len(added) == 4


def test_master_output_requires_all_channels_to_be_known_for_off() -> None:
    data = ScpiPsuData(
        channel_count=2, channels={1: ChannelTelemetry(channel=1, output_enabled=False)}
    )
    assert data.all_outputs_enabled is None
    data.channels[2] = ChannelTelemetry(channel=2, output_enabled=True)
    assert data.all_outputs_enabled is True
    data.channels[2].output_enabled = False
    assert data.all_outputs_enabled is False
