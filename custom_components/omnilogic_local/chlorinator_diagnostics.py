"""Chlorinator cell diagnostics (cell temperature, voltage, current, board temperature, polarity).

Local addition to haomnilogic-local (adidiodev fork).

The OmniLogic telemetry packet only carries salt levels and status bits for the chlorinator.
The panel's "Chlorinator Diagnostics" screen data is available through a separate request,
``GetCHLORMeasurement``, which python-omnilogic-local does not implement. This module sends that
request itself through the library's transport and exposes the results as sensors.

Protocol notes (decoded from the Hayward OmniLogic app 5.2.16, confirmed against an OmniPL on
firmware R0502000, 2026-10-14):

* The controller dispatches on the XML ``<Name>``, not the UDP header message type. We send under
  header type 386 (GetUIFilterDiagnosticInfo, a read), which was verified to return
  ``GetCHLORMeasurementRsp``. The native request ID is 163.
* The response arrives as a LeadMessage whose ``SourceOpId`` is 1163. python-omnilogic-local 5.0.2
  validates SourceOpId against its MessageType enum and rejects it, so this module carries its own
  minimal UDP request/ACK/reassembly instead of using the library transport.
* Params: ``PoolID`` = body-of-water system id, ``ChlorID`` = Chlorinator-Equipment system id
  (the Chlorinator system id is ACKed but never answered).
* Values come back as High/Low byte pairs: raw = high * 256 + low.
* Scaling is taken from the app: voltage = raw * 5 / 255 * 8.15, current = raw * 5 / 255 / 0.489,
  temperatures = thermistor lookup table below (tenths of degF, raw // 8 indexes the table,
  raw % 8 interpolates). The app does not display board temperature; it is decoded with the same
  table as the cell, which gives plausible values but is an assumption.
* Relay polarity comes from a second request, ``GetCHLORRelayPolarity`` (native ID 174), sent under
  the same header type with the same params. The response is a single message (op 1174) carrying
  ``RelaySetting``; the app shows 0 as "Off" (cell not generating) and n as "K<n>" (K1/K2).
  Confirmed against the app: K1 2026-10-18 19:27 and K2 2026-10-19 06:38 while generating,
  Off between cycles.

Changelog
    1.0.0 - 2026-10-14 - Initial version: cell temp, cell voltage, cell current, board temp.
    1.0.1 - 2026-10-14 - Own UDP transport: the library rejected the LeadMessage (SourceOpId 1163).
    1.1.0 - 2026-10-19 - Relay polarity sensor (GetCHLORRelayPolarity). A polarity failure only
                         makes that one sensor unavailable; the measurement sensors are unaffected.
"""

from __future__ import annotations

import asyncio
import logging
import random
import struct
import time
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfElectricCurrent, UnitOfElectricPotential, UnitOfTemperature
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator, UpdateFailed
from pyomnilogic_local.api.constants import XML_NAMESPACE

from .const import DOMAIN, MANUFACTURER

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import OmniLogicCoordinator

_LOGGER = logging.getLogger(__name__)

DIAG_SCAN_INTERVAL = timedelta(seconds=60)

# Header message type the request is sent under (see module docstring).
REQUEST_MESSAGE_TYPE = 386

_HEADER_FORMAT = "!LQ4sLBBBB"
_HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)
_PROTOCOL_VERSION = b"1.19"
_MSG_ACK = 0
_MSG_MSP_ACK = 1002
_MSG_LEAD = 1998
_MSG_BLOCK = 1999
_BLOCK_HEADER_OFFSET = 8
_ACK_TIMEOUT = 4.0
_RESPONSE_TIMEOUT = 10.0
_SEND_ATTEMPTS = 3
_ACK_BODY = f'<Request xmlns="{XML_NAMESPACE}">\n<Name>Ack</Name>\n</Request>'
REQUEST_NAME = "GetCHLORMeasurement"
RESPONSE_NAME = "GetCHLORMeasurementRsp"
POLARITY_REQUEST_NAME = "GetCHLORRelayPolarity"
POLARITY_RESPONSE_NAME = "GetCHLORRelayPolarityRsp"
POLARITY_OPTIONS = ["Off", "K1", "K2"]

INVALID_RAW = 65535

# Thermistor table from the Hayward app (tenths of degF), index = raw // 8.
TEMP_TABLE_TENTHS_F: tuple[int, ...] = (
    8000, 3740, 3136, 2817, 2603, 2444, 2318, 2214, 2125, 2048, 1980, 1919, 1864, 1813, 1767, 1723,
    1683, 1645, 1610, 1576, 1544, 1514, 1485, 1458, 1431, 1406, 1381, 1358, 1335, 1313, 1291, 1270,
    1250, 1230, 1211, 1193, 1174, 1156, 1139, 1122, 1105, 1088, 1072, 1056, 1041, 1025, 1010, 995,
    980, 966, 951, 937, 923, 909, 895, 881, 868, 854, 841, 828, 815, 802,
    789, 776, 763, 750, 737, 725, 712, 699, 687, 674, 661, 649, 636, 624, 611, 598,
    586, 573, 560, 547, 534, 521, 508, 495, 482, 469, 455, 442, 428, 414, 400, 386,
    372, 357, 342, 327, 312, 297, 281, 265, 248, 231, 214, 196, 178, 159, 140, 120,
    99, 77, 54, 31, 6, -20, -48, -78, -110, -145, -182, -225, -272, -327, -392, -474, -586, -773,
)


def decode_temp_f(raw: int | None) -> float | None:
    """Convert a raw thermistor count to degF, mirroring the app's getDiagCellTempF."""
    if raw is None or raw <= 0 or raw >= INVALID_RAW:
        # The app shows 0 for raw 0; treat it as no reading.
        return None
    idx, frac = divmod(raw, 8)
    if idx >= len(TEMP_TABLE_TENTHS_F) - 1:
        return None
    value = TEMP_TABLE_TENTHS_F[idx]
    if frac > 0:
        step = TEMP_TABLE_TENTHS_F[idx] - TEMP_TABLE_TENTHS_F[idx + 1]
        value -= int((step * frac * 10 / 8 + 5) // 10)
    return value / 10


def decode_voltage(raw: int | None) -> float | None:
    if raw is None or raw < 0 or raw >= INVALID_RAW:
        return None
    return round(raw * 5 / 255 * 8.15, 2)


def decode_current(raw: int | None) -> float | None:
    if raw is None or raw < 0 or raw >= INVALID_RAW:
        return None
    return round(raw * 5 / 255 / 0.489, 2)


def decode_polarity(raw: int | None) -> str | None:
    """Mirror the app: 0 -> "Off", n -> "K<n>"; anything outside K1/K2 is reported as unknown."""
    if raw is None:
        return None
    value = "Off" if raw == 0 else f"K{raw}"
    return value if value in POLARITY_OPTIONS else None


def build_request(pool_id: int, chlor_id: int, name: str = REQUEST_NAME) -> str:
    body = ET.Element("Request", {"xmlns": XML_NAMESPACE})
    ET.SubElement(body, "Name").text = name
    params = ET.SubElement(body, "Parameters")
    for name, value in (("PoolID", pool_id), ("ChlorID", chlor_id)):
        ET.SubElement(params, "Parameter", name=name, dataType="int").text = str(value)
    return ET.tostring(body, xml_declaration=True, encoding="unicode")


def parse_response(xml: str, expected: str = RESPONSE_NAME) -> dict[str, int]:
    """Parse a chlorinator response into {base_name: raw}, combining High/Low byte pairs."""
    root = ET.fromstring(xml)
    ns = {"h": XML_NAMESPACE}
    name = root.findtext("h:Name", namespaces=ns) or root.findtext("Name")
    if name != expected:
        msg = f"Unexpected response {name!r}, expected {expected!r}"
        raise ValueError(msg)
    params: dict[str, int] = {}
    for param in root.iter(f"{{{XML_NAMESPACE}}}Parameter"):
        if param.text is not None:
            params[param.get("name", "")] = int(param.text)
    values: dict[str, int] = {}
    for key, val in params.items():
        if key.endswith("HighByte"):
            base = key[: -len("HighByte")]
            if f"{base}LowByte" in params:
                values[base] = val * 256 + params[f"{base}LowByte"]
        elif not key.endswith("LowByte"):
            values[key] = val
    return values



def _frame(msg_id: int, msg_type: int, body: str) -> bytes:
    header = struct.pack(_HEADER_FORMAT, msg_id, int(time.time()), _PROTOCOL_VERSION, msg_type, 0, 0, 0, 0)
    return header + (body + "\x00").encode("utf-8")


class _DiagProtocol(asyncio.DatagramProtocol):
    """Minimal OmniLogic UDP client: ACKs every non-ACK message and queues it."""

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.acked: set[int] = set()
        self.ack_event = asyncio.Event()
        self.queue: asyncio.Queue[tuple[int, int, bool, bytes]] = asyncio.Queue()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < _HEADER_SIZE:
            return
        msg_id, _ts, _ver, msg_type, _client, _r1, compressed, _r2 = struct.unpack(_HEADER_FORMAT, data[:_HEADER_SIZE])
        if msg_type in (_MSG_ACK, _MSG_MSP_ACK):
            self.acked.add(msg_id)
            self.ack_event.set()
            return
        if self.transport is not None:
            self.transport.sendto(_frame(msg_id, _MSG_ACK, _ACK_BODY))
        self.queue.put_nowait((msg_id, msg_type, bool(compressed), data[_HEADER_SIZE:]))


async def _async_request(host: str, port: int, body: str) -> str:
    """Send one XML request and return the reassembled XML response."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(_DiagProtocol, remote_addr=(host, port))
    try:
        for attempt in range(_SEND_ATTEMPTS):
            msg_id = random.randint(1, 2**31 - 1)
            protocol.ack_event.clear()
            transport.sendto(_frame(msg_id, REQUEST_MESSAGE_TYPE, body))
            try:
                async with asyncio.timeout(_ACK_TIMEOUT):
                    while msg_id not in protocol.acked:
                        await protocol.ack_event.wait()
                        protocol.ack_event.clear()
                break
            except TimeoutError:
                if attempt == _SEND_ATTEMPTS - 1:
                    raise
        async with asyncio.timeout(_RESPONSE_TIMEOUT):
            _, msg_type, compressed, payload = await protocol.queue.get()
            if msg_type == _MSG_LEAD:
                lead = ET.fromstring(payload.decode("utf-8").strip("\x00"))
                lead_params = {p.get("name"): int(p.text or 0) for p in lead.iter(f"{{{XML_NAMESPACE}}}Parameter")}
                size, blocks = lead_params.get("MsgSize", 0), lead_params.get("MsgBlockCount", 0)
                seen: set[int] = set()
                data = b""
                while len(seen) < blocks:
                    block_id, block_type, _, block_payload = await protocol.queue.get()
                    if block_type != _MSG_BLOCK or block_id in seen:
                        continue
                    seen.add(block_id)
                    data += block_payload[_BLOCK_HEADER_OFFSET:]
                payload = data[:size]
        if compressed:
            payload = zlib.decompress(payload)
        return payload.decode("utf-8").strip("\x00")
    finally:
        transport.close()


class ChlorinatorDiagnosticsCoordinator(DataUpdateCoordinator[dict[str, int] | None]):
    """Polls GetCHLORMeasurement for one chlorinator while its body of water has flow."""

    def __init__(self, hass: HomeAssistant, main: OmniLogicCoordinator, bow_id: int, chlor_equip_id: int) -> None:
        super().__init__(hass, _LOGGER, name=f"OmniLogic chlorinator {chlor_equip_id} diagnostics", update_interval=DIAG_SCAN_INTERVAL)
        self.main = main
        self.bow_id = bow_id
        self.chlor_equip_id = chlor_equip_id

    def _has_flow(self) -> bool:
        omni = self.main.omni
        if not omni.backyard.is_ready:
            return False
        bow = omni.get_equipment_by_id(self.bow_id)
        return bool(getattr(bow, "flow", False))

    async def _async_update_data(self) -> dict[str, int] | None:
        # No flow means no meaningful cell readings (and the cell is off), so skip the request.
        if not self._has_flow():
            return None
        api = self.main.omni._api
        try:
            resp = await _async_request(api.controller_ip, api.controller_port, build_request(self.bow_id, self.chlor_equip_id))
            values = parse_response(resp)
        except Exception as err:
            raise UpdateFailed(f"Chlorinator diagnostics request failed: {err}") from err
        # Polarity is best effort: without it only the polarity sensor goes unavailable.
        try:
            resp = await _async_request(
                api.controller_ip,
                api.controller_port,
                build_request(self.bow_id, self.chlor_equip_id, POLARITY_REQUEST_NAME),
            )
            polarity = parse_response(resp, POLARITY_RESPONSE_NAME)
            if "RelaySetting" in polarity:
                values["RelaySetting"] = polarity["RelaySetting"]
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Chlorinator relay polarity request failed: %s", err)
        return values


@dataclass(frozen=True, kw_only=True)
class ChlorDiagSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, int]], float | str | None]
    raw_key: str


DIAG_SENSORS: tuple[ChlorDiagSensorDescription, ...] = (
    ChlorDiagSensorDescription(
        key="chlorinator_cell_temperature",
        name="Chlorinator Cell Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        suggested_display_precision=1,
        raw_key="CellTemp",
        value_fn=lambda d: decode_temp_f(d.get("CellTemp")),
    ),
    ChlorDiagSensorDescription(
        key="chlorinator_cell_voltage",
        name="Chlorinator Cell Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        raw_key="Voltage",
        value_fn=lambda d: decode_voltage(d.get("Voltage")),
    ),
    ChlorDiagSensorDescription(
        key="chlorinator_cell_current",
        name="Chlorinator Cell Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        raw_key="Current",
        value_fn=lambda d: decode_current(d.get("Current")),
    ),
    ChlorDiagSensorDescription(
        key="chlorinator_board_temperature",
        name="Chlorinator Board Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        raw_key="BoardTemp",
        value_fn=lambda d: decode_temp_f(d.get("BoardTemp")),
    ),
    ChlorDiagSensorDescription(
        key="chlorinator_relay_polarity",
        name="Chlorinator Relay Polarity",
        device_class=SensorDeviceClass.ENUM,
        options=POLARITY_OPTIONS,
        icon="mdi:swap-horizontal",
        raw_key="RelaySetting",
        value_fn=lambda d: decode_polarity(d.get("RelaySetting")),
    ),
)


class ChlorinatorDiagnosticSensor(CoordinatorEntity[ChlorinatorDiagnosticsCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: ChlorDiagSensorDescription

    def __init__(self, coordinator: ChlorinatorDiagnosticsCoordinator, description: ChlorDiagSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.bow_id} {coordinator.chlor_equip_id} {description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, f"bow_{coordinator.bow_id}")}, manufacturer=MANUFACTURER)

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self.entity_description.raw_key in self.coordinator.data
        )

    @property
    def native_value(self) -> float | str | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        raw = self.coordinator.data.get(self.entity_description.raw_key) if self.coordinator.data else None
        return {"raw": raw, "omni_bow_id": self.coordinator.bow_id, "omni_system_id": self.coordinator.chlor_equip_id}


def async_add_chlorinator_diagnostic_sensors(
    hass: HomeAssistant, coordinator: OmniLogicCoordinator, async_add_entities: AddEntitiesCallback
) -> None:
    """Create a diagnostics coordinator and sensors for each main-panel chlorinator."""
    entities: list[ChlorinatorDiagnosticSensor] = []
    for _, _, chlor_equip in coordinator.omni.all_chlorinator_equipment.items():
        if chlor_equip.bow_id is None or chlor_equip.system_id is None:
            continue
        diag = ChlorinatorDiagnosticsCoordinator(hass, coordinator, chlor_equip.bow_id, chlor_equip.system_id)
        entities.extend(ChlorinatorDiagnosticSensor(diag, description) for description in DIAG_SENSORS)
        # First refresh in the background so a slow/failed request never blocks platform setup.
        hass.async_create_task(diag.async_refresh())
    async_add_entities(entities)
