"""Tests for sp_rtk_base.services.profile_store — profile persistence."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from sp_rtk_base.models.device_models import PortId, RtcmRowId
from sp_rtk_base.models.profile_models import Profile
from sp_rtk_base.profiles import BUILTIN_PROFILES
from sp_rtk_base.services.profile_store import (
    ProfileBusinessRuleError,
    ProfileConflictError,
    ProfileImmutableError,
    ProfileNotFoundError,
    ProfileStore,
)

BUILTIN_NAME = "ublox-f9p-base-standard"


@pytest.fixture()
def profiles_dir(tmp_path: Path) -> Path:
    return tmp_path / "profiles"


@pytest.fixture()
def store(profiles_dir: Path) -> ProfileStore:
    return ProfileStore(profiles_dir=profiles_dir)


def _custom(name: str = "my-custom", **overrides: object) -> Profile:
    kwargs: dict[str, object] = {
        "name": name,
        "version": 1,
        "hardware": "ZED-F9P",
        "data_link_port": [PortId.UART1],
        "rtcm_stream": {"matrix": {RtcmRowId.RTCM_1005: {PortId.UART1: True}}},
    }
    kwargs.update(overrides)
    return Profile.model_validate(kwargs)


class TestListProfiles:
    def test_lists_only_builtin_when_no_customs(self, store: ProfileStore) -> None:
        profiles = store.list_profiles()
        assert [p.name for p in profiles] == [BUILTIN_NAME]

    def test_builtins_before_customs_alphabetical(self, store: ProfileStore) -> None:
        store.create_profile(_custom("zeta"))
        store.create_profile(_custom("alpha"))
        profiles = store.list_profiles()
        assert [p.name for p in profiles] == [BUILTIN_NAME, "alpha", "zeta"]

    def test_skips_malformed_custom_file_with_warning(
        self, store: ProfileStore, profiles_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store.create_profile(_custom("good"))
        profiles_dir.mkdir(parents=True, exist_ok=True)
        (profiles_dir / "bad.yaml").write_text("not: [valid, profile", encoding="utf-8")

        with caplog.at_level("WARNING"):
            profiles = store.list_profiles()

        assert [p.name for p in profiles] == [BUILTIN_NAME, "good"]
        assert "bad.yaml" in caplog.text

    def test_skips_schema_invalid_custom_file(
        self, store: ProfileStore, profiles_dir: Path
    ) -> None:
        store.create_profile(_custom("good"))
        profiles_dir.mkdir(parents=True, exist_ok=True)
        (profiles_dir / "invalid.yaml").write_text(
            yaml.dump({"name": "invalid", "version": 999, "hardware": "ZED-F9P"}),
            encoding="utf-8",
        )

        profiles = store.list_profiles()
        assert [p.name for p in profiles] == [BUILTIN_NAME, "good"]


class TestGetProfile:
    def test_get_builtin(self, store: ProfileStore) -> None:
        assert store.get_profile(BUILTIN_NAME) is BUILTIN_PROFILES[BUILTIN_NAME]

    def test_get_custom(self, store: ProfileStore) -> None:
        created = store.create_profile(_custom())
        assert store.get_profile("my-custom") == created

    def test_get_missing_returns_none(self, store: ProfileStore) -> None:
        assert store.get_profile("does-not-exist") is None


class TestIsBuiltin:
    def test_true_for_builtin(self, store: ProfileStore) -> None:
        assert store.is_builtin(BUILTIN_NAME) is True

    def test_false_for_custom(self, store: ProfileStore) -> None:
        store.create_profile(_custom())
        assert store.is_builtin("my-custom") is False


class TestCreateProfile:
    def test_create_persists_to_disk(
        self, store: ProfileStore, profiles_dir: Path
    ) -> None:
        store.create_profile(_custom())
        assert (profiles_dir / "my-custom.yaml").exists()

    def test_create_returns_profile(self, store: ProfileStore) -> None:
        created = store.create_profile(_custom())
        assert created.name == "my-custom"

    def test_conflict_with_builtin(self, store: ProfileStore) -> None:
        with pytest.raises(ProfileConflictError):
            store.create_profile(_custom(BUILTIN_NAME))

    def test_conflict_with_existing_custom(self, store: ProfileStore) -> None:
        store.create_profile(_custom())
        with pytest.raises(ProfileConflictError):
            store.create_profile(_custom())

    def test_builtin_never_modified(self, store: ProfileStore) -> None:
        with pytest.raises(ProfileConflictError):
            store.create_profile(_custom(BUILTIN_NAME))
        assert BUILTIN_PROFILES[BUILTIN_NAME].hardware == "ZED-F9P"

    @pytest.mark.parametrize("bad_name", ["has space", "has/slash", "../traverse", ""])
    def test_unsafe_name_rejected(self, store: ProfileStore, bad_name: str) -> None:
        if bad_name == "":
            with pytest.raises(ValidationError):
                _custom(bad_name)
            return
        with pytest.raises(ProfileBusinessRuleError):
            store.create_profile(_custom(bad_name))


class TestRenameProfile:
    def test_rename_moves_file(self, store: ProfileStore, profiles_dir: Path) -> None:
        store.create_profile(_custom("old-name"))
        store.rename_profile("old-name", "new-name")
        assert not (profiles_dir / "old-name.yaml").exists()
        assert (profiles_dir / "new-name.yaml").exists()

    def test_rename_returns_updated_profile(self, store: ProfileStore) -> None:
        store.create_profile(_custom("old-name"))
        renamed = store.rename_profile("old-name", "new-name")
        assert renamed.name == "new-name"

    def test_rename_builtin_is_forbidden(self, store: ProfileStore) -> None:
        with pytest.raises(ProfileImmutableError):
            store.rename_profile(BUILTIN_NAME, "something-else")

    def test_rename_unknown_is_not_found(self, store: ProfileStore) -> None:
        with pytest.raises(ProfileNotFoundError):
            store.rename_profile("does-not-exist", "something-else")

    def test_rename_to_existing_builtin_conflicts(self, store: ProfileStore) -> None:
        store.create_profile(_custom("old-name"))
        with pytest.raises(ProfileConflictError):
            store.rename_profile("old-name", BUILTIN_NAME)

    def test_rename_to_existing_custom_conflicts(self, store: ProfileStore) -> None:
        store.create_profile(_custom("first"))
        store.create_profile(_custom("second"))
        with pytest.raises(ProfileConflictError):
            store.rename_profile("first", "second")

    def test_rename_to_unsafe_name_rejected(self, store: ProfileStore) -> None:
        store.create_profile(_custom("old-name"))
        with pytest.raises(ProfileBusinessRuleError):
            store.rename_profile("old-name", "has/slash")

    def test_rename_to_same_name_is_a_noop(
        self, store: ProfileStore, profiles_dir: Path
    ) -> None:
        created = store.create_profile(_custom("same-name"))
        renamed = store.rename_profile("same-name", "same-name")
        assert renamed == created
        assert (profiles_dir / "same-name.yaml").exists()


class TestDeleteProfile:
    def test_delete_removes_file(self, store: ProfileStore, profiles_dir: Path) -> None:
        store.create_profile(_custom())
        store.delete_profile("my-custom")
        assert not (profiles_dir / "my-custom.yaml").exists()

    def test_delete_builtin_is_forbidden(self, store: ProfileStore) -> None:
        with pytest.raises(ProfileImmutableError):
            store.delete_profile(BUILTIN_NAME)

    def test_delete_unknown_is_not_found(self, store: ProfileStore) -> None:
        with pytest.raises(ProfileNotFoundError):
            store.delete_profile("does-not-exist")

    def test_builtin_survives_repeated_delete_attempts(
        self, store: ProfileStore
    ) -> None:
        for _ in range(3):
            with pytest.raises(ProfileImmutableError):
                store.delete_profile(BUILTIN_NAME)
        assert store.get_profile(BUILTIN_NAME) is not None


class TestExportImportRoundtrip:
    def test_export_builtin(self, store: ProfileStore) -> None:
        exported = store.export_profile(BUILTIN_NAME)
        assert exported.name == BUILTIN_NAME

    def test_export_unknown_is_not_found(self, store: ProfileStore) -> None:
        with pytest.raises(ProfileNotFoundError):
            store.export_profile("does-not-exist")

    def test_roundtrip_through_export_import(self, store: ProfileStore) -> None:
        store.create_profile(_custom("source"))
        exported = store.export_profile("source")
        store.delete_profile("source")

        data = exported.model_dump(mode="json")
        imported = store.import_profile(data)

        assert imported == exported
        assert store.get_profile("source") == exported

    def test_import_unknown_version_is_422_shaped(self, store: ProfileStore) -> None:
        data = _custom("importee").model_dump(mode="json")
        data["version"] = 999
        with pytest.raises(ValidationError, match="version"):
            store.import_profile(data)

    def test_import_conflicting_name(self, store: ProfileStore) -> None:
        store.create_profile(_custom("dup"))
        data = _custom("dup").model_dump(mode="json")
        with pytest.raises(ProfileConflictError):
            store.import_profile(data)


class TestPathResolutionPrecedence:
    def test_explicit_path_wins_over_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_dir = tmp_path / "env-profiles"
        explicit_dir = tmp_path / "explicit-profiles"
        monkeypatch.setenv("SP_RTK_BASE_PROFILES_DIR", str(env_dir))

        store = ProfileStore(profiles_dir=explicit_dir)
        assert store.profiles_dir == explicit_dir

    def test_env_used_when_no_explicit_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_dir = tmp_path / "env-profiles"
        monkeypatch.setenv("SP_RTK_BASE_PROFILES_DIR", str(env_dir))

        store = ProfileStore()
        assert store.profiles_dir == env_dir

    def test_default_when_neither_given(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SP_RTK_BASE_PROFILES_DIR", raising=False)
        from sp_rtk_base.services.profile_store import DEFAULT_PROFILES_DIR

        store = ProfileStore()
        assert store.profiles_dir == DEFAULT_PROFILES_DIR
