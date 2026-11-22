"""Owner calls used by Events; standalone retains its HTTP clients."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Protocol, TypedDict, Unpack

from events import social_client, users_client
from events.domain.models import Event
from events.main_dependencies import settings


class Notification(TypedDict):
    recipient_user_id: str
    actor_user_id: str | None
    event_type: str
    target_type: str
    target_id: str
    target_url: str
    title: str
    body: str | None
    dedupe_key: str
    metadata: dict[str, str | int | bool | None]


class Notify(Protocol):
    def __call__(self, **notice: Unpack[Notification]) -> bool: ...


def _notify(**notice: Unpack[Notification]) -> bool:
    return users_client.notify_user(settings, **notice)


@dataclass(frozen=True)
class EventOwner:
    check_page_role: Callable[[str, str], str | None]
    get_artist_ref: Callable[[str], dict[str, str] | None]
    get_artist_refs: Callable[[list[str]], dict[str, dict[str, str]]]
    get_active_user_refs: Callable[[list[str]], dict[str, dict[str, str]]]
    get_user_by_username: Callable[[str], dict[str, str] | None]
    notify_user: Notify
    announce_event: Callable[[Event], bool]
    retry_announcement_on_conflict: bool = False


def get_event_owner() -> EventOwner:
    return EventOwner(
        check_page_role=partial(users_client.check_page_role, settings),
        get_artist_ref=partial(users_client.get_artist_ref, settings),
        get_artist_refs=partial(users_client.get_artist_refs, settings),
        get_active_user_refs=partial(users_client.get_active_user_refs, settings),
        get_user_by_username=partial(users_client.get_user_by_username, settings),
        notify_user=_notify,
        announce_event=partial(social_client.announce_event, settings),
    )
