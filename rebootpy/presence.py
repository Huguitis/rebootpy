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

from typing import TYPE_CHECKING

from .errors import Forbidden, PartyError
from .enums import Platform, AwayStatus

if TYPE_CHECKING:
    from .client import Client
    from .friend import Friend
    from .party import ClientParty


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

    __slots__ = ('client', 'private', 'platform', 'id', 'party_type_id',
                 'app_id', 'build_id', 'net_cl', 'party_flags',
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
        if self.client.party.id == self.id:
            raise PartyError('You are already a member of this party.')

        if self.private:
            raise Forbidden('You cannot join a private party.')

        return await self.client.join_party(self.id)


class Presence:
    """Represents a presence received from a friend

    Attributes
    ----------
    client: :class:`Client`
        The client.
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

    __slots__ = ('client', 'away', 'friend', 'platform',
                 'received_at', 'status', 'joinable',
                 'session_id', 'has_properties', 'homebase_rating', 'lfg',
                 'sub_game', 'in_unjoinable_match', 'playlist', 'party_size',
                 'max_party_size', 'server_player_count',
                 'gameplay_stats', 'party', 'island_code')

    def __init__(
        self,
        client: 'Client',
        raw: dict
    ) -> None:
        self.client = client
        from_id = raw['accountId']

        self.friend = self.client.get_friend(from_id)
        self.received_at = datetime.datetime.utcnow()
        self.away = AwayStatus.AWAY if raw['status'] == 'away' \
            else AwayStatus.ONLINE

        data = (raw.get('perNs') or [{}])[0]

        self.status = data.get('status')

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

        _basic_info = json.loads(raw_properties.get('FortBasicInfo', '{}'))
        self.homebase_rating = _basic_info.get('homeBaseRating')

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

        self.party_size = raw_properties.get('Event_PartySize')
        if self.party_size is not None:
            self.party_size = int(self.party_size)

        self.max_party_size = raw_properties.get('Event_PartyMaxSize')
        if self.max_party_size is not None:
            self.max_party_size = int(self.max_party_size)

        self.server_player_count = raw_properties.get(
            'ServerPlayerCount'
        )
        if self.server_player_count is not None:
            self.server_player_count = int(self.server_player_count)

        if 'FortGameplayStats' in raw_properties:
            self.gameplay_stats = PresenceGameplayStats(
                self.friend,
                json.loads(raw_properties['FortGameplayStats']),
                players_alive,
                self.playlist
            )
        else:
            self.gameplay_stats = None

        key = "party.joininfodata.286331153"
        if key not in raw_properties:
            self.party = None
            self.joinable = None
        else:
            self.party = PresenceParty(
                self.client,
                json.loads(raw_properties[key])
            )
            self.joinable = not self.party.private

    def __repr__(self) -> str:
        return (
            f'<Presence friend={self.friend!r} away={self.away} '
            f'received_at={self.received_at!r}>'
        )
