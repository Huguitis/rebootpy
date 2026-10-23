# -*- coding: utf-8 -*-

"""
MIT License

Copyright (c) 2019-2021 Terbau

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import re
import datetime
import json

from typing import TYPE_CHECKING, Optional

from .errors import Forbidden, PartyError
from .enums import Platform, AwayStatus

if TYPE_CHECKING:
    from .client import Client
    from .friend import Friend
    from .party import ClientParty

_UNSET = object()


class PresenceGameplayStats:
    """Represents gameplaystats received from presence.

    Attributes
    ----------
    friend: :class:`Friend`
        The friend these stats belong to.
    state: :class:`str`
        The state.

        .. note::

            It's not really known what value this property might
            hold. This is pretty much always an empty string.
    playlist: :class:`str`
        The playlist.

        .. note::

            The playlist from the gameplay stats property usually
            isn't updated. Consider using :attr:`Presence.playlist` instead
            as that seems to always be the correct playlist.
    players_alive: :class:`int`
        The amount of players alive in the current game.
    kills: :class:`int`
        The amount of kills the friend currently has. Aliased to ``num_kills``
        as well for legacy reasons.
    fell_to_death: :class:`bool`
        ``True`` if friend fell to death in its current game, else ``False``
    """

    __slots__ = ('friend', 'state', 'playlist', 'players_alive', 'kills',
                 'num_kills', 'fell_to_death')

    def __init__(
        self,
        friend: 'Friend',
        data: str,
        players_alive: int,
        playlist: str
    ) -> None:
        self.friend = friend
        self.state = data.get('state')
        self.playlist = playlist
        self.players_alive = players_alive

        self.kills = data.get('numKills')
        if self.kills is not None:
            self.kills = int(self.kills)

        self.num_kills = self.kills

        self.fell_to_death = True if data.get('bFellToDeath') else False

    def __repr__(self) -> str:
        return ('<PresenceGameplayStats friend={0.friend!r} '
                'players_alive={0.players_alive} num_kills={0.num_kills} '
                'playlist={0.playlist!r}>'.format(self))


class PresenceParty:
    """Represents a party received from presence.

    Before accessing any of this class' attributes or functions
    you should always check if the party is private: ::

        @client.event
        async def event_friend_presence(before, after):
            # after is the newly received presence
            presence = after

            # check if presence is from the account 'Terbau'
            # NOTE: you should always use id over display_name
            # but for this example i've use display_name just
            # to demonstrate.
            if presence.friend.display_name != 'Terbau':
                return

            # check if party is private
            if presence.party.private:
                return

            # if all the checks above succeeds we join the party
            await presence.party.join()


    .. note::

        If the party is private, all attributes below private will
        be ``None``.

    Attributes
    ----------
    client: :class:`str`
        The client.
    private: :class:`bool`
        ``True`` if the party is private else ``False``.
    platform: :class:`Platform`
        The platform of the friend.
    id: :class:`str`
        The party's id.
    epic_party_id: Optional[:class:`str`]
        The EOS party's id, if it was included in presence.
    party_type_id: :class:`str`
        The party's type id.
    app_id: :class:`str`
        The party's app id.
    build_id: :class:`str`
        The party's build id. Similar format to :attr:`Client.party_build_id`.
    net_cl: :class:`str`
        The party's net_cl. Similar format to :attr:`Client.net_cl`.
    party_flags: :class:`str`
        The party's flags.
    not_accepting_reason: :class:`str`
        The party's not accepting reason.
    playercount: :class:`int`
        The party's playercount.
    """

    __slots__ = ('client', 'private', 'platform', 'id', 'epic_party_id',
                 'party_type_id', 'app_id', 'build_id', 'net_cl', 'party_flags',
                 'not_accepting_reason', 'playercount', 'raw')

    def __init__(
        self,
        client: 'Client',
        data: dict
    ) -> None:
        self.client = client
        self.raw = data

        # In order to even get here, the key party.joininfodata.286331153
        # needs to exist, so we know the party type id is always 286331153
        self.party_type_id = 286331153

        pl = data.get('sP')
        self.platform = Platform(pl) if pl is not None else None
        self.private = data.get('bIsPrivate', False)
        self.id = data.get('p')
        self.epic_party_id = data.get('epic_party_id')
        self.app_id = data.get('d')
        self.build_id = data.get('b')

        if self.build_id is not None and self.build_id.startswith('1:3:'):
            self.net_cl = self.build_id[4:]
        else:
            self.net_cl = None

        self.party_flags = data.get('f')
        self.not_accepting_reason = data.get('nAR')

        self.playercount = data.get('pc')
        if self.playercount is not None:
            self.playercount = int(self.playercount)

    def __repr__(self) -> str:
        return ('<PresenceParty private={0.private} id={0.id!r} '
                'epic_party_id={0.epic_party_id!r} '
                'playercount={0.playercount}>'.format(self))

    async def join(self) -> 'ClientParty':
        """|coro|

        Joins the friends' party.

        Raises
        ------
        PartyError
            You are already a member of this party.
        Forbidden
            The party is private.
        HTTPException
            Something else went wrong when trying to join this party.

        Returns
        -------
        :class:`ClientParty`
            The party that was just joined.
        """
        current = self.client.party
        if (
            self.epic_party_id is not None
            and current.epic_party_id == self.epic_party_id
        ) or (
            self.id is not None and current.id == self.id
        ):
            raise PartyError('You are already a member of this party.')

        if self.private:
            raise Forbidden('You cannot join a private party.')

        if self.epic_party_id is not None:
            return await self.client.join_party(self.epic_party_id)

        if self.id is not None:
            return await self.client.join_legacy_party(self.id)

        raise PartyError('Party id is not available from presence.')


class Presence:
    """Represents a presence received from a friend

    Attributes
    ----------
    client: :class:`Client`
        The client.
    raw: :class:`dict`
        The raw data.
    away: :class:`AwayStatus`
        The users away status.
    friend: :class:`Friend`
        The friend you received this presence from.
    platform: :class:`Platform`
        The platform this presence was sent from.
    received_at: :class:`datetime.datetime`
        The UTC time of when the client received this presence.
    status: :class:`str`
        The friend's status.
    joinable: :class:`bool`
        Says if friend is joinable.
    invitable: :class:`bool`
        Says if the friend can be invited according to EOS presence.
    session_id: :class:`str`
        The friend's current session id. Often referred to as
        server key or game key. Returns ``None`` if the friend is not currently
        in a game.
    has_properties: :class:`bool`
        ``True`` if the presence has properties else ``False``.

        .. warning::

            All attributes below this point will be ``None`` if
            :attr:`has_properties` is ``False``.
    party: :class:`PresenceParty`
        The friend's party.
    gameplay_stats: Optional[:class:`PresenceGameplayStats`]
        The friend's gameplay stats. Will be ``None`` if no gameplay stats
        are currently available.
    homebase_rating: :class:`str`
        The friend's homebase rating
    lfg: :class:`bool`
        ``True`` if the friend is currently looking for a game.
    sub_game: :class:`int`
        The friend's current subgame.
    in_unjoinable_match: :class:`bool`
        ``True`` if friend is in unjoinable match else ``False``.
    playlist: :class:`str`
        The friend's current playlist.
    party_size: :class:`int`
        The size of the friend's party.
    max_party_size: :class:`int`
        The max size of the friend's party.
    server_player_count: :class:`str`
        The playercount of the friend's server.
    island_code: :class:`str`
        The friend's current experience, playlist or island code.
    """

    __slots__ = ('client', 'raw', 'away', 'friend', 'platform',
                 'received_at', 'status', 'joinable', 'invitable',
                 'session_id', 'has_properties', 'lfg',
                 'sub_game', 'in_unjoinable_match', 'playlist', 'party_size',
                 'max_party_size', 'server_player_count',
                 'party', 'island_code', '_basic_info_raw',
                 '_homebase_rating', '_gameplay_stats_raw',
                 '_gameplay_stats', '_players_alive')

    def __init__(
        self,
        client: 'Client',
        raw: dict,
        previous: Optional['Presence'] = None,
    ) -> None:
        self.client = client
        if previous is not None and raw.get('status') != 'offline':
            raw = self._merge_raw(previous.raw, raw)
        self.raw = raw

        from_id = raw['accountId']

        self.friend = self.client.get_friend(from_id)
        self.received_at = datetime.datetime.utcnow()
        self.away = AwayStatus.AWAY if raw['status'] == 'away' \
            else AwayStatus.ONLINE

        per_ns = raw.get('perNs') or [{}]
        private_data = next(
            (item for item in per_ns if item.get('ns') == '_'),
            {},
        )
        data = next(
            (
                item for item in per_ns
                if item.get('props')
                and (
                    item.get('productId') == 'prod-fn'
                    or item.get('ns') == self.client.deployment_id
                    or item.get('props', {}).get('EOS_ProductName')
                    == 'Fortnite'
                )
            ),
            next((item for item in per_ns if item.get('props')), per_ns[0]),
        )

        self.status = data.get('activity', {}).get('value')

        raw_properties = {
            key: (
                value[1:] if isinstance(value, str)
                and not key.startswith('EOS_') else value
            )
            for key, value in data.get('props', {}).items()
        }
        self.has_properties = raw_properties != {}

        # All values below will be "None" if properties is empty.

        self.platform = Platform(raw_properties.get('EOS_Platform'))

        self.session_id = raw_properties.get('SessionIdAttributeKey') or None

        self._basic_info_raw = raw_properties.get('FortBasicInfo')
        self._homebase_rating = _UNSET

        if raw_properties.get('FortLFG') is None:
            self.lfg = None
        else:
            self.lfg = int(raw_properties.get('FortLFG')) == 1

        self.sub_game = raw_properties.get('FortSubGame')

        self.in_unjoinable_match = raw_properties.get(
            'InUnjoinableMatch'
        )
        if self.in_unjoinable_match is not None:
            self.in_unjoinable_match = \
                True if self.in_unjoinable_match == 'true' else False

        self.playlist = None or raw_properties.get('GamePlaylistName')
        self.island_code = None or raw_properties.get('IslandCode')

        players_alive = raw_properties.get('Event_PlayersAlive')
        if players_alive is not None:
            players_alive = int(players_alive)

        party_size = raw_properties.get('FortPartySize') or raw_properties.get('Event_PartySize')
        if party_size is not None:
            self.party_size = int(party_size.replace('i', '').replace('s', ''))
        else:
            self.party_size = None

        self.max_party_size = raw_properties.get('Event_PartyMaxSize')
        if self.max_party_size is not None:
            self.max_party_size = int(self.max_party_size)

        self.server_player_count = raw_properties.get(
            'ServerPlayerCount'
        )
        if self.server_player_count is not None:
            self.server_player_count = int(self.server_player_count)

        self._gameplay_stats_raw = raw_properties.get('FortGameplayStats')
        self._players_alive = players_alive
        self._gameplay_stats = _UNSET

        eos_party = private_data.get('party') or {}
        key = "party.joininfodata.286331153"
        if key not in raw_properties:
            self.party = None
            legacy_joinable = None
        else:
            party_data = json.loads(raw_properties[key])
            party_data['epic_party_id'] = eos_party.get('id')
            self.party = PresenceParty(
                self.client,
                party_data,
            )
            legacy_joinable = not self.party.private

        self.joinable = eos_party.get('joinable', legacy_joinable)
        self.invitable = eos_party.get('invitable')

    @property
    def homebase_rating(self) -> Optional[int]:
        if self._homebase_rating is _UNSET:
            info = json.loads(self._basic_info_raw or '{}')
            self._homebase_rating = info.get('homeBaseRating')
        return self._homebase_rating

    @property
    def gameplay_stats(self) -> Optional['PresenceGameplayStats']:
        if self._gameplay_stats is _UNSET:
            raw = self._gameplay_stats_raw
            self._gameplay_stats = None if raw is None else (
                PresenceGameplayStats(
                    self.friend,
                    json.loads(raw),
                    self._players_alive,
                    self.playlist,
                )
            )
        return self._gameplay_stats

    @property
    def available(self) -> bool:
        """:class:`bool`: ``False`` if this presence reports the friend went
        offline. Any other status (online, away, ...) counts as available.
        """
        return self.raw.get('status') != 'offline'

    @staticmethod
    def _merge_raw(previous: dict, current: dict) -> dict:
        merged = {**previous, **current}
        namespaces = {}
        order = []

        for index, item in enumerate(previous.get('perNs') or []):
            key = item.get('ns', f'previous:{index}')
            namespaces[key] = dict(item)
            order.append(key)

        for index, item in enumerate(current.get('perNs') or []):
            key = item.get('ns', f'current:{index}')
            existing = namespaces.get(key, {})
            namespace = {**existing, **item}
            for field in ('activity', 'party', 'props', 'conn'):
                if field in existing or field in item:
                    namespace[field] = {
                        **(existing.get(field) or {}),
                        **(item.get(field) or {}),
                    }
            namespaces[key] = namespace
            if key not in order:
                order.append(key)

        merged['perNs'] = [namespaces[key] for key in order]
        return merged

    def __repr__(self) -> str:
        return (
            f'<Presence friend={self.friend!r} away={self.away} '
            f'received_at={self.received_at!r}>'
        )
