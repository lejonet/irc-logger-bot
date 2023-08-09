#!/usr/bin/env python

import asyncio, signal, logging
from sys import exit
from typing import List, Tuple, Dict, Optional, Optional, Optional, Optional
from time import time

import toml
import redis.asyncio as redis
from psycopg import AsyncConnection
from psycopg.sql import SQL, Identifier, Placeholder
from irctokens import build, Line
from ircrobots import Bot as BaseBot
from ircrobots import Server as BaseServer
from ircrobots import ConnectionParams
from ircrobots.transport import TCPTransport
from ircrobots.interface import ITCPTransport
from ircrobots.security import TLS_NOVERIFY, TLS_VERIFYCHAIN

logger = logging.getLogger("ircbot")
logger.setLevel(logging.DEBUG)

ch = logging.StreamHandler()
chfmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setLevel(logging.DEBUG)
ch.setFormatter(chfmt)
logger.addHandler(ch)

fh = logging.FileHandler("ircbot.log")
fhfmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
fh.setLevel(logging.INFO)
fh.setFormatter(fhfmt)
logger.addHandler(fh)

CHANNEL_LIST = 0
REDIS_CONF   = 1
DB_CONF      = 2

class Bot(BaseBot):
    irc_servers: List[Tuple]
    logger: logging.Logger

    def __init__(self, config) -> None:
        super().__init__()

        self.irc_servers  = config["servers"]

        self.logger = logging.getLogger("ircbot.bot")

    async def add_server(self, name: str, params: ConnectionParams, config: Tuple[List[str], str, str], transport: ITCPTransport = TCPTransport()) -> BaseServer:
        server = Server(self, name, config, params.nickname)
        self.servers[name] = server
        await server.connect(transport, params)
        await self._server_queue.put(server)
        return server

    async def add_all_servers(self) -> None:
        self.logger.info("Add all servers")
        for server in self.irc_servers:
            name, nick, host, port, tls, password, config = server
            self.logger.info(f"Server {name} with params({nick},{host},{port},{tls})")
            params = ConnectionParams(nickname = nick, host = host, port = port, tls = tls, password = password)
            await self.add_server(name, params, config)

        self.logger.info("Finished adding all servers")

    async def disconnect_all(self) -> None:
        for name, server in self.servers.items():
            self.logger.info(f"Disconnecting from server {name}")
            await server.disconnect()

class Server(BaseServer):
    logger: logging.Logger
    redis_conf: str
    db_conf: str
    channel_list: List[str]
    db_connection: Optional[AsyncConnection[any]]
    redis_connection: Optional[redis.client.Redis]
    def __init__(self, bot: BaseBot, name: str, config: Tuple[List[str], str, str], nickname: str) -> None:
        super().__init__(bot, name)

        self.bot_nick     = nickname
        self.logger       = logging.getLogger(f"ircbot.bot.{self.name}")
        self.channel_list = config[CHANNEL_LIST]
        self.redis_conf   = config[REDIS_CONF]
        self.db_conf      = config[DB_CONF]

        self.db_connection    = None
        self.redis_connection = None
        self.userlists        = dict()

    def _split_nick(self, nick: str) -> Tuple[str, str]:
        if "!" not in nick:
            return nick, ""

        tmp = nick.split("!")

        return tmp[0], tmp[1]

    def _parse_modeline(self, line_params: list) -> List[Tuple[str, str, str]]:
        modes = list()
        index = 0

        target = line_params[0]
        mode_line = line_params[1]
        nr_nicks = len(line_params[2:])
        nicks = line_params[2:] if nr_nicks != 0 else [target]
        mode_params = list(mode_line)
        for char in mode_params:
            if char in ['+', '-']:
                modifier = char
                continue

            match modifier:
                case '+':
                    match char:
                        case 'b':
                            op = "ban"
                        case 'q':
                            op = "quiet"
                        case 'o':
                            op = "op"
                        case _:
                            op = "mode"
                case '-':
                    match char:
                        case 'b':
                            op = "unban"
                        case 'q':
                            op = "unquiet"
                        case 'o':
                            op = "deop"
                        case _:
                            op = "mode"

            tup = (op, nicks[index], f"{modifier}{char}")
            self.logger.debug(f"tup: {tup}")
            modes.append(tup)

            if index != nr_nicks:
                index += 1

        self.logger.debug(f"modes: {modes}")
        return modes

    async def _persist_msg(self, message: Dict[str, str]) -> None:
        if self.db_connection is None:
            self.db_connection = await AsyncConnection.connect(self.db_conf)
        if self.redis_connection is None:
            self.redis_connection = await redis.from_url(self.redis_conf)
            async with self.redis_connection.pubsub() as pubsub:
                await pubsub.subscribe(f"message.{self.name}")

        keys = list(message)
        fields = SQL(", ").join(map(Identifier, keys))
        values = SQL(', ').join(map(Placeholder, keys))

        query = SQL("INSERT INTO irclog ({fields}) VALUES ({values}) RETURNING id").format(**{"fields": fields, "values": values})
        self.logger.debug(query.as_string(self.db_connection))
        async with self.db_connection.cursor() as cur:
            self.logger.debug(f"Cursor: {cur} Connection: {self.db_connection}")
            await cur.execute(query, message)
            row_id = await cur.fetchone()
            self.logger.debug(f"Cursor: {cur} Connection: {self.db_connection} Row id: {row_id}")
            await self.db_connection.commit()

        await self.redis_connection.publish(f"message.{self.name}", f"{message['channel']} {row_id[0]}")

    async def line_read(self, line: Line) -> None:
        self.logger.debug(f"{self.name} < {line.format()}")

        message = dict([
            ("channel", line.params[0]),
            ("timestamp", int(time()))
        ])
        if line.command in ["PRIVMSG", "JOIN", "PART", "KICK", "MODE", "TOPIC", "NICK"]:
            nick, fullname = self._split_nick(line.source)
            message["nick"] = nick

        constructed_line = None
        match line.command:
            case "001":
                self.logger.info(f"connected to {self.name} ({self.isupport.network})")
                for channel in self.channel_list:
                    await self.send(build("JOIN", [channel]))
            case "353":
                self.logger.debug(f"Names: {line}")
                def remove_modes(string):
                    return string.removeprefix("@").removeprefix("+").removeprefix("%").removeprefix("~")

                channel = line.params[2]
                userlist = map(remove_modes, line.params[3].split(" "))
                self.userlists[channel] = set(userlist)
                self.logger.debug(f"Userlists: {self.userlists}")
            case "PRIVMSG":
                if len(line.params) == 2: # If the length of params is 2, its a channel message
                    channel, msg = line.params
                    message["opcode"] = "msg"
                else:
                    self.logger.debug(f"Got a direct message: {line.params}")
                    msg = None

                if msg is not None and msg.startswith('\x01ACTION'):
                    msg = msg.split("ACTION ")[1].removesuffix('\x01')
                    message["nick"] = nick
                    message["opcode"] = "action"

                constructed_line = msg
            case "JOIN":
                channel = line.params[0]
                constructed_line = f"{nick} has joined {channel}"
                message["opcode"] = "join"
            case "PART":
                try:
                    channel, part_msg = line.params
                except ValueError:
                    channel = line.params[0]
                    part_msg = ""
                constructed_line = f"{nick} has left {channel} [{part_msg}]"
                message["opcode"] = "leave"
                message["payload"] = part_msg
            case "KICK":
                channel, kicked_nick, msg = line.params
                constructed_line = f"{kicked_nick} was kicked from {channel} by {nick} [{msg}]"
                message["opcode"] = "kick"
                message["nick"] = kicked_nick
                message["oper_nick"] = nick
                message["payload"] = msg
            case "TOPIC":
                channel, topic = line.params
                constructed_line = f"{nick} changed the topic of {channel} to: {topic}"
                message["opcode"] = "topic"
                message["payload"] = topic
            case "NICK":
                new_nick = line.params[0]
                constructed_line = f"{nick} is now known as {new_nick}"
                message["opcode"] = "nick"
                message["payload"] = new_nick
            case "MODE":
                self.logger.debug(line.params)
                channel = line.params[0]
                modes = self._parse_modeline(line.params)
                oper_nick = nick
                message["opcode"] = "mode"
                constructed_line = ""

        if constructed_line is not None:
            if "opcode" in message:
                match message["opcode"]:
                    case "mode":
                        for tup in modes:
                            opcode, nick, mode_payload = tup
                            only_nick, account_hostmask = self._split_nick(nick)
                            match opcode:
                                case "ban"|"unban":
                                    action = f"{opcode}ned {only_nick} in"
                                case "quiet":
                                    action = f"quieted {only_nick} in"
                                case "unquiet":
                                    action = f"allowed {only_nick} to speak again in"
                                case "op":
                                    action = f"made {only_nick} operator in"
                                case "deop":
                                    action = f"removed {only_nick} as operator in"
                                case _:
                                    action = f"{mode_payload}"
                                    if nick != channel:
                                        action += f" {nick} in"

                            constructed_line = f"{oper_nick} {action} {channel}"
                            if opcode in ['ban', 'unban', 'quiet', 'unquiet']:
                                constructed_line += " [{nick}]"

                            message["oper_nick"] = oper_nick
                            message["nick"] = only_nick
                            message["line"] = constructed_line
                            message["opcode"] = opcode
                            message["payload"] = nick if opcode in ['ban', 'unban', 'quiet', 'unquiet'] else mode_payload
                            self.logger.info(constructed_line)
                            await self._persist_msg(message)
                        return
                    case "part" | "kick":
                        self.logger.debug(self.userlists[channel])
                        self.userlists[channel] -= set([nick])
                        self.logger.debug(self.userlists[channel])
                    case "join":
                        if nick != self.bot_nick:
                            self.logger.debug(self.userlists[channel])
                            self.userlists[channel] |= set([nick])
                            self.logger.debug(self.userlists[channel])
                    case "nick":
                        for channel, userlist in self.userlists.items():
                            if nick in userlist:
                                message["channel"] = channel
                                message["line"] = constructed_line
                                self.logger.debug(self.userlists[channel])
                                self.userlists[channel] -= set([message["nick"]])
                                self.logger.debug(self.userlists[channel])
                                self.userlists[channel] |= set([message["payload"]])
                                self.logger.debug(self.userlists[channel])

                                self.logger.info(constructed_line)
                                await self._persist_msg(message)
                        return

            message["line"] = constructed_line
            self.logger.info(constructed_line)
            await self._persist_msg(message)

        self.logger.debug(message)

    async def line_send(self, line: Line) -> None:
        self.logger.info(f"{self.name} > {line.format()}")

    async def disconnect(self) -> None:
        super().disconnect()

        if self.db_connection is not None:
            await self.db_connection.close()

        if self.redis_connection is not None:
            await self.redis_connection.close()

def convert_config(config) -> None:
    tmp = list()

    for name, params in config["servers"].items():
        port = 6667 if not "port" in params else params["port"]
        try:
            match params["tls_verify"]:
                case False:
                    tls_verify = TLS_NOVERIFY
                case True:
                    tls_verify = TLS_VERIFYCHAIN
        except:
            tls_verify = TLS_VERIFYCHAIN

        channel_list = params["channel_list"].split(",")
        password = params["password"] if "password" in params else None
        srv_config = tuple([channel_list, config["redis_config"], config["db_config"]])
        srv_tuple = tuple([name, params["nick"], params["host"], port, tls_verify, password, srv_config])
        tmp.append(srv_tuple)
    config["servers"] = tmp

async def main() -> None:
    loop = asyncio.get_running_loop()

    async def shutdown(loop: asyncio.SelectorEventLoop) -> None:
        tasks = list()

        for task in asyncio.all_tasks(loop):
            if task is not asyncio.current_task(loop):
                task.cancel()
                tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(f"Finished awaiting cancelled tasks, results: {results}")
        loop.stop()
        exit(0)

    for sig in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(loop)))

    logger.info("Loading configuration")
    config = toml.load("bot.conf")
    logger.info(f"Config:\n{config}")
    convert_config(config)
    logger.info(f"Parsed config:\n{config}")
    logger.info("Creating bot instance")
    bot = Bot(config)
    logger.info("Adding all servers from the config")
    await bot.add_all_servers()
    try:
        await bot.run()
    except asyncio.CancelledError as e:
        self.logger.info("Got cancel signal, shutting down...")
        await bot.disconnect_all()
        raise e

if __name__ == "__main__":
    asyncio.run(main())
