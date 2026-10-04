"""Deterministic reconnect ownership and memory erasure-race regressions."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from test_voice import FakeLiveSession, FakePlayback, FakeProvider

from arise.adapters.sqlite import SQLiteDatabase, SQLiteMemoryRepository
from arise.core.extensions import AudioChunk, EmbeddingResult, MemoryConsentError, MemoryEntry
from arise.core.voice import (
    AudioHub,
    LiveEvent,
    LiveEventType,
    LiveSessionConfig,
    VoiceConfig,
    VoiceState,
)


def voice_fixture(provider):
    old = FakeLiveSession()
    hub = AudioHub(
        microphone=None,
        vad=None,
        wake_word_detector=None,
        provider=provider,
        playback=FakePlayback(),
        config=VoiceConfig(max_reconnect_attempts=1, reconnect_backoff_seconds=0),
    )
    hub._session = old
    hub._session_id = "conversation"
    hub._state = VoiceState.LISTENING
    return hub, old, LiveSessionConfig("conversation")


def output(identity, sequence=1):
    return LiveEvent(
        type=LiveEventType.OUTPUT_AUDIO,
        utterance_id=identity,
        text="Hello there.",
        generation_id=0,
        audio=AudioChunk(
            sequence=sequence,
            codec="pcm_s16le",
            sample_rate_hz=24000,
            channels=1,
            data=b"\x00\x00" * 24,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("metadata_first", [True, False])
async def test_replacement_output_without_input_settles_and_old_session_cannot_interfere(
    active,
    metadata_first,
):
    replacement = FakeLiveSession()
    hub, old, config = voice_fixture(FakeProvider([replacement]))
    try:
        if active:
            await hub._handle_live_event(
                old,
                LiveEvent(
                    type=LiveEventType.INPUT_TRANSCRIPT,
                    text="What time is it?",
                    utterance_id="old-turn",
                ),
            )
            assert hub._turn_in_flight
        assert await hub._reconnect(old, config, None) is replacement
        assert old.closed
        assert hub._live_utterance_id is None
        assert not hub._turn_in_flight
        assert hub._last_user_transcript is None
        if active:
            assert "old-turn" in hub._settled_utterances
        if metadata_first:
            await hub._handle_live_event(
                replacement,
                LiveEvent(type=LiveEventType.SESSION_RESUMPTION, utterance_id="replacement-turn"),
            )
        await hub._handle_live_event(replacement, output("replacement-turn"))
        assert hub._live_utterance_id == "replacement-turn"
        assert hub._turn_in_flight
        assert len(hub.playback.played) == 1
        # Even using the current identity cannot give the retired session authority.
        await hub._handle_live_event(old, output("replacement-turn", sequence=2))
        await hub._handle_live_event(
            old, LiveEvent(type=LiveEventType.TURN_COMPLETE, utterance_id="replacement-turn")
        )
        assert len(hub.playback.played) == 1
        assert hub._turn_in_flight
        await hub._handle_live_event(
            replacement,
            LiveEvent(type=LiveEventType.TURN_COMPLETE, utterance_id="replacement-turn"),
        )
        assert not hub._turn_in_flight
        assert "replacement-turn" in hub._settled_utterances
        await hub._handle_live_event(replacement, output("replacement-turn", sequence=3))
        await hub._handle_live_event(old, output("late-old-output", sequence=4))
        assert len(hub.playback.played) == 1
        assert not hub._turn_in_flight
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_reconnect_cancellation_retires_old_turn_before_connect_await():
    entered = asyncio.Event()

    async def connect(config):
        entered.set()
        await asyncio.Event().wait()

    provider = FakeProvider()
    provider.connect = connect
    hub, old, config = voice_fixture(provider)
    await hub._handle_live_event(
        old, LiveEvent(type=LiveEventType.INPUT_TRANSCRIPT, text="Hello", utterance_id="old-turn")
    )
    pending = asyncio.create_task(hub._reconnect(old, config, None))
    try:
        await entered.wait()
        assert hub._session is None and not hub._turn_in_flight
        await hub._handle_live_event(old, output("unknown-stale-id"))
        assert not hub.playback.played
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert hub._session is None and not hub._turn_in_flight
        await hub._handle_live_event(old, output("another-stale-id"))
        assert not hub.playback.played
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await hub.close()


@pytest.mark.asyncio
async def test_reconnect_does_not_publish_replacement_after_voice_close():
    entered, release = asyncio.Event(), asyncio.Event()
    replacement = FakeLiveSession()

    async def connect(config):
        entered.set()
        await release.wait()
        return replacement

    provider = FakeProvider()
    provider.connect = connect
    hub, old, config = voice_fixture(provider)
    pending = asyncio.create_task(hub._reconnect(old, config, None))
    try:
        await entered.wait()
        await hub.close()
        release.set()
        assert await pending is None
        assert replacement.closed
        assert hub._session is None and not hub._turn_in_flight
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await hub.close()


@pytest.mark.asyncio
async def test_reconnect_invalidates_transient_authority_but_preserves_tasks_and_pending_updates():
    replacement = FakeLiveSession()
    hub, old, config = voice_fixture(FakeProvider([replacement]))
    try:
        await hub._handle_live_event(
            old,
            LiveEvent(
                type=LiveEventType.INPUT_TRANSCRIPT, text="Open Chrome", utterance_id="old-turn"
            ),
        )
        hub._record_task_state("task", "running", version=1)
        pending = {
            "task_id": "task",
            "state": "running",
            "utterance_id": "request",
            "summary": "ARISE is still working on the task.",
            "verified": False,
        }
        hub._queue_runtime_update("task", pending)
        hub._runtime_reply_identity = "request"
        hub._runtime_reply_task_id = "task"
        hub._local_final_transcript = "Open Chrome"
        hub._allowed_spoken_texts.add("old authorized reply")
        hub._task_submission_used = True
        generation = hub._tts_generation_id
        await hub._reconnect(old, config, None)
        assert hub._tts_generation_id > generation
        assert hub._validated_user_text() is None
        assert hub._runtime_reply_identity is None and hub._runtime_reply_task_id is None
        assert not hub._allowed_spoken_texts
        assert not hub._task_submission_used
        assert hub._task_claim_guard  # reconnect cannot authorize unverified completion speech
        assert hub._active_task_id == "task"
        assert hub._pending_runtime_updates["task"] == pending
        hub._send_runtime_update = AsyncMock()
        await hub._handle_live_event(
            replacement, LiveEvent(type=LiveEventType.TURN_COMPLETE, utterance_id="new-turn")
        )
        hub._send_runtime_update.assert_awaited_once_with(replacement, "task", pending)
        assert not hub._turn_in_flight
        assert hub._active_task_id == "task"
    finally:
        await hub.close()


class DelayedEmbedding:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def embed(self, text, *, correlation_id):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return EmbeddingResult("fixture", (1.0, 0.0))


def proposal(text="Prefer concise replies.", principal="alice"):
    return MemoryEntry(
        principal_id=principal,
        text=text,
        consent_reference="pending",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )


async def approved(repo, entry):
    reference, _ = await repo.issue_write_consent(entry)
    return replace(entry, consent_reference=reference)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["store", "update"])
@pytest.mark.parametrize("separate_connection", [False, True])
async def test_delete_all_invalidates_inflight_persistence_and_clears_grants(
    tmp_path,
    operation,
    separate_connection,
):
    database = SQLiteDatabase(tmp_path / "memory.sqlite3")
    other_database = SQLiteDatabase(tmp_path / "memory.sqlite3") if separate_connection else None
    repo = SQLiteMemoryRepository(database)
    eraser = SQLiteMemoryRepository(other_database or database)
    delayed = DelayedEmbedding()
    pending = None
    try:
        entry = await approved(repo, proposal())
        unused = await approved(repo, proposal("Prefer large fonts."))
        if operation == "update":
            record_id = await repo.store(entry)
            entry = await approved(repo, replace(entry, text="Prefer detailed replies."))
        repo.embedding = delayed
        pending = asyncio.create_task(
            repo.store(entry)
            if operation == "store"
            else repo.update_record(
                principal_id="alice",
                record_id=record_id,
                text=entry.text,
                consent_reference=entry.consent_reference,
            )
        )
        await delayed.started.wait()
        eraser.delete_all(principal_id="alice")
        assert eraser.list_records(principal_id="alice") == []
        delayed.release.set()
        with pytest.raises(MemoryConsentError, match="deletion"):
            await pending
        assert repo.list_records(principal_id="alice") == []
        with database.locked() as connection:
            assert connection.execute("SELECT COUNT(*) FROM memory_consents").fetchone()[0] == 0
        for old_grant in (entry, unused):
            with pytest.raises(MemoryConsentError):
                await repo.store(old_grant)
        assert delayed.calls == 1  # invalidated grants cannot cause additional embedding egress
    finally:
        delayed.release.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        if other_database:
            other_database.close()
        database.close()


@pytest.mark.asyncio
async def test_post_delete_write_succeeds_while_older_write_is_still_inflight(tmp_path):
    database = SQLiteDatabase(tmp_path / "memory.sqlite3")
    delayed = DelayedEmbedding()
    old_repo = SQLiteMemoryRepository(database, embedding=delayed)
    fresh_repo = SQLiteMemoryRepository(database)
    pending = None
    try:
        old = await approved(old_repo, proposal())
        foreign = await approved(fresh_repo, proposal("Prefer blue menus.", principal="bob"))
        pending = asyncio.create_task(old_repo.store(old))
        await delayed.started.wait()
        fresh_repo.delete_all(principal_id="alice")
        # Reconstruct the repository: invalidation must belong to the DB, not one instance.
        fresh_repo = SQLiteMemoryRepository(database)
        fresh = await approved(fresh_repo, proposal("Prefer detailed replies."))
        record_id = await fresh_repo.store(fresh)
        await fresh_repo.store(foreign)  # deleting Alice must not revoke Bob's grant
        delayed.release.set()
        with pytest.raises(MemoryConsentError, match="deletion"):
            await pending
        records = fresh_repo.list_records(principal_id="alice")
        assert [record.record_id for record in records] == [record_id]
        assert records[0].text == fresh.text
        with pytest.raises(MemoryConsentError):
            await fresh_repo.store(fresh)  # legitimate post-delete grant remains one-time
        assert len(fresh_repo.list_records(principal_id="bob")) == 1
    finally:
        delayed.release.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        database.close()


@pytest.mark.asyncio
async def test_other_principal_deletion_does_not_invalidate_pending_write():
    database = SQLiteDatabase(":memory:")
    delayed = DelayedEmbedding()
    repo = SQLiteMemoryRepository(database, embedding=delayed)
    pending = None
    try:
        entry = await approved(repo, proposal())
        pending = asyncio.create_task(repo.store(entry))
        await delayed.started.wait()
        repo.delete_all(principal_id="bob")
        delayed.release.set()
        record_id = await pending
        assert repo.get_record(principal_id="alice", record_id=record_id) is not None
    finally:
        delayed.release.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        database.close()


@pytest.mark.asyncio
async def test_reconnect_invalidates_delayed_local_speech_without_cancelling_admitted_tasks():
    started, release = asyncio.Event(), asyncio.Event()

    class Synthesizer:
        async def synthesize(self, text, **kwargs):
            started.set()
            await release.wait()
            yield output("unused").audio

    replacement = FakeLiveSession()
    hub, old, config = voice_fixture(FakeProvider([replacement]))
    hub.speech_synthesizer = Synthesizer()
    hub._record_task_state("admitted-task", "running", version=1)
    speech = asyncio.create_task(hub.speak_text("Hello there."))
    try:
        await started.wait()
        await hub._reconnect(old, config, None)
        release.set()
        assert await speech == 0
        assert hub.playback.played == []
        assert hub._active_task_id == "admitted-task"
        assert hub._task_states["admitted-task"] == "running"
    finally:
        release.set()
        await asyncio.gather(speech, return_exceptions=True)
        await hub.close()


def test_epoch_migration_preserves_existing_enablement_and_survives_repository_reopen(tmp_path):
    path = tmp_path / "legacy-memory.sqlite3"
    database = SQLiteDatabase(path)
    try:
        # Model the governance table from before deletion epochs were introduced.
        with database.transaction() as connection:
            connection.execute(
                "CREATE TABLE memory_principal_settings (principal_id TEXT PRIMARY KEY, "
                "enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO memory_principal_settings VALUES (?, 0, ?)",
                ("alice", datetime.now(UTC).isoformat()),
            )
        repo = SQLiteMemoryRepository(database)
        assert not repo.is_enabled(principal_id="alice")
        repo.delete_all(principal_id="alice")
        assert not repo.is_enabled(principal_id="alice")
        repo.set_enabled(principal_id="alice", enabled=True)
        assert repo._deletion_epoch("alice") == 1
    finally:
        database.close()
    reopened = SQLiteDatabase(path)
    try:
        repo = SQLiteMemoryRepository(reopened)
        assert repo.is_enabled(principal_id="alice")
        assert repo._deletion_epoch("alice") == 1
        repo.delete_all(principal_id="alice")
        assert repo._deletion_epoch("alice") == 2
    finally:
        reopened.close()
