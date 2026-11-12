"""HYSPLIT trajectory meteorology flags and verified tdump labels.

The exact 8-character diagnostic labels were verified with Windows
hyts_std.exe and ERA5-derived ARL. Units from the official NOAA user guide:
https://hysplit2.arl.noaa.gov/hysplitusersguide/S614.htm

Pressure (TM_PRES) and terrain elevation (TM_TERR) remain on so existing
CSV columns and estimated MSL/geopotential heights remain available.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class MeteorologicalVariable:
    flag: str
    japanese_label: str
    unit: str
    tdump_label: str
    csv_column: str


MET_VARIABLES = (
    MeteorologicalVariable(
        "TM_TPOT", "温位", "K", "THETA", "potential_temperature_k"),
    MeteorologicalVariable(
        "TM_TAMB", "気温", "K", "AIR_TEMP", "air_temperature_k"),
    MeteorologicalVariable(
        "TM_RELH", "相対湿度", "%", "RELHUMID", "relative_humidity_percent"),
    MeteorologicalVariable(
        "TM_SPHU", "比湿", "g/kg（湿潤空気）", "SPCHUMID",
        "specific_humidity_g_per_kg_moist_air"),
    MeteorologicalVariable(
        "TM_MIXR", "水蒸気混合比", "g/kg（乾燥空気）", "H2OMIXRA",
        "water_vapor_mixing_ratio_g_per_kg_dry_air"),
    MeteorologicalVariable(
        "TM_UWND", "東西風速 U", "m/s", "UWIND", "eastward_wind_m_s"),
    MeteorologicalVariable(
        "TM_VWND", "南北風速 V", "m/s", "VWIND", "northward_wind_m_s"),
    MeteorologicalVariable(
        "TM_MIXD", "混合層高度", "m", "MIXDEPTH", "mixing_layer_depth_m"),
)

_BY_FLAG = {variable.flag: variable for variable in MET_VARIABLES}
DEFAULT_MET_VARIABLES = tuple(variable.flag for variable in MET_VARIABLES)


def selected_variables(flags: Iterable[str] = ()) -> tuple[MeteorologicalVariable, ...]:
    """Return configured diagnostics in a stable CSV column order.

    Invalid or duplicate choices are rejected before running HYSPLIT.
    """
    requested = tuple(flags)
    if len(set(requested)) != len(requested):
        raise ValueError("Duplicate HYSPLIT meteorological output selection")
    unknown = sorted(set(requested) - set(_BY_FLAG))
    if unknown:
        raise ValueError(f"Unknown HYSPLIT meteorological output flags: {unknown}")
    return tuple(option for option in MET_VARIABLES if option.flag in requested)


def flags_for_setup(flags: Iterable[str] = ()) -> str:
    """Generate optional HYSPLIT namelist entries for selected diagnostics."""
    return "".join(f" {option.flag} = 1,\n" for option in selected_variables(flags))

