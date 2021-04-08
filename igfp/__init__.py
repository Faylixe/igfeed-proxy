import logging

from enum import Enum
from functools import lru_cache
from time import time
from typing import Any, List, Optional, cast

from fastapi import Depends, FastAPI, HTTPException, status
from httpx import AsyncClient, HTTPStatusError, Response
from pydantic import BaseModel, BaseSettings

igfp = FastAPI(docs_url=None, openapi_url=None, redoc_url=None)
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

handler = logging.StreamHandler()
handler.setFormatter(
    # NOTE: use the same log format than Hypercorn.
    logging.Formatter(
        "%(asctime)s [%(process)d] [%(levelname)s] %(message)s",
        "[%Y-%m-%d %H:%M:%S %z]",
    )
)
logger = logging.getLogger(__name__)
logger.addHandler(handler)
logger.setLevel(logging.INFO)


class ProtocolEnum(str, Enum):
    HTTP = "http"
    HTTPS = "https"


class SettingsModel(BaseSettings):

    APPLICATION_ID: str
    APPLICATION_SECRET: str
    DOMAIN: str
    MEDIA_FIELDS: str = (
        "caption,"
        "id,"
        "media_type,"
        "media_url,"
        "permalink,"
        "thumbnail_url,"
        "timestamp,"
        "username,"
        "children{"
        "id,"
        "media_type,"
        "media_url,"
        "permalink,"
        "thumbnail_url,"
        "timestamp,"
        "username"
        "}"
    )
    MEDIA_REFRESH_DELAY: int = 60 * 5
    """ Refresh media every 5 minutes to avoid API rate limiting. """
    PROTOCOL: ProtocolEnum = ProtocolEnum.HTTPS
    SCOPES: List[str] = ["user_media", "user_profile"]
    TOKEN_REFRESH_DELAY: int = 60 * 60 * 24 * 30
    """ Refresh every 30 days, to be sure we do not miss the window without spamming. """

    class Config:
        env_prefix = "IGFP_"


class ContextModel(BaseModel):

    media: Any = None
    media_refreshed: float = 0
    token: Optional[str] = None
    token_refreshed: float = 0


def raise_for_status(response: Response) -> None:
    try:
        response.raise_for_status()
    except HTTPStatusError as e:
        raise HTTPException(detail=str(e), status_code=response.status_code)


@lru_cache(maxsize=1)
def Context() -> ContextModel:
    return ContextModel()


@lru_cache(maxsize=1)
def Settings() -> SettingsModel:
    return SettingsModel()


def RedirectURI(settings: SettingsModel = Depends(Settings)) -> str:
    return f"{settings.PROTOCOL}://{settings.DOMAIN}/authorize"


async def AccessToken(
    context: ContextModel = Depends(Context),
    settings: SettingsModel = Depends(Settings),
) -> str:
    """
    Retrieve and return API access token, refreshing it if necessary.
    Aims to be used as a dependency to ensure continuous token refresh.
    """
    if context.token is None:
        raise HTTPException(
            detail="You must authorize application first",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    now = time()
    if now - context.token_refreshed > settings.TOKEN_REFRESH_DELAY:
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
    return cast(str, context.token)


@igfp.on_event("startup")
def startup(
    # NOTE: FastAPI doesn't support event callback dependency injection yet :'(
    # redirect_uri: str = Depends(RedirectURI),
    # settings: SettingsModel = Depends(Settings),
) -> None:
    settings = Settings()
    scopes = ",".join(settings.SCOPES)
    logger.info(
        f"To activate Instagram feed proxy please authenticate to "
        f"{igapi.base_url}/oauth/authorize"
        f"?client_id={settings.APPLICATION_ID}"
        f"&redirect_uri={RedirectURI(settings)}"
        f"&response_type=code"
        f"&scope={scopes}"
    )


@igfp.get("/")
@igfp.post("/unauthorize")
@igfp.post("/remove")
async def placeholder() -> None:
    pass


@igfp.get("/authorize")
async def authorize(
    code: str,
    context: ContextModel = Depends(Context),
    redirect_uri: str = Depends(RedirectURI),
    settings: SettingsModel = Depends(Settings),
) -> None:
    if context.token is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    # NOTE: Retrieve initial token.
    response = await igapi.post(
        "/oauth/access_token",
        json={
            "app_id": settings.APPLICATION_ID,
            "app_secret": settings.APPLICATION_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    raise_for_status(response)
    initial_token = response.json()
    # NOTE: exchange for 60 days long token.
    response = await iggraph.get(
        "/access_token",
        params={
            "client_secret": settings.APPLICATION_SECRET,
            "grant_type": "ig_exchange_token",
            "access_token": initial_token,
        },
    )
    raise_for_status(response)
    context.token = response.json().get("access_token")
    context.token_refreshed = time()
    # TODO: redirect to home.


@igfp.get("/media")
async def media(
    access_token: str = Depends(AccessToken),
    context: ContextModel = Depends(Context),
    settings: SettingsModel = Depends(Settings),
) -> Any:
    """ """
    now = time()
    if now - context.media_refreshed > settings.MEDIA_REFRESH_DELAY:
        response = await iggraph.get(
            f"/me/media",
            params={"access_token": access_token, "fields": settings.MEDIA_FIELDS},
        )
        raise_for_status(response)
        context.media = response.json()
        context.media_refreshed = now
    return context.media
