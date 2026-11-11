"""The host sampler's reads, against a fake ``/sys/class/thermal`` tree.

A zone's ``temp`` that the kernel would refuse (``EIO`` while the sensor
isn't valid) is made to raise through the reader's file-read hook.
"""

from __future__ import annotations

import errno
from pathlib import Path

from sp_rtk_base.models.host_resources_models import NOT_SUPPORTED
from sp_rtk_base.services.host_resources.reader import HostResourcesReader

EIO = "<EIO>"


def _zone(root: Path, number: int, type_: str, temp: str | None) -> None:
    """One ``thermal_zoneN`` directory in the fake sysfs tree."""
    path = root / f"thermal_zone{number}"
    path.mkdir(parents=True)
    (path / "type").write_text(f"{type_}\n")
    if temp is not None:
        (path / "temp").write_text(f"{temp}\n")


def _read_raising_eio(path: Path) -> str:
    """Read files as text, but raise EIO for any whose content is the marker."""
    text = path.read_text()
    if text.strip() == EIO:
        raise OSError(errno.EIO, "Input/output error", str(path))
    return text


def _reader(root: Path) -> HostResourcesReader:
    return HostResourcesReader(thermal_root=root, read=_read_raising_eio)


def test_the_cpu_temperature_is_read_from_the_cpu_thermal_zone_by_type(
    tmp_path: Path,
) -> None:
    _zone(tmp_path, 0, "rp1_adc", "39000")
    _zone(tmp_path, 1, "cpu-thermal", "48312")
    _zone(tmp_path, 2, "gpu-thermal", "61000")

    reading = _reader(tmp_path).read()

    assert reading.cpu_temperature_millicelsius == 48_312


def test_no_cpu_thermal_zone_is_not_supported(tmp_path: Path) -> None:
    _zone(tmp_path, 0, "acpitz", "30000")

    reading = _reader(tmp_path).read()

    assert reading.cpu_temperature_millicelsius == NOT_SUPPORTED


def test_no_thermal_class_at_all_is_not_supported(tmp_path: Path) -> None:
    reading = _reader(tmp_path / "missing").read()

    assert reading.cpu_temperature_millicelsius == NOT_SUPPORTED


def test_a_cpu_thermal_zone_whose_temp_raises_is_a_failed_read(
    tmp_path: Path,
) -> None:
    _zone(tmp_path, 0, "cpu-thermal", EIO)

    reading = _reader(tmp_path).read()

    assert reading.cpu_temperature_millicelsius is None


def test_a_cpu_thermal_zone_with_no_temp_file_is_a_failed_read(
    tmp_path: Path,
) -> None:
    _zone(tmp_path, 0, "cpu-thermal", None)

    reading = _reader(tmp_path).read()

    assert reading.cpu_temperature_millicelsius is None


def test_a_temp_that_is_not_a_number_is_a_failed_read(tmp_path: Path) -> None:
    _zone(tmp_path, 0, "cpu-thermal", "hot")

    reading = _reader(tmp_path).read()

    assert reading.cpu_temperature_millicelsius is None


def test_a_zone_whose_type_raises_is_skipped(tmp_path: Path) -> None:
    _zone(tmp_path, 0, EIO, "1000")
    _zone(tmp_path, 1, "cpu-thermal", "50000")

    reading = _reader(tmp_path).read()

    assert reading.cpu_temperature_millicelsius == 50_000


def test_no_match_with_a_zone_whose_type_raises_is_a_failed_read(
    tmp_path: Path,
) -> None:
    # That zone may be the cpu-thermal one: it can't be called Not supported.
    _zone(tmp_path, 0, EIO, "1000")
    _zone(tmp_path, 1, "acpitz", "30000")

    reading = _reader(tmp_path).read()

    assert reading.cpu_temperature_millicelsius is None


def test_cooling_devices_beside_the_zones_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "cooling_device0").mkdir()
    (tmp_path / "cooling_device0" / "type").write_text("cpu-thermal\n")
    _zone(tmp_path, 0, "cpu-thermal", "45000")

    reading = _reader(tmp_path).read()

    assert reading.cpu_temperature_millicelsius == 45_000
