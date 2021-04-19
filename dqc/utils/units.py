from typing import Union, Optional, overload, Dict, Callable
import torch

# This file contains various physical constants and functions to convert units
# from the atomic units

__all__ = ["length_to", "time_to", "freq_to", "edipole_to", "equadrupole_to"]

LIGHT_SPEED = 2.99792458e8  # m/s
BOHR = 5.29177210903e-11  # m
TIME = 2.4188843265857e-17  # s
DEBYE = 2.541746473  # Debye

PhysVarType = torch.Tensor
UnitType = Optional[str]

_length_converter = {
    "a": BOHR * 1e10,
    "angst": BOHR * 1e10,
    "angstrom": BOHR * 1e10,
    "m": BOHR,
    "cm": BOHR * 1e2,
}

_freq_converter = {
    "cm-1": 1e-2 / TIME / LIGHT_SPEED,
    "cm^-1": 1e-2 / TIME / LIGHT_SPEED,
    "hz": 1.0 / TIME,
    "mhz": 1e-6 / TIME,
    "ghz": 1e-9 / TIME,
    "thz": 1e-12 / TIME,
}

_time_converter = {
    "s": TIME,
    "us": TIME / 1e-6,
    "ns": TIME / 1e-9,
}

_edipole_converter = {
    "d": DEBYE,
    "debye": DEBYE,
    "cm": DEBYE,
}

_equadrupole_converter = {
    # TODO: fill this in
}

def _avail_keys(converter: Dict[str, float]) -> str:
    # returns the available keys in a string of list of string
    return str(list(_length_converter.keys()))

def _add_docstr_to(phys: str, converter: Dict[str, float]) -> Callable:
    # automatically add docstring for converter functions

    def decorator(callable: Callable):
        callable.__doc__ = f"""
            Convert the {phys} from atomic unit to the given unit.
            Available units are (case-insensitive): {_avail_keys(converter)}
        """
        return callable
    return decorator

@_add_docstr_to("time", _freq_converter)
def time_to(a: PhysVarType, unit: UnitType) -> PhysVarType:
    # convert unit time from atomic unit to the given unit
    return _converter_to(a, unit, _time_converter)

@_add_docstr_to("frequency", _freq_converter)
def freq_to(a: PhysVarType, unit: UnitType) -> PhysVarType:
    # convert unit frequency from atomic unit to the given unit
    return _converter_to(a, unit, _freq_converter)

@_add_docstr_to("length", _length_converter)
def length_to(a: PhysVarType, unit: UnitType) -> PhysVarType:
    # convert unit length from atomic unit to the given unit
    return _converter_to(a, unit, _length_converter)

@_add_docstr_to("electric dipole", _edipole_converter)
def edipole_to(a: PhysVarType, unit: UnitType) -> PhysVarType:
    # convert unit electric dipole from atomic unit to the given unit
    return _converter_to(a, unit, _edipole_converter)

@_add_docstr_to("electric quadrupole", _equadrupole_converter)
def equadrupole_to(a: PhysVarType, unit: UnitType) -> PhysVarType:
    # convert unit electric dipole from atomic unit to the given unit
    return _converter_to(a, unit, _equadrupole_converter)

def _converter_to(a: PhysVarType, unit: UnitType, converter: Dict[str, float]) -> PhysVarType:
    # converter from the atomic unit
    if unit is None:
        return a
    u = unit.lower()
    try:
        return a * converter[u]
    except KeyError:
        avail_units = _avail_keys(converter)
        raise ValueError(f"Unknown unit: {unit}. Available units are: {avail_units}")