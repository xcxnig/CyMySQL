# Python implementation of the MySQL client-server protocol
#   https://dev.mysql.com/doc/dev/mysql-server/latest/PAGE_PROTOCOL.html

import hashlib
import socket
import ssl
import struct
import sys
import os
import stat
import getpass
try:
    from ConfigParser import RawConfigParser
except ImportError:
    from configparser import RawConfigParser

from cymysql.charset import charset_by_name, encoding_by_charset
from cymysql.cursors import Cursor
from cymysql.constants import CLIENT, COMMAND, SERVER_STATUS
from cymysql.converters import decoders, encoders, escape_item
from cymysql.err import Warning, Error, \
     InterfaceError, DataError, DatabaseError, OperationalError, \
     IntegrityError, InternalError, NotSupportedError, ProgrammingError
from cymysql.packet import MysqlPacket
from cymysql.result import MySQLResult
from cymysql.socketwrapper import SocketWrapper

DEFAULT_USER = getpass.getuser()
DEFAULT_CHARSET = 'utf8mb4'


def sha_new(*args, **kwargs):
    return hashlib.new("sha1", *args, **kwargs)


def sha256_new(*args, **kwargs):
    return hashlib.new("sha256", *args, **kwargs)


def byte2int(b):
    if isinstance(b, int):
        return b
    else:
        return ord(b)


def int2byte(i):
    return bytes([i])


def pack_int24(n):
    return bytes([n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF])


SCRAMBLE_LENGTH = 20


def _xor(data1, data2):
    result = b''
    for i in range(len(data1)):
        j = i % len(data2)
        x = struct.unpack('B', data1[i:i+1])[0] ^ struct.unpack('B', data2[j:j+1])[0]
        result += struct.pack('B', x)
    return result


def _mysql_native_password_scramble(password, message):
    if password is None or len(password) == 0:
        return b''
    message2 = sha_new(password).digest()
    stage2 = sha_new(message2).digest()
    s = sha_new()
    s.update(message[:SCRAMBLE_LENGTH])
    s.update(stage2)
    message1 = s.digest()
    return _xor(message1, message2)


def _caching_sha2_password_scramble(password, nonce):
    if password is None or len(password) == 0:
        return b''
    message1 = sha256_new(password).digest()
    s = sha256_new()
    s.update(sha256_new(message1).digest())
    s.update(nonce[:SCRAMBLE_LENGTH])
    message2 = s.digest()
    return _xor(message1, message2)


class Connection(object):
    """
    Representation of a socket with a mysql server.

    The proper way to get an instance of this class is to call
    connect()."""

    def errorhandler(connection, cursor, errorclass, errorvalue):
        err = errorclass, errorvalue

        if cursor:
            cursor.messages.append(err)
        else:
            connection.messages.append(err)
        del cursor
        del connection

        if not issubclass(errorclass, Error):
            raise Error(errorclass, errorvalue)
        elif isinstance(errorvalue, errorclass):
            raise errorvalue
        else:
            raise errorclass(*errorvalue)

    def __init__(self, host="localhost", user=None, passwd="",
                 db=None, port=3306, unix_socket=None,
                 charset='', sql_mode=None,
                 read_default_file=None,
                 client_flag=0, cursorclass=None, init_command=None,
                 connect_timeout=None, ssl=None, read_default_group=None,
                 compress="", zstd_compression_level=3, named_pipe=None,
                 conv=decoders, encoders=encoders):
        """
        Establish a connection to the MySQL database. Accepts several
        arguments:

        host: Host where the database server is located
        user: Username to log in as
        passwd: Password to use.
        db: Database to use, None to not use a particular one.
        port: MySQL port to use, default is usually OK.
        unix_socket: Optionally, you can use a unix socket rather than TCP/IP.
        charset: Charset you want to use.
        sql_mode: Default SQL_MODE to use.
        read_default_file: Specifies  my.cnf file to read these parameters from under the [client] section.
        conv: Decoders dictionary to use instead of the default one. This is used to provide custom marshalling of types. See converters.
        client_flag: Custom flags to send to MySQL. Find potential values in constants.CLIENT.
        cursorclass: Custom cursor class to use.
        init_command: Initial SQL statement to run when connection is established.
        connect_timeout: Timeout before throwing an exception when connecting.
        ssl: A dict of arguments similar to mysql_ssl_set()'s parameters. For now the capath and cipher arguments are not supported.
        read_default_group: Group to read from in the configuration file.
        compress: Compression algorithm ("zlib" or "zstd").
        zstd_compression_level: zstd compression leve (1-22), default is 3.
        named_pipe: Not supported
        """
        if named_pipe:
            raise NotImplementedError("named_pipe argument are not supported")

        if ssl and ('capath' in ssl or 'cipher' in ssl):
            raise NotImplementedError('ssl options capath and cipher are not supported')

        if compress and compress not in ("zlib", "zstd"):
            raise NotImplementedError('compress argument can set zlib or zstd')

        self.compress = compress
        self.zstd_compression_level = zstd_compression_level
        self.socket = None
        self.ssl = False
        if ssl:
            self.ssl = True
            client_flag |= CLIENT.SSL
            for k in ('key', 'cert', 'ca'):
                v = None
                if k in ssl:
                    v = ssl[k]
                setattr(self, k, v)

        if read_default_group and not read_default_file:
            if sys.platform.startswith("win"):
                read_default_file = "c:\\my.ini"
            else:
                for f in ('~/.my.cnf', '/etc/my.cnf', '/etc/mysql/my.cnf'):
                    if os.path.isfile(os.path.expanduser(f)):
                        read_default_file = f
                        break

        if read_default_file:
            if not read_default_group:
                read_default_group = "client"

            cfg = RawConfigParser()
            cfg.read(os.path.expanduser(read_default_file))

            def _config(key, default):
                try:
                    return cfg.get(read_default_group, key)
                except:
                    return default

            user = _config("user", user)
            passwd = _config("password", passwd)
            host = _config("host", host)
            db = _config("db", db)
            unix_socket = _config("socket", unix_socket)
            port = _config("port", port)
            charset = _config("default-character-set", charset)

        if (
            host == 'localhost' and port == 3306
            and not sys.platform.startswith('win')
            and (unix_socket is None or not os.path.exists(unix_socket))
        ):
            for f in (
                    '/var/lib/mysql/mysql.sock',
                    '/var/run/mysql/mysql.sock',
                    '/var/run/mysql.sock',
                    '/var/mysql/mysql.sock'
            ):
                if os.path.exists(f) and stat.S_ISSOCK(os.stat(f).st_mode):
                    unix_socket = f
                    break
        self.host = host
        self.port = port
        self.user = user or DEFAULT_USER
        self.password = passwd
        self.db = db
        self.unix_socket = unix_socket
        self.conv = conv
        self.encoders = encoders
        self.charset = charset if charset else DEFAULT_CHARSET
        self.encoding = encoding_by_charset(self.charset)

        client_flag |= CLIENT.CAPABILITIES
        client_flag |= CLIENT.MULTI_STATEMENTS
        if self.db:
            client_flag |= CLIENT.CONNECT_WITH_DB
        # self.client_flag |= CLIENT.DEPRECATE_EOF
        if self.compress == "zlib":
            client_flag |= CLIENT.COMPRESS
        elif self.compress == "zstd":
            client_flag |= CLIENT.ZSTD_COMPRESSION_ALGORITHM
        self.client_flag = client_flag

        self.cursorclass = cursorclass
        self.connect_timeout = connect_timeout
        self.messages = []
        self._result = None

        self.sql_mode = sql_mode
        self.init_command = init_command

    def _initialize(self):
        self._get_server_information()
        self._request_authentication()
        self.set_charset(self.charset)

        self.autocommit(False)

        if self.sql_mode is not None:
            c = self.cursor()
            c.execute("SET sql_mode=%s", (self.sql_mode,))

        if self.init_command is not None:
            c = self.cursor()
            c.execute(self.init_command)

            self.commit()

    def close(self):
        ''' Send the quit message and close the socket '''
        if self.socket is None:
            return
        send_data = b'\x01\x00\x00\x00' + int2byte(COMMAND.COM_QUIT)
        self.socket.send_packet(send_data)
        self.socket.close()
        self.socket = None

    @property
    def closed(self):
        return self.socket is None

    def autocommit(self, value):
        ''' Set whether or not to commit after every execute() '''
        if value:
            q = "SET AUTOCOMMIT = 1"
        else:
            q = "SET AUTOCOMMIT = 0"
        try:
            self._execute_command(COMMAND.COM_QUERY, q)
            self.read_packet()
        except:
            exc, value, tb = sys.exc_info()
            self.errorhandler(None, exc, value)

    def commit(self):
        ''' Commit changes to stable storage '''
        try:
            self._execute_command(COMMAND.COM_QUERY, "COMMIT")
            self.read_packet()
        except:
            exc, value, tb = sys.exc_info()
            self.errorhandler(None, exc, value)

    def rollback(self):
        ''' Roll back the current transaction '''
        try:
            self._execute_command(COMMAND.COM_QUERY, "ROLLBACK")
            self.read_packet()
        except:
            exc, value, tb = sys.exc_info()
            self.errorhandler(None, exc, value)

    def escape(self, obj):
        ''' Escape whatever value you pass to it  '''
        return escape_item(obj, self.charset, self.encoders)

    def literal(self, obj):
        ''' Alias for escape() '''
        return escape_item(obj, self.charset, self.encoders)

    def cursor(self, cursor=None):
        ''' Create a new cursor to execute queries with '''
        if cursor is None:
            cursor = self.cursorclass
        if cursor is None:
            cursor = Cursor
        return cursor(self)

    def __enter__(self):
        ''' Context manager that returns a Cursor '''
        return self.cursor()

    def __exit__(self, exc, value, traceback):
        ''' On successful exit, commit. On exception, rollback. '''
        if exc:
            self.rollback()
        else:
            self.commit()

    def __del__(self):
        if hasattr(self, 'socket') and self.socket:
            self.socket.close()
            self.socket = None

    def _is_connect(self):
        return bool(self.socket)

    # The following methods are INTERNAL USE ONLY (called from Cursor)
    def query(self, sql):
        self._execute_command(COMMAND.COM_QUERY, sql)
        self._result = MySQLResult(self)
        self._result.read_result()

    def next_result(self):
        self._result = MySQLResult(self)
        self._result.read_result()

    def affected_rows(self):
        if self._result:
            self._result._affected_rows
        else:
            return 0

    def kill(self, thread_id):
        arg = struct.pack('<I', thread_id)
        try:
            self._execute_command(COMMAND.COM_PROCESS_KILL, arg)
            pkt = self.read_packet()
            return pkt.is_ok_packet()
        except:
            exc, value, tb = sys.exc_info()
            self.errorhandler(None, exc, value)
        return False

    def ping(self, reconnect=True):
        ''' Check if the server is alive '''
        try:
            self._execute_command(COMMAND.COM_PING, "")
        except:
            if reconnect:
                self._connect()
                return self.ping(False)
            else:
                exc, value, tb = sys.exc_info()
                self.errorhandler(None, exc, value)
                return

        pkt = self.read_packet()
        return pkt.is_ok_packet()

    def set_charset(self, charset):
        try:
            if charset:
                self._execute_command(COMMAND.COM_QUERY, "SET NAMES %s" %
                                      self.escape(charset))
                self.read_packet()
                self.charset = charset
        except:
            exc, value, tb = sys.exc_info()
            self.errorhandler(None, exc, value)

    def _get_socket(self):
        sock = None
        try:
            if self.unix_socket and (self.host == 'localhost' or self.host == '127.0.0.1'):
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(self.connect_timeout)
                sock.connect(self.unix_socket)
                self.host_info = "Localhost via UNIX socket"
            else:
                sock = socket.create_connection((self.host, self.port), self.connect_timeout)
                self.host_info = "socket %s:%d" % (self.host, self.port)
        except socket.error as e:
            if sock:
                sock.close()
            raise OperationalError(
                2003, "Can't connect to MySQL server on %r (%s)" % (self.host, e.args[0])
            )

        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return sock

    def _connect(self):
        self.socket = SocketWrapper(self._get_socket(), self.compress)

    def read_packet(self):
        """Read an entire "mysql packet" in its entirety from the network
        and return a MysqlPacket type that represents the results."""
        return MysqlPacket(self.socket.recv_packet(), self.charset, self.encoding)

    def insert_id(self):
        if self._result:
            return self._result.insert_id
        else:
            return 0

    def _execute_command(self, command, sql):
        if not self.socket:
            self.errorhandler(None, InterfaceError, (-1, 'socket not found'))

        if isinstance(sql, str):
            sql = sql.encode(self.encoding)

        if len(sql) + 1 > 0xffffff:
            raise ValueError('Sending query packet is too large')
        prelude = struct.pack('<i', len(sql)+1) + int2byte(command)
        self.socket.send_packet(prelude + sql)

    def _scramble(self):
        if self.auth_plugin_name in ('', 'mysql_native_password'):
            data = _mysql_native_password_scramble(
                self.password.encode(self.encoding), self.salt
            )
        elif self.auth_plugin_name == 'caching_sha2_password':
            data = _caching_sha2_password_scramble(
                self.password.encode(self.encoding), self.salt
            )
        elif self.auth_plugin_name == 'mysql_clear_password':
            data = self.password.encode(self.encoding) + b'\x00'
        else:
            raise NotImplementedError(
                "%s authentication plugin is not implemented" % (self.auth_plugin_name, )
            )
        return data

    def _request_authentication(self):
        if self.user is None:
            raise ValueError("Did not specify a username")

        next_packet = 1

        charset_id = charset_by_name(self.charset).id
        user = self.user.encode(self.encoding)

        data_init = (
            struct.pack('<i', self.client_flag) +
            struct.pack("<I", 1) +
            int2byte(charset_id) + int2byte(0)*23
        )

        if self.ssl and self.server_capabilities & CLIENT.SSL:
            data = pack_int24(len(data_init)) + int2byte(next_packet) + data_init
            self.socket.send_uncompress_packet(data)
            next_packet += 1
            self.socket = ssl.wrap_socket(self.socket, keyfile=self.key,
                                          certfile=self.cert,
                                          ca_certs=self.ca)

        data = data_init + user + int2byte(0)
        authresp = self._scramble()

        if self.server_capabilities & CLIENT.SECURE_CONNECTION:
            data += int2byte(len(authresp)) + authresp
        else:
            data += authresp + int2byte(0)

        if self.db and self.server_capabilities & CLIENT.CONNECT_WITH_DB:
            data += self.db.encode(self.encoding) + int2byte(0)

        if self.server_capabilities & CLIENT.PLUGIN_AUTH:
            data += self.auth_plugin_name.encode(self.encoding) + int2byte(0)

        if self.server_capabilities & CLIENT.ZSTD_COMPRESSION_ALGORITHM:
            data += int2byte(self.zstd_compression_level)

        data = pack_int24(len(data)) + int2byte(next_packet) + data
        next_packet += 2

        self.socket.send_uncompress_packet(data)
        auth_packet = self.socket.recv_uncompress_packet()

        if auth_packet[0] == 0xfe:  # EOF packet
            # AuthSwitchRequest
            # https://dev.mysql.com/doc/internals/en/connection-phase-packets.html#packet-Protocol::AuthSwitchRequest
            i = auth_packet.find(b'\0', 1)
            self.auth_plugin_name = auth_packet[1:i].decode('utf-8')
            j = auth_packet.find(b'\0', i + 1)
            self.salt = auth_packet[i + 1:j]
            data = self._scramble()
            data = pack_int24(len(data)) + int2byte(next_packet) + data
            next_packet += 2
            self.socket.send_uncompress_packet(data)
            auth_packet = self.socket.recv_uncompress_packet()

        if self.auth_plugin_name == 'caching_sha2_password':
            self._caching_sha2_authentication2(auth_packet, next_packet)

    def _caching_sha2_authentication2(self, auth_packet, next_packet):
        # https://dev.mysql.com/doc/dev/mysql-server/latest/page_caching_sha2_authentication_exchanges.html
        if auth_packet == b'\x01\x03':   # fast_auth_success
            self.read_packet()
            return

        # perform_full_authentication
        assert auth_packet == b'\x01\x04'

        if self.ssl or self.unix_socket:
            data = self.password.encode(self.encoding) + b'\x00'
        else:
            # request_public_key
            data = b'\x02'
            data = pack_int24(len(data)) + int2byte(next_packet) + data
            next_packet += 2
            self.socket.send_uncompress_packet(data)
            response = self.read_packet()
            public_pem = response.get_all_data()[1:]

            from Crypto.PublicKey import RSA
            from Crypto.Cipher import PKCS1_OAEP
            key = RSA.importKey(public_pem)
            cipher = PKCS1_OAEP.new(key)
            password = self.password.encode(self.encoding) + b'\x00'
            data = cipher.encrypt(_xor(password, self.salt))

        data = pack_int24(len(data)) + int2byte(next_packet) + data
        next_packet += 2
        self.socket.send_uncompress_packet(data)

        self.read_packet()

    # _mysql support
    def thread_id(self):
        return self.server_thread_id[0]

    def character_set_name(self):
        return self.charset

    def get_host_info(self):
        return self.host_info

    def get_proto_info(self):
        return self.protocol_version

    def _get_server_information(self):
        # https://dev.mysql.com/doc/internals/en/connection-phase-packets.html#packet-Protocol::Handshake
        i = 0
        data =  self.socket.recv_uncompress_packet()

        self.protocol_version = byte2int(data[i:i+1])
        i += 1
        str_end = data.find(int2byte(0), i)
        self.server_version = data[i:str_end].decode('utf-8')
        i = str_end + 1
        self.server_thread_id = struct.unpack('<I', data[i:i+4])
        i += 4
        self.salt = data[i:i+8]
        i += 9
        self.server_capabilities = struct.unpack('<H', data[i:i+2])[0]
        i += 2

        self.server_status = None
        self.auth_plugin_name = ''
        if len(data) > i:
            # Drop server_language and server_charset now.
            # character_set(1) only the lower 8 bits
            # self.server_language = byte2int(data[i:i+1])
            # self.server_charset = charset_by_id(self.server_language).name
            i += 1
            self.server_status = struct.unpack('<H', data[i:i+2])[0]
            i += 2
            self.server_capabilities |= (struct.unpack('<H', data[i:i+2])[0]) << 16
            i += 2

            salt_len = byte2int(data[i:i+1])
            i += 1

            i += 10     # reserverd
            if salt_len:
                rest_salt_len = max(13, salt_len-8)
                self.salt += data[i:i+rest_salt_len-1]
                i += rest_salt_len
            self.auth_plugin_name = data[i:data.find(int2byte(0), i)].decode('utf-8')

    def get_transaction_status(self):
        return bool(self.server_status & SERVER_STATUS.SERVER_STATUS_IN_TRANS)

    def get_server_info(self):
        return self.server_version

    Warning = Warning
    Error = Error
    InterfaceError = InterfaceError
    DatabaseError = DatabaseError
    DataError = DataError
    OperationalError = OperationalError
    IntegrityError = IntegrityError
    InternalError = InternalError
    ProgrammingError = ProgrammingError
    NotSupportedError = NotSupportedError
