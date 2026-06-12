"""Bearer-token verification for capability workers.

Every worker previously compared tokens with ``==``; the Odysseus broker
route used ``secrets.compare_digest``. This module makes constant-time
comparison the only path.
"""

from __future__ import annotations

import secrets


def verify_bearer(authorization: str | None, expected_token: str) -> bool:
    """True iff ``authorization`` is exactly ``Bearer <expected_token>``.

    Constant-time on the comparison itself. An empty ``expected_token``
    never verifies — a worker without a configured token must reject
    requests loudly rather than run open.
    """
    if not expected_token or not authorization:
        return False
    return secrets.compare_digest(authorization, f"Bearer {expected_token}")
