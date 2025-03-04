"""Tests for the Rituals Perfume Genie binary sensor platform."""
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.rituals_perfume_genie.binary_sensor import CHARGING_SUFFIX
from homeassistant.const import ATTR_DEVICE_CLASS, ENTITY_CATEGORY_DIAGNOSTIC, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry

from .common import (
    init_integration,
    mock_config_entry,
    mock_diffuser_v1_battery_cartridge,
)


async def test_binary_sensors(hass: HomeAssistant) -> None:
    """Test the creation and values of the Rituals Perfume Genie binary sensor."""
    config_entry = mock_config_entry(unique_id="binary_sensor_test_diffuser_v1")
    diffuser = mock_diffuser_v1_battery_cartridge()
    await init_integration(hass, config_entry, [diffuser])
    registry = entity_registry.async_get(hass)
    hublot = diffuser.hublot

    state = hass.states.get("binary_sensor.genie_battery_charging")
    assert state
    assert state.state == STATE_ON
    assert (
        state.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.BATTERY_CHARGING
    )

    entry = registry.async_get("binary_sensor.genie_battery_charging")
    assert entry
    assert entry.unique_id == f"{hublot}{CHARGING_SUFFIX}"
    assert entry.entity_category == ENTITY_CATEGORY_DIAGNOSTIC
