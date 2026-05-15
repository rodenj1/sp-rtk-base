"""Config import/export API endpoints.

Provides YAML-based configuration export (download) and import (upload)
for backing up and restoring the full application configuration.
"""

from __future__ import annotations

import logging

import yaml
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response

from sp_rtk_base.models.config_models import AppConfig
from sp_rtk_base.services import get_config_service
from sp_rtk_base.services.config_service import ConfigService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/export")
async def export_config(
    config_svc: ConfigService = Depends(get_config_service),
) -> Response:
    """Export the full application configuration as a YAML file download.

    Returns:
        YAML file response with Content-Disposition attachment header.
    """
    config = config_svc.get_config()
    data = config.model_dump(mode="json", exclude_none=True)
    yaml_text = yaml.dump(data, default_flow_style=False, sort_keys=False)

    return Response(
        content=yaml_text,
        media_type="application/x-yaml",
        headers={"Content-Disposition": "attachment; filename=sp-rtk-base-config.yaml"},
    )


@router.post("/import")
async def import_config(
    file: UploadFile,
    config_svc: ConfigService = Depends(get_config_service),
) -> dict[str, str]:
    """Import application configuration from a YAML file upload.

    Validates the uploaded YAML against the AppConfig schema before saving.

    Args:
        file: The uploaded YAML configuration file.

    Returns:
        Success message with destination/input counts.

    Raises:
        HTTPException: 400 if the file is not valid YAML or fails schema validation.
    """
    try:
        content = await file.read()
        text = content.decode("utf-8")
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"Failed to read uploaded file: {exc}"
        ) from exc

    if not text.strip():
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid YAML: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400, detail="YAML content must be a mapping (object)"
        )

    try:
        config = AppConfig.model_validate(data)
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid configuration schema: {exc}"
        ) from exc

    config_svc.save_config(config)
    logger.info("Configuration imported successfully")

    return {
        "status": "ok",
        "message": (
            f"Configuration imported: {len(config.destinations)} destinations, "
            f"input={'configured' if config.input else 'none'}"
        ),
    }
