"""In-process owner port used by the composed Social router, never by standalone."""

from typing import Protocol


class SocialOwner(Protocol):
    def followed_users(self, viewer_id: str) -> set[str]: ...

    def blocks(self, viewer_id: str) -> tuple[set[str], set[str]]:
        """Return (outgoing, incoming) IDs from the canonical registry."""
        ...

    def mention(self, kind: str, handle: str) -> dict[str, str | None] | None: ...

    def notify(self, payload: dict[str, object]) -> None: ...


def get_social_owner() -> SocialOwner | None:
    return None
