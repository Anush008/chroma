from contextvars import ContextVar
from functools import wraps
import logging
from typing import Callable, Optional, Dict, List, Union, cast, Any
from overrides import override
from starlette.middleware.base import (
  BaseHTTPMiddleware, RequestResponseEndpoint
)
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

import chromadb
from chromadb.config import DEFAULT_TENANT, System, Component
from chromadb.auth import (
    AuthorizationContext,
    AuthzAction,
    AuthzResource,
    AuthzResourceActions,
    UserIdentity,
    DynamicAuthzResource,
    ServerAuthenticationProvider,
    ChromaAuthzMiddleware,
    ServerAuthorizationProvider,
)
from chromadb.errors import AuthorizationError
from chromadb.utils.fastapi import fastapi_json_response
from chromadb.telemetry.opentelemetry import (
    OpenTelemetryGranularity,
    trace_method,
)

logger = logging.getLogger(__name__)


request_var: ContextVar[Optional[Request]] = ContextVar("request_var",
                                                        default=None)
authz_provider: ContextVar[Optional[ServerAuthorizationProvider]] = ContextVar(
    "authz_provider", default=None
)

# This needs to be module-level config, since it's used in authz_context()
# where we don't have a system (so don't have easy access to the settings).
overwrite_singleton_tenant_database_access_from_auth: bool = False


def set_overwrite_singleton_tenant_database_access_from_auth(
    overwrite: bool = False,
) -> None:
    global overwrite_singleton_tenant_database_access_from_auth
    overwrite_singleton_tenant_database_access_from_auth = overwrite


def authz_context(
    action: Union[str, AuthzResourceActions, List[str],
                  List[AuthzResourceActions]],
    resource: Union[AuthzResource, DynamicAuthzResource],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(f)
        def wrapped(*args: Any, **kwargs: Dict[Any, Any]) -> Any:
            _dynamic_kwargs = {
                "api": args[0]._api,
                "function": f,
                "function_args": args,
                "function_kwargs": kwargs,
            }
            request = request_var.get()
            if not request:
                return

            _provider = authz_provider.get()
            a_list: List[Union[str, AuthzAction]] = []
            if not isinstance(action, list):
                a_list = [action]
            else:
                a_list = cast(List[Union[str, AuthzAction]], action)
            a_authz_responses = []
            for a in a_list:
                _action = a if isinstance(a, AuthzAction) else AuthzAction(id=a)
                _resource = (
                    resource
                    if isinstance(resource, AuthzResource)
                    else resource.to_authz_resource(**_dynamic_kwargs)
                )
                _context = AuthorizationContext(
                    user=UserIdentity(
                        user_id=request.state.user_identity.get_user_id()
                        if hasattr(request.state, "user_identity")
                        else "Anonymous",
                        tenant=request.state.user_identity.get_user_tenant()
                        if hasattr(request.state, "user_identity")
                        else DEFAULT_TENANT,
                        attributes=request.state.user_identity.get_user_attributes()
                        if hasattr(request.state, "user_identity")
                        else {},
                    ),
                    resource=_resource,
                    action=_action,
                )

                if _provider:
                    a_authz_responses.append(_provider.authorize(_context))
            if not any(a_authz_responses):
                raise AuthorizationError("Unauthorized")

            # In a multi-tenant environment, we may want to allow users to send
            # requests without configuring a tenant and DB. If so, they can set
            # the request tenant and DB however they like and we simply overwrite it.
            if overwrite_singleton_tenant_database_access_from_auth:
                desired_tenant = request.state.user_identity.get_user_tenant()
                if desired_tenant and "tenant" in kwargs:
                    if isinstance(kwargs["tenant"], str):
                        kwargs["tenant"] = desired_tenant
                    elif isinstance(
                        kwargs["tenant"], chromadb.server.fastapi.types.CreateTenant
                    ):
                        kwargs["tenant"].name = desired_tenant
                databases = request.state.user_identity.get_user_databases()
                if databases and len(databases) == 1 and "database" in kwargs:
                    desired_database = databases[0]
                    if isinstance(kwargs["database"], str):
                        kwargs["database"] = desired_database
                    elif isinstance(
                        kwargs["database"],
                        chromadb.server.fastapi.types.CreateDatabase,
                    ):
                        kwargs["database"].name = desired_database

            return f(*args, **kwargs)

        return wrapped

    return decorator
