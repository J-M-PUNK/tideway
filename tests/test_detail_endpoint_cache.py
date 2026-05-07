"""Tests for the album / mix / playlist detail TTL cache.

Each detail endpoint blocks on multiple synchronous Tidal API calls
(album fans out 5 parallel calls; playlist hits the playlist + tracks;
mix grabs items). The frontend SWR cache covers in-session repeats
within a single window, but a second window or a fresh app launch
hits Tidal cold every time — that's what this server-side cache
fixes.

Pinned contracts:
  - Cache hit: a second request inside the TTL skips the Tidal call.
  - Cache miss after TTL: a request after TTL re-invokes Tidal.
  - Per-key isolation: album/mix/playlist with the same numeric id
    don't collide.
  - Mutation invalidation: every playlist edit endpoint invalidates
    the entry so a follow-up GET sees the post-mutation state.
  - Auth-state invalidation: logout / login completion clears every
    entry so a different user can never see the previous user's
    library.
"""
import pytest
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _clean_detail_cache():
    import server

    server._invalidate_detail_cache()
    yield
    server._invalidate_detail_cache()


@pytest.fixture
def stub_auth(monkeypatch):
    import server

    monkeypatch.setattr(server, "_require_auth", lambda: None)


# ---------------------------------------------------------------------------
# Cache primitive tests
# ---------------------------------------------------------------------------


def test_lookup_returns_none_when_empty():
    import server
    assert server._lookup_detail_cache("album:42") is None


def test_store_then_lookup_returns_value():
    import server
    server._store_detail_cache("album:42", {"name": "Hello"})
    assert server._lookup_detail_cache("album:42") == {"name": "Hello"}


def test_lookup_expires_after_ttl(monkeypatch):
    import server

    times = iter([0.0, server._DETAIL_CACHE_TTL + 1.0])
    monkeypatch.setattr(server.time, "monotonic", lambda: next(times))

    server._store_detail_cache("album:42", {"hits": 1})
    assert server._lookup_detail_cache("album:42") is None


def test_invalidate_entry_drops_only_that_key():
    import server
    server._store_detail_cache("album:42", {"a": 1})
    server._store_detail_cache("playlist:abc", {"b": 2})
    server._invalidate_detail_cache_entry("album:42")
    assert server._lookup_detail_cache("album:42") is None
    assert server._lookup_detail_cache("playlist:abc") == {"b": 2}


def test_invalidate_drops_all_entries():
    import server
    server._store_detail_cache("album:42", {"a": 1})
    server._store_detail_cache("playlist:abc", {"b": 2})
    server._store_detail_cache("mix:xyz", {"c": 3})
    server._invalidate_detail_cache()
    assert server._lookup_detail_cache("album:42") is None
    assert server._lookup_detail_cache("playlist:abc") is None
    assert server._lookup_detail_cache("mix:xyz") is None


# ---------------------------------------------------------------------------
# Endpoint integration: mix_detail and playlist_detail. Album_detail uses
# more tidalapi surface (similar/review/more_by/related_artists) so we
# stick to the simpler endpoints — the cache wiring is identical.
# ---------------------------------------------------------------------------


def test_mix_detail_caches_within_ttl(stub_auth, monkeypatch):
    import server

    calls = {"n": 0}

    fake_mix = _StubMix(title="Daily Mix 1", sub_title="Today's hits")

    def fake_session_mix(_id):
        calls["n"] += 1
        return fake_mix

    monkeypatch.setattr(server.tidal.session, "mix", fake_session_mix)

    r1 = server.mix_detail("xyz")
    r2 = server.mix_detail("xyz")
    assert r1 == r2
    assert calls["n"] == 1


def test_mix_detail_distinct_ids_isolated(stub_auth, monkeypatch):
    import server

    calls = {"n": 0}

    def fake_session_mix(_id):
        calls["n"] += 1
        return _StubMix(title=f"Mix {_id}", sub_title="")

    monkeypatch.setattr(server.tidal.session, "mix", fake_session_mix)

    server.mix_detail("a")
    server.mix_detail("b")
    server.mix_detail("a")
    server.mix_detail("b")
    assert calls["n"] == 2


def test_playlist_detail_caches_within_ttl(stub_auth, monkeypatch):
    import server

    calls = {"n": 0}

    fake_playlist = _StubPlaylist(name="Chill")

    def fake_session_playlist(_id):
        calls["n"] += 1
        return fake_playlist

    monkeypatch.setattr(server.tidal.session, "playlist", fake_session_playlist)
    monkeypatch.setattr(server, "playlist_to_dict", lambda p: {"name": p.name})

    r1 = server.playlist_detail("p1")
    r2 = server.playlist_detail("p1")
    assert r1 == r2
    assert calls["n"] == 1


def test_album_id_and_mix_id_with_same_value_dont_collide(stub_auth, monkeypatch):
    """A numeric album id and a string mix id that look the same
    (e.g. both `42`) must not share a cache entry — keys are
    namespaced by kind."""
    import server

    server._store_detail_cache("album:42", {"kind": "album"})
    server._store_detail_cache("mix:42", {"kind": "mix"})
    assert server._lookup_detail_cache("album:42") == {"kind": "album"}
    assert server._lookup_detail_cache("mix:42") == {"kind": "mix"}


# ---------------------------------------------------------------------------
# Mutation invalidation: every playlist edit endpoint must drop the
# affected entry so a follow-up GET sees the post-mutation state. We
# call these endpoints as plain functions and verify the cache state.
# ---------------------------------------------------------------------------


def test_delete_playlist_invalidates_cache(stub_auth, monkeypatch):
    import server

    server._store_detail_cache("playlist:p1", {"stale": True})

    fake_pl = _StubOwnedPlaylist()
    monkeypatch.setattr(server, "_get_owned_playlist", lambda _id: fake_pl)

    server.delete_playlist("p1")
    assert server._lookup_detail_cache("playlist:p1") is None


def test_edit_playlist_invalidates_cache(stub_auth, monkeypatch):
    import server

    server._store_detail_cache("playlist:p1", {"stale": True})

    fake_pl = _StubOwnedPlaylist(name="Old", description="Old description")
    monkeypatch.setattr(server, "_get_owned_playlist", lambda _id: fake_pl)
    monkeypatch.setattr(server.tidal.session, "playlist", lambda _id: fake_pl)
    monkeypatch.setattr(server, "playlist_to_dict", lambda p: {"name": p.name})

    server.edit_playlist("p1", server.EditPlaylistRequest(title="New"))
    assert server._lookup_detail_cache("playlist:p1") is None


def test_add_tracks_to_playlist_invalidates_cache(stub_auth, monkeypatch):
    import server

    server._store_detail_cache("playlist:p1", {"stale": True})

    fake_pl = _StubOwnedPlaylist()
    monkeypatch.setattr(server, "_get_owned_playlist", lambda _id: fake_pl)

    server.add_tracks_to_playlist(
        "p1", server.AddTracksRequest(track_ids=["1", "2"])
    )
    assert server._lookup_detail_cache("playlist:p1") is None


def test_remove_track_from_playlist_invalidates_cache(stub_auth, monkeypatch):
    import server

    server._store_detail_cache("playlist:p1", {"stale": True})

    fake_pl = _StubOwnedPlaylist()
    monkeypatch.setattr(server, "_get_owned_playlist", lambda _id: fake_pl)

    server.remove_track_from_playlist("p1", 0)
    assert server._lookup_detail_cache("playlist:p1") is None


def test_move_track_in_playlist_invalidates_cache(stub_auth, monkeypatch):
    import server

    server._store_detail_cache("playlist:p1", {"stale": True})

    fake_pl = _StubOwnedPlaylist()
    monkeypatch.setattr(server, "_get_owned_playlist", lambda _id: fake_pl)

    server.move_track_in_playlist(
        "p1", server.MoveTrackRequest(media_id="123", position=2)
    )
    assert server._lookup_detail_cache("playlist:p1") is None


def test_failed_mutation_does_not_invalidate(stub_auth, monkeypatch):
    """Order matters: the invalidation only fires when the underlying
    mutation succeeded. A 502 from tidalapi must propagate without
    dropping the cached entry — otherwise a transient Tidal error
    forces a re-fetch on the next page open."""
    import server

    server._store_detail_cache("playlist:p1", {"stale": True})

    fake_pl = _StubOwnedPlaylist(should_raise=True)
    monkeypatch.setattr(server, "_get_owned_playlist", lambda _id: fake_pl)

    with pytest.raises(HTTPException):
        server.delete_playlist("p1")

    assert server._lookup_detail_cache("playlist:p1") == {"stale": True}


def test_logout_clears_detail_cache(stub_auth, monkeypatch):
    """Different user signing in on the same local server must not
    see the previous user's albums / mixes / playlists. Logout
    invalidates everything."""
    import server

    server._store_detail_cache("album:42", {"a": 1})
    server._store_detail_cache("playlist:abc", {"b": 2})
    server._store_detail_cache("mix:xyz", {"c": 3})

    monkeypatch.setattr(server, "_pcm_player_singleton", None)
    monkeypatch.setattr(server.tidal, "logout", lambda: None)

    server.auth_logout()

    assert server._lookup_detail_cache("album:42") is None
    assert server._lookup_detail_cache("playlist:abc") is None
    assert server._lookup_detail_cache("mix:xyz") is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubMix:
    def __init__(self, title: str, sub_title: str):
        self.title = title
        self.sub_title = sub_title

    def items(self):
        return []

    def image(self, _size):
        return None


class _StubPlaylist:
    def __init__(self, name: str):
        self.name = name

    def tracks(self):
        return []


class _StubOwnedPlaylist:
    """Stand-in for the tidalapi UserPlaylist surface that the edit
    endpoints touch. Each method either no-ops or raises depending
    on `should_raise`."""

    def __init__(self, name: str = "", description: str = "", should_raise: bool = False):
        self.name = name
        self.description = description
        self._raise = should_raise

    def _maybe_raise(self):
        if self._raise:
            raise RuntimeError("simulated tidal failure")

    def delete(self):
        self._maybe_raise()

    def edit(self, **_kwargs):
        self._maybe_raise()

    def add(self, _ids):
        self._maybe_raise()

    def remove_by_index(self, _index):
        self._maybe_raise()

    def move_by_id(self, _media_id, _position):
        self._maybe_raise()
