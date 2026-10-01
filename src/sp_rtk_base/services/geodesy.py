"""WGS84 conversions between ECEF (metres) and latitude / longitude / height."""

from __future__ import annotations

import math

_A = 6378137.0  # WGS84 semi-major axis (m)
_F = 1.0 / 298.257223563  # WGS84 flattening
_E2 = 2.0 * _F - _F * _F  # first eccentricity squared


def ecef_to_llh(x_m: float, y_m: float, z_m: float) -> tuple[float, float, float]:
    """Convert ECEF coordinates (metres) to WGS84 latitude, longitude, height.

    Uses an iterative method for sub-mm accuracy.

    Returns:
        Tuple of (latitude_deg, longitude_deg, height_above_ellipsoid_m).
    """
    lon = math.atan2(y_m, x_m)
    p = math.sqrt(x_m * x_m + y_m * y_m)

    lat = math.atan2(z_m, p * (1.0 - _E2))
    for _ in range(10):
        sin_lat = math.sin(lat)
        n = _A / math.sqrt(1.0 - _E2 * sin_lat * sin_lat)
        lat = math.atan2(z_m + _E2 * n * sin_lat, p)

    sin_lat = math.sin(lat)
    n = _A / math.sqrt(1.0 - _E2 * sin_lat * sin_lat)
    alt = p / math.cos(lat) - n

    return (math.degrees(lat), math.degrees(lon), alt)


def llh_to_ecef(
    lat_deg: float, lon_deg: float, alt_m: float
) -> tuple[float, float, float]:
    """Convert WGS84 latitude, longitude, height to ECEF coordinates (metres).

    Inverse of :func:`ecef_to_llh`.

    Returns:
        Tuple of (x_m, y_m, z_m).
    """
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    n = _A / math.sqrt(1.0 - _E2 * sin_lat * sin_lat)

    x = (n + alt_m) * math.cos(lat) * math.cos(lon)
    y = (n + alt_m) * math.cos(lat) * math.sin(lon)
    z = (n * (1.0 - _E2) + alt_m) * sin_lat

    return (x, y, z)
