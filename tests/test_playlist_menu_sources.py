"""The Add-to-playlist menu and the sidebar list the same playlists.

Reported repeatedly, most recently in #397: the menu comes up empty and
tells the user to create a playlist, while the sidebar's Playlist tab
shows their playlists fine. The two surfaces disagreed because they
asked Tidal different questions.

`/api/library/playlists` unions `get_user_playlists()` with
`get_favorite_playlists()`. `/api/playlists/mine` called
`get_user_playlists()` alone — tidalapi's legacy, non-paginated
`users/{id}/playlists` endpoint, the one playlist call that doesn't go
through `_fetch_all_pages`. Tidal has been inconsistent about what that
legacy endpoint reports, so an account whose own playlists were missing
from it got an empty menu and a populated sidebar.

Both now read the same union. The menu additionally filters to owned
playlists, because POST /api/playlists/{id}/tracks 403s on anything
else — offering a row that cannot accept the track would trade an empty
menu for a failing one.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def _playlist(pid: str, name: str, creator_id: str | None):
    return SimpleNamespace(
        id=pid,
        name=name,
        creator=SimpleNamespace(id=creator_id) if creator_id else None,
    )


@pytest.fixture
def srv(monkeypatch):
    import server

    monkeypatch.setattr(server, "playlist_to_dict", lambda p: {"id": p.id, "name": p.name})
    return server


def test_union_recovers_a_playlist_the_legacy_endpoint_dropped(srv, monkeypatch):
    """The #397 case: the legacy call under-reports, the favorites call
    still has it, so it must survive into the union."""
    monkeypatch.setattr(srv.tidal, "get_user_playlists", lambda: [])
    monkeypatch.setattr(
        srv.tidal, "get_favorite_playlists", lambda: [_playlist("p1", "Mine", "u1")]
    )

    out = srv._owned_and_favorited_playlists()

    assert [p.id for p in out] == ["p1"]


def test_union_dedupes_with_own_entries_winning(srv, monkeypatch):
    """A playlist in both lists appears once. Own entry first so its
    (usually richer) object is the one callers see."""
    own = _playlist("p1", "From own listing", "u1")
    fav = _playlist("p1", "From favorites", "u1")
    monkeypatch.setattr(srv.tidal, "get_user_playlists", lambda: [own])
    monkeypatch.setattr(srv.tidal, "get_favorite_playlists", lambda: [fav])

    out = srv._owned_and_favorited_playlists()

    assert len(out) == 1
    assert out[0].name == "From own listing"


def test_menu_lists_owned_playlists_from_the_union(srv, monkeypatch):
    monkeypatch.setattr(srv, "_require_auth", lambda: None)
    monkeypatch.setattr(srv.tidal, "get_user_playlists", lambda: [])
    monkeypatch.setattr(
        srv.tidal, "get_favorite_playlists", lambda: [_playlist("p1", "Mine", "u1")]
    )
    monkeypatch.setattr(srv.tidal, "owns_playlist", lambda p: True)

    assert [p["id"] for p in srv.my_playlists()] == ["p1"]


def test_menu_excludes_playlists_the_user_does_not_own(srv, monkeypatch):
    """Favorited-but-not-owned playlists are in the union for the
    sidebar. They must not reach the menu: adding to one 403s."""
    monkeypatch.setattr(srv, "_require_auth", lambda: None)
    monkeypatch.setattr(srv.tidal, "get_user_playlists", lambda: [])
    monkeypatch.setattr(
        srv.tidal,
        "get_favorite_playlists",
        lambda: [_playlist("mine", "Mine", "u1"), _playlist("theirs", "Someone else's", "u2")],
    )
    monkeypatch.setattr(
        srv.tidal, "owns_playlist", lambda p: getattr(p.creator, "id", None) == "u1"
    )

    assert [p["id"] for p in srv.my_playlists()] == ["mine"]


def test_sidebar_and_menu_agree_on_owned_playlists(srv, monkeypatch):
    """The property the bug violated: anything owned that the sidebar
    shows must also be offered by the menu."""
    monkeypatch.setattr(srv, "_require_auth", lambda: None)
    owned = [_playlist("a", "A", "u1"), _playlist("b", "B", "u1")]
    monkeypatch.setattr(srv.tidal, "get_user_playlists", lambda: [])
    monkeypatch.setattr(srv.tidal, "get_favorite_playlists", lambda: owned)
    monkeypatch.setattr(srv.tidal, "owns_playlist", lambda p: True)

    sidebar = {p["id"] for p in srv.library_playlists()}
    menu = {p["id"] for p in srv.my_playlists()}

    assert sidebar == menu
