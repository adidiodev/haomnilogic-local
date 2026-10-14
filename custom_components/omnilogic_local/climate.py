"""Climate (thermostat) platform for OmniLogic virtual heaters.

Added locally on top of cryptk/haomnilogic-local. Exposes each virtual heater
as a ClimateEntity so a heat/cool-capable unit (e.g. the HP31405T heat pump)
can be driven Heat / Cool / Auto from Home Assistant, alongside the setpoint.

Mode mapping (HVACMode <-> library HeaterMode):
    OFF        -> virtual heater disabled (turn_off)
    HEAT       -> HeaterMode.HEATING
    COOL       -> HeaterMode.COOLING   (only offered if a unit SupportsCooling)
    HEAT_COOL  -> HeaterMode.AUTO      (only offered if a unit SupportsCooling)

The Heater model doesn't wrap the set-mode command, so mode writes go through
the API layer (async_set_heater_mode), the same way the model's own turn_on/off
reach self._api internally.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from pyomnilogic_local import Heater
from pyomnilogic_local.omnitypes import HeaterMode, HeaterState

from .const import DOMAIN, KEY_COORDINATOR
from .entity import OmniLogicEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import OmniLogicCoordinator

_LOGGER = logging.getLogger(__name__)

_MODE_TO_HVAC: dict[HeaterMode, HVACMode] = {
    HeaterMode.HEATING: HVACMode.HEAT,
    HeaterMode.COOLING: HVACMode.COOL,
    HeaterMode.AUTO: HVACMode.HEAT_COOL,
}
_HVAC_TO_MODE: dict[HVACMode, HeaterMode] = {v: k for k, v in _MODE_TO_HVAC.items()}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the climate platform."""
    coordinator: OmniLogicCoordinator = hass.data[DOMAIN][entry.entry_id][KEY_COORDINATOR]
    entities: list[OmniLogicClimateEntity] = []

    for _, _, heater in coordinator.omni.all_heaters.items():
        entities.append(OmniLogicClimateEntity(coordinator=coordinator, equipment=heater))

    async_add_entities(entities)


class OmniLogicClimateEntity(OmniLogicEntity[Heater], ClimateEntity):
    """Thermostat-style climate entity for an OmniLogic virtual heater."""

    _attr_name = "Heat Pump"
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
    )
    # This entity is authored against the modern turn_on/off contract.
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, coordinator: OmniLogicCoordinator, equipment: Heater) -> None:
        super().__init__(coordinator, equipment)

    @property
    def temperature_unit(self) -> str:
        # Heaters always return Fahrenheit regardless of system units (issue #96).
        return UnitOfTemperature.FAHRENHEIT

    @property
    def _cooling_capable(self) -> bool:
        """True when any heater unit under this virtual heater supports cooling."""
        try:
            return any(
                getattr(equip, "supports_cooling", None) is True for _, _, equip in self.equipment.heater_equipment.items()
            )
        except Exception:  # noqa: BLE001 - defensive against model shape changes
            return False

    @property
    def hvac_modes(self) -> list[HVACMode]:
        modes = [HVACMode.OFF, HVACMode.HEAT]
        if self._cooling_capable:
            modes += [HVACMode.COOL, HVACMode.HEAT_COOL]
        return modes

    @property
    def hvac_mode(self) -> HVACMode:
        if not self.equipment.is_on:
            return HVACMode.OFF
        return _MODE_TO_HVAC.get(self.equipment.mode, HVACMode.HEAT)

    @property
    def hvac_action(self) -> HVACAction:
        if not self.equipment.is_on:
            return HVACAction.OFF
        running = False
        try:
            running = any(equip.state == HeaterState.ON for _, _, equip in self.equipment.heater_equipment.items())
        except Exception:  # noqa: BLE001
            running = False
        if not running:
            return HVACAction.IDLE
        mode = self.equipment.mode
        if mode == HeaterMode.COOLING:
            return HVACAction.COOLING
        if mode == HeaterMode.HEATING:
            return HVACAction.HEATING
        # AUTO: infer direction from current vs target water temp.
        current = self.current_temperature
        target = self.target_temperature
        if current is not None and target is not None and current > target:
            return HVACAction.COOLING
        return HVACAction.HEATING

    @property
    def min_temp(self) -> float:
        return self.equipment.min_temp

    @property
    def max_temp(self) -> float:
        return self.equipment.max_temp

    @property
    def target_temperature(self) -> float | None:
        return self.equipment.current_set_point

    @property
    def current_temperature(self) -> float | None:
        if self.equipment.bow_id is None:
            return None
        bow = self.coordinator.omni.all_bows.get(self.equipment.bow_id)
        if bow is None:
            return None
        current_temp = bow.water_temp
        return current_temp if current_temp != -1 else None

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature."""
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        await self.equipment.set_temperature(int(temp))
        self.coordinator.do_next_refresh_after()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set heat / cool / auto / off."""
        if hvac_mode == HVACMode.OFF:
            await self.equipment.turn_off()
            self.coordinator.do_next_refresh_after()
            return

        mode = _HVAC_TO_MODE.get(hvac_mode)
        if mode is None:
            raise ValueError(f"Unsupported hvac_mode: {hvac_mode}")

        # Make sure the virtual heater is enabled before selecting a running mode.
        if not self.equipment.is_on:
            await self.equipment.turn_on()
        # The Heater model has no mode setter; go through the API layer like its
        # own turn_on/turn_off do internally.
        await self.equipment._api.async_set_heater_mode(self.bow_id, self.system_id, mode)
        self.coordinator.do_next_refresh_after()

    async def async_turn_on(self) -> None:
        await self.equipment.turn_on()
        self.coordinator.do_next_refresh_after()

    async def async_turn_off(self) -> None:
        await self.equipment.turn_off()
        self.coordinator.do_next_refresh_after()

    @property
    def _extra_state_attributes(self) -> dict[str, Any]:
        # Virtual-heater-level attributes, plus a block per physical unit. Mirrors
        # the stock water_heater exposure (so hiding it loses nothing) and adds
        # heater_type / supports_cooling, which distinguish e.g. the HP31405T heat
        # pump from the HDF400 gas heater once both are under the virtual heater.
        extra_state_attributes: dict[str, Any] = {
            "omni_solar_set_point": self.equipment.solar_set_point,
            "omni_why_on": self.equipment.why_on,
        }
        # EquipmentDict.items() yields (system_id, name, equipment)
        for system_id, _, heater_equip in self.equipment.heater_equipment.items():
            name = heater_equip.name or "unknown"
            prefix = f"omni_heater_equip_{name}_"
            extra_state_attributes |= {
                f"{prefix}_enabled": heater_equip.enabled,
                f"{prefix}_system_id": system_id,
                f"{prefix}_bow_id": heater_equip.bow_id,
                f"{prefix}_state": str(heater_equip.state),
                f"{prefix}_current_temp": heater_equip.current_temp,
                f"{prefix}_heater_type": str(heater_equip.heater_type),
                f"{prefix}_supports_cooling": heater_equip.supports_cooling,
            }
        return extra_state_attributes