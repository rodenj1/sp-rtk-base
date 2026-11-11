"""The host sampler's reads, against fake ``/sys/class/thermal``,
``/sys/class/hwmon`` and ``/proc`` trees, with the filesystem-space call
injected.

A zone's ``temp`` that the kernel would refuse (``EIO`` while the sensor
isn't valid) is made to raise through the reader's file-read hook.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass
from pathlib import Path

import pytest

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


@dataclass(frozen=True)
class _Space:
    """What ``statvfs`` gives: fragment size and block counts."""

    f_frsize: int = 4096
    f_blocks: int = 7_500_000
    f_bfree: int = 6_000_000
    f_bavail: int = 5_600_000


class _Statvfs:
    """A fake ``os.statvfs``: gives *space* (or raises it) and records
    the paths asked for."""

    def __init__(self, space: _Space | Exception | None = None) -> None:
        self.space = space or _Space()
        self.seen: list[str] = []

    def __call__(self, path: str) -> _Space:
        self.seen.append(path)
        if isinstance(self.space, Exception):
            raise self.space
        return self.space


def _reader(
    root: Path,
    hwmon_root: Path | None = None,
    proc: Path | None = None,
    statvfs: _Statvfs | None = None,
) -> HostResourcesReader:
    return HostResourcesReader(
        thermal_root=root,
        hwmon_root=hwmon_root or root / "no-hwmon",
        proc_root=proc or root / "no-proc",
        clock_ticks=100,
        statvfs=statvfs or _Statvfs(),
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


STAT = """\
cpu  10132153 290696 3084719 46828483 16683 0 25195 0 175628 0
cpu0 1393280 32966 572056 13343292 6130 0 17875 0 23933 0
intr 1462898 0 0 0
ctxt 115315
btime 1700000000
"""

MEMINFO = """\
MemTotal:        3884564 kB
MemFree:          244916 kB
MemAvailable:    2841212 kB
Buffers:          120040 kB
SwapTotal:        102396 kB
SwapFree:         102396 kB
"""


def _proc(root: Path, stat: str | None = STAT, meminfo: str | None = MEMINFO) -> Path:
    proc = root / "proc"
    proc.mkdir()
    if stat is not None:
        (proc / "stat").write_text(stat)
    if meminfo is not None:
        (proc / "meminfo").write_text(meminfo)
    return proc


def test_cpu_seconds_by_mode_come_from_the_aggregate_cpu_line(tmp_path: Path) -> None:
    reading = _reader(tmp_path, proc=_proc(tmp_path)).read()

    # Ticks at 100 per second; guest time is already inside user and nice.
    assert reading.cpu_seconds == {
        "user": 101321.53,
        "nice": 2906.96,
        "system": 30847.19,
        "idle": 468284.83,
        "iowait": 166.83,
        "irq": 0.0,
        "softirq": 251.95,
        "steal": 0.0,
    }


def test_cpu_seconds_from_an_older_kernel_with_fewer_columns(tmp_path: Path) -> None:
    proc = _proc(tmp_path, stat="\ncpu  200 0 100 700 0\n")

    reading = _reader(tmp_path, proc=proc).read()

    assert reading.cpu_seconds == {
        "user": 2.0,
        "nice": 0.0,
        "system": 1.0,
        "idle": 7.0,
        "iowait": 0.0,
    }


@pytest.mark.parametrize(
    "stat",
    [None, EIO, "cpu0 1 2 3 4\n", "cpu  1 2 x 4\n", "cpu  1 2\n", ""],
    ids=["missing", "raises", "no aggregate line", "not a number", "short", "empty"],
)
def test_an_unreadable_cpu_line_is_a_failed_read(
    tmp_path: Path, stat: str | None
) -> None:
    reading = _reader(tmp_path, proc=_proc(tmp_path, stat=stat)).read()

    assert reading.cpu_seconds is None


def test_memory_is_the_total_and_the_kernels_available_estimate_in_bytes(
    tmp_path: Path,
) -> None:
    reading = _reader(tmp_path, proc=_proc(tmp_path)).read()

    assert reading.memory_total_bytes == 3_884_564 * 1024
    assert reading.memory_available_bytes == 2_841_212 * 1024


@pytest.mark.parametrize(
    "meminfo",
    [None, EIO, "MemTotal:  3884564 kB\nMemFree:  244916 kB\n", "MemTotal: lots\n"],
    ids=["missing", "raises", "no MemAvailable", "not a number"],
)
def test_an_unreadable_meminfo_is_a_failed_memory_read(
    tmp_path: Path, meminfo: str | None
) -> None:
    reading = _reader(tmp_path, proc=_proc(tmp_path, meminfo=meminfo)).read()

    assert reading.memory_total_bytes is None
    assert reading.memory_available_bytes is None


def test_the_root_filesystem_is_its_size_and_the_space_a_non_root_user_can_use(
    tmp_path: Path,
) -> None:
    statvfs = _Statvfs(_Space(f_frsize=4096, f_blocks=7_500_000, f_bavail=5_600_000))

    reading = _reader(tmp_path, statvfs=statvfs).read()

    assert statvfs.seen == ["/"]
    assert reading.root_filesystem_size_bytes == 7_500_000 * 4096
    # f_bavail, not f_bfree: the root-reserved blocks aren't the user's.
    assert reading.root_filesystem_available_bytes == 5_600_000 * 4096


def test_a_root_filesystem_whose_statvfs_raises_is_a_failed_read(
    tmp_path: Path,
) -> None:
    statvfs = _Statvfs(OSError(errno.EIO, "Input/output error"))

    reading = _reader(tmp_path, statvfs=statvfs).read()

    assert reading.root_filesystem_size_bytes is None
    assert reading.root_filesystem_available_bytes is None


def test_one_failed_read_leaves_the_others(tmp_path: Path) -> None:
    _zone(tmp_path, 0, "cpu-thermal", "50000")
    proc = _proc(tmp_path, stat=None)

    reading = _reader(tmp_path, proc=proc).read()

    assert reading.cpu_seconds is None
    assert reading.memory_total_bytes == 3_884_564 * 1024
    assert reading.cpu_temperature_millicelsius == 50_000
    assert reading.root_filesystem_size_bytes == 7_500_000 * 4096


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
