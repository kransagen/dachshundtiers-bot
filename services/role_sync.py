"""Centrální RoleSyncService – jediná cesta „Discord tier role → players.json".

Nepřidává paralelní logiku k ``services/playersync`` – je to centrální fasáda,
ve které se schází kompletní kanonický tok:

    Discord member (id, jména, role_ids)
        → make_member                (normalizace)
        → analyze_sync               (analyza: 7 kategorií, retired tiery,
                                      párování podle discordId)
        → fingerprint / build_actions (plán změn k potvrzení)
        → log_playersync_event       (audit)

Cogy (``cogs/playersync``, budoucí ``/sync roles``) importují JEN odsud, aby
existoval jeden synchronizační tok místo paralelních implementací. Chování je
zděděné z ``services/playersync`` – nové schopnosti (retired tiery jako
archivovaná historie, discordId jako permanentní identita) jsou zde zapnuté
defaultně.
"""

from services.playersync import (
    KIND_LABELS,
    KINDS,
    PLAYERSYNC_LOG_FILE,
    RETIRED_TIER_PREFIX,
    analyze_sync,
    build_actions,
    fingerprint,
    get_playersync_log,
    is_retired_tier,
    log_playersync_event,
    make_member,
)

__all__ = [
    "KIND_LABELS",
    "KINDS",
    "PLAYERSYNC_LOG_FILE",
    "RETIRED_TIER_PREFIX",
    "analyze_role_sync",
    "analyze_sync",
    "build_actions",
    "fingerprint",
    "get_playersync_log",
    "is_retired_tier",
    "log_playersync_event",
    "make_member",
]


def analyze_role_sync(
    players, members, roles_map, kit_display=None, *, retired_tiers=None
) -> dict:
    """Kanonická analýza role syncu (retired tiery + discordId párování zapnuté)."""
    return analyze_sync(
        players,
        members,
        roles_map,
        kit_display,
        retired_tiers=retired_tiers,
        match_by_discord_id=True,
    )