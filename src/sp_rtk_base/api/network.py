"""Network API — console status, WiFi scan, join, switch, and forget (issues #22-24).

No authentication, consistent with the rest of the console.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from sp_rtk_base.models.api_models import (
    NetworkActionResponse,
    NetworkConnectRequest,
    NetworkConnectResponse,
    NetworkFallbackInfoResponse,
    NetworkStatusResponse,
)
from sp_rtk_base.models.net_provision_models import SavedWifiConnection, WifiNetwork
from sp_rtk_base.services import get_network_service
from sp_rtk_base.services.network_service import (
    NetworkNotConfiguredError,
    NetworkService,
)

router = APIRouter(prefix="/api/network", tags=["network"])


@router.get("/status", response_model=NetworkStatusResponse)
async def get_network_status(
    svc: NetworkService = Depends(get_network_service),
) -> NetworkStatusResponse:
    """Return the device's current wired/WiFi link.

    ``configured=False`` (with ``link=None``) means this device has no
    net-provisioning config yet — not an error, just nothing to show.
    """
    try:
        link = await svc.get_active_link()
    except NetworkNotConfiguredError:
        return NetworkStatusResponse(configured=False)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return NetworkStatusResponse(configured=True, link=link)


@router.get("/scan", response_model=list[WifiNetwork])
async def scan_networks(
    svc: NetworkService = Depends(get_network_service),
) -> list[WifiNetwork]:
    """Scan for nearby WiFi networks, strongest signal first."""
    try:
        return await svc.scan_networks()
    except NetworkNotConfiguredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/fallback-info", response_model=NetworkFallbackInfoResponse)
async def get_fallback_info(
    svc: NetworkService = Depends(get_network_service),
) -> NetworkFallbackInfoResponse:
    """AP SSID + fallback window, for the console's pre-apply warning copy."""
    try:
        info = await svc.get_ap_fallback_info()
    except NetworkNotConfiguredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return NetworkFallbackInfoResponse(
        ap_ssid=info.ap_ssid, fallback_window_seconds=info.fallback_window_seconds
    )


@router.post("/connect", response_model=NetworkConnectResponse, status_code=202)
async def connect_network(
    request: NetworkConnectRequest,
    svc: NetworkService = Depends(get_network_service),
) -> NetworkConnectResponse:
    """Join a WiFi network — fire-and-acknowledge.

    Returns as soon as NetworkManager has been instructed, not once the
    device is confirmed on the new network — see
    :meth:`NetworkService.connect_to_network`. 409 means there's no
    provisioning config to hold a connection profile; it does not mean
    the join itself failed, since that outcome isn't known yet.
    """
    try:
        await svc.connect_to_network(
            request.ssid, request.password, hidden=request.hidden
        )
    except NetworkNotConfiguredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return NetworkConnectResponse(
        status="accepted",
        message=f"Instructed NetworkManager to join {request.ssid!r}",
    )


@router.get("/saved", response_model=list[SavedWifiConnection])
async def list_saved_networks(
    svc: NetworkService = Depends(get_network_service),
) -> list[SavedWifiConnection]:
    """List saved WiFi profiles, for the console's switch/forget list."""
    try:
        return await svc.list_saved_networks()
    except NetworkNotConfiguredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/saved/{name}/activate", response_model=NetworkActionResponse, status_code=202
)
async def switch_network(
    name: str,
    svc: NetworkService = Depends(get_network_service),
) -> NetworkActionResponse:
    """Switch the active WiFi to an already-saved network — fire-and-acknowledge.

    Returns as soon as NetworkManager has been instructed to activate
    ``name``, not once the device is confirmed connected — see
    :meth:`NetworkService.switch_to_network`. 409 means there's no
    provisioning config; it does not mean the switch itself failed,
    since that outcome isn't known yet.
    """
    try:
        await svc.switch_to_network(name)
    except NetworkNotConfiguredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return NetworkActionResponse(
        status="accepted",
        message=f"Instructed NetworkManager to switch to {name!r}",
    )


@router.delete("/saved/{name}", response_model=NetworkActionResponse, status_code=202)
async def forget_network(
    name: str,
    svc: NetworkService = Depends(get_network_service),
) -> NetworkActionResponse:
    """Forget a saved WiFi network, including the currently active one.

    Fire-and-acknowledge, same as switch: forgetting the active network
    deactivates it as part of the delete, which can drop this very
    request's own connection before nmcli reports success — see
    :meth:`NetworkService.forget_network`.
    """
    try:
        await svc.forget_network(name)
    except NetworkNotConfiguredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return NetworkActionResponse(
        status="accepted",
        message=f"Instructed NetworkManager to forget {name!r}",
    )
