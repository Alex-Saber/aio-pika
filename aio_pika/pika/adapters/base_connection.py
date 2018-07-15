"""Base class extended by connection adapters. This extends the
connection.Connection class to encapsulate connection behavior but still
isolate socket and low level communication.

"""
import errno
import logging
import socket
import ssl

from .. import connection
from .. import exceptions


log = logging.getLogger(__name__)


class BaseConnection(connection.Connection):
    """BaseConnection class that should be extended by connection adapters"""

    # Use epoll's constants to keep life easy
    READ = 0x0001
    WRITE = 0x0004
    ERROR = 0x0008

    ERRORS_TO_ABORT = [errno.EBADF, errno.ECONNABORTED, errno.EPIPE]
    ERRORS_TO_IGNORE = [errno.EWOULDBLOCK, errno.EAGAIN, errno.EINTR]
    DO_HANDSHAKE = True
    WARN_ABOUT_IOLOOP = False

    def __init__(self,
                 parameters=None,
                 on_open_callback=None,
                 on_open_error_callback=None,
                 on_close_callback=None,
                 ioloop=None,
                 stop_ioloop_on_close=True):
        """Create a new instance of the Connection object.

        :param pika.connection.Parameters parameters: Connection parameters
        :param method on_open_callback: Method to call on connection open
        :param on_open_error_callback: Method to call if the connection cant
                                       be opened
        :type on_open_error_callback: method
        :param method on_close_callback: Method to call on connection close
        :param object ioloop: IOLoop object to use
        :param bool stop_ioloop_on_close: Call ioloop.stop() if disconnected
        :raises: RuntimeError
        :raises: ValueError

        """
        if parameters and not isinstance(parameters, connection.Parameters):
            raise ValueError('Expected instance of Parameters, not %r' %
                             parameters)

        # Let the developer know we could not import SSL
        if parameters and parameters.ssl and not ssl:
            raise RuntimeError("SSL specified but it is not available")
        self.base_events = self.READ | self.ERROR
        self.event_state = self.base_events
        self.ioloop = ioloop
        self.socket = None
        self.stop_ioloop_on_close = stop_ioloop_on_close
        self.write_buffer = None
        super(BaseConnection, self).__init__(parameters, on_open_callback,
                                             on_open_error_callback,
                                             on_close_callback)

    def add_timeout(self, deadline, callback_method):
        """Add the callback_method to the IOLoop timer to fire after deadline
        seconds. Returns a handle to the timeout

        :param int deadline: The number of seconds to wait to call callback
        :param method callback_method: The callback method
        :rtype: str

        """
        return self.ioloop.add_timeout(deadline, callback_method)

    def close(self, reply_code=200, reply_text='Normal shutdown'):
        """Disconnect from RabbitMQ. If there are any open channels, it will
        attempt to close them prior to fully disconnecting. Channels which
        have active consumers will attempt to send a Basic.Cancel to RabbitMQ
        to cleanly stop the delivery of messages prior to closing the channel.

        :param int reply_code: The code number for the close
        :param str reply_text: The text reason for the close

        """
        super(BaseConnection, self).close(reply_code, reply_text)
        self._handle_ioloop_stop()

    def remove_timeout(self, timeout_id):
        """Remove the timeout from the IOLoop by the ID returned from
        add_timeout.

        :rtype: str

        """
        self.ioloop.remove_timeout(timeout_id)

    def _adapter_connect(self):
        """Connect to the RabbitMQ broker, returning True if connected.

        :returns: error string or exception instance on error; None on success

        """
        # Get the addresses for the socket, supporting IPv4 & IPv6
        while True:
            try:
                addresses = socket.getaddrinfo(self.params.host, self.params.port,
                                               0, socket.SOCK_STREAM,
                                               socket.IPPROTO_TCP)
                break
            except OSError as error:
                if error.errno == errno.EINTR:
                    continue

                log.critical('Could not get addresses to use: %s (%s)', error,
                             self.params.host)
                return error

        # If the socket is created and connected, continue on
        error = "No socket addresses available"
        for sock_addr in addresses:
            error = self._create_and_connect_to_socket(sock_addr)
            if not error:
                # Make the socket non-blocking after the connect
                self.socket.setblocking(0)
                return None
            self._cleanup_socket()

        # Failed to connect
        return error

    def _adapter_disconnect(self):
        """Invoked if the connection is being told to disconnect"""
        try:
            self._remove_heartbeat()
            self._cleanup_socket()
            self._check_state_on_disconnect()
        finally:
            # Ensure proper cleanup since _check_state_on_disconnect may raise
            # an exception
            self._handle_ioloop_stop()
            self._init_connection_state()

    def _check_state_on_disconnect(self):
        """Checks to see if we were in opening a connection with RabbitMQ when
        we were disconnected and raises exceptions for the anticipated
        exception types.

        """
        if self.connection_state == self.CONNECTION_PROTOCOL:
            log.error('Incompatible Protocol Versions')
            raise exceptions.IncompatibleProtocolError
        elif self.connection_state == self.CONNECTION_START:
            log.error("Socket closed while authenticating indicating a "
                         "probable authentication error")
            raise exceptions.ProbableAuthenticationError
        elif self.connection_state == self.CONNECTION_TUNE:
            log.error("Socket closed while tuning the connection indicating "
                         "a probable permission error when accessing a virtual "
                         "host")
            raise exceptions.ProbableAccessDeniedError
        elif self.is_open:
            log.warning("Socket closed when connection was open")
        elif not self.is_closed and not self.is_closing:
            log.warning('Unknown state on disconnect: %i',
                        self.connection_state)

    def _cleanup_socket(self):
        """Close the socket cleanly"""
        if self.socket:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.socket.close()
            self.socket = None

    def _create_and_connect_to_socket(self, sock_addr_tuple):
        """Create socket and connect to it, using SSL if enabled.

        :returns: error string on failure; None on success
        """
        self.socket = socket.socket(sock_addr_tuple[0], socket.SOCK_STREAM, 0)
        self.socket.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        self.socket.settimeout(self.params.socket_timeout)

        # Wrap socket if using SSL
        if self.params.ssl:
            self.socket = self._wrap_socket(self.socket)
            ssl_text = " with SSL"
        else:
            ssl_text = ""

        log.info('Connecting to %s:%s%s', sock_addr_tuple[4][0],
                 sock_addr_tuple[4][1], ssl_text)

        # Connect to the socket
        try:
            self.socket.connect(sock_addr_tuple[4])
        except socket.timeout:
            error = 'Connection to %s:%s failed: timeout' % (
                sock_addr_tuple[4][0], sock_addr_tuple[4][1]
            )
            log.error(error)
            return error
        except OSError as error:
            error = 'Connection to %s:%s failed: %s' % (sock_addr_tuple[4][0],
                                                        sock_addr_tuple[4][1],
                                                        error)
            log.warning(error)
            return error

        # Handle SSL Connection Negotiation
        if self.params.ssl and self.DO_HANDSHAKE:
            try:
                self._do_ssl_handshake()
            except ssl.SSLError as error:
                error = 'SSL connection to %s:%s failed: %s' % (
                    sock_addr_tuple[4][0], sock_addr_tuple[4][1], error
                )
                log.error(error)
                return error
        # Made it this far
        return None

    def _do_ssl_handshake(self):
        """Perform SSL handshaking, copied from python stdlib test_ssl.py.

        """
        if not self.DO_HANDSHAKE:
            return
        while True:
            try:
                self.socket.do_handshake()
                break
            except ssl.SSLError as err:
                if err.args[0] == ssl.SSL_ERROR_WANT_READ:
                    self.event_state = self.READ
                elif err.args[0] == ssl.SSL_ERROR_WANT_WRITE:
                    self.event_state = self.WRITE
                else:
                    raise
                self._manage_event_state()

    @staticmethod
    def _get_error_code(error_value):
        """Get the error code from the error_value accounting for Python
        version differences.

        :rtype: int

        """
        if not error_value:
            return None
        if hasattr(error_value, 'errno'):  # Python >= 2.6
            return error_value.errno
        elif error_value is not None:
            return error_value[0]  # Python <= 2.5
        return None

    def _flush_outbound(self):
        """write early, if the socket will take the data why not get it out
        there asap.
        """
        self._handle_write()
        self._manage_event_state()

    def _handle_disconnect(self):
        """Called internally when the socket is disconnected already
        """
        self._adapter_disconnect()
        self._on_connection_closed(None, True)

    def _handle_ioloop_stop(self):
        """Invoked when the connection is closed to determine if the IOLoop
        should be stopped or not.

        """
        if self.stop_ioloop_on_close and self.ioloop:
            self.ioloop.stop()
        elif self.WARN_ABOUT_IOLOOP:
            log.warning('Connection is closed but not stopping IOLoop')

    def _handle_error(self, error_value):
        """Internal error handling method. Here we expect a socket.error
        coming in and will handle different socket errors differently.

        :param int|object error_value: The inbound error

        """
        if 'timed out' in str(error_value):
            raise socket.timeout
        error_code = self._get_error_code(error_value)
        if not error_code:
            log.critical("Tried to handle an error where no error existed")
            return

        # Ok errors, just continue what we were doing before
        if error_code in self.ERRORS_TO_IGNORE:
            log.debug("Ignoring %s", error_code)
            return

        # Socket is no longer connected, abort
        elif error_code in self.ERRORS_TO_ABORT:
            log.error("Fatal Socket Error: %r", error_value)

        elif self.params.ssl and isinstance(error_value, ssl.SSLError):

            if error_value.args[0] == ssl.SSL_ERROR_WANT_READ:
                self.event_state = self.READ
            elif error_value.args[0] == ssl.SSL_ERROR_WANT_WRITE:
                self.event_state = self.WRITE
            else:
                log.error("SSL Socket error: %r", error_value)

        else:
            # Haven't run into this one yet, log it.
            log.error("Socket Error: %s", error_code)

        # Disconnect from our IOLoop and let Connection know what's up
        self._handle_disconnect()

    def _handle_timeout(self):
        """Handle a socket timeout in read or write.
        We don't do anything in the non-blocking handlers because we
        only have the socket in a blocking state during connect."""
        pass

    def _handle_events(self, fd, events, error=None, write_only=False):
        """Handle IO/Event loop events, processing them.

        :param int fd: The file descriptor for the events
        :param int events: Events from the IO/Event loop
        :param int error: Was an error specified
        :param bool write_only: Only handle write events

        """
        if not self.socket:
            log.error('Received events on closed socket: %r', fd)
            return

        if self.socket and (events & self.WRITE):
            self._handle_write()
            self._manage_event_state()

        if self.socket and not write_only and (events & self.READ):
            self._handle_read()

        if (self.socket and write_only and (events & self.READ) and
            (events & self.ERROR)):
            log.error('BAD libc:  Write-Only but Read+Error. '
                         'Assume socket disconnected.')
            self._handle_disconnect()

        if self.socket and (events & self.ERROR):
            log.error('Error event %r, %r', events, error)
            self._handle_error(error)

    def _handle_read(self):
        """Read from the socket and call our on_data_available with the data."""
        try:
            while True:
                try:
                    if self.params.ssl:
                        data = self.socket.read(self._buffer_size)
                    else:
                        data = self.socket.recv(self._buffer_size)

                    break
                except OSError as error:
                    if error.errno == errno.EINTR:
                        continue
                    else:
                        raise

        except socket.timeout:
            self._handle_timeout()
            return 0

        except ssl.SSLError as error:
            if error.args[0] == ssl.SSL_ERROR_WANT_READ:
                # ssl wants more data but there is nothing currently
                # available in the socket, wait for it to become readable.
                return 0
            return self._handle_error(error)

        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return 0
            return self._handle_error(error)

        # Empty data, should disconnect
        if not data or data == 0:
            log.error('Read empty data, calling disconnect')
            return self._handle_disconnect()

        # Pass the data into our top level frame dispatching method
        self._on_data_available(data)
        return len(data)

    def _handle_write(self):
        """Try and write as much as we can, if we get blocked requeue
        what's left"""
        bytes_written = 0
        try:
            while self.outbound_buffer:
                frame = self.outbound_buffer.popleft()
                while True:
                    try:
                        bw = self.socket.send(frame)
                        break
                    except OSError as error:
                        if error.errno == errno.EINTR:
                            continue
                        else:
                            raise

                bytes_written += bw
                if bw < len(frame):
                    log.debug("Partial write, requeing remaining data")
                    self.outbound_buffer.appendleft(frame[bw:])
                    break

        except socket.timeout:
            # Will only come here if the socket is blocking
            log.debug("socket timeout, requeuing frame")
            self.outbound_buffer.appendleft(frame)
            self._handle_timeout()

        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                log.debug("Would block, requeuing frame")
                self.outbound_buffer.appendleft(frame)
            else:
                return self._handle_error(error)

        return bytes_written


    def _init_connection_state(self):
        """Initialize or reset all of our internal state variables for a given
        connection. If we disconnect and reconnect, all of our state needs to
        be wiped.

        """
        super(BaseConnection, self)._init_connection_state()
        self.base_events = self.READ | self.ERROR
        self.event_state = self.base_events
        self.socket = None

    def _manage_event_state(self):
        """Manage the bitmask for reading/writing/error which is used by the
        io/event handler to specify when there is an event such as a read or
        write.

        """
        if self.outbound_buffer:
            if not self.event_state & self.WRITE:
                self.event_state |= self.WRITE
                self.ioloop.update_handler(self.socket.fileno(),
                                           self.event_state)
        elif self.event_state & self.WRITE:
            self.event_state = self.base_events
            self.ioloop.update_handler(self.socket.fileno(), self.event_state)

    def _wrap_socket(self, sock):
        """Wrap the socket for connecting over SSL.

        :rtype: ssl.SSLSocket

        """
        return ssl.wrap_socket(sock,
                               do_handshake_on_connect=self.DO_HANDSHAKE,
                               **self.params.ssl_options)