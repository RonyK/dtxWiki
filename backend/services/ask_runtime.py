"""One application process schedules Ask turns and streams their live output.

Questions in a session are answered one at a time, in order. The process that
accepted a question answers it. Access is checked when a request is made and
when the database is read for it; a turn then runs with the access its request
had, and an open stream is not re-checked.

Recovery is deliberately simple (MVP). The question is always kept; these
losses are accepted rather than handled:

- The queue and every answer in progress live in memory. A restart does not
  resume them: when serving starts, unanswered questions end as
  ``failed/server_restarted`` (``prepare_serving``), and instructions still
  waiting end as ``unknown``. A partial answer is not kept.
- If storing a turn's ending fails, the turn stays ``pending`` until the
  session's next question starts (``_start`` ends it) or the server restarts.
- One serving process per database; a second one is not coordinated with.
- A retry after a lost response reuses its request id; if the original was
  answered meanwhile, a retry sent as a new question asks it again.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.llm.binding import ModelBinding
from backend.llm.streaming import SteerInput, SteerUpdate
from backend.models.ask import (
    ASK_TURN_FAILED,
    ASK_TURN_PENDING,
    ASK_TURN_STOPPED,
    AskTurn,
)
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import get_in_workspace, select_in_workspace
from backend.schemas.ask import AskQuestion, AskSteerRequest, AskTurnDetail
from backend.schemas.input_policy import validate_request_inputs
from backend.services.ask_connection_service import AskConnectionService
from backend.services.ask_service import (
    ASK_TURN_INTERNAL_ERROR,
    LLM_PROVIDER_UNAVAILABLE_WARNING,
    AskEvidence,
    AskGenerationError,
    AskService,
    ComposedTurn,
    PreparedTurn,
    _validate_user_message,
    ask_evidence_visible,
    ask_session_for_turn_insert_statement,
)
from backend.services.db_write_intent import acquire_workspace_write_intent
from backend.services.llm_connection_service import LLMConnectionError
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError

logger = logging.getLogger(__name__)

SessionFactory = sessionmaker[Session]
_T = TypeVar("_T")
_LIVE = frozenset({"pending", "queued"})
# Steering outcomes stored on the turn; "sent" and "accepted" are transient.
_STORED_STEER_STATES = frozenset({"applied", "failed", "unknown"})
# A quiet stream still sends something this often, so proxies keep it open.
_IDLE_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class AskTurnState:
    """The turn as the subscriber may read it."""

    turn: AskTurnDetail


@dataclass(frozen=True, slots=True)
class AskTurnPreview:
    """Provisional answer text: appended, or replacing what was shown."""

    text: str
    reset: bool


@dataclass(frozen=True, slots=True)
class AskTurnIdle:
    """Nothing changed for a while."""


AskTurnEvent = AskTurnState | AskTurnPreview | AskTurnIdle


@dataclass
class _Job:
    workspace_id: UUID
    turn_id: UUID
    factory: SessionFactory
    actor: ActorContext
    request: AskQuestion
    # A replacement starts in the slot its predecessor held.
    reserved: bool = False
    task: asyncio.Task[None] | None = None
    error: Exception | None = None
    evidence: AskEvidence | None = None
    # Provisional text of the current model response. The epoch changes when
    # a new response replaces it (an applied steering instruction).
    preview: str = ""
    preview_epoch: int = 0
    state_version: int = 0
    # Steering inputs not yet sent to the model, oldest first.
    steering: deque[SteerInput] = field(default_factory=deque)
    steering_added: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    def state_changed(self) -> None:
        self.state_version += 1
        self.notify()

    def owned_by(self, actor: ActorContext) -> bool:
        return _person(self.actor) == _person(actor)


class AskRuntime:
    def __init__(
        self,
        service: AskService,
        connections: AskConnectionService,
        control_factory: Callable[[], SessionFactory],
    ) -> None:
        self.service = service
        self.connections = connections
        self.control_factory = control_factory
        self._write = asyncio.Lock()
        self._jobs: dict[UUID, _Job] = {}
        self._queues: dict[UUID, deque[_Job]] = {}
        self._workers: set[asyncio.Task[None]] = set()
        self._accepting: set[asyncio.Task[Any]] = set()

    # Requests -------------------------------------------------------------

    async def submit(
        self,
        factory: SessionFactory,
        workspace_id: UUID,
        session_id: UUID,
        request: AskQuestion,
        actor: ActorContext,
        *,
        replace_turn_id: UUID | None = None,
    ) -> tuple[AskTurnDetail, _Job | None]:
        """Store the question and queue it; the job is None for a resubmission."""

        return await self._owned(
            self._accept(
                factory,
                workspace_id,
                session_id,
                request,
                actor,
                replace_turn_id,
            )
        )

    async def answer(
        self,
        factory: SessionFactory,
        workspace_id: UUID,
        session_id: UUID,
        request: AskQuestion,
        actor: ActorContext,
    ) -> AskTurnDetail:
        """Submit and wait for the answer (the JSON endpoint)."""

        detail, job = await self.submit(
            factory, workspace_id, session_id, request, actor
        )
        if job is None:
            return detail
        await job.done.wait()
        if job.error is not None:
            raise job.error
        return await run_in_threadpool(
            self.get_turn, factory, workspace_id, detail.id, actor
        )

    async def stop(
        self,
        factory: SessionFactory,
        workspace_id: UUID,
        turn_id: UUID,
        actor: ActorContext,
    ) -> AskTurnDetail:
        """Stop the asker's queued or running turn."""

        return await self._owned(self._stop(factory, workspace_id, turn_id, actor))

    async def steer(
        self,
        factory: SessionFactory,
        workspace_id: UUID,
        turn_id: UUID,
        request: AskSteerRequest,
        actor: ActorContext,
    ) -> AskTurnDetail:
        """Add an instruction to the asker's running turn."""

        return await self._owned(
            self._steer(factory, workspace_id, turn_id, request, actor)
        )

    def get_turn(
        self,
        factory: SessionFactory,
        workspace_id: UUID,
        turn_id: UUID,
        actor: ActorContext,
    ) -> AskTurnDetail:
        with factory() as session:
            return self.service.get_turn(session, workspace_id, turn_id, actor)

    async def events(
        self,
        factory: SessionFactory,
        workspace_id: UUID,
        turn_id: UUID,
        actor: ActorContext,
    ) -> AsyncIterator[AskTurnEvent]:
        """Follow one turn for a subscriber authorized when the stream opened.

        The turn is read again only when its state changes. Provisional text is
        sent from memory once the subscriber is shown to be able to read the
        evidence it is written from.
        """

        # Taken before the first read, so a change after it is not missed.
        job = self._jobs.get(turn_id)
        seen_version = job.state_version if job is not None else 0
        detail = await run_in_threadpool(
            self.get_turn, factory, workspace_id, turn_id, actor
        )
        yield AskTurnState(detail)
        if job is None or detail.status not in _LIVE:
            return
        can_read: bool | None = None
        sent_epoch: int | None = None
        sent_length = 0
        while True:
            changed = job.changed
            finished = job.done.is_set()
            if finished or job.state_version != seen_version:
                seen_version = job.state_version
                detail = await run_in_threadpool(
                    self.get_turn, factory, workspace_id, turn_id, actor
                )
                yield AskTurnState(detail)
                if finished or detail.status not in _LIVE:
                    return
            if job.preview:
                if can_read is None:
                    can_read = await run_in_threadpool(
                        self._visible, factory, workspace_id, job.evidence, actor
                    )
                if can_read and sent_epoch != job.preview_epoch:
                    sent_epoch, sent_length = job.preview_epoch, len(job.preview)
                    yield AskTurnPreview(job.preview, reset=True)
                elif can_read and len(job.preview) > sent_length:
                    piece = job.preview[sent_length:]
                    sent_length = len(job.preview)
                    yield AskTurnPreview(piece, reset=False)
            elif sent_epoch is not None and sent_epoch != job.preview_epoch:
                # A new response started and has no text yet.
                sent_epoch, sent_length = job.preview_epoch, 0
                yield AskTurnPreview("", reset=True)
            try:
                await asyncio.wait_for(changed.wait(), timeout=_IDLE_SECONDS)
            except TimeoutError:
                yield AskTurnIdle()

    async def close(self) -> None:
        # Let accepted requests finish registering before stopping workers.
        await asyncio.gather(*tuple(self._accepting), return_exceptions=True)
        workers = tuple(self._workers)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def _owned(self, work: Coroutine[Any, Any, _T]) -> _T:
        """Run a write the request only waits for.

        A request cancelled while its question is being stored must not leave
        a stored question without its queued job, so the runtime owns the work.
        """

        task = asyncio.create_task(work)
        self._accepting.add(task)
        task.add_done_callback(self._settled)
        return await asyncio.shield(task)

    def _settled(self, task: asyncio.Task[Any]) -> None:
        self._accepting.discard(task)
        # Its request may be gone; the error was already returned or is moot.
        if not task.cancelled():
            task.exception()

    # Accepting and controlling turns ----------------------------------------

    async def _accept(
        self,
        factory: SessionFactory,
        workspace_id: UUID,
        session_id: UUID,
        request: AskQuestion,
        actor: ActorContext,
        replace_turn_id: UUID | None,
    ) -> tuple[AskTurnDetail, _Job | None]:
        async with self._write:
            replaced = (
                self._jobs.get(replace_turn_id) if replace_turn_id is not None else None
            )
            detail, created = await run_in_threadpool(
                self._store_question,
                factory,
                workspace_id,
                session_id,
                request,
                actor,
                replace_turn_id,
                replaced,
            )
            if not created:
                return detail, self._jobs.get(detail.id)
            job = _Job(
                workspace_id,
                detail.id,
                factory,
                actor,
                request,
                reserved=replaced is not None,
            )
            self._jobs[job.turn_id] = job
            queue = self._session_queue(session_id)
            if replaced is None:
                queue.append(job)
            else:
                self._cancel(replaced)
                queue.appendleft(job)
            return detail, job

    def _store_question(
        self,
        factory: SessionFactory,
        workspace_id: UUID,
        session_id: UUID,
        request: AskQuestion,
        actor: ActorContext,
        replace_turn_id: UUID | None,
        replaced: _Job | None,
    ) -> tuple[AskTurnDetail, bool]:
        """Store a new question; a resubmission returns the stored one first."""

        with factory.begin() as session:
            acquire_workspace_write_intent(session)
            ask_session = session.scalar(
                ask_session_for_turn_insert_statement(session_id, workspace_id)
            )
            if ask_session is None:
                raise ReadNotFoundError("ask session not found")
            if request.request_id is not None:
                existing = session.scalar(
                    select_in_workspace(
                        AskTurn,
                        workspace_id=workspace_id,
                        criteria=(
                            AskTurn.ask_session_id == session_id,
                            AskTurn.request_id == request.request_id,
                        ),
                    ).with_only_columns(AskTurn.id)
                )
                if existing is not None:
                    return (
                        self.service.get_turn(session, workspace_id, existing, actor),
                        False,
                    )
            earlier_text = None
            if replace_turn_id is not None:
                replaced = _controllable(replaced, workspace_id, actor)
                target = self._turn(session, workspace_id, replaced.turn_id)
                if target.ask_session_id != session_id or not _running(target):
                    raise _turn_changed()
                # The replacement continues the stopped question.
                earlier_text = "\n\n".join(
                    [target.user_message or "", *_applied_steering(target)]
                )
                self._mark_stopped(target)
                session.flush()
            turn_id = self.service.submit_turn(
                session,
                workspace_id=workspace_id,
                ask_session_id=session_id,
                request=request,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                previous_user_message=earlier_text,
            )
            if replace_turn_id is not None:
                # Take the stopped turn's slot in the same transaction, before
                # the worker picks the next queued question.
                self._turn(session, workspace_id, turn_id).started_at = datetime.now(
                    UTC
                )
                session.flush()
            return self.service.get_turn(session, workspace_id, turn_id, actor), True

    async def _stop(
        self,
        factory: SessionFactory,
        workspace_id: UUID,
        turn_id: UUID,
        actor: ActorContext,
    ) -> AskTurnDetail:
        async with self._write:
            job = _controllable(self._jobs.get(turn_id), workspace_id, actor)
            detail = await run_in_threadpool(
                self._store_stop, factory, workspace_id, turn_id, actor
            )
            self._cancel(job)
            return detail

    def _store_stop(
        self,
        factory: SessionFactory,
        workspace_id: UUID,
        turn_id: UUID,
        actor: ActorContext,
    ) -> AskTurnDetail:
        with factory.begin() as session:
            turn = self._locked_turn(session, workspace_id, turn_id)
            if turn.status != ASK_TURN_PENDING:
                raise _turn_changed()
            self._mark_stopped(turn)
            session.flush()
            return self.service.get_turn(session, workspace_id, turn_id, actor)

    async def _steer(
        self,
        factory: SessionFactory,
        workspace_id: UUID,
        turn_id: UUID,
        request: AskSteerRequest,
        actor: ActorContext,
    ) -> AskTurnDetail:
        async with self._write:
            job = _controllable(self._jobs.get(turn_id), workspace_id, actor)
            detail, created = await run_in_threadpool(
                self._store_steer, factory, workspace_id, turn_id, request, actor
            )
            if created:
                job.steering.append(
                    SteerInput(str(request.request_id), request.user_message)
                )
                job.steering_added.set()
                job.state_changed()
            return detail

    def _store_steer(
        self,
        factory: SessionFactory,
        workspace_id: UUID,
        turn_id: UUID,
        request: AskSteerRequest,
        actor: ActorContext,
    ) -> tuple[AskTurnDetail, bool]:
        validate_request_inputs(request)
        request.user_message = _validate_user_message(request.user_message)
        with factory.begin() as session:
            turn = self._locked_turn(session, workspace_id, turn_id)
            request_id = str(request.request_id)
            if any(message["id"] == request_id for message in turn.steering_messages):
                return self.service.get_turn(
                    session, workspace_id, turn_id, actor
                ), False
            if not _running(turn) or not turn.steer_supported:
                raise _turn_changed()
            turn.steering_messages = [
                *turn.steering_messages,
                {
                    "id": request_id,
                    "user_message": request.user_message,
                    "status": "waiting",
                },
            ]
            session.flush()
            return self.service.get_turn(session, workspace_id, turn_id, actor), True

    def _cancel(self, job: _Job) -> None:
        if job.task is not None:
            job.task.cancel()
        else:
            # A stopped job that never started will not run; those waiting for
            # it get the stopped turn now, not when the queue reaches it.
            job.done.set()
        job.state_changed()

    def _session_queue(self, session_id: UUID) -> deque[_Job]:
        queue = self._queues.get(session_id)
        if queue is None:
            queue = self._queues[session_id] = deque()
            worker = asyncio.create_task(self._drain(session_id, queue))
            self._workers.add(worker)
            worker.add_done_callback(self._workers.discard)
        return queue

    # Answering ----------------------------------------------------------------

    async def _drain(self, session_id: UUID, queue: deque[_Job]) -> None:
        try:
            while True:
                async with self._write:
                    if not queue:
                        return
                    job = queue.popleft()
                    job.task = asyncio.create_task(self._run(job))
                try:
                    await job.task
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                finally:
                    job.done.set()
                    job.notify()
                    self._jobs.pop(job.turn_id, None)
        finally:
            self._queues.pop(session_id, None)

    async def _run(self, job: _Job) -> None:
        """Answer one turn with the access its request had."""

        try:
            if not await run_in_threadpool(self._start, job):
                return
            job.state_changed()
            binding = await self._binding(job)
            prepared = await run_in_threadpool(self._prepare, job, binding)
            asks_model = binding is not None and bool(prepared.retrieved.pack)
            if asks_model and binding.steer:
                await run_in_threadpool(self._enable_steering, job)
                job.state_changed()
            job.evidence = prepared.evidence
            if asks_model:
                composed = await self.service.stream_turn(
                    prepared,
                    binding,
                    on_delta=lambda text: self._on_delta(job, text),
                    next_steer=lambda: self._next_steer(job),
                    on_steer=lambda update: self._on_steer(job, update),
                )
            else:
                composed = self.service.compose_evidence(prepared)
            await run_in_threadpool(self._finish, job, prepared, composed)
        except AskGenerationError as exc:
            await run_in_threadpool(self._fail, job, exc.code)
        except LLMConnectionError:
            await run_in_threadpool(self._fail, job, LLM_PROVIDER_UNAVAILABLE_WARNING)
        except Exception as exc:
            job.error = exc
            logger.exception("Ask turn %s failed", job.turn_id)
            await run_in_threadpool(self._fail, job, ASK_TURN_INTERNAL_ERROR)

    async def _binding(self, job: _Job) -> ModelBinding | None:
        """The model a question asks: none named is the default model, null
        is search only, a selection is that connection's model."""

        if _names_no_model(job.request):
            if self.connections.default_unavailable_reason:
                # A configured default model that cannot be built is a model
                # failure like any other; no default model at all is not.
                raise AskGenerationError(LLM_PROVIDER_UNAVAILABLE_WARNING)
            return self.connections.default
        if job.request.selection is None:
            return None
        return await run_in_threadpool(
            self.connections.resolve,
            self.control_factory(),
            job.factory,
            job.workspace_id,
            job.actor,
            job.request.selection,
        )

    def _start(self, job: _Job) -> bool:
        with job.factory.begin() as session:
            turn = self._locked_turn(session, job.workspace_id, job.turn_id)
            if turn.status != ASK_TURN_PENDING:
                return False
            if turn.started_at is not None:
                return job.reserved
            # The worker finished its previous job. A started row whose ending
            # could not be stored still holds the slot: end it now.
            stale = session.scalar(
                select_in_workspace(
                    AskTurn,
                    workspace_id=job.workspace_id,
                    criteria=(
                        AskTurn.ask_session_id == turn.ask_session_id,
                        AskTurn.status == ASK_TURN_PENDING,
                        AskTurn.started_at.is_not(None),
                    ),
                )
            )
            if stale is not None:
                stale.status = ASK_TURN_FAILED
                stale.error_code = ASK_TURN_INTERNAL_ERROR
                _settle_steering(stale, "unknown")
                session.flush()
            turn.started_at = datetime.now(UTC)
        return True

    def _prepare(self, job: _Job, binding: ModelBinding | None) -> PreparedTurn:
        with job.factory() as session:
            return self.service.prepare_turn(
                session,
                job.workspace_id,
                job.turn_id,
                job.actor,
                binding=binding,
                # Search only uses keyword search, as "search again" does.
                keyword_only=not _names_no_model(job.request)
                and job.request.selection is None,
            )

    def _enable_steering(self, job: _Job) -> None:
        with job.factory.begin() as session:
            turn = self._locked_turn(session, job.workspace_id, job.turn_id)
            if turn.status == ASK_TURN_PENDING:
                turn.steer_supported = True

    def _finish(
        self,
        job: _Job,
        prepared: PreparedTurn,
        composed: ComposedTurn,
    ) -> None:
        with job.factory.begin() as session:
            # Instructions the model never received did not shape the answer.
            _settle_steering(
                self._locked_turn(session, job.workspace_id, job.turn_id), "failed"
            )
            session.flush()
            self.service.finish_turn(session, job.workspace_id, prepared, composed)

    def _fail(self, job: _Job, code: str) -> None:
        try:
            with job.factory.begin() as session:
                _settle_steering(
                    self._locked_turn(session, job.workspace_id, job.turn_id),
                    "unknown",
                )
                session.flush()
                self.service.fail_turn(session, job.workspace_id, job.turn_id, code)
        except Exception:
            logger.exception("Could not record failure for Ask turn %s", job.turn_id)

    # Model callbacks --------------------------------------------------------

    @staticmethod
    async def _on_delta(job: _Job, text: str) -> None:
        job.preview += text
        job.notify()

    @staticmethod
    async def _next_steer(job: _Job) -> SteerInput:
        """The oldest unsent instruction; it stays queued until it is sent."""

        while not job.steering:
            job.steering_added.clear()
            await job.steering_added.wait()
        return job.steering[0]

    async def _on_steer(self, job: _Job, update: SteerUpdate) -> None:
        if update.status == "sent" and job.steering and job.steering[0].id == update.id:
            job.steering.popleft()
        if update.status not in _STORED_STEER_STATES:
            return
        await run_in_threadpool(self._store_steer_status, job, update)
        if update.status == "applied":
            # The model continues in a new response; its text replaces the
            # provisional answer shown so far.
            job.preview = ""
            job.preview_epoch += 1
        job.state_changed()

    def _store_steer_status(self, job: _Job, update: SteerUpdate) -> None:
        with job.factory.begin() as session:
            turn = self._locked_turn(session, job.workspace_id, job.turn_id)
            if turn.status == ASK_TURN_PENDING:
                turn.steering_messages = [
                    {**message, "status": update.status}
                    if message["id"] == update.id
                    else message
                    for message in turn.steering_messages
                ]

    # Shared helpers -----------------------------------------------------------

    @staticmethod
    def _visible(
        factory: SessionFactory,
        workspace_id: UUID,
        evidence: AskEvidence | None,
        actor: ActorContext,
    ) -> bool:
        if evidence is None:
            return False
        with factory() as session:
            return ask_evidence_visible(
                session,
                context_refs=evidence.context,
                artifact_ids=evidence.library,
                actor=actor,
                workspace_id=workspace_id,
            )

    @staticmethod
    def _turn(session: Session, workspace_id: UUID, turn_id: UUID) -> AskTurn:
        turn = get_in_workspace(
            session, AskTurn, workspace_id=workspace_id, resource_id=turn_id
        )
        if turn is None:
            raise ReadNotFoundError("ask turn not found")
        return turn

    def _locked_turn(
        self, session: Session, workspace_id: UUID, turn_id: UUID
    ) -> AskTurn:
        acquire_workspace_write_intent(session)
        turn = self._turn(session, workspace_id, turn_id)
        session.scalar(
            ask_session_for_turn_insert_statement(turn.ask_session_id, workspace_id)
        )
        session.refresh(turn)
        return turn

    @staticmethod
    def _mark_stopped(turn: AskTurn) -> None:
        if turn.status == ASK_TURN_PENDING:
            turn.status = ASK_TURN_STOPPED
            _settle_steering(turn, "unknown")


def _names_no_model(request: AskQuestion) -> bool:
    return "selection" not in request.model_fields_set


def _person(actor: ActorContext) -> tuple[str, str]:
    """One person across their browser session and tokens."""

    if actor.user_id is not None:
        return ("user", str(actor.user_id))
    return (actor.actor_kind, actor.actor_id)


def _controllable(job: _Job | None, workspace_id: UUID, actor: ActorContext) -> _Job:
    """The live job of a turn ``actor`` asked. Whether the turn is in a state
    the change applies to is checked on the row, under its lock."""

    if job is None or job.workspace_id != workspace_id:
        raise _turn_changed()
    if not job.owned_by(actor):
        raise RejectedByPolicyError(
            "only the person who asked can change this Ask turn",
            error_code="ask_turn_not_yours",
        )
    return job


def _running(turn: AskTurn) -> bool:
    return turn.status == ASK_TURN_PENDING and turn.started_at is not None


def _applied_steering(turn: AskTurn) -> list[str]:
    return [
        message["user_message"]
        for message in turn.steering_messages
        if message["status"] == "applied"
    ]


def _settle_steering(turn: AskTurn, status: str) -> None:
    """Give every instruction still waiting its final status."""

    if any(message["status"] == "waiting" for message in turn.steering_messages):
        turn.steering_messages = [
            {**message, "status": status} if message["status"] == "waiting" else message
            for message in turn.steering_messages
        ]


def _turn_changed() -> RejectedByPolicyError:
    return RejectedByPolicyError(
        "the active Ask turn has changed", error_code="ask_session_changed"
    )
