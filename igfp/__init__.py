from functools import lru_cache
from time import time
from typing import Any, List, Optional, Set

from fastapi import Depends, FastAPI, HTTPException
from fastapi.logger import logger
from httpx import AsyncClient
from pydantic import BaseModel, BaseSettings

igfp = FastAPI(openapi_url=None)
igapi = AsyncClient(
    base_url="https://api.instagram.com",
    headers={"Accept": "application/json"},
    http2=True,
)
iggraph = AsyncClient(
    base_url="https://graph.instagram.com",
    headers={"Accept": "application/json"},
    http2=True,
)


class SettingsModel(BaseSettings):

    application_id: str
    application_secret: str
    hostname: str
    media_fields: str = (
        "caption, "
        "id, "
        "media_type, "
        "media_url, "
        "permalink, "
        "thumbnail_url, "
        "timestamp, "
        "username, "
        "children{"
        "id, "
        "media_type, "
        "media_url, "
        "permalink, "
        "thumbnail_url, "
        "timestamp, "
        "username"
        "}"
    )
    media_refresh_delay: int = 60 * 5
    """ Refresh media every 5 minutes to avoid API rate limiting. """
    scopes: List[str] = ["user_media", "user_profile"]
    token_refresh_delay: int = 60 * 60 * 24 * 30
    """ Refresh every 30 days, to be sure we do not miss the window without spamming. """

    class Config:
        env_prefix = "IGFP"


class ContextModel(BaseModel):

    media: Any = None
    media_refreshed: float = 0
    token: Optional[str] = None
    token_refreshed: float = 0


@lru_cache(maxsize=1)
def Context() -> ContextModel:
    return ContextModel()


@lru_cache(maxsize=1)
def Settings() -> SettingsModel:
    return SettingsModel()


@lru_cache(maxsize=1)
def RedirectURI(settings: SettingsModel = Depends(Settings)) -> str:
    # TODO: build URI from settings.hostname
    return ""


async def AccessToken(
    context: ContextModel = Depends(Context),
    settings: SettingsModel = Depends(Settings),
) -> str:
    """
    Retrieve and return API access token, refreshing it if necessary.
    Aims to be used as a dependency to ensure continuous token refresh.
    """
    if context.token is None:
        raise HTTPException()
    now = time()
    if now - context.token_refreshed > settings.token_refresh_delay:
        response = await iggraph.get(
            f"refresh_access_token",
            params={
                "grant_type": "ig_refresh_token",
                "access_token": context.token,
            },
        )
        response.raise_for_status()
        context.token = response.json().get("access_token")
        context.token_refreshed = now
    # TODO: fix this edge case :/
    return context.token


@igfp.on_event("startup")
def startup(
    redirect_uri: str = Depends(RedirectURI),
    settings: SettingsModel = Depends(Settings),
) -> None:
    scopes = ",".join(settings.scopes)
    logger.info(
        f"To activate Instagram feed proxy please authenticate to "
        f"{igapi.base_url}/oauth/authorize"
        f"?client_id={settings.application_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={scopes}"
    )


@igfp.post("/authorize")
async def authorize(
    code: str,
    context: ContextModel = Depends(Context),
    redirect_uri: str = Depends(RedirectURI),
    settings: SettingsModel = Depends(Settings),
) -> None:
    if context.token is not None:
        raise HTTPException()
    # NOTE: Retrieve initial token.
    response = await igapi.post(
        "/oauth/access_token",
        json={
            "app_id": settings.application_id,
            "app_secret": settings.application_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    response.raise_for_status()
    initial_token = response.json()
    # NOTE: exchange for 60 days long token.
    response = await iggraph.get(
        "/access_token",
        params={
            "client_secret": settings.application_secret,
            "grant_type": "ig_exchange_token",
            "access_token": initial_token,
        },
    )
    response.raise_for_status()
    context.token = response.json().get("access_token")
    context.token_refreshed = time()


@igfp.get("/")
async def media(
    access_token: str = Depends(AccessToken),
    context: ContextModel = Depends(Context),
    settings: SettingsModel = Depends(Settings),
) -> None:
    """ """
    now = time()
    if now - context.media_refreshed > settings.media_refresh_delay:
        response = await iggraph.get(
            f"/me/media",
            params={"access_token": access_token, "fields": settings.media_fields},
        )
        # TODO: handle / log error.
        context.media = response.json()
        context.media_refreshed = now
    return context.media
