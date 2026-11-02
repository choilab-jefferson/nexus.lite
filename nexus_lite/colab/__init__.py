"""
nexus_lite.colab — Standalone Colab utilities: remote execution, tunneling, Drive sync and env bootstrap.

Depends on the kernel only; the kernel never imports this package.
"""

from __future__ import annotations

from .env import (
    ColabInitResult,
    colab_sync_from_drive,
    colab_sync_to_drive,
    detect_env,
    ensure_colab,
    ensure_env,
    sync_dotdir_to_drive,
)
from .tunnel import (
    DEFAULT_EXEC_TIMEOUT,
    MAX_BODY_BYTES,
    MAX_EXEC_TIMEOUT,
    TUNNEL_SERVICE,
    ColabTunnelClient,
    TunnelRequestHandler,
    ensure_cloudflared,
    get_tunnel_info,
    is_tunnel_alive,
    redact_tunnel_info,
    start_tunnel,
    stop_tunnel,
    token_file_path,
)

__all__ = [
    "ColabInitResult",
    "colab_sync_from_drive",
    "colab_sync_to_drive",
    "detect_env",
    "ensure_colab",
    "ensure_env",
    "sync_dotdir_to_drive",
    "DEFAULT_EXEC_TIMEOUT",
    "MAX_BODY_BYTES",
    "MAX_EXEC_TIMEOUT",
    "TUNNEL_SERVICE",
    "ColabTunnelClient",
    "TunnelRequestHandler",
    "ensure_cloudflared",
    "get_tunnel_info",
    "is_tunnel_alive",
    "redact_tunnel_info",
    "start_tunnel",
    "stop_tunnel",
    "token_file_path",
]
