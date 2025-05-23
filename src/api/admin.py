"""VeriFact Admin API.

This module contains admin endpoints for the VeriFact API,
including API key management and system configuration.
"""

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.api.middleware import verify_api_key
from src.models.security import ApiKey, ApiKeyScope
from src.utils.cache.cache import (
    claim_cache,
    entity_cache,
    evidence_cache,
    model_cache,
    search_cache,
)
from src.utils.db.api_keys import (
    create_api_key,
    list_api_keys,
    list_user_api_keys,
    revoke_api_key,
    rotate_api_key,
)
from src.utils.exceptions import DatabaseError, InvalidAPIKeyError
from src.utils.metrics import (
    claims_metrics,
    evidence_metrics,
    model_metrics,
    search_metrics,
)
from src.utils.metrics.db_metrics import ConnectionPoolMetrics

# Create router
router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    responses={404: {"description": "Not found"}, 500: {"description": "Internal server error"}},
)


# Models for API key management
class ApiKeyRequest(BaseModel):
    """Request for creating a new API key."""

    user_id: str | None = None
    permissions: list[str] | None = None
    expiry_days: int | None = None


class ApiKeyResponse(BaseModel):
    """Response containing an API key."""

    key: str
    prefix: str
    expires_at: str
    permissions: list[str]
    user_id: str | None = None


class ApiKeyInfo(BaseModel):
    """Information about an API key without the actual key."""

    id: str
    prefix: str
    expires_at: str
    permissions: list[str]
    created_at: str


# Models for cache invalidation
class InvalidateCacheRequest(BaseModel):
    """Request for invalidating cache entries."""

    namespace: str = Field(
        ...,
        description="The cache namespace to invalidate: 'evidence', 'claims', 'search_results', etc.",
    )
    pattern: str | None = Field(None, description="Optional pattern to match specific cache keys")
    reason: str | None = Field(None, description="Optional reason for cache invalidation")


class InvalidateCacheResponse(BaseModel):
    """Response for cache invalidation."""

    success: bool
    message: str
    invalidated_namespace: str
    invalidated_count: int | None = None


async def require_admin(request: Request) -> dict[str, Any]:
    """Dependency to check if the requester has admin permissions.

    Args:
        request: The request object

    Returns:
        Dict[str, Any]: API key data

    Raises:
        HTTPException: If the requester is not an admin
    """
    # Get API key data from request state (set by APIKeyAuthMiddleware)
    api_key_data = getattr(request.state, "api_key_data", None)

    if not api_key_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin authentication required"
        )

    # Check if the API key has admin permissions
    permissions = api_key_data.get("permissions", [])
    if "admin:keys" not in permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin permission required"
        )

    return api_key_data


@router.post(
    "/keys",
    response_model=ApiKeyResponse,
    summary="Create a new API key",
    description="""
    Create a new API key with the specified permissions.

    Requires admin permissions.

    The API key will be returned only once, so make sure to store it securely.
    """,
)
async def create_key(request: ApiKeyRequest = None, admin: dict[str, Any] = None):
    """Create a new API key."""
    if request is None:
        request = Body()
    if admin is None:
        admin = require_admin()

    try:
        # Create the API key
        key, key_data = await create_api_key(
            name="API Key",  # Default name
            owner_id=request.user_id or "default",
            scopes=request.permissions or [ApiKeyScope.READ_ONLY],
            expires_at=(
                datetime.utcnow() + timedelta(days=request.expiry_days)
                if request.expiry_days
                else None
            ),
        )

        # Return the key and metadata
        return ApiKeyResponse(
            key=key,
            prefix=key_data["prefix"],
            expires_at=key_data["expires_at"],
            permissions=key_data["permissions"],
            user_id=key_data["user_id"],
        )
    except DatabaseError as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create API key: {str(err)}",
        ) from err


@router.delete(
    "/keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
    description="""
    Revoke an API key by ID.

    Requires admin permissions.

    Once revoked, the API key can no longer be used.
    """,
)
async def revoke_key(key: str, admin: dict[str, Any] = None):
    """Revoke an API key."""
    if admin is None:
        admin = require_admin()

    try:
        # Revoke the API key
        result = await revoke_api_key(key)

        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="API key not found or already revoked"
            )

        return None  # 204 No Content
    except DatabaseError as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to revoke API key: {str(err)}",
        ) from err


@router.post(
    "/keys/{key}/rotate",
    response_model=ApiKeyResponse,
    summary="Rotate an API key",
    description="""
    Rotate an API key by revoking the old key and creating a new one.

    Requires admin permissions.

    The new API key will be returned only once, so make sure to store it securely.
    """,
)
async def rotate_key(key: str, admin: dict[str, Any] = None):
    """Rotate an API key."""
    if admin is None:
        admin = require_admin()

    try:
        # Rotate the API key
        new_key, key_data = await rotate_api_key(key)

        # Return the new key and metadata
        return ApiKeyResponse(
            key=new_key,
            prefix=key_data["prefix"],
            expires_at=key_data["expires_at"],
            permissions=key_data["permissions"],
            user_id=key_data["user_id"],
        )
    except InvalidAPIKeyError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API key not found or invalid: {str(err)}",
        ) from err
    except DatabaseError as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to rotate API key: {str(err)}",
        ) from err


@router.get(
    "/users/{user_id}/keys",
    response_model=list[ApiKeyInfo],
    summary="List API keys for a user",
    description="""
    List all active API keys for a user.

    Requires admin permissions.

    Returns only metadata about the keys, not the actual keys.
    """,
)
async def list_keys(user_id: str, admin: dict[str, Any] = None):
    """List API keys for a user."""
    if admin is None:
        admin = require_admin()

    try:
        # Get all API keys for the user
        api_keys = await list_user_api_keys(user_id)

        # Transform to response format
        return [
            ApiKeyInfo(
                id=key["id"],
                prefix=key["prefix"],
                expires_at=key["expires_at"],
                permissions=key["permissions"],
                created_at=key["created_at"],
            )
            for key in api_keys
        ]
    except DatabaseError as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list API keys: {str(err)}",
        ) from err


@router.post(
    "/cache/invalidate",
    status_code=status.HTTP_200_OK,
    response_model=InvalidateCacheResponse,
    summary="Invalidate cache entries",
    description="""
    Invalidate cache entries based on namespace and optional pattern.

    Requires admin permissions.

    This is useful for clearing stale data when underlying information changes.
    """,
)
async def invalidate_cache(
    request: InvalidateCacheRequest = None, admin: dict[str, Any] = None
) -> dict[str, Any]:
    """Invalidate cache entries."""
    if request is None:
        request = InvalidateCacheRequest()
    if admin is None:
        admin = require_admin()

    # Map namespace to cache instance
    cache_map = {
        "evidence": evidence_cache,
        "claims": claim_cache,
        "entities": entity_cache,
        "search_results": search_cache,
        "model_responses": model_cache,
    }

    # Check if namespace exists
    if request.namespace not in cache_map:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid cache namespace: {request.namespace}. Valid options are: {', '.join(cache_map.keys())}",
        )

    cache = cache_map[request.namespace]

    try:
        # Clear the entire namespace
        success = cache.clear_namespace()

        # Log the operation
        log_message = f"Cache invalidation: namespace={request.namespace}"
        if request.pattern:
            log_message += f", pattern={request.pattern}"
        if request.reason:
            log_message += f", reason={request.reason}"

        return {
            "success": success,
            "message": f"Successfully invalidated {request.namespace} cache",
            "invalidated_namespace": request.namespace,
            "invalidated_count": None,  # We don't track count in current implementation
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to invalidate cache: {str(e)}",
        ) from e


@router.get(
    "/cache/status",
    status_code=status.HTTP_200_OK,
    summary="Get cache status",
    description="""
    Get status information about the caching system.

    Requires admin permissions.

    Returns information about the Redis connection and configured TTLs.
    """,
)
async def cache_status(admin: dict[str, Any] = None) -> dict[str, Any]:
    """Get cache status."""
    if admin is None:
        admin = require_admin()

    import os

    from src.utils.cache.cache import DEFAULT_CACHE_TTL, REDIS_ENABLED, REDIS_URL

    # Get evidence-specific TTL
    evidence_ttl = int(os.environ.get("EVIDENCE_CACHE_TTL", DEFAULT_CACHE_TTL))

    return {
        "redis_enabled": REDIS_ENABLED,
        "redis_url": (
            REDIS_URL.replace(os.environ.get("REDIS_PASSWORD", ""), "***")
            if REDIS_ENABLED
            else None
        ),
        "default_ttl": DEFAULT_CACHE_TTL,
        "evidence_ttl": evidence_ttl,
        "cache_namespaces": ["evidence", "claims", "entities", "search_results", "model_responses"],
    }


@router.get(
    "/cache/metrics",
    status_code=status.HTTP_200_OK,
    summary="Get cache metrics",
    description="""
    Get performance metrics for the caching system.

    Requires admin permissions.

    Returns metrics like hit rate, miss rate, and latency information.
    """,
)
async def cache_metrics(
    namespace: str | None = None, admin: dict[str, Any] = None
) -> dict[str, Any]:
    """Get cache metrics."""
    if admin is None:
        admin = require_admin()

    # Map namespace to metrics instance
    metrics_map = {
        "evidence": evidence_metrics,
        "claims": claims_metrics,
        "search_results": search_metrics,
        "model_responses": model_metrics,
    }

    if namespace and namespace not in metrics_map:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid metrics namespace: {namespace}. Valid options are: {', '.join(metrics_map.keys())}",
        )

    # Return metrics for a specific namespace
    if namespace:
        return metrics_map[namespace].stats()

    # Return metrics for all namespaces
    return {
        "all_metrics": {name: metrics.stats() for name, metrics in metrics_map.items()},
        "timestamp": datetime.datetime.now().isoformat(),
    }


@router.get(
    "/metrics/database",
    status_code=status.HTTP_200_OK,
    summary="Get database connection pool metrics",
    description="""
    Get metrics for the database connection pool.

    Requires admin permissions.

    Returns information about connection usage, pool size, and status.
    """,
)
async def database_metrics(admin: dict[str, Any] = None) -> dict[str, Any]:
    """Get database metrics."""
    if admin is None:
        admin = require_admin()

    try:
        metrics = await ConnectionPoolMetrics.collect()

        # Add timestamp to the metrics
        result = {"metrics": metrics, "timestamp": datetime.datetime.now().isoformat()}

        return result
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get database metrics: {str(e)}",
        ) from e


@router.post("/api-keys", response_model=dict)
async def create_new_api_key(
    name: str,
    scopes: list[ApiKeyScope],
    expires_days: int = 365,
    owner_id: str = None,
):
    """Create a new API key with the given parameters."""
    # Get owner_id from verification
    if owner_id is None:
        owner_id = verify_api_key(required_scopes=[ApiKeyScope.ADMIN])
    expires_at = datetime.utcnow() + timedelta(days=expires_days)

    # Create the key
    api_key, plain_key = await create_api_key(name, owner_id, scopes, expires_at)

    # Return the plain key - this is the only time it will be visible
    return {
        "id": api_key.id,
        "key": plain_key,  # Full key shown only once
        "name": api_key.name,
        "scopes": api_key.scopes,
        "expires_at": api_key.expires_at,
        "note": "Store this key securely. It will not be shown again.",
    }


@router.get("/api-keys", response_model=list[ApiKey])
async def get_api_keys(
    owner_id: str = Depends(lambda: verify_api_key(required_scopes=[ApiKeyScope.ADMIN])),
):
    """List all API keys (without the actual key values)."""
    return await list_api_keys(owner_id)


@router.delete("/api-keys/{key_id}")
async def delete_api_key(
    key_id: str,
    owner_id: str = Depends(lambda: verify_api_key(required_scopes=[ApiKeyScope.ADMIN])),
):
    """Revoke an API key."""
    success = await revoke_api_key(key_id, owner_id)
    if not success:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"status": "success", "message": "API key revoked"}
