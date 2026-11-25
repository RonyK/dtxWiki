"""One application process schedules Ask turns and streams their live output."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.llm.streaming import SteerInput, SteerUpdate
from backend.models.ask import (
    ASK_TURN_FAILED,
    ASK_TURN_PENDING,
    ASK_TURN_STOPPED,
    AskTurn,
)
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import get_in_workspace, select_in_workspace
from backend.schemas.ask import AskSteerRequest, AskTurnCreate, AskTurnDetail
from backend.schemas.input_policy import validate_request_inputs
from backend.services.ask_connection_service import AskConnectionService
from backend.services.ask_service import (
    ASK_TURN_ACCESS_REVOKED,
    ASK_TURN_EVIDENCE_UNAVAILABLE,
    ASK_TURN_INTERNAL_ERROR,
    LLM_PROVIDER_UNAVAILABLE_WARNING,
    AskAccessRevokedError,
    AskEvidence,
    AskGenerationError,
    AskService,
    _validate_user_message,
    ask_evidence_visible,
    ask_session_for_turn_insert_statement,
)
from backend.services.db_write_intent import acquire_workspace_write_intent
from backend.services.llm_connection_service import LLMConnectionError
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError

logger = logging.getLogger(__name__)
_LIVE = {"pending", "queued"}
_STEER_STATES = {"applied": "applied", "failed": "failed", "unknown": "unknown"}


@dataclass
class _Job:
    workspace_id: UUID
    turn_id: UUID
    factory: sessionmaker[Session]
    authorize: Callable[[], ActorContext]
    actor: ActorContext
    request: AskTurnCreate
    reserved: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    steering: asyncio.Queue[SteerInput] = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    error: Exception | None = None
    evidence: AskEvidence | None = None
    preview: str = ""

    def notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()


class AskRuntime:
    def __init__(
        self,
        service: AskService,
        connections: AskConnectionService,
        control_factory: Callable[[], sessionmaker[Session]],
    ) -> None:
        self.service = service
        self.connections = connections
        self.control_factory = control_factory
        self._write = asyncio.Lock()
        self._jobs: dict[UUID, _Job] = {}
        self._queues: dict[UUID, deque[_Job]] = {}
        self._workers: set[asyncio.Task] = set()

    async def submit(
        self,
        factory,
        workspace_id,
        session_id,
        request,
        actor,
        authorize,
        *,
        replace_turn_id=None,
    ) -> tuple[AskTurnDetail, _Job | None]:
        # Pair acceptance and registration so a concurrent stop sees both.
        async with self._write:
            detail, created = await run_in_threadpool(
                self._submit,
                factory,
                workspace_id,
                session_id,
                request,
                actor,
                replace_turn_id,
            )
            if not created:
                return detail, self._jobs.get(detail.id)
            job = _Job(
                workspace_id,
                detail.id,
                factory,
                authorize,
                actor,
                request,
                reserved=replace_turn_id is not None,
            )
            self._jobs[detail.id] = job
            if replace_turn_id is not None:
                self._cancel(replace_turn_id)
            queue = self._queues.get(session_id)
            if queue is None:
                queue = self._queues[session_id] = deque()
                worker = asyncio.create_task(self._drain(session_id, queue))
                self._workers.add(worker)
                worker.add_done_callback(self._workers.discard)
            if replace_turn_id is None:
                queue.append(job)
            else:
                queue.appendleft(job)
            return detail, job

    def _submit(
        self, factory, workspace_id, session_id, request, actor, replace_turn_id
    ):
        with factory.begin() as session:
            acquire_workspace_write_intent(session)
            ask_session = session.scalar(
                ask_session_for_turn_insert_statement(session_id, workspace_id)
            )
            if ask_session is None or ask_session.workspace_id != workspace_id:
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
                    return self.service.get_turn(
                        session, workspace_id, existing, actor
                    ), False
            previous_user_message = None
            if replace_turn_id is not None:
                target = self._turn(session, workspace_id, replace_turn_id)
                if (
                    target.ask_session_id != session_id
                    or target.status != ASK_TURN_PENDING
                    or target.started_at is None
                ):
                    raise self._turn_changed()
                existing_text = [target.user_message or ""]
                existing_text.extend(
                    message["user_message"]
                    for message in target.steering_messages
                    if message["status"] == "applied"
                )
                previous_user_message = "\n\n".join(existing_text)
                self._mark_stopped(target)
                session.flush()
            turn_id = self.service.submit_turn(
                session,
                workspace_id=workspace_id,
                ask_session_id=session_id,
                request=request,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                queued=True,
                previous_user_message=previous_user_message,
            )
            if replace_turn_id is not None:
                # Reserve the old turn's slot in the same transaction that
                # stops it, before the browser or worker chooses the next turn.
                self._turn(session, workspace_id, turn_id).started_at = datetime.now(
                    UTC
                )
                session.flush()
            return self.service.get_turn(session, workspace_id, turn_id, actor), True

    async def answer(
        self, factory, workspace_id, session_id, request, actor, authorize
    ):
        detail, job = await self.submit(
            factory, workspace_id, session_id, request, actor, authorize
        )
        if job is not None:
            await job.done.wait()
            if job.error is not None:
                raise job.error
            return await run_in_threadpool(
                self.get_turn, factory, workspace_id, detail.id, actor
            )
        return detail

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
                    if asyncio.current_task().cancelling():
                        raise
                finally:
                    job.done.set()
                    job.notify()
                    self._jobs.pop(job.turn_id, None)
        finally:
            self._queues.pop(session_id, None)

    async def _run(self, job: _Job) -> None:
        try:
            if not await run_in_threadpool(self._start, job):
                return
            job.notify()
            actor = await run_in_threadpool(job.authorize)
            binding = None
            if job.request.selection is not None:
                binding = await run_in_threadpool(
                    self.connections.resolve,
                    self.control_factory(),
                    job.factory,
                    job.workspace_id,
                    actor,
                    job.request.selection,
                )
            explicit = "selection" in job.request.model_fields_set
            prepared = await run_in_threadpool(
                self._prepare, job, actor, binding, explicit
            )
            actor = await run_in_threadpool(job.authorize)
            if not await run_in_threadpool(
                self._visible, job.factory, job.workspace_id, prepared.evidence, actor
            ):
                await run_in_threadpool(self._fail, job, ASK_TURN_EVIDENCE_UNAVAILABLE)
                return
            job.evidence = prepared.evidence
            if binding is not None and prepared.retrieved.pack:
                if binding.steer:
                    await run_in_threadpool(self._enable_steering, job)
                    job.notify()
                composed = await self.service.stream_turn(
                    prepared,
                    binding,
                    on_delta=lambda text: self._delta(job, text),
                    next_steer=lambda: self._next_steer(job),
                    on_steer=lambda update: self._steer_update(job, update),
                )
            elif explicit:
                composed = self.service.compose_evidence(prepared)
            else:
                composed = await run_in_threadpool(self.service.compose_turn, prepared)
            actor = await run_in_threadpool(job.authorize)
            await run_in_threadpool(self._finish, job, prepared, composed, actor)
        except AskAccessRevokedError as exc:
            job.error = exc
            await run_in_threadpool(self._fail, job, ASK_TURN_ACCESS_REVOKED)
        except (AskGenerationError, LLMConnectionError) as exc:
            code = (
                exc.code
                if isinstance(exc, AskGenerationError)
                else LLM_PROVIDER_UNAVAILABLE_WARNING
            )
            await run_in_threadpool(self._fail, job, code)
        except Exception as exc:
            job.error = exc
            logger.exception("Ask turn %s failed", job.turn_id)
            await run_in_threadpool(self._fail, job, ASK_TURN_INTERNAL_ERROR)

    def _start(self, job: _Job) -> bool:
        with job.factory.begin() as session:
            turn = self._locked_turn(session, job.workspace_id, job.turn_id)
            if turn.status != ASK_TURN_PENDING:
                return False
            if turn.started_at is not None:
                return job.reserved
            # The session worker has finished its previous job. A pending row
            # left by a failed terminal write must release the slot on recovery.
            interrupted = session.scalar(
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
            if interrupted is not None:
                interrupted.status = ASK_TURN_FAILED
                interrupted.error_code = ASK_TURN_INTERNAL_ERROR
                self._settle_steering(interrupted, "unknown")
                session.flush()
            turn.started_at = datetime.now(UTC)
        return True

    def _prepare(self, job, actor, binding, explicit):
        with job.factory() as session:
            return self.service.prepare_turn(
                session,
                job.workspace_id,
                job.turn_id,
                actor,
                binding=binding,
                keyword_only=explicit and binding is None,
                use_legacy_provider=not explicit,
            )

    def _finish(self, job, prepared, composed, actor) -> None:
        with job.factory.begin() as session:
            turn = self._locked_turn(session, job.workspace_id, job.turn_id)
            self._settle_steering(turn, "failed")
            session.flush()
            self.service.finish_turn(
                session, job.workspace_id, prepared, composed, actor=actor
            )

    def _fail(self, job, code) -> None:
        try:
            with job.factory.begin() as session:
                turn = self._locked_turn(session, job.workspace_id, job.turn_id)
                self._settle_steering(turn, "unknown")
                session.flush()
                self.service.fail_turn(session, job.workspace_id, job.turn_id, code)
        except Exception:
            logger.exception("Could not record failure for Ask turn %s", job.turn_id)

    async def stop(self, factory, workspace_id, turn_id, actor) -> AskTurnDetail:
        async with self._write:
            detail = await run_in_threadpool(
                self._stop, factory, workspace_id, turn_id, actor
            )
            self._cancel(turn_id)
            return detail

    def _stop(self, factory, workspace_id, turn_id, actor) -> AskTurnDetail:
        with factory.begin() as session:
            turn = self._locked_turn(session, workspace_id, turn_id)
            self._mark_stopped(turn)
            session.flush()
            return self.service.get_turn(session, workspace_id, turn_id, actor)

    def _cancel(self, turn_id: UUID) -> None:
        job = self._jobs.get(turn_id)
        if job is not None:
            if job.task is not None:
                job.task.cancel()
            job.notify()

    @classmethod
    def _mark_stopped(cls, turn: AskTurn) -> None:
        if turn.status == ASK_TURN_PENDING:
            turn.status = ASK_TURN_STOPPED
            cls._settle_steering(turn, "unknown")

    @staticmethod
    def _settle_steering(turn: AskTurn, status: str) -> None:
        if any(message["status"] == "waiting" for message in turn.steering_messages):
            turn.steering_messages = [
                {**message, "status": status}
                if message["status"] == "waiting"
                else message
                for message in turn.steering_messages
            ]

    async def steer(
        self, factory, workspace_id, turn_id, request, actor
    ) -> AskTurnDetail:
        async with self._write:
            job = self._jobs.get(turn_id)
            detail, created = await run_in_threadpool(
                self._register_steer,
                factory,
                workspace_id,
                turn_id,
                request,
                actor,
                job,
            )
            if created and job is not None:
                job.steering.put_nowait(
                    SteerInput(str(request.request_id), request.user_message)
                )
                job.notify()
            return detail

    def _register_steer(
        self, factory, workspace_id, turn_id, request: AskSteerRequest, actor, job
    ):
        validate_request_inputs(request)
        request.user_message = _validate_user_message(request.user_message)
        with factory.begin() as session:
            turn = self._locked_turn(session, workspace_id, turn_id)
            request_id = str(request.request_id)
            if any(message["id"] == request_id for message in turn.steering_messages):
                return self.service.get_turn(
                    session, workspace_id, turn_id, actor
                ), False
            if (
                job is None
                or job.actor.actor_id != actor.actor_id
                or turn.status != ASK_TURN_PENDING
                or not turn.steer_supported
            ):
                raise self._turn_changed()
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

    def _enable_steering(self, job) -> None:
        with job.factory.begin() as session:
            turn = self._locked_turn(session, job.workspace_id, job.turn_id)
            if turn.status == ASK_TURN_PENDING:
                turn.steer_supported = True

    async def _next_steer(self, job) -> SteerInput:
        steer = await job.steering.get()
        actor = await run_in_threadpool(job.authorize)
        if not await run_in_threadpool(
            self._visible, job.factory, job.workspace_id, job.evidence, actor
        ):
            raise AskGenerationError(ASK_TURN_EVIDENCE_UNAVAILABLE)
        return steer

    async def _steer_update(self, job: _Job, update: SteerUpdate) -> None:
        status = _STEER_STATES.get(update.status)
        if status is not None:
            await run_in_threadpool(self._update_steer, job, update.id, status)
            job.notify()

    def _update_steer(self, job, request_id, status) -> None:
        with job.factory.begin() as session:
            turn = self._locked_turn(session, job.workspace_id, job.turn_id)
            if turn.status == ASK_TURN_PENDING:
                turn.steering_messages = [
                    {**message, "status": status}
                    if message["id"] == request_id
                    else message
                    for message in turn.steering_messages
                ]

    @staticmethod
    async def _delta(job: _Job, text: str) -> None:
        job.preview += text
        job.notify()

    def get_turn(self, factory, workspace_id, turn_id, actor) -> AskTurnDetail:
        with factory() as session:
            return self.service.get_turn(session, workspace_id, turn_id, actor)

    async def events(
        self, factory, workspace_id, initial, authorize
    ) -> AsyncIterator[str]:
        job = self._jobs.get(initial.id)
        last_state = None
        preview = ""
        while True:
            changed = job.changed if job is not None else None
            done = job is None or job.done.is_set()
            try:
                actor = await run_in_threadpool(authorize)
                detail = await run_in_threadpool(
                    self.get_turn, factory, workspace_id, initial.id, actor
                )
            except (AskAccessRevokedError, ReadNotFoundError):
                return
            state = detail.model_dump_json()
            if state != last_state:
                yield f"event: state\ndata: {state}\n\n"
                last_state = state
            if detail.status not in _LIVE or done:
                return
            current = job.preview
            if current or preview:
                can_read = await run_in_threadpool(
                    self._visible,
                    factory,
                    workspace_id,
                    job.evidence,
                    actor,
                )
                if can_read and current != preview:
                    delta = json.dumps({"text": current[len(preview) :]})
                    yield f"event: delta\ndata: {delta}\n\n"
                    preview = current
                elif preview and not can_read:
                    yield 'event: delta\ndata: {"text":"","reset":true}\n\n'
                    preview = ""
            try:
                await asyncio.wait_for(changed.wait(), timeout=15)
            except TimeoutError:
                yield ": keepalive\n\n"

    @staticmethod
    def _visible(factory, workspace_id, evidence, actor) -> bool:
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
    def _turn(session, workspace_id, turn_id) -> AskTurn:
        turn = get_in_workspace(
            session, AskTurn, workspace_id=workspace_id, resource_id=turn_id
        )
        if turn is None:
            raise ReadNotFoundError("ask turn not found")
        return turn

    def _locked_turn(self, session, workspace_id, turn_id) -> AskTurn:
        acquire_workspace_write_intent(session)
        turn = self._turn(session, workspace_id, turn_id)
        session.scalar(
            ask_session_for_turn_insert_statement(turn.ask_session_id, workspace_id)
        )
        session.refresh(turn)
        return turn

    @staticmethod
    def _turn_changed() -> RejectedByPolicyError:
        return RejectedByPolicyError(
            "the active Ask turn has changed", error_code="ask_session_changed"
        )

    async def close(self) -> None:
        workers = tuple(self._workers)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
