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
from typing import Optional

from .message import FriendMessage, PartyMessage
from .presence import Presence
from .errors import STOMPError, HTTPException
from .utils import reconnect_delay, describe_connect_error, json_loads, spawn

from aiohttp import hdrs, helpers, client_reqrep, connector
from aiohttp.http import StreamWriter, HttpVersion10, HttpVersion11

log = logging.getLogger(__name__)

STOMP_URL = 'wss://connect.epicgames.dev/v2'

# How long Epic has to confirm a new connection (core.connect.v1.connected).
CONNECT_TIMEOUT = 15

# The heart-beat we offer in CONNECT, in milliseconds.
HEARTBEAT_MS = 30000


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
        self._heartbeat_task = None
        self._ready_event = asyncio.Event()

        # One connect or close at a time; the auth refresh and the background
        # reconnect can otherwise tear down each other's fresh connection.
        self._connect_lock = asyncio.Lock()
        self._reconnect_task = None
        self._last_error = None

        self.connection_id = None
        self.public_connection_id = None
        self.private_connection_id = None
        self._eas_public_sub_id = None
        self._eas_private_sub_id = None

        self.message_history = deque(maxlen=100)
        self._seen_message_ids = set()
        self._late_tasks = set()

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

    async def send_heartbeat(self, delay: float,
                             websocket: Optional[aiohttp.ClientWebSocketResponse] = None  # noqa
                             ) -> None:
        # Bound to one connection: reading self.websocket here would carry the
        # loop over to the next connection after a reconnect, and every
        # reconnect would leave one more heartbeat loop behind.
        websocket = websocket or self.websocket
        try:
            while not websocket.closed:
                await websocket.send_str("\n")
                await asyncio.sleep(delay)
        except (ConnectionError, aiohttp.ClientError):
            pass  # the connection is going away; its reader handles that

    async def parse_message(self, raw: str) -> None:
        raw_headers, raw_json = raw.split('\n\n', 1)
        header_lines = raw_headers.splitlines()
        message_type = header_lines[0]

        headers = {}
        for line in header_lines[1:]:
            key, value = line.split(':', 1)
            headers[key.strip()] = value.strip()

        data = json_loads(raw_json[:-1]) if len(raw_json) >= 3 else {}

        if message_type == 'MESSAGE':
            message_id = data.get('id')
            if message_id:
                if message_id in self._seen_message_ids:
                    return
                history = self.message_history
                if len(history) == history.maxlen:
                    self._seen_message_ids.discard(history[0])
                history.append(message_id)
                self._seen_message_ids.add(message_id)

        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                '%s - Received websocket message with type %s with the '
                'headers %s and body \n%s.',
                datetime.datetime.now(datetime.timezone.utc),
                message_type, headers, data,
            )

        if message_type == 'CONNECTED' and not self.heartbeat_started:
            self.heartbeat_started = True
            session_id = headers.get('session', '')

            # STOMP: we send at max(what we offered, what the server wants).
            # A server value of 0 used to mean a busy loop of heartbeats.
            try:
                wanted = int(headers['heart-beat'].split(',')[1])
            except (KeyError, IndexError, ValueError):
                wanted = 0
            delay = max(HEARTBEAT_MS, wanted) / 1000
            self._heartbeat_task = self.client.loop.create_task(
                self.send_heartbeat(delay, self.websocket)
            )

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
            spawn(
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
                sender_id = data['payload']['message']['senderId']
                waiter = self.client.wait_for(
                    'friend_add',
                    check=lambda f: f.id == sender_id,
                    timeout=2
                )
                self._run_late(self._friend_message_later(waiter, data))
            else:
                self._dispatch_friend_message(author, data)
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
                waiter = self.client.wait_for(
                    'friend_add',
                    check=lambda f: f.id == user_id,
                    timeout=1
                )
                self._run_late(self._presence_later(waiter, data))
            else:
                self._apply_presence(friend, data)
        elif message_type == 'ERROR':
            # Kept so that a connection Epic refuses reports why instead of
            # just timing out.
            status = data.get('statusCode') if isinstance(data, dict) else None
            text = (
                (data.get('message') if isinstance(data, dict) else None)
                or headers.get('message')
                or 'no message'
            )
            self._last_error = f'STOMP error {status}: {text}'
            if status == 4019:
                log.debug('STOMP authentication token is now invalid')
                # The connection is unusable even if Epic keeps it open. Not
                # awaited: this runs inside the connection's own reader,
                # which the reconnect has to cancel.
                self._ready_event.clear()
                self.schedule_reconnect('authentication token invalid')
            else:
                log.warning(f'[{self._who()}] {self._last_error}')
        elif (
            message_type == 'MESSAGE' and
            isinstance(data.get('type'), str) and
            ('party' in data['type'].lower() or 'invite' in data['type'].lower())  # noqa
        ):
            spawn(self.client._epic_party_poll())

    def _run_late(self, coro) -> None:
        task = self.client.loop.create_task(coro)
        self._late_tasks.add(task)
        task.add_done_callback(self._late_tasks.discard)

    def _dispatch_friend_message(self, author, data: dict) -> None:
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

    async def _friend_message_later(self, waiter, data: dict) -> None:
        try:
            author = await waiter
        except asyncio.TimeoutError:
            return
        self._dispatch_friend_message(author, data)

    async def _presence_later(self, waiter, data: dict) -> None:
        try:
            friend = await waiter
        except asyncio.TimeoutError:
            return
        self._apply_presence(friend, data)

    def _apply_presence(self, friend, data: dict) -> None:
        user_id = data['payload']['accountId']
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

    async def connect_to_websocket(self) -> None:
        headers = {
            'Authorization': f'Bearer {self.client.auth.eas_access_token}',
            'Epic-Connect-Protocol': 'stomp',
            "Sec-WebSocket-Protocol": "v10.stomp,v11.stomp,v12.stomp",
            'Epic-Connect-Device-Id': " ",
        }
        async with self.wss_session.ws_connect(
            STOMP_URL,
            protocols=['stomp'],
            headers=headers
        ) as websocket:
            self.websocket = websocket
            connect_frame = (
                "CONNECT\n"
                "accept-version:1.0,1.1,1.2\n"
                f"heart-beat:{HEARTBEAT_MS},0\n"
                f"authorization:Bearer {self.client.auth.eas_access_token}\n"
                "\n\x00"
            )
            await websocket.send_str(connect_frame)

            async for msg in websocket:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    raw = msg.data.decode()
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    raw = msg.data
                else:
                    break  # ERROR: the connection is unusable

                if not raw.strip('\n\r\x00'):
                    continue  # a heartbeat from the server

                # One frame we fail to handle must not end the connection:
                # the reader would die silently and the client go deaf.
                try:
                    await self.parse_message(raw)
                except Exception:
                    log.exception('Failed to handle a STOMP frame')

    @property
    def connected(self) -> bool:
        """:class:`bool`: Whether the STOMP connection is up and confirmed."""
        return (
            self._ready_event.is_set()
            and self.ws_task is not None
            and not self.ws_task.done()
        )

    def _who(self) -> str:
        user = getattr(self.client, 'user', None)
        return getattr(user, 'display_name', None) or 'unknown account'

    def _client_closing(self) -> bool:
        return bool(
            getattr(self.client, '_closing', False)
            or self.client.is_closed()
        )

    async def run(self) -> None:
        """Opens a new STOMP connection, replacing the current one, and waits
        until Epic confirms it.

        Raises
        ------
        STOMPError
            Epic refused the connection, closed it, or did not confirm it in
            time. The message says which; nothing is left half open.
        """
        async with self._connect_lock:
            await self._open()

    async def connect(self, attempts: int = 3) -> None:
        """Like :meth:`run`, but retries a failed connection, up to
        ``attempts`` tries in all, with backoff.
        """
        for attempt in range(attempts):
            try:
                return await self.run()
            except STOMPError as exc:
                if attempt + 1 >= attempts:
                    raise
                delay = reconnect_delay(attempt, base=3, cap=30)
                log.debug(f'{exc}; retrying in {delay:.0f}s')
                await asyncio.sleep(delay)

    async def _open(self) -> None:
        log.debug('Starting STOMP websocket client')
        await self._teardown()

        self._last_error = None
        await self.set_session()
        task = self.ws_task = self.client.loop.create_task(
            self.connect_to_websocket()
        )
        task.add_done_callback(self._on_reader_done)

        ready = asyncio.ensure_future(self._ready_event.wait())
        try:
            done, _ = await asyncio.wait(
                (ready, task),
                timeout=CONNECT_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            ready.cancel()
            await self._teardown()
            raise

        if ready in done:
            return

        ready.cancel()
        cause = None
        if task.done():
            if not task.cancelled():
                cause = task.exception()
            reason = (
                describe_connect_error(cause)
                or self._last_error
                or 'closed before it was ready'
            )
        else:
            reason = f'not confirmed within {CONNECT_TIMEOUT}s'
            if self._last_error:
                reason += f' ({self._last_error})'

        await self._teardown()
        # No "from cause": the message already says why, and a chained
        # aiohttp traceback adds nothing but noise.
        raise STOMPError(f'Could not connect to STOMP: {reason}')

    def _on_reader_done(self, task: asyncio.Task) -> None:
        # Always retrieve the result, so a failed connection never surfaces
        # as "Task exception was never retrieved" (whose repr of an aiohttp
        # error prints the request headers, bearer token included).
        exc = None if task.cancelled() else task.exception()

        # A connection we replaced or closed ourselves is no longer
        # self.ws_task; one that failed before it was ready is reported by
        # _open. Only a confirmed connection lost on its own is handled here.
        if task is not self.ws_task or not self._ready_event.is_set():
            return

        self._ready_event.clear()
        reason = (
            describe_connect_error(exc)
            or self._last_error
            or 'closed by the server'
        )
        self.schedule_reconnect(reason)

    def schedule_reconnect(self, reason: str) -> None:
        """Reconnects in the background, retrying with backoff until it works
        or the client closes, then reattaches the party to the new
        connection. Does nothing if a reconnect is already running.
        """
        if self._client_closing():
            return
        task = self._reconnect_task
        if task is not None and not task.done():
            return

        self._reconnect_task = self.client.loop.create_task(
            self._reconnect_loop(reason)
        )

    async def _reconnect_loop(self, reason: str) -> None:
        log.warning(
            f'[{self._who()}] STOMP connection lost ({reason}); reconnecting'
        )
        attempt = 0
        while not self._client_closing():
            await asyncio.sleep(reconnect_delay(attempt))
            if self._client_closing():
                return
            if self.connected:
                return  # an auth refresh reconnected meanwhile

            try:
                await self.run()
            except STOMPError as exc:
                attempt += 1
                log.debug(f'STOMP reconnect attempt {attempt} failed: {exc}')
                continue

            log.warning(
                f'[{self._who()}] STOMP reconnected after '
                f'{attempt + 1} attempt(s)'
            )
            try:
                await self.client._restore_party_connection()
            except Exception:
                log.exception(
                    'Could not restore the party after a STOMP reconnect'
                )
            return

    async def _teardown(self) -> None:
        """Drops the current connection with its reader and heartbeat."""
        current = asyncio.current_task()
        reader, self.ws_task = self.ws_task, None
        heartbeat, self._heartbeat_task = self._heartbeat_task, None
        websocket, self.websocket = self.websocket, None
        session, self.wss_session = self.wss_session, None

        self.heartbeat_started = False
        self.connection_id = None
        self.public_connection_id = None
        self.private_connection_id = None
        self._eas_public_sub_id = None
        self._eas_private_sub_id = None
        self._ready_event.clear()

        tasks = [t for t in (heartbeat, reader)
                 if t is not None and t is not current]
        for t in tasks:
            t.cancel()

        if websocket is not None and not websocket.closed:
            try:
                await asyncio.wait_for(websocket.close(), timeout=2)
            except Exception:
                pass

        # Cancelled or failed, they are done with. If *we* get cancelled
        # meanwhile, gather still propagates it.
        await asyncio.gather(*tasks, return_exceptions=True)

        if session is not None and not session.closed:
            await session.close()

    async def close(self) -> None:
        log.debug('Closing STOMP websocket client')
        task, self._reconnect_task = self._reconnect_task, None
        if (task is not None and not task.done()
                and task is not asyncio.current_task()):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        async with self._connect_lock:
            await self._teardown()

    async def restart(self) -> None:
        log.debug('Restarting STOMP websocket client')
        if self.ws_task is not None and self.ws_task is asyncio.current_task():
            # Called from a frame handler: the reader can't replace itself.
            self._ready_event.clear()
            self.schedule_reconnect('restart requested')
            return

        await self.run()
        await self.client.rebind_epic_party_connection()
