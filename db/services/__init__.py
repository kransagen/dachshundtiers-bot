"""Service layer — orchestrators over the explicit-session repositories.

These are the only objects that combine multiple persistence steps; they own
transaction boundaries via ``db.services.transaction``. None of them can
touch Discord — repositories already guarantee that, and services only compose
repositories.
"""

from db.services.config_validation import (
    KitRoleConfigError,
    KitRoleValidation,
    validate_kit_role_configuration,
)
from db.services.mirror_sync import (
    SYNC_ANOMALY_UNKNOWN_PLAYER,
    DiscordSyncOutcome,
    DiscordSyncService,
)
from db.services.outbox_consumer import (
    CONSUMED_ALREADY_COMMITTED,
    CONSUMED_DEAD_LETTER,
    CONSUMED_DONE,
    CONSUMED_REFUSED,
    CONSUMED_RETRY,
    OutboxConsumer,
    OutboxConsumption,
    build_commit_kwargs,
    default_stale_cutoff,
)
from db.services.promotion import (
    CooldownSpec,
    PromotionCommitResult,
    PromotionCommitService,
    PromotionWedgeOutcome,
    WEDGE_EVENT_TYPE,
    commit_confirmed_promotion,
    commit_promotion_with_wedge,
    enqueue_promotion_wedge,
    grant_confirmation,
)
from db.services.session import transaction
from db.services.tier_mirror import (
    ANOMALY_MISSING_TIER,
    ANOMALY_MULTIPLE_TIERS,
    ANOMALY_UNKNOWN_ROLES,
    ClassificationResult,
    KitObservation,
    MirrorService,
    classify_member_roles,
)

__all__ = [
    "ANOMALY_MISSING_TIER",
    "ANOMALY_MULTIPLE_TIERS",
    "ANOMALY_UNKNOWN_ROLES",
    "CONSUMED_ALREADY_COMMITTED",
    "CONSUMED_DEAD_LETTER",
    "CONSUMED_DONE",
    "CONSUMED_REFUSED",
    "CONSUMED_RETRY",
    "ClassificationResult",
    "CooldownSpec",
    "DiscordSyncOutcome",
    "DiscordSyncService",
    "KitObservation",
    "KitRoleConfigError",
    "KitRoleValidation",
    "MirrorService",
    "OutboxConsumer",
    "OutboxConsumption",
    "PromotionCommitResult",
    "PromotionCommitService",
    "PromotionWedgeOutcome",
    "SYNC_ANOMALY_UNKNOWN_PLAYER",
    "WEDGE_EVENT_TYPE",
    "build_commit_kwargs",
    "classify_member_roles",
    "commit_confirmed_promotion",
    "commit_promotion_with_wedge",
    "default_stale_cutoff",
    "enqueue_promotion_wedge",
    "grant_confirmation",
    "transaction",
    "validate_kit_role_configuration",
]