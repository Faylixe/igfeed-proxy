import json
import logging
from enum import Enum
from functools import lru_cache
from time import time
from typing import Any, List, Optional, Union, cast

from redis import Redis, from_url as create_redis

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from httpx import AsyncClient, HTTPStatusError, Response
from pydantic import AnyHttpUrl, BaseModel, BaseSettings, validator
from pydantic.tools import parse_obj_as
from starlette.responses import RedirectResponse

api = FastAPI(docs_url=None, openapi_url=None, redoc_url=None)

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


class RedisKeys(str, Enum):
    MEDIA = "igfp:media"
    MEDIA_REFRESHED = "igfp:media-refreshed"
    TOKEN = "igfp:access-token"
    TOKEN_REFRESHED = "igfp:access-token-refreshed"


class Settings(BaseSettings):

    APPLICATION_ID: str
    APPLICATION_SECRET: str
    AUTO_PING_DELAY: int = -1
    DOMAIN: str
    CORS_ORIGINS: Union[str, List[AnyHttpUrl]] = []
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
    REDIS_URL: str
    # NOTE: Consider using Enumeration to avoid wrong scope usage.
    SCOPES: Union[str, List[str]] = ["user_media", "user_profile"]
    TOKEN_REFRESH_DELAY: int = 60 * 60 * 24 * 30
    """ Refresh every 30 days, to be sure we do not miss the window without spamming. """

    class Config:
        env_prefix = "IGFP_"
        fields = {"REDIS_URL": {"env": "REDIS_URL"}}

    @validator("CORS_ORIGINS", pre=True)
    def _assemble_cors_origins(
        cls, origins: Union[str, List[AnyHttpUrl]]
    ) -> List[AnyHttpUrl]:
        if isinstance(origins, str):
            return [
                parse_obj_as(AnyHttpUrl, origin.strip())
                for origin in origins.split(",")
            ]
        return origins

    @validator("SCOPES", pre=True)
    def _assemble_scopes(cls, scopes: Union[str, List[str]]) -> List[str]:
        if isinstance(scopes, str):
            return [scope.strip() for scope in scopes.split(",")]
        return scopes


class Context(BaseModel):

    media: Any = None
    media_refreshed: float = 0
    redis: Redis
    token: Optional[str] = None
    token_refreshed: float = 0

    class Config:
        arbitrary_types_allowed = True


def raise_for_status(response: Response) -> None:
    try:
        response.raise_for_status()
    except HTTPStatusError as e:
        raise HTTPException(detail=str(e), status_code=response.status_code)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_context() -> Context:
    settings = get_settings()
    redis = create_redis(settings.REDIS_URL)
    media = redis.get(RedisKeys.MEDIA)
    media_refreshed = redis.get(RedisKeys.MEDIA_REFRESHED)
    token = redis.get(RedisKeys.TOKEN)
    token_refreshed = redis.get(RedisKeys.TOKEN_REFRESHED)
    if media is not None:
        media = json.loads(media)
    if media_refreshed is not None:
        media_refreshed = float(media_refreshed.decode())
    if token is not None:
        token = token.decode()
    if token_refreshed is not None:
        token_refreshed = float(token_refreshed.decode())
    return Context(
        media=media,
        media_refreshed=media_refreshed,
        redis=redis,
        token=token,
        token_refreshed=token_refreshed,
    )


@lru_cache(maxsize=1)
def get_redirect_uri() -> str:
    settings = get_settings()
    return f"{settings.PROTOCOL}://{settings.DOMAIN}/authorize"


async def get_access_token(
    context: Context = Depends(get_context),
    settings: Settings = Depends(get_settings),
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
        logger.info("Refresh long lived access token")
        response = await iggraph.get(
            f"refresh_access_token",
            params={
                "grant_type": "ig_refresh_token",
                "access_token": context.token,
            },
        )
        response.raise_for_status()
        context.token = cast(str, response.json().get("access_token"))
        context.token_refreshed = now
        context.redis.set(RedisKeys.TOKEN, context.token)
        context.redis.set(RedisKeys.TOKEN_REFRESHED, str(context.token_refreshed))
    return context.token


@api.on_event("startup")
def startup(
    # NOTE: FastAPI doesn't support event callback dependency injection yet :'(
    # redirect_uri: str = Depends(get_redirect_uri),
    # settings: get_settingsModel = Depends(get_settings),
) -> None:
    settings = get_settings()
    if settings.AUTO_PING_DELAY > 0:
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            AsyncClient().get,
            "interval",
            args=(f"{settings.PROTOCOL}://{settings.DOMAIN}",),
            id="autoping",
            minutes=settings.AUTO_PING_DELAY,
        )
        scheduler.start()
    api.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    scopes = ",".join(settings.SCOPES)
    context = get_context()
    if context.token is None:
        logger.info(
            f"To activate Instagram feed proxy please authenticate to "
            f"{igapi.base_url}/oauth/authorize"
            f"?client_id={settings.APPLICATION_ID}"
            f"&redirect_uri={get_redirect_uri(settings)}"
            f"&response_type=code"
            f"&scope={scopes}"
        )


@api.post("/unauthorize")
@api.post("/remove")
async def sink() -> RedirectResponse:
    return RedirectResponse(api.url_path_for("media"))


@api.get("/authorize")
async def authorize(
    code: str,
    context: Context = Depends(get_context),
    redirect_uri: str = Depends(get_redirect_uri),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if context.token is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    # NOTE: Retrieve initial short lived token.
    logger.info("Fetch short lived access token")
    response = await igapi.post(
        "/oauth/access_token",
        data={
            "client_id": settings.APPLICATION_ID,
            "client_secret": settings.APPLICATION_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    raise_for_status(response)
    # NOTE: exchange for 60 days long token.
    logger.info("Exchange for long lived access token")
    response = await iggraph.get(
        "/access_token",
        params={
            "client_secret": settings.APPLICATION_SECRET,
            "grant_type": "ig_exchange_token",
            "access_token": response.json().get("access_token"),
        },
    )
    raise_for_status(response)
    context.token = response.json().get("access_token")
    context.token_refreshed = time()
    context.redis.set(RedisKeys.TOKEN, context.token)
    context.redis.set(RedisKeys.TOKEN_REFRESHED, str(context.token_refreshed))
    return RedirectResponse(api.url_path_for("media"))


@api.get("/")
async def media(
    access_token: str = Depends(get_access_token),
    context: Context = Depends(get_context),
    settings: Settings = Depends(get_settings),
) -> Any:
    now = time()
    if now - context.media_refreshed > settings.MEDIA_REFRESH_DELAY:
        logger.info("Refreshing media content")
        response = await iggraph.get(
            f"/me/media",
            params={"access_token": access_token, "fields": settings.MEDIA_FIELDS},
        )
        raise_for_status(response)
        context.media = response.json()
        context.media_refreshed = now
        context.redis.set(RedisKeys.MEDIA, json.dumps(context.media))
        context.redis.set(RedisKeys.MEDIA_REFRESHED, str(context.media_refreshed))
    return context.media
