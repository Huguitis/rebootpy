# -*- coding: utf-8 -*-

"""
MIT License

Copyright (c) 2024 Oli

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

import asyncio
import aiohttp
import json
import functools
import logging
import base64
import datetime
import random

from collections import deque

from .message import FriendMessage, PartyMessage
from .presence import Presence
from .errors import STOMPError, HTTPException

from aiohttp import hdrs, helpers, client_reqrep, connector
from aiohttp.http import StreamWriter, HttpVersion10, HttpVersion11

log = logging.getLogger(__name__)


def decode_message_body(body: str) -> str:
    try:
        decoded = base64.b64decode(body).decode('utf-8')
        decoded = decoded.rstrip('\x00')
        parsed = json.loads(decoded)
        return parsed.get('msg', body)
    except (ValueError, KeyError, json.JSONDecodeError, Exception):
        return body


class WebsocketClient:
    def __init__(self, client) -> None:
        self.client = client

        self.wss_session = None
        self.websocket = None
        self.ws_task = None

        self.heartbeat_started = False
        self._ready_event = asyncio.Event()

        self.connection_id = None
        self.public_connection_id = None
        self.private_connection_id = None
        self._eas_public_sub_id = None
        self._eas_private_sub_id = None

        self.message_history = deque(maxlen=100)

    async def set_session(self) -> None:
        self.wss_session = aiohttp.ClientSession()

    async def send_presence(self, connection_id: str, retry: bool = True) -> None:
        try:
            await self.client.http.chat_send_presence(
                connection_id=connection_id,
                auth="EAS_ACCESS_TOKEN"
            )
        except HTTPException as e:
            if e.status == 404 and retry:
                log.warning(
                    f'Received 404 for presence endpoint (connection_id: {connection_id}). '
                    'Restarting websocket and retrying.'
                )
                await self.restart()
                await asyncio.sleep(1)
                await self.send_presence(connection_id=connection_id, retry=False)
            else:
                raise

    async def send_heartbeat(self, delay: int) -> None:
        while not self.websocket.closed:
            await self.websocket.send_str("\n")
            await asyncio.sleep(delay)

    async def parse_message(self, raw: str) -> None:
        raw_headers, raw_json = raw.split('\n\n', 1)
        header_lines = raw_headers.splitlines()
        message_type = header_lines[0]

        headers = {}
        for line in header_lines[1:]:
            key, value = line.split(':', 1)
            headers[key.strip()] = value.strip()

        data = json.loads(raw_json[:-1]) if len(raw_json) >= 3 else {}

        if message_type == 'MESSAGE':
            message_id = data.get('id')
            if message_id:
                if message_id in self.message_history:
                    return
                self.message_history.append(message_id)

        log.debug(
            f'{datetime.datetime.now(datetime.timezone.utc)} - Received websocket message with type'
            f' {message_type} with the headers {headers} and body \n{data}.')

        if message_type == 'CONNECTED' and not self.heartbeat_started:
            self.heartbeat_started = True
            session_id = headers.get('session', '')

            delay = int(headers['heart-beat'].split(',')[1]) // 1000
            self.client.loop.create_task(self.send_heartbeat(delay))

            eas_n = str(random.randint(1, 0xffffffff))
            self._eas_public_sub_id = f'sub-eas-{eas_n}'
            self._eas_private_sub_id = f'sub-eas-private-{eas_n}'

            token = self.client.auth.eas_access_token
            destination = (
                f'deploymentId/{self.client.deployment_id}/'
                f'epicAccountId/{self.client.user.id}'
            )
            eas_headers = {
                'authorization': f'Bearer {token}',
                'ec-coord-accept-language': 'en',
            }

            sub_public = (
                'SUBSCRIBE\n'
                f'id:{self._eas_public_sub_id}\n'
                f'destination:{destination}\n'
            )
            if session_id:
                sub_public += f'receipt:sub-0-{session_id}\n'
            for key, value in eas_headers.items():
                sub_public += f'{key}:{value}\n'
            await self.websocket.send_str(sub_public + '\n\x00')

            sub_private = (
                'SUBSCRIBE\n'
                f'id:{self._eas_private_sub_id}\n'
                f'destination:{destination}\n'
                'ec-coord-temporary-subscription:parties-internal\n'
            )
            if session_id:
                sub_private += f'receipt:sub-1-{session_id}\n'
            for key, value in eas_headers.items():
                sub_private += f'{key}:{value}\n'
            await self.websocket.send_str(sub_private + '\n\x00')
        elif (message_type == 'MESSAGE' and 'type' in data
              and data['type'] == 'core.connect.v1.connected'):
            self.connection_id = data['connectionId']

            if self._eas_public_sub_id:
                self.public_connection_id = (
                    f'{self.connection_id}#{self._eas_public_sub_id}'
                )
            if self._eas_private_sub_id:
                self.private_connection_id = (
                    f'{self.connection_id}#{self._eas_private_sub_id}'
                )

            await self.client.send_eos_presence()
            self._ready_event.set()
        elif (
            message_type == 'MESSAGE' and
            isinstance(data.get('type'), str) and
            data['type'].startswith('party.v2.')
        ):
            asyncio.ensure_future(
                self.client.handle_epic_party_notification(
                    data['type'], data.get('payload') or {}
                )
            )
        elif (
            message_type == 'MESSAGE' and
            data.get('type') == 'social.chat.v1.NEW_MESSAGE' and
            data.get('payload').get('conversation').get('type') == 'dm'
        ):
            author = self.client.get_friend(
                data['payload']['message']['senderId']
            )
            if author is None:
                try:
                    author = await self.client.wait_for(
                        'friend_add',
                        check=lambda f: f.id == data['payload']['message']
                        ['senderId'],
                        timeout=2
                    )
                except asyncio.TimeoutError:
                    return

            try:
                decoded_content = decode_message_body(
                    data['payload']['message']['body']
                )
                m = FriendMessage(
                    client=self.client,
                    author=author,
                    content=decoded_content
                )
                self.client.dispatch_event('friend_message', m)
            except ValueError:
                pass
        elif (
            message_type == 'MESSAGE' and
            data.get('type') == 'social.chat.v1.NEW_MESSAGE' and
            data.get('payload', {}).get('conversation', {}).get('type')
            in ('party', 'epic_party')
        ):
            conversation_type = data['payload']['conversation']['type']
            conversation_id = data['payload']['conversation']['conversationId']  # noqa
            user_id = data['payload']['message']['senderId']
            party = self.client.party

            client_party_id = party.id if party is not None else None
            if conversation_type == 'epic_party':
                bare_id = conversation_id[3:] if conversation_id.startswith(
                    'ep-'
                ) else conversation_id
                matches_party = bool(client_party_id) and (
                    client_party_id.startswith(f'{bare_id}-')
                )
            else:
                bare_id = conversation_id[2:] if conversation_id.startswith(
                    'p-'
                ) else conversation_id
                matches_party = client_party_id == bare_id

            if (
                party is None
                or not matches_party
                or user_id == self.client.user.id
                or user_id not in party._members
            ):
                return

            decoded_content = decode_message_body(
                data['payload']['message']['body']
            )
            self.client.dispatch_event('party_message', PartyMessage(
                client=self.client,
                party=party,
                author=party._members[data['payload']['message']['senderId']],
                content=decoded_content
            ))
        elif (
            message_type == 'MESSAGE' and
            data.get('type') == 'presence.v1.UPDATE'
        ):
            user_id = data['payload']['accountId']
            friend = self.client.get_friend(user_id)
            if friend is None:
                try:
                    friend = await self.client.wait_for(
                        'friend_add',
                        check=lambda f: f.id == user_id,
                        timeout=1
                    )
                except asyncio.TimeoutError:
                    return

            before_pres = friend.last_presence

            _pres = Presence(
                self.client,
                data['payload'],
                previous=before_pres,
            )

            # The presence cache only holds friends who are online (see
            # Client.presences), so a friend going offline is pruned rather
            # than stored. Otherwise is_online()/get_presence() would keep
            # reporting them online until restart.
            if _pres.available:
                self.client._presences[user_id] = _pres
            else:
                if before_pres is not None:
                    friend._update_last_logout(
                        datetime.datetime.now(datetime.timezone.utc)
                    )
                self.client._presences.pop(user_id, None)

            if _pres.party is not None and _pres.party.net_cl:
                self.client._note_presence_net_cl(_pres.party.net_cl)

            self.client.dispatch_event('friend_presence', before_pres, _pres)
        elif (
            message_type == 'ERROR' and
            data.get('statusCode') == 4019
        ):
            log.debug('STOMP authentication token is now invalid')
            await self.restart()
        elif (
            message_type == 'MESSAGE' and
            isinstance(data.get('type'), str) and
            ('party' in data['type'].lower() or 'invite' in data['type'].lower())  # noqa
        ):
            asyncio.ensure_future(self.client._epic_party_poll())

    async def connect_to_websocket(self) -> None:
        headers = {
            'Authorization': f'Bearer {self.client.auth.eas_access_token}',
            'Epic-Connect-Protocol': 'stomp',
            "Sec-WebSocket-Protocol": "v10.stomp,v11.stomp,v12.stomp",
            'Epic-Connect-Device-Id': " ",
        }
        async with self.wss_session.ws_connect(
            "wss://connect.epicgames.dev/v2",
            protocols=['stomp'],
            headers=headers
        ) as websocket:
            self.websocket = websocket
            connect_frame = (
                "CONNECT\n"
                "accept-version:1.0,1.1,1.2\n"
                "heart-beat:30000,0\n"
                f"authorization:Bearer {self.client.auth.eas_access_token}\n"
                "\n\x00"
            )
            await websocket.send_str(connect_frame)

            async for msg in websocket:
                await self.parse_message(msg.data.decode())

    async def run(self) -> None:
        log.debug('Starting STOMP websocket client')
        self._ready_event.clear()
        await self.set_session()
        self.ws_task = self.client.loop.create_task(
            self.connect_to_websocket()
        )

        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            raise STOMPError(
                'Timed out connecting to connect to STOMP'
            )

    async def close(self) -> None:
        log.debug('Closing STOMP websocket client')
        await self.websocket.close()
        await self.wss_session.close()

        self.heartbeat_started = False
        self.connection_id = None
        self.public_connection_id = None
        self.private_connection_id = None
        self._eas_public_sub_id = None
        self._eas_private_sub_id = None
        self._ready_event.clear()

    async def restart(self) -> None:
        log.debug('Restarting STOMP websocket client')
        await self.close()

        if self.ws_task:
            self.ws_task.cancel()
            try:
                await self.ws_task
            except asyncio.CancelledError:
                pass
            self.ws_task = None

        await self.run()
        await self.client.rebind_epic_party_connection()
