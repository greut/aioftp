import asyncio
import pathlib
import functools
import contextlib
import inspect


from . import common
from . import errors
from . import pathio


def add_prefix(message):

    return str.format("aioftp server: {}", message)


class Permission:

    def __init__(self, path="/", *, readable=True, writable=True):

        self.path = pathlib.Path(path)
        self.readable = readable
        self.writable = writable

    def is_parent(self, other):

        try:

            other.relative_to(self.path)
            return True

        except ValueError:

            return False

    def __repr__(self):

        return str.format(
            "Permission({!r}, readable={!r}, writable={!r})",
            self.path,
            self.readable,
            self.writable,
        )


class User:

    def __init__(self, login=None, password=None, *,
                 base_path=pathlib.Path("."), home_path=pathlib.Path("/"),
                 permissions=None):

        self.login = login
        self.password = password
        self.base_path = pathlib.Path(base_path)
        self.home_path = pathlib.Path(home_path)
        self.permissions = permissions or [Permission()]

    def get_permissions(self, path):

        path = pathlib.Path(path)
        parents = filter(lambda p: p.is_parent(path), self.permissions)
        perm = min(
            parents,
            key=lambda p: len(path.relative_to(p.path).parts),
            default=Permission(),
        )
        return perm

    def __repr__(self):

        return str.format(
            "User({!r}, {!r}, base_path={!r}, home_path={!r}, "
            "permissions={!r})",
            self.login,
            self.password,
            self.base_path,
            self.home_path,
            self.permissions,
        )


class BaseServer:

    @asyncio.coroutine
    def start(self, host=None, port=0, **kw):

        self.connections = {}
        self.host = host
        self.port = port
        self.server = yield from asyncio.start_server(
            self.dispatcher,
            host,
            port,
            loop=self.loop,
            **kw
        )
        host, port = self.server.sockets[0].getsockname()
        message = str.format("serving on {}:{}", host, port)
        common.logger.info(add_prefix(message))

    def close(self):

        self.server.close()

    @asyncio.coroutine
    def wait_closed(self):

        yield from self.server.wait_closed()

    @asyncio.coroutine
    def write_line(self, reader, writer, line, encoding="utf-8"):

        common.logger.info(add_prefix(line))
        writer.write(str.encode(line + "\r\n", encoding=encoding))
        yield from writer.drain()

    @asyncio.coroutine
    def write_response(self, reader, writer, code, lines="", list=False):

        lines = common.wrap_with_container(lines)
        write = functools.partial(self.write_line, reader, writer)
        if list:

            head, *body, tail = lines
            yield from write(code + "-" + head)
            for line in body:

                yield from write(" " + line)

            yield from write(code + " " + tail)

        else:

            *body, tail = lines
            for line in body:

                yield from write(code + "-" + line)

            yield from write(code + " " + tail)

    @asyncio.coroutine
    def parse_command(self, reader, writer):

        line = yield from asyncio.wait_for(reader.readline(), self.timeout)
        if not line:

            raise errors.ConnectionClosedError()

        s = str.rstrip(bytes.decode(line, encoding="utf-8"))
        common.logger.info(add_prefix(s))
        cmd, _, rest = str.partition(s, " ")
        return str.lower(cmd), rest

    @asyncio.coroutine
    def dispatcher(self, reader, writer):

        host, port = writer.transport.get_extra_info("peername", ("", ""))
        message = str.format("new connection from {}:{}", host, port)
        common.logger.info(add_prefix(message))

        key = reader, writer
        connection = {
            "client_host": host,
            "client_port": port,
            "server_host": self.host,
            "server_port": self.port,
            "command_connection": (reader, writer),
            "timeout": self.timeout,
            "path_io": self.path_io,
            "loop": self.loop,
        }
        self.connections[key] = connection
        write = functools.partial(self.write_response, reader, writer)

        try:

            ok, *args = yield from self.greeting(connection, "")
            yield from write(*args)

            while ok:

                cmd, rest = yield from self.parse_command(reader, writer)
                if cmd == "pass":

                    # is there a better solution?
                    cmd = "pass_"

                if hasattr(self, cmd):

                    ok, *args = yield from getattr(self, cmd)(connection, rest)
                    yield from write(*args)

                else:

                    template = "'{}' not implemented"
                    yield from write("502", str.format(template, cmd))

        finally:

            writer.close()
            self.connections.pop(key)

    @asyncio.coroutine
    def greeting(self, connection, rest):

        raise NotImplementedError


def fields_required(*fields):

    def decorator(f):

        @functools.wraps(f)
        def wrapper(self, connection, rest, **kw):

            for name, message in fields:

                if name not in connection:

                    template = "bad sequence of commands ({})"
                    return True, "503", str.format(template, message)

            return f(self, connection, rest, **kw)

        return wrapper

    return decorator


def unpack_keywords(f):

    @functools.wraps(f)
    def wrapper(self, connection, rest):

        sig = inspect.signature(f)
        unpacked = {}
        for name, parameter in sig.parameters.items():

            if parameter.kind == inspect.Parameter.KEYWORD_ONLY:

                unpacked[name] = connection[name]

        return f(self, connection, rest, **unpacked)

    return wrapper


user_required = fields_required(
    ("user", "no user (use USER firstly)"),
)
login_required = fields_required(
    ("logged", "not logged in"),
)
passive_required = fields_required(
    ("passive_server", "no listen socket created (use PASV firstly)"),
    ("passive_connection", "no passive connection created (connect firstly)"),
)


class Server(BaseServer):

    path_facts = (
        ("st_size", "Size"),
        ("st_mtime", "Modify"),
        ("st_ctime", "Create"),
    )

    def __init__(self, users=None, loop=None, *, timeout=None,
                 path_io_factory=pathio.AsyncPathIO):

        self.users = users or [User()]
        self.loop = loop or asyncio.get_event_loop()
        self.timeout = timeout
        self.path_io = path_io_factory(loop)

    @unpack_keywords
    def get_paths(self, connection, path, *, user):

        virtual_path = pathlib.Path(path)
        if not virtual_path.is_absolute():

            virtual_path = connection["current_directory"] / virtual_path

        real_path = user.base_path / virtual_path.relative_to("/")
        return real_path, virtual_path

    @asyncio.coroutine
    def greeting(self, connection, rest):

        return True, "220", "welcome"

    @asyncio.coroutine
    def user(self, connection, rest):

        current_user = None
        for user in self.users:

            if user.login is None and current_user is None:

                current_user = user

            elif user.login == rest:

                current_user = user
                break

        if current_user is None:

            code, info = "530", "no such username"
            ok = False

        elif current_user.login is None:

            connection["logged"] = True
            connection["current_directory"] = current_user.home_path
            connection["user"] = current_user
            code, info = "230", "anonymous login"
            ok = True

        else:

            connection["user"] = current_user
            code, info = "331", "require password"
            ok = True

        return ok, code, info

    @asyncio.coroutine
    @user_required
    @unpack_keywords
    def pass_(self, connection, rest, *, user):

        if user.password == rest:

            connection["logged"] = True
            connection["current_directory"] = user.home_path
            connection["user"] = user
            code, info = "230", "normal login"

        else:

            code, info = "530", "wrong password"

        return True, code, info

    @asyncio.coroutine
    def quit(self, connection, rest):

        return False, "221", "bye"

    @asyncio.coroutine
    @login_required
    @unpack_keywords
    def pwd(self, connection, rest, *, current_directory):

        return True, "257", str.format("\"{}\"", current_directory)

    @asyncio.coroutine
    @login_required
    @unpack_keywords
    def cwd(self, connection, rest, *, user, path_io):

        real_path, virtual_path = self.get_paths(connection, rest)
        if not (yield from path_io.exists(real_path)):

            code, info = "550", "path does not exists"

        elif not (yield from path_io.is_dir(real_path)):

            code, info = "550", "path is not a directory"

        else:

            permissions = user.get_permissions(virtual_path)
            if permissions.readable:

                connection["current_directory"] = virtual_path
                code, info = "250", ""

            else:

                code, info = "550", "permission denied"

        return True, code, info

    @asyncio.coroutine
    @login_required
    @unpack_keywords
    def cdup(self, connection, rest, *, current_directory):

        return (yield from self.cwd(connection, current_directory.parent))

    @asyncio.coroutine
    @login_required
    @unpack_keywords
    def mkd(self, connection, rest, *, user, path_io):

        real_path, virtual_path = self.get_paths(connection, rest)
        if (yield from path_io.exists(real_path)):

            code, info = "550", "path already exists"

        else:

            permissions = user.get_permissions(virtual_path)
            if permissions.writable:

                yield from path_io.mkdir(real_path, parents=True)
                code, info = "257", ""

            else:

                code, info = "550", "permission denied"

        return True, code, info

    @asyncio.coroutine
    @login_required
    @unpack_keywords
    def rmd(self, connection, rest, *, user, path_io):

        real_path, virtual_path = self.get_paths(connection, rest)
        if not (yield from path_io.exists(real_path)):

            code, info = "550", "path does not exists"

        elif not (yield from path_io.is_dir(real_path)):

            code, info = "550", "path is not a directory"

        else:

            permissions = user.get_permissions(virtual_path)
            if permissions.writable:

                try:

                    yield from path_io.rmdir(real_path)
                    code, info = "257", ""

                except OSError as e:

                    code, info = "550", str.format("os error: {}", e.strerror)

            else:

                code, info = "550", "permission denied"

    @asyncio.coroutine
    @unpack_keywords
    def build_mlsx_string(self, connection, path, *, path_io):

        stats = {}
        if (yield from path_io.is_file(path)):

            stats["Type"] = "file"

        elif (yield from path_io.is_dir(path)):

            stats["Type"] = "dir"

        else:

            raise errors.UnknownPathType(str(path))

        raw_stats = yield from path_io.stat(path)
        for attr, fact in Server.path_facts:

            stats[fact] = getattr(raw_stats, attr)

        s = ""
        for fact, value in stats.items():

            s += str.format("{}={};", fact, value)

        s += " " + path.name
        return s

    @asyncio.coroutine
    @login_required
    @passive_required
    @unpack_keywords
    def mlsd(self, connection, rest, *, user, path_io):

        @asyncio.coroutine
        def mlsd_writer():

            data_reader, data_writer = connection.pop("passive_connection")
            with contextlib.closing(data_writer) as data_writer:

                for path in (yield from path_io.list(real_path)):

                    s = yield from self.build_mlsx_string(connection, path)
                    data_writer.write(str.encode(s + "\n", "utf-8"))
                    yield from data_writer.drain()

            reader, writer = connection["command_connection"]
            code, info = "200", "mlsd data transer done"
            yield from self.write_response(reader, writer, code, info)

        real_path, virtual_path = self.get_paths(connection, rest)
        permissions = user.get_permissions(virtual_path)
        if permissions.readable:

            # ensure_future
            asyncio.async(mlsd_writer(), loop=connection["loop"])
            code, info = "150", "mlsd transer started"

        else:

            code, info = "550", "permission denied"

        return True, code, info

    @asyncio.coroutine
    @login_required
    @unpack_keywords
    def mlst(self, connection, rest, *, user, path_io):

        real_path, virtual_path = self.get_paths(connection, rest)
        permissions = user.get_permissions(virtual_path)
        if permissions.readable:

            s = yield from self.build_mlsx_string(connection, real_path)
            code, info, list = "250", ["start", s, "end"], True

        else:

            code, info, list = "550", "permission denied", False

        return True, code, info, list

    @asyncio.coroutine
    @login_required
    def rnfr(self, connection, rest):

        pass

    @asyncio.coroutine
    @login_required
    def rnto(self, connection, rest):

        pass

    @asyncio.coroutine
    @login_required
    def dele(self, connection, rest):

        pass

    @asyncio.coroutine
    @login_required
    def stor(self, connection, rest):

        pass

    @asyncio.coroutine
    @login_required
    def retr(self, connection, rest):

        pass

    @asyncio.coroutine
    @login_required
    def type(self, connection, rest):

        if rest == "I":

            connection["transfer_type"] = rest
            code, info = "200", ""

        else:

            code, info = "502", str.format("type '{}' not implemented", rest)

        return True, code, info

    @asyncio.coroutine
    @login_required
    def pasv(self, connection, rest):

        @asyncio.coroutine
        def handler(reader, writer):

            if "passive_connection" in connection:

                writer.close()

            else:

                connection["passive_connection"] = reader, writer

        if "passive_server" not in connection:

            connection["passive_server"] = yield from asyncio.start_server(
                handler,
                connection["server_host"],
                0,
                loop=self.loop,
            )
            code, info = "227", ["listen socket created"]

        else:

            code, info = "227", ["listen socket already exists"]

        host, port = connection["passive_server"].sockets[0].getsockname()
        nums = tuple(map(int, str.split(host, "."))) + (port >> 8, port & 0xff)
        info.append(str.format("({})", str.join(",", map(str, nums))))
        return True, code, info

    @asyncio.coroutine
    @login_required
    def abor(self, connection, rest):

        pass
