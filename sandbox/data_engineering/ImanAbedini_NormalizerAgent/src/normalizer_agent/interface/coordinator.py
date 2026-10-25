"""Coordinator Integration Adapter.

Provides an abstract base interface and a reference queue/batch adapter
for seamless plugging into the Fanos workflow orchestrator (Celery/Temporal/Kafka).
"""

from __future__ import annotations

import abc
import logging
from typing import Any, Protocol

from normalizer_agent.interface.api import Normalizer
from normalizer_agent.schemas.envelope import NormalizeResult, RecordStatus

logger = logging.getLogger(__name__)


class CoordinatorMessage(Protocol):
    """Protocol for messages ingested from coordinator queues."""

    id: str
    body: dict[str, Any]
    trace_id: str | None


class AbstractCoordinatorAdapter(abc.ABC):
    """Abstract interface defining the contract between Normalizer and Fanos Coordinator."""

    def __init__(self, normalizer: Normalizer) -> None:
        self.normalizer = normalizer

    @abc.abstractmethod
    async def process_item(self, message: CoordinatorMessage) -> NormalizeResult:
        """Process a single unit of work from the coordinator."""
        raise NotImplementedError

    @abc.abstractmethod
    async def process_batch(self, messages: list[CoordinatorMessage]) -> list[NormalizeResult]:
        """Process a batch of work units."""
        raise NotImplementedError

    @abc.abstractmethod
    async def handle_result(self, message: CoordinatorMessage, result: NormalizeResult) -> None:
        """Route normalized records, review flags, or failures to appropriate queues/DB."""
        raise NotImplementedError


class ReferenceCoordinatorAdapter(AbstractCoordinatorAdapter):
    """Reference in-memory/stub coordinator adapter demonstrating delivery semantics."""

    def __init__(self, normalizer: Normalizer) -> None:
        super().__init__(normalizer)
        self.published_canonical: list[dict[str, Any]] = []
        self.published_review: list[dict[str, Any]] = []
        self.published_dead_letter: list[dict[str, Any]] = []

    async def process_item(self, message: CoordinatorMessage) -> NormalizeResult:
        result = await self.normalizer.normalize(
            message.body,
            record_id=message.id,
            trace_id=message.trace_id,
        )
        await self.handle_result(message, result)
        return result

    async def process_batch(self, messages: list[CoordinatorMessage]) -> list[NormalizeResult]:
        records = [(m.id, m.body) for m in messages]
        traces = [m.trace_id for m in messages]
        results = await self.normalizer.normalize_batch(records, trace_ids=traces)
        for msg, res in zip(messages, results, strict=False):
            await self.handle_result(msg, res)
        return results

    async def handle_result(self, message: CoordinatorMessage, result: NormalizeResult) -> None:
        dumped = result.model_dump(mode="json")
        if result.status == RecordStatus.NORMALIZED:
            logger.info("routing to canonical storage: %s", result.record_id)
            self.published_canonical.append(dumped)
        elif result.status == RecordStatus.NEEDS_REVIEW:
            logger.warning(
                "routing to human review queue: %s (%d items)",
                result.record_id,
                len(result.review_items),
            )
            self.published_review.append(dumped)
        else:
            logger.error(
                "routing to dead-letter queue: %s (%s)", result.record_id, result.error_codes
            )
            self.published_dead_letter.append(dumped)
