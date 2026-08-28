"""Built-in GPS receiver profiles.

Each built-in ships as one YAML file under ``profiles/builtin/`` in
the package, validated by the ``Profile`` Pydantic model at import
time — adding a built-in is a file drop, zero new Python. Built-ins
are read-only; nothing here ever mutates a loaded ``Profile``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from sp_rtk_base.models.profile_models import Profile

BUILTIN_PROFILES_DIR: Path = Path(__file__).parent / "builtin"


def _load_builtin_profiles(directory: Path) -> dict[str, Profile]:
    profiles: dict[str, Profile] = {}
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        profile = Profile.model_validate(data)
        profiles[profile.name] = profile
    return profiles


#: Internal name -> Profile, for every built-in shipped with this app.
BUILTIN_PROFILES: dict[str, Profile] = _load_builtin_profiles(BUILTIN_PROFILES_DIR)
