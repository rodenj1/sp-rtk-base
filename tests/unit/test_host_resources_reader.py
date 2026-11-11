"""The host sampler's reads, against fake ``/sys/class/thermal`` and
``/sys/class/hwmon`` trees.

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


def _reader(root: Path, hwmon_root: Path | None = None) -> HostResourcesReader:
    return HostResourcesReader(
        thermal_root=root,
        hwmon_root=hwmon_root or root / "no-hwmon",
        read=_read_raising_eio,
    )


def _hwmon(root: Path, number: int, name: str | None, alarm: str | None) -> None:
    """One ``hwmonN`` device in the fake sysfs tree."""
    path = root / f"hwmon{number}"
    path.mkdir(parents=True)
    if name is not None:
        (path / "name").write_text(f"{name}\n")
    if alarm is not None:
        (path / "in0_lcrit_alarm").write_text(f"{alarm}\n")


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


def test_undervoltage_is_read_from_the_rpi_volt_hwmon_device_by_name(
    tmp_path: Path,
) -> None:
    _hwmon(tmp_path, 0, "cpu_thermal", "0")
    _hwmon(tmp_path, 1, "rp1_adc", "0")
    _hwmon(tmp_path, 2, "rpi_volt", "1")
    _hwmon(tmp_path, 3, "pwmfan", None)

    reading = _reader(tmp_path / "thermal", hwmon_root=tmp_path).read()

    assert reading.undervoltage_alarm == 1


def test_no_undervoltage_alarm_reads_0(tmp_path: Path) -> None:
    _hwmon(tmp_path, 0, "rpi_volt", "0")

    reading = _reader(tmp_path / "thermal", hwmon_root=tmp_path).read()

    assert reading.undervoltage_alarm == 0


def test_no_rpi_volt_device_is_not_supported(tmp_path: Path) -> None:
    _hwmon(tmp_path, 0, "coretemp", None)
    _hwmon(tmp_path, 1, "acpitz", None)

    reading = _reader(tmp_path / "thermal", hwmon_root=tmp_path).read()

    assert reading.undervoltage_alarm == NOT_SUPPORTED


def test_no_hwmon_class_at_all_is_not_supported(tmp_path: Path) -> None:
    reading = _reader(tmp_path / "thermal", hwmon_root=tmp_path / "missing").read()

    assert reading.undervoltage_alarm == NOT_SUPPORTED


def test_a_hwmon_device_with_no_name_is_skipped(tmp_path: Path) -> None:
    _hwmon(tmp_path, 0, None, "1")
    _hwmon(tmp_path, 1, "rpi_volt", "0")

    reading = _reader(tmp_path / "thermal", hwmon_root=tmp_path).read()

    assert reading.undervoltage_alarm == 0


def test_an_rpi_volt_alarm_that_raises_is_a_failed_read(tmp_path: Path) -> None:
    _hwmon(tmp_path, 0, "rpi_volt", EIO)

    reading = _reader(tmp_path / "thermal", hwmon_root=tmp_path).read()

    assert reading.undervoltage_alarm is None


def test_an_rpi_volt_alarm_that_is_not_0_or_1_is_a_failed_read(
    tmp_path: Path,
) -> None:
    _hwmon(tmp_path, 0, "rpi_volt", "2")

    reading = _reader(tmp_path / "thermal", hwmon_root=tmp_path).read()

    assert reading.undervoltage_alarm is None


def test_no_match_with_a_device_whose_name_raises_is_a_failed_read(
    tmp_path: Path,
) -> None:
    _hwmon(tmp_path, 0, EIO, "1")
    _hwmon(tmp_path, 1, "coretemp", None)

    reading = _reader(tmp_path / "thermal", hwmon_root=tmp_path).read()

    assert reading.undervoltage_alarm is None


def test_a_failed_undervoltage_read_leaves_the_temperature_reading(
    tmp_path: Path,
) -> None:
    _zone(tmp_path / "thermal", 0, "cpu-thermal", "50000")
    _hwmon(tmp_path / "hwmon", 0, "rpi_volt", EIO)

    reading = _reader(tmp_path / "thermal", hwmon_root=tmp_path / "hwmon").read()

    assert reading.cpu_temperature_millicelsius == 50_000
    assert reading.undervoltage_alarm is None
