"""Support for Netatmo Smart thermostats."""
from __future__ import annotations

import logging
from typing import Any, cast

import pyatmo
import voluptuous as vol

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    CURRENT_HVAC_HEAT,
    CURRENT_HVAC_IDLE,
    DEFAULT_MIN_TEMP,
    HVAC_MODE_AUTO,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    PRESET_AWAY,
    PRESET_BOOST,
    SUPPORT_PRESET_MODE,
    SUPPORT_TARGET_TEMPERATURE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_BATTERY_LEVEL,
    ATTR_SUGGESTED_AREA,
    ATTR_TEMPERATURE,
    PRECISION_HALVES,
    STATE_OFF,
    TEMP_CELSIUS,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import async_get_registry
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_HEATING_POWER_REQUEST,
    ATTR_SCHEDULE_NAME,
    ATTR_SELECTED_SCHEDULE,
    DATA_DEVICE_IDS,
    DATA_HANDLER,
    DATA_HOMES,
    DATA_SCHEDULES,
    DOMAIN,
    EVENT_TYPE_CANCEL_SET_POINT,
    EVENT_TYPE_SCHEDULE,
    EVENT_TYPE_SET_POINT,
    EVENT_TYPE_THERM_MODE,
    MANUFACTURER,
    SERVICE_SET_SCHEDULE,
    SIGNAL_NAME,
    TYPE_ENERGY,
)
from .data_handler import (
    HOMEDATA_DATA_CLASS_NAME,
    HOMESTATUS_DATA_CLASS_NAME,
    NetatmoDataHandler,
)
from .helper import get_all_home_ids, update_climate_schedules
from .netatmo_entity_base import NetatmoBase

_LOGGER = logging.getLogger(__name__)

PRESET_FROST_GUARD = "Frost Guard"
PRESET_SCHEDULE = "Schedule"
PRESET_MANUAL = "Manual"

SUPPORT_FLAGS = SUPPORT_TARGET_TEMPERATURE | SUPPORT_PRESET_MODE
SUPPORT_HVAC = [HVAC_MODE_HEAT, HVAC_MODE_AUTO, HVAC_MODE_OFF]
SUPPORT_PRESET = [PRESET_AWAY, PRESET_BOOST, PRESET_FROST_GUARD, PRESET_SCHEDULE]

STATE_NETATMO_SCHEDULE = "schedule"
STATE_NETATMO_HG = "hg"
STATE_NETATMO_MAX = "max"
STATE_NETATMO_AWAY = PRESET_AWAY
STATE_NETATMO_OFF = STATE_OFF
STATE_NETATMO_MANUAL = "manual"
STATE_NETATMO_HOME = "home"

PRESET_MAP_NETATMO = {
    PRESET_FROST_GUARD: STATE_NETATMO_HG,
    PRESET_BOOST: STATE_NETATMO_MAX,
    PRESET_SCHEDULE: STATE_NETATMO_SCHEDULE,
    PRESET_AWAY: STATE_NETATMO_AWAY,
    STATE_NETATMO_OFF: STATE_NETATMO_OFF,
}

NETATMO_MAP_PRESET = {
    STATE_NETATMO_HG: PRESET_FROST_GUARD,
    STATE_NETATMO_MAX: PRESET_BOOST,
    STATE_NETATMO_SCHEDULE: PRESET_SCHEDULE,
    STATE_NETATMO_AWAY: PRESET_AWAY,
    STATE_NETATMO_OFF: STATE_NETATMO_OFF,
    STATE_NETATMO_MANUAL: STATE_NETATMO_MANUAL,
    STATE_NETATMO_HOME: PRESET_SCHEDULE,
}

HVAC_MAP_NETATMO = {
    PRESET_SCHEDULE: HVAC_MODE_AUTO,
    STATE_NETATMO_HG: HVAC_MODE_AUTO,
    PRESET_FROST_GUARD: HVAC_MODE_AUTO,
    PRESET_BOOST: HVAC_MODE_HEAT,
    STATE_NETATMO_OFF: HVAC_MODE_OFF,
    STATE_NETATMO_MANUAL: HVAC_MODE_AUTO,
    PRESET_MANUAL: HVAC_MODE_AUTO,
    STATE_NETATMO_AWAY: HVAC_MODE_AUTO,
}

CURRENT_HVAC_MAP_NETATMO = {True: CURRENT_HVAC_HEAT, False: CURRENT_HVAC_IDLE}

DEFAULT_MAX_TEMP = 30

NA_THERM = "NATherm1"
NA_VALVE = "NRV"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Netatmo energy platform."""
    data_handler = hass.data[DOMAIN][entry.entry_id][DATA_HANDLER]

    await data_handler.register_data_class(
        HOMEDATA_DATA_CLASS_NAME, HOMEDATA_DATA_CLASS_NAME, None
    )
    home_data = data_handler.data.get(HOMEDATA_DATA_CLASS_NAME)

    if not home_data or home_data.raw_data == {}:
        raise PlatformNotReady

    entities = []
    for home_id in get_all_home_ids(home_data):
        for room_id in home_data.rooms[home_id]:
            signal_name = f"{HOMESTATUS_DATA_CLASS_NAME}-{home_id}"
            await data_handler.register_data_class(
                HOMESTATUS_DATA_CLASS_NAME, signal_name, None, home_id=home_id
            )
            home_status = data_handler.data.get(signal_name)
            if home_status and room_id in home_status.rooms:
                entities.append(NetatmoThermostat(data_handler, home_id, room_id))

    hass.data[DOMAIN][DATA_SCHEDULES].update(
        update_climate_schedules(
            home_ids=get_all_home_ids(home_data),
            schedules=data_handler.data[HOMEDATA_DATA_CLASS_NAME].schedules,
        )
    )

    hass.data[DOMAIN][DATA_HOMES] = {
        home_id: home_data.get("name")
        for home_id, home_data in (
            data_handler.data[HOMEDATA_DATA_CLASS_NAME].homes.items()
        )
    }

    _LOGGER.debug("Adding climate devices %s", entities)
    async_add_entities(entities, True)

    platform = entity_platform.async_get_current_platform()

    if home_data is not None:
        platform.async_register_entity_service(
            SERVICE_SET_SCHEDULE,
            {vol.Required(ATTR_SCHEDULE_NAME): cv.string},
            "_async_service_set_schedule",
        )


class NetatmoThermostat(NetatmoBase, ClimateEntity):
    """Representation a Netatmo thermostat."""

    _attr_hvac_mode = HVAC_MODE_AUTO
    _attr_hvac_modes = [HVAC_MODE_AUTO, HVAC_MODE_HEAT]
    _attr_max_temp = DEFAULT_MAX_TEMP
    _attr_preset_modes = SUPPORT_PRESET
    _attr_target_temperature_step = PRECISION_HALVES
    _attr_temperature_unit = TEMP_CELSIUS

    def __init__(
        self, data_handler: NetatmoDataHandler, home_id: str, room_id: str
    ) -> None:
        """Initialize the sensor."""
        ClimateEntity.__init__(self)
        super().__init__(data_handler)

        self._id = room_id
        self._home_id = home_id

        self._home_status_class = f"{HOMESTATUS_DATA_CLASS_NAME}-{self._home_id}"

        self._data_classes.extend(
            [
                {
                    "name": HOMEDATA_DATA_CLASS_NAME,
                    SIGNAL_NAME: HOMEDATA_DATA_CLASS_NAME,
                },
                {
                    "name": HOMESTATUS_DATA_CLASS_NAME,
                    "home_id": self._home_id,
                    SIGNAL_NAME: self._home_status_class,
                },
            ]
        )

        self._home_status = self.data_handler.data[self._home_status_class]
        self._room_status = self._home_status.rooms[room_id]
        self._room_data: dict = self._data.rooms[home_id][room_id]

        self._model: str = NA_VALVE
        for module in self._room_data.get("module_ids", []):
            if self._home_status.thermostats.get(module):
                self._model = NA_THERM
                break

        self._netatmo_type = TYPE_ENERGY

        self._device_name = self._data.rooms[home_id][room_id]["name"]
        self._attr_name = f"{MANUFACTURER} {self._device_name}"
        self._away: bool | None = None
        self._support_flags = SUPPORT_FLAGS
        self._battery_level = None
        self._connected: bool | None = None

        self._away_temperature: float | None = None
        self._hg_temperature: float | None = None
        self._boilerstatus: bool | None = None
        self._setpoint_duration = None
        self._selected_schedule = None

        if self._model == NA_THERM:
            self._attr_hvac_modes.append(HVAC_MODE_OFF)

        self._attr_unique_id = f"{self._id}-{self._model}"

    async def async_added_to_hass(self) -> None:
        """Entity created."""
        await super().async_added_to_hass()

        for event_type in (
            EVENT_TYPE_SET_POINT,
            EVENT_TYPE_THERM_MODE,
            EVENT_TYPE_CANCEL_SET_POINT,
            EVENT_TYPE_SCHEDULE,
        ):
            self.data_handler.config_entry.async_on_unload(
                async_dispatcher_connect(
                    self.hass,
                    f"signal-{DOMAIN}-webhook-{event_type}",
                    self.handle_event,
                )
            )

        registry = await async_get_registry(self.hass)
        device = registry.async_get_device({(DOMAIN, self._id)}, set())
        assert device
        self.hass.data[DOMAIN][DATA_DEVICE_IDS][self._home_id] = device.id

    @callback
    def handle_event(self, event: dict) -> None:
        """Handle webhook events."""
        data = event["data"]

        if self._home_id != data["home_id"]:
            return

        if data["event_type"] == EVENT_TYPE_SCHEDULE and "schedule_id" in data:
            self._selected_schedule = self.hass.data[DOMAIN][DATA_SCHEDULES][
                self._home_id
            ].get(data["schedule_id"])
            self._attr_extra_state_attributes.update(
                {"selected_schedule": self._selected_schedule}
            )
            self.async_write_ha_state()
            self.data_handler.async_force_update(self._home_status_class)
            return

        home = data["home"]

        if self._home_id != home["id"]:
            return

        if data["event_type"] == EVENT_TYPE_THERM_MODE:
            self._attr_preset_mode = NETATMO_MAP_PRESET[home[EVENT_TYPE_THERM_MODE]]
            self._attr_hvac_mode = HVAC_MAP_NETATMO[self._attr_preset_mode]
            if self._attr_preset_mode == PRESET_FROST_GUARD:
                self._attr_target_temperature = self._hg_temperature
            elif self._attr_preset_mode == PRESET_AWAY:
                self._attr_target_temperature = self._away_temperature
            elif self._attr_preset_mode == PRESET_SCHEDULE:
                self.async_update_callback()
                self.data_handler.async_force_update(self._home_status_class)
            self.async_write_ha_state()
            return

        for room in home.get("rooms", []):
            if data["event_type"] == EVENT_TYPE_SET_POINT and self._id == room["id"]:
                if room["therm_setpoint_mode"] == STATE_NETATMO_OFF:
                    self._attr_hvac_mode = HVAC_MODE_OFF
                    self._attr_preset_mode = STATE_NETATMO_OFF
                    self._attr_target_temperature = 0
                elif room["therm_setpoint_mode"] == STATE_NETATMO_MAX:
                    self._attr_hvac_mode = HVAC_MODE_HEAT
                    self._attr_preset_mode = PRESET_MAP_NETATMO[PRESET_BOOST]
                    self._attr_target_temperature = DEFAULT_MAX_TEMP
                elif room["therm_setpoint_mode"] == STATE_NETATMO_MANUAL:
                    self._attr_hvac_mode = HVAC_MODE_HEAT
                    self._attr_target_temperature = room["therm_setpoint_temperature"]
                else:
                    self._attr_target_temperature = room["therm_setpoint_temperature"]
                    if self._attr_target_temperature == DEFAULT_MAX_TEMP:
                        self._attr_hvac_mode = HVAC_MODE_HEAT
                self.async_write_ha_state()
                return

            if (
                data["event_type"] == EVENT_TYPE_CANCEL_SET_POINT
                and self._id == room["id"]
            ):
                self.async_update_callback()
                self.async_write_ha_state()
                return

    @property
    def _data(self) -> pyatmo.AsyncHomeData:
        """Return data for this entity."""
        return cast(
            pyatmo.AsyncHomeData, self.data_handler.data[self._data_classes[0]["name"]]
        )

    @property
    def supported_features(self) -> int:
        """Return the list of supported features."""
        return self._support_flags

    @property
    def hvac_action(self) -> str | None:
        """Return the current running hvac operation if supported."""
        if self._model == NA_THERM and self._boilerstatus is not None:
            return CURRENT_HVAC_MAP_NETATMO[self._boilerstatus]
        # Maybe it is a valve
        if self._room_status and self._room_status.get("heating_power_request", 0) > 0:
            return CURRENT_HVAC_HEAT
        return CURRENT_HVAC_IDLE

    async def async_set_hvac_mode(self, hvac_mode: str) -> None:
        """Set new target hvac mode."""
        if hvac_mode == HVAC_MODE_OFF:
            await self.async_turn_off()
        elif hvac_mode == HVAC_MODE_AUTO:
            if self.hvac_mode == HVAC_MODE_OFF:
                await self.async_turn_on()
            await self.async_set_preset_mode(PRESET_SCHEDULE)
        elif hvac_mode == HVAC_MODE_HEAT:
            await self.async_set_preset_mode(PRESET_BOOST)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        if self.hvac_mode == HVAC_MODE_OFF:
            await self.async_turn_on()

        if self.target_temperature == 0:
            await self._home_status.async_set_room_thermpoint(
                self._id,
                STATE_NETATMO_HOME,
            )

        if (
            preset_mode in (PRESET_BOOST, STATE_NETATMO_MAX)
            and self._model == NA_VALVE
            and self.hvac_mode == HVAC_MODE_HEAT
        ):
            await self._home_status.async_set_room_thermpoint(
                self._id,
                STATE_NETATMO_HOME,
            )
        elif (
            preset_mode in (PRESET_BOOST, STATE_NETATMO_MAX) and self._model == NA_VALVE
        ):
            await self._home_status.async_set_room_thermpoint(
                self._id,
                STATE_NETATMO_MANUAL,
                DEFAULT_MAX_TEMP,
            )
        elif (
            preset_mode in (PRESET_BOOST, STATE_NETATMO_MAX)
            and self.hvac_mode == HVAC_MODE_HEAT
        ):
            await self._home_status.async_set_room_thermpoint(
                self._id, STATE_NETATMO_HOME
            )
        elif preset_mode in (PRESET_BOOST, STATE_NETATMO_MAX):
            await self._home_status.async_set_room_thermpoint(
                self._id, PRESET_MAP_NETATMO[preset_mode]
            )
        elif preset_mode in (PRESET_SCHEDULE, PRESET_FROST_GUARD, PRESET_AWAY):
            await self._home_status.async_set_thermmode(PRESET_MAP_NETATMO[preset_mode])
        else:
            _LOGGER.error("Preset mode '%s' not available", preset_mode)

        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature for 2 hours."""
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        await self._home_status.async_set_room_thermpoint(
            self._id, STATE_NETATMO_MANUAL, min(temp, DEFAULT_MAX_TEMP)
        )

        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn the entity off."""
        if self._model == NA_VALVE:
            await self._home_status.async_set_room_thermpoint(
                self._id,
                STATE_NETATMO_MANUAL,
                DEFAULT_MIN_TEMP,
            )
        elif self.hvac_mode != HVAC_MODE_OFF:
            await self._home_status.async_set_room_thermpoint(
                self._id, STATE_NETATMO_OFF
            )
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Turn the entity on."""
        await self._home_status.async_set_room_thermpoint(self._id, STATE_NETATMO_HOME)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """If the device hasn't been able to connect, mark as unavailable."""
        return bool(self._connected)

    @callback
    def async_update_callback(self) -> None:
        """Update the entity's state."""
        self._home_status = self.data_handler.data[self._home_status_class]
        if self._home_status is None:
            if self.available:
                self._connected = False
            return

        self._room_status = self._home_status.rooms.get(self._id)
        self._room_data = self._data.rooms.get(self._home_id, {}).get(self._id, {})

        if not self._room_status or not self._room_data:
            if self._connected:
                _LOGGER.info(
                    "The thermostat in room %s seems to be out of reach",
                    self._device_name,
                )

            self._connected = False
            return

        roomstatus = {"roomID": self._room_status.get("id", {})}
        if self._room_status.get("reachable"):
            roomstatus.update(self._build_room_status())

        self._away_temperature = self._data.get_away_temp(self._home_id)
        self._hg_temperature = self._data.get_hg_temp(self._home_id)
        self._setpoint_duration = self._data.setpoint_duration[self._home_id]
        self._selected_schedule = roomstatus.get("selected_schedule")

        if "current_temperature" not in roomstatus:
            return

        self._attr_current_temperature = roomstatus["current_temperature"]
        self._attr_target_temperature = roomstatus["target_temperature"]
        self._attr_preset_mode = NETATMO_MAP_PRESET[roomstatus["setpoint_mode"]]
        self._attr_hvac_mode = HVAC_MAP_NETATMO[self._attr_preset_mode]
        self._battery_level = roomstatus.get("battery_state")
        self._connected = True

        self._away = self._attr_hvac_mode == HVAC_MAP_NETATMO[STATE_NETATMO_AWAY]

        if self._battery_level is not None:
            self._attr_extra_state_attributes[ATTR_BATTERY_LEVEL] = self._battery_level

        if self._model == NA_VALVE:
            self._attr_extra_state_attributes[
                ATTR_HEATING_POWER_REQUEST
            ] = self._room_status.get("heating_power_request", 0)

        if self._selected_schedule is not None:
            self._attr_extra_state_attributes[
                ATTR_SELECTED_SCHEDULE
            ] = self._selected_schedule

    def _build_room_status(self) -> dict:
        """Construct room status."""
        try:
            roomstatus = {
                "roomname": self._room_data["name"],
                "target_temperature": self._room_status["therm_setpoint_temperature"],
                "setpoint_mode": self._room_status["therm_setpoint_mode"],
                "current_temperature": self._room_status["therm_measured_temperature"],
                "module_type": self._data.get_thermostat_type(
                    home_id=self._home_id, room_id=self._id
                ),
                "module_id": None,
                "heating_status": None,
                "heating_power_request": None,
                "selected_schedule": self._data._get_selected_schedule(  # pylint: disable=protected-access
                    home_id=self._home_id
                ).get(
                    "name"
                ),
            }

            batterylevel = None
            for module_id in self._room_data["module_ids"]:
                if (
                    self._data.modules[self._home_id][module_id]["type"] == NA_THERM
                    or roomstatus["module_id"] is None
                ):
                    roomstatus["module_id"] = module_id
            if roomstatus["module_type"] == NA_THERM:
                self._boilerstatus = self._home_status.boiler_status(
                    roomstatus["module_id"]
                )
                roomstatus["heating_status"] = self._boilerstatus
                batterylevel = self._home_status.thermostats[
                    roomstatus["module_id"]
                ].get("battery_state")
            elif roomstatus["module_type"] == NA_VALVE:
                roomstatus["heating_power_request"] = self._room_status[
                    "heating_power_request"
                ]
                roomstatus["heating_status"] = roomstatus["heating_power_request"] > 0
                if self._boilerstatus is not None:
                    roomstatus["heating_status"] = (
                        self._boilerstatus and roomstatus["heating_status"]
                    )
                batterylevel = self._home_status.valves[roomstatus["module_id"]].get(
                    "battery_state"
                )

            if batterylevel:
                roomstatus["battery_state"] = batterylevel

            return roomstatus

        except KeyError as err:
            _LOGGER.error("Update of room %s failed. Error: %s", self._id, err)

        return {}

    async def _async_service_set_schedule(self, **kwargs: Any) -> None:
        schedule_name = kwargs.get(ATTR_SCHEDULE_NAME)
        schedule_id = None
        for sid, name in self.hass.data[DOMAIN][DATA_SCHEDULES][self._home_id].items():
            if name == schedule_name:
                schedule_id = sid

        if not schedule_id:
            _LOGGER.error("%s is not a valid schedule", kwargs.get(ATTR_SCHEDULE_NAME))
            return

        await self._data.async_switch_home_schedule(
            home_id=self._home_id, schedule_id=schedule_id
        )
        _LOGGER.debug(
            "Setting %s schedule to %s (%s)",
            self._home_id,
            kwargs.get(ATTR_SCHEDULE_NAME),
            schedule_id,
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info for the thermostat."""
        device_info: DeviceInfo = super().device_info
        device_info[ATTR_SUGGESTED_AREA] = self._room_data["name"]
        return device_info
