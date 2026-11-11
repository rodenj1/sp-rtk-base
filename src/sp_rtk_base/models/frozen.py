"""The immutable pydantic base the sampler models share."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """A model whose instances can't be changed once built."""

    model_config = ConfigDict(frozen=True)
