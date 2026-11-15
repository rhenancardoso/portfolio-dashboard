"""Connection settings for Moomoo OpenD, and how authentication works.

The application never sees Moomoo credentials. Moomoo's gateway program, OpenD,
is logged in to your moomoo ID (login password and any device verification are
handled by OpenD). The application only connects to OpenD over TCP and uses
read-only queries, so it never needs or unlocks the trading password.

If OpenD runs on another machine, enable RSA protocol encryption: configure the
same private key in OpenD and point ``MOOMOO_RSA_KEY_PATH`` at it. Keep the key
outside source control.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

TRADING_ENVIRONMENTS = ("REAL", "SIMULATE")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class MoomooConfigError(ValueError):
    """Invalid Moomoo/OpenD configuration."""


@dataclass(frozen=True)
class OpenDSettings:
    """Where OpenD runs and which accounts to read."""

    host: str = "127.0.0.1"
    port: int = 11111
    security_firm: str = "FUTUAU"
    trading_environment: str = "REAL"
    rsa_private_key_path: Path | None = None
    connect_timeout_seconds: int = 15


def load_opend_settings() -> OpenDSettings:
    """Build OpenD settings from environment variables.

    MOOMOO_OPEND_HOST       - OpenD host (default: 127.0.0.1)
    MOOMOO_OPEND_PORT       - OpenD API port (default: 11111)
    MOOMOO_SECURITY_FIRM    - broker entity (default: FUTUAU, i.e. Moomoo AU)
    MOOMOO_TRADING_ENV      - REAL or SIMULATE (default: REAL)
    MOOMOO_RSA_KEY_PATH     - RSA private key for protocol encryption (optional)
    MOOMOO_CONNECT_TIMEOUT  - seconds to wait for OpenD (default: 15)
    """
    try:
        port = int(os.environ.get("MOOMOO_OPEND_PORT", "11111"))
        timeout = int(os.environ.get("MOOMOO_CONNECT_TIMEOUT", "15"))
    except ValueError as exc:
        raise MoomooConfigError(f"Invalid number in Moomoo settings: {exc}") from exc

    environment = os.environ.get("MOOMOO_TRADING_ENV", "REAL").upper()
    if environment not in TRADING_ENVIRONMENTS:
        raise MoomooConfigError(f"MOOMOO_TRADING_ENV must be one of {TRADING_ENVIRONMENTS}")

    key_value = os.environ.get("MOOMOO_RSA_KEY_PATH")
    key_path = Path(key_value) if key_value else None
    if key_path is not None and not key_path.is_file():
        raise MoomooConfigError(f"MOOMOO_RSA_KEY_PATH does not exist: {key_path}")

    settings = OpenDSettings(
        host=os.environ.get("MOOMOO_OPEND_HOST", "127.0.0.1"),
        port=port,
        security_firm=os.environ.get("MOOMOO_SECURITY_FIRM", "FUTUAU").upper(),
        trading_environment=environment,
        rsa_private_key_path=key_path,
        connect_timeout_seconds=timeout,
    )
    if settings.host not in LOCAL_HOSTS and key_path is None:
        logger.warning(
            "OpenD at %s is not local and protocol encryption is off; set MOOMOO_RSA_KEY_PATH",
            settings.host,
        )
    return settings


def configure_sdk(settings: OpenDSettings) -> None:
    """Apply process-wide SDK options. Must run before a context is created."""
    from moomoo import SysConfig

    SysConfig.set_all_thread_daemon(True)
    if settings.rsa_private_key_path is not None:
        SysConfig.enable_proto_encrypt(True)
        SysConfig.set_init_rsa_file(str(settings.rsa_private_key_path))
