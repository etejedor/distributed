from __future__ import annotations

import asyncio
import contextvars
import functools
import importlib
import inspect
import json
import logging
import multiprocessing
import os
import pkgutil
import re
import socket
import sys
import tempfile
import threading
import warnings
import weakref
import xml.etree.ElementTree
from asyncio import TimeoutError
from collections import OrderedDict, UserDict, deque
from collections.abc import Container, KeysView, ValuesView
from concurrent.futures import CancelledError, ThreadPoolExecutor  # noqa: F401
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from hashlib import md5
from importlib.util import cache_from_source
from time import sleep
from types import ModuleType
from typing import Any as AnyType
from typing import ClassVar

import click
import tblib.pickling_support

try:
    import resource
except ImportError:
    resource = None  # type: ignore

import tlz as toolz
from tornado import gen
from tornado.ioloop import IOLoop

import dask
from dask import istask
from dask.utils import parse_timedelta as _parse_timedelta
from dask.widgets import get_template

from distributed.compatibility import WINDOWS
from distributed.metrics import time

try:
    from dask.context import thread_state
except ImportError:
    thread_state = threading.local()

# For some reason this is required in python >= 3.9
if WINDOWS:
    import multiprocessing.popen_spawn_win32
else:
    import multiprocessing.popen_spawn_posix

logger = _logger = logging.getLogger(__name__)


no_default = "__no_default__"


def _initialize_mp_context():
    method = dask.config.get("distributed.worker.multiprocessing-method")
    ctx = multiprocessing.get_context(method)
    if method == "forkserver":
        # Makes the test suite much faster
        preload = ["distributed"]
        if "pkg_resources" in sys.modules:
            preload.append("pkg_resources")

        from distributed.versions import optional_packages, required_packages

        for pkg, _ in required_packages + optional_packages:
            try:
                importlib.import_module(pkg)
            except ImportError:
                pass
            else:
                preload.append(pkg)
        ctx.set_forkserver_preload(preload)

    return ctx


mp_context = _initialize_mp_context()


def has_arg(func, argname):
    """
    Whether the function takes an argument with the given name.
    """
    while True:
        try:
            if argname in inspect.getfullargspec(func).args:
                return True
        except TypeError:
            break
        try:
            # For Tornado coroutines and other decorated functions
            func = func.__wrapped__
        except AttributeError:
            break
    return False


def get_fileno_limit():
    """
    Get the maximum number of open files per process.
    """
    if resource is not None:
        return resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    else:
        # Default ceiling for Windows when using the CRT, though it
        # is settable using _setmaxstdio().
        return 512


@toolz.memoize
def _get_ip(host, port, family):
    # By using a UDP socket, we don't actually try to connect but
    # simply select the local address through which *host* is reachable.
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        sock.connect((host, port))
        ip = sock.getsockname()[0]
        return ip
    except OSError as e:
        warnings.warn(
            "Couldn't detect a suitable IP address for "
            "reaching %r, defaulting to hostname: %s" % (host, e),
            RuntimeWarning,
        )
        addr_info = socket.getaddrinfo(
            socket.gethostname(), port, family, socket.SOCK_DGRAM, socket.IPPROTO_UDP
        )[0]
        return addr_info[4][0]
    finally:
        sock.close()


def get_ip(host="8.8.8.8", port=80):
    """
    Get the local IP address through which the *host* is reachable.

    *host* defaults to a well-known Internet host (one of Google's public
    DNS servers).
    """
    return _get_ip(host, port, family=socket.AF_INET)


def get_ipv6(host="2001:4860:4860::8888", port=80):
    """
    The same as get_ip(), but for IPv6.
    """
    return _get_ip(host, port, family=socket.AF_INET6)


def get_ip_interface(ifname):
    """
    Get the local IPv4 address of a network interface.

    KeyError is raised if the interface doesn't exist.
    ValueError is raised if the interface does no have an IPv4 address
    associated with it.
    """
    import psutil

    net_if_addrs = psutil.net_if_addrs()

    if ifname not in net_if_addrs:
        allowed_ifnames = list(net_if_addrs.keys())
        raise ValueError(
            "{!r} is not a valid network interface. "
            "Valid network interfaces are: {}".format(ifname, allowed_ifnames)
        )

    for info in net_if_addrs[ifname]:
        if info.family == socket.AF_INET:
            return info.address
    raise ValueError(f"interface {ifname!r} doesn't have an IPv4 address")


async def All(args, quiet_exceptions=()):
    """Wait on many tasks at the same time

    Err once any of the tasks err.

    See https://github.com/tornadoweb/tornado/issues/1546

    Parameters
    ----------
    args: futures to wait for
    quiet_exceptions: tuple, Exception
        Exception types to avoid logging if they fail
    """
    tasks = gen.WaitIterator(*map(asyncio.ensure_future, args))
    results = [None for _ in args]
    while not tasks.done():
        try:
            result = await tasks.next()
        except Exception:

            @gen.coroutine
            def quiet():
                """Watch unfinished tasks

                Otherwise if they err they get logged in a way that is hard to
                control.  They need some other task to watch them so that they
                are not orphaned
                """
                for task in list(tasks._unfinished):
                    try:
                        yield task
                    except quiet_exceptions:
                        pass

            quiet()
            raise
        results[tasks.current_index] = result
    return results


async def Any(args, quiet_exceptions=()):
    """Wait on many tasks at the same time and return when any is finished

    Err once any of the tasks err.

    Parameters
    ----------
    args: futures to wait for
    quiet_exceptions: tuple, Exception
        Exception types to avoid logging if they fail
    """
    tasks = gen.WaitIterator(*map(asyncio.ensure_future, args))
    results = [None for _ in args]
    while not tasks.done():
        try:
            result = await tasks.next()
        except Exception:

            @gen.coroutine
            def quiet():
                """Watch unfinished tasks

                Otherwise if they err they get logged in a way that is hard to
                control.  They need some other task to watch them so that they
                are not orphaned
                """
                for task in list(tasks._unfinished):
                    try:
                        yield task
                    except quiet_exceptions:
                        pass

            quiet()
            raise

        results[tasks.current_index] = result
        break
    return results


class NoOpAwaitable:
    """An awaitable object that always returns None.

    Useful to return from a method that can be called in both asynchronous and
    synchronous contexts"""

    def __await__(self):
        async def f():
            return None

        return f().__await__()


class SyncMethodMixin:
    """
    A mixin for adding an `asynchronous` attribute and `sync` method to a class.

    Subclasses must define a `loop` attribute for an associated
    `tornado.IOLoop`, and may also add a `_asynchronous` attribute indicating
    whether the class should default to asynchronous behavior.
    """

    @property
    def asynchronous(self):
        """Are we running in the event loop?"""
        return in_async_call(self.loop, default=getattr(self, "_asynchronous", False))

    def sync(self, func, *args, asynchronous=None, callback_timeout=None, **kwargs):
        """Call `func` with `args` synchronously or asynchronously depending on
        the calling context"""
        callback_timeout = _parse_timedelta(callback_timeout)
        if asynchronous is None:
            asynchronous = self.asynchronous
        if asynchronous:
            future = func(*args, **kwargs)
            if callback_timeout is not None:
                future = asyncio.wait_for(future, callback_timeout)
            return future
        else:
            return sync(
                self.loop, func, *args, callback_timeout=callback_timeout, **kwargs
            )


def in_async_call(loop, default=False):
    """Whether this call is currently within an async call"""
    try:
        return loop.asyncio_loop is asyncio.get_running_loop()
    except RuntimeError:
        # No *running* loop in thread. If the event loop isn't running, it
        # _could_ be started later in this thread though. Return the default.
        if not loop.asyncio_loop.is_running():
            return default
        return False


def sync(loop, func, *args, callback_timeout=None, **kwargs):
    """
    Run coroutine in loop running in separate thread.
    """
    callback_timeout = _parse_timedelta(callback_timeout, "s")
    if loop.asyncio_loop.is_closed():
        raise RuntimeError("IOLoop is closed")

    e = threading.Event()
    main_tid = threading.get_ident()
    result = error = future = None  # set up non-locals

    @gen.coroutine
    def f():
        nonlocal result, error, future
        try:
            if main_tid == threading.get_ident():
                raise RuntimeError("sync() called from thread of running loop")
            yield gen.moment
            future = func(*args, **kwargs)
            if callback_timeout is not None:
                future = asyncio.wait_for(future, callback_timeout)
            future = asyncio.ensure_future(future)
            result = yield future
        except Exception:
            error = sys.exc_info()
        finally:
            e.set()

    def cancel():
        if future is not None:
            future.cancel()

    def wait(timeout):
        try:
            return e.wait(timeout)
        except KeyboardInterrupt:
            loop.add_callback(cancel)
            raise

    loop.add_callback(f)
    if callback_timeout is not None:
        if not wait(callback_timeout):
            raise TimeoutError(f"timed out after {callback_timeout} s.")
    else:
        while not e.is_set():
            wait(10)

    if error:
        typ, exc, tb = error
        raise exc.with_traceback(tb)
    else:
        return result


class LoopRunner:
    """
    A helper to start and stop an IO loop in a controlled way.
    Several loop runners can associate safely to the same IO loop.

    Parameters
    ----------
    loop: IOLoop (optional)
        If given, this loop will be re-used, otherwise an appropriate one
        will be looked up or created.
    asynchronous: boolean (optional, default False)
        If false (the default), the loop is meant to run in a separate
        thread and will be started if necessary.
        If true, the loop is meant to run in the thread this
        object is instantiated from, and will not be started automatically.
    """

    # All loops currently associated to loop runners
    _all_loops: ClassVar[
        weakref.WeakKeyDictionary[IOLoop, tuple[int, LoopRunner | None]]
    ] = weakref.WeakKeyDictionary()
    _lock = threading.Lock()

    def __init__(self, loop=None, asynchronous=False):
        if loop is None:
            if asynchronous:
                self._loop = IOLoop.current()
            else:
                # We're expecting the loop to run in another thread,
                # avoid re-using this thread's assigned loop
                self._loop = IOLoop()
        else:
            self._loop = loop
        self._asynchronous = asynchronous
        self._loop_thread = None
        self._started = False
        with self._lock:
            self._all_loops.setdefault(self._loop, (0, None))

    def start(self):
        """
        Start the IO loop if required.  The loop is run in a dedicated
        thread.

        If the loop is already running, this method does nothing.
        """
        with self._lock:
            self._start_unlocked()

    def _start_unlocked(self):
        assert not self._started

        count, real_runner = self._all_loops[self._loop]
        if self._asynchronous or real_runner is not None or count > 0:
            self._all_loops[self._loop] = count + 1, real_runner
            self._started = True
            return

        assert self._loop_thread is None
        assert count == 0

        loop_evt = threading.Event()
        done_evt = threading.Event()
        in_thread = [None]
        start_exc = [None]

        def loop_cb():
            in_thread[0] = threading.current_thread()
            loop_evt.set()

        def run_loop(loop=self._loop):
            loop.add_callback(loop_cb)
            # run loop forever if it's not running already
            try:
                if not loop.asyncio_loop.is_running():
                    loop.start()
            except Exception as e:
                start_exc[0] = e
            finally:
                done_evt.set()

        thread = threading.Thread(target=run_loop, name="IO loop")
        thread.daemon = True
        thread.start()

        loop_evt.wait(timeout=10)
        self._started = True

        actual_thread = in_thread[0]
        if actual_thread is not thread:
            # Loop already running in other thread (user-launched)
            done_evt.wait(5)
            if start_exc[0] is not None and not isinstance(start_exc[0], RuntimeError):
                if not isinstance(
                    start_exc[0], Exception
                ):  # track down infrequent error
                    raise TypeError(
                        f"not an exception: {start_exc[0]!r}",
                    )
                raise start_exc[0]
            self._all_loops[self._loop] = count + 1, None
        else:
            assert start_exc[0] is None, start_exc
            self._loop_thread = thread
            self._all_loops[self._loop] = count + 1, self

    def stop(self, timeout=10):
        """
        Stop and close the loop if it was created by us.
        Otherwise, just mark this object "stopped".
        """
        with self._lock:
            self._stop_unlocked(timeout)

    def _stop_unlocked(self, timeout):
        if not self._started:
            return

        self._started = False

        count, real_runner = self._all_loops[self._loop]
        if count > 1:
            self._all_loops[self._loop] = count - 1, real_runner
        else:
            assert count == 1
            del self._all_loops[self._loop]
            if real_runner is not None:
                real_runner._real_stop(timeout)

    def _real_stop(self, timeout):
        assert self._loop_thread is not None
        if self._loop_thread is not None:
            try:
                self._loop.add_callback(self._loop.stop)
                self._loop_thread.join(timeout=timeout)
                with suppress(KeyError):  # IOLoop can be missing
                    self._loop.close()
            finally:
                self._loop_thread = None

    def is_started(self):
        """
        Return True between start() and stop() calls, False otherwise.
        """
        return self._started

    def run_sync(self, func, *args, **kwargs):
        """
        Convenience helper: start the loop if needed,
        run sync(func, *args, **kwargs), then stop the loop again.
        """
        if self._started:
            return sync(self.loop, func, *args, **kwargs)
        else:
            self.start()
            try:
                return sync(self.loop, func, *args, **kwargs)
            finally:
                self.stop()

    @property
    def loop(self):
        return self._loop


@contextmanager
def set_thread_state(**kwargs):
    old = {}
    for k in kwargs:
        try:
            old[k] = getattr(thread_state, k)
        except AttributeError:
            pass
    for k, v in kwargs.items():
        setattr(thread_state, k, v)
    try:
        yield
    finally:
        for k in kwargs:
            try:
                v = old[k]
            except KeyError:
                delattr(thread_state, k)
            else:
                setattr(thread_state, k, v)


@contextmanager
def tmp_text(filename, text):
    fn = os.path.join(tempfile.gettempdir(), filename)
    with open(fn, "w") as f:
        f.write(text)

    try:
        yield fn
    finally:
        if os.path.exists(fn):
            os.remove(fn)


def is_kernel():
    """Determine if we're running within an IPython kernel

    >>> is_kernel()
    False
    """
    # http://stackoverflow.com/questions/34091701/determine-if-were-in-an-ipython-notebook-session
    if "IPython" not in sys.modules:  # IPython hasn't been imported
        return False
    from IPython import get_ipython

    # check for `kernel` attribute on the IPython instance
    return getattr(get_ipython(), "kernel", None) is not None


hex_pattern = re.compile("[a-f]+")


@functools.lru_cache(100000)
def key_split(s):
    """
    >>> key_split('x')
    'x'
    >>> key_split('x-1')
    'x'
    >>> key_split('x-1-2-3')
    'x'
    >>> key_split(('x-2', 1))
    'x'
    >>> key_split("('x-2', 1)")
    'x'
    >>> key_split("('x', 1)")
    'x'
    >>> key_split('hello-world-1')
    'hello-world'
    >>> key_split(b'hello-world-1')
    'hello-world'
    >>> key_split('ae05086432ca935f6eba409a8ecd4896')
    'data'
    >>> key_split('<module.submodule.myclass object at 0xdaf372')
    'myclass'
    >>> key_split(None)
    'Other'
    >>> key_split('x-abcdefab')  # ignores hex
    'x'
    """
    if type(s) is bytes:
        s = s.decode()
    if type(s) is tuple:
        s = s[0]
    try:
        words = s.split("-")
        if not words[0][0].isalpha():
            result = words[0].split(",")[0].strip("'(\"")
        else:
            result = words[0]
        for word in words[1:]:
            if word.isalpha() and not (
                len(word) == 8 and hex_pattern.match(word) is not None
            ):
                result += "-" + word
            else:
                break
        if len(result) == 32 and re.match(r"[a-f0-9]{32}", result):
            return "data"
        else:
            if result[0] == "<":
                result = result.strip("<>").split()[0].split(".")[-1]
            return result
    except Exception:
        return "Other"


def key_split_group(x) -> str:
    """A more fine-grained version of key_split

    >>> key_split_group(('x-2', 1))
    'x-2'
    >>> key_split_group("('x-2', 1)")
    'x-2'
    >>> key_split_group('ae05086432ca935f6eba409a8ecd4896')
    'data'
    >>> key_split_group('<module.submodule.myclass object at 0xdaf372')
    'myclass'
    >>> key_split_group('x')
    'x'
    >>> key_split_group('x-1')
    'x'
    """
    typ = type(x)
    if typ is tuple:
        return x[0]
    elif typ is str:
        if x[0] == "(":
            return x.split(",", 1)[0].strip("()\"'")
        elif len(x) == 32 and re.match(r"[a-f0-9]{32}", x):
            return "data"
        elif x[0] == "<":
            return x.strip("<>").split()[0].split(".")[-1]
        else:
            return key_split(x)
    elif typ is bytes:
        return key_split_group(x.decode())
    else:
        return "Other"


@contextmanager
def log_errors(pdb=False):
    from distributed.comm import CommClosedError

    try:
        yield
    except (CommClosedError, gen.Return):
        raise
    except Exception as e:
        try:
            logger.exception(e)
        except TypeError:  # logger becomes None during process cleanup
            pass
        if pdb:
            import pdb

            pdb.set_trace()
        raise


def silence_logging(level, root="distributed"):
    """
    Change all StreamHandlers for the given logger to the given level
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper())

    old = None
    logger = logging.getLogger(root)
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            old = handler.level
            handler.setLevel(level)

    return old


@toolz.memoize
def ensure_ip(hostname):
    """Ensure that address is an IP address

    Examples
    --------
    >>> ensure_ip('localhost')
    '127.0.0.1'
    >>> ensure_ip('')  # Maps as localhost for binding e.g. 'tcp://:8811'
    '127.0.0.1'
    >>> ensure_ip('123.123.123.123')  # pass through IP addresses
    '123.123.123.123'
    """
    if not hostname:
        hostname = "localhost"

    # Prefer IPv4 over IPv6, for compatibility
    families = [socket.AF_INET, socket.AF_INET6]
    for fam in families:
        try:
            results = socket.getaddrinfo(
                hostname, 1234, fam, socket.SOCK_STREAM  # dummy port number
            )
        except socket.gaierror as e:
            exc = e
        else:
            return results[0][4][0]

    raise exc


tblib.pickling_support.install()


def get_traceback():
    exc_type, exc_value, exc_traceback = sys.exc_info()
    bad = [
        os.path.join("distributed", "worker"),
        os.path.join("distributed", "scheduler"),
        os.path.join("tornado", "gen.py"),
        os.path.join("concurrent", "futures"),
    ]
    while exc_traceback and any(
        b in exc_traceback.tb_frame.f_code.co_filename for b in bad
    ):
        exc_traceback = exc_traceback.tb_next
    return exc_traceback


def truncate_exception(e, n=10000):
    """Truncate exception to be about a certain length"""
    if len(str(e)) > n:
        try:
            return type(e)("Long error message", str(e)[:n])
        except Exception:
            return Exception("Long error message", type(e), str(e)[:n])
    else:
        return e


def validate_key(k):
    """Validate a key as received on a stream."""
    typ = type(k)
    if typ is not str and typ is not bytes:
        raise TypeError(f"Unexpected key type {typ} (value: {k!r})")


def _maybe_complex(task):
    """Possibly contains a nested task"""
    return (
        istask(task)
        or type(task) is list
        and any(map(_maybe_complex, task))
        or type(task) is dict
        and any(map(_maybe_complex, task.values()))
    )


def seek_delimiter(file, delimiter, blocksize):
    """Seek current file to next byte after a delimiter bytestring

    This seeks the file to the next byte following the delimiter.  It does
    not return anything.  Use ``file.tell()`` to see location afterwards.

    Parameters
    ----------
    file: a file
    delimiter: bytes
        a delimiter like ``b'\n'`` or message sentinel
    blocksize: int
        Number of bytes to read from the file at once.
    """

    if file.tell() == 0:
        return

    last = b""
    while True:
        current = file.read(blocksize)
        if not current:
            return
        full = last + current
        try:
            i = full.index(delimiter)
            file.seek(file.tell() - (len(full) - i) + len(delimiter))
            return
        except ValueError:
            pass
        last = full[-len(delimiter) :]


def read_block(f, offset, length, delimiter=None):
    """Read a block of bytes from a file

    Parameters
    ----------
    f: file
        File-like object supporting seek, read, tell, etc..
    offset: int
        Byte offset to start read
    length: int
        Number of bytes to read
    delimiter: bytes (optional)
        Ensure reading starts and stops at delimiter bytestring

    If using the ``delimiter=`` keyword argument we ensure that the read
    starts and stops at delimiter boundaries that follow the locations
    ``offset`` and ``offset + length``.  If ``offset`` is zero then we
    start at zero.  The bytestring returned WILL include the
    terminating delimiter string.

    Examples
    --------

    >>> from io import BytesIO  # doctest: +SKIP
    >>> f = BytesIO(b'Alice, 100\\nBob, 200\\nCharlie, 300')  # doctest: +SKIP
    >>> read_block(f, 0, 13)  # doctest: +SKIP
    b'Alice, 100\\nBo'

    >>> read_block(f, 0, 13, delimiter=b'\\n')  # doctest: +SKIP
    b'Alice, 100\\nBob, 200\\n'

    >>> read_block(f, 10, 10, delimiter=b'\\n')  # doctest: +SKIP
    b'Bob, 200\\nCharlie, 300'
    """
    if delimiter:
        f.seek(offset)
        seek_delimiter(f, delimiter, 2**16)
        start = f.tell()
        length -= start - offset

        f.seek(start + length)
        seek_delimiter(f, delimiter, 2**16)
        end = f.tell()

        offset = start
        length = end - start

    f.seek(offset)
    bytes = f.read(length)
    return bytes


def ensure_bytes(s):
    """Attempt to turn `s` into bytes.

    Parameters
    ----------
    s : Any
        The object to be converted. Will correctly handled

        * str
        * bytes
        * objects implementing the buffer protocol (memoryview, ndarray, etc.)

    Returns
    -------
    b : bytes

    Raises
    ------
    TypeError
        When `s` cannot be converted

    Examples
    --------
    >>> ensure_bytes('123')
    b'123'
    >>> ensure_bytes(b'123')
    b'123'
    """
    if isinstance(s, bytes):
        return s
    elif hasattr(s, "encode"):
        return s.encode()
    else:
        try:
            return bytes(s)
        except Exception as e:
            raise TypeError(
                "Object %s is neither a bytes object nor has an encode method" % s
            ) from e


def open_port(host=""):
    """Return a probably-open port

    There is a chance that this port will be taken by the operating system soon
    after returning from this function.
    """
    # http://stackoverflow.com/questions/2838244/get-open-tcp-port-in-python
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((host, 0))
    s.listen(1)
    port = s.getsockname()[1]
    s.close()
    return port


def import_file(path: str):
    """Loads modules for a file (.py, .zip, .egg)"""
    directory, filename = os.path.split(path)
    name, ext = os.path.splitext(filename)
    names_to_import: list[str] = []
    tmp_python_path: str | None = None

    if ext in (".py",):  # , '.pyc'):
        if directory not in sys.path:
            tmp_python_path = directory
        names_to_import.append(name)
    if ext == ".py":  # Ensure that no pyc file will be reused
        cache_file = cache_from_source(path)
        with suppress(OSError):
            os.remove(cache_file)
    if ext in (".egg", ".zip", ".pyz"):
        if path not in sys.path:
            sys.path.insert(0, path)
        names = (mod_info.name for mod_info in pkgutil.iter_modules([path]))
        names_to_import.extend(names)

    loaded: list[ModuleType] = []
    if not names_to_import:
        logger.warning("Found nothing to import from %s", filename)
    else:
        importlib.invalidate_caches()
        if tmp_python_path is not None:
            sys.path.insert(0, tmp_python_path)
        try:
            for name in names_to_import:
                logger.info("Reload module %s from %s file", name, ext)
                loaded.append(importlib.reload(importlib.import_module(name)))
        finally:
            if tmp_python_path is not None:
                sys.path.remove(tmp_python_path)
    return loaded


def asciitable(columns, rows):
    """Formats an ascii table for given columns and rows.

    Parameters
    ----------
    columns : list
        The column names
    rows : list of tuples
        The rows in the table. Each tuple must be the same length as
        ``columns``.
    """
    rows = [tuple(str(i) for i in r) for r in rows]
    columns = tuple(str(i) for i in columns)
    widths = tuple(max(max(map(len, x)), len(c)) for x, c in zip(zip(*rows), columns))
    row_template = ("|" + (" %%-%ds |" * len(columns))) % widths
    header = row_template % tuple(columns)
    bar = "+%s+" % "+".join("-" * (w + 2) for w in widths)
    data = "\n".join(row_template % r for r in rows)
    return "\n".join([bar, header, bar, data, bar])


def nbytes(frame, _bytes_like=(bytes, bytearray)):
    """Number of bytes of a frame or memoryview"""
    if isinstance(frame, _bytes_like):
        return len(frame)
    else:
        try:
            return frame.nbytes
        except AttributeError:
            return len(frame)


def json_load_robust(fn, load=json.load):
    """Reads a JSON file from disk that may be being written as we read"""
    while not os.path.exists(fn):
        sleep(0.01)
    for i in range(10):
        try:
            with open(fn) as f:
                cfg = load(f)
            if cfg:
                return cfg
        except (ValueError, KeyError):  # race with writing process
            pass
        sleep(0.1)


class DequeHandler(logging.Handler):
    """A logging.Handler that records records into a deque"""

    _instances: ClassVar[weakref.WeakSet[DequeHandler]] = weakref.WeakSet()

    def __init__(self, *args, n=10000, **kwargs):
        self.deque = deque(maxlen=n)
        super().__init__(*args, **kwargs)
        self._instances.add(self)

    def emit(self, record):
        self.deque.append(record)

    def clear(self):
        """
        Clear internal storage.
        """
        self.deque.clear()

    @classmethod
    def clear_all_instances(cls):
        """
        Clear the internal storage of all live DequeHandlers.
        """
        for inst in list(cls._instances):
            inst.clear()


def reset_logger_locks():
    """Python 2's logger's locks don't survive a fork event

    https://github.com/dask/distributed/issues/1491
    """
    for name in logging.Logger.manager.loggerDict.keys():
        for handler in logging.getLogger(name).handlers:
            handler.createLock()


@functools.lru_cache(1000)
def has_keyword(func, keyword):
    return keyword in inspect.signature(func).parameters


@functools.lru_cache(1000)
def command_has_keyword(cmd, k):
    if cmd is not None:
        if isinstance(cmd, str):
            try:
                from importlib import import_module

                cmd = import_module(cmd)
            except ImportError:
                raise ImportError("Module for command %s is not available" % cmd)

        if isinstance(getattr(cmd, "main"), click.core.Command):
            cmd = cmd.main
        if isinstance(cmd, click.core.Command):
            cmd_params = {
                p.human_readable_name
                for p in cmd.params
                if isinstance(p, click.core.Option)
            }
            return k in cmd_params

    return False


# from bokeh.palettes import viridis
# palette = viridis(18)
palette = [
    "#440154",
    "#471669",
    "#472A79",
    "#433C84",
    "#3C4D8A",
    "#355D8C",
    "#2E6C8E",
    "#287A8E",
    "#23898D",
    "#1E978A",
    "#20A585",
    "#2EB27C",
    "#45BF6F",
    "#64CB5D",
    "#88D547",
    "#AFDC2E",
    "#D7E219",
    "#FDE724",
]


@toolz.memoize
def color_of(x, palette=palette):
    h = md5(str(x).encode())
    n = int(h.hexdigest()[:8], 16)
    return palette[n % len(palette)]


def _iscoroutinefunction(f):
    return inspect.iscoroutinefunction(f) or gen.is_coroutine_function(f)


@functools.lru_cache(None)
def _iscoroutinefunction_cached(f):
    return _iscoroutinefunction(f)


def iscoroutinefunction(f):
    # Attempt to use lru_cache version and fall back to non-cached version if needed
    try:
        return _iscoroutinefunction_cached(f)
    except TypeError:  # unhashable type
        return _iscoroutinefunction(f)


@contextmanager
def warn_on_duration(duration, msg):
    start = time()
    yield
    stop = time()
    if stop - start > _parse_timedelta(duration):
        warnings.warn(msg, stacklevel=2)


def format_dashboard_link(host, port):
    template = dask.config.get("distributed.dashboard.link")
    if dask.config.get("distributed.scheduler.dashboard.tls.cert"):
        scheme = "https"
    else:
        scheme = "http"
    return template.format(
        **toolz.merge(os.environ, dict(scheme=scheme, host=host, port=port))
    )


def parse_ports(port):
    """Parse input port information into list of ports

    Parameters
    ----------
    port : int, str, None
        Input port or ports. Can be an integer like 8787, a string for a
        single port like "8787", a string for a sequential range of ports like
        "8000:8200", or None.

    Returns
    -------
    ports : list
        List of ports

    Examples
    --------
    A single port can be specified using an integer:

    >>> parse_ports(8787)
    [8787]

    or a string:

    >>> parse_ports("8787")
    [8787]

    A sequential range of ports can be specified by a string which indicates
    the first and last ports which should be included in the sequence of ports:

    >>> parse_ports("8787:8790")
    [8787, 8788, 8789, 8790]

    An input of ``None`` is also valid and can be used to indicate that no port
    has been specified:

    >>> parse_ports(None)
    [None]

    """
    if isinstance(port, str) and ":" not in port:
        port = int(port)

    if isinstance(port, (int, type(None))):
        ports = [port]
    else:
        port_start, port_stop = map(int, port.split(":"))
        if port_stop <= port_start:
            raise ValueError(
                "When specifying a range of ports like port_start:port_stop, "
                "port_stop must be greater than port_start, but got "
                f"{port_start=} and {port_stop=}"
            )
        ports = list(range(port_start, port_stop + 1))

    return ports


is_coroutine_function = iscoroutinefunction


class Log(str):
    """A container for newline-delimited string of log entries"""

    def _repr_html_(self):
        return get_template("log.html.j2").render(log=self)


class Logs(dict):
    """A container for a dict mapping names to strings of log entries"""

    def _repr_html_(self):
        return get_template("logs.html.j2").render(logs=self)


def cli_keywords(d: dict, cls=None, cmd=None):
    """Convert a kwargs dictionary into a list of CLI keywords

    Parameters
    ----------
    d : dict
        The keywords to convert
    cls : callable
        The callable that consumes these terms to check them for validity
    cmd : string or object
        A string with the name of a module, or the module containing a
        click-generated command with a "main" function, or the function itself.
        It may be used to parse a module's custom arguments (that is, arguments that
        are not part of Worker class), such as nworkers from dask-worker CLI or
        enable_nvlink from dask-cuda-worker CLI.

    Examples
    --------
    >>> cli_keywords({"x": 123, "save_file": "foo.txt"})
    ['--x', '123', '--save-file', 'foo.txt']

    >>> from dask.distributed import Worker
    >>> cli_keywords({"x": 123}, Worker)
    Traceback (most recent call last):
    ...
    ValueError: Class distributed.worker.Worker does not support keyword x
    """
    from dask.utils import typename

    if cls or cmd:
        for k in d:
            if not has_keyword(cls, k) and not command_has_keyword(cmd, k):
                if cls and cmd:
                    raise ValueError(
                        "Neither class %s or module %s support keyword %s"
                        % (typename(cls), typename(cmd), k)
                    )
                elif cls:
                    raise ValueError(
                        f"Class {typename(cls)} does not support keyword {k}"
                    )
                else:
                    raise ValueError(
                        f"Module {typename(cmd)} does not support keyword {k}"
                    )

    def convert_value(v):
        out = str(v)
        if " " in out and "'" not in out and '"' not in out:
            out = '"' + out + '"'
        return out

    return sum(
        (["--" + k.replace("_", "-"), convert_value(v)] for k, v in d.items()), []
    )


def is_valid_xml(text):
    return xml.etree.ElementTree.fromstring(text) is not None


_offload_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Dask-Offload")
weakref.finalize(_offload_executor, _offload_executor.shutdown)


def import_term(name: str):
    """Return the fully qualified term

    Examples
    --------
    >>> import_term("math.sin") # doctest: +SKIP
    <function math.sin(x, /)>
    """
    try:
        module_name, attr_name = name.rsplit(".", 1)
    except ValueError:
        return importlib.import_module(name)

    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


async def offload(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    # Retain context vars while deserializing; see https://bugs.python.org/issue34014
    context = contextvars.copy_context()
    return await loop.run_in_executor(
        _offload_executor, lambda: context.run(fn, *args, **kwargs)
    )


class EmptyContext:
    def __enter__(self):
        pass

    def __exit__(self, *args):
        pass

    async def __aenter__(self):
        pass

    async def __aexit__(self, *args):
        pass


empty_context = EmptyContext()


class LRU(UserDict):
    """Limited size mapping, evicting the least recently looked-up key when full"""

    def __init__(self, maxsize):
        super().__init__()
        self.data = OrderedDict()
        self.maxsize = maxsize

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.data.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        if len(self) >= self.maxsize:
            self.data.popitem(last=False)
        super().__setitem__(key, value)


def clean_dashboard_address(addrs: AnyType, default_listen_ip: str = "") -> list[dict]:
    """
    Examples
    --------
    >>> clean_dashboard_address(8787)
    [{'address': '', 'port': 8787}]
    >>> clean_dashboard_address(":8787")
    [{'address': '', 'port': 8787}]
    >>> clean_dashboard_address("8787")
    [{'address': '', 'port': 8787}]
    >>> clean_dashboard_address("8787")
    [{'address': '', 'port': 8787}]
    >>> clean_dashboard_address("foo:8787")
    [{'address': 'foo', 'port': 8787}]
    >>> clean_dashboard_address([8787, 8887])
    [{'address': '', 'port': 8787}, {'address': '', 'port': 8887}]
    >>> clean_dashboard_address(":8787,:8887")
    [{'address': '', 'port': 8787}, {'address': '', 'port': 8887}]
    """

    if default_listen_ip == "0.0.0.0":
        default_listen_ip = ""  # for IPV6

    if isinstance(addrs, str):
        addrs = addrs.split(",")
    if not isinstance(addrs, list):
        addrs = [addrs]

    addresses = []
    for addr in addrs:
        try:
            addr = int(addr)
        except (TypeError, ValueError):
            pass

        if isinstance(addr, str):
            addr = addr.split(":")

        if isinstance(addr, (tuple, list)):
            if len(addr) == 2:
                host, port = (addr[0], int(addr[1]))
            elif len(addr) == 1:
                [host], port = addr, 0
            else:
                raise ValueError(addr)
        elif isinstance(addr, int):
            host = default_listen_ip
            port = addr

        addresses.append({"address": host, "port": port})
    return addresses


_deprecations = {
    "deserialize_for_cli": "dask.config.deserialize",
    "serialize_for_cli": "dask.config.serialize",
    "format_bytes": "dask.utils.format_bytes",
    "format_time": "dask.utils.format_time",
    "funcname": "dask.utils.funcname",
    "parse_bytes": "dask.utils.parse_bytes",
    "parse_timedelta": "dask.utils.parse_timedelta",
    "typename": "dask.utils.typename",
    "tmpfile": "dask.utils.tmpfile",
}


def __getattr__(name):
    if name in _deprecations:
        use_instead = _deprecations[name]

        warnings.warn(
            f"{name} is deprecated and will be removed in a future release. "
            f"Please use {use_instead} instead.",
            category=FutureWarning,
            stacklevel=2,
        )
        return import_term(use_instead)
    else:
        raise AttributeError(f"module {__name__} has no attribute {name}")


# Used internally by recursive_to_dict to stop infinite recursion. If an object has
# already been encountered, a string representation will be returned instead. This is
# necessary since we have multiple cyclic referencing data structures.
_recursive_to_dict_seen: ContextVar[set[int]] = ContextVar("_recursive_to_dict_seen")
_to_dict_no_nest_flag = False


def recursive_to_dict(
    obj: AnyType, *, exclude: Container[str] = (), members: bool = False
) -> AnyType:
    """Recursively convert arbitrary Python objects to a JSON-serializable
    representation. This is intended for debugging purposes only.

    The following objects are supported:

    list, tuple, set, frozenset, deque, dict, dict_keys, dict_values
        Descended into these objects recursively. Python-specific collections are
        converted to JSON-friendly variants.
    Classes that define ``_to_dict(self, *, exclude: Container[str] = ())``:
        Call the method and dump its output
    Classes that define ``_to_dict_no_nest(self, *, exclude: Container[str] = ())``:
        Like above, but prevents nested calls (see below)
    Other Python objects
        Dump the output of ``repr()``
    Objects already encountered before, regardless of type
        Dump the output of ``repr()``. This breaks circular references and shortens the
        output.

    Parameters
    ----------
    exclude:
        A list of attribute names to be excluded from the dump.
        This will be forwarded to the objects ``_to_dict`` methods and these methods
        are required to accept this parameter.
    members:
        If True, convert the top-level Python object to a dict of its public members

    **``_to_dict_no_nest`` vs. ``_to_dict``**

    The presence of the ``_to_dict_no_nest`` method signals ``recursive_to_dict`` to
    have a mutually exclusive full dict representation with other objects that also have
    the ``_to_dict_no_nest``, regardless of their class. Only the outermost object in a
    nested structure has the method invoked; all others are
    dumped as their string repr instead, even if they were not encountered before.

    Example:

    .. code-block:: python

        >>> class Person:
        ...     def __init__(self, name):
        ...         self.name = name
        ...         self.children = []
        ...         self.pets = []
        ...
        ...     def _to_dict_no_nest(self, exclude=()):
        ...         return recursive_to_dict(self.__dict__, exclude=exclude)
        ...
        ...     def __repr__(self):
        ...         return self.name

        >>> class Pet:
        ...     def __init__(self, name):
        ...         self.name = name
        ...         self.owners = []
        ...
        ...     def _to_dict_no_nest(self, exclude=()):
        ...         return recursive_to_dict(self.__dict__, exclude=exclude)
        ...
        ...     def __repr__(self):
        ...         return self.name

        >>> alice = Person("Alice")
        >>> bob = Person("Bob")
        >>> charlie = Pet("Charlie")
        >>> alice.children.append(bob)
        >>> alice.pets.append(charlie)
        >>> bob.pets.append(charlie)
        >>> charlie.owners[:] = [alice, bob]
        >>> recursive_to_dict({"people": [alice, bob], "pets": [charlie]})
        {
            "people": [
                {"name": "Alice", "children": ["Bob"], "pets": ["Charlie"]},
                {"name": "Bob", "children": [], "pets": ["Charlie"]},
            ],
            "pets": [
                {"name": "Charlie", "owners": ["Alice", "Bob"]},
            ],
        }

    If we changed the methods to ``_to_dict``, the output would instead be:

    .. code-block:: python

        {
            "people": [
                {
                    "name": "Alice",
                    "children": [
                        {
                            "name": "Bob",
                            "children": [],
                            "pets": [{"name": "Charlie", "owners": ["Alice", "Bob"]}],
                        },
                    ],
                    pets: ["Charlie"],
                ],
                "Bob",
            ],
            "pets": ["Charlie"],
        }

    Also notice that, if in the future someone will swap the creation of the
    ``children`` and ``pets`` attributes inside ``Person.__init__``, the output with
    ``_to_dict`` will change completely whereas the one with ``_to_dict_no_nest`` won't!
    """
    if isinstance(obj, (int, float, bool, str)) or obj is None:
        return obj
    if isinstance(obj, (type, bytes)):
        return repr(obj)

    if members:
        obj = {
            k: v
            for k, v in inspect.getmembers(obj)
            if not k.startswith("_") and k not in exclude and not callable(v)
        }

    # Prevent infinite recursion
    try:
        seen = _recursive_to_dict_seen.get()
    except LookupError:
        seen = set()
    seen = seen.copy()
    tok = _recursive_to_dict_seen.set(seen)
    try:
        if id(obj) in seen:
            return repr(obj)

        if hasattr(obj, "_to_dict_no_nest"):
            global _to_dict_no_nest_flag
            if _to_dict_no_nest_flag:
                return repr(obj)

            seen.add(id(obj))
            _to_dict_no_nest_flag = True
            try:
                return obj._to_dict_no_nest(exclude=exclude)
            finally:
                _to_dict_no_nest_flag = False

        seen.add(id(obj))

        if hasattr(obj, "_to_dict"):
            return obj._to_dict(exclude=exclude)
        if isinstance(obj, (list, tuple, set, frozenset, deque, KeysView, ValuesView)):
            return [recursive_to_dict(el, exclude=exclude) for el in obj]
        if isinstance(obj, dict):
            res = {}
            for k, v in obj.items():
                k = recursive_to_dict(k, exclude=exclude)
                v = recursive_to_dict(v, exclude=exclude)
                try:
                    res[k] = v
                except TypeError:
                    res[str(k)] = v
            return res

        return repr(obj)
    finally:
        tok.var.reset(tok)
