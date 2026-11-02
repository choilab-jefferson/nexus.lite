"""
nexus_lite.colab.env — Colab/Drive environment helpers (opencode bootstrap, Drive sync, ensure_colab).

Moved out of nexus_lite.core. Depends on the kernel only.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Union

from .._db import get_workspace, init
from ..core import ensure_opencode  # noqa: F401  (kernel keeps the opencode locator; re-exported here)


COLAB_LOCAL_DIR = "/content/.nexus"
COLAB_DRIVE_DIR = "/content/drive/MyDrive/nexus-lite"
_DRIVE_SYNC_DIRS = ("artifacts", "wiki", "stm")


def _colab_sync(src: Path, dst: Path) -> list[str]:
    copied = []
    for name in _DRIVE_SYNC_DIRS:
        if (src / name).is_dir():
            shutil.copytree(src / name, dst / name, dirs_exist_ok=True)
            copied.append(name)
    return copied


def colab_sync_to_drive(drive_dir: Optional[Union[str, Path]] = None) -> list[str]:
    """Explicit export of artifacts, wiki and STM from the local workspace to Drive."""
    return _colab_sync(get_workspace(), Path(drive_dir or COLAB_DRIVE_DIR))


def colab_sync_from_drive(drive_dir: Optional[Union[str, Path]] = None) -> list[str]:
    """Explicit import of artifacts, wiki and STM from Drive into the local workspace."""
    return _colab_sync(Path(drive_dir or COLAB_DRIVE_DIR), get_workspace())



def sync_dotdir_to_drive(
    local_dir: Union[str, Path],
    drive_dir: Union[str, Path],
    overwrite_local_changes: bool = False,
) -> Path:
    """
    Safely link a local dotdir (e.g. ~/.gemini) to a Google Drive directory.

    Guarantees bidirectional persistence via symlink:
    - Creates drive_dir if missing.
    - If local_dir is already a symlink: if pointing to drive_dir, flushes OS buffers
      and returns drive_dir; otherwise unlinks only the symlink and re-points to drive_dir.
      Because ~/.gemini is a symlink directly into drive_dir, any file created or modified
      inside ~/.gemini after sync_dotdir_to_drive runs is written straight to Google Drive.
    - If local_dir is an existing real directory: recursively walks local_dir and copies
      files into drive_dir when the destination file does not exist in drive_dir (or when
      overwrite_local_changes=True). Then rmtree(local_dir) and creates symlink to drive_dir.
    - If local_dir does not exist: creates parent directories and symlinks to drive_dir.
    """
    local_path = Path(local_dir)
    drive_path = Path(drive_dir).resolve()
    drive_path.mkdir(parents=True, exist_ok=True)

    # 1. If local_dir is already a symlink
    if local_path.is_symlink():
        try:
            target = Path(os.readlink(local_path))
            if not target.is_absolute():
                target = (local_path.parent / target).resolve()
            else:
                target = target.resolve()
            if target == drive_path:
                if hasattr(os, "sync"):
                    try:
                        os.sync()
                    except Exception:
                        pass
                return drive_path
        except Exception:
            pass
        local_path.unlink()
        os.symlink(drive_path, local_path)
        return drive_path

    # 2. If local_dir is an existing real directory
    if local_path.is_dir():
        for root, _dirs, files in os.walk(local_path):
            rel_dir = Path(root).relative_to(local_path)
            target_sub_dir = drive_path / rel_dir
            target_sub_dir.mkdir(parents=True, exist_ok=True)
            for fname in files:
                src_file = Path(root) / fname
                dst_file = target_sub_dir / fname
                if src_file.is_symlink() and not src_file.exists():
                    continue
                if overwrite_local_changes or not dst_file.exists():
                    shutil.copy2(src_file, dst_file)
        shutil.rmtree(local_path)
        os.symlink(drive_path, local_path)
        if hasattr(os, "sync"):
            try:
                os.sync()
            except Exception:
                pass
        return drive_path

    # 3. If local_dir does not exist (or is a stale non-dir file)
    if local_path.exists():
        local_path.unlink()
    local_path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(drive_path, local_path)
    return drive_path


class ColabInitResult(dict):
    """
    Result dictionary returned by ensure_colab / ensure_env containing 'workspace' and 'env'.
    Supports Path equality and str conversion for seamless backwards compatibility.
    """

    def __init__(self, workspace: Path, **kwargs: Any) -> None:
        super().__init__(workspace=workspace, **kwargs)
        self.workspace = workspace
        self.env = kwargs.get("env")
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, (Path, str)):
            return self.workspace == Path(other)
        return super().__eq__(other)

    def __fspath__(self) -> str:
        return str(self.workspace)

    def __str__(self) -> str:
        return str(self.workspace)

    def __truediv__(self, other: Any) -> Path:
        return self.workspace / other

    def __rtruediv__(self, other: Any) -> Path:
        return Path(other) / self.workspace


def detect_env() -> str:
    """
    Detect the current execution runtime environment.

    Returns one of:
    - 'colab': Google Colab notebook environment
    - 'codespaces': GitHub Codespaces cloud container
    - 'devcontainer': VS Code Dev Container
    - 'docker': Generic Docker container
    - 'local': Standard local Linux/macOS host
    """
    if "google.colab" in sys.modules or os.environ.get("COLAB_RELEASE_TAG"):
        return "colab"
    if os.environ.get("CODESPACES") == "true":
        return "codespaces"
    if os.environ.get("REMOTE_CONTAINERS") == "true" or (
        Path("/.dockerenv").exists() and Path("/workspaces").exists()
    ):
        return "devcontainer"
    if Path("/.dockerenv").exists():
        return "docker"
    return "local"


def ensure_colab(
    drive_path: Optional[Union[str, Path]] = None,
    gemini_drive_path: Optional[Union[str, Path]] = None,
    mount_drive: Optional[bool] = None,
    install_opencode: Optional[bool] = None,
    **kwargs: Any,
) -> ColabInitResult:
    """
    Ensure dtach is installed, initialize workspace, and optionally mount Drive and sync credentials.

    Parameters:
    - drive_path: Custom workspace directory path on Google Drive or filesystem.
    - gemini_drive_path: Custom path on Google Drive to sync ~/.gemini credentials.
    - mount_drive: Default True when running in Google Colab ('google.colab' in sys.modules), False otherwise.
    - install_opencode: Default True when running in Google Colab ('google.colab' in sys.modules), False otherwise.

    When mount_drive is True and '/content/drive/MyDrive' exists, default persistent_dir
    to '/content/drive/MyDrive/nexus-lite', set NEXUS_LITE_DIR to persistent_dir,
    and call sync_dotdir_to_drive('/root/.gemini', f'{persistent_dir}/.gemini').
    """
    if shutil.which("dtach") is None:
        try:
            subprocess.run(["apt-get", "update", "-qq"], check=False)
            subprocess.run(["apt-get", "install", "-y", "-qq", "dtach"], check=True)
        except Exception as e:
            sys.stderr.write(f"Warning: dtach install failed: {e}\n")

    if install_opencode is None:
        install_opencode = "google.colab" in sys.modules or bool(os.environ.get("COLAB_RELEASE_TAG"))
    opencode_bin = ensure_opencode(install=bool(install_opencode))

    if mount_drive is None:
        mount_drive = "google.colab" in sys.modules

    persistent_dir: Optional[Path] = None
    if drive_path:
        persistent_dir = Path(drive_path).resolve()
        os.environ["NEXUS_LITE_HOME"] = str(persistent_dir)
        os.environ["NEXUS_LITE_DIR"] = str(persistent_dir)
    elif mount_drive and os.path.exists("/content/drive/MyDrive"):
        # DB, sockets and bus stay on local disk (Drive FUSE: unreliable locks, long paths);
        # Drive is only the artifact store, reached via colab_sync_to_drive/colab_sync_from_drive.
        persistent_dir = Path(os.environ.get("NEXUS_LITE_LOCAL_DIR") or COLAB_LOCAL_DIR).resolve()
        os.environ["NEXUS_LITE_HOME"] = str(persistent_dir)
        os.environ["NEXUS_LITE_DIR"] = str(persistent_dir)
        if not os.environ.get("NEXUS_LITE_SOCK_DIR"):
            tag = hashlib.sha256(str(persistent_dir).encode()).hexdigest()[:8]
            os.environ["NEXUS_LITE_SOCK_DIR"] = f"/tmp/nexus-lite-{tag}"

    target_gemini_drive: Optional[Path] = None
    if gemini_drive_path:
        target_gemini_drive = Path(gemini_drive_path)
    elif mount_drive and os.path.exists("/content/drive/MyDrive"):
        # Prefer top-level /content/drive/MyDrive/.gemini if it already exists on Drive,
        # otherwise fall back to <persistent_dir>/.gemini
        top_level_gemini = Path("/content/drive/MyDrive/.gemini")
        if top_level_gemini.exists():
            target_gemini_drive = top_level_gemini
        else:
            target_gemini_drive = Path(COLAB_DRIVE_DIR) / ".gemini"

    synced_gemini: Optional[Path] = None
    if target_gemini_drive is not None:
        local_gemini = Path("/root/.gemini") if ("google.colab" in sys.modules and not gemini_drive_path) else (Path.home() / ".gemini")
        try:
            synced_gemini = sync_dotdir_to_drive(local_gemini, target_gemini_drive)
        except Exception as e:
            sys.stderr.write(f"Warning: failed syncing .gemini to drive ({target_gemini_drive}): {e}\n")

    ws = init(root=persistent_dir)

    return ColabInitResult(
        workspace=ws,
        env="colab",
        gemini_dir=str(synced_gemini) if synced_gemini else None,
        opencode_bin=opencode_bin,
    )


def ensure_env(
    workspace: Optional[Union[str, Path]] = None,
    mount_drive: Optional[bool] = None,
    persistent_dir: Optional[Union[str, Path]] = None,
    gemini_drive_path: Optional[Union[str, Path]] = None,
    install_opencode: Optional[bool] = None,
    **kwargs: Any,
) -> dict:
    """
    Universal entrypoint across Colab, Dev Containers, Codespaces, Docker, and local Linux/macOS.

    Calls detect_env() and returns a dictionary (ColabInitResult) with 'workspace' and 'env'.
    - In 'colab': defaults mount_drive=True (delegating to ensure_colab) and
      automatically syncs ~/.gemini with /content/drive/MyDrive/.gemini (or gemini_drive_path).
    - In 'devcontainer', 'codespaces', 'docker', or 'local': defaults mount_drive=False.
      Ensures dtach is available and initializes workspace (.nexus/).
    """
    env_kind = detect_env()

    if env_kind == "colab":
        actual_mount = True if mount_drive is None else mount_drive
        res = ensure_colab(
            drive_path=persistent_dir or workspace,
            gemini_drive_path=gemini_drive_path,
            mount_drive=actual_mount,
            install_opencode=install_opencode,
            **kwargs,
        )
        res["env"] = "colab"
        return res

    # Non-colab environments: devcontainer, codespaces, docker, local
    if shutil.which("dtach") is None:
        try:
            subprocess.run(["apt-get", "update", "-qq"], check=False)
            subprocess.run(["apt-get", "install", "-y", "-qq", "dtach"], check=True)
        except Exception as e:
            sys.stderr.write(f"Warning: dtach install failed: {e}\n")

    opencode_bin = ensure_opencode(install=bool(install_opencode))

    target_ws = persistent_dir or workspace
    ws = init(root=target_ws)

    return ColabInitResult(workspace=ws, env=env_kind, opencode_bin=opencode_bin)
