"""The Bluetooth Verification endpoint.

``POST /api/input/bluetooth/test`` runs a dress rehearsal of the relay's
own connect path against the values currently in the form, and answers
one question: would Save and Start connect?

The work itself lives in
:class:`~sp_rtk_base.services.bluetooth_service.BluetoothVerificationService`
— this module only shapes the request and turns a refusal into a 409.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from sp_rtk_base.models.bluetooth_models import VerificationResult
from sp_rtk_base.services import get_bluetooth_verification_service
from sp_rtk_base.services.bluetooth_service import (
    BluetoothVerificationService,
    VerificationRefusedError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/input/bluetooth", tags=["bluetooth"])


class VerificationRequest(BaseModel):
    """What a Verification needs, and deliberately nothing more.

    There is **no** ``scan_timeout``: the server passes the same value
    ``to_relay_config()`` injects, so the Verification and the real run
    wait identically.  A client-chosen timeout would be a way to
    manufacture a Green that Save will not reproduce — the exact failure
    class the Verification exists to eliminate.
    """

    #: Required.  The test path does no name discovery: the page's scan
    #: flow already resolves a name to a MAC before a Verification is
    #: meaningful, and a second discovery semantic is a second thing to
    #: keep in step with the relay's.
    mac_address: str = Field(min_length=1)
    pin: str = ""
    adapter: str = "hci0"
    #: The operator has seen the force-repair dialog and agreed.  Never
    #: defaulted true: consent is not assumed.
    confirm_repair: bool = False


@router.post("/test", response_model=VerificationResult)
async def verify_bluetooth_connection(
    request: VerificationRequest,
    service: BluetoothVerificationService = Depends(get_bluetooth_verification_service),
) -> VerificationResult | JSONResponse:
    """Run a Verification against the submitted MAC and PIN.

    Returns:
        The :class:`VerificationResult` — Green or Red — or a 409 when
        the Verification was refused and nothing was touched.
    """
    try:
        return await service.verify(
            mac_address=request.mac_address,
            pin=request.pin,
            adapter=request.adapter,
            confirm_repair=request.confirm_repair,
        )
    except VerificationRefusedError as exc:
        # 409 matches how the repo already maps conflict states, and the
        # refusal is deliberately not a third verdict.  Three unrelated
        # refusals share this status with unrelated remedies, so the
        # machine-readable ``code`` is what clients branch on.
        logger.info("Verification refused (%s): %s", exc.code, exc.message)
        return JSONResponse(
            status_code=409,
            content={"status": "error", "message": exc.message, "code": exc.code},
        )
