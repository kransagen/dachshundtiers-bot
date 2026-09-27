"""Repository layer — explicit-session, pure-persistence access to PostgreSQL.

Rules enforced here (design §15):
* every method takes the session as an argument; the caller owns the
  transaction boundary (see ``db.services.transaction``)
* NO discord import anywhere in this package — the danger of a repository
  spontaneously mutating Discord roles is structurally impossible
* repositories return ORM instances and never guess identity or authority
"""

from db.repositories.cooldowns import CooldownRepository, COOLDOWN_HT3, COOLDOWN_WAITLIST
from db.repositories.evaluations import EvaluationRepository, TesterRepository
from db.repositories.kits import (
    KitRepository,
    KitRoleRepository,
    TierDefinitionRepository,
    ensure_dimensions,
)
from db.repositories.outbox import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_DONE,
    OUTBOX_FAILED,
    OUTBOX_IN_PROGRESS,
    OUTBOX_MAX_ATTEMPTS,
    OUTBOX_PENDING,
    OutboxRepository,
)
from db.repositories.players import (
    CLAIM_ADOPTED,
    CLAIM_CREATED,
    CLAIM_RENAMED,
    CLAIM_UNCHANGED,
    PLAYER_SOURCE_DISCORD,
    PLAYER_SOURCE_MIGRATION,
    RESOLVE_DISCORD_ID,
    RESOLVE_IGN,
    PlayerIdentityError,
    PlayerRepository,
)
from db.repositories.queues import (
    QUEUE_ENTRY_LEFT,
    QUEUE_ENTRY_PULLED,
    QUEUE_ENTRY_TESTED,
    QUEUE_ENTRY_WAITING,
    QueueEntryRepository,
    QueueRepository,
)
from db.repositories.results import (
    ANNOUNCEMENT_FAILED,
    ANNOUNCEMENT_PENDING,
    ANNOUNCEMENT_SENT,
    PROMOTION_COMMITTED,
    PROMOTION_DB_FAILED_OUTBOXED,
    PROMOTION_DISCORD_FAILED,
    PROMOTION_DISCORD_PENDING,
    ResultRepository,
)
from db.repositories.sync_audit import (
    SYNC_ACTION_ANOMALY,
    SYNC_ACTION_APPLIED,
    SYNC_ACTION_FAILED,
    SYNC_ACTION_SKIPPED,
    SYNC_RUN_FAILED,
    SYNC_RUN_PARTIAL,
    SYNC_RUN_RUNNING,
    SYNC_RUN_SUCCESS,
    AuditRepository,
    BotConfigRepository,
    MigrationIssueRepository,
    SyncActionRepository,
    SyncRunRepository,
)
from db.repositories.tickets import (
    TICKET_CLOSED,
    TICKET_OPEN,
    TICKET_TYPE_EVAL,
    TICKET_TYPE_FIGHT,
    TicketMemberRepository,
    TicketRepository,
)
from db.repositories.tiers import (
    MirrorRepository,
    MirrorServiceRepository,
    ObservationResult,
    TierHistoryRepository,
)

__all__ = [
    "ANNOUNCEMENT_FAILED",
    "ANNOUNCEMENT_PENDING",
    "ANNOUNCEMENT_SENT",
    "CLAIM_ADOPTED",
    "CLAIM_CREATED",
    "CLAIM_RENAMED",
    "CLAIM_UNCHANGED",
    "COOLDOWN_HT3",
    "COOLDOWN_WAITLIST",
    "OUTBOX_DEAD_LETTER",
    "OUTBOX_DONE",
    "OUTBOX_FAILED",
    "OUTBOX_IN_PROGRESS",
    "OUTBOX_MAX_ATTEMPTS",
    "OUTBOX_PENDING",
    "PLAYER_SOURCE_DISCORD",
    "PLAYER_SOURCE_MIGRATION",
    "PROMOTION_COMMITTED",
    "PROMOTION_DB_FAILED_OUTBOXED",
    "PROMOTION_DISCORD_FAILED",
    "PROMOTION_DISCORD_PENDING",
    "QUEUE_ENTRY_LEFT",
    "QUEUE_ENTRY_PULLED",
    "QUEUE_ENTRY_TESTED",
    "QUEUE_ENTRY_WAITING",
    "RESOLVE_DISCORD_ID",
    "RESOLVE_IGN",
    "SYNC_ACTION_ANOMALY",
    "SYNC_ACTION_APPLIED",
    "SYNC_ACTION_FAILED",
    "SYNC_ACTION_SKIPPED",
    "SYNC_RUN_FAILED",
    "SYNC_RUN_PARTIAL",
    "SYNC_RUN_RUNNING",
    "SYNC_RUN_SUCCESS",
    "TICKET_CLOSED",
    "TICKET_OPEN",
    "TICKET_TYPE_EVAL",
    "TICKET_TYPE_FIGHT",
    "AuditRepository",
    "BotConfigRepository",
    "CooldownRepository",
    "EvaluationRepository",
    "KitRepository",
    "KitRoleRepository",
    "MigrationIssueRepository",
    "MirrorRepository",
    "MirrorServiceRepository",
    "ObservationResult",
    "OutboxRepository",
    "PlayerIdentityError",
    "PlayerRepository",
    "QueueEntryRepository",
    "QueueRepository",
    "ResultRepository",
    "SyncActionRepository",
    "SyncRunRepository",
    "TesterRepository",
    "TierDefinitionRepository",
    "TierHistoryRepository",
    "TicketMemberRepository",
    "TicketRepository",
    "ensure_dimensions",
]