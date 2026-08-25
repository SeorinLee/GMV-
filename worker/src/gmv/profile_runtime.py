"""Disposable browser-profile snapshots used by GMV jobs.

Login/verify/reset own the persistent source profile. A normal job gets a private snapshot so
multiple jobs from the same TikTok login can launch independent Chromium contexts without
contending for the source profile's SingletonLock.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from gmv.config import BrowserProfile, default_runtime_profile_root

# Runtime locks must never be copied. LevelDB's LOCK files are recreated and do not contain login
# data. Matching is case-insensitive because Chrome and Edge differ across platforms/releases.
_RUNTIME_FILE_NAMES = {
    "devtoolsactiveport",
    "lock",
    "singletoncookie",
    "singletonlock",
    "singletonsocket",
}

# These directories are disposable caches only. Authentication state remains in Cookies, Local
# Storage, IndexedDB, Preferences, Secure Preferences, Login Data, and Network storage, all of
# which are intentionally copied.
_CACHE_DIRECTORY_NAMES = {
    "browsermetrics",
    "cache",
    "cachestorage",
    "code cache",
    "component_crx_cache",
    "crashpad",
    "dawncache",
    "dawngraphitecache",
    "dawnwebgpucache",
    "deferredbrowsermetrics",
    "extensions_crx_cache",
    "gpucache",
    "gpupersistentcache",
    "grshadercache",
    "optimization_guide_model_store",
    "segmentation_platform",
    "shadercache",
    "scriptcache",
}


class RuntimeProfileError(RuntimeError):
    """A job profile could not be cloned safely."""


def _safe_job_component(job_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(job_id)).strip(".-")
    return value[:12] or "job"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _absolute(path: Path) -> Path:
    """Normalize lexically; resolving two not-yet-created clones concurrently is racy on Windows."""
    return Path(os.path.abspath(os.fspath(path)))


def _validate_paths(source: Path, runtime_root: Path, destination: Path) -> None:
    source = _absolute(source)
    runtime_root = _absolute(runtime_root)
    destination = _absolute(destination)
    if source == destination:
        raise RuntimeProfileError("runtime profile must differ from source profile")
    if not _is_within(destination, runtime_root):
        raise RuntimeProfileError("runtime profile escaped its configured root")
    # Prevent recursive copies and ensure cleanup can never target the persistent source profile.
    if _is_within(runtime_root, source) or _is_within(source, runtime_root):
        raise RuntimeProfileError("source and runtime profile roots must be separate")


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        folded = name.casefold()
        if folded in _RUNTIME_FILE_NAMES or folded in _CACHE_DIRECTORY_NAMES:
            ignored.add(name)
    return ignored


def _remove_readonly(func, path, _exc_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _cleanup_tree(path: Path, runtime_root: Path) -> None:
    resolved_path = _absolute(path)
    resolved_root = _absolute(runtime_root)
    if resolved_path == resolved_root or not _is_within(resolved_path, resolved_root):
        raise RuntimeProfileError("refusing to clean a path outside the runtime profile root")
    if path.exists():
        shutil.rmtree(path, onerror=_remove_readonly)


def _clone_profile(
    source: Path,
    runtime_root: Path,
    profile_code: str,
    job_id: str,
) -> Path:
    profile_root = runtime_root / profile_code
    destination = profile_root / f"{_safe_job_component(job_id)}-{uuid.uuid4().hex[:8]}"
    _validate_paths(source, runtime_root, destination)
    profile_root.mkdir(parents=True, exist_ok=True)
    try:
        if source.exists():
            shutil.copytree(source, destination, ignore=_copy_ignore)
        else:
            # Tests and first-run users may not have logged in yet. An empty private directory is
            # still required so the resulting browser can report LOGIN_REQUIRED without touching
            # or creating the source profile.
            destination.mkdir()
    except Exception as exc:
        try:
            _cleanup_tree(destination, runtime_root)
        except Exception:
            pass
        raise RuntimeProfileError(
            f"could not clone browser profile ({type(exc).__name__}: {exc})"
        ) from exc
    return destination


def cleanup_stale_runtime_profiles(runtime_root: Path | None = None) -> None:
    """Remove clone directories left by a previous interrupted worker process."""
    root = _absolute(runtime_root or default_runtime_profile_root())
    if not root.exists():
        return
    for profile_root in root.iterdir():
        if not profile_root.is_dir():
            continue
        for clone in profile_root.iterdir():
            if clone.is_dir():
                try:
                    _cleanup_tree(clone, root)
                except Exception as exc:  # noqa: BLE001 - one stale clone must not block startup
                    print(
                        f"[profile-runtime] action=stale-cleanup-failed "
                        f"path={clone} error={type(exc).__name__}: {exc}"
                    )


@asynccontextmanager
async def cloned_profile_for_job(
    source_profile: BrowserProfile,
    job_id: str,
    *,
    source_lock: asyncio.Lock | None = None,
    runtime_root: Path | None = None,
) -> AsyncIterator[BrowserProfile]:
    """Yield one private profile for a job and always attempt cleanup afterward.

    ``source_lock`` is held only while taking the snapshot. Browser execution uses only the clone,
    allowing subsequent snapshots and source-profile management once copying finishes.
    """
    source = Path(source_profile.storage_root)
    root = runtime_root or default_runtime_profile_root()

    async def clone() -> Path:
        return await asyncio.to_thread(
            _clone_profile,
            source,
            root,
            source_profile.profile_code,
            job_id,
        )

    if source_lock is None:
        destination = await clone()
    else:
        async with source_lock:
            destination = await clone()

    runtime_profile = source_profile.model_copy(
        update={
            "storage_root_override": str(destination),
            "runtime_job_id": str(job_id),
        }
    )
    print(
        f"[profile-runtime] action=clone-created job_id={job_id} "
        f"profile={source_profile.profile_code} browser={source_profile.browser_channel} "
        f"source={source} temp={destination}"
    )
    try:
        yield runtime_profile
    finally:
        cleanup_error = None
        for attempt in range(3):
            try:
                await asyncio.to_thread(_cleanup_tree, destination, root)
                cleanup_error = None
                break
            except Exception as exc:  # noqa: BLE001 - Windows may release browser files slowly
                cleanup_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.1 * (attempt + 1))
        if cleanup_error is None:
            print(
                f"[profile-runtime] action=clone-cleaned job_id={job_id} "
                f"profile={source_profile.profile_code} temp={destination}"
            )
        else:
            # Cleanup failure is visible but never masks the job's actual exception/result.
            print(
                f"[profile-runtime] action=clone-cleanup-failed job_id={job_id} "
                f"profile={source_profile.profile_code} temp={destination} "
                f"error={type(cleanup_error).__name__}: {cleanup_error}"
            )
