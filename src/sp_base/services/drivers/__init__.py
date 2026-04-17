"""GPS receiver driver registry.

Provides a factory for creating driver instances by vendor name.
New drivers are registered here — adding a vendor requires only
a new driver module and a registry entry.
"""

from __future__ import annotations

from sp_base.services.drivers.base import GpsReceiverDriver
from sp_base.services.drivers.ublox import UbloxDriver

# ---------------------------------------------------------------------------
# Driver registry — maps vendor key to driver class
# ---------------------------------------------------------------------------

_DRIVER_REGISTRY: dict[str, type[GpsReceiverDriver]] = {
    "ublox": UbloxDriver,
}


def register_driver(vendor_key: str, driver_class: type[GpsReceiverDriver]) -> None:
    """Register a GPS receiver driver class.

    Args:
        vendor_key: Short identifier (e.g. ``"ublox"``, ``"septentrio"``).
        driver_class: Concrete subclass of :class:`GpsReceiverDriver`.
    """
    _DRIVER_REGISTRY[vendor_key] = driver_class


def get_driver_class(vendor_key: str) -> type[GpsReceiverDriver] | None:
    """Look up a registered driver class by vendor key.

    Args:
        vendor_key: Short identifier.

    Returns:
        The driver class, or ``None`` if not registered.
    """
    return _DRIVER_REGISTRY.get(vendor_key)


def list_drivers() -> list[str]:
    """Return all registered vendor keys.

    Returns:
        Sorted list of vendor keys.
    """
    return sorted(_DRIVER_REGISTRY.keys())


def create_driver(vendor_key: str) -> GpsReceiverDriver:
    """Create a driver instance by vendor key.

    Args:
        vendor_key: Short identifier.

    Returns:
        A new driver instance.

    Raises:
        ValueError: If the vendor key is not registered.
    """
    cls = _DRIVER_REGISTRY.get(vendor_key)
    if cls is None:
        available = ", ".join(list_drivers()) or "(none)"
        raise ValueError(
            f"Unknown GPS driver '{vendor_key}'. Available: {available}"
        )
    return cls()


def clear_registry() -> None:
    """Remove all registered drivers.

    Intended for use in tests to ensure a clean registry state.
    """
    _DRIVER_REGISTRY.clear()


__all__ = [
    "GpsReceiverDriver",
    "UbloxDriver",
    "clear_registry",
    "create_driver",
    "get_driver_class",
    "list_drivers",
    "register_driver",
]
