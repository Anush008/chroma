import json
import logging
import string
from enum import Enum
from typing import List, Optional, TypedDict, cast, Dict, TypeVar

from overrides import override
from pydantic import SecretStr
import yaml

from chromadb.auth import (
    ServerAuthProvider,
    ClientAuthProvider,
    ServerAuthCredentialsProvider,
    ClientAuthHeaders,
    SecretStrAbstractCredentials,
    AbstractCredentials,
    ServerAuthenticationResponse,
    UserIdentity,
)
from chromadb.config import System
from chromadb.telemetry.opentelemetry import (
    OpenTelemetryGranularity,
    trace_method,
)
from starlette.datastructures import Headers

T = TypeVar("T")

logger = logging.getLogger(__name__)

__all__ = ["TokenAuthServerProvider", "TokenAuthClientProvider"]


class TokenTransportHeader(Enum):
    AUTHORIZATION = "Authorization"
    X_CHROMA_TOKEN = "X-Chroma-Token"


def check_token(token: str) -> None:
    token_str = str(token)
    if not all(
        c in string.digits + string.ascii_letters + string.punctuation
        for c in token_str
    ):
        raise ValueError("Invalid token. Must contain \
                         only ASCII letters and digits.")


class Token(TypedDict):
    token: str
    secret: str


class UserTokenConfigServerAuthCredentialsProvider(
    ServerAuthCredentialsProvider
):
    class User(TypedDict):
        id: str
        role: str
        tenant: Optional[str]
        databases: Optional[List[str]]
        tokens: List[Token]

    _users: List[User]
    _token_user_mapping: Dict[str, str]  # reverse mapping of token to user

    def __init__(self, system: System) -> None:
        super().__init__(system)
        if not system.settings.chroma_server_auth_credentials_file:
            raise ValueError("chroma_server_auth_credentials_file not set")

        system.settings.require("chroma_server_auth_credentials_file")
        user_file = str(
            system.settings.chroma_server_auth_credentials_file
        )
        with open(user_file) as f:
            self._users = cast(List[User], yaml.safe_load(f)["users"])

        self._token_user_mapping = {}
        for user in self._users:
            for t in user["tokens"]:
                token_str = t["token"]
                check_token(token_str)
                if token_str in self._token_user_mapping:
                    raise ValueError("Token already exists for another user")
                self._token_user_mapping[token_str] = user["id"]

    def find_user_by_id(self, _user_id: str) -> Optional[User]:
        for user in self._users:
            if user["id"] == _user_id:
                return user
        return None

    @override
    def validate_credentials(self,
                             credentials: AbstractCredentials[T]) -> bool:
        _creds = cast(Dict[str, SecretStr], credentials.get_credentials())
        if "token" not in _creds:
            logger.error("Returned credentials do not contain token")
            return False
        return _creds["token"].get_secret_value() in \
            self._token_user_mapping.keys()

    @override
    def get_user_identity(
        self, credentials: AbstractCredentials[T]
    ) -> Optional[UserIdentity]:
        _creds = cast(Dict[str, SecretStr], credentials.get_credentials())
        if "token" not in _creds:
            logger.error("Returned credentials do not contain token")
            return None
        # below is just simple identity mapping and may need
        # future work for more complex use cases
        _user_id = self._token_user_mapping[_creds["token"].get_secret_value()]
        _user = self.find_user_by_id(_user_id)
        return UserIdentity(
            user_id=_user_id,
            tenant=_user["tenant"] if _user and "tenant" in _user else "*",
            databases=_user[
                "databases"
            ] if _user and "databases" in _user else ["*"],
        )


class TokenAuthCredentials(SecretStrAbstractCredentials):
    _token: SecretStr

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    @override
    def get_credentials(self) -> Dict[str, SecretStr]:
        return {"token": self._token}

    @staticmethod
    def from_header(
        header: str,
        token_transport_header: TokenTransportHeader =
            TokenTransportHeader.AUTHORIZATION,
    ) -> "TokenAuthCredentials":
        """
        Extracts token from header and returns a TokenAuthCredentials object.
        """
        if token_transport_header == TokenTransportHeader.AUTHORIZATION:
            header = header.replace("Bearer ", "")
            header = header.strip()
            token = header
        elif token_transport_header == TokenTransportHeader.X_CHROMA_TOKEN:
            header = header.strip()
            token = header
        else:
            raise ValueError(
                f"Invalid token transport header: {token_transport_header}"
            )
        return TokenAuthCredentials(SecretStr(token))


class TokenAuthServerProvider(ServerAuthProvider):
    _credentials_provider: ServerAuthCredentialsProvider
    _token_transport_header: TokenTransportHeader = \
        TokenTransportHeader.AUTHORIZATION

    def __init__(self, system: System) -> None:
        super().__init__(system)
        self._settings = system.settings
        self._credentials_provider = system.require(
            system.settings.chroma_server_auth_credentials_provider
        )
        if system.settings.chroma_auth_token_transport_header:
            self._token_transport_header = TokenTransportHeader[
                str(system.settings.chroma_auth_token_transport_header)
            ]

    @trace_method("TokenAuthServerProvider.authenticate",
                  OpenTelemetryGranularity.ALL)
    @override
    def authenticate(
        self, headers: Headers
    ) -> ServerAuthenticationResponse:
        try:
            _auth_header = headers[
                self._token_transport_header.value
            ]
            _token_creds = TokenAuthCredentials.from_header(
                _auth_header, self._token_transport_header
            )
            return ServerAuthenticationResponse(
                self._credentials_provider.validate_credentials(_token_creds),
                self._credentials_provider.get_user_identity(_token_creds),
            )
        except Exception as e:
            logger.error(
                f"TokenAuthServerProvider.authenticate failed: {repr(e)}"
            )
            return ServerAuthenticationResponse(False, None)


class TokenAuthClientProvider(ClientAuthProvider):
    _token_transport_header: TokenTransportHeader = \
        TokenTransportHeader.AUTHORIZATION

    def __init__(self, system: System) -> None:
        super().__init__(system)
        self._settings = system.settings

        system.settings.require("chroma_client_auth_credentials")
        self._token = SecretStr(
            str(system.settings.chroma_client_auth_credentials)
        )
        check_token(self._token.get_secret_value())

        if system.settings.chroma_auth_token_transport_header:
            self._token_transport_header = TokenTransportHeader[
                str(system.settings.chroma_auth_token_transport_header)
            ]

    @trace_method("TokenAuthClientProvider.authenticate",
                  OpenTelemetryGranularity.ALL)
    @override
    def authenticate(self) -> ClientAuthHeaders:
        key = None
        if type == TokenTransportHeader.AUTHORIZATION:
            key = "Authorization"
        elif type == TokenTransportHeader.X_CHROMA_TOKEN:
            key = "X-Chroma-Token"
        else:
            raise ValueError(f"Invalid token transport header: {type}")
        return {key: SecretStr(self._token.get_secret_value())}
