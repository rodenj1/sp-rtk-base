"""What the agent keeps on disk once Claimed: the base key and the Claim.

All in the agent's state directory (its unit's ``StateDirectory``, mode
0700): ``base_key.pem`` (PKCS#8, mode 0600), ``claim.json`` (what the local
Portal page shows) and ``base_id`` (the ``<id>`` alone, which the app's
identity endpoint serves). Nothing is written until a Claim succeeds.
Once Claimed, ``agent_config.json`` holds the last agent config block the
Portal sent, and ``claim.json``'s ``last_contact`` is refreshed on each
Check-in (not each poll: an SD card shouldn't take a write every 20 s).
An empty ``paused`` file is there while remote access is paused on site.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from sp_rtk_portal_protocol import AgentConfig, BaseKey, MessageError

logger = logging.getLogger(__name__)

KEY_FILE = "base_key.pem"
CLAIM_FILE = "claim.json"
ID_FILE = "base_id"
CONFIG_FILE = "agent_config.json"
PAUSED_FILE = "paused"
"""Present while remote access is paused on site (survives restarts)."""


@dataclass(frozen=True)
class Claim:
    base_id: str
    account: str
    portal: str
    claimed_at: str
    """ISO 8601."""
    last_contact: str
    """ISO 8601."""


def _write(path: Path, data: bytes, mode: int) -> None:
    """Replace ``path`` atomically; it never exists with a wider mode."""
    tmp = path.with_name(path.name + ".tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_claim(state_dir: Path, key: BaseKey, claim: Claim) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    _write(state_dir / KEY_FILE, key.to_pem(), 0o600)
    _write(state_dir / CLAIM_FILE, json.dumps(asdict(claim), indent=2).encode(), 0o600)
    _write(state_dir / ID_FILE, (claim.base_id + "\n").encode(), 0o644)


def load_claim(state_dir: Path) -> tuple[BaseKey, Claim] | None:
    """The saved key and Claim, or ``None`` if unclaimed (or unreadable)."""
    key_file, claim_file = state_dir / KEY_FILE, state_dir / CLAIM_FILE
    if not key_file.exists() and not claim_file.exists():
        return None
    try:
        key = BaseKey.from_pem(key_file.read_bytes())
        claim = Claim(**json.loads(claim_file.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Ignoring an unreadable Claim in %s: %s", state_dir, exc)
        return None
    return key, claim


def read_base_id(state_dir: Path) -> str | None:
    """The Claimed base's ``<id>``, or ``None`` when unclaimed."""
    try:
        value = (state_dir / ID_FILE).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value if value.isalnum() else None


def save_claim_file(state_dir: Path, claim: Claim) -> None:
    """Rewrite ``claim.json`` alone (e.g. a newer ``last_contact``)."""
    _write(state_dir / CLAIM_FILE, json.dumps(asdict(claim), indent=2).encode(), 0o600)


def save_agent_config(state_dir: Path, config: AgentConfig) -> None:
    _write(state_dir / CONFIG_FILE, json.dumps(config.to_json()).encode(), 0o600)


def load_agent_config(state_dir: Path) -> AgentConfig | None:
    """The last agent config block applied, or ``None`` (built-in defaults)."""
    try:
        data = json.loads((state_dir / CONFIG_FILE).read_text(encoding="utf-8"))
        return AgentConfig.from_json(data)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, MessageError) as exc:
        logger.warning("Ignoring an unreadable agent config: %s", exc)
        return None


def save_paused(state_dir: Path, paused: bool) -> None:
    """Remember that remote access is paused (or not) across restarts."""
    if paused:
        _write(state_dir / PAUSED_FILE, b"", 0o600)
    else:
        with contextlib.suppress(FileNotFoundError):
            (state_dir / PAUSED_FILE).unlink()


def load_paused(state_dir: Path) -> bool:
    return (state_dir / PAUSED_FILE).exists()
