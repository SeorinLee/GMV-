"""Job-scoped browser-profile clone lifecycle tests."""

import asyncio
from pathlib import Path

import pytest

from gmv.config import get_profile
from gmv.profile_runtime import (
    RuntimeProfileError,
    cleanup_stale_runtime_profiles,
    cloned_profile_for_job,
)


def _source_profile(monkeypatch, tmp_path):
    profile_root = tmp_path / "persistent_profiles"
    runtime_root = tmp_path / "runtime_profiles"
    monkeypatch.setenv("GMV_PROFILE_ROOT", str(profile_root))
    monkeypatch.setenv("GMV_RUNTIME_PROFILE_ROOT", str(runtime_root))
    profile = get_profile("US_CHROME")
    source = Path(profile.storage_root)
    source.mkdir(parents=True)
    return profile, source, runtime_root


def _write(path: Path, value: str = "data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_clone_preserves_login_state_and_excludes_runtime_artifacts(monkeypatch, tmp_path):
    profile, source, runtime_root = _source_profile(monkeypatch, tmp_path)
    for relative in (
        "Local State",
        "Default/Cookies",
        "Default/Network/Cookies",
        "Default/Local Storage/leveldb/000001.ldb",
        "Default/IndexedDB/site/000001.ldb",
        "Default/Preferences",
        "Default/Secure Preferences",
        "Default/Login Data",
    ):
        _write(source / relative, relative)
    for relative in (
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
        "DevToolsActivePort",
        "Default/Local Storage/leveldb/LOCK",
        "Default/Cache/junk",
        "Default/Code Cache/junk",
        "Default/GPUCache/junk",
        "Default/Service Worker/CacheStorage/junk",
    ):
        _write(source / relative)

    async def go():
        async with cloned_profile_for_job(profile, "job-101") as runtime_profile:
            clone = Path(runtime_profile.storage_root)
            assert clone != source
            assert clone.is_relative_to(runtime_root)
            assert runtime_profile.runtime_job_id == "job-101"
            assert (clone / "Default/Cookies").exists()
            assert (clone / "Default/Network/Cookies").exists()
            assert (clone / "Default/Local Storage/leveldb/000001.ldb").exists()
            assert (clone / "Default/IndexedDB/site/000001.ldb").exists()
            assert not (clone / "SingletonLock").exists()
            assert not (clone / "Default/Local Storage/leveldb/LOCK").exists()
            assert not (clone / "Default/Cache").exists()
            assert not (clone / "Default/Service Worker/CacheStorage").exists()
            return clone

    clone = asyncio.run(go())
    assert not clone.exists()
    assert (source / "SingletonLock").exists()  # source is read-only and remains untouched


def test_two_jobs_from_same_source_get_distinct_live_clones(monkeypatch, tmp_path):
    profile, source, _ = _source_profile(monkeypatch, tmp_path)
    _write(source / "Default/Cookies", "logged-in")
    entered = asyncio.Event()
    release = asyncio.Event()
    paths = []

    async def use_clone(job_id):
        async with cloned_profile_for_job(profile, job_id) as runtime_profile:
            paths.append(Path(runtime_profile.storage_root))
            if len(paths) == 2:
                entered.set()
            await release.wait()

    async def go():
        tasks = [asyncio.create_task(use_clone(job_id)) for job_id in ("job-1", "job-2")]
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert paths[0] != paths[1]
        assert all(path.exists() for path in paths)
        assert all((path / "Default/Cookies").read_text() == "logged-in" for path in paths)
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(go())
    assert all(not path.exists() for path in paths)


def test_source_lock_guards_snapshot_only(monkeypatch, tmp_path):
    profile, _, _ = _source_profile(monkeypatch, tmp_path)

    async def go():
        source_lock = asyncio.Lock()
        await source_lock.acquire()
        entered = asyncio.Event()

        async def job():
            async with cloned_profile_for_job(
                profile, "locked-snapshot", source_lock=source_lock
            ) as runtime_profile:
                entered.set()
                # The source can be used for the next mutation/snapshot while this job runs.
                assert not source_lock.locked()
                assert Path(runtime_profile.storage_root).exists()

        task = asyncio.create_task(job())
        await asyncio.sleep(0.02)
        assert not entered.is_set()
        source_lock.release()
        await asyncio.wait_for(task, timeout=2)
        assert entered.is_set()

    asyncio.run(go())


def test_failure_and_cancellation_cleanup_clones(monkeypatch, tmp_path):
    profile, _, _ = _source_profile(monkeypatch, tmp_path)
    failure_path = None

    async def fail():
        nonlocal failure_path
        with pytest.raises(ValueError, match="boom"):
            async with cloned_profile_for_job(profile, "failure") as runtime_profile:
                failure_path = Path(runtime_profile.storage_root)
                raise ValueError("boom")

    asyncio.run(fail())
    assert failure_path is not None and not failure_path.exists()

    cancel_path = None
    entered = asyncio.Event()

    async def wait_forever():
        nonlocal cancel_path
        async with cloned_profile_for_job(profile, "cancel") as runtime_profile:
            cancel_path = Path(runtime_profile.storage_root)
            entered.set()
            await asyncio.Event().wait()

    async def cancel():
        task = asyncio.create_task(wait_forever())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert cancel_path is not None and not cancel_path.exists()


def test_retry_context_and_stale_cleanup_use_new_directories(monkeypatch, tmp_path):
    profile, _, runtime_root = _source_profile(monkeypatch, tmp_path)

    async def one_clone():
        async with cloned_profile_for_job(profile, "same-job") as runtime_profile:
            return Path(runtime_profile.storage_root)

    first = asyncio.run(one_clone())
    second = asyncio.run(one_clone())
    assert first != second
    assert not first.exists() and not second.exists()

    stale = runtime_root / profile.profile_code / "stale-clone"
    _write(stale / "Default/Cookies")
    cleanup_stale_runtime_profiles(runtime_root)
    assert not stale.exists()


def test_runtime_root_cannot_contain_source_profile(monkeypatch, tmp_path):
    profile_root = tmp_path / "profiles"
    monkeypatch.setenv("GMV_PROFILE_ROOT", str(profile_root))
    monkeypatch.setenv("GMV_RUNTIME_PROFILE_ROOT", str(profile_root))
    profile = get_profile("US_CHROME")

    async def go():
        with pytest.raises(RuntimeProfileError):
            async with cloned_profile_for_job(profile, "unsafe"):
                pass

    asyncio.run(go())
