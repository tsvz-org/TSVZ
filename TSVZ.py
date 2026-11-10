#! /usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
# /// script
# requires-python = ">=3.6"
# dependencies = [
# ]
# ///
"""TSVZ: a key-value store kept in a delimiter-separated text file.

Plain ``.tsv`` / ``.csv`` / ``.nsv`` / ``.psv`` files (and any other
extension) keep the exact TSVZ 3.39 format. ``.tsvz`` / ``.csvz`` / ``.nsvz``
/ ``.psvz`` files follow tsvz-spec-v1 (tsvz-spec-v1.md). Both use the same
API: ``TSVZed``, ``TSVZedLite``, ``readTabularFile`` and friends.
"""
import array
import atexit
import bisect
import codecs
import contextlib
import errno
import functools
import hashlib
import io
import itertools
import os
import re
import shutil
import stat
import threading
import _thread
import time
import sys
import weakref
from collections import OrderedDict, deque, namedtuple
from collections.abc import MutableMapping
RESOURCE_LIB_AVAILABLE = True
try:
	import resource
except ImportError:
	RESOURCE_LIB_AVAILABLE = False

if os.name == 'nt':
	import msvcrt
elif os.name == 'posix':
	import fcntl

version = '5.13'
__version__ = version
author = 'pan@zopyr.us'
COMMIT_DATE = '2026-11-09'

DEFAULT_DELIMITER = '\t'
DEFAULTS_INDICATOR_KEY = '#_defaults_#'

#: Compression suffix -> codec name (spec §16.2). Code dispatches on the name, so each spelling of a codec acts alike.
_CODEC_NAMES = {'gz': 'gzip', 'gzip': 'gzip', 'bz2': 'bzip2', 'bzip2': 'bzip2', 'xz': 'xz', 'lzma': 'xz', 'zst': 'zstd', 'zstd': 'zstd'}
COMPRESSED_FILE_EXTENSIONS = list(_CODEC_NAMES)
#: Codec name -> (its module, the module's decompressor class, the keyword open() and compress() take for
#: the level, the bytes each member begins with). gzip members are decompressed with zlib, one at a time.
_CODECS = {
	'gzip': ('gzip', None, 'compresslevel', b'\x1f\x8b\x08'),
	'bzip2': ('bz2', 'BZ2Decompressor', 'compresslevel', b'BZh'),
	'xz': ('lzma', 'LZMADecompressor', 'preset', b'\xfd7zXZ\x00'),
	'zstd': ('compression.zstd', 'ZstdDecompressor', 'level', b'\x28\xb5\x2f\xfd'),  # Python 3.14+
}


def _codecModule(codec):
	"""The module for ``codec`` (a suffix or a name): gzip, bz2, lzma or compression.zstd."""
	import importlib
	return importlib.import_module(_CODECS[_CODEC_NAMES.get(codec, codec)][0])

def _openStreams(codec, f):
	"""The text of ``codec``'s members in the open binary file ``f`` (at offset 0), as a binary reader that is
	closed without closing ``f``: the members that pass their check, one after another (_checkedMembers). At the
	end of the text it raises _DamagedStream when a member was left out."""
	_codecModule(codec)  # ImportError now for a codec this Python lacks (zstd before 3.14), not at the first read
	return _MemberReader(_MemberStream(f, codec), 1 << 16)


class _DamagedStream(Exception):
	"""Raised by a _MemberReader at the end of its text when a member was damaged: what was left out, and where."""


def _streamDamage(e):
	"""The note a read makes for the error ``e`` reading a compressed legacy file."""
	if isinstance(e, _DamagedStream):
		return 'compressed stream is damaged: {}'.format(e)
	return 'compressed stream is damaged ({}: {}); read up to the damage'.format(type(e).__name__, e)


#: How many times _checkedMembers moves the end of a cut member on to the next magic bytes (see there).
_CUT_EXTENSIONS = 8


def _skipZeros(src):
	"""Skip the zero bytes at the current offset of ``src`` (a ``_PartBytes``): xz stream padding."""
	while True:
		piece = src.take(1 << 16)
		if not piece:
			return
		zeros = len(piece) - len(bytes(piece).lstrip(b'\0'))
		if zeros < len(piece):
			src.giveBack(len(piece) - zeros)
			return


def _checkedMembers(f, codec, damage):
	"""Yield the text of each member (gzip member, bzip2 or xz stream, zstd frame) of ``codec`` in the open binary
	file ``f`` (at offset 0; it is seeked) once that member has decoded to its end and passed its check, and append
	to ``damage`` a note for each member left out.

	Why: a codec's own reader (GzipFile, BZ2File, LZMAFile, ZstdFile) hands a member's text over as it decodes it
	and checks it only at the member's end. A writer killed mid-append leaves its member cut short; once another
	member is appended after it, that reader decodes the next member's bytes as the rest of the cut one, and its
	last line can end in a garbage value before the check fails. Here a member is held whole (_readMember: memory
	grows with the largest member, which TSVZ writes at most 1 MiB of text into) and used only once it is complete.
	Zero bytes before a gzip member, or after an xz stream (stream padding), are padding, not damage.

	A member that is not complete is damaged. Its bytes run to the next offset after its start where a member of
	the codec begins (its magic bytes: gzip 1F 8B 08, bzip2 'BZh', xz FD '7zXZ' 00, zstd 28 B5 2F FD), as spec
	§16.6 has them.

	- The first member of the file damaged stops the reading there, as a codec's reader does: nothing is read. A
	  whole rewrite killed part way leaves its new first member cut short and then the old file's bytes, its old
	  members among them, which hold rows as they were (deleted keys too); a killed append can leave it only to a
	  file that held nothing.
	- A later member whose bytes, decoded on their own, merely end before the member does, is a member cut short:
	  what a killed append leaves, whose rows its writer was never told were saved. It is left out whole and
	  reading goes on at the next member. A rewrite killed inside a later member's write leaves the same shape, and
	  the old members after it are then read: decoding cannot tell the two apart (docs/limitations.md).
	- Magic bytes inside a cut member's own compressed bytes (about one in 2^24 a byte for gzip) end it early: when
	  what begins there is damaged too and its own bytes do not decode as cut short, the cut member's bytes are
	  taken to the magic bytes after it, and they must still decode as cut short (at most _CUT_EXTENSIONS times in
	  a row). A damaged member's own bytes are tried first: two killed appends in a row are two cut members.
	- Any other damage (bytes that do not decode, a member that fails its check) stops the reading there, as a
	  codec's reader does: what follows is not read.

	A zstd frame written without a content checksum (TSVZ always writes one, spec §16.8) is complete once it
	decodes to its end.
	"""
	src = _PartBytes(f)
	name = _CODEC_NAMES[codec]
	first = True  # no member read yet
	cut = None  # where the member cut short just left out begins, while no member has been read after it
	extended = 0
	while True:
		if name == 'xz':
			_skipZeros(src)
		status, output, start, end, error = _readMember(src, codec)
		if status == 'none':
			return
		if status == 'complete':
			first, cut, extended = False, None, 0
			if output:
				yield output
			continue
		if first:
			damage.append('the first member is cut short or does not decode ({}): reading stopped there; a scrub '
						  '(tsvz scrub, scrubTabularFile) makes the file again, as an empty store with its header'.format(
				error or 'it ends before the member does'))
			return
		following = _nextMemberStart(f, codec, start + 1)
		if following is None:  # the last member: nothing after it to read
			damage.append('the member at byte {} is cut short: left out'.format(start) if status == 'short' else
						  'the bytes at byte {} do not decode ({}): left out'.format(start, error))
			return
		cutShort, size, error = _cutShortTo(f, codec, start, following)
		if cutShort:
			damage.append('the member at byte {} is cut short: left out, the rest is read'.format(start))
			cut, extended = start, 0
		elif cut is not None and extended < _CUT_EXTENSIONS and _cutShortTo(f, codec, cut, following)[0]:
			extended += 1  # magic bytes inside the cut member before: its bytes run on to the next magic
		else:
			damage.append('the {} bytes at byte {} do not decode ({}): reading stopped there'.format(size, start, error))
			return
		src.seek(following)


def _cutShortTo(f, codec, begin, end):
	"""``(cutShort, size, error)``: whether the bytes of ``f`` from ``begin`` to ``end``, decoded on their own (spec
	§16.6 rule 2; gzip zero padding that ends them left out), merely end before their member does."""
	f.seek(begin)
	raw = f.read(end - begin)
	cutShort, _, error = _decodeAlone(_gzipUnpad(raw) if _CODEC_NAMES[codec] == 'gzip' else raw, codec)
	return cutShort, len(raw), error


def _firstLineIn(f, codec):
	"""Line 1 (bytes, with its newline) of the members of ``codec`` in the open binary file ``f`` (at offset 0; it is
	seeked), as _checkedMembers reads it, without holding the first member: it is decoded in pieces and only line 1
	of it kept, and handed over once the member has passed its check, so a header check of a file another tool
	compressed whole costs its decoding, not its text in memory. When the first member does not end line 1 (or
	is damaged), the members are read as _openStreams reads them (raising _DamagedStream at the end)."""
	src = _PartBytes(f, block=1 << 16)
	decompressor = None
	line, ended = [], False
	while True:
		piece = src.take(1 << 12)  # small pieces: what one decodes to is held (xz text can shrink 40-fold)
		if not piece:
			break
		if decompressor is None:
			piece = piece[_gzipPadding(piece, codec):]
			if not piece:
				continue
			decompressor = _newDecompressor(codec)
		try:
			data = decompressor.decompress(piece)
		except Exception:
			break
		if not ended:
			nl = data.find(b'\n')
			line.append(data if nl < 0 else data[:nl + 1])
			ended = nl >= 0
		if decompressor.eof:
			if ended:
				return b''.join(line)
			break
	f.seek(0)
	stream = _openStreams(codec, f)
	try:
		return stream.readline()
	finally:
		stream.close()


class _MemberStream(io.RawIOBase):
	"""The raw reader under a _MemberReader: the text of the members of ``codec`` in the open binary file ``f`` that
	pass their check (_checkedMembers), with what was left out in ``damage``. Closing it closes ``f`` only when
	``owned``. It seeks as a codec's reader does, by reading again (from the end: by reading to it first)."""

	def __init__(self, f, codec, owned=False):
		self.f, self.codec, self.owned = f, codec, owned
		self.name = getattr(f, 'name', None)
		self._restart()

	def _restart(self):
		self.damage = []
		self._members = _checkedMembers(self.f, self.codec, self.damage)
		self._out = memoryview(b'')
		self._pos = 0

	def readable(self):
		return True

	def seekable(self):
		return True

	def readinto(self, b):
		while not len(self._out):
			output = next(self._members, None)
			if output is None:
				return 0
			self._out = memoryview(output)
		n = min(len(b), len(self._out))
		b[:n] = self._out[:n]
		self._out = self._out[n:]
		self._pos += n
		return n

	def tell(self):
		return self._pos

	def seek(self, offset, whence=io.SEEK_SET):
		if whence == io.SEEK_CUR:
			offset += self._pos
		elif whence == io.SEEK_END:
			while self.readinto(bytearray(1 << 20)):
				pass
			offset += self._pos
		elif whence != io.SEEK_SET:
			raise ValueError('invalid whence ({!r})'.format(whence))
		offset = max(offset, 0)
		if offset < self._pos:
			self.f.seek(0)
			self._restart()
		while self._pos < offset and self.readinto(bytearray(min(1 << 20, offset - self._pos))):
			pass
		return self._pos

	def fileno(self):
		return self.f.fileno()

	def close(self):
		if not self.closed and self.owned:
			self.f.close()
		super(_MemberStream, self).close()


class _MemberReader(io.BufferedReader):
	"""A buffered _MemberStream whose read(), read1() and readline() (iteration too) raise _DamagedStream where they
	would return nothing at the end of the text, when a member was left out: every member that passed its check
	has been read by then, so a read that takes the error for damage has missed none of them. A read() of the rest
	(no size) returns it all, and raises only when called again."""

	def _end(self, data, size):
		if not data and size != 0 and self.raw.damage:
			raise _DamagedStream('; '.join(self.raw.damage))
		return data

	def read(self, size=-1):
		return self._end(super(_MemberReader, self).read(size), size)

	def read1(self, size=-1):
		return self._end(super(_MemberReader, self).read1(size), size)

	def readline(self, size=-1):
		return self._end(super(_MemberReader, self).readline(size), size)


def _fileCodec(fileName):
	"""The codec name ``fileName``'s compression suffix names, or None for a file that is not compressed."""
	return _CODEC_NAMES.get(fileName.rpartition('.')[2].lower())

def _isCompressedFile(fileName):
	return _fileCodec(fileName) is not None

def get_delimiter(delimiter = ...,file_name = ''):
	"""
	Resolve the delimiter to use for an operation.

	When the delimiter is not provided (the ``...`` sentinel or a falsy value),
	it is determined automatically: inferred from the file name when one is
	available (a trailing compression extension such as ``.gz`` is ignored for
	the purpose of inference), otherwise falling back to the module default
	(``DEFAULT_DELIMITER``, a tab). Explicit delimiters (including the aliases
	'comma', 'tab', 'pipe', 'null' and escape sequences) are normalized.

	This function is pure: it has no side effects and never mutates module
	state, so concurrent callers operating on different file types do not
	interfere with one another.

	Parameters:
	- delimiter: The delimiter or alias, or ``...``/``None`` to auto-determine.
	- file_name (str, optional): The file name to infer the delimiter from.

	Returns:
	str: The resolved delimiter.
	"""
	if delimiter is ...:
		if not file_name:
			# Nothing to infer from; fall back to the module default.
			return DEFAULT_DELIMITER
		name = _parsePartName(file_name)
		if name.ext in STRICT_EXTENSIONS:
			return _EXTENSION_DELIMITERS[name.ext]
		# Ignore a trailing compression extension so 'data.csv.gz' still
		# infers ',' rather than the tab fallback.
		lowerName = file_name.lower()
		base, _, ext = lowerName.rpartition('.')
		if ext in COMPRESSED_FILE_EXTENSIONS:
			lowerName = base
		if lowerName.endswith('.csv'):
			return ','
		elif lowerName.endswith('.nsv'):
			return '\0'
		elif lowerName.endswith('.psv'):
			return '|'
		else:
			return '\t'
	elif not delimiter:
		return DEFAULT_DELIMITER
	elif delimiter == 'comma':
		return ','
	elif delimiter == 'tab':
		return '\t'
	elif delimiter == 'pipe':
		return '|'
	elif delimiter == 'null':
		return '\0'
	else:
		return delimiter.encode().decode('unicode_escape')

#: The exit has let go of every store (_closeStreamsToDaemons): from here on a daemon thread stays out of the streams (_say).
_daemonsOut = False
#: The daemon threads inside a print (_say).
_daemonsPrinting = set()
#: How long the exit waits for a daemon thread's print: bounded, a reader of the stream may never read it.
_DAEMON_PRINT_WAIT = 5.0


def _say(write, message):
	"""Run ``write()``, the print of ``message``: the prints of a store's thread pass here (``eprint``, ``_warn``, the
	store's own). Not ``format_bytes`` and ``get_resource_usage``, which no store thread calls, nor the ``logger.teelog`` calls
	of ``tsvz serve``'s handler, which has its own threads. The interpreter's end freezes a
	daemon thread (a store's writer) where it stands, and one frozen inside a print keeps the stream's lock, so the last
	flush dies with ``Fatal Python error: _enter_buffered_busy``. Once the exit has passed _closeStreamsToDaemons a daemon
	thread never enters a stream: ``message`` goes to file descriptor 2 by a system call, which holds no lock."""
	me = threading.current_thread()
	if not me.daemon:
		return write()
	_daemonsPrinting.add(me)  # (before the look at _daemonsOut, which the exit sets before it looks here)
	try:
		if not _daemonsOut:
			return write()
	finally:
		_daemonsPrinting.discard(me)
	try:
		os.write(2, (message + '\n').encode('utf8', 'replace'))
	except OSError:
		pass


def _closeStreamsToDaemons(wait=None):
	"""Keep daemon threads out of the streams from here to the interpreter's end (_say), and wait for a print under way,
	``wait`` seconds (None: _DAEMON_PRINT_WAIT) at most (is_alive(): a thread a fork left behind in one is not there to end it)."""
	global _daemonsOut
	_daemonsOut = True
	deadline = time.monotonic() + (_DAEMON_PRINT_WAIT if wait is None else wait)
	while any(thread.is_alive() for thread in list(_daemonsPrinting)) and time.monotonic() < deadline:
		time.sleep(0.001)


def eprint(*args, **kwargs):
	def write():
		try:
			if 'file' in kwargs:
				print(*args, **kwargs)
			else:
				print(*args, file=sys.stderr, **kwargs)
		except Exception as e:
			print(f"Error: Cannot print to stderr: {e}")
			print(*args, **kwargs)
	_say(write, ' '.join(str(arg) for arg in args))

def _codecWriteOptions(codec, compressLevel):
	"""The keyword arguments ``codec``'s ``open()`` and ``compress()`` take to write at ``compressLevel``."""
	codec = _CODEC_NAMES.get(codec, codec)
	if codec == 'zstd':  # a content checksum on every frame (spec §16.8)
		parameter = _codecModule(codec).CompressionParameter
		return {'options': {parameter.compression_level: compressLevel, parameter.checksum_flag: 1}}
	return {_CODECS[codec][2] if codec else 'compresslevel': compressLevel}


def _openUntruncated(fileName):
	"""Open ``fileName`` for reading and writing at offset 0, creating it when missing, without emptying it.

	A rewrite opens it so and empties it only once it holds the file lock: opening it with 'wb' empties it
	before the lock is taken, under another writer that holds it, and for good when the lock then fails.
	"""
	return open(fileName, 'r+b', opener=lambda path, flags: os.open(path, flags | os.O_CREAT, 0o666))


def _compressedWriter(fileName, raw, compressLevel=1):
	"""A writer that compresses into the open file ``raw`` with the codec ``fileName`` names, or None for none.
	Its ``_tsvzRaw`` is ``raw``, which closing it does not close (release_file_obj closes both)."""
	codec = _fileCodec(fileName)
	if not codec:
		return None
	writer = _codecModule(codec).open(raw, 'wb', **_codecWriteOptions(codec, compressLevel))
	writer._tsvzRaw = raw
	return writer


class _DropKey(list):
	"""An item of a legacy store's queue: ``[key]``, a delete a line cannot say (a lone key is a row in a one-column
	file). A whole read's replay applies it as a delete; the commit that meets it makes the rewrite that takes every
	line of the key out of the file (_legacyDropLines)."""


class _RewriteRows(object):
	"""What a legacy rewrite writes after its header: the lines ``first`` (bytes), then the rows of a flat snapshot
	of memory, each encoded as it is written, then the lines ``tail`` (bytes), all without their newline; '#'-prefixed
	keys are in-memory only (comments / internal), never written.

	The snapshot is every row's cells in one list (``cells``) and each row's count in an array (``lengths``), made
	from ``values`` (the rows, listed under _memLock; those of '#' keys left out) in one pass that takes each
	row's cells once, so the two always agree: 8 bytes a cell and 4 a row, not the rows' bytes. check() encodes
	every row of it, dropping each, and raises the TSVZEncodeError of one that cannot be written, before anything
	is: it encodes the cells as they are, joined, and sanitizes (_sanitize) only a row that fails so, for the error
	naming its cell (sanitizing replaces only characters every encoding has with ASCII tokens, so it changes no
	row's fate, and the warnings it prints for a reserved token are printed once, by the write). Under an
	``errors`` handler that never raises, there is nothing to check.

	The write encodes the same cells again, so it writes exactly what was checked: a row another thread changes
	in place after the snapshot is written as it was then.
	"""

	widened = False  # the lines were widened to the store's header (_legacyWidened): memory reads the file again after

	def __init__(self, first, values, delimiter, encoding, errors, header, tail=()):
		self.first = first
		self.tail = tail  # lines written after every row
		cells = []
		lengths = array.array('I')
		extend, append = cells.extend, lengths.append
		for value in values:
			if value and not str(value[0]).startswith('#'):
				before = len(cells)
				extend(value)
				append(len(cells) - before)
		self.cells = cells
		self.lengths = lengths
		self.encode = functools.partial(_encodeRow, delimiter=delimiter, encoding=encoding, errors=errors, header=header)
		self.delimiter = delimiter
		self.encoding = encoding or 'utf8'
		self.errors = errors

	def _rows(self):
		"""Each row of the snapshot, as a tuple or a list of its cells."""
		cells, lengths = self.cells, self.lengths
		if lengths and lengths.count(lengths[0]) == len(lengths):
			return zip(*[iter(cells)] * lengths[0])  # every row as wide (the column count): cut in C
		return self._slices()

	def _slices(self):
		cells = self.cells
		start = 0
		for count in self.lengths:
			end = start + count
			yield cells[start:end]
			start = end

	def check(self):
		if not _mayRaise(self.errors):
			return
		encode, delimiter, encoding, errors = self.encode, self.delimiter, self.encoding, self.errors
		for row in self._rows():
			try:
				delimiter.join(row).encode(encoding, errors)
			except (UnicodeEncodeError, TypeError):
				encode(_sanitize(row,delimiter=delimiter))  # raises naming the cell, unless sanitizing it helped

	def __iter__(self):
		for line in self.first:
			yield line
		encode, delimiter = self.encode, self.delimiter
		for row in self._rows():
			yield encode(_sanitize(row,delimiter=delimiter))
		for line in self.tail:
			yield line


class _LinesFile(object):
	"""The lines ``lines`` (bytes, without their newline) read as an open binary file is (readline, iteration, tell),
	without joining them into a second copy."""

	def __init__(self, lines):
		self._lines = lines
		self._next = 0
		self._pos = 0

	def readline(self, *args):
		if self._next >= len(self._lines):
			return b''
		line = self._lines[self._next] + b'\n'
		self._next += 1
		self._pos += len(line)
		return line

	def __iter__(self):
		return self

	def __next__(self):
		line = self.readline()
		if not line:
			raise StopIteration
		return line

	def tell(self):
		return self._pos


def _fileDefaultsAtEnd(lines, width, delimiter, encoding, consumed, strict, start=()):
	"""The defaults the legacy ``lines`` leave in effect at their end, read from ``start`` (none: what the lines
	themselves say; a store's given defaults: what its reader ends with): their #_defaults_# lines alone, in order,
	through _processLine at ``width`` and ``strict``, as a store's reader takes them; line 1 left out when that reader
	takes it for its header (``consumed``). Without the empty cells at the end; [] when they set none."""
	defaults = list(start)
	enc = encoding or 'utf8'
	for line in (itertools.islice(lines, 1, None) if consumed else lines):
		if line.lstrip(b'\x00').startswith(DEFAULTS_INDICATOR_KEY.encode(enc)):
			_processLine(line.decode(enc, errors='replace') + '\n', {}, width, strict=strict, delimiter=delimiter,
						 defaults=defaults)  # (a line with its newline, as the reader gets it)
	return _trimmedCells(defaults)


def _trimmedCells(cells):
	"""``cells`` without the empty cells at its end."""
	cells = list(cells)
	while cells and not cells[-1]:
		cells.pop()
	return cells


def _fileIdentity(st):
	"""``(st_dev, st_ino)`` of the os.stat_result ``st``: which file it is; (None, None) where the platform gives no
	inode number."""
	return (st.st_dev, st.st_ino) if st.st_ino else (None, None)


#: What a legacy store knows its file holds (TSVZed._known) when it knows the file holds nothing, whichever file it is.
_KNOWN_NOTHING = ((None, None), 0, hashlib.sha256())


class _HashingReader(object):
	"""The open binary file ``f``, read through it: what is read is hashed (``digest``) and counted (``size``), so
	that what a read parsed is what the store then knows the file holds, byte for byte, even when another writer
	appends meanwhile. A codec's reader reads through it too."""

	def __init__(self, f):
		self.f = f
		self.digest = hashlib.sha256()
		self.size = 0
		self.at = 0  # the offset in ``f``: a codec's reader seeks back (_checkedMembers), and bytes are hashed once

	def _took(self, data):
		new = self.at + len(data) - self.size  # bytes past the most read so far
		self.at += len(data)
		if new > 0:
			self.digest.update(data if new == len(data) else data[len(data) - new:])
			self.size += new
		return data

	def seek(self, offset, whence=io.SEEK_SET):
		"""Seek ``f``, back to bytes already read (they are not hashed again) or to the end of what was read."""
		self.at = self.f.seek(offset, whence)
		return self.at

	def read(self, size=-1):
		return self._took(self.f.read(size))

	def readline(self, size=-1):
		return self._took(self.f.readline(size))

	def __iter__(self):
		return self

	def __next__(self):
		line = self.readline()
		if not line:
			raise StopIteration
		return line

	def tell(self):
		return self.f.tell()


def _fileKnown(fd, end=None):
	"""``(identity, size, sha256)`` of the first ``end`` bytes (default: all) the open file descriptor ``fd`` holds,
	read through it: a buffer over it, which a failed write may have left holding bytes, is not flushed (the file is
	closed right after)."""
	st = os.fstat(fd)
	end = st.st_size if end is None else end
	digest = hashlib.sha256()
	size = 0
	os.lseek(fd, 0, os.SEEK_SET)
	while size < end:
		chunk = os.read(fd, min(1 << 20, end - size))
		if not chunk:
			break
		digest.update(chunk)
		size += len(chunk)
	return _fileIdentity(st), size, digest


def _readAfter(f, known):
	"""``(rest, known, endsLine)``: what the open binary file ``f`` holds after the bytes ``known`` (``(identity,
	size, sha256)``) says it holds, what is known of it once that is read, and whether those bytes end a line (or
	are none). None when ``known`` is None, or the file is not one that begins with them: another file (an
	identity of None stands for any), shorter, or other bytes there."""
	if known is None:
		return None
	identity, size, digest = known
	if identity[1] is not None and _fileIdentity(os.fstat(f.fileno())) != identity:
		return None
	reader = _HashingReader(f)
	f.seek(0)
	last = b''
	while reader.size < size:
		chunk = reader.read(min(1 << 20, size - reader.size))
		if not chunk:
			return None  # shorter now
		last = chunk[-1:]
	if reader.digest.digest() != digest.digest():
		return None
	rest = reader.read()
	return rest, (_fileIdentity(os.fstat(f.fileno())), reader.size, reader.digest), not size or last == b'\n'


def _decodeMembers(codec, data):
	"""``(text, damage)``: what ``data`` (members or frames of ``codec`` one after another, such as what other
	processes appended to a legacy file after a damage) holds, read as a compressed legacy file is read
	(_checkedMembers: the members that pass their check), and a note for each place it is damaged (an empty list
	when it is not)."""
	damage = []
	return b''.join(_checkedMembers(io.BytesIO(data), codec, damage)), damage

# What fsync(2) fails with on a file it cannot sync, whose data is written all the same: EINVAL and EROFS ("fd is
# bound to a special file which does not support synchronization"), ENOTSUP / EOPNOTSUPP (some FUSE and network
# file systems). Not EIO, ENOSPC or EDQUOT: those mean the data may not be on disk.
_FSYNC_UNSUPPORTED = frozenset([errno.EINVAL, errno.EROFS] + [getattr(errno, name) for name in ('ENOTSUP', 'EOPNOTSUPP') if hasattr(errno, name)])


def _fsync(fd):
	"""os.fsync(fd), which does nothing where the file system cannot sync (_FSYNC_UNSUPPORTED)."""
	try:
		os.fsync(fd)
	except OSError as e:
		if e.errno not in _FSYNC_UNSUPPORTED:
			raise


def _writeLines(file, header, lines, chunk=1 << 20, codec=None):
	"""Write ``header`` (None for none) and ``lines`` to ``file``, each with its newline, ``chunk`` bytes at a time;
	compressed in ``codec`` when given, each chunk then a whole member, as appends write them.

	Joined here rather than through a buffered writer: nothing is left in a buffer of its own that a failure would
	drop when it is collected, or that would write into a file already closed; a header alone is one write.
	"""
	write = file.write if codec is None else (lambda data: file.write(_compressBytes(codec, data)))
	parts = [] if header is None else [header, b'\n']
	size = 0 if header is None else len(header) + 1
	for line in lines:
		parts.append(line)
		parts.append(b'\n')
		size += len(line) + 1
		if size >= chunk:
			write(b''.join(parts))
			parts = []
			size = 0
	if parts:
		write(b''.join(parts))


def _closeUnflushed(f):
	"""Close the open file ``f`` without writing what its buffer still holds (after a write that failed)."""
	inner = getattr(f, 'raw', None)  # the descriptor under a buffered file: closing it closes both
	(inner if inner is not None else f).close()


def openFileAsCompressed(fileName,mode = 'rb',encoding = 'utf8',teeLogger = None,compressLevel = 1):
	codec = _fileCodec(fileName)
	if codec and any(c in mode for c in 'wax+'):
		# Writing plain bytes into a compressed file destroys it: refuse when this Python lacks the codec.
		_requireCodec(codec)
	if 'b' not in mode:
		mode += 't'
	kwargs = _codecWriteOptions(codec, compressLevel) if 'r' not in mode else {}
	if 'b' not in mode:
		kwargs['encoding'] = encoding
	if codec:
		try:
			if not any(c in mode for c in 'wax+'):  # a read: member by member, each once its check passes
				raw = open(fileName, 'rb')
				try:
					_codecModule(codec)
				except BaseException:
					raw.close()
					raise
				reader = _MemberReader(_MemberStream(raw, codec, owned=True), 1 << 16)
				return reader if 'b' in mode else io.TextIOWrapper(reader, encoding=encoding)
			return _codecModule(codec).open(fileName, mode, **kwargs)
		except Exception:
			__teePrintOrNot(f"Failed to open {fileName} with {codec}, trying bin",teeLogger=teeLogger)
	if 't' in mode:
		mode = mode.replace('t','')
		return open(fileName, mode, encoding=encoding)
	if 'b' not in mode:
		mode += 'b'
	return open(fileName, mode)
	
def get_terminal_size():
	'''
	Get the terminal size

	@params:
		None

	@returns:
		(int,int): the number of columns and rows of the terminal
	'''
	try:
		import os
		_tsize = os.get_terminal_size()
		if not (_tsize[0] and _tsize[1]):
			raise OSError('the terminal has no size set')
	except Exception:
		try:
			import fcntl
			import struct
			import termios
			packed = fcntl.ioctl(0, termios.TIOCGWINSZ, struct.pack('HHHH', 0, 0, 0, 0))
			rows, columns = struct.unpack('HHHH', packed)[:2]
			if not (rows and columns):
				raise OSError('the terminal has no size set')
			_tsize = (columns, rows)
		except Exception:
			import shutil
			_tsize = shutil.get_terminal_size(fallback=(240, 50))
	return _tsize

def pretty_format_table(data, delimiter="\t", header=None, full=False):
	"""Format ``data`` as a text table fitted to the terminal; ``full`` keeps every cell whole.

	``header`` names the columns (by default the first row does); ``header=False``
	prints the rows alone, without a header or its rule. Widths count terminal
	columns: an East Asian wide character takes two, a combining mark and an ANSI
	color code none.
	"""
	import unicodedata
	ansi = re.compile(r"(\x1b\[[0-?]*[ -/]*[@-~])")
	def char_width(ch):
		if ch < "\u0300":  # ASCII and Latin-1
			return 1
		if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf") or "\u1160" <= ch <= "\u11ff":
			return 0
		return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
	def visible_len(s):
		if "\x1b" in s:
			s = ansi.sub("", s)
		if not s or max(s) < "\u0300":
			return len(s)
		return sum(char_width(ch) for ch in s)
	def pad(s, width):
		return s + " " * (width - visible_len(s))
	def table_width(col_widths, sep_len):
		# total width = sum of column widths + separators between columns
		return sum(col_widths) + sep_len * (len(col_widths) - 1)
	def cut(s, width):
		# The characters that fit in ``width`` columns, and every color code.
		used, out = 0, []
		for i, piece in enumerate(ansi.split(s)):
			if i % 2:
				out.append(piece)
				continue
			for ch in piece:
				w = char_width(ch)
				if used + w > width:
					width = -1  # full: no further characters
					break
				out.append(ch)
				used += w
		return "".join(out)
	def truncate_to_width(s, width):
		# A cell wider than ``width`` keeps what fits in width - 2 columns, then "..".
		if visible_len(s) <= width:
			return s
		if width < 2:
			return "."[:max(width, 0)]
		return cut(s, width - 2) + ".."
	if not data:
		return ""
	# Normalize input data structure
	if isinstance(data, str):
		data = data.strip("\n").split("\n")
		data = [line.split(delimiter) for line in data]
	elif isinstance(data, dict):
		if isinstance(next(iter(data.values())), dict):
			tempData = [["key"] + list(next(iter(data.values())).keys())] if header is None else []
			tempData.extend([[key] + list(value.values()) for key, value in data.items()])
			data = tempData
		else:
			data = [[key] + list(value) for key, value in data.items()]
	elif not isinstance(data, list):
		data = list(data)
	if isinstance(data[0], dict):
		tempData = [list(data[0].keys())] if header is None else []
		tempData.extend([list(item.values()) for item in data])
		data = tempData
	data = [[str(item) for item in row] for row in data]
	num_cols = len(data[0])
	# Resolve header and rows
	show_header = header is not False
	if header is None:
		header = data[0]
		rows = data[1:]
	else:
		if header is False:
			header = []
		elif isinstance(header, str):
			header = header.split(delimiter)
		# Pad/trim header to match num_cols
		if len(header) < num_cols:
			header = header + [""] * (num_cols - len(header))
		elif len(header) > num_cols:
			header = header[:num_cols]
		rows = data
	# Compute initial column widths based on data and header
	def compute_col_widths(hdr, rows_):
		col_w = [0] * len(hdr)
		for i in range(len(hdr)):
			col_w[i] = max(0, visible_len(hdr[i]), *(visible_len(r[i]) for r in rows_ if i < len(r)))
		return col_w
	# Ensure all rows have the same number of columns
	normalized_rows = []
	for r in rows:
		if len(r) < num_cols:
			r = r + [""] * (num_cols - len(r))
		elif len(r) > num_cols:
			r = r[:num_cols]
		normalized_rows.append(r)
	rows = normalized_rows
	col_widths = compute_col_widths(header, rows)
	# If full=True, keep existing formatting
	# Else try to fit within the terminal width by:
	# 1) Switching to compressed separators if needed
	# 2) Recursively compressing columns (truncating with ".")
	sep = " | "
	hsep = "-+-"
	cols = get_terminal_size()[0]
	def render(hdr, rows, col_w, sep_str, hsep_str):
		rule = hsep_str.join("-" * w for w in col_w)
		out = [sep_str.join(pad(cell, w) for cell, w in zip(hdr, col_w)), rule] if show_header else []
		for row in rows:
			if not any(row):
				out.append(rule)
			else:
				out.append(sep_str.join(pad(truncate_to_width(cell, w), w) for cell, w in zip(row, col_w)))
		return "\n".join(out) + "\n"
	if full:
		return render(header, rows, col_widths, sep, hsep)
	# Try default separators first
	if table_width(col_widths, len(sep)) <= cols:
		return render(header, rows, col_widths, sep, hsep)
	# Use compressed separators (no spaces)
	sep = "|"
	hsep = "+"
	if table_width(col_widths, len(sep)) <= cols:
		return render(header, rows, col_widths, sep, hsep)
	# Begin column compression
	# Track which columns have been compressed already to header width
	header_widths = [visible_len(h) for h in header]
	width_diff = [max(col_widths[i] - header_widths[i],0) for i in range(num_cols)]
	total_overflow_width = table_width(col_widths, len(sep)) - cols
	for i, diff in sorted(enumerate(width_diff), key=lambda x: -x[1]):
		if total_overflow_width <= 0:
			break
		if diff <= 0:
			continue
		reduce_by = min(diff, total_overflow_width)
		col_widths[i] -= reduce_by
		total_overflow_width -= reduce_by
	return render(header, rows, col_widths, sep, hsep)

def format_bytes(size, use_1024_bytes=None, to_int=False, to_str=False,str_format='.2f'):
	"""
	Format the size in bytes to a human-readable format or vice versa.
	From hpcp: https://github.com/yufei-pan/hpcp

	Args:
		size (int or str): The size in bytes or a string representation of the size.
		use_1024_bytes (bool, optional): Whether to use 1024 bytes as the base for conversion. If None, it will be determined automatically. Default is None.
		to_int (bool, optional): Whether to convert the size to an integer. Default is False.
		to_str (bool, optional): Whether to convert the size to a string representation. Default is False.
		str_format (str, optional): The format string to use when converting the size to a string. Default is '.2f'.

	Returns:
		int or str: The formatted size based on the provided arguments.

	Examples:
		>>> format_bytes(1500, use_1024_bytes=False)
		'1.50 K'
		>>> format_bytes('1.5 GiB', to_int=True)
		1610612736
		>>> format_bytes('1.5 GiB', to_str=True)
		'1.50 Gi'
		>>> format_bytes(1610612736, use_1024_bytes=True, to_str=True)
		'1.50 Gi'
		>>> format_bytes(1610612736, use_1024_bytes=False, to_str=True)
		'1.61 G'
	"""
	if to_int or isinstance(size, str):
		if isinstance(size, int):
			return size
		elif isinstance(size, str):
			# Use regular expression to split the numeric part from the unit, handling optional whitespace
			match = re.match(r"(\d+(\.\d+)?)\s*([a-zA-Z]*)", size)
			if not match:
				if to_str:
					return size
				print("Invalid size format. Expected format: 'number [unit]', e.g., '1.5 GiB' or '1.5GiB'")
				print(f"Got: {size}")
				return 0
			number, _, unit = match.groups()
			number = float(number)
			unit  = unit.strip().lower().rstrip('b')
			# Define the unit conversion dictionary
			if unit.endswith('i'):
				# this means we treat the unit as 1024 bytes if it ends with 'i'
				use_1024_bytes = True
			elif use_1024_bytes is None:
				use_1024_bytes = False
			unit  = unit.rstrip('i')
			if use_1024_bytes:
				power = 2**10
			else:
				power = 10**3
			unit_labels = {'': 0, 'k': 1, 'm': 2, 'g': 3, 't': 4, 'p': 5}
			if unit not in unit_labels:
				if to_str:
					return size
				print(f"Invalid unit '{unit}'. Expected one of {list(unit_labels.keys())}")
				return 0
			if to_str:
				return format_bytes(size=int(number * (power ** unit_labels[unit])), use_1024_bytes=use_1024_bytes, to_str=True, str_format=str_format)
			# Calculate the bytes
			return int(number * (power ** unit_labels[unit]))
		else:
			try:
				return int(size)
			except Exception:
				return 0
	elif to_str or isinstance(size, int) or isinstance(size, float):
		if isinstance(size, str):
			try:
				size = size.rstrip('B').rstrip('b')
				size = float(size.lower().strip())
			except Exception:
				return size
		# size is in bytes
		if use_1024_bytes or use_1024_bytes is None:
			power = 2**10
			n = 0
			power_labels = {0 : '', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti', 5: 'Pi'}
			while size >= power:
				size /= power
				n += 1
			return f"{size:{str_format}}{' '}{power_labels[n]}"
		else:
			power = 10**3
			n = 0
			power_labels = {0 : '', 1: 'K', 2: 'M', 3: 'G', 4: 'T', 5: 'P'}
			while size >= power:
				size /= power
				n += 1
			return f"{size:{str_format}}{' '}{power_labels[n]}"
	else:
		try:
			return format_bytes(float(size), use_1024_bytes)
		except Exception as e:
			import traceback
			print(f"Error: {e}")
			print(traceback.format_exc())
			print(f"Invalid size: {size}")
		return 0

def get_resource_usage(return_dict = False):
	try:
		if RESOURCE_LIB_AVAILABLE:
			rawResource =  resource.getrusage(resource.RUSAGE_SELF)
			resourceDict = {}
			resourceDict['user mode time'] = f'{rawResource.ru_utime} seconds'
			resourceDict['system mode time'] = f'{rawResource.ru_stime} seconds'
			resourceDict['max resident set size'] = f'{format_bytes(rawResource.ru_maxrss * 1024)}B'
			resourceDict['shared memory size'] = f'{format_bytes(rawResource.ru_ixrss * 1024)}B'
			resourceDict['unshared memory size'] = f'{format_bytes(rawResource.ru_idrss * 1024)}B'
			resourceDict['unshared stack size'] = f'{format_bytes(rawResource.ru_isrss * 1024)}B'
			resourceDict['cached page hits'] = f'{rawResource.ru_minflt}'
			resourceDict['missed page hits'] = f'{rawResource.ru_majflt}'
			resourceDict['swapped out page count'] = f'{rawResource.ru_nswap}'
			resourceDict['block input operations'] = f'{rawResource.ru_inblock}'
			resourceDict['block output operations'] = f'{rawResource.ru_oublock}'
			resourceDict['IPC messages sent'] = f'{rawResource.ru_msgsnd}'
			resourceDict['IPC messages received'] = f'{rawResource.ru_msgrcv}'
			resourceDict['signals received'] = f'{rawResource.ru_nsignals}'
			resourceDict['voluntary context sw'] = f'{rawResource.ru_nvcsw}'
			resourceDict['involuntary context sw'] = f'{rawResource.ru_nivcsw}'
			if return_dict:
				return resourceDict
			return '\n'.join(['\t'.join(line) for line in resourceDict.items()])
	except Exception as e:
		print(f"Error: {e}")
	if return_dict:
		return {}
	return ''

def __teePrintOrNot(message,level = 'info',teeLogger = None):
	"""
	Prints the given message or logs it using the provided teeLogger.

	Parameters:
		message (str): The message to be printed or logged.
		level (str, optional): The log level. Defaults to 'info'.
		teeLogger (object, optional): The logger object used for logging. Defaults to None.

	Returns:
		None
	"""
	def write():
		try:
			if teeLogger:
				try:
					teeLogger.teelog(message,level,callerStackDepth=3)
				except Exception:
					teeLogger.teelog(message,level)
			else:
				print(message,flush=True)
		except Exception:
			print(message,flush=True)
	_say(write, message)

# ---------------------------------------------------------------------------
# 4.1 infrastructure: file names, dialect selection, warnings, locked appends
# ---------------------------------------------------------------------------
STRICT_EXTENSIONS = ('tsvz', 'csvz', 'nsvz', 'psvz')
_EXTENSION_DELIMITERS = {'tsv': '\t', 'csv': ',', 'nsv': '\0', 'psv': '|',
						 'tsvz': '\t', 'csvz': ',', 'nsvz': '\0', 'psvz': '|'}
_HEX_RE = re.compile(r'^[0-9A-Fa-f]+$')
_PartName = namedtuple('_PartName', 'store ext ordinal rotated codec')
#: Read size for spec-dialect parts (tests shrink it to force chunk splits).
_CHUNK = 1 << 20


def _parsePartName(path):
	"""Split a file name per tsvz-spec-v1 Appendix A.

	Returns ``_PartName(store, ext, ordinal, rotated, codec)``: ``store`` is the
	path up to and including the format extension, ``ext`` the lowercase format
	extension ('' when the name has none), ``ordinal`` an int or None,
	``rotated`` a bool and ``codec`` the lowercase compression suffix or ''.
	"""
	rest, codec = path, ''
	head, dot, tail = rest.rpartition('.')
	if dot and tail.lower() in COMPRESSED_FILE_EXTENSIONS:
		rest, codec = head, tail.lower()
	plain = rest
	rotated = False
	head, dot, tail = rest.rpartition('.')
	if dot and tail.lower() == 'rotated':
		rest, rotated = head, True
	ordinal = None
	head, dot, tail = rest.rpartition('.')
	if dot and _HEX_RE.match(tail) and head.rpartition('.')[2].lower() in _EXTENSION_DELIMITERS:
		rest, ordinal = head, int(tail, 16)
	head, dot, tail = rest.rpartition('.')
	ext = tail.lower() if dot else ''
	if ext not in _EXTENSION_DELIMITERS:
		return _PartName(plain, '', None, False, codec)
	return _PartName(rest, ext, ordinal, rotated, codec)


def _isSpecPath(path):
	"""True when ``path`` names a tsvz-spec-v1 file (``.tsvz``/``.csvz``/``.nsvz``/``.psvz``)."""
	return _parsePartName(path).ext in STRICT_EXTENSIONS


_REFUSED_RETRY_SECONDS = 1.0  # shortest wait before a refused rewrite is tried again (rewrite_interval when longer);
# doubled at each refusal in a row up to _FAILURE_RETRY_MAX, as a failed write's back-off (from _FAILURE_RETRY_FIRST) is
_FAILURE_RETRY_FIRST = 0.1  # seconds the background writer waits before it tries a failed legacy write again,
_FAILURE_RETRY_MAX = 60.0  # doubled at each failure in a row up to this


class TSVZEncodeError(ValueError):
	"""Text to write cannot be encoded: a lone surrogate (what ``os.fsdecode`` gives for a non-UTF-8 file name), or a character the encoding lacks.

	Writers raise it, before writing anything, unless they were given
	``errors='replace'`` (or another codec error handler) to take the old
	behaviour of writing ``?`` for the character. ``reason`` is the message
	without its advice, which names options of the library (the ``tsvz``
	command prints it alone).
	"""

	def __init__(self, reason, advice=''):
		ValueError.__init__(self, reason + ('; ' + advice if advice else ''))
		self.reason = reason
		self.advice = advice


def _encodingName(encoding):
	try:
		return codecs.lookup(encoding or 'utf8').name.upper()
	except LookupError:
		return str(encoding)


def _unencodable(text, error, encoding, where=None):
	"""The ``TSVZEncodeError`` for ``error``, the ``UnicodeEncodeError`` raised encoding ``text``."""
	name = _encodingName(encoding)
	char = error.object[error.start]
	code = ord(char)
	shown = ascii(char)[1:-1]
	if 0xDC80 <= code <= 0xDCFF:
		what = '{} (an undecodable byte 0x{:02x}, as os.fsdecode gives)'.format(shown, code - 0xDC00)
	elif 0xD800 <= code <= 0xDFFF:
		what = '{} (a lone surrogate)'.format(shown)
	elif code == 0xFFFD:
		what = '{} (U+FFFD, what reading puts for a byte it could not decode), which {} cannot encode'.format(shown, name)
	else:
		what = '{} (U+{:04X}), which {} cannot encode'.format(shown, code, name)
	if code == 0xFFFD:
		# '?' in its place would lose for good the bytes the file holds there.
		advice = ("open the store with the encoding it was written in; errors='replace' would write '?' for it, "
				  "losing the bytes it stands for")
	else:
		advice = "escape it first or pass errors='replace'"
	subject = ascii(text)
	if len(subject) > 100:
		subject = subject[:97] + '...'
	return TSVZEncodeError("cannot write {} as {}{}: it holds {}".format(
		subject, name, ' ({})'.format(where) if where else '', what), advice)


_NEVER_RAISING_HANDLERS = frozenset(('replace', 'ignore', 'xmlcharrefreplace', 'backslashreplace', 'namereplace'))


def _checkErrors(errors):
	"""Return ``errors`` if it names a codec error handler, else raise ``ValueError`` (or ``TypeError``) now, not in the background writer."""
	if not isinstance(errors, str):
		raise TypeError("errors must be the name of a codec error handler such as 'strict' or 'replace', not {}".format(ascii(errors)))
	try:
		codecs.lookup_error(errors)
	except LookupError:
		raise ValueError("unknown errors= handler {}; use 'strict', 'replace' or a registered codec error handler".format(ascii(errors))) from None
	return errors


def _mayRaise(errors):
	"""True when encoding with ``errors`` can fail, so a writer has to check text before it changes anything."""
	return errors not in _NEVER_RAISING_HANDLERS


def _encodeText(text, encoding='utf8', errors='strict', where=None):
	"""``text.encode(encoding, errors)``; a failure is a ``TSVZEncodeError`` naming ``where``."""
	encoding = encoding or 'utf8'
	try:
		return text.encode(encoding, errors)
	except UnicodeEncodeError as e:
		raise _unencodable(text, e, encoding, where) from None


def _encodeRow(cells, delimiter, encoding='utf8', errors='strict', header=None, what='row'):
	"""The bytes of ``cells`` joined by ``delimiter``; a failure names the cell (``what``: 'row', 'header' or 'defaults')."""
	encoding = encoding or 'utf8'
	line = delimiter.join(cells)
	try:
		return line.encode(encoding, errors)
	except UnicodeEncodeError as e:
		char = e.object[e.start]
		for i, cell in enumerate(cells):
			if char in cell:
				if what != 'row':
					where = 'the ' + what if what == 'header' else 'the defaults line'
				elif i == 0:
					where = 'the key'
				else:
					name = header[i] if header and i < len(header) and header[i] else ''
					where = 'column {}{} of key {}'.format(i + 1, " '{}'".format(name) if name else '', ascii(cells[0]))
				raise _unencodable(cell, e, encoding, where) from None
		raise _unencodable(line, e, encoding) from None


def _checkRow(cells, delimiter, encoding='utf8', errors='strict', header=None, what='row'):
	"""Raise ``TSVZEncodeError`` now when ``cells`` could not be written (nothing else: used before state changes)."""
	if _mayRaise(errors):
		_encodeRow(cells, delimiter, encoding, errors, header, what)


def _warn(message, teeLogger=None):
	"""Report a tolerance warning through ``teeLogger``, else stderr (never stdout)."""
	if teeLogger:
		try:
			_say(lambda: teeLogger.teelog(message, 'warning'), message)
			return
		except Exception:
			pass
	eprint(message)


class _Reporter(object):
	"""Collect tolerance events for one operation and report each kind once.

	``note(kind, where, detail)`` records an event; ``where`` is a location such
	as ``'line 17'`` or None. ``flush()`` prints one line per kind, in first-seen
	order, with the number of occurrences and the first location.
	"""

	def __init__(self, path, teeLogger=None):
		self.path = path
		self.teeLogger = teeLogger
		self._events = OrderedDict()

	def note(self, kind, where, detail):
		event = self._events.get(kind)
		if event is None:
			self._events[kind] = [1, where, detail]
		else:
			event[0] += 1

	def discard(self, kind):
		"""Forget the events of ``kind`` (for a caller that reports them another way)."""
		self._events.pop(kind, None)

	def has(self, kind):
		"""True when an event of ``kind`` was noted and not yet flushed."""
		return kind in self._events

	def mark(self):
		"""The events noted so far, for ``rewind``."""
		return [(kind, list(event)) for kind, event in self._events.items()]

	def rewind(self, mark):
		"""Forget the events noted since ``mark`` (for a read that is done again)."""
		self._events = OrderedDict((kind, list(event)) for kind, event in mark)

	def incomplete(self):
		"""True when the read did not see the whole store: a part was unreadable, a stop policy ended it, or
		parts kept changing under it (it may be torn)."""
		return 'unreadable' in self._events or 'stopped' in self._events or 'torn' in self._events

	def flush(self):
		for count, where, detail in self._events.values():
			message = 'TSVZ warning: {}: {}'.format(self.path, detail)
			if count > 1:
				message += ' ({} occurrences{})'.format(count, ', first at ' + where if where else '')
			elif where:
				message += ' ({})'.format(where)
			_warn(message, self.teeLogger)
		self._events.clear()


def _warnOnce(owner, kind, path, detail, teeLogger=None):
	"""Warn about ``detail`` once per ``owner`` object and ``kind``."""
	warned = owner.__dict__.setdefault('_tsvzWarned', set())
	if kind in warned:
		return
	warned.add(kind)
	_warn('TSVZ warning: {}: {}'.format(path, detail), teeLogger)


_PATH_LOCKS = {}


def _pathLock(path):
	"""Return the process-wide lock for ``path``.

	One-shot appends (_lockedAppend), the making of a missing file, scrubs and
	clears (_specRewriteInPlace) and .tsvz part writes also hold it while they
	lock the file: ``lockf``, still the fallback where the other locks are
	refused, excludes no thread of the process, and Windows' ``msvcrt`` gives
	up after 10 s. A TSVZed's own writes, which its writeLock serialises, and
	readers do not take it.
	"""
	key = os.path.realpath(path)
	# (setdefault: one step, where a guard lock could be left held by a Ctrl-C and stop every thread's writes, see _Flag)
	return _PATH_LOCKS.get(key) or _PATH_LOCKS.setdefault(key, threading.RLock())


#: The lock modes of ``_lockFile`` (spec §18.6): READ, taken by readers that make state out of the file, shared, for the
#: read (``_readLock``); APPEND, taken by appends and by the making of a missing file; REWRITE, taken by anything that
#: changes bytes already written, and by a read again after damage.
_READ, _APPEND, _REWRITE = 'read', 'append', 'rewrite'

#: How files are locked between processes: 'ofd', Linux open file description locks in byte ranges (READ: byte 2, the
#: gate, shared until byte 0 is held shared; APPEND: byte 1; REWRITE: byte 1, the gate, then the whole file);
#: 'flock', whole-file flock (READ shared, else exclusive) on other POSIX systems; 'lockf', process-owned, where neither works (readers then take no lock); None
#: on Windows (msvcrt, writers only). A writer whose lock is refused falls back to lockf for that descriptor.
_LOCK_KIND = ('ofd' if sys.platform.startswith('linux') else 'flock') if os.name == 'posix' else None

#: The Linux open file description lock commands: (wait, no wait).
_OFD_COMMANDS = ((getattr(fcntl, 'F_OFD_SETLKW', 38), getattr(fcntl, 'F_OFD_SETLK', 37))
				 if os.name == 'posix' and sys.platform.startswith('linux') else None)


def _ofdLock(f, kind, start=0, length=0):
	"""Set a shared (``fcntl.F_RDLCK``), exclusive (``fcntl.F_WRLCK``) or no (``fcntl.F_UNLCK``) lock owned by the
	open file description on ``length`` bytes from ``start`` (0: to the end, whatever it grows to), waiting for it.
	Returns False when the kernel lacks such locks (EINVAL: before Linux 3.15, or a file system without them)."""
	import struct
	# struct flock: l_type, l_whence, l_start, l_len, l_pid (0 for these locks); trailing padding added.
	request = struct.pack('@hhqqi', kind, os.SEEK_SET, start, length, 0) + b'\0' * 8
	try:
		fcntl.fcntl(f.fileno(), _OFD_COMMANDS[1] if kind == fcntl.F_UNLCK else _OFD_COMMANDS[0], request)
	except OSError as e:
		if e.errno != errno.EINVAL:
			raise
		return False
	return True


#: The descriptors TSVZ holds a lock on: fd -> [kind ('ofd', 'flock' or 'lockf': how it is unlocked), the file's
#: _fileIdentity, the locking thread's ident, a weak reference to the file object]. Entered before the lock call,
#: left at the unlock, so a fork at any moment finds every lock the child could inherit (_locksAfterFork).
_HELD = {}


def _heldHere():
	"""The _fileIdentity of every file this thread holds a TSVZ lock on, through a file object still open (one
	closed without an unlock let go of its lock): a read of one takes no lock (_readLock)."""
	me = threading.get_ident()
	held = set()
	for _, key, owner, ref in list(_HELD.values()):
		f = ref()
		if owner == me and f is not None and not f.closed:
			held.add(key)
	return held


def _locksAfterFork():
	"""In a forked child: the threads that held a path's in-process lock (``_pathLock``) at the fork are not there to
	release it, and the parent's lockf locks are not inherited. Its open file description and flock locks are: each
	descriptor that holds one is pointed at /dev/null, so the child holds none and the parent's lock is as it was (a
	copy of a get_file_obj() holder then fails its writes with EBADF)."""
	_PATH_LOCKS.clear()
	held = list(_HELD.items())
	_HELD.clear()
	inherited = [fd for fd, (kind, _, _, ref) in held if kind != 'lockf' and ref() is not None and not ref().closed]
	if inherited:
		null = os.open(os.devnull, os.O_RDONLY)
		try:
			for fd in inherited:
				os.dup2(null, fd)
		finally:
			os.close(null)


if hasattr(os, 'register_at_fork'):  # Python 3.7+
	os.register_at_fork(after_in_child=_locksAfterFork)


def _lockFile(f, mode=_REWRITE):
	"""Lock the open file ``f`` for ``mode`` (_READ, _APPEND, _REWRITE), waiting for it; True when it is locked.

	On Linux the lock belongs to the open file description, in byte ranges
	(spec §18.6): READ byte 0 shared, passing through the gate (byte 2, held
	shared only until byte 0 is), APPEND byte 1, REWRITE byte 1, then the gate,
	then the whole file. Appends never wait for readers; a rewrite waits for
	both, and while it waits no new append or read starts, so the reads in
	flight finish and overlapping reads cannot hold it off (Linux grants new
	shared locks while an exclusive one waits). On other
	POSIX systems it is a whole-file flock (READ shared). Neither is dropped
	when another descriptor of the file closes in this process, and both
	exclude other threads. Every 3.39 to 5.2 lock (one exclusive whole-file
	lockf or open file description lock) excludes every mode, and is excluded.
	A writer's lock the kernel or file system refuses falls back to lockf, which
	closing any descriptor of the file in this process drops; a READ is then
	not taken (a shared lockf would replace this process's own write lock), nor
	when its lock fails in any way: False, and the read goes on unlocked.
	Windows: msvcrt for writers (from the raw position: see _seekRawStart),
	nothing for READ. Unlock with _unlockFile, before the file is closed.
	"""
	if os.name == 'nt':
		if mode == _READ:
			return False
		msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 2147483647)
		return True
	if os.name != 'posix':
		return False
	kind = _LOCK_KIND
	if mode == _READ and kind not in ('ofd', 'flock'):
		return False
	fd = f.fileno()
	entry = [kind, _fileIdentity(os.fstat(fd)), threading.get_ident(), weakref.ref(f)]
	_HELD[fd] = entry  # before the lock call: a fork from now on finds it (an entry left by a file closed unlocked goes)
	try:
		try:
			if kind == 'ofd':
				if mode == _READ:
					ok = _ofdLock(f, fcntl.F_RDLCK, 2, 1) and _ofdLock(f, fcntl.F_RDLCK, 0, 1)
					if ok:
						_ofdLock(f, fcntl.F_UNLCK, 2, 1)  # through the gate
				else:
					ok = _ofdLock(f, fcntl.F_WRLCK, 1, 1) and (mode == _APPEND or (
						_ofdLock(f, fcntl.F_WRLCK, 2, 1) and _ofdLock(f, fcntl.F_WRLCK)))
			elif kind == 'flock':
				try:
					fcntl.flock(fd, fcntl.LOCK_SH if mode == _READ else fcntl.LOCK_EX)
					ok = True
				except OSError as e:
					if e.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL):
						raise
					ok = False
			else:
				ok = False
		except OSError:
			if mode == _READ:
				ok = False
			else:
				raise
		if not ok:
			if mode == _READ:
				_HELD.pop(fd, None)
				return False
			fcntl.lockf(f, fcntl.LOCK_EX)
			entry[0] = 'lockf'
	except BaseException:
		if _HELD.get(fd) is entry:
			del _HELD[fd]
		if kind == 'ofd':
			try:
				_ofdLock(f, fcntl.F_UNLCK)  # a Ctrl-C in a later step: the bytes taken before it (byte 1, the gate)
			except BaseException:
				pass
		raise
	return True


@contextlib.contextmanager
def _readLock(path):
	"""Hold the READ lock (_lockFile) on the file ``path`` for the ``with`` block: a rewrite by another process (or
	thread) that holds the file's lock is waited out, so the read never meets it part way. Taken on a descriptor of
	its own, so the reader opens the file as before. Not taken when this thread holds a TSVZ lock on the file
	already (it would wait for itself), where readers do not lock (lockf, Windows), or when the file cannot be
	opened or locked: the read goes on unlocked, and its own open raises what it raises. A Ctrl-C ends the wait.
	Yields the descriptor it opened (None when it opened none)."""
	if _LOCK_KIND not in ('ofd', 'flock'):
		yield
		return
	try:
		f = open(path, 'rb')
	except OSError:
		yield
		return
	locked = False
	try:
		try:
			if _fileIdentity(os.fstat(f.fileno())) not in _heldHere():
				locked = _lockFile(f, _READ)
		except OSError:
			pass  # a read never fails because its lock did
		yield f
	finally:
		try:
			if locked:
				_unlockFile(f)
		finally:
			f.close()


def _tryLockFile(f):
	"""Take an exclusive whole-file lock without waiting; return False when another handle holds it.

	POSIX uses ``flock``: it belongs to the open file description, so closing
	another descriptor of the same file in this process does not drop it.
	Windows locks are mandatory, so there one byte far past the content is
	locked and readers can still read the file.
	"""
	try:
		if os.name == 'posix':
			fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
		elif os.name == 'nt':
			os.lseek(f.fileno(), 1 << 30, os.SEEK_SET)
			msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
		return True
	except OSError as e:
		if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES, getattr(errno, 'EDEADLK', -1)):
			return False
		raise  # e.g. ENOLCK: this file system cannot lock


def _unlockFile(f):
	"""Release the whole-file lock _lockFile took on ``f``, the way it was taken; nothing when TSVZ holds none on it."""
	try:
		if os.name == 'posix':
			fd = f.fileno()
			entry = _HELD.pop(fd, None)
			if entry is None:
				return
			kind = entry[0]
			if kind == 'ofd':
				_ofdLock(f, fcntl.F_UNLCK)
			elif kind == 'flock':
				fcntl.flock(fd, fcntl.LOCK_UN)
			else:
				fcntl.lockf(f, fcntl.LOCK_UN)
		elif os.name == 'nt':
			msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 2147483647)
	except Exception:
		pass


def _seekRawStart(f):
	"""Move a buffered file object's underlying fd to offset 0.

	A plain ``f.seek(0)`` can be served from the read buffer without moving the
	raw position, but ``msvcrt.locking`` locks from the raw position. Seeking to
	the end first discards the buffer, so the following seek really moves it.
	"""
	f.seek(0, os.SEEK_END)
	f.seek(0)


def _lastNewlineEnd(f, size):
	"""Return the offset just after the last b'\\n' within the first ``size`` bytes of ``f``, or 0."""
	position = size
	while position > 0:
		step = min(65536, position)
		position -= step
		f.seek(position)
		index = f.read(step).rfind(b'\n')
		if index >= 0:
			return position + index + 1
	return 0


#: A compressed file at most this large is decoded to tell whether it holds anything (_holdsNothing): an empty
#: stream is 9 to 40 bytes, and a file of only empty streams is never much larger.
_EMPTY_STREAMS_MAX = 4096


def _holdsNothing(f, codec, size):
	"""True when the open binary file ``f`` (read at its raw level), ``size`` bytes long, holds no line: it has no
	byte, or, compressed in ``codec``, only complete empty streams, which a codec's writer closed with nothing
	written leaves (gzip.open(p, 'wb').close(), a clear of a store with no header). A reader reads the same from
	both: nothing. Only a file of at most _EMPTY_STREAMS_MAX bytes is decoded for it (``f`` is left at offset 0);
	a damaged one holds something (bytes another writer left, not this store's empty file)."""
	if not size:
		return True
	if not codec or size > _EMPTY_STREAMS_MAX or not _codecSupported(codec):
		return False
	f.seek(0)
	data = f.read(size)
	f.seek(0)  # where a rewrite that looked expects it
	text, damage = _decodeMembers(codec, data)
	return not text and not damage


def _fileHoldsNothing(fileName):
	"""_holdsNothing for the file ``fileName``, which exists."""
	with open(fileName, 'rb') as f:
		return _holdsNothing(f, _fileCodec(fileName), os.fstat(f.fileno()).st_size)


def _appendOpen(f, payload, codec=None, header=None, tailPolicy='newline', reporter=None, fsync=False, memberSize=None,
				written=None):
	"""Append ``payload`` (whole lines, each ending in b'\\n') to the open file ``f``: the one write of every append
	to a legacy file (and of _lockedAppend's to a spec part). Returns the offset in ``f`` where the payload's first
	line begins (for a compressed file, where what was written begins).

	``f`` is binary, readable and seekable, opened at its raw level ('a+b' or 'r+b', never through a codec) and
	held by the caller: locked (_lockedAppend, TSVZed.commitAppendToFile) or, for TSVZedLite, which never locks a
	legacy file, owned alone. What is written depends on what the file holds:

	- nothing (_holdsNothing: no byte, or only empty compressed streams): ``header`` first, when it is not None:
	  a legacy store's header line without its newline, or a function that makes it, called only then (a header
	  that cannot be encoded refuses only an append that would write it). An append to an empty file writes the header first, as creating the file does: a file a
	  rewrite cut short (a full disk, a KeyboardInterrupt) or another process emptied is left empty, and rows
	  there with no header would read with the first one taken for the header (a mismatch, the row lost);
	- else, for a file that is not compressed, a last line with no b'\\n' is terminated first (``tailPolicy``
	  'newline': to a legacy reader it is a record) or cut off ('truncate': spec, those bytes were never
	  committed, spec §4.3), noted on ``reporter``; a compressed file's last line is not looked at.

	For a ``codec`` the bytes are compressed as whole members (_compressBytes; ``memberSize``): no codec writer is
	made, whose close would write a member of its own. They are written with os.write, past any buffer of ``f``
	(nothing is left in one for a close to write later), and synced with ``fsync``. An error raises; one in the
	write first cuts off again what it wrote of them, so an append cut short (a full disk) leaves no torn row for
	a reader, or a merge, to take for a row (a failed sync leaves what was all written). ``written``, a list, gets
	the bytes written once they all are, as they are on disk.
	"""
	f.flush()
	size = f.seek(0, os.SEEK_END)
	prefix = b''
	if header is not None and _holdsNothing(f, codec, size):
		prefix = (header() if callable(header) else header) + b'\n'
	elif size and not codec:
		f.seek(size - 1)
		if f.read(1) != b'\n':
			if tailPolicy == 'newline':
				prefix = b'\n'
				if reporter is not None:
					reporter.note('missing-newline', None, 'last line had no trailing newline; added one before appending')
			else:
				keep = _lastNewlineEnd(f, size)
				f.seek(keep)
				tail = f.read(size - keep)
				f.truncate(keep)
				if reporter is not None:
					reporter.note('truncated-tail', None, 'removed uncommitted tail {!r} before appending'.format(tail[:80]))
	data = prefix + payload
	if codec:
		data = _compressBytes(codec, data, memberSize=memberSize)
	fd = f.fileno()
	start = f.seek(0, os.SEEK_END)
	try:
		try:
			view = memoryview(data)
			while view:
				view = view[os.write(fd, view):]
		except BaseException:
			try:
				if os.fstat(fd).st_size <= start + len(data):  # only its own bytes: nothing written after them
					os.ftruncate(fd, start)
			except Exception:
				pass  # the error raised says the append failed
			raise
		if written is not None:
			written.append(data)
		if fsync:
			os.fsync(fd)  # all of it was written, and is read: a sync that fails leaves it
	finally:
		f.seek(0, os.SEEK_END)  # the offset ``f`` keeps is the file's again
	return start if codec else start + len(prefix)


def _lockedAppend(path, payload, tailPolicy, reporter, fsync=False, header=None, codec=None, memberSize=None):
	"""Append ``payload`` (whole records ending in b'\\n') to the file ``path`` (_appendOpen), compressed in
	``codec`` when given, under its locks: ``_pathLock`` and the file lock (``_lockFile``), so that no other writer
	that takes them writes between the look at what the file holds and the write. A missing file is created."""
	with _pathLock(path):
		with open(path, 'a+b') as f:
			_lockFile(f, mode=_APPEND)
			try:
				_appendOpen(f, payload, codec, header, tailPolicy, reporter, fsync, memberSize)
			finally:
				_unlockFile(f)


_CODEC_SUPPORT = {}


def _codecSupported(codec):
	"""True when this Python has ``codec`` (zstd needs Python 3.14; a minimal build may lack lzma or bz2). Checked once per codec."""
	name = _CODEC_NAMES.get(codec, codec)
	if name not in _CODEC_SUPPORT:
		try:
			_newDecompressor(codec)
			_CODEC_SUPPORT[name] = True
		except ImportError:
			_CODEC_SUPPORT[name] = False
	return _CODEC_SUPPORT[name]


def _requireCodec(codec):
	"""Raise ImportError before any write to a part in a codec this Python lacks (spec §16.5): nothing is written."""
	if codec and not _codecSupported(codec):
		raise ImportError('no {} support in this Python'.format(_CODEC_NAMES.get(codec, codec)))


def _compressBytes(codec, data, level=1, memberSize=None):
	"""Compress ``data`` as complete members of ``codec`` (spec §16.7): each holds at most
	``memberSize`` bytes (default 1 MiB) and ends at a line boundary; a longer line is a
	member of its own. Zstandard frames carry their content checksum (spec §16.8)."""
	_requireCodec(codec)
	name = _CODEC_NAMES.get(codec, codec)
	compress = functools.partial(_codecModule(name).compress, **_codecWriteOptions(name, level))
	size = memberSize or _DEFAULT_MEMBER_SIZE
	if len(data) <= size:
		return compress(data)
	members = []
	start = 0
	while start < len(data):
		end = start + size
		if end < len(data):
			cut = data.rfind(b'\n', start, end)
			if cut < 0:
				cut = data.find(b'\n', end)
			end = len(data) if cut < 0 else cut + 1
		members.append(compress(data[start:end]))
		start = end
	return b''.join(members)


def _dirMtimeNs(path):
	"""Modification time (ns) of the directory holding ``path``, or 0."""
	try:
		return os.stat(os.path.dirname(os.path.abspath(path))).st_mtime_ns
	except OSError:
		return 0


def _fitRow(cells, width):
	"""A legacy row fitted to the column count ``width``: a narrower row is padded with empty cells; a wider one keeps
	its cells, less only the empty cells at its end past ``width`` (``a\t1\t\t`` at 2 is ``[a, 1]``, ``a\t1\t2`` stays
	whole). Never drops a non-empty cell. A ``width`` of 0 or less leaves the row as it is. Returns a list."""
	cells = list(cells)
	if width <= 0:
		return cells
	if len(cells) < width:
		return cells + [''] * (width - len(cells))
	end = len(cells)
	while end > width and not cells[end - 1]:
		end -= 1
	return cells[:end]


def _processLine(line,taskDic,correctColumnNum,strict = True,delimiter = ...,defaults = ...,
				 storeOffset = False, offset = -1, reporter = None):
	"""
	Process a line of text and update the task dictionary.

	Parameters:
	line (str): The line of text to process.
	taskDic (dict): The dictionary to update with the processed line.
	correctColumnNum (int): The expected number of columns in the line.
	strict (bool, optional): Whether to strictly enforce the correct number of columns. Defaults to True.
	defaults (list, optional): The default values to use for missing columns. Defaults to [].
	storeOffset (bool, optional): Whether to store the offset of the line in the taskDic. Defaults to False.
	offset (int, optional): The offset of the line in the file. Defaults to -1.
	reporter (_Reporter, optional): Receives a note for each line dropped by strict mode (L4).

	Returns:
	tuple: A tuple containing the updated correctColumnNum and the processed lineCache or offset.

	"""
	if defaults is ...:
		defaults = []
	delimiter = get_delimiter(delimiter)
	line = line.strip('\x00').rstrip('\r\n')
	if not line or (line.startswith('#') and not line.startswith(DEFAULTS_INDICATOR_KEY)):
		# if verbose:
		# 	__teePrintOrNot(f"Ignoring comment line: {line}",teeLogger=teeLogger)
		return correctColumnNum , []
	# we only interested in the lines that have the correct number of columns
	lineCache = _unsanitize(line.split(delimiter),delimiter)
	if not lineCache or not lineCache[0]:
		return correctColumnNum , []
	if correctColumnNum == -1:
		if defaults and len(defaults) > 1:
			correctColumnNum = len(defaults)
		else:
			correctColumnNum = len(lineCache)
		# if verbose:
		# 	__teePrintOrNot(f"detected correctColumnNum: {len(lineCache)}",teeLogger=teeLogger)
	if len(lineCache) == 1 or not any(lineCache[1:]):
		if correctColumnNum == 1: 
			taskDic[lineCache[0]] = lineCache if not storeOffset else offset
		elif lineCache[0] == DEFAULTS_INDICATOR_KEY:
			# if verbose:
			# 	__teePrintOrNot(f"Empty defaults line found: {line}",teeLogger=teeLogger)
			defaults.clear()
			defaults[:] = [DEFAULTS_INDICATOR_KEY]
		else:
			# if verbose:
			# 	__teePrintOrNot(f"Key {lineCache[0]} found with empty value, deleting such key's representaion",teeLogger=teeLogger)
			if lineCache[0] in taskDic:
				del taskDic[lineCache[0]]
		return correctColumnNum , []
	elif len(lineCache) != correctColumnNum:
		if strict and len(lineCache) < correctColumnNum and not any(defaults[1:]):
			# (5.5: strict drops only a narrower line, which it would have to pad; a wider one is taken whole)
			if reporter is not None:
				reporter.note('strict-drop', None, 'dropped a line whose column count is not {} (strict mode)'.format(correctColumnNum))
			return correctColumnNum , []
		else:
			# a narrower line is padded to the column count; a wider one is read whole (a row is its own cells)
			lineCache = _fitRow(lineCache, correctColumnNum)
	if lineCache[0] == DEFAULTS_INDICATOR_KEY and len(lineCache) < len(defaults):
		lineCache += [''] * (len(defaults) - len(lineCache))  # (a defaults cell it lacks is the one before it)
	# now replace empty values with defaults
	if defaults and len(defaults) > 1:
		for i in range(1,len(lineCache)):
			if not lineCache[i] and i < len(defaults) and defaults[i]:
				lineCache[i] = defaults[i]
	if lineCache[0] == DEFAULTS_INDICATOR_KEY:
		# if verbose:
		# 	__teePrintOrNot(f"Defaults line found: {line}",teeLogger=teeLogger)
		defaults[:] = lineCache
		return correctColumnNum , []
	taskDic[lineCache[0]] = lineCache if not storeOffset else offset
	# if verbose:
	# 	__teePrintOrNot(f"Key {lineCache[0]} added",teeLogger=teeLogger)
	return correctColumnNum, lineCache

def _read_last_valid_line_forward(fileName, taskDic, correctColumnNum, verbose=False, teeLogger=None,
								  strict=False, encoding='utf8', delimiter=...,
								  defaults=None, storeOffset=False):
	"""
	Forward, single-pass variant of read_last_valid_line for compressed files.

	Backward chunked seeking forces a full re-decompress on every seek (and is not
	reliably supported for some codecs e.g. zstd), so for compressed files we decompress
	once, streaming forward, and keep only the last valid line / offset. A scratch dict is
	used so memory stays flat instead of accumulating the whole file into taskDic.
	"""
	if defaults is None:
		defaults = []
	delimiter = get_delimiter(delimiter, file_name=fileName)
	last_valid = -1 if storeOffset else []
	scratch = {}
	reporter = _Reporter(fileName, teeLogger)
	try:
		with _readLock(fileName), openFileAsCompressed(fileName, 'rb', encoding=encoding, teeLogger=teeLogger) as file:
			while True:
				offset = file.tell()
				try:
					line = file.readline()
				except _DamagedStream as e:  # raised at the end of the text: every member that checks out was read (L5)
					reporter.note('damaged', None, _streamDamage(e))
					break
				if not line:
					break
				if not line.strip():
					continue
				correctColumnNum, lineCache = _processLine(
					line=line.decode(encoding=encoding, errors='replace'),
					taskDic=scratch,
					correctColumnNum=correctColumnNum,
					strict=strict,
					delimiter=delimiter,
					defaults=defaults,
					storeOffset=storeOffset,
					offset=offset,
				)
				if lineCache:
					last_valid = offset if storeOffset else lineCache
	finally:
		reporter.flush()
	return last_valid

def read_last_valid_line(fileName, taskDic, correctColumnNum, verbose=False, teeLogger=None, strict=False,
						 encoding = 'utf8',delimiter = ...,defaults = ...,storeOffset = False	):
	"""
	Reads the last valid line from a file.

	Args:
		fileName (str): The name of the file to read.
		taskDic (dict): A dictionary to pass to processLine function.
		correctColumnNum (int): A column number to pass to processLine function.
		verbose (bool, optional): Whether to print verbose output. Defaults to False.
		teeLogger (optional): Logger to use for tee print. Defaults to None.
		encoding (str, optional): The encoding of the file. Defaults to None.
		strict (bool, optional): Whether to enforce strict processing. Defaults to False.
		delimiter (str, optional): The delimiter used in the file. Defaults to None.
		defaults (list, optional): The default values to use for missing columns. Defaults to [].
		storeOffset (bool, optional): Instead of storing the data in taskDic, store the offset of each line. Defaults to False.

	Returns:
		list: The last valid line as a list of strings, or an empty list if no valid line is found.
	"""
	if _isSpecPath(fileName):
		return _specReadTabularFile(fileName, teeLogger=teeLogger, lastLineOnly=True, verifyHeader=False,
									verbose=verbose, encoding=encoding, strict=strict, delimiter=delimiter,
									defaults=defaults, correctColumnNum=correctColumnNum, storeOffset=storeOffset)
	chunk_size = 1024  # Read in chunks of 1024 bytes
	last_valid_line = []
	if defaults is ...:
		defaults = []
	delimiter = get_delimiter(delimiter,file_name=fileName)
	if verbose:
		__teePrintOrNot(f"Reading last line only from {fileName}",teeLogger=teeLogger)
	if _isCompressedFile(fileName):
		# Backward seeking through a codec is slow/unsupported; decompress once, forward.
		return _read_last_valid_line_forward(
			fileName, taskDic, correctColumnNum, verbose=verbose, teeLogger=teeLogger,
			strict=strict, encoding=encoding, delimiter=delimiter, defaults=defaults,
			storeOffset=storeOffset)
	with _readLock(fileName), openFileAsCompressed(fileName, 'rb',encoding=encoding, teeLogger=teeLogger) as file:
		file.seek(0, os.SEEK_END)
		file_size = file.tell()
		buffer = b''
		position = file_size
		processedSize = 0

		while position > 0:
			# Read chunks from the end of the file
			read_size = min(chunk_size, position)
			position -= read_size
			file.seek(position)
			chunk = file.read(read_size)
			
			# Prepend new chunk to buffer
			buffer = chunk + buffer
			
			# Split the buffer into lines
			lines = buffer.split(b'\n')

			# When more of the file remains to the left, lines[0] may be an
			# incomplete line; defer it (it is carried into the next chunk via
			# `buffer = lines[0]` below) so it is counted/processed exactly once.
			# Only when we have reached the start of the file (position == 0) is
			# lines[0] a complete line that must be processed here.
			lowest = 1 if position > 0 else 0
			# Process lines from the last to the first
			for i in range(len(lines) - 1, lowest - 1, -1):
				processedSize += len(lines[i]) + 1  # +1 for the newline character
				if lines[i].strip():  # Skip empty lines
					# Process the line
					correctColumnNum, lineCache = _processLine(
						line=lines[i].decode(encoding=encoding,errors='replace'),
						taskDic=taskDic,
						correctColumnNum=correctColumnNum,
						strict=strict,
						delimiter=delimiter,
						defaults=defaults,
						storeOffset=storeOffset,
						offset=file_size - processedSize + 1
					)
					# If the line is valid, return it
					if lineCache:
						if storeOffset :
							return file_size - processedSize + 1
						return lineCache
			
			# Keep the last (possibly incomplete) line in buffer for the next read
			buffer = lines[0]

	# Return empty list if no valid line found
	if storeOffset:
		return -1
	return last_valid_line

@functools.lru_cache(maxsize=None)
def _get_sanitization_re(delimiter = ...):
	delimiter = get_delimiter(delimiter)
	return re.compile(r"(</sep/>|</LF/>|<sep>|<LF>|\n|" + re.escape(delimiter) + r")")

_sanitize_replacements = {
	"<sep>":"</sep/>",
	"<LF>":"</LF/>",
	"\n":"<LF>",
}
_inverse_sanitize_replacements = {v: k for k, v in _sanitize_replacements.items()}

def _sanitize(data,delimiter = ...):
	if not data:
		return data
	delimiter = get_delimiter(delimiter)
	def repl(m):
		tok = m.group(0)
		if tok == delimiter:
			return "<sep>"
		if tok == "</sep/>":
			eprint("Warning: '</sep/>' is a reserved token and is invalid as field data; "
				   "it will be read back as the literal '<sep>'.")
			return tok
		if tok == "</LF/>":
			eprint("Warning: '</LF/>' is a reserved token and is invalid as field data; "
				   "it will be read back as the literal '<LF>'.")
			return tok
		return _sanitize_replacements.get(tok, tok)
	pattern = _get_sanitization_re(delimiter)
	if isinstance(data,str):
		return pattern.sub(repl, data)
	else:
		return [pattern.sub(repl,str(segment)) if segment else '' for segment in data]

def _unsanitize(data,delimiter = ...):
	if not data:
		return data
	delimiter = get_delimiter(delimiter)
	def repl(m):
		tok = m.group(0)
		if tok == "<sep>":
			return delimiter
		return _inverse_sanitize_replacements.get(tok, tok)
	pattern = _get_sanitization_re(delimiter)
	if isinstance(data,str):
		return pattern.sub(repl, data.rstrip())
	else:
		return [pattern.sub(repl,str(segment).rstrip()) if segment else '' for segment in data]

def _formatHeader(header,verbose = False,teeLogger = None,delimiter = ...):
	"""
	Format the header string.

	Parameters:
	- header (str or list): The header string or list to format.
	- verbose (bool, optional): Whether to print verbose output. Defaults to False.
	- teeLogger (object, optional): The tee logger object for printing output. Defaults to None.

	Returns:
		list: The formatted header list of string.
	"""
	if isinstance(header,str):
		header = header.split(get_delimiter(delimiter))
	else:
		try:
			header = [str(s) for s in header]
		except Exception:
			if verbose:
				__teePrintOrNot('Invalid header, setting header to empty.','error',teeLogger=teeLogger)
			header = []
	return [s.rstrip() for s in header]

def _headerCellsMatch(header, cells):
	"""Whether the formatted line ``cells`` holds ``header`` (the rule of ``_lineContainHeader``)."""
	return len(header) == len(cells) and all(header[i] in cells[i] for i in range(len(header)))


def _sameCells(a, b):
	"""Whether the cell lists ``a`` and ``b`` are the same once the whitespace around each cell is taken off: the exact
	rule by which a header and a line 1 that widens it are told apart (_legacyReadHeader, _legacyWidened), never the
	containment of ``_headerCellsMatch``, under which a line 1 that only resembles the header would pass for it."""
	return len(a) == len(b) and all(x.strip() == y.strip() for x, y in zip(a, b))


def _lineOneCells(line, delimiter):
	"""The cells of ``line``, a legacy line 1, as a header and a line 1 that widens it, or that it widens, are compared
	(_legacyExtendsHeader, _legacyWidened): no line end or UTF-8 byte order mark (Excel writes one), no whitespace around a
	cell, and no empty cells at its end."""
	return _trimmedCells(cell.strip() for cell in line.rstrip('\r\n').lstrip('\ufeff').split(delimiter))


def _legacyAppendWidth(header, line, delimiter):
	"""The column count every writer pads a row it appends under ``header`` to, for a legacy file whose line 1 is ``line``
	(None: none yet): when line 1 holds the header or widens it (the same leading cells, _lineOneCells, then others that
	are named), as many cells as line 1 has, its empty cells at the end too, and at least the header's: a strict reader
	given no header counts them all and drops a narrower row. Else 0: the writer's own count stands. TSVZed, TSVZedLite,
	the CLI and a handler, and appendTabularFile all ask here."""
	if not header or not any(header) or line is None:
		return 0
	cells = _lineOneCells(line, delimiter)
	if len(cells) < len(header) or not _sameCells(header, cells[:len(header)]) or not all(cells[len(header):]):
		return 0
	return max(len(header), len(line.rstrip('\r\n').split(delimiter)))


def _legacyExtendsHeader(header, line, delimiter):
	"""The cell count of ``line``, a legacy line 1, when it is ``header`` widened by a program: the same leading cells
	(_lineOneCells), then others that are named; else 0. A store reads such a file at that count, loose: what its whole
	read does (_legacyReadHeader) and its rewrite's look at the same lines (_snapshotForRewrite) both ask here. The count
	is the one every writer pads to (_legacyAppendWidth)."""
	return _legacyAppendWidth(header, line, delimiter) if len(_lineOneCells(line, delimiter)) > len(header) else 0


def _headerMatches(header, line, delimiter):
	"""Whether ``line`` holds ``header``, as ``_lineContainHeader`` judges it, saying nothing."""
	return _headerCellsMatch(header, _formatHeader(line, delimiter=get_delimiter(delimiter)))


def _isHeaderLine(header, line, delimiter):
	"""Whether ``line`` is the header ``header`` to the CLI's ``--x-header`` rule: the same number of cells, each equal
	once the whitespace around it (and the line's end, and a UTF-8 byte order mark before it, as Excel writes one) is
	taken off. Stricter than 3.39's ``_lineContainHeader``."""
	cells = line.rstrip('\r\n').lstrip('\ufeff').split(delimiter)
	return len(cells) == len(header) and all(a.strip() == b.strip() for a, b in zip(cells, header))


def _headerMismatch(header, line, teeLogger, strict, delimiter):
	"""Print 3.39's "Header mismatch" (as ``_lineContainHeader`` prints it) for ``line``; raise as it does when strict."""
	__teePrintOrNot(f"Header mismatch: \n{_formatHeader(line, delimiter=delimiter)} \n!= \n{header}",teeLogger=teeLogger)
	if strict:
		raise ValueError("Data format error! Header mismatch")


def _lineContainHeader(header,line,verbose = False,teeLogger = None,strict = False,delimiter = ...):
	"""
	Verify if a line contains the header.

	Parameters:
	- header (str): The header string to verify.
	- line (str): The line to verify against the header.
	- verbose (bool, optional): Whether to print verbose output. Defaults to False.
	- teeLogger (object, optional): The tee logger object for printing output. Defaults to None.
	- strict (bool, optional): Whether to raise an exception if there is a data format error. Defaults to False.

	Returns:
	bool: True if the header matches the line, False otherwise.
	"""
	delimiter = get_delimiter(delimiter)
	line = _formatHeader(line,verbose=verbose,teeLogger=teeLogger,delimiter=delimiter)
	if verbose:
		__teePrintOrNot(f"Header: \n{header}",teeLogger=teeLogger)
		__teePrintOrNot(f"First line: \n{line}",teeLogger=teeLogger)
	if not _headerCellsMatch(header, line):
		__teePrintOrNot(f"Header mismatch: \n{line} \n!= \n{header}",teeLogger=teeLogger)
		if strict:
			raise ValueError("Data format error! Header mismatch")
		return False
	return True

def _verifyFileExistence(fileName,createIfNotExist = True,teeLogger = None,header = [],encoding = 'utf8',strict = True,delimiter = ...,errors = 'strict'):
	"""
	Verify the existence of the tabular file.

	Parameters:
	- fileName (str): The path of the tabular file.
	- createIfNotExist (bool, optional): Whether to create the file if it doesn't exist. Defaults to True.
	- teeLogger (object, optional): The tee logger object for printing output. Defaults to None.
	- header (list, optional): The header line to verify against. Defaults to [].
	- encoding (str, optional): The encoding of the file. Defaults to 'utf8'.
	- strict (bool, optional): Whether to raise an exception if there is a data format error. Defaults to True.
	- errors (str, optional): What to do with text the encoding cannot hold, such as a lone surrogate from a non-UTF-8 file name (os.fsdecode). 'strict' (the default) raises a ValueError (TSVZEncodeError) naming it, before anything is written; 'replace' writes '?' instead.

	Returns:
	bool: True if the file exists, False otherwise.
	"""
	delimiter = get_delimiter(delimiter, file_name=fileName)
	remainingFileName, _ ,extenstionName = fileName.lower().rpartition('.')
	if extenstionName in COMPRESSED_FILE_EXTENSIONS:
		remainingFileName, _ ,extenstionName = remainingFileName.rpartition('.')
	if delimiter and delimiter == '\t' and not extenstionName == 'tsv':
		__teePrintOrNot(f'Warning: Filename {fileName} does not end with .tsv','warning',teeLogger=teeLogger)
	elif delimiter and delimiter == ',' and not extenstionName == 'csv':
		__teePrintOrNot(f'Warning: Filename {fileName} does not end with .csv','warning',teeLogger=teeLogger)
	elif delimiter and delimiter == '\0' and not extenstionName == 'nsv':
		__teePrintOrNot(f'Warning: Filename {fileName} does not end with .nsv','warning',teeLogger=teeLogger)
	elif delimiter and delimiter == '|' and not extenstionName == 'psv':
		__teePrintOrNot(f'Warning: Filename {fileName} does not end with .psv','warning',teeLogger=teeLogger)
	if not os.path.isfile(fileName):
		if createIfNotExist:
			# Encode first: a header that cannot be written must not leave an empty file behind.
			headerBytes = _encodeRow(_sanitize(_formatHeader(header,
											 teeLogger=teeLogger,
											 delimiter=delimiter,
											 ),delimiter=delimiter),delimiter,encoding,errors,what='header')
			try:
				codec = _fileCodec(fileName)
				data = _compressBytes(codec, headerBytes + b'\n') if codec else headerBytes + b'\n'  # before: no gap
				# Not over a file another writer made since it was looked for ('xb'), nor a link (its target is made),
				# and under the lock, as every writer writes: an append that took it first has written the header.
				with _pathLock(fileName), open(os.path.realpath(fileName), 'xb') as file:  # lockf excludes no thread
					_lockFile(file, mode=_APPEND)
					try:
						if not os.fstat(file.fileno()).st_size:
							file.write(data)
							file.flush()
					finally:
						_seekRawStart(file)  # msvcrt unlocks from the raw position: the range it locked
						_unlockFile(file)
				__teePrintOrNot('Created '+fileName,teeLogger=teeLogger)
				return True
			except FileExistsError:
				if os.path.exists(fileName) and not os.path.isdir(fileName):
					return True  # another writer made it meanwhile (not a directory, nor a link that leads nowhere)
			except Exception:
				pass
			__teePrintOrNot('Failed to create '+fileName,'error',teeLogger=teeLogger)
			if strict:
				raise FileNotFoundError("Failed to create file")
			return False
		elif strict:
			__teePrintOrNot('File not found','error',teeLogger=teeLogger)
			raise FileNotFoundError("File not found")
		else:
			return False
	return True

def readTSV(fileName,teeLogger = None,header = '',createIfNotExist = False, lastLineOnly = False,verifyHeader = True,
			verbose = False,taskDic = None,encoding = 'utf8',strict = True,delimiter = '\t',defaults = ...,
			correctColumnNum = -1,errors = 'strict'):
	"""
	Compatibility method, calls readTabularFile. 
	Read a Tabular (CSV / TSV / NSV) file and return the data as a dictionary.

	Parameters:
	- fileName (str): The path to the Tabular file.
	- teeLogger (Logger, optional): The logger object to log messages. Defaults to None.
	- header (str or list, optional): The header of the Tabular file. If a string, it should be a tab-separated list of column names. If a list, it should contain the column names. Defaults to ''.
	- createIfNotExist (bool, optional): Whether to create the file if it doesn't exist. Defaults to False.
	- lastLineOnly (bool, optional): Whether to read only the last valid line of the file. Defaults to False.
	- verifyHeader (bool, optional): Whether to verify the header of the file. Defaults to True.
	- verbose (bool, optional): Whether to print verbose output. Defaults to False.
	- taskDic (OrderedDict, optional): The dictionary to store the data. Defaults to an empty OrderedDict.
	- encoding (str, optional): The encoding of the file. Defaults to 'utf8'.
	- strict (bool, optional): Whether to raise an exception if there is a data format error. Defaults to True.
	- delimiter (str, optional): The delimiter used in the Tabular file. Defaults to '\t'.
	- defaults (list or str, optional): The default values to use for missing columns: a list, whose first cell stands in the key's column, or text, which TSVZed reads the same (split at the delimiter, '#_defaults_#' first or not). The caller's list is left as it is. Defaults to [].
	- correctColumnNum (int, optional): The expected number of columns in the file. If -1, it will be determined from the first valid line. Defaults to -1.

	Returns:
	- OrderedDict: The dictionary containing the data from the Tabular file.

	Raises:
	- Exception: If the file is not found or there is a data format error.

	"""
	errors = _checkErrors(errors)
	return readTabularFile(fileName,teeLogger = teeLogger,header = header,createIfNotExist = createIfNotExist,
						   lastLineOnly = lastLineOnly,verifyHeader = verifyHeader,verbose = verbose,taskDic = taskDic,
						   encoding = encoding,strict = strict,delimiter = delimiter,defaults=defaults,
						   correctColumnNum = correctColumnNum,errors = errors)

def readTabularFile(fileName,teeLogger = None,header = '',createIfNotExist = False, lastLineOnly = False,verifyHeader = True,
					verbose = False,taskDic = None,encoding = 'utf8',strict = True,delimiter = ...,defaults = ...,
					correctColumnNum = -1,storeOffset = False,errors = 'strict'):
	"""
	Read a Tabular (CSV / TSV / NSV) file and return the data as a dictionary.

	Parameters:
	- fileName (str): The path to the Tabular file.
	- teeLogger (Logger, optional): The logger object to log messages. Defaults to None.
	- header (str or list, optional): The header of the Tabular file. If a string, it should be a tab-separated list of column names. If a list, it should contain the column names. Defaults to ''.
	- createIfNotExist (bool, optional): Whether to create the file if it doesn't exist. Defaults to False.
	- lastLineOnly (bool, optional): Whether to read only the last valid line of the file. Defaults to False.
	- verifyHeader (bool, optional): Whether to verify the header of the file. Defaults to True.
	- verbose (bool, optional): Whether to print verbose output. Defaults to False.
	- taskDic (OrderedDict, optional): The dictionary to store the data. Defaults to an empty OrderedDict.
	- encoding (str, optional): The encoding of the file. Defaults to 'utf8'.
	- strict (bool, optional): Whether to raise an exception if there is a data format error. Defaults to True.
	- delimiter (str, optional): The delimiter used in the Tabular file. Defaults to '\t' for TSV, ',' for CSV, '\0' for NSV.
	- defaults (list or str, optional): The default values to use for missing columns: a list, whose first cell stands in the key's column, or text, which TSVZed reads the same (split at the delimiter, '#_defaults_#' first or not). The caller's list is left as it is. Defaults to [].
	- correctColumnNum (int, optional): The expected number of columns in the file. If -1, it will be determined from the first valid line. Defaults to -1.
	- storeOffset (bool, optional): Instead of storing the data in taskDic, store the offset of each line. Defaults to False.
	- errors (str, optional): Reading replaces invalid UTF-8 with U+FFFD whatever this is; it applies only to the header written when createIfNotExist creates the file: 'strict' (the default) raises a ValueError for text the encoding cannot hold, 'replace' writes '?'.
	
	Returns:
	- OrderedDict: The dictionary containing the data from the Tabular file.

	Raises:
	- Exception: If the file is not found or there is a data format error.

	"""
	errors = _checkErrors(errors)
	if _isSpecPath(fileName):
		return _specReadTabularFile(fileName, teeLogger=teeLogger, header=header, createIfNotExist=createIfNotExist,
									lastLineOnly=lastLineOnly, verifyHeader=verifyHeader, verbose=verbose,
									taskDic=taskDic, encoding=encoding, strict=strict, delimiter=delimiter,
									defaults=defaults, correctColumnNum=correctColumnNum, storeOffset=storeOffset,
									errors=errors)
	delimiter = get_delimiter(delimiter,file_name=fileName)
	return _legacyRead(fileName, teeLogger, header, createIfNotExist, lastLineOnly, verifyHeader, verbose, taskDic, encoding,
					   strict, delimiter, _readerDefaults(defaults, delimiter), correctColumnNum, storeOffset, errors)[0]


def _legacyRead(fileName, teeLogger, header, createIfNotExist, lastLineOnly, verifyHeader, verbose, taskDic, encoding, strict,
				delimiter, defaults, correctColumnNum, storeOffset, errors, seen=None):
	"""readTabularFile for a legacy file: ``(what it returns, the column count the read took)``, -1 for none.

	``delimiter`` is resolved, and ``defaults`` a list (_readerDefaults) that the read works on, so it ends as the
	defaults in effect at the end of the file: a caller who needs those (TSVZedLite) passes its own. ``seen``: a list
	that ends as [the line 1 the read met] when a header is verified (see _legacyReadHeader)."""
	if taskDic is None:
		taskDic = {}
	header = _formatHeader(header,verbose = verbose,teeLogger = teeLogger, delimiter = delimiter)
	if not _verifyFileExistence(fileName,createIfNotExist = createIfNotExist,teeLogger = teeLogger,header = header,encoding = encoding,strict = strict,delimiter=delimiter,errors = errors):
		return taskDic, -1
	if _isCompressedFile(fileName) and _fileHoldsNothing(fileName):
		# No byte, or only empty streams: an empty store, as an empty uncompressed file is (below), not a damaged
		# stream (BZ2File and LZMAFile fail on a file with no byte).
		return ((-1 if storeOffset else []) if lastLineOnly else taskDic), -1
	reporter = _Reporter(fileName, teeLogger)
	try:
		# Under the READ lock: another process's rewrite is waited out (a nested read of the file takes no second lock).
		with _readLock(fileName), openFileAsCompressed(fileName, mode ='rb',encoding=encoding,teeLogger=teeLogger) as file:
			start = _legacyReadHeader(file, fileName, header, verifyHeader, verbose, teeLogger, strict, delimiter,
									  correctColumnNum, encoding, reporter, lastLineOnly, seen=seen)
			if start is None:
				return taskDic, -1
			correctColumnNum, lineNo = start
			if lastLineOnly:
				return read_last_valid_line(fileName, taskDic, correctColumnNum, verbose=verbose, teeLogger=teeLogger, strict=strict, delimiter=delimiter, defaults=defaults,storeOffset=storeOffset), correctColumnNum
			correctColumnNum = _legacyReadLoop(file, fileName, taskDic, correctColumnNum, lineNo, strict, delimiter, defaults,
											   storeOffset, encoding, reporter)[0]
	finally:
		reporter.flush()
	return taskDic, correctColumnNum


def _legacyReadHeader(file, fileName, header, verifyHeader, verbose, teeLogger, strict, delimiter, correctColumnNum,
					  encoding, reporter, lastLineOnly=False, widening=False, said=None, seen=None):
	"""Read and check the header line of the open legacy ``file`` when ``header`` is to be verified (readTabularFile).

	Returns ``(correctColumnNum, lineNo)``, the column count and the lines read, or None when a compressed stream
	is damaged inside the header line (noted on ``reporter``: nothing more is read, L5). ``widening``, a store's own
	read: a line 1 that extends ``header`` (the same leading cells, then others that are named) is a program's widening
	of it, not a mismatch: it is the header, and the column count is its own. ``said``, a store's own reads: a list of one
	item, the line 1 whose mismatch was last said; a read says it once for the line, as 3.39 does (a strict one raises
	before it is set).
	"""
	if not (any(header) and verifyHeader):
		return correctColumnNum, 0
	try:
		raw = file.readline()
	except Exception as e:
		if lastLineOnly or not _isCompressedFile(fileName):
			raise
		reporter.note('damaged', None, _streamDamage(e))
		return None
	try:
		line = raw.decode(encoding=encoding)
	except UnicodeDecodeError:
		line = raw.decode(encoding=encoding,errors='replace')
		reporter.note('decode', 'line 1', 'invalid {} replaced with U+FFFD'.format(encoding))
	if seen is not None:
		seen[:] = [line if raw else None]
	wider = _legacyExtendsHeader(header, line, delimiter) if widening and raw else 0
	if wider:
		return (wider if correctColumnNum == -1 else correctColumnNum), 1
	# A file that holds nothing (no byte, or only empty compressed streams) is an empty store with this header, as
	# an append to it takes it (it writes the header first): not a header mismatch.
	if said is not None and raw and said[0] == line:
		matches = _headerMatches(header, line, delimiter)  # (said already for this line)
	else:
		matches = not raw or _lineContainHeader(header,line,verbose = verbose,teeLogger = teeLogger,strict = strict,delimiter = delimiter)
		if said is not None and not matches:
			said[0] = line
	if matches and correctColumnNum == -1:
		correctColumnNum = len(header)
		if verbose:
			__teePrintOrNot(f"correctColumnNum: {correctColumnNum}",teeLogger=teeLogger)
	return correctColumnNum, 1


def _legacyReadLoop(file, fileName, taskDic, correctColumnNum, lineNo, strict, delimiter, defaults, storeOffset,
					encoding, reporter):
	"""3.39's read loop over the rest of an open legacy file (``readTabularFile``).

	Returns ``(correctColumnNum, committed, lineNo)``: the column count after
	the last line, the offset just past the last line that ends in b'\\n'
	(decompressed bytes for a compressed file) and the number of lines read.
	"""
	committed = file.tell()
	lines = iter(file)
	while True:
		try:
			line = next(lines)
		except StopIteration:
			break
		except Exception as e:
			# L5: a damaged compressed stream ends the read instead of raising.
			if not _isCompressedFile(fileName):
				raise
			reporter.note('damaged', None, _streamDamage(e))
			break
		lineNo += 1
		try:
			text = line.decode(encoding=encoding)
		except UnicodeDecodeError:
			text = line.decode(encoding=encoding,errors='replace')
			reporter.note('decode', 'line {}'.format(lineNo), 'invalid {} replaced with U+FFFD'.format(encoding))
		position = file.tell()
		correctColumnNum, _ = _processLine(text,taskDic,correctColumnNum,strict = strict,delimiter=delimiter,defaults = defaults,storeOffset=storeOffset,offset=position-len(line),reporter=reporter)
		if line.endswith(b'\n'):
			committed = position
	return correctColumnNum, committed, lineNo

def appendTSV(fileName,lineToAppend,teeLogger = None,header = '',createIfNotExist = False,verifyHeader = True,verbose = False,encoding = 'utf8', strict = True, delimiter = '\t', errors = 'strict'):
	"""
	Compatibility method, calls appendTabularFile.
	Append a line of data to a Tabular file.
	Parameters:
	- fileName (str): The path of the Tabular file.
	- lineToAppend (str or list): The line of data to append. If it is a string, it will be split by delimiter to form a list.
	- teeLogger (optional): A logger object for logging messages.
	- header (str or list, optional): The header line to verify against. If provided, the function will check if the existing header matches the provided header.
	- createIfNotExist (bool, optional): If True, the file will be created if it does not exist. If False and the file does not exist, an exception will be raised.
	- verifyHeader (bool, optional): If True, the function will verify if the existing header matches the provided header. If False, the header will not be verified.
	- verbose (bool, optional): If True, additional information will be printed during the execution.
	- encoding (str, optional): The encoding of the file.
	- strict (bool, optional): If True, the function will raise an exception if there is a data format error. If False, the function will ignore the error and continue.
	- delimiter (str, optional): The delimiter used in the Tabular file. Defaults to '\t' for TSV, ',' for CSV, '\0' for NSV.
	- errors (str, optional): What to do with text the encoding cannot hold, such as a lone surrogate from a non-UTF-8 file name (os.fsdecode). 'strict' (the default) raises a ValueError (TSVZEncodeError) naming it, before anything is written; 'replace' writes '?' instead.
	Raises:
	- Exception: If the file does not exist and createIfNotExist is False.
	- Exception: If the existing header does not match the provided header.
	- ValueError (TSVZEncodeError): If a value, key or header cannot be encoded and errors is 'strict'. Nothing is written.
	"""
	return appendTabularFile(fileName,lineToAppend,teeLogger = teeLogger,header = header,createIfNotExist = createIfNotExist,verifyHeader = verifyHeader,verbose = verbose,encoding = encoding, strict = strict, delimiter = delimiter, errors = errors)

def appendTabularFile(fileName,lineToAppend,teeLogger = None,header = '',createIfNotExist = False,verifyHeader = True,verbose = False,encoding = 'utf8', strict = True, delimiter = ..., errors = 'strict'):
	"""
	Append a line of data to a Tabular file.
	Parameters:
	- fileName (str): The path of the Tabular file.
	- lineToAppend (str or list): The line of data to append. If it is a string, it will be split by delimiter to form a list.
	- teeLogger (optional): A logger object for logging messages.
	- header (str or list, optional): The header line to verify against. If provided, the function will check if the existing header matches the provided header.
	- createIfNotExist (bool, optional): If True, the file will be created if it does not exist. If False and the file does not exist, an exception will be raised.
	- verifyHeader (bool, optional): If True, the function will verify if the existing header matches the provided header. If False, the header will not be verified.
	- verbose (bool, optional): If True, additional information will be printed during the execution.
	- encoding (str, optional): The encoding of the file.
	- strict (bool, optional): If True, the function will raise an exception if there is a data format error. If False, the function will ignore the error and continue.
	- delimiter (str, optional): The delimiter used in the Tabular file. Defaults to '\t' for TSV, ',' for CSV, '\0' for NSV.
	- errors (str, optional): What to do with text the encoding cannot hold, such as a lone surrogate from a non-UTF-8 file name (os.fsdecode). 'strict' (the default) raises a ValueError (TSVZEncodeError) naming it, before anything is written; 'replace' writes '?' instead.
	Raises:
	- Exception: If the file does not exist and createIfNotExist is False.
	- Exception: If the existing header does not match the provided header.
	- ValueError (TSVZEncodeError): If a value, key or header cannot be encoded and errors is 'strict'. Nothing is written.
	"""
	errors = _checkErrors(errors)
	return appendLinesTabularFile(fileName,[lineToAppend],teeLogger = teeLogger,header = header,createIfNotExist = createIfNotExist,verifyHeader = verifyHeader,verbose = verbose,encoding = encoding, strict = strict, delimiter = delimiter, errors = errors)

def appendLinesTabularFile(fileName,linesToAppend,teeLogger = None,header = '',createIfNotExist = False,verifyHeader = True,verbose = False,encoding = 'utf8', strict = True, delimiter = ..., errors = 'strict'):
	"""
	Append lines of data to a Tabular file.
	Parameters:
	- fileName (str): The path of the Tabular file.
	- linesToAppend (list): The lines of data to append. If it is a list of string, then each string will be split by delimiter to form a list.
	- teeLogger (optional): A logger object for logging messages.
	- header (str or list, optional): The header line to verify against. If provided, the function will check if the existing header matches the provided header.
	- createIfNotExist (bool, optional): If True, the file will be created if it does not exist. If False and the file does not exist, an exception will be raised.
	- verifyHeader (bool, optional): If True, the function will verify if the existing header matches the provided header. If False, the header will not be verified.
	- verbose (bool, optional): If True, additional information will be printed during the execution.
	- encoding (str, optional): The encoding of the file.
	- strict (bool, optional): If True, the function will raise an exception if there is a data format error. If False, the function will ignore the error and continue.
	- delimiter (str, optional): The delimiter used in the Tabular file. Defaults to '\t' for TSV, ',' for CSV, '\0' for NSV.
	- errors (str, optional): What to do with text the encoding cannot hold, such as a lone surrogate from a non-UTF-8 file name (os.fsdecode). 'strict' (the default) raises a ValueError (TSVZEncodeError) naming it, before anything is written; 'replace' writes '?' instead.
	Raises:
	- Exception: If the file does not exist and createIfNotExist is False.
	- Exception: If the existing header does not match the provided header.
	- ValueError (TSVZEncodeError): If a value, key or header cannot be encoded and errors is 'strict'. Nothing is written.
	"""
	errors = _checkErrors(errors)
	if _isSpecPath(fileName):
		return _specAppendLinesTabularFile(fileName, linesToAppend, teeLogger=teeLogger, header=header,
										   createIfNotExist=createIfNotExist, verifyHeader=verifyHeader,
										   verbose=verbose, encoding=encoding, strict=strict, delimiter=delimiter,
										   errors=errors)
	delimiter = get_delimiter(delimiter,file_name=fileName)
	header = _formatHeader(header,verbose = verbose,teeLogger = teeLogger,delimiter=delimiter)
	rows = _legacyRows(linesToAppend, delimiter)
	formatted = None
	if createIfNotExist and _mayRaise(errors) and not os.path.isfile(fileName):
		# The file is about to be created with ``header``: format, and so look at, what is written to it before it
		# exists, so that a refused row leaves nothing behind.
		formatted = _legacyFormatPayload(fileName, linesToAppend, teeLogger, header, verifyHeader, verbose, encoding,
										 strict, delimiter, errors, rows, assume=header)
	if not _verifyFileExistence(fileName,createIfNotExist = createIfNotExist,teeLogger = teeLogger,header = header,encoding = encoding,strict = strict,delimiter = delimiter,errors = errors):
		return
	headerLine = None
	if any(header):
		# An existing empty file gets the header first, as one created here does (_appendOpen, which looks under the
		# file lock and encodes it only then: a header that cannot be encoded refuses only an append that writes it,
		# before anything is written). Also when the file was missing above: another process may have made it, empty,
		# before _verifyFileExistence looked. Its rows are formatted as for a file that begins with it.
		headerLine = functools.partial(_encodeRow, _sanitize(header,delimiter=delimiter), delimiter, encoding, errors, what='header')
		if formatted is None and _fileHoldsNothing(fileName):
			formatted = _legacyFormatPayload(fileName, linesToAppend, teeLogger, header, verifyHeader, verbose, encoding,
											 strict, delimiter, errors, rows, assume=header)
	payload, count = formatted or _legacyFormatPayload(fileName, linesToAppend, teeLogger, header, verifyHeader, verbose,
													   encoding, strict, delimiter, errors, rows)
	_legacyWritePayload(fileName, payload, count, teeLogger, verbose, encoding, headerLine)


def _legacyWritePayload(fileName, payload, count, teeLogger, verbose, encoding, header=None):
	"""Append the ``payload`` of ``count`` rows to the existing file (all of it, or nothing), after ``header`` (the
	header line or a function that makes it, None for none) when the file is empty (_appendOpen)."""
	if not count:
		if verbose:
			__teePrintOrNot(f"No lines to append to {fileName}",teeLogger=teeLogger)
		return
	codec = _fileCodec(fileName)
	_requireCodec(codec)
	# L1: a last line without '\n' is a record to the 3.39 reader; terminate it
	# instead of gluing the new record onto it. Compressed or not, under the file's locks.
	reporter = _Reporter(fileName, teeLogger)
	try:
		_lockedAppend(fileName, payload, 'newline', reporter, header=header, codec=codec)
	finally:
		reporter.flush()
	if verbose:
		__teePrintOrNot(f"Appended {count} lines to {fileName}",teeLogger=teeLogger)


def _legacyRows(linesToAppend, delimiter):
	"""``linesToAppend`` as sanitised lists of cells, the key first for a dict, as 3.39 formats them."""
	formatedLines = []
	for line in linesToAppend:
		if isinstance(linesToAppend,dict):
			key = line
			line = linesToAppend[key]
		if isinstance(line,str):
			line = line.split(delimiter)
		elif line:
			# Build a new list rather than mutating the caller's list in place.
			newLine = []
			for item in line:
				if isinstance(item,str):
					newLine.append(item)
				else:
					try:
						newLine.append(str(item).rstrip())
					except Exception as e:
						newLine.append(str(e))
			line = newLine
		if isinstance(linesToAppend,dict):
			if (not line or line[0] != key):
				line = [key]+line
		formatedLines.append(_sanitize(line,delimiter=delimiter))
	return formatedLines


def _legacyFormatPayload(fileName, linesToAppend, teeLogger, header, verifyHeader, verbose, encoding, strict, delimiter,
						 errors='strict', rows=None, assume=None, given=None, first=None):
	"""Format rows as 3.39's ``appendLinesTabularFile`` writes them; return ``(payload, count)``.

	``header`` is a formatted header list and ``delimiter`` a resolved
	delimiter. Rows are sanitised and padded to the column count: the
	header's when the file's first line matches it, else the longest row's;
	a wider row is written whole, less its empty cells past the count (_fitRow).
	The bytes are what is written, so a ``TSVZEncodeError`` names a cell that
	would be written. ``rows`` are the rows already made
	by ``_legacyRows``. ``assume`` is the header list that the file's first line
	is (or is to be) when the file is not to be read: the file is about to be
	created, or emptied with that header left. ``given``: the header the caller was given, when ``header`` is another one
	(the CLI's in effect, none when line 1 is a row to it): every row is padded to what line 1 says of it
	(_legacyAppendWidth), as every writer pads. ``first``: the file's first line as text, when the caller has just read
	it (``assume`` stands in for it otherwise): not read again.
	"""
	formatedLines = _legacyRows(linesToAppend, delimiter) if rows is None else rows
	if not formatedLines:
		return b'', 0
	correctColumnNum = max([len(line) for line in formatedLines])
	given = header if given is None else given
	line = None
	if (any(header) or any(given)) and verifyHeader:
		if assume is not None:
			line = delimiter.join(assume)
		elif first is not None:
			line = first
		else:
			line = _legacyFirstLine(fileName, encoding, teeLogger)
	if any(header) and verifyHeader:
		if _lineContainHeader(header,line,verbose = verbose,teeLogger = teeLogger,strict = strict,delimiter = delimiter):
			correctColumnNum = len(header)
			if verbose:
				__teePrintOrNot(f"correctColumnNum: {correctColumnNum}",teeLogger=teeLogger)
	wide = max(correctColumnNum, _legacyAppendWidth(given, line, delimiter)) if line is not None and verifyHeader else correctColumnNum
	# pad the lines to the correct number of columns; a wider one keeps its cells (writes never cut). A row that holds a value
	# is as wide as line 1 counts its cells (_legacyAppendWidth); a deletion line (no value) is not: it is as 5.07 wrote it,
	# which a reader that counts cells the spec's way (tsvz-rs) reads as the deletion it is
	fitted = [_fitRow(row, wide if any(row[1:]) else correctColumnNum) for row in formatedLines]
	payload = b'\n'.join([_encodeRow(line,delimiter,encoding,errors,header) for line in fitted]) + b'\n'
	return payload, len(fitted)

def _legacyFirstLine(fileName, encoding, teeLogger):
	"""The first line of the legacy file ``fileName`` (which exists) as text; '' for a file that holds nothing: no byte,
	or, compressed, only empty streams (a 0-byte .bz2 or .xz is no stream to read, as _fileHoldsNothing's readers know).
	Read under the READ lock (_readLock), as every read is: a header check runs before the append takes its lock, and
	met part way, another process's rewrite would read as a damaged first member, or as half its line 1."""
	with _readLock(fileName):
		if _isCompressedFile(fileName) and _fileHoldsNothing(fileName):
			return ''
		codec = _fileCodec(fileName)
		if codec and _codecSupported(codec):  # (else read as openFileAsCompressed falls back to reading it)
			with open(fileName, 'rb') as f:
				return _firstLineIn(f, codec).decode(encoding=encoding,errors='replace')
		with openFileAsCompressed(fileName, mode ='rb',encoding=encoding,teeLogger=teeLogger) as file:
			return file.readline().decode(encoding=encoding,errors='replace')

def _legacyKeptHeader(fileName, header, verifyHeader, verbose, teeLogger, encoding, strict, delimiter, warn=True):
	"""The header ``clearTabularFile`` leaves in the file: the file's own first line when a header is given and
	``verifyHeader`` (the header given for a file that holds nothing), else ``header``; [] when it has no cell: the file
	is then left with no header line."""
	if any(header) and verifyHeader:
		try:
			line = _legacyFirstLine(fileName, encoding, teeLogger)
		except Exception:  # a first line the codec cannot decode (gzip: BadGzipFile, zlib.error; bz2, xz, zstd: their own errors): none to keep
			line = ''  # (a scrub or clear makes the file again; an append, which calls _legacyFirstLine itself, raises)
		if line:  # a file that holds nothing ('') is an empty store with any header, as the readers take it: the one given is kept
			if warn and not _lineContainHeader(header,line,verbose = verbose,teeLogger = teeLogger,strict = strict,delimiter = delimiter):
				__teePrintOrNot(f'Warning: Header mismatch in {fileName}. Keeping original header in file...','warning',teeLogger)
			header = _formatHeader(line,verbose = verbose,teeLogger = teeLogger,delimiter=delimiter)
	return header if any(header) else []  # no header: no header line (3.39 wrote an empty one, read back as a row)


def clearTSV(fileName,teeLogger = None,header = '',verifyHeader = False,verbose = False,encoding = 'utf8',strict = False,delimiter = '\t',errors = 'strict'):
	"""
	Compatibility method, calls clearTabularFile.
	Clear the contents of a Tabular file. Will create if not exist.
	Parameters:
	- fileName (str): The path of the Tabular file.
	- teeLogger (optional): A logger object for logging messages.
	- header (str or list, optional): The header line to verify against. If provided, the function will check if the existing header matches the provided header.
	- verifyHeader (bool, optional): If True, the function will verify if the existing header matches the provided header. If False, the header will not be verified.
	- verbose (bool, optional): If True, additional information will be printed during the execution.
	- encoding (str, optional): The encoding of the file.
	- strict (bool, optional): If True, the function will raise an exception if there is a data format error. If False, the function will ignore the error and continue.
	- errors (str, optional): What to do with text the encoding cannot hold, such as a lone surrogate from a non-UTF-8 file name (os.fsdecode). 'strict' (the default) raises a ValueError (TSVZEncodeError) naming it, before anything is written; 'replace' writes '?' instead.
	"""
	return clearTabularFile(fileName,teeLogger = teeLogger,header = header,verifyHeader = verifyHeader,verbose = verbose,encoding = encoding,strict = strict,delimiter = delimiter,errors = errors)

def clearTabularFile(fileName,teeLogger = None,header = '',verifyHeader = False,verbose = False,encoding = 'utf8',strict = False,delimiter = ...,errors = 'strict'):
	"""
	Clear the contents of a Tabular file. Will create if not exist.
	Parameters:
	- fileName (str): The path of the Tabular file.
	- teeLogger (optional): A logger object for logging messages.
	- header (str or list, optional): The header line to verify against. If provided, the function will check if the existing header matches the provided header.
	- verifyHeader (bool, optional): If True, the function will verify if the existing header matches the provided header. If False, the header will not be verified.
	- verbose (bool, optional): If True, additional information will be printed during the execution.
	- encoding (str, optional): The encoding of the file.
	- strict (bool, optional): If True, the function will raise an exception if there is a data format error. If False, the function will ignore the error and continue.
	- errors (str, optional): What to do with text the encoding cannot hold, such as a lone surrogate from a non-UTF-8 file name (os.fsdecode). 'strict' (the default) raises a ValueError (TSVZEncodeError) naming it, before anything is written; 'replace' writes '?' instead.
	"""
	errors = _checkErrors(errors)
	if _isSpecPath(fileName):
		_specClearTabularFile(fileName, teeLogger=teeLogger, header=header, verifyHeader=verifyHeader,
							  verbose=verbose, encoding=encoding, strict=strict, delimiter=delimiter, errors=errors)
		return
	delimiter = get_delimiter(delimiter,file_name=fileName)
	header = _formatHeader(header,verbose = verbose,teeLogger = teeLogger,delimiter=delimiter)
	if not _verifyFileExistence(fileName,createIfNotExist = True,teeLogger = teeLogger,header = header,encoding = encoding,strict = False,delimiter=delimiter,errors = errors):
		raise FileNotFoundError("Something catastrophic happened! File still not found after creation")
	kept = _legacyKeptHeader(fileName, header, verifyHeader, verbose, teeLogger, encoding, strict, delimiter)
	# With no header the file keeps the empty first line that stands for it, as 3.39 and 5.0 left it (a reader given a
	# header the file lacks takes that line for it, rather than a row).
	_legacyReplace(fileName, kept, True, encoding, delimiter, errors)
	if verbose:
		__teePrintOrNot(f"Cleared {fileName}",teeLogger=teeLogger)

def _isRowLine(text, delimiter):
	"""True when the line ``text`` is a data row to a reader told no header: not what ``_processLine`` skips (empty, a
	comment, a line with no key) and not a defaults line. A first line that is not one is where a reader given a header
	the file lacks loses nothing: it takes that line for the header."""
	line = _legacyLineBody(text)
	if not line or (line.startswith('#') and not line.startswith(DEFAULTS_INDICATOR_KEY)):
		return False
	cells = _unsanitize(line.split(delimiter),delimiter)
	return bool(cells and cells[0] and cells[0] != DEFAULTS_INDICATOR_KEY)

def _legacyBeginsBlankIn(f, encoding, delimiter):
	"""True when the open binary legacy file ``f`` (at its raw level) holds nothing, cannot be read, or begins with a
	line that is not a data row (_isRowLine): then a header-less whole write puts the empty line that stands for a
	header in front of its rows, as 3.39 and 5.0 always do. Read through that descriptor and left open: a lockf lock is dropped by closing any descriptor of the file, so a writer that holds one must not open the
	file again to look. ``f`` is left at offset 0."""
	codec = _fileCodec(f.name)
	try:
		f.flush()
		if _holdsNothing(f, codec, os.fstat(f.fileno()).st_size):
			return True
		f.seek(0)
		line = _firstLineIn(f, codec) if codec else f.readline()
		return not _isRowLine(line.decode(encoding or 'utf8', errors='replace'), delimiter)
	except Exception:
		return True
	finally:
		f.seek(0)

def _legacyReplace(fileName, kept, blank, encoding, delimiter, errors, payload=b'', expect=None):
	"""Make the existing legacy file ``fileName`` hold ``kept``, the header list it keeps (_legacyKeptHeader), and then
	``payload`` (whole lines): the one write of ``clearTabularFile`` and ``scrubTabularFile``. ``delimiter`` is a
	resolved delimiter. Where ``kept`` is empty, ``blank`` says whether the file begins with an empty line in its
	place (never in front of a first line that is a row).

	The new bytes are made whole first, then written over the file from its start and the file is cut there, under
	its locks (_specRewriteInPlace: spec §18.7 / §19.8, as ``TSVZed``'s whole rewrites do), so a reader that takes no
	lock never meets the file emptied. A compressed file is written as whole members of at most 1 MiB. Nothing is
	written when the header cannot be encoded. ``expect``, the ``_specStamp`` of the file as a caller read it: the
	file is left alone when it no longer matches under the lock (another writer committed since), and None is
	returned; else the result is not None."""
	# Encoded before the file is written to, which would leave it cut short.
	headerBytes = _encodeRow(_sanitize(kept,delimiter=delimiter),delimiter,encoding,errors,what='header') + b'\n' if kept else b'\n' if blank else b''
	if not stat.S_ISREG(os.stat(fileName).st_mode):
		with open(fileName, 'wb') as f:  # /dev/null holds nothing to empty, and cannot be cut or given space
			f.write(headerBytes + payload)
		return True, None
	return _specRewriteInPlace(fileName, headerBytes + payload, expect=expect)

def getFileUpdateTimeNs(fileName):
	# return 0 if the file does not exist
	if not os.path.isfile(fileName):
		return 0
	try:
		return os.stat(fileName).st_mtime_ns
	except Exception:
		__teePrintOrNot(f"Failed to get file update time for {fileName}",'error')
		return get_time_ns()

def _updateTimeNs(st):
	"""getFileUpdateTimeNs() of the file ``st`` is the stat of: 0 for one that is not a regular file (/dev/null)."""
	return st.st_mtime_ns if stat.S_ISREG(st.st_mode) else 0

def get_time_ns():
	try:
		return time.time_ns()
	except Exception:
		# try to get the time in nanoseconds
		return int(time.time()*1e9)

def scrubTSV(fileName,teeLogger = None,header = '',createIfNotExist = False, lastLineOnly = False,verifyHeader = True,verbose = False,taskDic = None,encoding = 'utf8',strict = False,delimiter = '\t',defaults = ...,errors = 'strict'):
	"""
	Compatibility method, calls scrubTabularFile.
	Scrub a Tabular (CSV / TSV / NSV) file by reading it and writing the contents back into the file.
	Return the data as a dictionary.

	Parameters:
	- fileName (str): The path to the Tabular file.
	- teeLogger (Logger, optional): The logger object to log messages. Defaults to None.
	- header (str or list, optional): The header of the Tabular file. If a string, it should be a tab-separated list of column names. If a list, it should contain the column names. Defaults to ''.
	- createIfNotExist (bool, optional): Whether to create the file if it doesn't exist. Defaults to False.
	- lastLineOnly (bool, optional): Whether to read only the last valid line of the file. Defaults to False.
	- verifyHeader (bool, optional): Whether to verify the header of the file. Defaults to True.
	- verbose (bool, optional): Whether to print verbose output. Defaults to False.
	- taskDic (OrderedDict, optional): The dictionary to store the data. Defaults to an empty OrderedDict.
	- encoding (str, optional): The encoding of the file. Defaults to 'utf8'.
	- strict (bool, optional): Whether to raise an exception if there is a data format error. Defaults to False.
	- delimiter (str, optional): The delimiter used in the Tabular file. Defaults to '\t' for TSV, ',' for CSV, '\0' for NSV.
	- defaults (list or str, optional): The default values to use for missing columns: a list, whose first cell stands in the key's column, or text, which TSVZed reads the same (split at the delimiter, '#_defaults_#' first or not). The caller's list is left as it is. Defaults to [].
	- errors (str, optional): What to do with text the encoding cannot hold, such as a lone surrogate from a non-UTF-8 file name (os.fsdecode). 'strict' (the default) raises a ValueError (TSVZEncodeError) naming it, before anything is written; 'replace' writes '?' instead.

	Returns:
	- OrderedDict: The dictionary containing the data from the Tabular file.

	Raises:
	- Exception: If the file is not found or there is a data format error.

	"""
	return scrubTabularFile(fileName,teeLogger = teeLogger,header = header,createIfNotExist = createIfNotExist,lastLineOnly = lastLineOnly,verifyHeader = verifyHeader,verbose = verbose,taskDic = taskDic,encoding = encoding,strict = strict,delimiter = delimiter,defaults=defaults,errors = errors)

def scrubTabularFile(fileName,teeLogger = None,header = '',createIfNotExist = False, lastLineOnly = False,verifyHeader = True,
					 verbose = False,taskDic = None,encoding = 'utf8',strict = False,delimiter = ...,defaults = ...,correctColumnNum = -1,errors = 'strict'):
	"""
	Scrub a Tabular (CSV / TSV / NSV) file by reading it and writing the contents back into the file.
	If using compressed files. This will recompress the file in whole and possibily increase the compression ratio reducing the file size.
	Return the data as a dictionary.

	For a .tsv-family file the scrub drops comments, empty lines, superseded lines and dead keys' lines, and keeps
	every other line as the file has it, in place (line 1 and every #_defaults_# line included), so every reader
	given the scrub's header, defaults and column count reads the file as before; a strict scrub writes what a loose
	one writes. It never refuses text it cannot encode, because it copies bytes. A header given with
	verifyHeader=False is written first when line 1 is not it.
	
	Parameters:
	- fileName (str): The path to the Tabular file.
	- teeLogger (Logger, optional): The logger object to log messages. Defaults to None.
	- header (str or list, optional): The header of the Tabular file. If a string, it should be a tab-separated list of column names. If a list, it should contain the column names. Defaults to ''.
	- createIfNotExist (bool, optional): Whether to create the file if it doesn't exist. Defaults to False.
	- lastLineOnly (bool, optional): Whether to read only the last valid line of the file. Defaults to False.
	- verifyHeader (bool, optional): Whether to verify the header of the file. Defaults to True.
	- verbose (bool, optional): Whether to print verbose output. Defaults to False.
	- taskDic (OrderedDict, optional): The dictionary to store the data. Defaults to an empty OrderedDict.
	- encoding (str, optional): The encoding of the file. Defaults to 'utf8'.
	- strict (bool, optional): Whether to raise an exception if there is a data format error. Defaults to False.
	- delimiter (str, optional): The delimiter used in the Tabular file. Defaults to '\t' for TSV, ',' for CSV, '\0' for NSV.
	- defaults (list or str, optional): The default values to use for missing columns: a list, whose first cell stands in the key's column, or text, which TSVZed reads the same (split at the delimiter, '#_defaults_#' first or not). The caller's list is left as it is. Defaults to [].
	- correctColumnNum (int, optional): The expected number of columns in the file. If -1, it will be determined from the first valid line. Defaults to -1.
	- errors (str, optional): What to do with text the encoding cannot hold, such as a lone surrogate from a non-UTF-8 file name (os.fsdecode). 'strict' (the default) raises a ValueError (TSVZEncodeError) naming it, before anything is written; 'replace' writes '?' instead.

	Returns:
	- OrderedDict: The dictionary containing the data from the Tabular file.

	Raises:
	- Exception: If the file is not found or there is a data format error.

	"""
	errors = _checkErrors(errors)
	if _isSpecPath(fileName):
		return _specScrubTabularFile(fileName, teeLogger=teeLogger, header=header, createIfNotExist=createIfNotExist,
									 lastLineOnly=lastLineOnly, verifyHeader=verifyHeader, verbose=verbose,
									 taskDic=taskDic, encoding=encoding, strict=strict, delimiter=delimiter,
									 defaults=defaults, correctColumnNum=correctColumnNum, errors=errors)
	if lastLineOnly:  # a read option, as for a .tsvz (a scrub of the last line alone would leave the file holding that)
		return readTabularFile(fileName,teeLogger = teeLogger,header = header,createIfNotExist = createIfNotExist,
							   lastLineOnly = True,verifyHeader = verifyHeader,verbose = verbose,taskDic = taskDic,
							   encoding = encoding,strict = strict,delimiter = delimiter,defaults=defaults,correctColumnNum = correctColumnNum,errors = errors)
	return _legacyScrub(fileName,teeLogger = teeLogger,header = header,createIfNotExist = createIfNotExist,verifyHeader = verifyHeader,
						verbose = verbose,taskDic = taskDic,encoding = encoding,strict = strict,delimiter = delimiter,
						defaults=defaults,correctColumnNum = correctColumnNum,errors = errors)[0]


def _legacyLineBody(text):
	"""``text`` (a legacy line, with or without its newline) as the reader sees it: _processLine strips NULs from the
	start of a line that ends in a newline, and from the end too only when it has none (a file's last line). The
	compaction and the drop work on lines without their newline, so one is put back for them (a .nsv line ends in
	cells that are empty, and the reader keeps those)."""
	return (text + '\n').strip('\x00').rstrip('\r\n')


def _legacyRawLine(line):
	"""A line of a legacy file as the compaction and the drop hold it: bytes without the newline. A file's last line
	may have none, and then the reader strips the NULs at its end (_legacyLineBody): they go here, as the line is
	written with a newline and would otherwise read with them."""
	return line[:-1] if line.endswith(b'\n') else line.rstrip(b'\x00')


def _legacyCells(line, delimiter):
	"""The cells of the legacy line body ``line`` (_legacyLineBody) as _processLine reads them (_unsanitize), quicker when
	it holds no escape: then each is the text between delimiters without the whitespace at its end."""
	cells = line.split(delimiter)
	return _unsanitize(cells, delimiter) if '<' in line else [cell.rstrip() for cell in cells]


def _legacyLineKind(text, width, strictActive, delimiter):
	"""What a read at column count ``width`` makes of the legacy line ``text``, as _processLine decides it, changing
	nothing: ``(kind, key, ncells, strictTakes)``. ``kind`` is 'skip' (comment, empty, no key), 'row', 'delete',
	'defaults' or 'reset' (a #_defaults_# line with no value); ``strictTakes`` whether a strict read applies it, with
	defaults that hold a value in effect where it stands (``strictActive``). A bare #_defaults_# line is a 'reset' at
	every width (at width 1 it is also the row '#_defaults_#' to the reader: the compaction keeps every such line)."""
	line = _legacyLineBody(text)
	if not line or (line.startswith('#') and not line.startswith(DEFAULTS_INDICATOR_KEY)):
		return 'skip', None, 0, False
	cells = _legacyCells(line, delimiter)
	if not cells or not cells[0]:
		return 'skip', None, 0, False
	if len(cells) == 1 or not any(cells[1:]):
		if cells[0] == DEFAULTS_INDICATOR_KEY:
			return 'reset', cells[0], len(cells), True
		return ('row' if width == 1 else 'delete'), cells[0], len(cells), True
	takes = len(cells) >= width or strictActive  # (strict drops only a narrower line)
	return ('defaults' if cells[0] == DEFAULTS_INDICATOR_KEY else 'row'), cells[0], len(cells), takes


def _legacyCompactPlan(raw, width, consumed, defaults, delimiter, encoding, matched, still=(), lives=False):
	"""What one reader's compaction of the legacy lines ``raw`` (bytes, without their newline) keeps:
	``(kept, content, moved, lineLife, lifeEnd, width, alone)``, ``width`` the column count it reads at, ``alone`` whether
	line 1 is a row or a deletion to it and no other keyed line has line 1's key. ``kept`` has a 1 for each line kept, each a line of ``raw`` byte for
	byte and in its place, but for those in ``content`` (index -> bytes): a key's last line moved up into its first line's
	place (``moved``: first -> last) and a dead key's protected line emptied (built from its own bytes). ``still``: the
	indexes where no line is moved in or emptied. With ``lives``, the key lives this reader reads: ``lineLife`` gives each
	row line its life's number (-1 for any other line), ``lifeEnd`` each life the deletion that ends it (-1: none).

	``width`` is the column count of the reader (-1: taken from the first keyed line), ``consumed`` whether it takes line
	1 for its header, ``matched`` whether line 1 holds that header, ``defaults`` the caller's.
	Kept: line 1; every #_defaults_# line; for each live key since its last deletion, its first line (its place) and
	its last (its value), and the first and last a strict reader takes, unless nothing between the first and the last
	changes how a row reads, when the last line takes the first one's place; the first keyed line after line 1 when a
	reader takes its column count from it (line 1 holds no key, or a header the reader does not take): a dead key's line
	there is kept emptied (a deletion as wide as it was), or with the deletion that ends it."""
	enc = encoding or 'utf8'
	strictActive = len(defaults) > 1 and any(defaults[1:])
	# 3.39's active defaults, cell by cell (5.11): it cuts or fills a #_defaults_# line to its count, fills the line's empty
	# cells from the defaults before it, and takes the result; a line it then holds with no value beyond the key makes it
	# strict again. Strict while they hold no value, so a line not as wide as its count is dropped.
	oldDefaults = list(defaults)
	strictActiveOld = any(oldDefaults[1:])

	def keyed(i):  # (a keyed line to any reader)
		return _legacyLineKind(raw[i].decode(enc, errors='replace'), 1, False, delimiter)[0] != 'skip'
	line0Keyed = bool(raw) and keyed(0)
	after0 = next((i for i in range(1, len(raw)) if keyed(i)), None)
	protectable = (0,) if after0 is None else (0, after0)
	key0 = _legacyLineKind(raw[0].decode(enc, errors='replace'), 1, False, delimiter)[1] if raw else None
	alone = not consumed  # (line 1 a row or a deletion to this reader, and no other keyed line of its key)
	# Each live key's lines since its last deletion, one slot per key in flat arrays (small per key: memory peaks here
	# during a TSVZed rewrite): its first and last line, the first and last a strict reader takes (-1: none), the cell
	# counts of the first and last, and which strict readers take them (a mask: 1, 5.5's, which takes a wider line; 2,
	# 3.39 to 5.4's, which takes only a line exactly as wide; each keeps its own first and last line taken).
	live = {}  # key -> slot
	first, last, firstTaken, lastTaken = array.array('l'), array.array('l'), array.array('l'), array.array('l')
	firstTakenOld, lastTakenOld = array.array('l'), array.array('l')
	firstN, lastN = array.array('l'), array.array('l')
	firstTakes, lastTakes = bytearray(), bytearray()
	lastFull = bytearray()  # whether the last line is exactly W wide with a value in every cell: no defaults reach it
	rowTaken = (None, bytearray(len(raw)), bytearray(len(raw)))  # (per strict reader, 1 and 2: a 1 for each row line it takes)
	lineLife = array.array('l', [-1]) * len(raw) if lives else None
	lifeEnd = array.array('l') if lives else None  # (a slot is a life: a key starts a new one after each deletion)
	pinfo = {}  # protectable index holding a row -> (key, ncells)
	killer = {}  # protectable index whose key died -> the deletion that ended it
	lastDelete = {}  # key -> its last deletion
	boundaries = []  # #_defaults_# lines, in order
	firstKeyed = None
	for i, line in enumerate(raw):
		if i == 0 and consumed:
			continue
		text = line.decode(enc, errors='replace')
		kind, key, n, takes = _legacyLineKind(text, width, strictActive, delimiter)
		if kind == 'skip':
			continue
		if width == -1:  # the first keyed line sets the column count, as _processLine does
			width = len(defaults) if len(defaults) > 1 else n
			kind, key, n, takes = _legacyLineKind(text, width, strictActive, delimiter)
		if firstKeyed is None:
			firstKeyed = i
		if alone and (key == key0 if i else kind not in ('row', 'delete')):
			alone = False
		takesOld = kind in ('delete', 'reset') or n == width or strictActiveOld
		if not takesOld and kind == 'row' and width == 1:  # (a line with no value is a row at width 1, whatever its cells)
			takesOld = not any(_legacyCells(_legacyLineBody(text), delimiter)[1:])
		mask = (1 if takes else 0) | (2 if takesOld else 0)
		if kind in ('defaults', 'reset'):
			if boundaries and raw[i] == raw[boundaries[-1]] and i not in protectable:
				continue  # (the same line again, nothing between them changing the defaults: it changes nothing)
			boundaries.append(i)
			strictActive = False if kind == 'reset' else (True if takes else strictActive)
			if kind == 'reset':
				if width != 1:  # (at one column a bare #_defaults_# is a row to 3.39, which keeps its defaults)
					oldDefaults = [DEFAULTS_INDICATOR_KEY]
			elif takesOld:
				cells = _legacyCells(_legacyLineBody(text), delimiter)[:width]
				cells += [''] * (width - len(cells))
				oldDefaults = [cell or (oldDefaults[j] if 0 < j < len(oldDefaults) else '') for j, cell in enumerate(cells)]
			strictActiveOld = any(oldDefaults[1:])
		elif kind == 'delete':
			slot = live.pop(key, None)
			if slot is not None:
				for j in protectable:
					if j in pinfo and pinfo[j][0] == key and j >= first[slot]:
						killer[j] = i
				if lives:
					lifeEnd[slot] = i
			lastDelete[key] = i
		else:
			if i in protectable:
				pinfo[i] = (key, n)
			rowTaken[1][i], rowTaken[2][i] = mask & 1, mask >> 1
			full = bool(boundaries) and n == width and all(  # (only a defaults line before it can matter)
				c.strip() for c in _legacyCells(_legacyLineBody(text), delimiter)[1:])
			slot = live.get(key)
			if slot is None:
				slot = live[key] = len(first)
				first.append(i)
				last.append(i)
				firstTaken.append(i if takes else -1)
				lastTaken.append(i if takes else -1)
				firstTakenOld.append(i if takesOld else -1)
				lastTakenOld.append(i if takesOld else -1)
				firstN.append(n)
				lastN.append(n)
				firstTakes.append(mask)
				lastTakes.append(mask)
				lastFull.append(full)
				if lives:
					lifeEnd.append(-1)
			else:
				last[slot], lastN[slot], lastTakes[slot], lastFull[slot] = i, n, mask, full
				if takes:
					if firstTaken[slot] < 0:
						firstTaken[slot] = i
					lastTaken[slot] = i
				if takesOld:
					if firstTakenOld[slot] < 0:
						firstTakenOld[slot] = i
					lastTakenOld[slot] = i
			if lives:
				lineLife[i] = slot
	kept = bytearray(len(raw))  # (one byte a line: whether it is kept)
	for i in boundaries:
		kept[i] = 1
	if raw:
		kept[0] = 1
	content = {}
	moved = {}

	ahead = {}  # (per strict reader: (from, the first row line it takes at or after from, -1 for none))

	def takesBefore(bit, start, end):
		# (whether that reader takes a row line in [start, end). The slots come in file order, so the next one asks from
		# further on and the answer is reused: each stretch is searched once, not once per key it lies before)
		got = ahead.get(bit)
		if got is None or got[0] > start or 0 <= got[1] < start:
			got = ahead[bit] = (start, rowTaken[bit].find(1, start))
		return 0 <= got[1] < end

	def inOrder(slot, f):
		# (5.11) Each strict reader that takes the key's first line takes its last, and one that does not, if it takes a line
		# of the key, takes the last and no row of another key before its first: the moved line then reads to it as the
		# last line did, and the key keeps its place among the keys it reads. (A reader that takes the first line and not
		# the last would lose the key; one that takes neither, but a line between, would lose its value.)
		for bit, rFirst in ((1, firstTaken[slot]), (2, firstTakenOld[slot])):
			if rFirst < 0:
				continue
			if not lastTakes[slot] & bit:
				return False
			if not firstTakes[slot] & bit and takesBefore(bit, f + 1, rFirst):
				return False
		return True
	for slot in live.values():
		f, l = first[slot], last[slot]
		kept[f] = 1
		if f == l:
			continue
		# The key becomes one line (its value at its place) when the same defaults reach both places (no defaults line
		# between them), or none can reach its last line (exactly W wide, a value in every cell, for every reader), and
		# both strict readers take both lines, or (5.10) the two lines are as wide as each other and each strict reader
		# takes both or none of the key's: a reader at any count, given any defaults, then takes both or neither, and
		# reads the moved line as it read the last one, or not at all.
		# 5.11: or, the two lines as wide as each other, each strict reader takes the moved line where it took the key
		# before, as it read the last one, and finds no other key's row between (inOrder): a set and a delete of a key in a
		# one-column file under wider defaults (c\t1, c\t) are one line again, where 3.39's strict reader took only the last.
		if ((lastFull[slot] or bisect.bisect_right(boundaries, f) == bisect.bisect_left(boundaries, l))
				and f not in still
				and ((firstTakes[slot] == lastTakes[slot]
					and (firstTakes[slot] == 3 or (firstN[slot] == lastN[slot] and (firstTakes[slot] & 1 or firstTaken[slot] < 0)
						and (firstTakes[slot] & 2 or firstTakenOld[slot] < 0))))
					or (firstN[slot] == lastN[slot] and inOrder(slot, f)))
				and f != 0 and (f not in (firstKeyed, after0) or firstN[slot] == lastN[slot])):
			content[f] = raw[l]
			moved[f] = l
			continue
		kept[l] = 1
		if firstTaken[slot] >= 0:
			kept[firstTaken[slot]] = 1  # (a strict reader's place)
		if lastTaken[slot] >= 0:
			kept[lastTaken[slot]] = 1  # (and its value)
		if firstTakenOld[slot] >= 0:
			kept[firstTakenOld[slot]] = 1  # (the same for an older strict reader, which takes no wider line)
		if lastTakenOld[slot] >= 0:
			kept[lastTakenOld[slot]] = 1
	protected = [0] if raw else []
	if after0 is not None and (not line0Keyed or (consumed and not matched)):
		protected.append(after0)
	for j in protected:
		kept[j] = 1
		if j in killer:
			if j != 0 and pinfo[j][1] > 1 and delimiter != '\x00' and j not in still:
				# Emptied: a deletion as wide as the line was, so a reader takes the same column count from it and
				# the key stays dead; the deletion that ended it is then not needed. (Not in .nsv: its readers strip
				# trailing NULs, so an emptied line would read one cell wide.)
				d = delimiter.encode(enc)
				content[j] = raw[j].strip(b'\x00').split(d, 1)[0] + d * (pinfo[j][1] - 1)
			else:
				kept[killer[j]] = 1
	if consumed and line0Keyed and not matched:
		# line 1 is a row to a reader given no header: when its key ends deleted, that deletion stays for it
		key0 = _legacyLineKind(raw[0].decode(enc, errors='replace'), width, False, delimiter)[1]
		if key0 is not None and key0 not in live and key0 in lastDelete:
			kept[lastDelete[key0]] = 1
	return kept, content, moved, lineLife, lifeEnd, width, alone


def _legacyCompactReaders(raw, width, consumed, defaults, delimiter, encoding, matched, counts=()):
	"""The readers a compaction of the legacy lines ``raw`` keeps what they take for, as ``(width, consumed, matched,
	defaults)`` (_legacyCompactPlan): the caller's, and, when it takes line 1 for a header line 1 does not hold, the readers that
	count from line 1 as well: given no header (line 1 a row to them, at its count, or the defaults' as the caller's
	reader takes them; a line 1 of one cell at both, 5.12: at one, the one-column rule, so a reader at one column keeps its
	lone keys, and at the defaults', which a reader given them reads at, where it can read otherwise than the caller's) and
	given line 1 for their header (a store that follows a widening of its header among them).
	The caller's reader then counts from the first keyed line after line 1 (or a program that does not follow a
	widening, given the header line 1 widens), and its count is the one ``width`` gives. Each of these readers is given the
	caller's defaults. A reader given none is another reader: it reads at the file's own count, not the defaults', and a
	strict one takes only the lines as wide as its count (3.39's) or at least as wide (5.5's), where defaults with a value
	take every line. So each is listed again, given none and at the count it then reads at, where that count or the
	strictness differs. ``counts``: column counts readers are given explicitly (a scrub's ``correctColumnNum``), each
	another reader given the caller's header and defaults where it differs from ``width`` (5.11), and, given none, at that
	count whatever ``width`` is: a reader given none and a count reads at the count, not at the file's."""
	given = sorted({count for count in counts if count > 0})
	readers = [(width, consumed, matched, defaults)]
	# (the count each reader is given no defaults reads at: the file's. A header the file holds gives it, else the first
	# keyed line's, line 1 left out for a reader that takes it for its header)
	own = [(width if matched else _legacyFileWidth(itertools.islice(raw, 1, None) if consumed else raw, (), False, encoding, delimiter),
			consumed, matched)]
	lone = None  # (line 1 of one cell: its kind, 'row' for a lone key, 'reset' for a bare #_defaults_#)
	if consumed and not matched and raw:
		kind, _, cells, _ = _legacyLineKind(raw[0].decode(encoding or 'utf8', errors='replace'), 1, False, delimiter)
		if kind != 'skip':
			readers.append((cells if cells == 1 or len(defaults) < 2 else len(defaults), False, False, defaults))
			readers.append((cells, True, True, defaults))
			own += [(cells, False, False), (cells, True, True)]
			if cells == 1:
				lone = kind
	if width == 1 and len(defaults) > 1 and not matched:
		# A file the plan reads as one column (the one-column rule) is read at the defaults' count by a reader given them
		# (5.6 to 5.10 kept it for the one-column readers only, and a row such a reader takes was dropped).
		readers.append((len(defaults), consumed, matched, defaults))
	valued = len(defaults) > 1 and any(defaults[1:])
	if valued or any(o[0] != r[0] for o, r in zip(own, readers)):
		counts = {}  # line index (0, or 1 for every later line) -> the cell counts of its lines with a value
		for i, line in enumerate(raw):
			kind, _, cells, _ = _legacyLineKind(line.decode(encoding or 'utf8', errors='replace'), 2, False, delimiter)
			if kind in ('row', 'defaults'):
				counts.setdefault(i > 0, set()).add(cells)
		# (given no defaults the two differ only on a line with a value that is not exactly w cells wide: a file with none
		# needs no twin where the counts agree; where they differ the twin is a reader of its own)
		readers += [(w, c, m, ()) for (w, c, m), r in zip(own, readers)
					if (valued or w != r[0])
					and not (valued and w > 0 and counts.get(True, set()) <= {w} and (c or counts.get(False, set()) <= {w}))]
	if lone and len(defaults) > 1 and (width != len(defaults) or (
			lone == 'reset' and valued and not counts.get(True, set()) <= {len(defaults)})):
		# A reader given no header and the defaults reads a line 1 of one cell at the defaults' count, not at line 1's (5.12;
		# 5.11 listed it there only at a caller's count of one, and a line it takes could go). Not where it reads every line
		# as the caller's reader: at the caller's count, after a lone key (the deletion of a key not yet read), or after a
		# bare #_defaults_#, which leaves it as one given none, where the defaults hold no value or no line with a value
		# differs from that count (a strict and a lenient reader then take the same lines). A plan of its own there kept
		# lines no reader needs. (A line 1 of more cells: line 1's reader is listed at their count already.)
		readers.append((len(defaults), False, False, defaults))
	for count in given:
		if count != width:
			readers.append((count, consumed, matched, defaults))
		if (valued and (count, consumed, matched, ()) not in readers
				and not (counts.get(True, set()) <= {count} and (consumed or counts.get(False, set()) <= {count}))):
			# (a strict one given none: the count given, its own strictness; as above, not where no line with a value differs
			# from the count: it then takes the lines the reader given the defaults takes)
			readers.append((count, consumed, matched, ()))
	return readers


def _legacyCompactOnce(raw, width, consumed, defaults, delimiter, encoding, matched, counts=()):
	"""One pass of _legacyCompact over the legacy lines ``raw`` (bytes, without their newline): ``(lines, emptied)``, the
	lines kept, each a line of ``raw`` byte for byte and in its place, but for a key's last line moved up into its first
	line's place and a dead key's protected line emptied (built from its own bytes), for every reader of
	_legacyCompactReaders; ``emptied`` whether a line was emptied (a pass that empties none leaves nothing new droppable).

	With one reader, what its plan keeps (_legacyCompactPlan). With more, every line any of their plans keeps, and a
	line moved or emptied only where every plan keeps that line with the same bytes, or (an emptied line, which is a
	deletion as wide) a reader at two columns or more keeps no line of that key's life there; else no plan moves or
	empties it, and each keeps those lines as they are. A line one plan keeps is then another's to read as well: where
	it is a row of a key that reader reads as deleted after it, the deletion stays. (No line stays for a reader that moved
	a key's last line into its first line's place: a move survives only when every plan moves the same bytes there, and
	a plan that collapses a life keeps no other line of it.) Every reader then reads its own plan's rows, in its order:
	a line kept for another reader falls inside a key's life whose first and last line the reader takes are kept, or in a
	life whose end it reads."""
	readers = _legacyCompactReaders(raw, width, consumed, defaults, delimiter, encoding, matched, counts)
	if len(readers) == 1:
		kept, content, moved = _legacyCompactPlan(raw, width, consumed, defaults, delimiter, encoding, matched)[:3]
		return [content.get(i, raw[i]) for i in range(len(raw)) if kept[i]], any(i not in moved for i in content)

	def plans(still=()):
		out = []
		prev = None
		for n, (w, c, m, d) in enumerate(readers):
			if c and m and n and prev is not None and prev[0] == (w, False, False, d) and prev[1][6]:
				continue  # (the readers given line 1 for a header read as those given none but for line 1's key: it has no other line)
			plan = _legacyCompactPlan(raw, w, c, d, delimiter, encoding, m, still, True)
			prev = ((w, c, m, d), plan)
			out.append(plan)
		return out
	every = plans()

	def agrees(other, i, line, source):
		# (the same bytes there; or an emptied line (``source`` None), which reads as a deletion as wide, where the other
		# reader keeps no line of that key's life: it reads the key deleted after it all the same; or (5.11) a line moved
		# from ``source``, the deletion that ends that life to the other reader: it reads the key deleted there instead,
		# and a line of the life kept for another reader gets that deletion back after it)
		if other[0][i]:
			return other[1].get(i) == line
		life = other[3][i]
		return (life >= 0 and other[4][life] >= 0
				and (other[4][life] == source if source is not None else other[5] > 1))
	still = {i for plan in every for i, line in plan[1].items()
			 if not all(agrees(other, i, line, plan[2].get(i)) for other in every)}
	if still:
		every = plans(still)
	union = 0
	for plan in every:
		union |= int.from_bytes(plan[0], 'little')
	kept = bytearray(union.to_bytes(len(raw), 'little'))
	content = {}
	movedFrom = {}
	for plan in every:
		content.update(plan[1])  # (where two plans have a line, the same bytes now)
		movedFrom.update(plan[2])
	changed = True
	while changed:
		changed = False
		for own, _, _, lineLife, lifeEnd, _, _ in every:
			# (the lines kept for the other readers alone: a byte 1 for each)
			extra = (int.from_bytes(kept, 'little') & ~int.from_bytes(own, 'little')).to_bytes(len(raw), 'little')
			i = extra.find(1)
			while i >= 0:
				life = lineLife[i]
				if life >= 0:
					end = lifeEnd[life]
					if end >= 0 and not kept[end] and movedFrom.get(i) != end:
						kept[end] = 1  # (a row kept for another reader: this one reads the key deleted after it; not when the
						# line there is that deletion, moved up)
						changed = True
				i = extra.find(1, i + 1)
	return [content.get(i, raw[i]) for i in range(len(raw)) if kept[i]], any(i not in plan[2] for plan in every for i in plan[1])


def _legacyCompact(raw, width, consumed, defaults, delimiter, encoding, matched, counts=()):
	"""The legacy file whose lines are ``raw`` (bytes, without their newline) compacted by dropping lines (comments,
	empty lines, superseded lines, dead keys' lines): what every reader given the caller's arguments takes from it,
	of any version and either strictness, stays, and under a header line 1 does not hold what every reader that counts
	from line 1 takes too, and every reader given a count of ``counts`` (_legacyCompactReaders), because every line kept
	is kept as it is, in its place
	(_legacyCompactOnce). Repeats while a pass empties a line (that can make another one droppable; a pass that empties
	none left nothing new droppable in 64,000 compactions tried), at most 4 passes: stopping early is less compact, never
	wrong."""
	defaults = list(defaults) if defaults and defaults is not ... else []
	for _ in range(4):
		out, emptied = _legacyCompactOnce(raw, width, consumed, defaults, delimiter, encoding, matched, counts)
		changed = out != raw
		raw = out
		if not (changed and emptied):
			break
	return raw


def _legacyHoldsHeader(line0, header, consumed, encoding, delimiter):
	"""Whether line 1 (bytes, or None) holds ``header`` to a reader that takes line 1 for it (``consumed``)."""
	return bool(consumed and line0 is not None
				and _headerMatches(header, line0.decode(encoding or 'utf8', errors='replace').rstrip('\r\n'), delimiter))


def _legacyDropLines(raw, dropAt, skipFirst, widthFromLines, delimiter, encoding, both=False):
	"""The legacy lines ``raw`` (bytes, without their newline) without the lines of each key of ``dropAt`` before the
	index it gives (a row queued after a delete stays); line 1 is left alone when a reader takes it for its header
	(``skipFirst``). No line moves and no cell changes.

	A reader takes its column count from its first keyed line: the file's first (when the count comes from the lines,
	``widthFromLines``), and with ``both`` also the first after line 1, which a reader that takes line 1 for a header the
	caller holds for a row counts from (--x-header H, line 1 not H). When such a line goes and the line that would come
	in its place has another cell count, a stand-in takes its place: the key of a later row that stays, and as many
	empty cells as the line had, so no reader's count changes. With the same count nothing is needed, but on line 1 with
	``both`` a bare ``#`` keeps the place a header reader skips.

	The key is line 1's own, when the first keyed line after the place is a row of it as wide as line 1 (5.10): every
	reader that read line 1's row reads the key deleted at the stand-in and takes that line back before any other key, a
	reader that counts from the place reads the key first there, as it read that line before, and a compaction keeps that
	line for line 1's readers (_legacyCompact). 5.12 tries it first: any other key comes first to a reader that counts from
	the place, and can so take another key's place in its order (5.10 and 5.11 tried it last). Else one whose later line
	holds no value: every reader applies that line (a row at one column, a deletion at more), so the key reads after it as
	it did. Else, unless a line 1 of one cell that the caller reads as a row comes before the place (``both``: its readers
	would read the stand-in as a row, and 3.39's strict read, which drops a wider line at one column, would gain it; Q3),
	a key that no row with a value before the place names (5.11: in a store's own delete under a header line 1 is not, the
	store counts from the place, and a stand-in keeps its cells as they read; 3.39's strict reader given no header gains
	the key, Q4's kind): at two columns or more the stand-in deletes a key not yet read, at one it is a row the key's
	later line replaces. No such row: the line just goes."""
	enc = encoding or 'utf8'
	d = delimiter.encode(enc)
	texts = [line.decode(enc, errors='replace') for line in raw]
	kinds = [_legacyLineKind(text, 1, False, delimiter) for text in texts]

	def goes(i):
		kind, key = kinds[i][:2]
		return not (i == 0 and skipFirst) and kind == 'row' and i < dropAt.get(key.rstrip(), 0)

	def valued(i):  # (a row that holds a value: a row, not a deletion, at two columns)
		return _legacyLineKind(texts[i], 2, False, delimiter)[0] == 'row'

	def keyed(start):
		return next((i for i in range(start, len(raw)) if kinds[i][0] != 'skip'), None)
	places = [keyed(1 if skipFirst else 0)] if widthFromLines else []
	if both and places == [0]:
		places.append(keyed(1))
	stand = {}
	for i in sorted((i for i in places if i is not None and goes(i)), reverse=True):  # (a later stand-in can come in an earlier's place)
		nxt = next((j for j in range(i + 1, len(raw)) if j in stand or (kinds[j][0] != 'skip' and not goes(j))), None)
		same = nxt is not None and (stand[nxt].count(d) + 1 if nxt in stand else kinds[nxt][2]) == kinds[i][2]
		if same and both and i == 0:
			stand[i] = b'#'  # (the count stays: only line 1's place, which a reader given the header skips, is kept)
			continue
		if nxt is None or same:
			continue
		later = [j for j in range(i + 1, len(raw)) if kinds[j][0] == 'row' and not goes(j)]
		# (named: the keys a row with a value before the place names, which is line 1's key alone, and none at line 1's place)
		named = {kinds[j][1].rstrip() for j in range(i) if valued(j)}
		if kinds[nxt][1].rstrip() in named and valued(nxt) and kinds[nxt][2] == kinds[0][2]:
			pick = [nxt]  # (line 1's key: its next line comes first after the place, as wide as line 1; 5.12: tried first)
		else:
			pick = [j for j in later if not valued(j)]
			if not pick and not (both and i > 0 and kinds[0][0] != 'skip' and kinds[0][2] == 1):
				pick = [j for j in later if kinds[j][1].rstrip() not in named]
		if pick:
			stand[i] = raw[pick[0]].strip(b'\x00').rstrip(b'\r').split(d)[0] + d * (kinds[i][2] - 1)
	return [stand.get(i, line) for i, line in enumerate(raw) if i in stand or not goes(i)]


def _legacyFileWidth(lines, header, consumed, encoding, delimiter):
	"""The column count the legacy lines ``lines`` (bytes, without their newline) say themselves, reading no further than
	it must. Every writer asks this before it treats a line with no value as a row (at 1) or a deletion (at any other): a
	rewrite that widens or compacts, a scrub, a delete. Decided here once, and no reader's defaults count: they say how that
	reader reads, not what the file holds.

	The header's when line 1 holds it (``consumed``: a reader takes line 1 for its header). Else 1 when line 1 is one cell:
	its lone keys are rows to a reader given no header, or given that cell for one. Else the first keyed line's, line 1 left
	out when ``consumed``. -1 for no keyed line."""
	lines = iter(lines)
	line0 = next(lines, None)
	if line0 is None:
		return -1
	if _legacyHoldsHeader(line0, header, consumed, encoding, delimiter):
		return len(header)

	def cellsOf(line):
		kind, _, cells, _ = _legacyLineKind(line.decode(encoding or 'utf8', errors='replace'), 1, False, delimiter)
		return -1 if kind == 'skip' else cells
	opening = cellsOf(line0)
	if opening == 1 or (opening != -1 and not consumed):
		return opening
	return next((cells for cells in map(cellsOf, lines) if cells != -1), -1)


def _legacyReaderWidth(raw, header, consumed, matched, encoding, delimiter):
	"""The column count the caller's reader takes from the legacy lines ``raw``, which _legacyCompact keeps what it reads
	at: the file's (_legacyFileWidth), but under a header line 1 does not hold (``matched``) the first keyed line after
	line 1's, which that reader counts from; the readers that count from line 1 are _legacyCompactReaders' others."""
	if consumed and not matched:
		return _legacyFileWidth(itertools.islice(raw, 1, None), (), False, encoding, delimiter)
	return _legacyFileWidth(raw, header, consumed, encoding, delimiter)


@contextlib.contextmanager
def _legacyLinesOf(f, fileName):
	"""The lines of the open legacy file ``f`` (named ``fileName``) as an iterator, bytes without their newline, read
	through that descriptor, which the caller holds locked (never by name: closing another descriptor would drop a lockf
	lock); it yields nothing for a file that holds nothing, and raises when the read meets damage. Read as far as the caller
	takes it. ``f`` is left at offset 0."""
	codec = _fileCodec(fileName)
	stream = None
	try:
		f.flush()
		f.seek(0)
		if _holdsNothing(f, codec, os.fstat(f.fileno()).st_size):
			yield iter(())
			return
		f.seek(0)
		stream = _openStreams(codec, f) if codec else f
		yield map(_legacyRawLine, stream)
	finally:
		if stream is not None and codec:
			stream.close()
		f.seek(0)


def _legacyLinesIn(f, fileName):
	"""All the lines of the open legacy file ``f`` (_legacyLinesOf), as a list; [] when it holds nothing."""
	with _legacyLinesOf(f, fileName) as lines:
		return list(lines)


def _legacyWidthNow(fileName, header, verifyHeader, encoding, delimiter):
	"""_legacyFileWidth of the legacy file as it is now, under the READ lock; None when it could not be read that far
	(missing, not a file, damage)."""
	try:
		if _fileHoldsNothing(fileName):
			return -1
		with _readLock(fileName), openFileAsCompressed(fileName, mode='rb', encoding=encoding) as f:
			return _legacyFileWidth(map(_legacyRawLine, f), header, bool(any(header) and verifyHeader), encoding, delimiter)
	except Exception:
		return None


def _legacyDropKeys(fileName, keys, header, verifyHeader, encoding, delimiter, xHeader=False):
	"""Delete ``keys`` from the legacy file where no line can: a file of one column, where a lone key is a row (and of
	no column count yet, where it would make one). True when that is done: their lines are out of the file
	(_legacyDropLines, which keeps the one column), or there were none. False when the file is of two columns or more
	(_legacyFileWidth), or could not be read (a deletion line says it there; damage is reported): nothing is written.
	``header`` is formatted and ``delimiter`` resolved. Only those lines go: this compacts nothing, and copies the others
	as bytes.

	``xHeader``: the caller is a --x-header one: line 1, when it is not ``header`` (_isHeaderLine), is a row to it, and a
	reader given ``header`` skips it all the same and counts from the first keyed line after it. Both readers' count is
	kept (_legacyDropLines, ``both``). Decided here, on the line 1 read under the rewrite lock the write is made under,
	not on one read before it.

	Read through the file's REWRITE lock and written under it, as ``TSVZed`` writes, so another process's write
	(an append beside it) cannot fall between the read and the write: no retry, and none is lost. OSError when the
	write fails."""
	consumed = bool(any(header) and verifyHeader)
	done = []

	def build(f):
		try:
			with _legacyLinesOf(f, fileName) as lines:
				# (the first lines say the width, and at two columns or more the delete is a line: the rest is not read)
				seen = []

				def tap():
					for line in lines:
						seen.append(line)
						yield line
				width = _legacyFileWidth(tap(), header, consumed, encoding, delimiter)
				lineOneIsRow = bool(xHeader and any(header) and seen
									and not _isHeaderLine(header, seen[0].decode(encoding or 'utf8', errors='replace'), delimiter))
				raw = seen + list(lines) if width == 1 or lineOneIsRow and width == -1 else None
		except Exception as e:
			_warn('TSVZ warning: {}: the file cannot be read ({}): the delete is written as a deletion line, which deletes '
				  'nothing if the file is of one column'.format(fileName, e))
			done.append(False)
			return None
		done.append(width <= 1)
		if not raw:
			return None
		fromLines = lineOneIsRow or not _legacyHoldsHeader(raw[0], header, consumed, encoding, delimiter)
		kept = _legacyDropLines(raw, dict.fromkeys((key.rstrip() for key in keys), len(raw)), consumed and not lineOneIsRow,
								fromLines, delimiter, encoding, lineOneIsRow)
		return None if kept == raw else b''.join(line + b'\n' for line in kept)
	if not os.path.isfile(fileName):
		return False
	_specRewriteInPlace(fileName, None, build=build)
	return done[0]


def _legacyWidened(raw, header, encoding, delimiter, errors):
	"""The legacy lines ``raw`` (bytes, without their newline) with line 1 widened to ``header``, when line 1 is that
	header's beginning: the same cells, each without the whitespace around it, and fewer of them (empty cells at its end
	not counted). This exact rule, not the containment of _lineContainHeader: a line 1 that only resembles the header may
	be a row, and is left alone. None when it does not apply; False when it would lose rows: in a file of one column
	(_legacyFileWidth) a key whose last line holds no value is a row with no value, that line reads as a deletion at the
	header's width, and no line can hold it there. An earlier line of a key that a later one gives a value loses nothing,
	unless that later line is wider than the header (5.11): 3.39's strict reader, which took the line with no value as a
	row at one column, drops a wider line at the header's count, and would lose the key (Q-C: the widening is refused).

	The lines a reader takes that are narrower than ``header`` are padded with it, each as a loose read already pads it: a
	strict reader, which drops a narrower line, then takes them all, those it dropped at the old column count too. Nothing
	else changes."""
	enc = encoding or 'utf8'
	cells = _lineOneCells(raw[0].decode(enc, errors='replace'), delimiter) if raw else []
	if not cells or len(cells) >= len(header) or not _sameCells(cells, header[:len(cells)]):
		return None
	pad = delimiter.encode(enc)
	out = [_encodeRow(_sanitize(list(header),delimiter=delimiter),delimiter,encoding,errors,what='header')]
	valueless = set()  # (the keys whose last line so far holds no value)
	for line in raw[1:]:
		kind, key, count, _ = _legacyLineKind(line.decode(enc, errors='replace'), len(header), False, delimiter)
		if kind == 'delete':
			valueless.add(key.rstrip())
		elif kind == 'row' and count <= len(header):
			valueless.discard(key.rstrip())  # (a wider line clears nothing: 3.39's strict reader drops it at the header's count)
		out.append(line.rstrip(b'\r') + pad * (len(header) - count) if kind in ('row', 'defaults') and count < len(header) else line)
	return False if valueless and _legacyFileWidth(raw, header, True, encoding, delimiter) == 1 else out


class _RawTap(object):
	"""The open binary legacy file ``f`` as a read takes it (readline, iteration, tell), keeping each line it hands
	over in ``raw`` (bytes, the newline taken off): what a scrub compacts is what its read took, from the same bytes."""

	def __init__(self, f):
		self.f = f
		self.raw = []
		self._lines = None

	def _keep(self, line):
		if line:
			self.raw.append(_legacyRawLine(line))
		return line

	def readline(self, *args):
		return self._keep(self.f.readline(*args))

	def __iter__(self):
		return self

	def __next__(self):
		if self._lines is None:
			self._lines = iter(self.f)
		return self._keep(next(self._lines))

	def tell(self):
		return self.f.tell()


def _legacyScrubRead(fileName, teeLogger, header, verifyHeader, verbose, taskDic, encoding, strict, delimiter, defaults,
					 correctColumnNum):
	"""The one read of a scrub attempt: the legacy file read into ``taskDic`` exactly as readTabularFile reads it (the
	same warnings, damage notes and strict drops), through one open under the READ lock; returns ``(raw, W)``, every
	line that read took (bytes, without the newline; nothing past damage it stopped at) and the column count it ended
	with. ``header`` is formatted and ``delimiter`` resolved."""
	reporter = _Reporter(fileName, teeLogger)
	tap = None
	width = correctColumnNum
	try:
		with _readLock(fileName), openFileAsCompressed(fileName, mode='rb', encoding=encoding, teeLogger=teeLogger) as file:
			tap = _RawTap(file)
			start = _legacyReadHeader(tap, fileName, header, verifyHeader, verbose, teeLogger, strict, delimiter,
									  correctColumnNum, encoding, reporter)
			if start is not None:
				width = _legacyReadLoop(tap, fileName, taskDic, start[0], start[1], strict, delimiter, defaults, False,
										encoding, reporter)[0]
	finally:
		reporter.flush()
	return (tap.raw if tap is not None else []), width


def _legacyScrub(fileName,teeLogger = None,header = '',createIfNotExist = False,verifyHeader = True,verbose = False,
				 taskDic = None,encoding = 'utf8',strict = False,delimiter = ...,defaults = ...,correctColumnNum = -1,errors = 'strict'):
	"""Compact a legacy file in place; return ``(taskDic, done)``: the rows read, and whether the file was written (or
	had nothing to write). ``done`` is False when nothing was written because the file kept changing (the shape of
	``_specScrub``): it is rewritten only if it is what the read saw (spec §19.8), so a row another process appends
	after the read is not scrubbed away; the read is made again, up to ``_SPEC_REWRITE_ATTEMPTS`` times.

	One read per attempt (_legacyScrubRead) gives the rows and the lines; the lines are compacted by dropping lines
	(_legacyCompact): comments, empty lines, superseded lines and dead keys' lines go, every other line stays as the file
	has it, in its place, so every reader given this scrub's header, defaults and column count, of any version and either
	strictness, reads the file as before. Strict and loose scrubs write the same. Lines are copied as bytes: text the
	encoding cannot hold never refuses a scrub. The one line a scrub may add: a header passed with
	``verifyHeader=False`` goes first when line 1 is not it. A file that holds nothing (no byte, or only empty streams)
	is left as it is; a compressed file is written again whole (recompressed) even when nothing is dropped."""
	given = list(taskDic.items()) if taskDic else []
	if taskDic is None:
		taskDic = {}
	dlm = get_delimiter(delimiter,file_name=fileName)
	givenDefaults = _readerDefaults(defaults, dlm)
	formatted = _formatHeader(header,delimiter=dlm)
	consumed = bool(any(formatted) and verifyHeader)
	enc = encoding or 'utf8'
	for tries in range(_SPEC_REWRITE_ATTEMPTS):
		if tries:
			# Read again as the caller passed it.
			taskDic.clear()
			taskDic.update(given)
		if not _verifyFileExistence(fileName,createIfNotExist = createIfNotExist,teeLogger = teeLogger,header = formatted,encoding = encoding,strict = strict,delimiter=dlm,errors = errors):
			return taskDic, True
		if _fileHoldsNothing(fileName):
			return taskDic, True  # no byte (or only empty streams) to compact: as it is
		# The stamp is taken before the read, and compared under the file lock.
		stamp = _specFileStamp(fileName)
		raw, width = _legacyScrubRead(fileName, teeLogger, formatted, verifyHeader, verbose, taskDic, encoding, strict,
									  dlm, list(givenDefaults), correctColumnNum)
		line0 = raw[0].decode(enc, errors='replace').rstrip('\r\n') if raw else None
		matched = consumed and line0 is not None and _headerMatches(formatted, line0, dlm)
		if consumed and line0 is not None and not matched and not tries:
			__teePrintOrNot(f'Warning: Header mismatch in {fileName}. Keeping original header in file...','warning',teeLogger)
		if _legacyReaderWidth(raw, formatted, consumed, matched, encoding, dlm) == 1:
			width = 1  # (a file of one column is compacted as one, as a TSVZed rewrite does: its lone keys are rows)
		# (a correctColumnNum given is planned too where the header's width replaced it: readers given it read at it)
		lines = _legacyCompact(raw, width, consumed, givenDefaults, dlm, encoding, matched, (correctColumnNum,))
		del raw
		if any(formatted) and (not verifyHeader or not lines) and (line0 is None or not _isHeaderLine(formatted, line0, dlm)):
			# A header given and not checked goes first when line 1 is not it (line 1 stays, as line 2); a read that met
			# damage before line 1 took nothing, and the file is made again with the header given.
			lines.insert(0, _encodeRow(_sanitize(formatted,delimiter=dlm),dlm,encoding,errors,what='header'))
		payload = b''.join(line + b'\n' for line in lines)
		if not _verifyFileExistence(fileName,createIfNotExist = True,teeLogger = teeLogger,header = formatted,encoding = encoding,strict = False,delimiter=dlm,errors = errors):
			raise FileNotFoundError("Something catastrophic happened! File still not found after creation")  # (removed since the read: made again)
		if _legacyReplace(fileName, [], False, encoding, dlm, errors, payload, expect=stamp) is None:
			continue  # another writer changed the file after the read
		if verbose:
			__teePrintOrNot(f"Cleared {fileName}",teeLogger=teeLogger)
			__teePrintOrNot(f"Appended {len(lines)} lines to {fileName}" if lines else f"No lines to append to {fileName}",teeLogger=teeLogger)
		return taskDic, True
	__teePrintOrNot(f'Warning: scrub of {fileName} skipped: the file kept changing while it was being scrubbed; nothing was written','warning',teeLogger)
	return taskDic, False

# ===========================================================================
# TSVZ spec v1 dialect (.tsvz / .csvz / .nsvz / .psvz) -- see tsvz-spec-v1.md
# ===========================================================================
MAX_SPEC_VERSION = 1
_MARKER_RE = re.compile(r'^#_[A-Za-z0-9_-]+_#$')
#: What a lookup gets for a key that is not live (spec §20.2.7: then the stripped key is tried).
_NO_ROW = object()
_CHECKSUM_RE = re.compile(r'^#_checksum_([A-Za-z0-9_-]+)_#$', re.IGNORECASE)
_ASCII_DIGITS_RE = re.compile(r'^[0-9]+\Z')  # \Z: '$' would also match before a final newline
_SPEC_DECODE_RE = re.compile(r'<(?:sep|LF|CR|NUL|lt|#)>')
_SPEC_TOKENS = {'<LF>': '\n', '<CR>': '\r', '<NUL>': '\0', '<lt>': '<', '<#>': '#'}
_RowState = namedtuple('_RowState', 'defaults strip fillEmpty')
_BOOL_VALUES = {'true': True, 'yes': True, 'on': True, '1': True,
				'false': False, 'no': False, 'off': False, '0': False}
#: Stateful boolean markers: key -> (state attribute, built-in default).
_BOOL_MARKERS = {'#_strip_trailing_whites_#': ('strip', True),
				 '#_fill_empty_with_default_#': ('fillEmpty', False),
				 '#_return_defaults_when_missing_#': ('returnDefaults', True)}
#: Enumerated markers: key -> (state attribute, choices; first is the default).
_ENUM_MARKERS = {'#_rotate_#': ('rotate', ('keep', 'rename', 'delete')),
				 '#_write_ack_#': ('writeAck', ('memory', 'disk')),
				 '#_on_corruption_#': ('onCorruption', ('recover', 'skip', 'stop'))}
#: Advisory marker: the most uncompressed data a writer puts in one compressed member (spec §16.7).
MEMBER_SIZE_KEY = '#_member_size_#'
_DEFAULT_MEMBER_SIZE = 1 << 20
_SPEC_MARKER_KEYS = (frozenset(['#_version_#', DEFAULTS_INDICATOR_KEY, MEMBER_SIZE_KEY]) | frozenset(_BOOL_MARKERS)
					 | frozenset(_ENUM_MARKERS))


def _specDecodeField(field, delimiter):
	"""Decode spec §13 tokens; any other ``<...>`` passes through literally."""
	if '<' not in field:
		return field
	return _SPEC_DECODE_RE.sub(lambda m: delimiter if m.group(0) == '<sep>' else _SPEC_TOKENS[m.group(0)], field)


def _specEncodeField(value, delimiter, isKey=False):
	"""Encode one field per spec §13.3 (``isKey`` also escapes a leading '#')."""
	if '<' in value:
		value = value.replace('<', '<lt>')
	if delimiter in value:
		value = value.replace(delimiter, '<sep>')
	if '\n' in value:
		value = value.replace('\n', '<LF>')
	if '\r' in value:
		value = value.replace('\r', '<CR>')
	if '\0' in value:  # (in .nsvz every zero byte is the delimiter, already <sep>)
		value = value.replace('\0', '<NUL>')
	if isKey and value.startswith('#'):
		value = '<#>' + value[1:]
	return value


def _specFormatRecord(cells, delimiter, marker=False):
	"""Format one record without its '\\n'. ``marker`` writes ``cells[0]`` raw as a marker key.

	Raises ValueError for ``#_snapshot_#``: only a snapshot writes it, as a part's first line
	(spec §19.10.2), and TSVZ writes none; as a record it could make readers skip the parts
	before it."""
	if marker and cells[0].lower() == SNAPSHOT_KEY:
		raise ValueError('{} is not written by a writer appending records: it marks a part that holds the '
						 "store's whole state, written by a snapshot (spec §19.10.2)".format(cells[0]))
	first = cells[0] if marker else _specEncodeField(cells[0], delimiter, isKey=True)
	if len(cells) == 1:
		return first
	return delimiter.join([first] + [_specEncodeField(cell, delimiter) for cell in cells[1:]])


def _specTombstones(keys, delimiter, strip):
	"""``(cells, marker)`` records that delete ``keys`` (spec §9.2) from a store whose stripping is ``strip``.

	While stripping is on a reader strips a tombstone's key too (spec §9.1): the
	tombstone of a key whose encoded form ends in a space or tab, stored while
	stripping was off, would delete the stripped key instead. A batch holding
	one is written with stripping off, and stripping is turned back on after it
	(spec §9.6).
	"""
	rows = [([key], False) for key in keys]
	if strip and any(_specStrippable(key, delimiter) for key in keys):
		rows.insert(0, (['#_strip_trailing_whites_#', 'false'], True))
		rows.append((['#_strip_trailing_whites_#', 'true'], True))
	return rows


def _specFormatRecords(rows, delimiter):
	"""``rows`` (``[(cells, marker), ...]``) as record lines, each with its '\\n'."""
	return ''.join(_specFormatRecord(cells, delimiter, marker=marker) + '\n' for cells, marker in rows)


def _specTombstoneLines(key, delimiter, strip):
	"""The tombstone of ``key`` (spec §9.6) as lines joined by '\\n', without a last '\\n'."""
	return '\n'.join(_specFormatRecord(cells, delimiter, marker=marker)
					 for cells, marker in _specTombstones([key], delimiter, strip))


def _specStrippable(key, delimiter):
	"""True when a reader that strips (spec §7.7) would change ``key``: its encoded form ends in a space or tab."""
	return key.endswith((' ', '\t')) and _specEncodeField(key, delimiter, isKey=True).endswith((' ', '\t'))


def _specNameKey(key, delimiter, strip, live):
	"""The key that a requested ``key`` names (spec §20.2.7) in a store whose stripping is ``strip``.

	While stripping is on, a key a reader would strip names the live key equal
	to it (``live`` holds the live keys), else the key a reader resolves it to.
	"""
	if not strip or key in live or not _specStrippable(key, delimiter):
		return key
	return _specDecodeField(_specEncodeField(key, delimiter, isKey=True).rstrip(' \t'), delimiter)


def _specNamesKeys(rows, delimiter):
	"""True when naming the keys of ``delete`` rows (``[(cells, marker), ...]``) needs the store (spec §20.2.7)."""
	return any(not marker and _specStrippable(cells[0], delimiter) for cells, marker in rows)


def _specDeleteRows(rows, delimiter, strip, live):
	"""The batch ``delete`` appends for ``rows`` (``[(cells, marker), ...]``, spec §20.2, §20.3).

	Every key names a key of the store as it was before the batch, whose
	stripping was ``strip`` and whose live keys are ``live`` (§20.2.7). The
	tombstone of a key a reader would strip is written with stripping off
	(§9.6). A reset of ``#_strip_trailing_whites_#`` in the batch turns
	stripping back on (§10.3, §12.1).
	"""
	batch = []
	named = strip
	guarded = False
	for cells, marker in rows:
		if marker:
			if cells[0].lower() == '#_strip_trailing_whites_#':
				strip, guarded = True, False
			batch.append((cells, marker))
			continue
		key = _specNameKey(cells[0], delimiter, named, live)
		if strip and not guarded and _specStrippable(key, delimiter):
			batch.append((['#_strip_trailing_whites_#', 'false'], True))
			guarded = True
		batch.append(([key], False))
	if guarded:
		batch.append((['#_strip_trailing_whites_#', 'true'], True))
	return batch


class _SpecState(object):
	"""Reader state of spec §12, carried across records and parts."""

	def __init__(self, defaults=None):
		self.newestVersion = 1  # the highest version a #_version_# line declared (spec §19.4, §19.8)
		self.defaults = list(defaults) if defaults and len(defaults) > 1 else [DEFAULTS_INDICATOR_KEY]
		self.strip = True
		self.fillEmpty = False
		self.returnDefaults = True
		self.rotate = 'keep'
		self.writeAck = 'memory'
		self.memberSize = _DEFAULT_MEMBER_SIZE
		self.onCorruption = 'recover'  # what a reader does with damaged data (spec §4.10)
		self.sawDefaults = False
		self.digests = {}
		self.mismatches = []
		self.rowState = None
		self.refresh()

	def refresh(self):
		"""Rebuild ``rowState``, the part of the state a data row binds to (spec §12.4.4)."""
		self.rowState = _RowState(tuple(self.defaults), self.strip, self.fillEmpty)

	@classmethod
	def fromRowState(cls, rowState):
		state = cls(list(rowState.defaults))
		state.strip = rowState.strip
		state.fillEmpty = rowState.fillEmpty
		state.rowState = rowState
		return state


def _asciiLower(text):
	"""``text`` lowercased over ASCII only (spec §12.4.1): other letters keep their case."""
	return ''.join(c.lower() if c < '\x80' else c for c in text)


def _asciiInt(value):
	"""The number the ASCII digits ``value`` spell, else 0. A value of more than 64 significant digits, which int()
	refuses from Python 3.11 when it is very long, counts as 10 ** 64: more than any version or member size."""
	if not _ASCII_DIGITS_RE.match(value):
		return 0
	digits = value.lstrip('0') or '0'
	return int(digits) if len(digits) <= 64 else 10 ** 64


def _specApplyMarker(state, keyLower, values, reporter, where):
	"""Apply an official marker (spec §12.4). Returns False when its value was rejected."""
	value = values[0] if values else ''  # marker lines are not stripped (spec §10.4)
	if keyLower == DEFAULTS_INDICATOR_KEY:
		state.defaults = [DEFAULTS_INDICATOR_KEY] + list(values)
		state.sawDefaults = True
	elif keyLower == '#_version_#':
		if value:  # an empty value resets to version 1, which changes nothing that is read
			number = _asciiInt(value)
			if number < 1:
				reporter.note('bad-marker', where, 'ignored #_version_# with invalid value {!r}'.format(value))
				return False
			if number > MAX_SPEC_VERSION:
				shown = value if len(value) <= 20 else '{}… ({} digits)'.format(value[:20], len(value))
				reporter.note('newer-version', where, 'declares spec version {}; read as version {}'.format(shown, MAX_SPEC_VERSION))
			state.newestVersion = max(state.newestVersion, number)
	elif keyLower in _BOOL_MARKERS:
		attr, default = _BOOL_MARKERS[keyLower]
		if not value:
			setattr(state, attr, default)
		elif _asciiLower(value) in _BOOL_VALUES:
			setattr(state, attr, _BOOL_VALUES[_asciiLower(value)])
		else:
			reporter.note('bad-marker', where, 'ignored {} with invalid value {!r}'.format(keyLower, value))
			return False
	elif keyLower == MEMBER_SIZE_KEY:
		size = _asciiInt(value)
		if not value:
			state.memberSize = _DEFAULT_MEMBER_SIZE
		elif size >= 1:
			state.memberSize = size
		else:
			reporter.note('bad-marker', where, 'ignored {} with invalid value {!r}'.format(keyLower, value))
			return False
	elif keyLower in _ENUM_MARKERS:
		attr, choices = _ENUM_MARKERS[keyLower]
		if not value:
			setattr(state, attr, choices[0])
		elif _asciiLower(value) in choices:
			setattr(state, attr, _asciiLower(value))
		else:
			reporter.note('bad-marker', where, 'ignored {} with invalid value {!r}'.format(keyLower, value))
			return False
	state.refresh()
	return True


def _specProcessRecord(text, state, delimiter, reporter, where):
	"""Classify and apply one committed record (spec §7.2-§7.9).

	``text`` is the decoded line without its terminator. Returns None for a
	comment, a marker or an empty key; ``(key, None)`` for a tombstone; and
	``(key, row)`` for a data row, where ``row`` holds the key and the written
	cells after stripping, decoding and fill-empty.
	"""
	fields = text.split(delimiter)
	f0 = fields[0]
	if f0.startswith('#'):
		if _MARKER_RE.match(f0):
			keyLower = f0.lower()
			if keyLower in _SPEC_MARKER_KEYS:
				_specApplyMarker(state, keyLower, [_specDecodeField(f, delimiter) for f in fields[1:]], reporter, where)
		return None
	strip = state.strip
	key = _specDecodeField(f0.rstrip(' \t') if strip else f0, delimiter)
	if not key:
		return None
	if len(fields) == 1:
		return (key, None)
	defaults = state.defaults
	fill = state.fillEmpty
	row = [key]
	for j in range(1, len(fields)):
		cell = fields[j]
		if strip:
			cell = cell.rstrip(' \t')
		if '<' in cell:
			cell = _specDecodeField(cell, delimiter)
		if not cell and fill and j < len(defaults):
			cell = defaults[j]
		row.append(cell)
	return (key, row)


class _Crc32(object):
	def __init__(self):
		self.value = 0

	def update(self, data):
		import zlib
		self.value = zlib.crc32(data, self.value)

	def hexdigest(self):
		return '%08x' % (self.value & 0xffffffff)


_HASHLIB_ALGORITHMS = frozenset(name.lower() for name in hashlib.algorithms_available)
_DIGEST_SUPPORT = {}


def _newDigest(algo):
	"""Return a fresh accumulator for ``algo`` (spec §15), or None if unsupported."""
	if algo == 'crc32':
		return _Crc32()
	if algo not in _HASHLIB_ALGORITHMS:
		return None
	try:
		digest = hashlib.new(algo)
		digest.hexdigest()
		return digest
	except (ValueError, TypeError):
		return None


def _digestSupported(algo):
	if algo not in _DIGEST_SUPPORT:
		_DIGEST_SUPPORT[algo] = _newDigest(algo) is not None
	return _DIGEST_SUPPORT[algo]


def _normalizeDefaults(defaults, delimiter):
	"""Accept every 3.39 ``defaults=`` form and return ``['#_defaults_#', d1, ...]``."""
	if not defaults or defaults is ...:
		return [DEFAULTS_INDICATOR_KEY]
	if isinstance(defaults, str):
		defaults = defaults.split(delimiter)
	else:
		try:
			defaults = list(defaults)
		except Exception:
			return [DEFAULTS_INDICATOR_KEY]
	defaults = [str(s).rstrip() if s else '' for s in defaults]
	if not any(defaults):
		return [DEFAULTS_INDICATOR_KEY]
	if defaults[0] != DEFAULTS_INDICATOR_KEY:
		defaults = [DEFAULTS_INDICATOR_KEY] + defaults
	return defaults


def _readerDefaults(defaults, delimiter):
	"""The ``defaults=`` of a legacy reader or scrub as the list it works on, the caller's never: text as TSVZed takes it
	(_normalizeDefaults), a sequence copied as it is. The first cell of a sequence stands in the key's column whatever
	it holds, as in every reader from 3.39 (``['', 'X']`` fills cell 1), where TSVZed puts #_defaults_# in front of one
	that does not begin with it."""
	if isinstance(defaults, str):
		return _normalizeDefaults(defaults, delimiter)
	return list(defaults) if defaults and defaults is not ... else []


def _cellText(item):
	"""3.39's conversion of a non-str cell given to the append helpers."""
	try:
		return str(item).rstrip()
	except Exception as e:
		return str(e)


class _PartInfo(object):
	"""What reading one part found: its codec, uncommitted tail and damage."""

	def __init__(self, path, codec=None):
		self.path = path
		self.codec = _parsePartName(path).codec if codec is None else codec
		self.tail = b''
		self.damaged = []  # what was wrong with a compressed part's members, for the warning
		self.lost = False  # damaged data was recovered, skipped or stopped at (spec §4.10): a rewrite keeps a copy
		self.stopped = False  # a stop policy ended the read here (spec §4.10)
		self.state = None  # the reader's marker state, for the corruption policy in force (spec §4.10)
		self.unreadable = False  # the part could not be opened, or this Python lacks its codec
		self.committed = 0  # bytes of committed lines read (decompressed bytes for a compressed part)
		self.lines = 0  # committed lines replayed (set by _specReplay)
		self.stamp = None  # the part's _specStamp just before it was opened
		# The last bytes (_SERVE_CHECK_BYTES at most) of a plain part's committed lines as read, ending at
		# ``committed`` unless a stop policy ended the read.
		self.check = b''
		# For a compressed part: its first bytes and the last ones before where its members were read
		# up to (_SERVE_CHECK_BYTES at most each), as they were read, and that raw offset.
		self.rawHead = b''
		self.rawEnd = 0
		self.rawCheck = b''
		self.skipped = False  # not read: a later snapshot part holds the store's state (spec §19.10.3)


def _newDecompressor(codec):
	name = _CODEC_NAMES.get(codec, codec)
	if name == 'gzip':
		import zlib
		return zlib.decompressobj(wbits=31)  # one gzip member; the caller starts the next
	return getattr(_codecModule(name), _CODECS[name][1])()


class _PartBytes(object):
	"""The raw bytes of a part, read in ``_CHUNK`` blocks and handed out in pieces; a piece can be given back."""

	def __init__(self, f, block=_CHUNK):
		self.f = f
		self.size = block  # bytes read from the file at a time
		self.block = b''
		self.at = 0  # offset of the next unread byte in ``block``
		self.base = 0  # raw offset of ``block[0]``
		self.before = b''  # the last _SERVE_CHECK_BYTES bytes read before ``block``, since the last seek
		self.head = None  # the first _SERVE_CHECK_BYTES bytes of the part, as read

	def offset(self):
		return self.base + self.at

	def seek(self, offset):
		self.f.seek(offset)
		self.block, self.at, self.base = b'', 0, offset
		self.before = b''

	def take(self, size):
		"""Up to ``size`` unread bytes (a memoryview), or an empty one at the end of the part."""
		if self.at >= len(self.block):
			self.before = (self.before + self.block[-_SERVE_CHECK_BYTES:])[-_SERVE_CHECK_BYTES:]
			self.base += len(self.block)
			self.block, self.at = self.f.read(self.size), 0
			if self.head is None and self.base == 0:
				self.head = self.block[:_SERVE_CHECK_BYTES]
		piece = memoryview(self.block)[self.at:self.at + size]
		self.at += len(piece)
		return piece

	def giveBack(self, size):
		"""Return the last ``size`` bytes of the piece just taken."""
		self.at -= size

	def tail(self):
		"""The last ``_SERVE_CHECK_BYTES`` bytes before the current offset, as read (since the last seek)."""
		return (self.before + self.block[max(0, self.at - _SERVE_CHECK_BYTES):self.at])[-_SERVE_CHECK_BYTES:]


def _gzipPadding(piece, codec):
	"""How many zero bytes start ``piece`` when they are gzip padding before a member (spec §16.6), else 0."""
	if _CODEC_NAMES[codec] != 'gzip':
		return 0
	return len(piece) - len(bytes(piece).lstrip(b'\0'))


def _readMember(src, codec):
	"""Decode the member at the current offset of ``src`` (a ``_PartBytes``): ``(status, output, start, end, error)``.

	``status`` is 'complete' (it decoded to its end and passed its checks;
	``src`` is left after it), 'short' (the part ended first), 'bad' (it failed
	to decode or its check: ``error``) or 'none' (no member: only gzip zero
	padding, or nothing). ``start`` is where the member begins and ``end`` the
	offset after it, or the end of the part. The whole output is held, so that
	a member is not used before it is known complete (spec §16.6): memory grows
	with the largest member, which writers keep to 1 MiB (spec §16.7).
	"""
	decompressor = _newDecompressor(codec)
	out = []
	start = None
	feed = 4096  # small pieces at first, so many small members cost one pass over the part
	while True:
		piece = src.take(feed)
		if not piece:
			break
		if start is None:
			piece = piece[_gzipPadding(piece, codec):]
			if not piece:
				continue
			start = src.offset() - len(piece)
		try:
			out.append(decompressor.decompress(piece))
		except Exception as e:
			return 'bad', b'', start, src.offset(), str(e)
		if decompressor.eof:
			src.giveBack(len(decompressor.unused_data))
			return 'complete', b''.join(out), start, src.offset(), None
		feed = min(feed * 2, _CHUNK)
	if start is None:
		return 'none', b'', src.offset(), src.offset(), None
	return 'short', b''.join(out), start, src.offset(), None


def _nextMemberStart(f, codec, start):
	"""The first raw offset at or after ``start`` where a member of ``codec`` begins (its magic bytes), or None.

	Whether that member is complete does not matter: a damaged member's bytes
	end there (spec §16.6 rule 2).
	"""
	magic = _CODECS[_CODEC_NAMES[codec]][3]
	offset = start
	while True:
		f.seek(offset)
		window = f.read(_CHUNK)
		if len(window) < len(magic):
			return None
		i = window.find(magic)
		if i >= 0:
			return offset + i
		offset += len(window) - len(magic) + 1


#: Yielded by _decompressChunks, returned by _specZeroFill: a stop policy ended the read (spec §4.10).
_STOP = object()


def _gzipHeaderLength(data):
	"""The length of the gzip member header at the start of ``data`` (RFC 1952), or None when it is not all there."""
	if len(data) < 10 or data[:3] != b'\x1f\x8b\x08':
		return None
	flags = data[3]
	pos = 10
	if flags & 4:  # FEXTRA
		if len(data) < pos + 2:
			return None
		pos += 2 + data[pos] + 256 * data[pos + 1]
	for flag in (8, 16):  # FNAME, FCOMMENT: zero-terminated
		if flags & flag:
			end = data.find(b'\0', pos)
			if end < 0:
				return None
			pos = end + 1
	if flags & 2:  # FHCRC
		pos += 2
	return pos if pos <= len(data) else None


def _gzipUnpad(region):
	"""A damaged gzip member's bytes without the zero padding that ends them (spec §16.6 rule 2), keeping the
	zero bytes that are its own: the end of its compressed data, or the rest of a trailer it has begun."""
	import zlib
	stripped = region.rstrip(b'\0')
	zeros = len(region) - len(stripped)
	if not zeros:
		return region
	start = _gzipHeaderLength(region)
	if start is None or start > len(stripped):
		return stripped
	inflater = zlib.decompressobj(-15)  # the compressed data alone, to see where it ends
	try:
		inflater.decompress(stripped[start:])
		if not inflater.eof:  # its last bytes may be zero bytes of its own
			# Fed only zeros, compressed data in a Huffman block ends within a few hundred bytes, fails or
			# never ends. In a stored block it can end only if the block is final, after the rest of its
			# NLEN (both bytes zero only when LEN is 65535) and of its data: at most 2 + 65535 zeros.
			zeros = min(zeros, 2 + 65535)
			fed = 0
			while fed < zeros:  # in pieces, with bounded output
				piece = b'\0' * min(1 << 16, zeros - fed)
				inflater.decompress(piece, 1 << 16)
				while inflater.unconsumed_tail and not inflater.eof:
					inflater.decompress(inflater.unconsumed_tail, 1 << 16)
				if inflater.eof:
					return stripped + b'\0' * (fed + len(piece) - len(inflater.unused_data))  # then padding
				fed += len(piece)
			return stripped
	except zlib.error:
		return stripped  # the zero bytes are not compressed data: padding after a member cut short
	begun = len(inflater.unused_data)  # trailer bytes before the zeros
	if 0 < begun < 8:
		return stripped + b'\0' * min(8 - begun, zeros)
	return stripped


def _decodeAlone(raw, codec):
	"""Decode a damaged member's bytes on their own (spec §16.6 rule 2): ``(cutShort, output, error)``.

	``cutShort`` is True when they merely end before the member does. Otherwise
	``output`` is what the member decoded before it failed: the piece that
	failed is fed again a byte at a time, so none of it is lost with the call
	that raised.
	"""
	step = 1 << 16
	def run(sizes):
		decompressor = _newDecompressor(codec)
		out = []
		pos = 0
		for size in sizes:
			piece = raw[pos:pos + size]
			if not piece:
				break
			try:
				out.append(decompressor.decompress(piece))
			except Exception as e:
				return out, pos, str(e)
			pos += len(piece)
			if decompressor.eof:
				break
		return out, None, None
	out, failed, error = run([step] * (len(raw) // step + 1))
	if failed is None:
		return True, b''.join(out), None
	out = run([step] * (failed // step) + [1] * min(step, len(raw) - failed))[0]
	return False, b''.join(out), error


def _decompressChunks(f, info):
	"""Yield what a compressed part contributes (spec §16.6), member by member.

	A complete member's output is used. A damaged member's bytes, up to the next
	member start (complete or not; gzip zero padding left out), are decoded on
	their own: a member cut short contributes what it decoded; a corrupt one
	follows the corruption policy in force at its first byte (spec §4.10): its
	output before the failure (recover), nothing (skip), or ``_STOP`` (stop).
	None is yielded before the next member, so the caller drops the line left
	unfinished at the damage. Damage is recorded on ``info``.
	"""
	src = _PartBytes(f)
	try:
		while True:
			status, out, start, end, error = _readMember(src, info.codec)
			if status == 'none':
				break
			if status == 'complete':
				if out:
					yield out
				continue
			following = _nextMemberStart(f, info.codec, start + 1)
			stop = f.seek(0, os.SEEK_END) if following is None else following
			f.seek(start)
			raw = f.read(stop - start)
			if _CODEC_NAMES[info.codec] == 'gzip':
				raw = _gzipUnpad(raw)  # zero padding before the next member (spec §16.6)
			cutShort, out, error = _decodeAlone(raw, info.codec)
			if cutShort:
				info.damaged.append('the member at byte {} is cut short; its whole lines are read'.format(start))
				if out:
					yield out
			else:
				info.lost = True
				policy = info.state.onCorruption if info.state is not None else 'recover'
				if policy == 'stop':
					info.damaged.append('the member at byte {} is corrupt ({}); reading stopped there (#_on_corruption_# stop)'.format(start, error))
					yield _STOP
					return
				if policy == 'skip':
					info.damaged.append('skipped the corrupt member at byte {} ({})'.format(start, error))
				else:
					info.damaged.append('used {} bytes the corrupt member at byte {} decoded before it failed ({}); they are not verified'.format(len(out), start, error))
					if out:
						yield out
			if following is None:
				break
			yield None
			src.seek(following)  # the next member, damaged or not; the search moved the file
	finally:
		info.rawHead = src.head or b''
		info.rawEnd, info.rawCheck = src.offset(), src.tail()


def _iterPartLines(info, reporter, label=''):
	"""Yield ``(rawLine, offset)`` for each committed line of one part (spec §4.3).

	A byte order mark at the start of the part's bytes is removed (spec §4.7);
	offsets still count it. Bytes after the last b'\\n' are not yielded; they
	are kept on ``info.tail`` and reported, as is any damage. An unreadable part
	is reported and skipped. ``label`` prefixes locations in reports.
	"""
	try:
		info.stamp = _specStamp(os.stat(info.path))  # just before the open: a change while it is read shows
		f = open(info.path, 'rb')
	except OSError as e:
		info.unreadable = True
		reporter.note('unreadable', None, 'skipped unreadable part {} ({})'.format(info.path, e))
		return
	if info.codec and not _codecSupported(info.codec):
		# Spec §16.5: a codec this Python lacks makes the part unreadable; it is not damage to repair.
		info.unreadable = True
		f.close()
		reporter.note('unreadable', None, 'cannot read part {}: no {} support in this Python'.format(info.path, _CODEC_NAMES.get(info.codec, info.codec)))
		return
	rest = b''
	atStart = True
	with f:
		chunks = _decompressChunks(f, info) if info.codec else iter(lambda: f.read(_CHUNK), b'')
		offset = 0
		try:
			for chunk in chunks:
				if chunk is None:  # a damaged member ended: the unfinished line before the next member is dropped
					rest = b''
					atStart = False  # the part's bytes began before this point (spec §4.7)
					continue
				if chunk is _STOP:
					info.stopped = True
					rest = b''  # nothing after the stop is read
					break
				data = rest + chunk if rest else chunk
				if atStart:
					if len(data) < 3 and b'\n' not in data:
						rest = data  # too few bytes yet to tell whether they begin with a byte order mark
						continue
					atStart = False
					if data.startswith(b'\xef\xbb\xbf'):
						data = data[3:]
						offset = 3
						reporter.note('bom', '{}line 1'.format(label), 'stripped a UTF-8 byte order mark')
				start = 0
				while True:
					nl = data.find(b'\n', start)
					if nl < 0:
						break
					yield data[start:nl + 1], offset
					offset += nl + 1 - start
					start = nl + 1
				rest = data[start:]
				if start and not info.codec:
					info.check = (info.check + data[max(0, start - _SERVE_CHECK_BYTES):start])[-_SERVE_CHECK_BYTES:]
		except OSError as e:
			info.unreadable = True
			reporter.note('unreadable', None, 'stopped reading part {} ({})'.format(info.path, e))
			info.committed = offset
			return
	info.committed = offset
	if rest:
		info.tail = rest
		reporter.note('tail', None, 'ignored uncommitted bytes after the last newline: {!r}'.format(rest[:80]))
	_reportPartDamage(info, reporter)


def _reportPartDamage(info, reporter):
	"""Report what was wrong with a compressed part's members (spec §4.10)."""
	if info.damaged:
		reporter.note('stopped' if info.stopped else 'damaged', None, 'compressed part {}: {}'.format(info.path, '; '.join(info.damaged)))


def _decodeLine(raw, reporter, where):
	"""Decode one committed line as UTF-8 (replacing bad bytes) and drop its terminator (spec §4.2)."""
	try:
		text = raw.decode('utf-8')
	except UnicodeDecodeError:
		text = raw.decode('utf-8', errors='replace')
		reporter.note('utf8', where, 'invalid UTF-8 replaced with U+FFFD')
	if text.endswith('\n'):
		text = text[:-1]
	if text.endswith('\r'):
		text = text[:-1]
	return text


def _specChecksum(state, algo, text, delimiter, reporter, where, position):
	"""Arm or verify-and-reset the ``algo`` accumulator (spec §15.3).

	A mismatch is noted on ``reporter`` and appended to ``state.mismatches`` as
	``position + (algo, expected, computed)``; ``position`` is ``(partPath, lineNo)``.
	"""
	if algo in state.digests:
		fields = text.split(delimiter)
		expected = fields[1].lower() if len(fields) > 1 else ''  # not stripped (spec §10.4)
		if expected:
			actual = state.digests[algo].hexdigest()
			if actual != expected:
				reporter.note('checksum', where, '#_checksum_{}_# mismatch: expected {}, computed {}; data kept'.format(algo, expected, actual))
				state.mismatches.append(tuple(position) + (algo, expected, actual))
	state.digests[algo] = _newDigest(algo)


def _specZeroFill(raw, delimiter, state, reporter, where):
	"""Apply spec §4.9 to one committed line: the bytes to replay, None to ignore the line, or ``_STOP``.

	A zero byte in a .tsvz, .csvz or .psvz record is zero-fill damage, handled by
	the corruption policy in force (spec §4.10).
	"""
	if delimiter == '\0':
		return raw  # the zero byte is the .nsvz delimiter: zero fill cannot be told from data
	cut = raw.rfind(b'\0') + 1
	if not cut:
		return raw
	policy = state.onCorruption
	if policy == 'stop':
		reporter.note('stopped', where, 'a zero-filled line: reading stopped there (#_on_corruption_# stop)')
		return _STOP
	if policy == 'skip':
		reporter.note('zero-fill', where, 'ignored a zero-filled line')
		return None
	reporter.note('zero-fill', where, 'read only the bytes after the zero-filled data in a line')
	return raw[cut:]


def _specReplay(parts, delimiter, state, reporter, infos, start=0):
	"""Replay ``parts`` as one stream (spec §7, §17.4).

	Yields ``(partIndex, offset, text, record)`` for every committed line except
	checksum markers of supported algorithms (``_specReplayLines``). Marker state
	and checksum accumulators in ``state`` carry across parts. One ``_PartInfo``
	per part is appended to ``infos``. The parts before ``start`` (a snapshot part,
	spec §19.10.3) are not read: their infos are marked ``skipped``.
	"""
	multi = len(parts) > 1
	for partIndex, path in enumerate(parts):
		info = _PartInfo(path)
		info.state = state
		infos.append(info)
		if partIndex < start:
			info.skipped = True
			continue
		label = os.path.basename(path) + ' ' if multi else ''
		yield from _specReplayLines(_iterPartLines(info, reporter, label), info, label, state, delimiter, reporter,
									partIndex=partIndex)
		if info.stopped:  # a stop policy ends the read of the whole store (spec §4.10)
			return


def _specReplayLines(lines, info, label, state, delimiter, reporter, lineNo=0, partIndex=0):
	"""Replay committed lines of the part ``info`` describes (spec §4.9, §7, §15).

	``lines`` yields ``(raw, offset)``: each line with its b'\\n' (a byte order
	mark already removed) and where it begins in the part; ``lineNo`` lines of the
	part come before them. Yields ``(partIndex, offset, text, record)`` for each
	line except checksum markers of supported algorithms, where ``record`` is what
	``_specProcessRecord`` returned; ``offset`` is past any zero fill. Sets
	``info.lines``, and ``info.lost`` and ``info.stopped`` for damaged data.
	"""
	path = info.path
	for raw, offset in lines:
		lineNo += 1
		where = '{}line {}'.format(label, lineNo)
		whole = raw  # digests cover the line as written, zero bytes included (spec §4.9)
		fill = _specZeroFill(raw, delimiter, state, reporter, where)
		if fill is not raw:
			info.lost = True
			if fill is _STOP:
				_reportPartDamage(info, reporter)  # the part reader stops here too, before its own report
				info.stopped = True
				info.lines = lineNo
				info.committed = offset  # where reading stopped: a follower checks the bytes before it
				return
			if fill is None:
				for digest in state.digests.values():
					digest.update(whole)
				continue
			offset += len(raw) - len(fill)
			raw = fill
		text = _decodeLine(raw, reporter, where)
		check = _CHECKSUM_RE.match(text.split(delimiter, 1)[0]) if text.startswith('#_') else None
		algo = check.group(1).lower() if check and _digestSupported(check.group(1).lower()) else None
		for name, digest in state.digests.items():
			if name != algo:
				digest.update(whole)
		if algo:
			_specChecksum(state, algo, text, delimiter, reporter, where, (path, lineNo))
			continue
		yield partIndex, offset, text, _specProcessRecord(text, state, delimiter, reporter, where)
	info.lines = lineNo


SNAPSHOT_KEY = '#_snapshot_#'


def _specIsSnapshotPart(path, delimiter):
	"""True when the part ``path`` begins with a value-less ``#_snapshot_#`` (spec §19.10.1).

	It counts as the first committed line, or the second after a comment. A part
	whose first lines cannot be read whole is not a snapshot part (spec §19.10.3).
	Only the start of the part is read (``_specFirstLines``); a compressed part that
	shows the marker there is read again as a read reads it, which uses a member only
	once it passed its checks, and must show it with no damage.
	"""
	if not _snapshotLines(_specFirstLines(path), delimiter):
		return False
	if not _parsePartName(path).codec:
		return True
	info = _PartInfo(path)
	lines = _iterPartLines(info, _Reporter(path))
	return _snapshotLines((raw for raw, _ in lines), delimiter, info, lines)


def _snapshotLines(lines, delimiter, info=None, close=None):
	"""Whether the first committed ``lines`` (raw, with their ends) of a part make it a snapshot part;
	``info``: the part read had no damage up to the marker. ``lines`` (and ``close``) are closed."""
	quiet = _Reporter('')  # never flushed: nothing found here is reported
	try:
		for lineNo, raw in enumerate(lines, 1):
			if lineNo == 1 and raw.startswith(b'\xef\xbb\xbf'):
				raw = raw[3:]  # spec §4.7
			fields = _decodeLine(raw, quiet, None).split(delimiter)
			if fields[0].lower() == SNAPSHOT_KEY:
				return not (info is not None and info.damaged) and all(f == '' for f in fields[1:])
			if lineNo == 2 or not fields[0].startswith('#') or _MARKER_RE.match(fields[0]):
				return False  # a first line that is not a comment, or a second line
	except _StartUnreadable:
		return False
	finally:
		lines.close()
		if close is not None:
			close.close()
	return False


def _specSnapshotStart(parts, delimiter):
	"""The index of the last snapshot part of ``parts``, where a read may begin (spec §19.10.3); else 0."""
	for index in range(len(parts) - 1, 0, -1):
		if _specIsSnapshotPart(parts[index], delimiter):
			return index
	return 0


def _storeParts(path, reporter=None):
	"""Return ``(parts, active)`` for the store ``path`` names (spec §17).

	``parts`` lists existing parts in replay order: the unnumbered file (part
	0) first, then ``<store>.<hex>`` parts by integer ordinal; ``.rotated``
	parts are excluded. ``active`` is the part appends go to: the highest
	numbered part, else ``path``. A path naming a numbered or rotated part is
	read alone (with a warning).
	"""
	name = _parsePartName(path)
	if name.ordinal is not None or name.rotated:
		if reporter is not None:
			reporter.note('fragment', None, 'names one part of a multi-part store; reading that part alone')
		return ([path] if os.path.isfile(path) else []), path
	directory = os.path.dirname(name.store)
	base = os.path.basename(name.store)
	own = os.path.basename(path)
	try:
		entries = os.listdir(directory or '.')
	except OSError:
		entries = []
	numbered = []
	for entry in entries:
		if entry == own or not (entry == base or entry.startswith(base + '.')):
			continue
		part = _parsePartName(entry)
		if not part.ext or part.store != base or part.rotated:
			continue
		if part.ordinal is None:
			if reporter is not None:
				reporter.note('part0-ambiguous', None, 'ignored {} beside {}: only the named file is part 0'.format(entry, own))
			continue
		numbered.append((part.ordinal, entry))
	numbered.sort()
	if reporter is not None:
		for i in range(1, len(numbered)):
			if numbered[i][0] == numbered[i - 1][0]:
				reporter.note('ordinal-tie', None, 'parts {} and {} share an ordinal; ordered by name'.format(numbered[i - 1][1], numbered[i][1]))
	parts = [path] if os.path.isfile(path) else []
	parts += [os.path.join(directory, entry) for _, entry in numbered]
	return parts, (parts[-1] if numbered else path)


#: Reads of a store that changes under them (spec §19.8) before the last one is kept as possibly torn.
_SPEC_READS = 3


def _specUnsteady(parts, infos):
	"""What changed in the parts ``infos`` describe while they were read, for a warning; None when nothing did.

	A part before the last must be as it was just before it was opened. The
	last part may also have grown by appends, as the handler's follower accepts
	them: the same file, longer, and for a plain part still holding the bytes
	read just before its committed offset; for a compressed part, still beginning
	with the raw bytes it began with when read, and holding those read just before
	where the read ended (appends add members after them, and change neither; damage
	the part held before is no change; a rewrite in place changes its first bytes). A
	part that was not there to open is not checked.
	"""
	last = len(parts) - 1
	for i, info in enumerate(infos):
		if info.stamp is None:
			continue
		stamp = _specFileStamp(info.path)
		if stamp == info.stamp:
			continue
		if i != last:
			return 'the parts before the last kept changing while they were read'
		appended = stamp is not None and not info.stopped and stamp[0] == info.stamp[0] and stamp[1] > info.stamp[1]
		if appended:
			# Where the read ended, in raw bytes, and the bytes it saw before that (committed counts raw
			# bytes only in a plain part, whose rawHead is empty).
			end, check = (info.rawEnd, info.rawCheck) if info.codec else (info.committed, info.check)
			try:
				with open(info.path, 'rb') as f:
					appended = stamp[1] >= end and f.read(len(info.rawHead)) == info.rawHead
					f.seek(end - len(check))
					appended = appended and f.read(len(check)) == check
			except OSError:
				appended = False
		if not appended:
			return 'the last part kept changing other than by appends while it was read'
	return None


#: The clock a torn TSVZed load's next read again is timed by.
_specClock = time.monotonic


def _specSteadyRead(read, reporter):
	"""Return what ``read(again)`` returns, done again while the store changed under it (spec §19.8).

	Readers take no lock, so a part rewritten in place while it is read (a
	scrub) gives a torn view. ``read`` replays the store and returns ``(result,
	parts, infos)``; ``again`` is True from its second call. When
	``_specUnsteady`` finds a part changed, the events the read noted on
	``reporter`` are forgotten and it is done again, up to ``_SPEC_READS``
	times in all. The last read is then kept, noted 'torn': an incomplete read,
	which nothing is written on the strength of.
	"""
	mark = reporter.mark()
	for attempt in range(_SPEC_READS):
		if attempt:
			reporter.rewind(mark)  # only the read kept reports its tolerance events
		result, parts, infos = read(bool(attempt))
		changed = _specUnsteady(parts, infos)
		if changed is None:
			return result
	reporter.note('torn', None, '{} ({} reads); the read may be torn'.format(changed, _SPEC_READS))
	return result


def _specVerify(fileName, delimiter, reporter):
	"""Replay the store ``fileName`` names and return its checksum mismatches (spec §15).

	Each mismatch is ``(partPath, lineNo, algo, expected, computed)``, where
	``lineNo`` is the 1-based line of the checksum marker in its part. The
	mismatches are returned rather than reported. A checksum marker whose
	algorithm this Python lacks is reported, since it cannot be verified. A
	read the store changed under is done again (``_specSteadyRead``).
	"""
	def read(again):
		state = _SpecState()
		parts, _ = _storeParts(fileName, reporter)
		infos = []
		for _, _, text, _ in _specReplay(parts, delimiter, state, reporter, infos):
			check = _CHECKSUM_RE.match(text.split(delimiter, 1)[0]) if text.startswith('#_') else None
			if check:
				reporter.note('checksum-unsupported', None, '#_checksum_{}_# not verified: this Python does not provide that algorithm'.format(check.group(1).lower()))
		return state.mismatches, parts, infos
	mismatches = _specSteadyRead(read, reporter)
	reporter.discard('checksum')
	return mismatches


class _SpecLoad(object):
	"""Result of ``_specLoad``."""

	def __init__(self):
		self.data = None
		self.state = None
		self.parts = []
		self.active = None
		self.infos = []
		self.transitions = []
		self.correctColumnNum = -1
		self.lastLine = None
		self.headerLine = None
		self.headerChecked = None  # the line the header was checked against, judged out loud once kept


def _specLoad(fileName, delimiter, header=(), verifyHeader=True, strict=True, defaults=None,
			  correctColumnNum=-1, storeOffset=False, taskDic=None, reporter=None, teeLogger=None,
			  verbose=False):
	"""Replay the store ``fileName`` names into ``taskDic`` (design §4).

	``defaults`` is the normalised ``['#_defaults_#', ...]`` preamble. Rows are
	padded to ``max(W, len(row-bound defaults))`` with the row-bound defaults;
	nothing is trimmed or dropped for its width. Header verification follows
	3.39 (``strict`` raises ``ValueError`` on a mismatch) against the first
	line of part 0 that is neither a marker nor blank. A read the store changed
	under is done again into ``taskDic`` as the caller passed it
	(``_specSteadyRead``).
	"""
	if reporter is None:
		reporter = _Reporter(fileName, teeLogger)
	given = list(taskDic.items()) if taskDic else []

	def read(again):
		if again and taskDic is not None:
			taskDic.clear()
			taskDic.update(given)
		load = _specLoadOnce(fileName, delimiter, header, verifyHeader, strict, defaults, correctColumnNum,
							 storeOffset, taskDic, reporter, teeLogger, verbose)
		return load, load.parts, load.infos
	load = _specSteadyRead(read, reporter)
	if load.headerChecked is not None:  # judged silently in each read; said, or raised on, once
		_lineContainHeader(header, load.headerChecked, verbose=verbose, teeLogger=teeLogger, strict=strict,
						   delimiter=delimiter)
	return load


def _specLoadOnce(fileName, delimiter, header, verifyHeader, strict, defaults, correctColumnNum, storeOffset,
				  taskDic, reporter, teeLogger, verbose):
	"""One replay of the store for ``_specLoad``."""
	result = _SpecLoad()
	result.data = taskDic if taskDic is not None else {}
	data = result.data
	state = _SpecState(defaults)
	result.state = state
	parts, active = _storeParts(fileName, reporter)
	result.parts, result.active = parts, active
	activeIndex = len(parts) - 1
	start = _specSnapshotStart(parts, delimiter)  # the parts before a snapshot part are not read (spec §19.10.3)
	if start:
		# The skipped history may have declared defaults: TSVZed and TSVZedLite write none of their own.
		state.sawDefaults = True
	width = correctColumnNum
	headerPending = bool(header and any(header) and verifyHeader)
	firstPartPending = True
	lastRowState = None
	for partIndex, offset, text, record in _specReplay(parts, delimiter, state, reporter, result.infos, start):
		if partIndex == start and firstPartPending and text.strip() and not _MARKER_RE.match(text.split(delimiter, 1)[0]):
			firstPartPending = False
			isComment = text.startswith('#')
			if isComment:
				result.headerLine = text
			if headerPending:
				headerPending = False
				candidate = text[1:] if isComment else text
				result.headerChecked = candidate  # said, or raised on, for the read kept only (_specLoad)
				if _headerMatches(header, candidate, delimiter):
					if not isComment:
						reporter.note('header-as-data', 'line 1', 'first line matches the header but is not a # comment; read as data key {!r} (prefix it with #)'.format(record[0] if record else text))
					if width == -1:
						width = len(header)
				elif strict:
					_lineContainHeader(header, candidate, verbose=verbose, teeLogger=teeLogger, strict=True,
									   delimiter=delimiter)  # raises: the store's own header line is not torn
		if record is None:
			if verbose and text.startswith('#_'):
				f0 = text.split(delimiter, 1)[0]
				if _MARKER_RE.match(f0) and f0.lower() not in _SPEC_MARKER_KEYS and f0.lower() != SNAPSHOT_KEY:
					__teePrintOrNot('Ignored unrecognised marker line {!r}'.format(text[:80]), teeLogger=teeLogger)
			continue
		key, row = record
		if row is None:
			data.pop(key, None)
			continue
		width = _specPadRow(row, state, width)
		result.lastLine = (offset, row)
		if storeOffset and partIndex == activeIndex and not result.infos[partIndex].codec:
			if state.rowState is not lastRowState:
				result.transitions.append((offset, state.rowState))
				lastRowState = state.rowState
			data[key] = offset
		else:
			data[key] = row
	if headerPending and not (len(result.infos) > start and (result.infos[start].unreadable or result.infos[start].lost)):
		# No line to compare (part 0 could not be read, or was damaged before any line is no mismatch):
		# said, or raised on, for the read kept only (_specLoad).
		result.headerChecked = ''
	result.correctColumnNum = width
	return result


def _specReplayQueued(load, records, delimiter):
	"""Replay formatted ``records`` not yet committed on ``load`` (a ``_SpecLoad``), as a read after their commit would."""
	quiet = _Reporter('')  # never flushed: they were checked when they were queued
	for record in list(records):
		for line in record.split('\n'):
			replayed = _specProcessRecord(line, load.state, delimiter, quiet, None)
			if replayed is None:
				continue
			key, row = replayed
			if row is None:
				load.data.pop(key, None)
			else:
				load.correctColumnNum = _specPadRow(row, load.state, load.correctColumnNum)
				load.data[key] = row


def _specCells(key, value, delimiter):
	"""``(key, cells)`` as a .tsvz set takes them (as 3.39 did): the key and every cell right-stripped, the key first."""
	key = str(key).rstrip()
	if isinstance(value, str):
		value = value.split(delimiter)
	cells = [str(s).rstrip() if s else '' for s in value]
	return key, (cells if cells and cells[0] == key else [key] + cells)


def _specFill(cells, defaults):
	"""Fill a set row's empty cells with ``defaults`` in place, as it is written (design §5.2)."""
	for i in range(1, min(len(cells), len(defaults))):
		if not cells[i] and defaults[i]:
			cells[i] = defaults[i]


def _specPadRow(row, state, width):
	"""Pad a data row in place to the store width with its row-bound defaults (design §4.7).

	``width`` is the store width so far (-1 before the first data row); the
	width after this row is returned. Nothing is trimmed.
	"""
	if width == -1:
		width = len(state.defaults) if len(state.defaults) > 1 else len(row)
	target = max(width, len(state.defaults))
	if len(row) < target:
		bound = state.defaults
		row.extend(bound[j] if j < len(bound) else '' for j in range(len(row), target))
	return width


def _specOptions(fileName, delimiter, encoding, reporter):
	"""Return ``(delimiter, 'utf8')`` for a spec path, overriding conflicting arguments (spec §4.1, §5.1)."""
	expected = _EXTENSION_DELIMITERS[_parsePartName(fileName).ext]
	if delimiter is not ... and delimiter and get_delimiter(delimiter) != expected:
		reporter.note('delimiter', None, 'delimiter {!r} conflicts with the file extension; using {!r} (spec §5.1)'.format(get_delimiter(delimiter), expected))
	if encoding:
		try:
			name = codecs.lookup(encoding).name
		except LookupError:
			name = str(encoding)
		if name != 'utf-8':
			reporter.note('encoding', None, 'encoding {!r} conflicts with spec §4.1; using UTF-8'.format(encoding))
	return expected, 'utf8'


def _specHeaderComment(header, delimiter):
	"""Format a header as a spec §11 comment line (without its '\\n')."""
	line = delimiter.join(_specEncodeField(cell, delimiter) for cell in header)
	return line if line.startswith('#') else '#' + line


def _specEnsureStore(fileName, createIfNotExist, header, defaults, strict, teeLogger, delimiter, errors='strict'):
	"""Make sure the store exists (3.39 ``_verifyFileExistence`` semantics).

	A new file starts with the header comment and, when ``defaults`` carry a
	value, the ``#_defaults_#`` marker; with neither it is 0 bytes. Returns
	False when the store is missing and may not be created (non-strict).
	"""
	parts, _ = _storeParts(fileName)
	if parts:
		return True
	if not createIfNotExist:
		if strict:
			__teePrintOrNot('File not found','error',teeLogger=teeLogger)
			raise FileNotFoundError("File not found")
		return False
	lines = []
	if header and any(header):
		_checkRow(header, delimiter, 'utf-8', errors, what='header')
		lines.append(_specHeaderComment(header, delimiter))
	if defaults and any(defaults[1:]):
		_checkRow(defaults, delimiter, 'utf-8', errors, what='defaults')
		lines.append(_specFormatRecord(defaults, delimiter, marker=True))
	content = _encodeText(''.join(line + '\n' for line in lines), 'utf-8', errors)
	codec = _parsePartName(fileName).codec
	try:
		_requireCodec(codec)  # not even an empty file in a codec this Python lacks
	except ImportError as e:
		__teePrintOrNot('Failed to create {}: {}'.format(fileName, e),'error',teeLogger=teeLogger)
		if strict:
			raise
		return False
	try:
		with open(fileName, 'xb') as f:
			f.write(_compressBytes(codec, content) if codec and content else content)
		__teePrintOrNot('Created '+fileName,teeLogger=teeLogger)
	except FileExistsError:
		pass
	except Exception:
		__teePrintOrNot('Failed to create '+fileName,'error',teeLogger=teeLogger)
		if strict:
			raise FileNotFoundError("Failed to create file")
		return False
	return True


def _specReadTabularFile(fileName, teeLogger=None, header='', createIfNotExist=False, lastLineOnly=False,
						 verifyHeader=True, verbose=False, taskDic=None, encoding='utf8', strict=True,
						 delimiter=..., defaults=..., correctColumnNum=-1, storeOffset=False, errors='strict'):
	"""``readTabularFile`` for spec paths (same signature and return values as 3.39)."""
	if taskDic is None:
		taskDic = {}
	reporter = _Reporter(fileName, teeLogger)
	try:
		delimiter, encoding = _specOptions(fileName, delimiter, encoding, reporter)
		header = _formatHeader(header, verbose=verbose, teeLogger=teeLogger, delimiter=delimiter)
		initial = _normalizeDefaults(defaults, delimiter)
		if not _specEnsureStore(fileName, createIfNotExist, header, initial, strict, teeLogger, delimiter, errors):
			if lastLineOnly:
				return -1 if storeOffset else []
			return taskDic
		load = _specLoad(fileName, delimiter, header=header, verifyHeader=verifyHeader, strict=strict,
						 defaults=initial, correctColumnNum=correctColumnNum,
						 storeOffset=storeOffset and not lastLineOnly,
						 taskDic={} if lastLineOnly else taskDic, reporter=reporter,
						 teeLogger=teeLogger, verbose=verbose)
	finally:
		reporter.flush()
	if lastLineOnly:
		if load.lastLine is None:
			return -1 if storeOffset else []
		return load.lastLine[0] if storeOffset else load.lastLine[1]
	return taskDic


def _fsyncDirectory(path):
	"""Sync the directory holding ``path``, so a file just created there survives a crash (POSIX)."""
	if os.name != 'posix':
		return
	unsupported = {errno.EINVAL, errno.EACCES} | {getattr(errno, name) for name in ('ENOTSUP', 'EOPNOTSUPP') if hasattr(errno, name)}
	try:
		fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
		try:
			os.fsync(fd)
		finally:
			os.close(fd)
	except OSError as e:
		if e.errno not in unsupported:
			raise  # some network and FUSE file systems do not sync directories, and a write-only one cannot be opened


def _specBackupPart(path, reporter):
	"""Copy a part's bytes to a new ``<path>.damaged-<timestamp>`` (``.1``, ``.2``, ... when taken) and sync it.

	For a rewrite that drops damaged members (spec §16.6). Raises OSError when
	the copy cannot be made; a partial copy is removed.
	"""
	with _pathLock(path):
		with open(path, 'r+b') as f:
			_lockFile(f, mode=_REWRITE)
			try:
				first = backup = '{}.damaged-{}'.format(path, time.strftime('%Y%m%dT%H%M%S'))
				# Readable by whoever could read the part, writable by its owner only, never setuid,
				# setgid, sticky or executable (the umask still applies on top).
				st = os.fstat(f.fileno())
				mode = st.st_mode & 0o644
				suffix = 0
				while True:
					try:
						# never overwrite an earlier backup; O_BINARY: no newline translation on Windows
						flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
						copy = os.fdopen(os.open(backup, flags, mode), 'wb')
						break
					except FileExistsError:
						suffix += 1
						backup = '{}.{}'.format(first, suffix)
				try:
					with copy:
						_matchOwner(copy.fileno(), st)
						shutil.copyfileobj(f, copy)
						copy.flush()
						os.fsync(copy.fileno())
				except BaseException:
					os.remove(backup)  # a partial copy is no backup
					raise
				_fsyncDirectory(backup)  # the new name is durable before the part is rewritten
			finally:
				_unlockFile(f)
	reporter.note('backup', None, 'kept the bytes of {}, whose damaged members are not rewritten, in {}'.format(path, backup))
	return backup


def _specAppendTo(f, path, payload, reporter, fsync=False, memberSize=None, proceed=None):
	"""Append whole records to the open part ``f`` (``path``) under its locks (design §5.4).

	Uncompressed: an uncommitted tail is truncated first. Compressed: the
	payload becomes new members of at most ``memberSize`` bytes (spec §16.7);
	the part is not read or checked for damage, since readers handle a damaged
	member (spec §16.6). Returns ``(offset, before, after)``: where the payload
	begins, and the part's ``_specStamp`` just before and after it is written,
	both taken under the lock, so no other locked writer's append falls between.
	``proceed``: called once the lock is taken; False writes nothing and returns None.
	"""
	with _pathLock(path):
		_seekRawStart(f)  # msvcrt locks from the raw position: lock and unlock the same range
		_lockFile(f, mode=_APPEND)
		try:
			if proceed is not None and not proceed():
				return None
			before = _specStamp(os.fstat(f.fileno()))
			offset = _appendOpen(f, payload, _parsePartName(path).codec, None, 'truncate', reporter, fsync, memberSize)
			return offset, before, _specStamp(os.fstat(f.fileno()))
		finally:
			try:
				_seekRawStart(f)
			except OSError:
				pass  # a failed write is already propagating; the lock must still be released
			_unlockFile(f)


def _specAppendPayload(path, payload, reporter, fsync=False, memberSize=None, proceed=None):
	"""``_specAppendTo`` the part ``path``; a missing part is created."""
	_requireCodec(_parsePartName(path).codec)
	with open(path, 'a+b') as f:
		return _specAppendTo(f, path, payload, reporter, fsync, memberSize, proceed)


#: Reads a scrub or clear makes before it gives up on a store that keeps changing.
_SPEC_REWRITE_ATTEMPTS = 3


def _specStamp(st):
	"""What identifies a part's bytes between a read and a rewrite: (inode, size, mtime, ctime). The change
	time catches a rewrite to the same size whose modification time was set back (cp -p, rsync -a): no
	program sets it back."""
	return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _specFileStamp(path):
	"""The ``_specStamp`` of the file ``path``, or None when it cannot be looked at."""
	try:
		return _specStamp(os.stat(path))
	except OSError:
		return None


def _specSnapshot(fileName):
	"""Return ``(parts, stamp)`` before a read: ``stamp`` is the single part's ``_specStamp``, else None."""
	parts, _ = _storeParts(fileName)
	return parts, (_specFileStamp(parts[0]) if len(parts) == 1 else None)


def _matchOwner(fd, st):
	"""Give a new copy the owner and group of the file ``st`` describes. Where that is not allowed,
	a copy left in another group loses its group bits, so no one reads it who could not read the file."""
	if not hasattr(os, 'fchown'):
		return
	for uid in (st.st_uid, -1):
		try:
			os.fchown(fd, uid, st.st_gid)
			return
		except OSError:
			pass
	created = os.fstat(fd)
	if created.st_gid != st.st_gid:
		os.fchmod(fd, created.st_mode & 0o7707)  # what the umask left, without the group bits


def _specRewriteInPlace(path, content, expect=None, memberSize=None, build=None):
	"""Replace a part's bytes in place (same inode, spec §18.7 / §19.8): the write of a spec store's scrub and
	clear, and of a legacy file's (_legacyReplace, _legacyDropKeys).

	``content`` is uncompressed; it is compressed for a compressed part. The
	file is left alone when its bytes already match. Returns ``(written, stamp)``:
	whether it wrote, and the part's ``_specStamp`` then, taken under the lock.
	``expect`` is a ``_specStamp`` taken before the caller read the part: when
	the part no longer matches it under the lock, another writer committed in
	between, so nothing is written and None is returned. ``build``, instead of
	``content``: called with the open file, locked, to read it and return the
	content (None: write nothing), so no other writer's change falls between the read
	and the write.
	"""
	codec = _parsePartName(path).codec
	_requireCodec(codec)
	raw = None if build else _compressBytes(codec, content, memberSize=memberSize) if codec else content  # (nothing: one empty member)
	with _pathLock(path):
		with open(path, 'r+b') as f:
			_lockFile(f, mode=_REWRITE)
			try:
				st = os.fstat(f.fileno())
				if expect is not None and _specStamp(st) != expect:
					return None
				if build:
					content = build(f)
					if content is None:
						return False, _specStamp(st)
					raw = _compressBytes(codec, content, memberSize=memberSize) if codec else content
				fd = f.fileno()
				# Read unbuffered, as the file is written below: a read-ahead in f would leave it a stale position to
				# close at. What a failed rewrite puts back is kept: the bytes the write overwrites (from the start to
				# the shorter end) and the old size to cut back to. A clear of a huge file reads none of it.
				size = st.st_size
				old = _readFrom0(fd, min(size, len(raw)))
				if size == len(raw) and old == raw:
					return False, _specStamp(st)
				try:
					_reserveSpace(fd, len(raw))  # spec §19.8: a full disk refuses the rewrite before any byte changes
					_writeFrom0(fd, raw)
				except BaseException as e:
					# Once the cut is made the rewrite is done: only the cut makes the file shorter, and it follows the
					# whole write, so the new bytes are complete (what was cut off is not kept to put back).
					if os.fstat(fd).st_size < size:
						raise
					# Put the old bytes back rather than leave a half-rewritten part (or one a failed
					# reservation lengthened): the write left what it did not overwrite as it was.
					try:
						os.lseek(fd, 0, os.SEEK_SET)
						view = memoryview(old)
						while view:
							view = view[os.write(fd, view):]
						os.ftruncate(fd, size)
						os.fsync(fd)
					except OSError as undo:
						damaged = '{}: the rewrite failed ({!r}) and the old bytes could not be put back ({}): the part is damaged'.format(
							path, e, undo)
						if isinstance(e, OSError):
							raise OSError(e.errno, damaged)
						_warn('TSVZ warning: ' + damaged)  # an interrupt stays an interrupt
					raise
				os.fsync(fd)
				return True, _specStamp(os.fstat(fd))
			finally:
				try:
					_seekRawStart(f)  # msvcrt unlocks from the raw position: the range it locked
				except OSError:
					pass
				_unlockFile(f)


def _reserveSpace(fd, length):
	"""Reserve ``length`` bytes from the start of the file before it is rewritten in place (spec
	§19.8). Where the platform or the file system cannot reserve space, nothing is reserved."""
	fallocate = getattr(os, 'posix_fallocate', None)
	if fallocate is None or length <= 0:
		return
	try:
		fallocate(fd, 0, length)
	except OSError as e:
		if e.errno not in (errno.EOPNOTSUPP, getattr(errno, 'ENOTSUP', errno.EOPNOTSUPP), errno.ENOSYS, errno.EINVAL):
			raise


def _readFrom0(fd, length):
	"""Up to ``length`` bytes from the start of the file (unbuffered)."""
	os.lseek(fd, 0, os.SEEK_SET)
	chunks = []
	while length > 0:
		chunk = os.read(fd, length)
		if not chunk:
			break
		chunks.append(chunk)
		length -= len(chunk)
	return b''.join(chunks)


def _writeFrom0(fd, data):
	"""Write ``data`` over the file from offset 0 and cut the file there (unbuffered)."""
	os.lseek(fd, 0, os.SEEK_SET)
	view = memoryview(data)
	while view:
		view = view[os.write(fd, view):]
	os.ftruncate(fd, len(data))


def _specPreamble(state, delimiter, compensate=False, version=True):
	"""Marker lines that re-create ``state`` (spec §19.2.3a): version, non-default markers, defaults last.

	``compensate`` forces stripping and fill-empty off for the rows that follow
	(the §19.4 compensating layout; ``_specPostamble`` restores them).
	"""
	d = delimiter
	lines = ['#_version_#' + d + '1'] if version else []
	if compensate:
		lines.append('#_strip_trailing_whites_#' + d + 'false')
		lines.append('#_fill_empty_with_default_#' + d + 'false')
	else:
		if not state.strip:
			lines.append('#_strip_trailing_whites_#' + d + 'false')
		if state.fillEmpty:
			lines.append('#_fill_empty_with_default_#' + d + 'true')
	if not state.returnDefaults:
		lines.append('#_return_defaults_when_missing_#' + d + 'false')
	if state.rotate != 'keep':
		lines.append('#_rotate_#' + d + state.rotate)
	if state.writeAck != 'memory':
		lines.append('#_write_ack_#' + d + state.writeAck)
	if state.memberSize != _DEFAULT_MEMBER_SIZE:
		lines.append(MEMBER_SIZE_KEY + d + str(state.memberSize))
	if state.onCorruption != 'recover':
		lines.append('#_on_corruption_#' + d + state.onCorruption)
	if any(state.defaults[1:]):
		lines.append(_specFormatRecord(state.defaults, d, marker=True))
	return lines


class _StartUnreadable(Exception):
	"""The start of a part could not be decoded: its first lines are unknown."""


def _specFirstLines(path):
	"""Yield the committed lines at the start of a part as they decode, for the 3.39 header check.

	Compressed members are streamed, not held until their check (spec §16.6
	governs records; nothing is read or written from these lines), so the
	check costs only what it decodes. Raises ``_StartUnreadable`` when the part
	cannot be opened, read or decoded before the lines run out.
	"""
	codec = _parsePartName(path).codec
	if codec and not _codecSupported(codec):
		raise _StartUnreadable()
	try:
		f = open(path, 'rb')
	except OSError:
		raise _StartUnreadable()
	with f:
		try:
			if not codec:
				for raw in f:
					if not raw.endswith(b'\n'):
						return  # uncommitted (spec §4.3)
					yield raw
				return
			src = _PartBytes(f, block=1 << 16)
			decompressor = None
			pending = []  # pieces of the line not yet ended
			while True:
				piece = src.take(1 << 16)
				if not piece:
					return
				if decompressor is None:
					piece = piece[_gzipPadding(piece, codec):]
					if not piece:
						continue
					decompressor = _newDecompressor(codec)
				try:
					data = decompressor.decompress(piece)
				except Exception:
					raise _StartUnreadable()
				if decompressor.eof:
					src.giveBack(len(decompressor.unused_data))
					decompressor = None
				start = 0
				while True:
					nl = data.find(b'\n', start)
					if nl < 0:
						break
					pending.append(data[start:nl + 1])
					yield b''.join(pending)
					pending = []
					start = nl + 1
				if start < len(data):
					pending.append(data[start:])
		except OSError:
			raise _StartUnreadable()


def _specCheckHeader(fileName, header, delimiter, strict, verbose, teeLogger):
	"""3.39 header verification against the first non-marker, non-blank line of part 0.

	Skipped, as a read skips it, when the start of part 0 cannot be read.
	"""
	parts, _ = _storeParts(fileName)
	line = ''
	if parts:
		reporter = _Reporter(fileName)  # not flushed: the full read reports these
		lines = _specFirstLines(parts[0])
		try:
			for raw in lines:
				text = _decodeLine(raw, reporter, None)
				if text.strip() and not _MARKER_RE.match(text.split(delimiter, 1)[0]):
					line = text
					break
		except _StartUnreadable:
			return True
		finally:
			lines.close()
	candidate = line[1:] if line.startswith('#') else line
	return _lineContainHeader(header, candidate, verbose=verbose, teeLogger=teeLogger, strict=strict, delimiter=delimiter)


def _specAppendLinesTabularFile(fileName, linesToAppend, teeLogger=None, header='', createIfNotExist=False,
								verifyHeader=True, verbose=False, encoding='utf8', strict=True, delimiter=..., errors='strict'):
	"""``appendLinesTabularFile`` for spec paths: rows are written exactly as given (design §5.2)."""
	reporter = _Reporter(fileName, teeLogger)
	try:
		delimiter, encoding = _specOptions(fileName, delimiter, encoding, reporter)
		header = _formatHeader(header, verbose=verbose, teeLogger=teeLogger, delimiter=delimiter)
		records = []
		for line in linesToAppend:
			if isinstance(linesToAppend, dict):
				key = line
				line = linesToAppend[key]
			if isinstance(line, str):
				line = line.split(delimiter)
			elif line:
				line = [item if isinstance(item, str) else _cellText(item) for item in line]
			else:
				line = []
			if isinstance(linesToAppend, dict) and (not line or line[0] != key):
				line = [key] + line
			if not line:
				continue
			_checkRow(line, delimiter, 'utf-8', errors, header)  # before the store is created or touched
			records.append(_specFormatRecord(line, delimiter, marker=bool(_MARKER_RE.match(line[0]))))
		_specAppendRecords(fileName, records, reporter, teeLogger=teeLogger, header=header,
						   createIfNotExist=createIfNotExist, verifyHeader=verifyHeader, verbose=verbose,
						   strict=strict, delimiter=delimiter, errors=errors)
	finally:
		reporter.flush()


def _specAppendRecords(fileName, records, reporter, teeLogger=None, header=(), createIfNotExist=False,
					   verifyHeader=True, verbose=False, strict=True, delimiter='\t', errors='strict'):
	"""Append formatted records (each without its '\\n') to the store in one write (spec §18.2).

	The store is created first when ``createIfNotExist`` allows it (see
	``_specEnsureStore``); ``header`` is a formatted header list. Returns False
	when the store is missing and may not be created.
	"""
	if not _specEnsureStore(fileName, createIfNotExist, header, [DEFAULTS_INDICATOR_KEY], strict, teeLogger, delimiter, errors):
		return False
	if not records:
		if verbose:
			__teePrintOrNot(f"No lines to append to {fileName}",teeLogger=teeLogger)
		return True
	if any(header) and verifyHeader:
		_specCheckHeader(fileName, header, delimiter, strict, verbose, teeLogger)
	_, active = _storeParts(fileName, reporter)
	_specAppendPayload(active, _encodeText(''.join(record + '\n' for record in records), 'utf-8', errors), reporter)
	if verbose:
		__teePrintOrNot(f"Appended {len(records)} lines to {fileName}",teeLogger=teeLogger)
	return True


def _specKeptHeader(load, header, delimiter, errors):
	"""The header line a clear or scrub writes, as a list of 0 or 1 lines: the store's own, else ``header``
	(checked, as it is written only then)."""
	if load.headerLine:
		return [load.headerLine]
	if not any(header):
		return []
	_checkRow(header, delimiter, 'utf-8', errors, what='header')
	return [_specHeaderComment(header, delimiter)]


def _specClearTabularFile(fileName, **kwargs):
	"""``clearTabularFile`` for spec paths (``_specClear``): True when the store was cleared."""
	return _specClear(fileName, **kwargs)[0]


def _specClear(fileName, teeLogger=None, header='', verifyHeader=False, verbose=False,
			   encoding='utf8', strict=False, delimiter=..., errors='strict'):
	"""Clear a spec store (design §5.5, §5.8); return ``(cleared, seen)``: whether it was cleared, and the
	``_specStamp`` of the part written, taken under its lock right after the write (None when none was).

	A single file is read without a lock and rewritten in place only if no
	other writer changed it since the read (else it is read again, up to
	``_SPEC_REWRITE_ATTEMPTS`` times). A store whose read may be torn is left
	alone, so a multi-part store never gets tombstones for the keys of a torn
	view. A path naming one part of a multi-part store is refused, and so is a
	single file declaring a spec version newer than ``MAX_SPEC_VERSION``.
	"""
	changing = 'clear skipped: the store kept changing while it was being cleared; nothing was written'
	reporter = _Reporter(fileName, teeLogger)
	attempt = None
	cleared, seen = False, None
	try:
		delimiter, encoding = _specOptions(fileName, delimiter, encoding, reporter)
		header = _formatHeader(header, verbose=verbose, teeLogger=teeLogger, delimiter=delimiter)
		name = _parsePartName(fileName)
		if name.ordinal is not None or name.rotated:
			reporter.note('fragment', None, 'clear skipped: {} names one part of a multi-part store; clear the store path instead; nothing was written'.format(fileName))
			return False, None
		if not _specEnsureStore(fileName, True, header, [DEFAULTS_INDICATOR_KEY], False, teeLogger, delimiter, errors):
			raise FileNotFoundError("Something catastrophic happened! File still not found after creation")
		headerChecked = False
		for _ in range(_SPEC_REWRITE_ATTEMPTS):
			attempt = _Reporter(fileName, teeLogger)  # only the last read reports its tolerance events
			parts, stamp = _specSnapshot(fileName)
			load = _specLoad(fileName, delimiter, header=header, verifyHeader=False, strict=False,
							 reporter=attempt, teeLogger=teeLogger, verbose=verbose)
			if attempt.has('torn'):  # read again already: never clear the keys of a torn view
				attempt.note('changing', None, changing)
				return False, None
			if attempt.incomplete():
				if _storeParts(fileName)[0] != parts:
					continue  # a part went away during the read (a rotation): read again
				attempt.note('refused', None, 'clear skipped: a part of the store could not be read; nothing was written')
				return False, None
			if len(load.parts) > 1:
				tombstones = _specFormatRecords(_specTombstones(load.data, delimiter, load.state.strip), delimiter)
				if tombstones:
					seen = _specAppendPayload(load.active, _encodeText(tombstones, 'utf-8', errors), attempt,
											  memberSize=load.state.memberSize)[2]
				attempt.note('multipart', None, 'cleared a multi-part store by appending tombstones; older parts are not compacted')
				cleared = True
				break
			if stamp is None or load.parts != parts:
				continue  # the set of parts changed under the read
			if load.state.newestVersion > MAX_SPEC_VERSION:
				attempt.note('refused', None, 'clear skipped: the part declares a spec version newer than {} (#_version_#), whose '
							 'markers this TSVZ cannot keep (spec §19.8); nothing was written'.format(MAX_SPEC_VERSION))
				return False, None
			if any(header) and verifyHeader and load.headerLine is not None and not headerChecked:
				headerChecked = True
				if not _lineContainHeader(header, load.headerLine[1:], verbose=verbose, teeLogger=teeLogger, strict=strict, delimiter=delimiter):
					__teePrintOrNot(f'Warning: Header mismatch in {fileName}. Keeping original header in file...','warning',teeLogger)
			lines = _specKeptHeader(load, header, delimiter, errors) + _specPreamble(load.state, delimiter, version=False)
			done = _specRewriteInPlace(load.parts[0], _encodeText(''.join(line + '\n' for line in lines), 'utf-8', errors),
									   expect=stamp, memberSize=load.state.memberSize)
			if done is not None:
				cleared, seen = True, done[1]
				break
		else:
			attempt.note('changing', None, changing)
	finally:
		reporter.flush()
		if attempt is not None:
			attempt.flush()
	if verbose and cleared:
		__teePrintOrNot(f"Cleared {fileName}",teeLogger=teeLogger)
	return cleared, seen


def _specPostamble(state, delimiter):
	"""Marker lines that restore stripping / fill-empty after a compensating scrub layout."""
	lines = []
	if state.strip:
		lines.append('#_strip_trailing_whites_#' + delimiter + 'true')
	if state.fillEmpty:
		lines.append('#_fill_empty_with_default_#' + delimiter + 'true')
	return lines


def _specScrubCells(row, state):
	"""Cells to write for a materialised row: extended with '' to the final defaults' width.

	A materialised row already carries every column that is not '' (design
	§4.7); the explicit '' cells keep a final default it was never bound to
	from filling the columns beyond it.
	"""
	final = state.defaults
	if len(final) > len(row):
		return row + [''] * (len(final) - len(row))
	return row


def _specRoundTrips(cells, state):
	"""True when ``cells`` read back unchanged under the final marker state (spec §19.4)."""
	if state.strip:
		for cell in cells:
			if cell and cell[-1] in ' \t':
				return False
	if state.fillEmpty:
		final = state.defaults
		for j in range(1, len(cells)):
			if not cells[j] and j < len(final) and final[j]:
				return False
	return True


def _specScrubTabularFile(fileName, teeLogger=None, header='', createIfNotExist=False, lastLineOnly=False,
						  verifyHeader=True, verbose=False, taskDic=None, encoding='utf8', strict=False,
						  delimiter=..., defaults=..., correctColumnNum=-1, errors='strict'):
	"""``scrubTabularFile`` for spec paths: archival in-place compaction (design §5.7)."""
	if lastLineOnly:
		return _specReadTabularFile(fileName, teeLogger=teeLogger, header=header, createIfNotExist=createIfNotExist,
									lastLineOnly=True, verifyHeader=verifyHeader, verbose=verbose, encoding=encoding,
									strict=strict, delimiter=delimiter, defaults=defaults, correctColumnNum=correctColumnNum,
									errors=errors)
	return _specScrub(fileName, teeLogger=teeLogger, header=header, createIfNotExist=createIfNotExist,
					  verifyHeader=verifyHeader, verbose=verbose, taskDic=taskDic, encoding=encoding,
					  strict=strict, delimiter=delimiter, defaults=defaults, correctColumnNum=correctColumnNum,
					  errors=errors)[0]


def _specScrub(fileName, teeLogger=None, header='', createIfNotExist=False, verifyHeader=True, verbose=False,
			   taskDic=None, encoding='utf8', strict=False, delimiter=..., defaults=..., correctColumnNum=-1, errors='strict'):
	"""Compact a single-part store in place; return ``(taskDic, done)``.

	The store is read without a lock and rewritten in place only if no other
	writer changed it since the read (else it is read again, up to
	``_SPEC_REWRITE_ATTEMPTS`` times), so a record committed meanwhile is
	never compacted away. ``done`` is False when nothing was written because
	the store is missing, has several parts (or the path names one part),
	declares a spec version newer than ``MAX_SPEC_VERSION``, or kept changing. It is None when the read stopped at damaged data (spec
	§4.10): the part was rewritten from what was read, the original kept.
	"""
	if taskDic is None:
		taskDic = {}
	reporter = _Reporter(fileName, teeLogger)
	attempt = None
	try:
		delimiter, encoding = _specOptions(fileName, delimiter, encoding, reporter)
		header = _formatHeader(header, verbose=verbose, teeLogger=teeLogger, delimiter=delimiter)
		initial = _normalizeDefaults(defaults, delimiter)
		if not _specEnsureStore(fileName, createIfNotExist, header, initial, strict, teeLogger, delimiter, errors):
			return taskDic, False
		name = _parsePartName(fileName)
		given = list(taskDic.items()) if taskDic else []
		backedUp = None  # the stamp of the part the last backup copied
		for tries in range(_SPEC_REWRITE_ATTEMPTS):
			if tries:
				# Read again into the dict as the caller passed it.
				taskDic.clear()
				taskDic.update(given)
			attempt = _Reporter(fileName, teeLogger)  # only the last read reports its tolerance events
			parts, stamp = _specSnapshot(fileName)
			load = _specLoad(fileName, delimiter, header=header, verifyHeader=verifyHeader, strict=strict,
							 defaults=initial, correctColumnNum=correctColumnNum, taskDic=taskDic,
							 reporter=attempt, teeLogger=teeLogger, verbose=verbose)
			if attempt.has('unreadable'):
				if _storeParts(fileName)[0] != parts:
					continue  # a part went away during the read (a rotation): read again
				attempt.note('refused', None, 'scrub skipped: a part of the store could not be read; nothing was written')
				return taskDic, False
			if len(load.parts) != 1 or name.ordinal is not None or name.rotated:
				attempt.note('multipart', None, 'scrub skipped: TSVZ does not compact multi-part stores or single parts of them; nothing was written')
				return taskDic, False
			if attempt.has('torn'):
				break  # the part kept changing while it was read, which was done again already
			if stamp is None or load.parts != parts:
				continue  # the set of parts changed under the read
			if load.state.newestVersion > MAX_SPEC_VERSION:
				attempt.note('refused', None, 'scrub skipped: the store declares a spec version newer than {0} (#_version_#), which '
							 'this TSVZ reads as version {0} only (spec §6.3); a rewrite would make that reading permanent; '
							 'nothing was written'.format(MAX_SPEC_VERSION))
				return taskDic, False
			info = load.infos[0]
			state = load.state
			rows = [_specScrubCells(row, state) for row in taskDic.values()]
			compensate = not all(_specRoundTrips(cells, state) for cells in rows)
			preamble = _specPreamble(state, delimiter, compensate=compensate)
			lines = _specKeptHeader(load, header, delimiter, errors) + preamble
			# Everything that is to be written is looked at before a copy of a damaged part is kept or the part is
			# touched, and a refused cell is named (its key and column), not the file.
			for line in preamble:
				_encodeText(line, 'utf-8', errors, 'a marker line')
			for cells in rows:
				_checkRow(cells, delimiter, 'utf-8', errors, header)
			lines += [_specFormatRecord(cells, delimiter) for cells in rows]
			if compensate:
				lines += _specPostamble(state, delimiter)
			if (info.lost or info.damaged) and backedUp != stamp:
				# Damaged data is not in what is written, or only unverified (spec §4.10, §16.6): keep the original
				# bytes, again whenever the part changed since the last copy.
				try:
					_specBackupPart(load.parts[0], reporter)
				except OSError as e:
					attempt.note('refused', None, 'scrub skipped: could not keep a copy of the damaged part ({}); nothing was written'.format(e))
					return taskDic, False
				backedUp = stamp
			done = _specRewriteInPlace(load.parts[0], _encodeText(''.join(line + '\n' for line in lines), 'utf-8', errors),
									   expect=stamp, memberSize=state.memberSize)
			if done is None:
				continue  # another writer committed after the read
			written = done[0]
			if verbose:
				__teePrintOrNot(f"Scrubbed {fileName}: {len(rows)} records, {'rewritten' if written else 'unchanged'}",teeLogger=teeLogger)
			# A read that stopped at damaged data was rewritten as far as it went, but it still failed (spec §4.10).
			return taskDic, None if attempt.has('stopped') else True
		attempt.note('changing', None, 'scrub skipped: the store kept changing while it was being compacted; nothing was written')
	finally:
		reporter.flush()
		if attempt is not None:
			attempt.flush()
	return taskDic, False


def getListView(tsvzDic,header = [],delimiter = ...):
	if header:
		if isinstance(header,str):
			header = header.split(get_delimiter(delimiter))
		elif not isinstance(header,list):
			try:
				header = list(header)
			except Exception:
				header = []
	if not tsvzDic:
		if not header:
			return []
		else:
			return [header]
	if not header:
		return list(tsvzDic.values())
	else:
		values = list(tsvzDic.values())
		if values[0] and values[0] == header:
			return values
		else:
			return [header] + values

class _Flag(object):
	"""threading.Event's is_set(), set(), clear() and wait(), with no lock that one thread holds while another waits
	for it: what a store's writer thread and the threads that hand it work signal each other with (5.13).

	A threading.Event holds its Condition's lock while Python code runs, and a KeyboardInterrupt can land where a
	``with`` statement's exit never runs (CPython bpo-29988): a Ctrl-C there left the lock held for ever, and the writer
	thread's next set() waited for it while the main thread waited for the writer (a program hung at exit after its
	first Ctrl-C, about 1 run in 250 of the 3.6 suite's Ctrl-C test). Here the flag is an attribute, and each waiter
	sleeps on a lock of its own that set() lets go of. A waiter puts its lock in before it looks at the flag, and set()
	sets the flag before it lets go of the locks it finds: a waiter either sees the flag or is woken. A Ctrl-C anywhere
	in a wait leaves its lock behind, which only ever costs a later set() a release; one inside set() leaves the waiters
	it did not reach to wake at their timeout, as threading.Event's set() does when it is cut off before notify_all().

	It relies on the GIL: set()'s copy of the waiters is one step against another thread's append or remove. A
	free-threaded build (which setup.py does not claim) could raise "deque mutated during iteration" there."""
	__slots__ = ('_on', '_waiters')

	def __init__(self):
		self._on = False
		self._waiters = deque()  # each waiting thread's lock, held until a set(); taken out by that thread

	def is_set(self):
		return self._on

	isSet = is_set  # (threading.Event's old name for it)

	def set(self):
		self._on = True
		for bell in list(self._waiters):
			try:
				bell.release()
			except RuntimeError:
				pass  # let go of by an earlier set(): its waiter has not taken it out yet

	def clear(self):
		self._on = False

	def wait(self, timeout=None):
		"""True once the flag is set, or False once ``timeout`` seconds pass first (None: no limit), as Event.wait()."""
		if self._on:
			return True
		bell = threading.Lock()
		bell.acquire()
		self._waiters.append(bell)
		try:
			return self._on or bell.acquire(True, -1 if timeout is None else max(timeout, 0)) or self._on
		finally:
			self._waiters.remove(bell)


class _SectionLock(_thread.RLock):
	"""A lock for sections that TSVZ's own code never nests, which a Ctrl-C cannot leave held for good: a TSVZed's _memLock
	(5.13).

	A KeyboardInterrupt can land just after a ``with`` statement takes its lock and before the block is set up (seen on
	3.14), or where the block's exit never runs (3.6's end of a block; CPython bpo-29988). A plain lock was then held for
	ever, the writer thread waited for it, and the exit for the writer: in 5.12 a Ctrl-C that ended a loop of sets hung
	the exit about 1 time in 14 on 3.14 and 1 in 250 on 3.6. This one is an RLock, which knows the thread that holds it.
	None of TSVZ's sections is entered from inside another (with a plain lock, that waited for ever): a thread that holds
	it as it enters one, or as it waits for the writer thread (letGo), holds it from a Ctrl-C, and lets go of it first.
	Another thread waits for such a hold until its holder next does either, or the exit does. A set, delete, pop or move
	that a Ctrl-C goes up through lets go too (``except BaseException: letGo(); raise``, for a Ctrl-C that skipped the
	exit of its ``with`` statement), so a program that catches the Ctrl-C and goes on holds nothing: 17 of 320 write loops
	ended by a caught Ctrl-C left the hold on 3.6, 2 of 320 on 3.14, before. Left are a second Ctrl-C landing inside that
	clause, and the sections the writer thread's work runs in the main thread (after close(), in a forked child).

	A signal handler that uses the store is not TSVZ's code: it runs inside the section it interrupted, and its own section
	(or its wait for the writer thread) lets go of the interrupted one's hold. The exit of the interrupted section then
	finds the lock not held, and ignores that (__exit__; a check of the hold before the release leaves a gap that a handler
	lands in). The rest of that section runs without the lock; at worst, as far as checked, a line is written twice (the last wins) or memory
	and the file differ until the next reload, and both need the writer thread to rewrite or reload in those microseconds.
	(Its exit is a Python method, about 0.1 microsecond a section dearer than the RLock's own on 3.6, 0.04 on 3.14.)"""
	__slots__ = ()

	def letGo(self):
		"""Let go of what a Ctrl-C left this thread holding; the caller is in no section."""
		while self._is_owned():
			self.release()

	def __enter__(self):
		if self._is_owned():
			self.letGo()
		return self.acquire()

	def __exit__(self, *exc):
		try:
			self.release()
		except RuntimeError:
			pass  # not held: a signal handler that used the store inside the section let go of it (letGo)


def _waited(flag, timeout):
	"""``flag.wait(timeout)`` (a _Flag): a thread's wait for the store's writer thread, which its callers make in steps
	of ``timeout``, 0.1 s: a Ctrl-C stops a lock's wait only on POSIX, and a tenth of a second is too short for a person
	to notice. (It is only ``flag.wait``, kept as the one place the tests stop a wait.)"""
	return flag.wait(timeout)


#: Every TSVZed and TSVZedLite not yet stopped, by id(): _stopLiveStores stops them at exit. Weak: a store
#: stopped (its writer thread ended) and dropped is freed.
_LIVE_STORES = weakref.WeakValueDictionary()


def _stopLiveStores():
	"""At exit, stop every store still running (_atExit: a TSVZed's last rewrite and appends), letting a write in
	progress finish: a first Ctrl-C meanwhile keeps waiting for it, and says so; a second abandons it."""
	interrupted = abandoned = False
	try:
		for store in reversed(list(_LIVE_STORES.values())):  # the last made first, as atexit runs its own
			try:
				store._atExit()
			except KeyboardInterrupt:
				if interrupted:
					raise
				interrupted = True
				eprint(f"TSVZ: the write to {store._fileName} is being finished; Ctrl-C again to abandon it")
				store._finish()
	except BaseException:
		abandoned = True
		for store in list(_LIVE_STORES.values()):
			store._letGo = True  # abandoned: no writer thread starts a write the interpreter's end would cut short
		raise
	finally:
		# (after the stores: what a last write of theirs reports is still printed; an abandon ends the program at once, so the
		# door shuts without waiting for a print under way)
		_closeStreamsToDaemons(0 if abandoned else None)


atexit.register(_stopLiveStores)


# create a tsv class that functions like a ordered dictionary but will update the file when modified
class _TSVZedLoad(OrderedDict):
	"""What ``TSVZed.reload()`` reads a legacy file into, apart from the live store (_readFrom).

	Its rows are shaped as ``TSVZed.__setitem__`` shapes them in memory only (keys and cells right-stripped,
	the column count and the defaults applied), with a column count and defaults of its own. ``dirty`` tells
	that the file holds a key twice (two values, or a row and its deletion).
	"""

	def __init__(self, owner, columns, defaults):
		super().__init__()
		self.owner = owner
		self.columns = columns
		self.defaults = defaults
		self.dirty = False

	def _change(self, key, value):
		"""``(key, row)``, the change a row read for ``key`` makes: its row as __setitem__ shapes it, None for a
		delete; None when it changes no key (an empty key, a row strict refuses, the defaults line: applied here)."""
		key = str(key).rstrip()
		if not key:
			return None
		shaped = self.owner._shapeRow(key, value, self.columns, self.defaults, load=True)
		if shaped is None:
			return None
		value, columns = shaped
		# (a read hands a lone key over only as a row, in a 1-column file; it deletes through __delitem__)
		self.columns = columns
		if key == DEFAULTS_INDICATOR_KEY:
			self.defaults[:] = value
			return None
		return key, value

	def __setitem__(self, key, value):
		change = self._change(key, value)
		if change is None:
			return
		key, value = change
		if value is None:
			OrderedDict.pop(self, key, None)
			return
		old = OrderedDict.get(self, key)
		if old is not None:
			if old == value:
				return
			self.dirty = True
		OrderedDict.__setitem__(self, key, value)

	def __contains__(self, key):
		key = str(key)
		return OrderedDict.__contains__(self, key) or OrderedDict.__contains__(self, key.rstrip())

	def __delitem__(self, key):
		key = str(key)
		OrderedDict.__delitem__(self, key if OrderedDict.__contains__(self, key) else key.rstrip())
		self.dirty = True  # the file holds the key twice: its row, and this deletion

	def replay(self, rows):
		"""Apply rows queued for the file, as reading them back will: a row whose values are all empty deletes."""
		for row in rows:
			key = row[0]
			if isinstance(row, _DropKey):
				OrderedDict.pop(self, key, None)
			elif key == DEFAULTS_INDICATOR_KEY:
				self.defaults[:] = row if any(row[1:]) else [DEFAULTS_INDICATOR_KEY]
			elif any(row[1:]) or (len(row) == 1 and self.columns == 1):
				self[key] = row
			else:
				OrderedDict.pop(self, key, None)


class _TSVZedDiff(_TSVZedLoad):
	"""What a compacting ``TSVZed`` rewrite reads its compacted lines into (_readLines): no rows, only whether
	each key the lines leave live reads as memory holds it (``flags[key]`` True or False, _readsAs) or is dead in them (None).
	Memory is looked up without _memLock (a read of one key): a row replaced meanwhile shows as a difference, and is
	written, which is harmless."""

	def __init__(self, owner, columns, defaults):
		super().__init__(owner, columns, defaults)
		self.flags = {}

	def __setitem__(self, key, value):
		change = self._change(key, value)
		if change is not None:
			key, row = change
			self.flags[key] = self._readsAs(row, dict.get(self.owner, key))

	def _readsAs(self, row, held):
		"""True when ``held``, memory's row, written again would read no otherwise than ``row``, as read: the same cells,
		less the empty ones at their end; past them, only what a read pads a row with (empty, or the defaults in effect
		here: memory read it at another column count or with other defaults)."""
		if row == held:
			return True
		if held is None:
			return False
		held = _trimmedCells(held)
		count = len(held)
		defaults = self.defaults
		return row[:count] == held and all(not cell or (count + i < len(defaults) and cell == defaults[count + i])
										   for i, cell in enumerate(row[count:]))

	def __contains__(self, key):
		return True  # (every deletion is noted)

	def __delitem__(self, key):
		self.flags[str(key).rstrip()] = None


class _TSVZedMerge(_TSVZedLoad):
	"""What ``TSVZed._applyAppended`` reads the rows another process appended into, apart from the live store: the
	last change of each key, to apply to memory after, as ``(row, readded)``: its row shaped as a read shapes it
	(None for a delete), and whether a delete came before that row. A read moves a key deleted and set again to
	the end, and keeps the place of one only set: the keys are in the order such a read leaves the ones it adds.
	"""

	def __setitem__(self, key, value):
		change = self._change(key, value)
		if change is not None:
			self._note(*change)

	def _note(self, key, row):
		last = OrderedDict.get(self, key)
		if row is None:
			OrderedDict.__setitem__(self, key, (None, False))
		elif last is not None and last[0] is None:  # set again after a delete: added again, at the end
			OrderedDict.__delitem__(self, key)
			OrderedDict.__setitem__(self, key, (row, True))
		else:
			OrderedDict.__setitem__(self, key, (row, last is not None and last[1]))

	def __contains__(self, key):
		return True  # a delete of a key memory holds: _processLine deletes only what it is told is there

	def __delitem__(self, key):
		self._note(str(key).rstrip(), None)


class TSVZed(OrderedDict):
	"""
	A thread-safe, file-backed ordered dictionary for managing TSV (Tab-Separated Values) files.
	TSVZed extends OrderedDict to provide automatic synchronization between an in-memory
	dictionary and a TSV file on disk. It supports concurrent file access, automatic
	persistence, and configurable sync strategies.
	Parameters
	----------
	fileName : str
		Path to the TSV file to be managed.
	teeLogger : object, optional
		Logger object with a teelog method for logging messages. If None, uses print.
	header : str, optional
		Column header line for the TSV file. Used for validation and file creation.
	createIfNotExist : bool, default=True
		If True, creates the file if it doesn't exist.
	verifyHeader : bool, default=True
		If True, verifies that the file header matches the provided header.
	rewrite_on_load : bool, default=False
		If True, rewrites (compacts) the file when loading, dirty or not (5.6). 5.5 turned the default off (3.39 to 5.4: on); call
		mapToFile() or hardMapToFile(), or scrub the file, to compact it. The three automatic rewrites
		(rewrite_on_load, rewrite_on_exit, rewrite_interval) are planned to go in a future version, for explicit calls.
	rewrite_on_exit : bool, default=False
		If True, rewrites the entire file when closing/exiting, dirty or not (5.6).
	rewrite_interval : float, default=0
		Minimum time interval (in seconds) between full file rewrites. 0 means do not rewrite.
	append_check_delay : float, default=0.01
		Time delay (in seconds) between checks of the append queue by the worker thread.
	monitor_external_changes : bool, default=True
		If True, monitors and detects external file modifications.
	verbose : bool, default=False
		If True, prints detailed operation logs.
	encoding : str, default='utf8'
		Character encoding for reading/writing the file.
	delimiter : str, optional
		Field delimiter character. Auto-detected from filename if not specified.
	defaults : list or str, optional
		Default values for columns when values are missing.
	strict : bool, default=False
		If True, enforces strict validation of column counts and raises errors on mismatch.
	correctColumnNum : int, default=-1
		Kept for compatibility, and no longer the store's column count (5.5): that is the header's, the
		defaults' (two cells or more) or the first keyed line's, whatever this says, and rows are not
		padded to it or cut to it (5.4 padded them to a count above the file's width in a loose store and
		refused them in a strict one; a count below it cut rows in memory at the open and in the file at a rewrite, or in a
		strict store emptied the file). Pad rows yourself, or give defaults with that many cells counting the '#_defaults_#' key
		(['#_defaults_#', '', '', '', ''] for 5) or a header that line 1 holds.
	errors : str, default='strict'
		What to do with text the encoding cannot hold, such as a lone surrogate from a non-UTF-8
		file name (os.fsdecode). 'strict' raises a ValueError (TSVZEncodeError) naming it, from the
		call that gave the text (setting an item) or from the constructor (a header or defaults),
		before anything is stored or written. 'replace' writes '?' for it, as 3.39 did. A rewrite
		that meets such text (a row changed in place, or U+FFFD under a legacy encoding) leaves the
		file as it is: mapToFile() and hardMapToFile() raise the error, the rewrites TSVZed makes
		itself report it once.
	Attributes
	----------
	version : str
		Version of the TSVZed implementation.
	dirty : bool
		True once a key is restated with another row or a live key is deleted, in the file or by this store, and
		after some failed writes. A new row does not set it.
		rewrite_interval writes only then. mapToFile(), hardMapToFile(), rewrite(force=True), rewrite_on_load and
		rewrite_on_exit always write (the last two not over damage no row went after: see _rewrite); nothing else
		rewrites the file, except to make a damaged or removed file again from memory and to compact a one-column file
		after a delete.
	deSynced : bool
		True if synchronization with the file has failed or external changes detected.
	memoryOnly : bool
		If True, changes are kept in memory only and not written to disk.
	appendQueue : deque
		Queue of lines waiting to be appended to the file.
	writeLock : threading.Lock
		Lock for ensuring thread-safe file operations.
	shutdownEvent : _Flag
		Set to stop the append worker thread: is_set(), set(), clear() and wait() as a threading.Event's (5.13: not a
		threading.Event, whose lock a Ctrl-C can leave held, see _Flag).
	appendThread : threading.Thread
		Background thread that handles asynchronous file appending.
	Methods
	-------
	load()
		Load or reload data from the TSV file.
	reload()
		Refresh data from the TSV file, discarding in-memory changes. Unsafe after a failed rewrite
		(see Notes): it replaces memory, the only good copy then, with the damaged file, and warns.
	rewrite(force=False, reloadInternalFromFile=None)
		Follow another process's change; force=True then rewrites the entire file with current in-memory data, always,
		as mapToFile() does (it returns False when it could not, and reports why, where mapToFile() raises a refusal).
	mapToFile()
		Synchronize in-memory data to the file using in-place updates.
	hardMapToFile()
		Completely rewrite the file from scratch with current data.
	clear()
		Clear all data from memory and optionally the file.
	clear_file()
		Clear the file, keeping only the header.
	commitAppendToFile()
		Write all queued append operations to the file.
	stopAppendThread()
		Stop the background append worker thread and perform final sync.
	setDefaults(defaults)
		Set default values for columns.
	getListView()
		Get a list representation of the data with headers.
	getResourceUsage(return_dict=False)
		Get current resource usage statistics.
	checkExternalChanges()
		Check if the file has been modified externally.
	close()
		Close the TSVZed object, stopping background threads and syncing data.
	Notes
	-----
	- The class uses a background thread to handle asynchronous file operations.
	- File locking is implemented for both POSIX and Windows systems.
	- Keys starting with '#' are treated as comments and not persisted to file.
	- The special key '#_defaults_#' is used to store column default values.
	- Supports compressed file formats through automatic detection.
	- Thread-safe for concurrent access from multiple threads.
	- A legacy file (.tsv, .csv, .nsv, .psv, compressed or not) is written by the store's background thread
	  only: rewrite(), mapToFile(), hardMapToFile(), commitAppendToFile(), clear(), reload() and close() hand
	  it the job and wait for it, so a KeyboardInterrupt stops the wait, never the write. At exit, a write in
	  progress is finished (a second Ctrl-C abandons it); a store with nothing to write is not waited for. A
	  .tsvz-family store's commitAppendToFile(), clear() and reload() are handed to it the same way (5.13; they
	  ran in the calling thread, whose Ctrl-C could leave writeLock held, and the writer thread waited for it).
	- Its reads of a legacy file (the load, reload() and the background read) hold the file's READ lock, so
	  another process's rewrite under the file lock is waited out, never met part way; its appends take the
	  append lock (which, on Linux, readers do not wait for) and its rewrites the rewrite lock (_lockFile).
	- Another process's change to a legacy file that leaves the bytes this store knows it holds where they
	  were (the same file, size and SHA-256) is read from there on and applied to memory; any other change
	  reloads the file, with the queued rows replayed on it.
	- A rewrite of a legacy file that fails part way (a full disk, an I/O error), or cannot make again a
	  removed file, leaves the file damaged and memory the only good copy. Until a rewrite gets through, the
	  file is not reloaded over memory (the background writer still appends the rows set meanwhile, merged
	  back as they are); the next rewrite makes it again from memory. Another process's change made
	  meanwhile is merged into memory first when it can have been only appended: the file is the same one
	  (device and inode) and still begins with exactly the bytes the failed rewrite left (a file left empty
	  must then begin with this store's header); otherwise memory is kept and that change is overwritten,
	  with a warning. reload() is unsafe then: it replaces memory with the damaged file, as asked, and warns.
	Examples
	--------
	>>> with TSVZed('data.tsv', header='id\tname\tvalue') as tsv:
	...     tsv['key1'] = ['key1', 'John', '100']
	...     tsv['key2'] = ['key2', 'Jane', '200']
	...     print(tsv['key1'])
	['key1', 'John', '100']
	>>> tsv = TSVZed('data.tsv', verbose=True, rewrite_on_exit=True)
	>>> tsv['key3'] = 'key3\tBob\t300'
	>>> tsv.close()
	"""
	def __teePrintOrNot(self,message,level = 'info'):
		def write():
			try:
				if self.teeLogger:
					self.teeLogger.teelog(message,level)
				else:
					print(message,flush=True)
			except Exception:
				print(message,flush=True)
		_say(write, message)

	def getResourceUsage(self,return_dict = False):
		return get_resource_usage(return_dict = return_dict)

	def __init__ (self,fileName,teeLogger = None,header = '',createIfNotExist = True,verifyHeader = True,rewrite_on_load = False,
				  rewrite_on_exit = False,rewrite_interval = 0, append_check_delay = 0.01,monitor_external_changes = True,
				  verbose = False,encoding = 'utf8',delimiter = ...,defaults = None,strict = False,correctColumnNum = -1,errors = 'strict'):
		super().__init__()
		self._constructed = False  # setDefaults() writes a #_defaults_# marker only once this is True
		self._spec = _isSpecPath(fileName)
		self.version = version
		self.strict = strict
		self.externalFileUpdateTime = getFileUpdateTimeNs(fileName)
		self.lastUpdateTime = self.externalFileUpdateTime
		self._fileName = fileName
		self.teeLogger = teeLogger
		self.errors = _checkErrors(errors)
		if self._spec:
			reporter = _Reporter(fileName, teeLogger)
			self.delimiter, encoding = _specOptions(fileName, delimiter, encoding, reporter)
			reporter.flush()
		else:
			self.delimiter = get_delimiter(delimiter,file_name=fileName)
		self.encoding = encoding
		self._mismatchSaid = [None]  # the line 1 whose header mismatch the whole reads last said (_legacyReadHeader)
		self.setDefaults(defaults)
		self._constructorDefaults = list(self.defaults)
		self.header = _formatHeader(header,verbose = verbose,teeLogger = self.teeLogger,delimiter=self.delimiter)
		_checkRow(self.header, self.delimiter, self.encoding, self.errors, what='header')  # before a thread or a file exists
		self.correctColumnNum = correctColumnNum
		# A whole read of a legacy file starts from the column count and defaults the program gave (the last
		# setDefaults()'s), never from those a previous read ended with.
		self._givenColumns = correctColumnNum
		self._givenDefaults = list(self.defaults)
		self.createIfNotExist = createIfNotExist
		self.verifyHeader = verifyHeader
		if rewrite_on_load is ...:
			# (5.5: off by default; 3.39 to 5.4 rewrote a legacy file on load. Spec stores are never rewritten so.)
			rewrite_on_load = False
		self.rewrite_on_load = rewrite_on_load
		self.rewrite_on_exit = rewrite_on_exit
		self.rewrite_interval = rewrite_interval
		self.monitor_external_changes = monitor_external_changes
		if not monitor_external_changes:
			self.__teePrintOrNot(f"Warning: External changes monitoring disabled for {self._fileName}. Will overwrite external changes.",'warning')
		self.verbose = verbose
		if append_check_delay < 0:
			append_check_delay = 0.00001
			self.__teePrintOrNot('append_check_delay cannot be less than 0, setting it to 0.00001','error')
		self.append_check_delay = append_check_delay
		self.appendQueue = deque()
		self.dirty = False
		self.deSynced = False
		self.memoryOnly = False
		self.writeLock = threading.Lock()
		self._memLock = _SectionLock()  # held while memory and appendQueue change together (a set, a delete, reload's swap)
		self._failures = {}  # legacy writes that fail, by kind: when each is tried again, see _failed()
		self._reported = {}  # and the failures of each kind reported since it last went through
		self.shutdownEvent = _Flag()
		if self._spec:
			for flag, given in (('rewrite_on_load', rewrite_on_load), ('rewrite_on_exit', rewrite_on_exit),
								('rewrite_interval', rewrite_interval)):
				if given:
					_warnOnce(self, flag, fileName,
							  f'{flag}={given!r} is ignored for .tsvz; behaviour undefined; use scrubTabularFile for archival compaction',
							  teeLogger)
			self._specState = _SpecState(self.defaults)
			self._parts = []
			self._activePath = fileName
			self._dirMtimeNs = _dirMtimeNs(fileName)
			self._writeFailing = False
			self._specLoaded = False
		self._requests = deque()  # the jobs _call hands the writer thread
		self._wake = _Flag()  # set when one is handed: the thread does not wait out its tick
		self._workerDone = _Flag()  # set as the writer thread ends (_workerRuns)
		self._workerPid = os.getpid()
		self.appendThread  = threading.Thread(target=self._appendWorker,daemon=True)
		self.appendThread.start()
		_LIVE_STORES[id(self)] = self  # before load(): what it writes is finished at exit, whatever ends the program
		try:
			self.load()
		except Exception:
			self._finish()  # it did not load: nothing of it is written at exit
			raise
		self._constructed = True

	def setDefaults(self,defaults):
		defaults = _normalizeDefaults(defaults, self.delimiter)
		_checkRow(defaults, self.delimiter, self.encoding, self.errors, what='defaults')
		if self._spec and self._constructed:
			# Spec rows are written sparse and read back under the file's
			# #_defaults_#, so new defaults are written as that marker.
			self._specSetMarker(DEFAULTS_INDICATOR_KEY, defaults)
			return
		if not self._constructed:  # (no lock exists yet)
			self._givenDefaults = list(defaults)
			self.defaults = defaults
			return
		try:  # (a Ctrl-C that skipped the with statement's exit leaves no hold behind: see _SectionLock)
			with self._memLock:  # (a read's swap sees either both of these or neither)
				self._givenDefaults = list(defaults)  # (the defaults a whole read starts from)
				self._readWhole = True  # memory's defaults now differ from the file's: another process's change is read whole
				self.defaults = defaults
		except BaseException:
			self._memLock.letGo()
			raise

	def load(self):
		self.reload()
		if self.rewrite_on_load and not self._spec:
			# It takes in a change another process made since the read. Over damage no row went after, only an explicit
			# call rewrites: the bytes past it hold rows no read reached.
			self.rewrite(force = not self._remake)
		return self

	def reload(self):
		if self._spec:
			return self._call(self._specReload)
		self._call(self._reload)
		self._accepted()  # memory is the file again: a refusal met later is reported again
		return self

	def _reload(self):
		"""reload() for a legacy file: memory becomes what the file holds (_readFrom), the file made first when it is
		missing and createIfNotExist. While a failed rewrite of this store's has left the file damaged, memory holds
		the only good copy: it is read all the same, as asked, with a warning."""
		with self.writeLock:
			if self._damaged:
				self.__teePrintOrNot(f"Warning: reload() replaces memory with {self._fileName}, which is damaged and not yet "
									 f"made again from memory (a rewrite of this object's failed: a full disk, an I/O error, "
									 f"the file removed, damage another writer left); memory held the only good copy, and "
									 f"what it held beyond the file is dropped. Without reload(), the next rewrite makes the "
									 f"file again from memory",'warning')
			if self.verbose:
				self.__teePrintOrNot(f"Loading {self._fileName}")
			if not os.path.isfile(self._fileName) and not _verifyFileExistence(
					self._fileName, createIfNotExist=self.createIfNotExist, teeLogger=self.teeLogger, header=self.header,
					encoding=self.encoding, strict=self.strict, delimiter=self.delimiter, errors=self.errors):
				self._readFrom(None, True)  # missing and not made: empty, until an append makes it
			else:
				with _readLock(self._fileName), open(self._fileName, 'rb') as f:  # never held while _readLocked waits
					cut = self._readFrom(f, False)
				if cut:
					self._readLocked(lambda f: self._readFrom(f, True))
		if self.verbose:
			self.__teePrintOrNot(f"Loaded {len(self)} records from {self._fileName}")
			self.__teePrintOrNot(f"correctColumnNum: {self.correctColumnNum}")
			self.__teePrintOrNot(f"TSVZed({self._fileName}) loaded")
		return True

	def _specReload(self):
		"""Load a spec store into memory (design §4); rows bypass __setitem__ so nothing is re-normalised.

		What is still queued is replayed on what was read, as a read once it is committed sees it: under
		writeLock no commit of this object's runs meanwhile, and under _memLock no set is half done.
		"""
		if self.verbose:
			self.__teePrintOrNot(f"Loading {self._fileName}")
		reporter = _Reporter(self._fileName, self.teeLogger)
		load = None
		with self.writeLock:
			dirMtime = _dirMtimeNs(self._fileName)  # before the read: a part created during it is a change
			try:
				if _specEnsureStore(self._fileName, self.createIfNotExist, self.header, self._constructorDefaults,
									self.strict, self.teeLogger, self.delimiter, self.errors):
					load = _specLoad(self._fileName, self.delimiter, header=self.header, verifyHeader=self.verifyHeader,
									 strict=self.strict, defaults=self._constructorDefaults, taskDic=OrderedDict(),
									 reporter=reporter, teeLogger=self.teeLogger, verbose=self.verbose)
				unreadable = reporter.incomplete()
				torn = reporter.has('torn')
			finally:
				reporter.flush()
			with self._memLock:
				width = -1
				if load is not None:
					_specReplayQueued(load, self.appendQueue, self.delimiter)
				self._replaceRows(load.data if load is not None else {})  # a live key never goes missing meanwhile
				if load is not None:
					self._specState = load.state
					self.defaults = list(load.state.defaults)
					self._parts, self._activePath = load.parts, load.active
					width = load.correctColumnNum
				if self.header and any(self.header) and self.verifyHeader:
					self.correctColumnNum = len(self.header)
				else:
					self.correctColumnNum = width
				if (load is not None and not load.state.sawDefaults and any(self._constructorDefaults[1:])
						and not self.memoryOnly and not unreadable):  # the store's own defaults may be in the unread part
					# Design §5.3: persist constructor defaults by appending, as 3.39 did by rewriting.
					self.appendQueue.append(_specFormatRecord(self._constructorDefaults, self.delimiter, marker=True))
					self._specState.sawDefaults = True
			# The active part as it was just before it was read (else as it is now): whatever is appended
			# after that, even before the read ends, is a change the next check sees.
			info = load.infos[-1] if load is not None and load.infos else None
			self._specNoteSeen(info.stamp if info is not None and info.stamp is not None else _specFileStamp(self._activePath))
		if self.verbose:
			self.__teePrintOrNot(f"Loaded {len(self)} records from {self._fileName}")
		self.lastUpdateTime = self.externalFileUpdateTime
		# The store kept changing under every read: no part watched need change again for the next
		# check to read it again (spec §19.8).
		self._specTorn = torn
		self._specTornAt = 0.0
		self._specTornWait = 0.0  # a load that worked starts the waits over
		self._dirMtimeNs = dirMtime
		self._specLoaded = True
		return self

	def __missing__(self, key):
		# Spec §14.5: while #_return_defaults_when_missing_# is true a missing key
		# reads as the active defaults. Only t[key] reaches this; get / in /
		# setdefault / pop keep dict semantics.
		if self._spec and self._specState.returnDefaults:
			row = [key] + list(self.defaults[1:])
			if self.correctColumnNum > len(row):
				row += [''] * (self.correctColumnNum - len(row))
			return row
		raise KeyError(key)

	@property
	def dialect(self):
		"""'tsvz' for tsvz-spec-v1 files (.tsvz/.csvz/.nsvz/.psvz), 'tsv' for 3.39-format files."""
		return 'tsvz' if self._spec else 'tsv'

	def _specNoteSeen(self, stamp):
		"""Note ``stamp`` (``_specStamp``, or None) as the active part's that memory and the queue account for."""
		self._specSeen = stamp
		self.externalFileUpdateTime = stamp[2] if stamp else 0

	def _specSync(self, reloadInternalFromFile=None):
		"""The part of 3.39's rewrite() that .tsvz keeps: reload after an external change."""
		if not self.deSynced:
			return False
		if reloadInternalFromFile is None:
			reloadInternalFromFile = self.monitor_external_changes
		if reloadInternalFromFile:
			try:
				self.commitAppendToFile()
				if self.appendQueue:
					return False  # writes are failing: keep the queue in memory, retry next tick
				self.reload()
			except Exception as e:
				self.__teePrintOrNot(f"Failed to reload {self._fileName} after an external change: {e}; keeping the in-memory state",'error')
				# Mark the change as seen so the failure is reported once, not every tick. A torn view is
				# read again after a while (1 s, doubling to a minute), not at every tick.
				if self._specTorn:
					self._specTornWait = min(max(1.0, self._specTornWait * 2), 60.0)
					self._specTornAt = _specClock() + self._specTornWait
				self._specNoteSeen(_specFileStamp(self._activePath))
				self._dirMtimeNs = _dirMtimeNs(self._fileName)
				self._parts = _storeParts(self._fileName)[0]
				self.deSynced = False
				return False
		self.deSynced = False
		return True

	def _specExternalChanged(self):
		"""True when another writer changed the active part or the set of parts, or the last load was torn."""
		if self._specTorn and _specClock() >= self._specTornAt:
			return True
		if _specFileStamp(self._activePath) != self._specSeen:
			return True
		dirMtime = _dirMtimeNs(self._fileName)
		if dirMtime != self._dirMtimeNs:
			self._dirMtimeNs = dirMtime
			if _storeParts(self._fileName)[0] != self._parts:
				return True
		return False

	def _checkWritable(self, cells, what='row'):
		"""Raise ``TSVZEncodeError`` now when ``cells`` could not be written: a row is refused before memory, the queue or the file change."""
		if not self.memoryOnly:
			_checkRow(cells, self.delimiter, self.encoding, self.errors, self.header, what)

	def _checkDelete(self, key):
		"""Raise ``TSVZEncodeError`` now when the tombstone of ``key`` could not be written, before memory changes.

		A key read from a file under a legacy ``encoding=`` can hold U+FFFD (what reading puts for a byte it
		could not decode), which that encoding may lack: the store could not name that key again, so the
		delete is refused, not written as the tombstone of another key.
		"""
		if key == DEFAULTS_INDICATOR_KEY or not key.startswith('#'):
			self._checkWritable([key])

	def __setitem__(self,key,value):
		if self._spec:
			return self._specSetItem(key, value)
		key = str(key).rstrip()
		if not key:
			self.__teePrintOrNot('Key cannot be empty','error')
			return
		shaped = self._shapeRow(key, value, self.correctColumnNum, self.defaults)
		if shaped is None:
			return
		value, newColumnNum = shaped
		if key == DEFAULTS_INDICATOR_KEY and len(value) > 1 and any(value[1:]) and len(value) < len(self.defaults):
			# R4, as a reader takes this line: a cell it lacks is the one before it
			value = value + self.defaults[len(value):]
		if self.correctColumnNum != 1 and len(value) == 1:
			# a lone key (no following values) means delete the key, per the
			# TSVZ spec. del is a no-op when the key is absent (tolerated).
			del self[key]
			return
		if (len(value) > 1 and not any(value[1:]) and key != DEFAULTS_INDICATOR_KEY
				and not key.startswith('#') and not self.memoryOnly):
			# L2: on disk this row is a delete; make memory agree with the file.
			_warnOnce(self, 'empty-row', self._fileName,
					  f'in .tsv files a row whose values are all empty is a delete; deleted {key!r} (use .tsvz to store empty rows)',
					  self.teeLogger)
			del self[key]
			return
		if key == DEFAULTS_INDICATOR_KEY or not key.startswith('#'):
			# What is written is what is checked: the row as cut to the column count and filled with the defaults,
			# and before memory, the queue or the column count change.
			self._checkWritable(value, 'defaults' if key == DEFAULTS_INDICATOR_KEY else 'row')
		if key == DEFAULTS_INDICATOR_KEY:
			try:  # (a Ctrl-C that skipped the with statement's exit leaves no hold behind: see _SectionLock)
				with self._memLock:  # with memory and the queue: a reload sets the column count under it too
					self.correctColumnNum = newColumnNum
					self.defaults = value
					if self.verbose:
						self.__teePrintOrNot(f"Defaults set to {value}")
					if not self.memoryOnly:
						self.appendQueue.append(value)
						self.lastUpdateTime = get_time_ns()
						if self.verbose:
							self.__teePrintOrNot(f"Appending Defaults {key} to the appendQueue")
			except BaseException:
				self._memLock.letGo()
				raise
			return
		if self.verbose:
			self.__teePrintOrNot(f"Setting {key} to {value}")
		try:  # (a Ctrl-C that skipped the with statement's exit leaves no hold behind: see _SectionLock)
			with self._memLock:  # memory and the queue change together: see reload()
				self.correctColumnNum = newColumnNum
				if key in self:
					if self[key] == value:
						if self.verbose:
							self.__teePrintOrNot(f"Key {key} already exists with the same value")
						return
					self.dirty = True
				# update the dictionary,
				super().__setitem__(key,value)
				if self.memoryOnly:
					if self.verbose:
						self.__teePrintOrNot(f"Key {key} updated in memory only")
					return
				elif key.startswith('#'):
					if self.verbose:
						self.__teePrintOrNot(f"Key {key} updated in memory only as it starts with #")
					return
				if self.verbose:
					self.__teePrintOrNot(f"Appending {key} to the appendQueue")
				self.appendQueue.append(value)
				self.lastUpdateTime = get_time_ns()
		except BaseException:
			self._memLock.letGo()
			raise
		# if not self.appendThread.is_alive():
		#     self.commitAppendToFile()
		# else:
		#     self.appendEvent.set()

	def _shapeRow(self, key, value, columns, defaults, load=False):
		"""The row ``__setitem__`` keeps for ``key`` (stripped, not empty) and the column count after it; None when strict refuses it.

		A row of the key alone, while ``columns`` is not 1, is a delete; it is returned as it is. What reload()
		reads into shapes its rows with ``load=True``, with its own column count and defaults: a row a read hands over,
		which the read fitted (padded, never cut) and checked, so it is only right-stripped; a cell the strip empties
		(whitespace only) takes the default for it, as 5.4 filled it, and a cell the read left empty stays so.
		"""
		if isinstance(value,str):
			value = value.split(self.delimiter)
		# sanitize the value
		emptied = [bool(s) and not str(s).rstrip() for s in value] if load else None
		value = [str(s).rstrip() if s else '' for s in value]
		# the first field in value should be the key
		# add it if it is not there
		if not value or value[0] != key:
			value = [key]+value
			if load:
				emptied.insert(0, False)
		if load:
			if defaults and len(defaults) > 1:
				for i in range(1, min(len(value), len(defaults))):
					if emptied[i] and defaults[i]:
						value[i] = defaults[i]
			return value, (columns if columns > 0 else len(value))
		# (a program whose header is one column, on a file another program widened, sets a lone key for a row: padded, it is
		# a row of no value, which the caller warns of and deletes, rather than a delete that says nothing)
		if columns != 1 and len(value) == 1 and not (self._followedWidening and len(self.header) == 1):
			return value, columns
		# verify the value has the correct number of columns
		if columns > 0:
			if len(value) > columns:
				value = _fitRow(value, columns)  # (its empty cells past the column count go; never a non-empty one)
			if len(value) != columns:
				# (5.5: strict refuses only a narrower row; a wider one is kept. A store that followed another program's
				# widening of its header is on the old header: its rows are narrower than the file's, and are padded)
				if self.strict and len(value) < columns and not self._followedWidening:
					self.__teePrintOrNot(f"Value {value} does not have the correct number of columns: {columns}. Refuse adding key...",'error')
					return None
				elif self.verbose:
					self.__teePrintOrNot(f"Value {value} does not have the correct number of columns: {columns}, correcting...",'warning')
				value = _fitRow(value, columns)  # (padded; a wider row is written whole)
		else:
			columns = len(value)  # taken by __setitem__ once the row is known to be writable
		if self._appendWidth > len(value) and any(value[1:]):
			value = _fitRow(value, self._appendWidth)  # (as wide as line 1 counts its cells: _legacyAppendWidth)
		if defaults and len(defaults) > 1:
			for i in range(1,len(value)):
				if not value[i] and i < len(defaults) and defaults[i]:
					value[i] = defaults[i]
					if self.verbose:
						self.__teePrintOrNot(f"    Replacing empty value at {i} with default: {defaults[i]}")
		return value, columns

	def _nameKey(self, key):
		"""The key a requested ``key`` names: the live key equal to it, else ``key`` right-stripped (spec §20.2.7).

		The fallback strips every trailing whitespace whatever the stripping
		marker says, as ``__setitem__`` strips every key it writes.
		"""
		key = str(key)
		return key if OrderedDict.__contains__(self, key) else key.rstrip()

	def __getitem__(self, key):
		key = str(key)
		row = dict.get(self, key, _NO_ROW)  # one probe for the usual hit
		return row if row is not _NO_ROW else OrderedDict.__getitem__(self, key.rstrip())

	def __contains__(self, key):
		# OrderedDict.setdefault asks this before it reads or writes, so it names keys as t[key] does.
		key = str(key)
		return dict.__contains__(self, key) or dict.__contains__(self, key.rstrip())

	def get(self, key, default=None):
		key = str(key)
		row = dict.get(self, key, _NO_ROW)
		if row is _NO_ROW:
			row = dict.get(self, key.rstrip(), _NO_ROW)
		return default if row is _NO_ROW else row


	def __delitem__(self,key):
		if self._spec:
			return self._specDelItem(key)
		key = self._nameKey(key)
		if key == DEFAULTS_INDICATOR_KEY:
			try:  # (a Ctrl-C that skipped the with statement's exit leaves no hold behind: see _SectionLock)
				with self._memLock:
					self.defaults = [DEFAULTS_INDICATOR_KEY]
					if self.verbose:
						self.__teePrintOrNot("Defaults cleared")
					if not self.memoryOnly:
						self.__appendEmptyLine(key)
						if self.verbose:
							self.__teePrintOrNot(f"Appending empty default line {key}")
			except BaseException:
				self._memLock.letGo()
				raise
			return
		# delete the key from the dictionary and update the file
		try:  # (a Ctrl-C that skipped the with statement's exit leaves no hold behind: see _SectionLock)
			with self._memLock:  # memory and the queue change together: see reload()
				if key not in self:
					if self.verbose:
						self.__teePrintOrNot(f"Key {key} not found")
					return
				self._checkDelete(key)
				row = dict.get(self, key)
				super().__delitem__(key)
				if self.memoryOnly or key.startswith('#'):
					if self.verbose:
						self.__teePrintOrNot(f"Key {key} deleted in memory")
					return
				self._deleteLine(key, row)
				if self.verbose:
					self.__teePrintOrNot(f"Appending empty line {key}")
				self.lastUpdateTime = get_time_ns()
		except BaseException:
			self._memLock.letGo()
			raise
		
	def _oneColumn(self, row):
		"""True when a deletion line of the legacy row ``row`` would be the key alone and read as a row: the store has one
		column, or none yet while memory holds a one-cell row. The caller holds _memLock."""
		return self.correctColumnNum == 1 or (self.correctColumnNum == -1 and (
			len(row) == 1 or any(len(value) == 1 for value in OrderedDict.values(self))))

	def _deleteLine(self, key, row):
		"""Queue the deletion of ``key`` (its row was ``row``), the caller holding _memLock: a deletion line or, in a
		legacy store where that would read as a row, a drop item (_DropKey)."""
		if not self._spec and self._oneColumn(row):
			self.dirty = True
			self.appendQueue.append(_DropKey([key]))
			return self
		return self.__appendEmptyLine(key)

	def __appendEmptyLine(self,key):
		# Append a tombstone line: the key followed by empty values. On read a
		# row whose values are all empty deletes the key (see _processLine).
		# Fillers MUST be empty strings ('') -- using the delimiter character
		# here would be sanitized to '<sep>' and read back as a non-empty value,
		# resurrecting the deleted key. The key has already been removed from
		# the mapping by the caller, so self[key] must not be accessed.
		self.dirty = True
		if self._spec:
			# Spec §9.2: a tombstone is the lone key, never padded. One queue item, so it is committed whole.
			self.appendQueue.append(_specTombstoneLines(key, self.delimiter, self._specState.strip))
			return self
		if self.correctColumnNum > 1:
			emptyLine = [key]+['']*(self.correctColumnNum-1)  # (not padded to _appendWidth: only a row with a value is)
		else:
			emptyLine = [key]
		if self.verbose:
			self.__teePrintOrNot(f"Appending {emptyLine} to the appendQueue")
		self.appendQueue.append(emptyLine)
		return self

	def _specSetItem(self, key, value):
		"""__setitem__ for .tsvz (design §5.2): write the given cells, keep the padded row in memory."""
		key, value = _specCells(key, value, self.delimiter)
		if not key:
			self.__teePrintOrNot('Key cannot be empty','error')
			return
		if _MARKER_RE.match(key):
			self._specSetMarker(key, value)
			return
		if len(value) == 1:
			del self[key]
			return
		self._checkWritable(value)
		try:  # (a Ctrl-C that skipped the with statement's exit leaves no hold behind: see _SectionLock)
			with self._memLock:  # memory and the queue change together: see _specReload
				if self.strict and self.correctColumnNum > 0 and len(value) != self.correctColumnNum:
					self.__teePrintOrNot(f"Value {value} does not have the correct number of columns: {self.correctColumnNum}. Refuse adding key...",'error')
					return
				_specFill(value, self.defaults)
				record = _specFormatRecord(value, self.delimiter)
				self.correctColumnNum = _specPadRow(value, self._specState, self.correctColumnNum if self.correctColumnNum > 0 else -1)
				if key in self:
					if OrderedDict.__getitem__(self, key) == value:
						return
					self.dirty = True
				OrderedDict.__setitem__(self, key, value)
				if self.memoryOnly:
					return
				self.appendQueue.append(record)
				self.lastUpdateTime = get_time_ns()
		except BaseException:
			self._memLock.letGo()
			raise

	def _specSetMarker(self, key, value):
		"""Write a reserved-key marker (design §5.2 e); official markers also update the reader state."""
		self._checkWritable(value, 'defaults' if key == DEFAULTS_INDICATOR_KEY else 'row')
		keyLower = key.lower()
		try:  # (a Ctrl-C that skipped the with statement's exit leaves no hold behind: see _SectionLock)
			with self._memLock:  # the reader state and the queue change together: see _specReload
				if keyLower in _SPEC_MARKER_KEYS:
					reporter = _Reporter(self._fileName, self.teeLogger)
					applied = _specApplyMarker(self._specState, keyLower, value[1:], reporter, None)
					reporter.flush()
					if not applied:
						return
					self.defaults = list(self._specState.defaults)
				if self.verbose:
					self.__teePrintOrNot(f"Marker {key} set to {value[1:]}")
				if self.memoryOnly:
					return
				self.appendQueue.append(_specFormatRecord(value, self.delimiter, marker=True))
				self.lastUpdateTime = get_time_ns()
		except BaseException:
			self._memLock.letGo()
			raise

	def _specDelItem(self, key):
		key = self._nameKey(key)
		if _MARKER_RE.match(key):
			# A lone marker key resets that marker (spec §12.1).
			self._specSetMarker(key, [key])
			return
		try:  # (a Ctrl-C that skipped the with statement's exit leaves no hold behind: see _SectionLock)
			with self._memLock:  # memory and the queue change together: see _specReload
				if key not in self:
					if self.verbose:
						self.__teePrintOrNot(f"Key {key} not found")
					return
				self._checkDelete(key)
				OrderedDict.__delitem__(self, key)
				if self.memoryOnly:
					return
				self.__appendEmptyLine(key)
				self.lastUpdateTime = get_time_ns()
		except BaseException:
			self._memLock.letGo()
			raise

	def _specCommit(self):
		"""Append the queued records to the active part (design §5.4).

		Records stay queued until the write succeeds; a failure is reported once
		and retried on the next tick. Nothing raised here may reach the worker.
		"""
		if not self._startWrite():  # (the exit let go of the store: no write starts that the interpreter's end could cut)
			return self
		try:
			with self.writeLock:
				return self._specCommitLocked()
		finally:
			self._endWrite()

	def _specCommitLocked(self):
		# The caller holds self.writeLock (a non-reentrant lock).
		if not self.appendQueue:
			return self
		if self.memoryOnly:
			self.appendQueue.clear()
			return self
		items = []
		while self.appendQueue:
			items.append(self.appendQueue.popleft())
		reporter = _Reporter(self._fileName, self.teeLogger)
		try:
			payload = _encodeText(''.join(item + '\n' for item in items), 'utf-8', self.errors)  # checked when set
			# The exit abandoned the store while this waited for the lock: nothing is written (_unattended).
			written = _specAppendPayload(self._activePath, payload, reporter, fsync=True,
										 memberSize=self._specState.memberSize, proceed=lambda: not self._unattended())
			if written is None:
				self.appendQueue.extendleft(reversed(items))
				reporter.flush()
				return self
			_, before, after = written
			if before == self._specSeen:
				self._specNoteSeen(after)  # else another writer changed the part first: the next check reloads
			if self._writeFailing:
				self._writeFailing = False
				_warn(f'TSVZ warning: {self._fileName}: writes recovered', self.teeLogger)
		except Exception as e:
			self.appendQueue.extendleft(reversed(items))
			if not self._writeFailing:
				self._writeFailing = True
				self.__teePrintOrNot(f"Failed to write at commitAppendToFile to {self._activePath}: {e}; will retry",'error')
				import traceback
				self.__teePrintOrNot(traceback.format_exc(),'error')
		reporter.flush()
		return self

	def _specClearFile(self):
		"""clear_file for .tsvz (design §5.8): commit queued markers, then clear in place."""
		try:
			with self.writeLock:
				self._specCommitLocked()
				if self.appendQueue:
					self.__teePrintOrNot(f"Failed to write at clear_file() to {self._fileName}: queued records could not be committed; not clearing",'error')
					self.deSynced = True
					return self
				cleared, seen = _specClear(self._fileName, teeLogger=self.teeLogger, header=self.header,
										   verbose=self.verbose, delimiter=self.delimiter, errors=self.errors)
				if seen is not None:
					self._specNoteSeen(seen)  # the part as the clear left it: an append after that is a change
			self.dirty = False
			# A skipped clear (reported) left the store as it was: reload it into memory.
			self.deSynced = not cleared
		except Exception as e:
			self.deSynced = True
			self.__teePrintOrNot(f"Failed to write at clear_file() to {self._fileName}: {e}",'error')
			import traceback
			self.__teePrintOrNot(traceback.format_exc(),'error')
		return self

	def getListView(self):
		return getListView(self,header=self.header,delimiter=self.delimiter)

	def clear(self):
		# clear the dictionary and update the file
		if self.verbose:
			self.__teePrintOrNot(f"Clearing {self._fileName}")
		if self.memoryOnly or self._spec:
			super().clear()
			if self.memoryOnly:
				return self
			self.clear_file()
		else:
			self._call(self._clearFile, True)
		self._accepted()  # nothing is left to refuse: a later refusal is reported again
		self.lastUpdateTime = self.externalFileUpdateTime
		return self

	def clear_file(self):
		if self._spec:
			return self._call(self._specClearFile)
		return self._call(self._clearFile, False)

	def _clearFile(self, dropRows):
		"""clear_file() for a legacy file; ``dropRows`` (clear()) also empties memory and drops the queued rows, once the
		file is open and locked (when it cannot be, nothing changed), under _memLock: no set is half done, and no row
		set before it is kept or appended after the emptied file. Queued defaults lines are kept: the defaults stay."""
		header = self._headerLine(None, clearing=True)

		def emptied():
			if dropRows:
				with self._memLock:
					OrderedDict.clear(self)
					kept = [row for row in self.appendQueue if row and row[0] == DEFAULTS_INDICATOR_KEY]
					self.appendQueue.clear()
					self.appendQueue.extend(kept)
			return header, [], ()

		try:
			if not self._writeFile(sync=None, snapshot=emptied):
				return self  # reported; tried again later
			if self.verbose:
				self.__teePrintOrNot(f"File {self._fileName} cleared, size: {os.path.getsize(self._fileName)}")
			self._worked('rewrite')
		except Exception as e:
			self._failed('rewrite', f"Failed to write at clear_file() to {self._fileName}: {e}")
			self.deSynced = True
		return self
	
	def __enter__(self):
		return self
	
	def close(self):
		self.stopAppendThread()
		return self

	def __exit__(self,exc_type,exc_value,traceback):
		return self.close()
	
	def __repr__(self):
		return f"""TSVZed(
file_name:{self._fileName}
teeLogger:{self.teeLogger}
header:{self.header}
correctColumnNum:{self.correctColumnNum}
createIfNotExist:{self.createIfNotExist}
verifyHeader:{self.verifyHeader}
rewrite_on_load:{self.rewrite_on_load}
rewrite_on_exit:{self.rewrite_on_exit}
rewrite_interval:{self.rewrite_interval}
append_check_delay:{self.append_check_delay}
appendQueueLength:{len(self.appendQueue)}
appendThreadAlive:{self._workerRuns()}
dirty:{self.dirty}
deSynced:{self.deSynced}
memoryOnly:{self.memoryOnly}
{dict(self)})"""
	
	def __str__(self):
		return f"TSVZed({self._fileName},{dict(self)})"

	def __del__(self):
		if 'appendThread' not in self.__dict__:
			return  # the constructor refused its arguments before it started anything
		return self.close()

	def popitem(self, last=True):
		# Not OrderedDict.popitem: before Python 3.11 it goes through self[key] and del self[key], which strip the key.
		try:  # (a Ctrl-C that skipped the with statement's exit leaves no hold behind: see _SectionLock)
			with self._memLock:  # memory and the queue change together: see reload()
				if not OrderedDict.__len__(self):
					raise KeyError('dictionary is empty')
				key = next(OrderedDict.__reversed__(self)) if last else next(OrderedDict.__iter__(self))
				self._checkDelete(key)
				value = OrderedDict.__getitem__(self, key)
				OrderedDict.__delitem__(self, key)
				if not self.memoryOnly and (self._spec or not key.startswith('#')):  # a legacy '#' key is memory only
					self._deleteLine(key, value)
				self.lastUpdateTime = get_time_ns()
		except BaseException:
			self._memLock.letGo()
			raise
		return key, value
	
	__marker = object()

	def pop(self, key, default=__marker):
		'''od.pop(k[,d]) -> v, remove specified key and return the corresponding
		value.  If key is not found, d is returned if given, otherwise KeyError
		is raised.

		'''
		key = self._nameKey(key)
		try:  # (a Ctrl-C that skipped the with statement's exit leaves no hold behind: see _SectionLock)
			with self._memLock:  # memory and the queue change together: see reload()
				if not OrderedDict.__contains__(self, key):
					if default is self.__marker:
						raise KeyError(key)
					return default
				# Not OrderedDict.pop: before Python 3.11 it goes through del self[key], which writes a tombstone too.
				self._checkDelete(key)
				value = OrderedDict.__getitem__(self, key)
				OrderedDict.__delitem__(self, key)
				if not self.memoryOnly and (self._spec or not key.startswith('#')):  # a legacy '#' key is memory only
					self._deleteLine(key, value)
				self.lastUpdateTime = get_time_ns()
		except BaseException:
			self._memLock.letGo()
			raise
		return value
	
	def move_to_end(self, key, last=True):
		'''Move an existing element to the end (or beginning if last is false). Raise KeyError if the element does not
		exist. In a legacy file the move is appended like any other change: the key's deletion, then its row, which is
		where a read puts it. Moving a key to the beginning moves every other key to the end, so it appends two lines
		per key; a rewrite or a scrub takes them out again.
		'''
		key = self._nameKey(key)
		if self._spec:
			super().move_to_end(key, last)
			_warnOnce(self, 'move_to_end', self._fileName,
					  'move_to_end only reorders memory for .tsvz; the file keeps first-appearance order',
					  self.teeLogger)
			return self
		if (not self.memoryOnly and self.monitor_external_changes and not self._failing('sync')
				and (self.deSynced or self._externalChange())):
			# The move queues rows memory holds again, so memory must be the file first: another process's change since
			# the last tick would be undone by those rows. What the writer thread's tick does: follow the change, and
			# rewrite only when one is due. (A stat, so a loop of moves stays cheap.)
			self.checkExternalChanges()
			self._call(self._rewrite, False, None, True)
		try:  # (a Ctrl-C that skipped the with statement's exit leaves no hold behind: see _SectionLock)
			with self._memLock:
				if not dict.__contains__(self, key):
					raise KeyError(key)
				moved = [(k, row) for k, row in OrderedDict.items(self) if k != key] if not last else [(key, dict.get(self, key))]
				if not self.memoryOnly:  # refused before memory or the queue change, as a set or a delete is
					for k, row in moved:
						if not k.startswith('#'):
							self._checkDelete(k)
							self._checkWritable(row)
				OrderedDict.move_to_end(self, key, last)
				if not self.memoryOnly:
					for k, row in moved:
						self._queueMove(k, row)
					self.lastUpdateTime = get_time_ns()
		except BaseException:
			self._memLock.letGo()
			raise
		return self

	def _queueMove(self, key, row):
		"""Queue what moves ``key`` to the end of the legacy file for every reader, the caller holding _memLock: its
		deletion, then its row. A row with an empty cell the defaults in effect would fill (one read before them) goes
		between a reset and the same defaults again. A '#' key is memory only, and a row with no value (a whitespace-only
		cell read, right-stripped) is not a line the format can write: their lines stay where they are."""
		one = self._oneColumn(row)
		if key.startswith('#') or not (one or any(row[1:])):
			return
		self._deleteLine(key, row)
		defaults = self.defaults
		if not one and any(i < len(defaults) and defaults[i] and not row[i] for i in range(1, len(row))):
			self.appendQueue.extend(([DEFAULTS_INDICATOR_KEY], row, _fitRow(defaults, self.correctColumnNum)))
		else:
			self.appendQueue.append(row)

	def __sizeof__(self):
		sizeof = sys.getsizeof
		size = sizeof(super()) + sizeof(True) * 12  # for the booleans / integers
		size += sizeof(self.externalFileUpdateTime)
		size += sizeof(self.lastUpdateTime)
		size += sizeof(self._fileName)
		size += sizeof(self.teeLogger)
		size += sizeof(self.delimiter)
		size += sizeof(self.defaults)
		size += sizeof(self.header)
		size += sizeof(self.appendQueue)
		size += sizeof(self.encoding)
		size += sizeof(self.writeLock)
		size += sizeof(self.shutdownEvent)
		size += sizeof(self.appendThread)
		size += super().__sizeof__()
		return size

	@classmethod
	def fromkeys(cls, iterable, value=None,fileName = None,teeLogger = None,header = '',createIfNotExist = True,verifyHeader = True,rewrite_on_load = False,rewrite_on_exit = False,rewrite_interval = 0, append_check_delay = 0.01,verbose = False,errors = 'strict'):
		'''Create a new ordered dictionary with keys from iterable and values set to value.
		'''
		self = cls(fileName,teeLogger = teeLogger,header = header,createIfNotExist = createIfNotExist,verifyHeader = verifyHeader,
				   rewrite_on_load = rewrite_on_load,rewrite_on_exit = rewrite_on_exit,rewrite_interval = rewrite_interval,
				   append_check_delay = append_check_delay,verbose = verbose,errors = errors)
		for key in iterable:
			self[key] = value
		return self


	def rewrite(self,force = False,reloadInternalFromFile = None):
		if self._spec:
			_warnOnce(self, 'rewrite()', self._fileName,
					  'rewrite() is ignored for .tsvz; behaviour undefined; use scrubTabularFile for archival compaction',
					  self.teeLogger)
			self._specSync(reloadInternalFromFile)
			return False
		return self._call(self._rewrite, force, reloadInternalFromFile)

	def _rewrite(self, force=False, reloadInternalFromFile=None, background=False):
		"""rewrite() for a legacy file. ``background``: the background writer's tick, which takes in another process's
		change at every tick, but makes no rewrite while one that failed waits to be tried again (see _failed).

		Following another process's change never rewrites. A rewrite is made when it is asked for (``force``: always,
		dirty or not), and otherwise only when it is due and the file holds more than memory needs (``dirty``) or is
		damaged: rewrite_interval set and over, the file damaged or left damaged with rows after the damage (memory is the
		only good copy), or one that failed being tried again."""
		due = (force or self._damaged or (self._remake and self.dirty) or 'rewrite' in self._failures or (
			self.dirty and self.rewrite_interval != 0
			and time.time() - getFileUpdateTimeNs(self._fileName) / 1e9 >= self.rewrite_interval))
		if not self.deSynced and not due:
			return False
		try:
			clean = reloadInternalFromFile is None and not (self.dirty or self._damaged)  # (memory holds nothing the file lacks)
			if reloadInternalFromFile is None:
				reloadInternalFromFile = self.monitor_external_changes
			# Not while a failed rewrite of this store's has left the file damaged: memory holds the only good copy,
			# and the rewrite below makes the file again from it, merging what another process appended.
			if reloadInternalFromFile and not self._damaged and self._externalChange():
				self._syncFile()
			self._worked('sync')
			if not self._damaged:
				self.deSynced = False  # (followed; a rewrite that failed is tried again as due, not for this flag)
			if self.memoryOnly:
				if self.verbose:
					self.__teePrintOrNot("Memory only mode. Map to file skipped.")
				return False
			made = True
			if force or (due and (self.dirty or self._damaged)):
				if not force and not self._closing and self._refusalPending():
					return False  # the last rewrite was refused (text that cannot be written): try again later, not at every tick
				if background and self._failing('rewrite'):
					return False  # the last one failed: tried again later; another process's change was still taken in above
				if self.verbose:
					self.__teePrintOrNot(f"Rewriting {self._fileName}")
				if clean and not self.createIfNotExist and not os.path.isfile(self._fileName):
					made = True  # (a store that holds nothing new has nothing to make the file from: an append makes it)
				else:
					# A clean store follows another process's change even with monitoring off (that choice is for
					# overwriting what it changed itself: a rewrite of nothing new must not delete a row it never held).
					made = self._mapToFile(sync=reloadInternalFromFile or clean)
				if made and self.verbose:
					self.__teePrintOrNot(f"{len(self)} records rewrote to {self._fileName}")
			if not self._workerRuns():
				self._commit()
			return made  # False when the rewrite failed or was refused (reported)
		except Exception as e:
			self._failed('sync', f"Failed to write at sync() to {self._fileName}: {e}")
			self.deSynced = True
			return False

	def _externalChange(self):
		"""True when the file changed since this object last wrote or read it: a later modification time, or another
		size than the bytes it knows (an append that landed right after its own, in the same tick)."""
		try:
			st = os.stat(self._fileName)
		except OSError:
			return False
		return self.externalFileUpdateTime < _updateTimeNs(st) or (self._known is not None and st.st_size != self._known[1])

	# ``(identity, size, sha256)`` of the bytes this store last read or wrote in its legacy file; None when not known.
	_known = None
	# True from when a rewrite starts changing the file until all of it is on disk. While a failed one leaves it True,
	# memory holds the only good copy: the file is never read over it, and the next rewrite makes it again, merging
	# another process's appends (the merge rule, _syncFrom); _known is then what the file held when it stopped.
	_damaged = False
	_readWhole = False  # setDefaults() gave memory defaults the file may not hold: its next change is read whole
	_widthOpen = False  # the last whole read took no column count from the file: its first keyed line will set it
	_appendWidth = 0  # what a row it appends is padded to, from line 1 as the last whole read or write left it (_legacyAppendWidth)
	# A read under the file lock met damage another writer left (a compressed file: every read of it stops there, L5).
	# Rows appended after it would be lost to the next read, so the next commit makes the file again from memory.
	_remake = False
	# The last whole read took line 1 for a widening of this store's header (_legacyExtendsHeader): the file reads loose, what
	# is appended to it too, and a narrower set is padded. Set by that read alone, and every whole read sets it again.
	_followedWidening = False
	# A commit appended after another process's change it has not read: the file holds unread bytes after _known, so
	# the next read is whole. _known still says how the file begins (a commit looks for damage after it).
	_unseen = False
	_refusedAt = None  # when a rewrite was last refused for text that cannot be written (_specClock); None when it was not
	_refusalReported = False
	_refusals = 0  # refusals in a row, since the last _accepted(): each doubles the wait before the next try
	_closing = False  # stopAppendThread() makes its last rewrite: none is tried after it
	_writing = 0  # write jobs and writes under way on the writer thread (_startWrite): the exit lets go of none of them
	_letGo = False  # the exit let go of the store (_atExit), or abandoned it: its writer thread starts no write
	_exiting = False  # _stopLiveStores stops it at exit: a Ctrl-C there finishes the store, which does not run on

	def _refuse(self, where, error, explicit=False):
		"""Remember that a rewrite was refused with ``error``; raise it for an ``explicit`` call, else report it once.

		mapToFile() and hardMapToFile() called by the program raise it, as a refused set does: the caller learns
		that nothing was written. The rewrites TSVZed makes itself report it, once for each different refusal, and
		again once the file is damaged or the writer thread stops (close() tries none again), until a rewrite goes
		through, clear() or reload() (no traceback: it is not a bug). The wait before a rewrite is tried again starts
		at rewrite_interval (at least a second) and doubles at each refusal in a row, up to a minute (_refusalWait()), as
		a failed write's does; close() does not wait it out.
		"""
		self._refusedAt = _specClock()
		self._refusals += 1
		if explicit:
			raise error
		message = str(error)
		stopped = self._closing or not self._workerRuns()
		if (message, self._damaged, stopped) == self._refusalReported:
			return
		self._refusalReported = (message, self._damaged, stopped)
		if stopped:
			retry = ("None is tried again on its own: this store's writer thread is stopping or stopped (close(), a "
					 "forked child).")
		elif self.rewrite_interval or self._damaged or self._dropQueued():  # (made again, or a delete to write: at any interval)
			wait = self._refusalWait()
			if wait >= _FAILURE_RETRY_MAX:
				retry = f"A rewrite is tried again at most every {wait:g} s."
			else:
				retry = (f"A rewrite is tried again {wait:g} s from now at the earliest, the wait doubling at each refusal "
						 f"in a row up to a minute.")
		else:
			retry = ("With rewrite_interval=0 none is tried again on its own, only one asked for (mapToFile(), "
					 "hardMapToFile(), rewrite(force=True), rewrite_on_exit) or, while damage another writer left is to be made "
					 "good, one at a later append.")
		damaged = (" The file is damaged (a rewrite of this object's failed) or was removed (rows are appended to a new "
				   "one): until a rewrite gets through, memory holds the only good copy." if self._damaged else "")
		self.__teePrintOrNot(f"Refused at {where} to {self._fileName}: {message}. The file is left as it is, and rows that can "
							 f"be written are still appended.{damaged} It is rewritten once every row can be written: set or delete the row; "
							 f"a key that cannot be written cannot be deleted either, so for one, edit the file, clear() the store "
							 f"or open it with the encoding it was written in. {retry}",'error')

	def _refusalWait(self):
		"""Seconds to wait after the last refusal before a rewrite is tried again: ``rewrite_interval`` (at least a
		second) for the first, doubled at each refusal in a row up to a minute, and never less than ``rewrite_interval``."""
		doublings = min(max(self._refusals - 1, 0), 16)  # capped: a long streak must not overflow the float
		first = max(self.rewrite_interval, _REFUSED_RETRY_SECONDS)
		return max(self.rewrite_interval, min(first * 2 ** doublings, _FAILURE_RETRY_MAX))

	def _refusalPending(self):
		"""True while the last rewrite was refused less than ``_refusalWait()`` ago (``rewrite_interval``, at least a
		second, after the first refusal, twice as long after each further one in a row, up to a minute)."""
		return self._refusedAt is not None and _specClock() - self._refusedAt < self._refusalWait()

	def _accepted(self):
		"""A rewrite went through, or memory was emptied or read again: a later refusal is reported again."""
		self._refusedAt = None
		self._refusals = 0
		self._refusalReported = False

	def _failed(self, kind, message, trace=True, retry=True):
		"""A legacy write of ``kind`` ('append', 'rewrite' or 'sync') failed: report ``message`` unless it was since that
		kind last went through, and (``retry``) have the background writer wait before it tries that kind again, 0.1 s
		doubling to a minute. ``retry=False``: the write goes on another way (mapToFile() falls back on
		hardMapToFile()), which counts the try. True when it was reported.
		"""
		if retry:
			state = self._failures.get(kind)
			wait = min(state[0] * 2, _FAILURE_RETRY_MAX) if state else _FAILURE_RETRY_FIRST
			self._failures[kind] = (wait, _specClock() + wait)
		reported = self._reported.setdefault(kind, set())
		if message in reported:
			return False
		reported.add(message)
		self.__teePrintOrNot(message if self._closing else f"{message}; not reported again until it works",'error')
		if trace:
			import traceback
			self.__teePrintOrNot(traceback.format_exc(),'error')
		return True

	def _worked(self, kind):
		"""A legacy write of ``kind`` went through: the next failure is reported, and retried soon."""
		self._failures.pop(kind, None)
		self._reported.pop(kind, None)

	def _failing(self, *kinds):
		"""True while the background writer waits to try again a write of one of ``kinds`` that failed."""
		now = _specClock()
		return any(state is not None and now < state[1] for state in map(self._failures.get, kinds))

	def _headerLine(self, f, clearing=False):
		"""The header line a legacy file of this store begins with, as bytes without its newline; None for no header.

		Checked by the constructor: it can be written. A store with no header (['']) begins the file with the empty line
		that stands for one (3.39 and 5.0 did, always) when the file holds nothing or begins with a line that is not a
		data row, and when it is cleared; it never puts one in front of a first line that is a row. ``f`` is the open,
		locked file a rewrite is to write: looked at through it (not by name, which would drop a lockf lock)."""
		if not self.header:
			return None
		if not clearing and not any(self.header) and not _legacyBeginsBlankIn(f, self.encoding, self.delimiter):
			return None
		return _encodeRow(_sanitize(list(self.header),delimiter=self.delimiter),self.delimiter,self.encoding,self.errors,what='header')

	def _readLines(self, kept, consumed, start, strict):
		"""What the legacy lines ``kept`` say, as this store's whole read takes them, streamed (no rows):
		``(flags, width)``, one flag per key (_TSVZedDiff) and the column count the read ended with. ``start`` and ``strict``
		are the count that read starts from and how it reads, which _snapshotForRewrite takes from line 1."""
		diff = _TSVZedDiff(self, self._givenColumns, list(self._givenDefaults))
		reporter = _Reporter(self._fileName, None)  # (never flushed: the store's own reads report)
		stream = _LinesFile(kept)
		if consumed:  # (as _legacyReadHeader takes line 1, saying nothing: the store's own reads warn)
			stream.readline()
		width = _legacyReadLoop(stream, '', diff, start, 1 if consumed else 0, strict, self.delimiter, diff.defaults,
								False, self.encoding, reporter)[0]
		return diff.flags, width

	def _fileLines(self, f):
		"""The lines of the legacy file (bytes, without their newline), read through the locked ``f``; None when it is to be
		made again from memory instead: damaged, made again, missing, not a regular file or holding nothing, or its
		read meets damage. Never opens the file by name nor takes the READ lock: this thread holds the REWRITE lock,
		and closing another descriptor would drop a lockf lock."""
		if self._damaged or self._remake:
			return None
		try:
			if not stat.S_ISREG(os.fstat(f.fileno()).st_mode) or not os.path.isfile(self._fileName):
				return None
			return _legacyLinesIn(f, self._fileName) or None
		except Exception:
			return None  # (damage met: memory is the only good copy)

	def _remadeLines(self, f):
		"""What a file made again from memory begins with, the lines that keep its reading: the header line (None for
		none: _headerLine), its first cell alone for a one-column store holding rows with no value; under no header, a line that keeps the column count: the first row memory holds exactly that
		wide, moved to the front, else at two columns or more an empty #_defaults_# line as wide (a reset, deleting
		nothing)."""
		delimiter, encoding, errors = self.delimiter, self.encoding, self.errors
		line = self._headerLine(f)
		lines = [] if line is None else [line]
		width = self.correctColumnNum
		if any(self.header) or width <= 0:
			with self._memLock:
				lone = width == 1 and len(self.header) > 1 and any(not any(row[1:]) for row in OrderedDict.values(self))
			if lone:  # (under the whole header they would read as deletions: _legacyWidened)
				lines = [_encodeRow(_sanitize(self.header[:1],delimiter=delimiter),delimiter,encoding,errors,what='header')]
			return lines
		with self._memLock:
			key = next((key for key, row in OrderedDict.items(self) if len(row) == width and not str(key).startswith('#')
						and (width == 1 or any(row[1:]))), None)
			row = None if key is None else dict.get(self, key)
			if key is not None:
				OrderedDict.move_to_end(self, key, last=False)
		if row is None and width > 1:
			row = [DEFAULTS_INDICATOR_KEY] + [''] * (width - 1)
		if row is not None:
			lines.append(_encodeRow(_sanitize(row,delimiter=delimiter),delimiter,encoding,errors,self.header))
		return lines

	def _snapshotForRewrite(self, f, commit=False):
		"""``(None, lines, queued)``, what a rewrite writes, and the queued rows, taken from the queue (_writeFile puts them
		back if it stops). A healthy file (Q-D3): its lines (_fileLines) with the queued rows after them, line 1 widened to
		the store's header when it is that header's beginning (_legacyWidened) and the lines
		of drop items' keys left out (_legacyDropLines), compacted as a scrub compacts them (_legacyCompact), but for a
		commit's (``commit``): it widens nothing. A file to
		be made again: only what keeps its reading (_remadeLines); memory holds what was queued. Then, either way, what
		memory holds that those lines do not say (_RewriteRows, a flat snapshot checked before anything is written: a
		TSVZEncodeError changes nothing): rows that read otherwise (changed in place, put in memory straight, every row
		of a file made again), deletion lines of keys memory no longer holds (in a one-column file, those keys' lines
		left out too), and the store's defaults when the lines do not end with them."""
		delimiter, encoding, errors = self.delimiter, self.encoding, self.errors
		enc = encoding or 'utf8'
		raw = self._fileLines(f)
		remade = raw is None
		with self._memLock:
			queued, self.appendQueue = self.appendQueue, deque()
		try:
			dropAt = {}  # key -> the lines of it before this index go (a drop item: its lines so far, not a later row's)
			if remade:
				raw, queuedLines = self._remadeLines(f), ()
			else:
				queuedLines = queued
			for row in queuedLines:  # as the commit would append them: the compaction sees the file as it will be
				if isinstance(row, _DropKey):
					dropAt[row[0]] = len(raw)
				elif row and (row[0] == DEFAULTS_INDICATOR_KEY or not str(row[0]).startswith('#')):
					raw.append(_encodeRow(_sanitize(row,delimiter=delimiter),delimiter,encoding,errors,self.header))
			header = list(self.header) if self.header and any(self.header) else []
			consumed = bool(header) and self.verifyHeader
			defaults = list(self._givenDefaults)
			widened = None
			if consumed and not remade and not commit:
				# A rewrite asked for decides on the lines as they read once the queued deletes are made, a lone key they take
				# out being no row the widening would lose, whether or not the writer thread committed its delete first; the
				# deletes are made on the same drop the commit makes (a stand-in where a line set a reader's count). A
				# commit's (``commit``: the rewrite a one-column delete makes) carries out the queue and widens nothing: the
				# delete changes nothing else for the file's other readers, so the next rewrite asked for widens line 1.
				fromLines = not _legacyHoldsHeader(raw[0] if raw else None, header, consumed, encoding, delimiter)
				widened = _legacyWidened(_legacyDropLines(raw, dropAt, consumed, fromLines, delimiter, encoding) if dropAt else raw,
										 header, encoding, delimiter, errors)
				if widened:
					raw = widened
					dropAt = {}  # (the deletes made: line 1 now holds the header, so no line sets a count)
				elif widened is False:
					_warnOnce(self, 'not-widened', self._fileName, 'line 1 is not widened to the header given: the file holds rows '
							  'with no value, which read as deletions at two columns or more', self.teeLogger)
			line0 = raw[0] if raw else None
			matched = _legacyHoldsHeader(line0, header, consumed, encoding, delimiter)
			# A file of one column is compacted as one, whatever the store's defaults say: its lone keys are rows, and at their
			# width they are deletions the compaction would carry out (a\nb\nc under defaults of two cells lost b and c). At
			# two columns or more the defaults set the width of the reader the compaction keeps what it reads for.
			width = _legacyReaderWidth(raw, header, consumed, matched, encoding, delimiter)
			if width != 1 and not matched and len(defaults) > 1:
				width = len(defaults)
			# How the store's read takes these lines: the same width and strictness, or the rewrite takes rows the read kept
			# for rows memory lacks, and adds them again.
			wider = _legacyExtendsHeader(header, line0.decode(enc, errors='replace'), delimiter) if consumed and raw else 0
			start = wider or (len(header) if consumed and (matched or not raw) else -1)
			strict = self.strict and not wider
			del line0
			for _ in range(2):  # (twice when the lines say keys are live that are gone from memory, in a one-column file)
				if dropAt:
					raw = _legacyDropLines(raw, dropAt, consumed, not matched, delimiter, encoding)
				# A commit's rewrite compacts too, or a one-column file that a store updates and deletes in grows with every
				# superseded line, and each delete rewrites all of it (5.9). The compaction keeps what every count the file is
				# read at takes (_legacyCompactReaders): one made at the store's count alone dropped a narrower line superseded
				# there, the only line 3.39's strict reader takes at line 1's two (5.8).
				# (a correctColumnNum the program gave is planned too, 5.11: the store reads at the file's count, but a reader
				# given the store's header, defaults and count reads at it)
				kept = _legacyCompact(raw, width, consumed, defaults, delimiter, encoding, matched, (self._givenColumns,))
				del raw
				flags, diffWidth = self._readLines(kept, consumed, start, strict)
				with self._memLock:
					values = [row for key, row in OrderedDict.items(self) if flags.get(key) is not True and not str(key).startswith('#')]
					gone = [key for key, flag in flags.items() if flag is not None and not dict.__contains__(self, key)]
					want = list(self.defaults)
				if not gone or diffWidth > 1:
					break
				if self.correctColumnNum > 1:
					# setDefaults() changed how the file reads, to one column; memory never held these: left as they read.
					gone = []
					break
				# A lone key is a row in a one-column file: a line cannot delete there. Their lines go instead.
				raw, dropAt = kept, dict.fromkeys(gone, len(kept))
			del flags
			# (no one-cell line is left: the file now reads at another count, and a reopen would read every row at it, so
			# memory reads the file again once it is written, as it does for a widened line 1)
			readAgain = bool(dropAt) and self.correctColumnNum == 1 and diffWidth > 1
			tail = [_encodeRow(_sanitize([key] + [''] * (diffWidth - 1),delimiter=delimiter),delimiter,encoding,errors,self.header)
					for key in gone]
			# The store's defaults, when the lines do not end with them (plan Q2, answer c): one line padded to the
			# column count (a strict reader takes no other width), with a reset in front when one line cannot say them
			# (a cell the file's defaults set that the store's leave empty: an empty cell takes the one before it).
			# Kept only when the store's reader then ends with them: a line it does not take (a reset at column count 1
			# is a row there) would be added again at every rewrite.
			def endOf(lines, begin=()):
				return _fileDefaultsAtEnd(lines, diffWidth, delimiter, encoding, consumed, strict, begin)
			own = endOf(kept)
			goal = _trimmedCells(want) if any(want[1:]) else [DEFAULTS_INDICATOR_KEY]
			reset = _encodeRow([DEFAULTS_INDICATOR_KEY],delimiter,encoding,errors,what='defaults')
			lines = []
			if any(want[1:]):
				if goal != own:
					lines = [_encodeRow(_sanitize(_fitRow(want, diffWidth),delimiter=delimiter),delimiter,encoding,errors,
										what='defaults')]
					# (from none and from the store's defaults: a strict reader takes a narrower defaults line only under
					# defaults with values, so the file's lines can read differently from the two)
					if endOf(itertools.chain(kept, lines)) != goal or endOf(itertools.chain(kept, lines), defaults) != goal:
						lines.insert(0, reset)
			elif any(own[1:]) or (remade and any(defaults[1:])):
				lines = [reset]
			if lines and endOf(itertools.chain(kept, lines)) == goal:
				if diffWidth > 0 and not remade:
					kept.extend(lines)  # (before memory's rows: they read with the store's defaults)
				else:
					tail.extend(lines)  # (after the rows: a file made again, or no column count yet, so it does not set one)
			if values and diffWidth > 0:
				# A row memory adds with an empty cell where the defaults then in effect hold a value would read filled:
				# those rows go between a reset and the same defaults again (the file still ends with them).
				end = endOf(kept, defaults)
				if any(i < len(end) and end[i] and not str(row[i]).strip() for row in values for i in range(1, len(row))):
					if remade:
						kept.append(reset)  # (the store's reader starts from its own defaults; the file's line is the tail)
					else:
						again = _encodeRow(_sanitize(_fitRow(end, diffWidth),delimiter=delimiter),delimiter,encoding,errors,
										   what='defaults')
						if endOf(itertools.chain(kept, (reset, again))) == end:
							kept.append(reset)
							tail.insert(0, again)
			# (a narrower row padded: it reads the same at this column count, and a strict reader with no defaults in
			# effect, as between a reset and the defaults, would drop it; a lone key stays one, for a reader at one column)
			values = [_fitRow(row, diffWidth) if len(row) < diffWidth and any(row[1:]) else row for row in values]
			rows = _RewriteRows(kept, values, delimiter, encoding, errors, self.header, tail=tail)
			# (memory reads the file again when its lines were widened to the header, or a file made again begins with a
			# header wider than the rows memory holds: they are padded in the file, so memory follows)
			rows.widened = bool(widened) or readAgain or (remade and matched and 0 < self.correctColumnNum < len(header))
			del values
			rows.check()
		except BaseException:
			with self._memLock:
				self.appendQueue.extendleft(reversed(queued))
			raise
		return None, rows, queued

	def _openLocked(self, opener, mode=_REWRITE):
		"""``opener(fileName)``, locked for ``mode`` (_lockFile; closed again when the lock fails)."""
		f = opener(self._fileName)
		try:
			_lockFile(f, mode=mode)
		except BaseException:
			f.close()
			raise
		return f

	def _changedSince(self, f):
		"""True when the open file ``f`` may not hold what _known says: unknown, another file, size or time."""
		st = os.fstat(f.fileno())
		known = self._known
		return (known is None or st.st_size != known[1] or _updateTimeNs(st) != self.externalFileUpdateTime
				or (known[0][1] is not None and _fileIdentity(st) != known[0]))

	def _writeFile(self, sync=True, snapshot=None, commit=False):
		"""_writeFileStarted, unless the exit has let go of the store (False: nothing is written)."""
		if not self._startWrite():
			return False
		try:
			return self._writeFileStarted(sync, snapshot, commit)
		finally:
			self._endWrite()

	def _writeFileStarted(self, sync=True, snapshot=None, commit=False):
		"""Write memory (or ``snapshot()``'s, a clear's) over the legacy file, whole (a compressed file as whole members;
		written over the file, then cut to its end): True when all of it is on disk, False when the file could not be
		opened or locked (reported); what stops it raises.

		The file is locked before anything is read and until all of it is on disk. Another process's change is taken
		in first (``sync``; False: overwritten with a warning; None, a clear: not looked for) and again if it lands
		while the rows are taken; while the file is damaged it is merged by the merge rule (_syncFrom). What stops the
		write leaves dirty, deSynced and _damaged set and puts the queued rows back."""
		with self.writeLock:
			if not os.path.isfile(self._fileName):
				# Removed (rm, a log rotation): memory is the only copy. Made again whole; what another process writes
				# there meanwhile is merged, whatever file it is. deSynced: tried again if this one is refused, once the
				# refusal wait (_refusalWait) is over, without waiting for the file to keep still for rewrite_interval.
				self.dirty = self.deSynced = self._damaged = True
				self._known, self._unseen = _KNOWN_NOTHING, False  # merged whatever is there, whichever file it is
			try:
				f = self._openLocked(_openUntruncated)
			except Exception as e:
				self._failed('rewrite', f"Failed to open file {self._fileName}: {e}", trace=False)
				self.deSynced = True
				return False
			if self._unattended():  # the exit let go of the store (abandoned) while this waited for the lock
				_unlockFile(f)
				f.close()
				return False
			started = False
			end = None
			try:
				for attempt in range(3):
					if sync is not None and (self._damaged or (sync and self._changedSince(f))):
						self._syncFrom(f, True)
					elif sync is False and self._externalChange():
						self.__teePrintOrNot(f"Warning: Overwriting external changes in {self._fileName}",'warning')
					header, lines, queued = snapshot() if snapshot else self._snapshotForRewrite(f, commit)
					if not sync or self._damaged or attempt == 2 or not self._changedSince(f):
						break
					# A writer that takes no lock appended while the rows were taken: taken in too, and taken again.
					header = lines = None
					with self._memLock:
						self.appendQueue.extendleft(reversed(queued))
				started = True
				self.dirty = self._damaged = True
				f.seek(0)
				# Written whole over what the file holds, then cut to its end (spec §19.8): a reader that takes no lock
				# never meets it emptied, and a write cut short leaves the old bytes after the new ones. (5.5: always;
				# 5.4's in-place write of mapToFile() padded a shorter line with spaces, which readers show as cells.)
				_writeLines(f, header, lines, codec=_fileCodec(self._fileName))
				if stat.S_ISREG(os.fstat(f.fileno()).st_mode):  # /dev/null cannot be cut
					f.truncate()
				f.flush()
				_fsync(f.fileno())
				end = f.tell()  # its own bytes end here: a writer that takes no lock may append after them
				self._damaged = self._remake = False
				self.dirty = self.deSynced = False
				first = header if header is not None else (getattr(lines, 'first', None) or [None])[0]
				self._appendWidth = _legacyAppendWidth(self.header, None if first is None else first.decode(
					self.encoding or 'utf8', errors='replace'), self.delimiter) if self.verifyHeader else 0  # (the line 1 it wrote)
				if getattr(lines, 'widened', False) or not self:
					self._readWidened(f)  # (no row left: the column count is what the file now says, as a reopen takes it)
			except BaseException:
				if started:
					self.dirty = self.deSynced = True  # made again from memory by the next rewrite
					with self._memLock:
						self.appendQueue.extendleft(reversed(queued))
				raise
			finally:
				try:
					if started:
						# What it wrote, or what stopping it left (the damage): read through the descriptor, before the
						# file is let go of. Its time is noted as this object's own.
						self._known, self._unseen = _fileKnown(f.fileno(), None if self._damaged else end), False
						self.externalFileUpdateTime = getFileUpdateTimeNs(self._fileName)
				except Exception:
					self._known = None
				finally:
					_unlockFile(f)
					_closeUnflushed(f)  # a buffer a failed write left is dropped, not written after the unlock
		return True

	def _readWidened(self, f):
		"""Memory becomes what the open legacy file ``f``, just rewritten, reads now that line 1 is widened: its rows gain
		the new cells and the store the new column count. A '#' key, which no file holds, stays."""
		with self._memLock:
			held = [(key, row) for key, row in OrderedDict.items(self) if key.startswith('#')]
		self._readFrom(f, True)
		with self._memLock:
			for key, row in held:
				OrderedDict.__setitem__(self, key, row)

	def _syncFile(self):
		"""Take in another process's change to the legacy file (_syncFrom), reading it as reload() does: under the READ
		lock (_readLock), so another process's rewrite is waited out, never met part way; a read still cut short by
		damage is read again under the write lock, once the READ lock is let go of (_readLocked)."""
		with self.writeLock:
			with _readLock(self._fileName), open(self._fileName, 'rb') as f:
				cut = self._syncFrom(f, False)
			if cut:
				self._readLocked(lambda f: self._syncFrom(f, True))

	def _readLocked(self, read):
		"""``read(f)`` again with the file open and locked. A read cut short by damage without the lock may have met
		another writer's write in progress, which every writer finishes under the lock; damage still there under it
		stays, and what that read took is known. Not when the file cannot be opened for writing (read-only) or is gone."""
		try:
			f = self._openLocked(lambda path: open(path, 'r+b'))  # removed meanwhile: not made again, empty
		except OSError:
			return
		try:
			read(f)
		finally:
			_unlockFile(f)
			f.close()

	def _syncFrom(self, f, locked):
		"""Take in what another process changed in the open legacy file ``f``: when it is the same file and still begins
		with the bytes _known says, only what follows is read and applied (_applyAppended), else it is read whole
		(_readFrom). While the file is damaged (the merge rule), any other change is left to the rewrite about to
		start to overwrite, with a warning: memory holds the only good copy. True when, not ``locked``, the read was
		cut short by damage: nothing of it is known, and the caller reads it again under the lock (_readLocked)."""
		seen = getFileUpdateTimeNs(self._fileName)  # before the read: a write landing after it is not noted as seen
		found = None if self._unseen else _readAfter(f, self._known)
		header = None  # True: a row was queued while what follows was read
		# A last line with no newline is read again: what is appended may complete it (a compressed file's appends
		# are members of their own). Not while rows are queued (unless the file is damaged): what follows would go after
		# them in memory, where the file will hold them after it. Nor when a store holds no row and the last whole read took
		# no column count from the file (a file with no keyed line yet): its first keyed line sets it, which a whole read takes.
		# (A store emptied at one column is read whole too: a lone key there is a row until a line says otherwise.)
		if (found is not None and (found[2] or self._damaged or _isCompressedFile(self._fileName))
				and (self._damaged or (not self.appendQueue and not self._readWhole
									   and (self or (self.correctColumnNum != 1 and not self._widthOpen))))):
			rest, known, _ = found
			header, torn = self._applyAppended(f, rest, self._known[1])
			if header is None:
				cut = torn and not locked  # a member cut short may be an append still being written
				self._known = None if cut else known
				self.externalFileUpdateTime = seen
				return cut
		if self._damaged:
			why = ("another process made it again with a first line that is not this store's header ({!r})".format(header)
				   if isinstance(header, str) else "and they do not follow the bytes the file held then")
			self.__teePrintOrNot(f"Warning: Overwriting external changes in {self._fileName}: they were made while the file was "
								 f"damaged and memory held the only good copy (a rewrite of this object's failed, part way or "
								 f"before it began on damage another writer left), {why}; memory is written over them",'warning')
			return False
		return self._readFrom(f, locked)

	def _applyAppended(self, f, rest, size):
		"""Apply ``rest``, what follows the first ``size`` bytes of the open legacy file ``f`` (a compressed one read member
		by member), to memory as a read of the file applies it: the last change of a key wins, a key deleted and set
		again moves to the end, a key still queued keeps memory's row. Returns ``(header, torn)``: ``header`` None when
		applied; else, after bytes that hold nothing, their first line, which is not this store's header (an append to
		an empty file writes it first), or True when a row was queued meanwhile (not while the file is damaged): what
		follows would go before it in memory. ``torn`` when a compressed member is cut short or does not decode."""
		codec = _fileCodec(self._fileName)
		reporter = _Reporter(self._fileName, self.teeLogger)
		damage = None
		try:  # what it notes is reported however it ends
			if codec:
				rest, damage = _decodeMembers(codec, rest)
				if damage:
					reporter.note('damaged', None, 'compressed stream another process appended is damaged ({}); the rest is '
								  'read'.format('; '.join(damage)))
			if any(self.header) and self.verifyHeader and _holdsNothing(f, codec, size):
				first, _, rest = rest.partition(b'\n')
				line = first.decode(self.encoding or 'utf8', errors='replace').rstrip()
				if first and not _headerMatches(self.header, line, self.delimiter):
					return line, False
			defaults = list(self.defaults)
			given = self._givenDefaults
			changes = _TSVZedMerge(self, self.correctColumnNum, list(defaults))
			width = _legacyReadLoop(io.BytesIO(rest), '', changes, self.correctColumnNum, 0,
									self.strict and not self._followedWidening,  # (as the whole read took this file)
									self.delimiter, changes.defaults, False, self.encoding, reporter)[0]
		finally:
			reporter.flush()
		with self._memLock:
			# No set or delete is half done: each holds _memLock from its change of memory to its queueing.
			if self.appendQueue and not self._damaged:
				return True, False
			queued = set(row[0] for row in self.appendQueue if row)
			for key, (row, readded) in changes.items():
				if key in queued:
					continue
				held = dict.get(self, key)
				if row is None:
					if held is not None:
						OrderedDict.__delitem__(self, key)  # not OrderedDict.pop: on 3.6 it calls TSVZed.__delitem__
						self.dirty = True  # the file holds the key twice
				else:
					if held is not None and held != row:
						self.dirty = True
					OrderedDict.__setitem__(self, key, row)
					if readded:  # deleted and set again: a read of the file has it at the end, not in its old place
						OrderedDict.move_to_end(self, key)
			if changes.defaults != defaults and DEFAULTS_INDICATOR_KEY not in queued and self._givenDefaults is given:
				self.defaults = changes.defaults  # (not when setDefaults() gave others during the read: they stay)
			if self.correctColumnNum == -1 and self:
				self.correctColumnNum = width if width > 0 else len(self[next(iter(self))])
		if self.verbose:
			self.__teePrintOrNot(f"Read what another process appended to {self._fileName}: {len(self)} records")
		return None, bool(damage)

	def _readFrom(self, f, locked):
		"""Make memory what the open legacy file ``f`` holds (None: no file), with the queued rows replayed on it. It is
		read apart from memory, then memory is changed in place under _memLock, never emptied (a key the file still
		holds is never missing for another thread) and takes the file's order. _known is then the bytes read."""
		seen = getFileUpdateTimeNs(self._fileName)  # before the read: a change during or after it is seen by the next check
		given = self._givenDefaults
		loaded = _TSVZedLoad(self, self._givenColumns, list(given))  # (as the first read did)
		width = -1
		known = _KNOWN_NOTHING
		damaged = False
		widened = False
		lineOne = [None]  # (the line 1 the read met: what every row appended is padded to, _legacyAppendWidth)
		if f is not None:
			codec = _fileCodec(self._fileName)
			reader = _HashingReader(f)
			reporter = _Reporter(self._fileName, self.teeLogger)
			try:
				f.seek(0)
				size = os.fstat(f.fileno()).st_size
				if _holdsNothing(f, codec, size):
					reader.read(size)  # no line: an empty store with its header; only the bytes looked at are known
				else:
					_requireCodec(codec)
					stream = _openStreams(codec, reader) if codec else reader
					try:
						start = _legacyReadHeader(stream, self._fileName, self.header, self.verifyHeader, self.verbose,
												  self.teeLogger, self.strict, self.delimiter, -1, self.encoding, reporter,
												  widening=True, said=self._mismatchSaid, seen=lineOne)
						if start is not None:
							# (the rows of a file another program widened are read loose, padded, the file untouched: a strict
							# reader would drop every row narrower than the new count, and a rewrite then delete them)
							widened = start[0] > len(self.header)
							width = _legacyReadLoop(stream, self._fileName, loaded, start[0], start[1], self.strict and not widened,
													self.delimiter, loaded.defaults, False, self.encoding, reporter)[0]
					finally:
						if codec:
							stream.close()
				# Read up to damage without the lock: maybe a whole rewrite still being written, so not known. Under it,
				# the damage stays: the bytes after it are known as they are (what follows them is read on from there).
				damaged = reporter.has('damaged')
				cut = not locked and damaged
				if locked and damaged:
					while reader.read(1 << 20):
						pass
			finally:
				reporter.flush()
			known = None if cut else (_fileIdentity(os.fstat(f.fileno())), reader.size, reader.digest)
		with self._memLock:
			# No set or delete is half done: each holds _memLock from its change of memory to its queueing. What is
			# queued is not in the file (appends take writeLock, held here): replayed on what was read, as reading it
			# once appended will, so memory stays what the file will hold.
			if width > 0:
				loaded.columns = width  # (the queue is replayed as reading it back will: at the width the read took)
			elif self.correctColumnNum == 1:
				loaded.columns = 1  # (no line read, an emptied store: its rows read back at one column, not as deletions)
			loaded.replay(list(self.appendQueue))
			self._replaceRows(loaded)
			if self._givenDefaults is given:  # (not when setDefaults() gave others during the read: they stay)
				self._readWhole = False
				self.defaults = loaded.defaults
			if loaded.dirty:
				self.dirty = True  # a key the file holds twice
			self._followedWidening = widened
			self._appendWidth = _legacyAppendWidth(self.header, lineOne[0], self.delimiter) if self.verifyHeader else 0
			self._widthOpen = width <= 0
			if width > 0:
				self.correctColumnNum = width  # the column count the read took (the header's, when line 1 holds it)
			elif self.header and any(self.header) and self.verifyHeader:
				self.correctColumnNum = len(self.header)  # (no keyed line: the header's, which the next line written follows)
			elif len(self._givenDefaults) > 1:
				self.correctColumnNum = len(self._givenDefaults)  # (no keyed line: what the store's reader will take)
			elif self:
				self.correctColumnNum = len(self[next(iter(self))])
			elif self.correctColumnNum != 1 or not self._constructed:  # (the constructor's 1 is correctColumnNum=, no fact of the file)
				self.correctColumnNum = -1
			# (else a one-column file another process emptied: a lone key stays a row, until a line says otherwise, _syncFrom)
		self._known, self._unseen = known, False
		self._damaged = False  # memory is what the file holds, damaged or not
		self._remake = locked and damaged
		self.externalFileUpdateTime = self.lastUpdateTime = seen
		self.deSynced = False  # memory is the file again: a change seen before needs no rewrite
		return known is None and f is not None

	def _replaceRows(self, rows):
		"""Make memory hold the mapping ``rows`` in its order, changed in place, never emptied: a key both hold is never
		missing for another thread meanwhile (a reload). The caller holds _memLock."""
		for key in [key for key in OrderedDict.__iter__(self) if not dict.__contains__(rows, key)]:
			OrderedDict.__delitem__(self, key)
		for key, row in rows.items():
			OrderedDict.__setitem__(self, key, row)
		if list(OrderedDict.__iter__(self)) != list(rows):  # another order: the keys take it, one at a time
			for key in rows:
				OrderedDict.move_to_end(self, key)

	def _rewritten(self):
		"""What mapToFile and hardMapToFile do once _writeFile wrote every row; True."""
		if self.verbose:
			self.__teePrintOrNot(f"{len(self)} records written to {self._fileName}")
			self.__teePrintOrNot(f"File {self._fileName} size: {os.path.getsize(self._fileName)}")
		self._accepted()
		self._worked('rewrite')
		return True

	def hardMapToFile(self):
		if self._spec:
			_warnOnce(self, 'hardMapToFile()', self._fileName,
					  'hardMapToFile() is ignored for .tsvz; behaviour undefined; use scrubTabularFile for archival compaction',
					  self.teeLogger)
			return self
		self._call(self._hardMapToFile, False, True)
		return self

	def _hardMapToFile(self, sync=False, explicit=False, commit=False):
		"""hardMapToFile(); ``sync``: see _writeFile, ``explicit``: see _refuse, ``commit``: see _snapshotForRewrite. True
		when the file is written."""
		try:
			return self._writeFile(sync=sync, commit=commit) and self._rewritten()
		except TSVZEncodeError as e:
			self._refuse('hardMapToFile()', e, explicit)
		except Exception as e:
			self._failed('rewrite', f"Failed to write at hardMapToFile() to {self._fileName}: {e}")
			self.deSynced = True
		return False
	
	def mapToFile(self):
		if self._spec:
			_warnOnce(self, 'mapToFile()', self._fileName,
					  'mapToFile() is ignored for .tsvz; behaviour undefined; use scrubTabularFile for archival compaction',
					  self.teeLogger)
			return self
		self._call(self._mapToFile, False, True)
		return self

	def _mapToFile(self, sync=False, explicit=False):
		"""mapToFile(); rewrite() calls it with ``sync`` (see _writeFile), ``explicit``: see _refuse. True when the file
		is written."""
		if _isCompressedFile(self._fileName):
			return self._hardMapToFile(sync, explicit)  # a compressed file is rewritten whole; it reports its own errors
		try:
			return self._writeFile(sync=sync) and self._rewritten()
		except TSVZEncodeError as e:
			self._refuse('mapToFile()', e, explicit)
			return False
		except Exception as e:
			if self._failed('rewrite', f"Failed to write at mapToFile() to {self._fileName}: {e}", retry=False):
				self.__teePrintOrNot("Trying failback hardMapToFile()")
			self.deSynced = True
			return self._hardMapToFile(sync, explicit)

	def checkExternalChanges(self):
		# (no early return when deSynced: the writer thread's tick may be reading the file as another process's append lands,
		# and clear it on the bytes it saw; a caller's check then looks at the file itself)
		if not self.monitor_external_changes:
			return self
		if self._spec:
			if self._specLoaded and self._specExternalChanged():
				self.deSynced = True
				if self.verbose:
					self.__teePrintOrNot(f"External changes detected in {self._fileName}")
			return self
		self._call(self._checkExternalChanges)
		return self

	def _checkExternalChanges(self):
		"""checkExternalChanges() for a legacy file, on the writer thread: never in the middle of this object's own write."""
		realExternalFileUpdateTime = getFileUpdateTimeNs(self._fileName)
		if self._externalChange():
			self.deSynced = True
			if self.verbose:
				self.__teePrintOrNot(f"External changes detected in {self._fileName}")
		elif self.externalFileUpdateTime > realExternalFileUpdateTime:
			self.externalFileUpdateTime = realExternalFileUpdateTime
			if self.verbose:
				self.__teePrintOrNot(f"Time anomalies detected in {self._fileName}, resetting externalFileUpdateTime")

	def _call(self, fn, *args):
		"""``fn(*args)``, run on the store's writer thread while this one waits: a KeyboardInterrupt stops the wait, never
		the write. Run here on that thread itself, or when it is not running (after close, in a forked child)."""
		if self.appendThread is threading.current_thread() or not self._workerRuns():
			return fn(*args)
		self._memLock.letGo()  # (no section waits for the writer thread, which may need the lock: a hold is a Ctrl-C's)
		job = [fn, args, _Flag(), None, None]
		self._requests.append(job)
		self._wake.set()
		while not _waited(job[2], 0.1):  # in steps: a Ctrl-C stops the wait on every platform
			if not self._workerRuns():  # it stopped before it took the job
				try:
					self._requests.remove(job)
				except ValueError:
					continue
				return fn(*args)
		if job[4] is not None:
			raise job[4]
		return job[3]

	def _workerRuns(self):
		"""True while the writer thread runs and takes jobs: until the end of _appendWorker, not Thread.is_alive() (on
		Python 3.9 to 3.12 a KeyboardInterrupt in Thread.join() marks a running thread stopped); False in a forked
		child, and once the exit has let go of the store (_letGo): then every write runs in the thread that asks for
		it, which the interpreter waits for, and nothing waits for the writer thread, which may wait to read."""
		return not self._letGo and not self._workerDone.is_set() and self._workerPid == os.getpid()

	def _unattended(self):
		"""True on the writer thread once the exit has let go of the store: it starts no write of its own (its tick's
		commits and rewrites, its last commit), and one that was waiting for the file lock writes nothing once it gets
		it, so the interpreter's end never cuts a write short."""
		return self._letGo and threading.current_thread() is self.appendThread

	def _startWrite(self):
		"""Note a write job or a write starting, so the exit waits for it (_atExit); False, and nothing noted, on the
		writer thread once the exit has let go of the store (_unattended). A write another thread asks for after that
		runs in that thread (_workerRuns) and is written."""
		with self._memLock:
			if self._unattended():
				return False
			self._writing += 1
			return True

	def _endWrite(self):
		with self._memLock:
			self._writing -= 1

	def _runRequests(self):
		"""Run, on the writer thread, the jobs _call handed it, in order. A job that writes (all but reload's and
		checkExternalChanges') is not started once the exit has let go of the store."""
		while True:
			try:
				job = self._requests.popleft()
			except IndexError:
				return
			writes = job[0] not in (self._reload, self._specReload, self._checkExternalChanges)
			try:
				if writes and not self._startWrite():
					job[3] = None
				else:
					try:
						job[3] = job[0](*job[1])
					finally:
						if writes:
							self._endWrite()
			except BaseException as e:
				job[4] = e
			job[2].set()

	def _appendWorker(self):
		try:
			self._appendWork()
		finally:
			self._workerDone.set()

	def _appendWork(self):
		while not self.shutdownEvent.is_set():
			self._wake.clear()
			self._runRequests()
			if not self.memoryOnly:
				try:  # nothing stops the thread: the store would stop writing
					self.checkExternalChanges()
					if self._spec:
						self._specSync()
					elif not self._failing('sync'):
						self._rewrite(background=True)  # it waits after a failed rewrite itself, but never to take in a change
					# Also to a file a failed rewrite damaged: the rewrite that makes it again merges them back as they
					# are, in their order among another process's appends (the last writer wins).
					if self._spec or not self._failing('append'):
						self.commitAppendToFile()
				except Exception as e:
					self._failed('sync', f"Failed to write at sync() to {self._fileName}: {e}")
			self._wake.wait(self.append_check_delay)
		self._runRequests()
		if self.verbose:
			self.__teePrintOrNot(f"Append worker for {self._fileName} shut down")
		self.commitAppendToFile()

	def commitAppendToFile(self):
		if self._spec:
			return self._call(self._specCommit)
		return self._call(self._commit)

	def _commit(self):
		"""commitAppendToFile() for a legacy file, under writeLock and the file lock. The rows stay queued until they are
		on disk (a merge keeps memory's row for a queued key). When the file held what _known says, what it appended
		is known too and its time noted; else another process's change is still to be taken in (in a compressed file,
		what it appended is first looked at for damage every read stops at: then the file is made again from memory)."""
		if self._commitOnce() == 'remake':
			self._commitOnce()  # the lock let go of: the file is made again from memory
		return self

	def _rewriteAllowed(self):
		"""False while a rewrite was refused or failed lately (the background writer's back-off), unless the store is
		closing (its last try)."""
		return self._closing or not self._refusalPending() and not self._failing('rewrite')

	def _remakeDue(self):
		"""True when the next commit makes the file again from memory (_remake), unless held back (_rewriteAllowed)."""
		return self._remake and self._rewriteAllowed()

	def _dropQueued(self):
		"""True when a drop item (_DropKey) is queued: the next commit rewrites, as soon as it may (_rewriteAllowed)."""
		with self._memLock:  # (a set or delete queues under it: not while the queue is looked through)
			return any(isinstance(row, _DropKey) for row in self.appendQueue)

	def _commitOnce(self):
		"""_commitOnceStarted, unless the exit has let go of the store (nothing is written)."""
		if not self._startWrite():
			return self
		try:
			return self._commitOnceStarted()
		finally:
			self._endWrite()

	def _commitOnceStarted(self):
		"""_commit(), once: 'remake' when another writer's change it read under the lock is damage (_remakeDue)."""
		if not self.appendQueue:
			return self
		if self.memoryOnly:
			self.appendQueue.clear()
			if self.verbose:
				self.__teePrintOrNot("Memory only mode. Append queue cleared.")
			return self
		missing = not os.path.exists(self._fileName)
		dropping = self._dropQueued()
		if missing or self._remakeDue() or (dropping and self._rewriteAllowed()):
			# Removed (rm, a log rotation): memory is the only copy, so the file is made again from it, as a rewrite
			# makes it, not begun with these rows alone (the next read would then drop every other row from memory).
			# Damaged where every read stops (_remake): made again too; a rewrite refused, these rows are appended.
			# A drop item says what no line can: the rewrite takes its key's lines out (and writes the queue).
			if self._hardMapToFile(sync=self.monitor_external_changes if dropping and not missing else True, commit=True):
				self._worked('append')
				return self
			if missing:
				return self
			self.dirty = True  # failed or refused: the rewrite that gets through makes the file again
		with self.writeLock:
			try:
				if self.verbose:
					self.__teePrintOrNot(f"Commiting {len(self.appendQueue)} records to {self._fileName}")
				codec = _fileCodec(self._fileName)
				_requireCodec(codec)
				# The file itself, compressed or not (_appendOpen compresses), created when missing.
				f = self._openLocked(lambda path: open(path, 'a+b'), mode=_APPEND)
			except Exception as e:
				self._failed('append', f"Failed to open file {self._fileName}: {e}", trace=False)
				self.deSynced = True
				return self
			if self._unattended():  # the exit let go of the store (abandoned) while this waited for the lock
				_unlockFile(f)
				f.close()
				return self
			known = None
			try:
				known = None if self._changedSince(f) else self._known
				if known is None and codec and not self._damaged and self.monitor_external_changes:
					# Every read stops at damage in a compressed file (L5): rows appended after it would be read by no
					# one. What another process appended after the bytes this store knows is looked at; damaged (or
					# not after them), the file is read now, under the lock, and made again from memory (_remakeDue).
					found = _readAfter(f, self._known)
					if found is None or _decodeMembers(codec, found[0])[1]:
						try:
							self._syncFrom(f, True)
						except Exception as e:  # the rows are appended all the same
							self._failed('sync', f"Failed to read {self._fileName}: {e}", trace=False)
						if self._remakeDue():
							return 'remake'  # the lock is let go of first (finally)
						known = None if self._changedSince(f) else self._known
				if self._remake and not self._damaged and not self._refusalPending():
					# Appended after damage every read stops at, the file not made again (that failed, or waits out its
					# back-off): memory holds the only good copy, as after a rewrite that fails part way (the merge rule).
					self.dirty = self.deSynced = True
					self._damaged, self._unseen = True, False
				elif self._remake:
					self.dirty = True  # appended after the damage while a remake is refused: the next rewrite makes it again
				if known is None and not self._damaged:
					# Another process's change is not known until it is read: the next tick reads all of it, whatever
					# the file's time says (a time that ticks coarsely may not have moved).
					self._unseen, self.externalFileUpdateTime = True, 0
				with self._memLock:  # a set or delete queues under it: not while the queue is listed
					rows = list(self.appendQueue)
				if not self._closing:
					# Only what comes before a drop item: a row after it must stay after the lines the drop takes out.
					rows = list(itertools.takewhile(lambda row: not isinstance(row, _DropKey), rows))
				chunks = []
				for row in rows:
					if isinstance(row, _DropKey):
						# The store's last try, and its rewrite did not go through: its row goes on being in the file.
						self.__teePrintOrNot(f"The delete of {row[0]!r} in {self._fileName} is not written: a line cannot say it in a "
											 f"one-column file, and the rewrite that would was refused or failed (reported)",'error')
						continue
					try:
						chunks.append(_encodeRow(_sanitize(row,delimiter=self.delimiter),self.delimiter,self.encoding,self.errors,self.header)+b'\n')
					except TSVZEncodeError as e:
						# A row changed in place after it was set. It is not written, but it must not take
						# the good rows with it: those are written. A rewrite stores it once it can be.
						self.dirty = True
						self.__teePrintOrNot(f"Refused at commitAppendToFile() to {self._fileName}: {e}; the row is not appended, the others are",'error')
				if chunks:
					# An existing empty file (a rewrite cut short, of this object's or another's) gets the header first,
					# as creating it writes it; L1: a hand-edited last line is terminated instead of glued onto.
					reporter = _Reporter(self._fileName, self.teeLogger)
					written = []
					try:
						_appendOpen(f, b''.join(chunks), codec, (lambda: self._headerLine(f)) if any(self.header) else None, 'newline',
									reporter, written=written)
						_fsync(f.fileno())  # a sync that fails fails the append: the rows stay queued
					finally:
						reporter.flush()
					if known is not None and stat.S_ISREG(os.fstat(f.fileno()).st_mode):  # /dev/null holds none of it
						# What it wrote, not what the file now holds: another writer's append that lands meanwhile
						# (one that takes no lock) is still to be read, and one that landed first fails the next look.
						digest = known[2].copy()
						digest.update(written[0])
						self._known = (_fileIdentity(os.fstat(f.fileno())), known[1] + len(written[0]), digest)
						self.externalFileUpdateTime = getFileUpdateTimeNs(self._fileName)
				with self._memLock:
					for _ in range(len(rows)):
						self.appendQueue.popleft()  # only those listed: rows queued since stay
				if chunks:
					self._worked('append')
					if self.verbose:
						self.__teePrintOrNot(f"Records commited to {self._fileName}")
			except Exception as e:
				if known is not None and os.fstat(f.fileno()).st_size == known[1]:
					# The append was cut off again (_appendOpen): the file holds what it did, and its time is this
					# store's own, not another process's change.
					self.externalFileUpdateTime = getFileUpdateTimeNs(self._fileName)
				self._failed('append', f"Failed to write at commitAppendToFile to {self._fileName}: {e}")
				self.deSynced = True
			finally:
				_unlockFile(f)
				f.close()
		return self
	
	def stopAppendThread(self):
		try:
			# Once the exit let go of the store, a close() (another exit handler's) still writes what is queued, here.
			if not self.shutdownEvent.is_set() or self._letGo:
				if self._spec:
					self._specSync()
				else:
					self._closing = True
					self._reported = {}  # a failure that lasts to the end is reported once more: nothing tries after it
					try:
						# A damaged file, or one another writer left damaged that rows went after (_remake and dirty), is
						# tried once more, however lately a rewrite was refused (no failed one's back-off is waited out
						# here either): memory holds the only good copy, and goes with the store. Over damage no row went
						# after, only an explicit call rewrites: the bytes past it hold rows no read reached.
						self.rewrite(force=self.rewrite_on_exit and not self._remake)  # Ensure any final sync operations are performed
						if self._letGo:
							self._commit()  # (the writer thread appends nothing more: the queued rows, here)
					except BaseException:
						self._closing = self._exiting  # a Ctrl-C stopped the wait: the store runs on, but not at exit
						raise
			self._finish()
			if self.verbose:
				self.__teePrintOrNot(f"Append thread for {self._fileName} stopped")
		except Exception as e:
			self.__teePrintOrNot(f"Failed to stop append thread for {self._fileName}: {e}",'error')
			import traceback
			self.__teePrintOrNot(traceback.format_exc(),'error')

	def _finish(self):
		"""Stop the writer thread once it has done its jobs and appended the queue; forget the store (_LIVE_STORES)."""
		self.shutdownEvent.set()
		self._wake.set()
		if self.appendThread is not threading.current_thread():
			self._memLock.letGo()  # (as _call)
			while self._workerRuns():
				_waited(self._workerDone, 0.1)  # in steps: a Ctrl-C stops the wait on every platform (not join(): _workerRuns)
		_LIVE_STORES.pop(id(self), None)

	def _atExit(self):
		"""What _stopLiveStores does with a store left running at exit. A store with nothing to write (nothing queued, no
		write job handed to its writer thread or under way there; a legacy one also not dirty, damaged or to be made
		again, no rewrite_on_exit) is let go of without waiting for that thread, a daemon, which may be waiting for another
		process's lock to read the file: a program whose store waits in its load ends at one Ctrl-C. From then on the
		thread starts no write (_startWrite), so none is cut short as the interpreter ends."""
		self._exiting = True
		with self._memLock:
			if self._spec:  # (a .tsvz store is never rewritten at exit: only its queue is written)
				self._letGo = not (self.appendQueue or self._requests or self._writing)
			else:
				self._letGo = not (self.appendQueue or self.dirty or self._damaged or self._remake or self.rewrite_on_exit
								   or self._requests or self._writing)
		if self._letGo:
			self.shutdownEvent.set()
			self._wake.set()
			_LIVE_STORES.pop(id(self), None)
			return None
		return self.stopAppendThread()
	
	def get_file_obj(self,modes = 'ab'):
		"""The store's file opened in ``modes`` and locked, with writeLock held until release_file_obj() (3.39 API; the
		store's own writes do not use it). 'wb' empties it once locked; for a compressed file 'w' and 'a' modes give
		a writer through its codec over the locked file (a new member at the end for 'a'). None when it cannot be opened or locked (reported).

		'a' modes take the append lock, so on Linux other processes' readers go on while it is held (with flock,
		elsewhere, they wait); other modes take the rewrite lock, which readers wait for (_lockFile). The calling thread reads the file (readTabularFile) without
		waiting for its own lock, but must not wait on another store of the same file (its reload(), close() or a
		write) while it holds it: that store's thread waits for this lock, for ever."""
		self.writeLock.acquire()
		file = locked = None
		try:
			if 'w' in modes:
				_requireCodec(_fileCodec(self._fileName))
				file = _openUntruncated(self._fileName)
			elif modes == 'a+b':
				file = open(self._fileName, 'a+b')
			elif 'a' in modes and '+' not in modes and _fileCodec(self._fileName):
				_requireCodec(_fileCodec(self._fileName))
				file = open(self._fileName, 'ab')  # the codec writes into this locked file, a new member at its end
			else:
				file = openFileAsCompressed(self._fileName, mode=modes, encoding=self.encoding, teeLogger=self.teeLogger)
			_lockFile(file, mode=_APPEND if 'a' in modes else _REWRITE)
			locked = file
			if 'w' in modes:
				file.truncate(0)
				file = _compressedWriter(self._fileName, file) or file
			elif 'a' in modes and '+' not in modes and _fileCodec(self._fileName):
				raw = file
				file = _compressedWriter(self._fileName, raw)  # (release_file_obj closes it, trailer and all, before the unlock)
				if 'b' not in modes:
					file = io.TextIOWrapper(file, encoding=self.encoding)
					file._tsvzRaw = raw
			if self.verbose:
				self.__teePrintOrNot(f"File {self._fileName} locked with mode {modes}")
			return file
		except BaseException as e:
			if locked is not None:
				_unlockFile(locked)  # (else _HELD keeps its descriptor, and a later file with the same number looks locked)
			if file is not None:
				try:
					file.close()  # an unlocked file is never handed out
				except Exception:
					pass
			self.writeLock.release()
			if not isinstance(e, Exception):
				raise
			self._failed('append' if 'a' in modes else 'rewrite', f"Failed to open file {self._fileName}: {e}", trace=False)
			return None

	def release_file_obj(self,file):
		"""Flush, sync, unlock and close ``file`` from get_file_obj(), note the file's time as this object's own (what was
		written through it is not known: _known), and let go of writeLock."""
		if file is None or not self.writeLock.locked():
			return
		raw = getattr(file, '_tsvzRaw', file)
		try:
			if raw is not file:
				file.close()  # the codec's writer writes all it holds, and its trailer, into the file
			raw.flush()
			_fsync(raw.fileno())
		except Exception as e:
			self.__teePrintOrNot(f"Failed to release file {self._fileName}: {e}",'error')
		finally:
			self.externalFileUpdateTime = getFileUpdateTimeNs(self._fileName)  # noted before the unlock
			self._known = None
			_unlockFile(raw)
			try:
				_closeUnflushed(raw)
			finally:
				self.writeLock.release()
		if self.verbose:
			self.__teePrintOrNot(f"File {self._fileName} unlocked / released")


class TSVZedLite(MutableMapping):
	"""
	A mutable mapping class that provides a dictionary-like interface to a Tabular (TSV by default) file.
	TSVZedLite stores key-value pairs where each row in the file represents an entry, with the first
	column serving as the key. The class maintains an in-memory index of file positions for efficient
	random access while keeping the actual data on disk.
	TSVZedLite is designed for light memory footprint and forgoes some features from TSVZed, Notably,
	- Does not support simultaneous multi-process access.
	- Does not support compressed file formats.
	- Does not support automatic file rewriting on load / exit / periodically.
	- Does not support append worker thread for background writes.
	- Does not support external file change monitoring.
	- Does not support in-place updates; updates are append-only (a delete in a one-column file takes the key's lines
	  out instead, a whole rewrite each, and reads the file again).
	- Does not support logging via teeLogger.
	- Does not support move_to_end method.
	- Does not support in-memory only mode. ( please just use a dict )
	- Does not lock the file for its reads and appends (a .tsvz write, and a delete in a legacy file whose column count is
	  not known to be two or more, take the exclusive lock other writers take, the delete to look at the file's first
	  lines). Its appends to a legacy file, the header it writes first to an empty one among them, assume it is the
	  only writer.
	- Does not track last update times.
	
	However, it may be preferred in scenarios when:
	- Memory usage needs to be minimized.
	- Working with extremely large datasets where loading everything into memory is impractical.
	- Simplicity and ease of use are prioritized over advanced features.
	- The dataset is primarily write-only with infrequent reads.
	- The application can tolerate the lack of concurrency control. (single process access only)
	- Underlying file system is fast and can do constant time random seek (e.g., SSD).

	Note: It is possible to load a custom dict like object for indexes (like TSVZed or pre-built dict)
	to avoid reading the entire data file to load the indexes at startup. 
	Index consistency is not enforced in this case.
	Will raise error if mismatch happen (only checkes key exist in file) and strict mode is enabled.
	If using an external file-backed Index. This can function similar to a key-value store (like nosql).

	Parameters
	----------
	fileName : str
		Path to the Tabular file to read from or create.
	header : str, optional
		Header row for the file. Can be a delimited string or empty string (default: '').
	createIfNotExist : bool, optional
		If True, creates the file if it doesn't exist (default: True).
	verifyHeader : bool, optional
		If True, verifies that the file header matches the provided header (default: True).
	verbose : bool, optional
		If True, prints detailed operation information to stderr (default: False).
	encoding : str, optional
		Character encoding for the file (default: 'utf8').
	delimiter : str, optional
		Field delimiter character. If Ellipsis (...), automatically detects from filename (default: ...).
	defaults : str, list, or None, optional
		Default values for columns. Can be a delimited string, list, or None (default: None).
	strict : bool, optional
		If True, enforces strict column count validation and raises errors on mismatches (default: True).
	correctColumnNum : int, optional
		Expected number of columns. -1 means auto-detect (default: -1).
	indexes : dict, optional
		Pre-existing index dictionary mapping keys to file positions (default: ...).
	fileObj : file object, optional
		Pre-existing file object to use (default: ...).
	errors : str, optional
		What to do with text the encoding cannot hold, such as a lone surrogate from a non-UTF-8
		file name (os.fsdecode). 'strict' (default) raises a ValueError (TSVZEncodeError) naming it,
		from the call that gave the text (setting an item) or from the constructor (a header or
		defaults), before anything is stored or written. 'replace' writes '?' for it, as 3.39 did.
	Attributes
	----------
	version : str
		Version identifier for the TSVZedLite format.
	indexes : dict
		Dictionary mapping keys to their file positions (or in-memory data for keys starting with '#').
	fileObj : file object
		Binary file object for reading/writing the underlying file.
	defaults : list
		List of default values for columns, with DEFAULTS_INDICATOR_KEY as the first element.
	correctColumnNum : int
		The validated number of columns per row.
	Notes
	-----
	- Keys starting with '#' are stored in memory only and not written to file.
	- The special key DEFAULTS_INDICATOR_KEY is used to store and retrieve default column values.
	- Empty values in rows are automatically filled with defaults if available.
	- The class implements the MutableMapping interface, providing dict-like operations.
	- File operations are buffered and written immediately (append-only for updates).
	- Deleted entries are marked by writing a row with only the key (empty values), except in a one-column file, where
	  that is a row: there the key's lines are taken out of the file.
	Examples
	--------
	>>> db = TSVZedLite('data.tsv', header='id\tname\tage')
	>>> db['user1'] = ['user1', 'Alice', '30']
	>>> print(db['user1'])
	['user1', 'Alice', '30']
	>>> del db['user1']
	>>> 'user1' in db
	False
	See Also
	--------
	collections.abc.MutableMapping : The abstract base class that this class implements.
	"""
	_appendWidth = 0  # what a row it appends is padded to, from line 1 (_legacyAppendWidth)

	#['__new__', '__repr__', '__hash__', '__lt__', '__le__', '__eq__', '__ne__', '__gt__', '__ge__', '__iter__', '__init__',
	#  '__or__', '__ror__', '__ior__', '__len__', '__getitem__', '__setitem__', '__delitem__', '__contains__', '__sizeof__',
	#  'get', 'setdefault', 'pop', 'popitem', 'keys', 'items', 'values', 'update', 'fromkeys', 'clear', 'copy', '__reversed__',
	#  '__class_getitem__', '__doc__']
	def __init__ (self,fileName,header = '',createIfNotExist = True,verifyHeader = True,
					verbose = False,encoding = 'utf8',
					delimiter = ...,defaults = None,strict = True,correctColumnNum = -1,
					indexes = ..., fileObj = ..., errors = 'strict'
					):
		self._constructed = False  # setDefaults() writes a #_defaults_# marker only once this is True
		self._spec = _isSpecPath(fileName)
		self.version = version
		self.strict = strict
		self._fileName = fileName
		self.errors = _checkErrors(errors)
		if self._spec:
			reporter = _Reporter(fileName)
			self.delimiter, encoding = _specOptions(fileName, delimiter, encoding, reporter)
			reporter.flush()
		else:
			self.delimiter = get_delimiter(delimiter,file_name=fileName)
		self.encoding = encoding
		self.setDefaults(defaults)
		self._constructorDefaults = list(self.defaults)
		self.header = _formatHeader(header,verbose = verbose,delimiter=self.delimiter)
		_checkRow(self.header, self.delimiter, self.encoding, self.errors, what='header')  # before a file is created
		self.correctColumnNum = correctColumnNum
		self._givenColumns = correctColumnNum
		self.createIfNotExist = createIfNotExist
		self.verifyHeader = verifyHeader
		self.verbose = verbose
		if self._spec:
			self._specInit(indexes, fileObj)
		else:
			# L3: offsets cannot address a compressed file; keep its rows in memory.
			self._inMemoryRows = _isCompressedFile(fileName)
			if self._inMemoryRows:
				_warnOnce(self, 'compressed', fileName, 'compressed file: TSVZedLite keeps rows in memory and appends new compressed members')
			if indexes is ...:
				self.indexes = dict()
				self.load()
			else:
				self.indexes = indexes
			if fileObj is ...:
				# 'r+b' requires the file to exist. When indexes were supplied
				# externally, load() (which would have created it) is skipped, so
				# ensure the file exists first -- creating it when allowed, or
				# raising a clear FileNotFoundError instead of a cryptic open error.
				_verifyFileExistence(self._fileName, createIfNotExist = self.createIfNotExist,
									  header = self.header, encoding = self.encoding,
									  strict = self.strict, delimiter = self.delimiter, errors = self.errors)
				self.fileObj = None if self._inMemoryRows else open(self._fileName,'r+b')
			else:
				self.fileObj = fileObj
		_LIVE_STORES[id(self)] = self  # closed at exit
		self._constructed = True

	# Implement custom methods just for TSVZedLite
	def getResourceUsage(self,return_dict = False):
		return get_resource_usage(return_dict = return_dict)

	@property
	def dialect(self):
		"""'tsvz' for tsvz-spec-v1 files (.tsvz/.csvz/.nsvz/.psvz), 'tsv' for 3.39-format files."""
		return 'tsvz' if self._spec else 'tsv'

	def _nameKey(self, key):
		"""The key a requested ``key`` names: the live key equal to it, else ``key`` right-stripped (spec §20.2.7).

		The fallback strips every trailing whitespace whatever the stripping
		marker says, as ``__setitem__`` strips every key it writes.
		"""
		key = str(key)
		return key if key in self.indexes else key.rstrip()

	def __contains__(self, key):
		if self._spec:
			return self._nameKey(key) in self.indexes
		return MutableMapping.__contains__(self, key)

	def get(self, key, default=None):
		if self._spec:
			return self[key] if key in self else default
		return MutableMapping.get(self, key, default)

	def setdefault(self, key, default=None):
		if self._spec:
			if key not in self:
				self[key] = default
			return self[key]
		return MutableMapping.setdefault(self, key, default)

	def _specInit(self, indexes, fileObj):
		self._inMemoryRows = False
		self._specState = _SpecState(self.defaults)
		self._transOffsets = []
		self._transStates = []
		self._activePath = self._fileName
		self._pendingDefaults = False
		if indexes is ...:
			self.indexes = dict()
			self.load()
		else:
			self.indexes = indexes
			# One scan for the marker state, the transitions and the active part.
			self._specLiteLoad({})
		if fileObj is ...:
			self.fileObj = None if self._inMemoryRows else open(self._activePath, 'r+b')
		else:
			self.fileObj = fileObj
		self._specFlushPendingDefaults()

	def _specLiteLoad(self, target):
		"""Load key -> offset (active, uncompressed part) or key -> row (elsewhere) into ``target``."""
		reporter = _Reporter(self._fileName)
		try:
			if not _specEnsureStore(self._fileName, self.createIfNotExist, self.header, self._constructorDefaults,
									self.strict, None, self.delimiter, self.errors):
				return self
			load = _specLoad(self._fileName, self.delimiter, header=self.header, verifyHeader=self.verifyHeader,
							 strict=self.strict, defaults=self._constructorDefaults,
							 correctColumnNum=self.correctColumnNum, storeOffset=True, taskDic=target,
							 reporter=reporter, verbose=self.verbose)
			unreadable = reporter.incomplete()
		finally:
			reporter.flush()
		self._specState = load.state
		self.defaults = list(load.state.defaults)
		self._activePath = load.active
		self._inMemoryRows = bool(_parsePartName(load.active).codec)
		self._transOffsets = [offset for offset, _ in load.transitions]
		self._transStates = [rowState for _, rowState in load.transitions]
		if self.correctColumnNum == -1:
			self.correctColumnNum = load.correctColumnNum
		held = sum(1 for value in target.values() if isinstance(value, list))
		if held:
			_warnOnce(self, 'held', self._fileName, f'{held} keys from older or compressed parts are held in memory (offsets cannot address them)')
		if not load.state.sawDefaults and any(self._constructorDefaults[1:]) and not unreadable:
			self._pendingDefaults = True
		return self

	def _specFlushPendingDefaults(self):
		"""Persist constructor defaults the store never declared (design §5.3)."""
		if self._pendingDefaults:
			self._pendingDefaults = False
			self._specWriteMarker(DEFAULTS_INDICATOR_KEY, list(self._constructorDefaults))

	def _specReadAt(self, pos, key=...):
		"""Read the row at ``pos`` under the marker state in force there (found by bisection)."""
		self.fileObj.seek(pos)
		raw = self.fileObj.readline()
		index = bisect.bisect_right(self._transOffsets, pos) - 1
		rowState = self._transStates[index] if index >= 0 else self._specState.rowState
		state = _SpecState.fromRowState(rowState)
		reporter = _Reporter(self._fileName)
		record = None
		if raw.endswith(b'\n'):
			record = _specProcessRecord(_decodeLine(raw, reporter, f'offset {pos}'), state, self.delimiter, reporter, None)
		reporter.flush()
		segments = []
		if record and record[1] is not None:
			segments = record[1]
			target = max(self.correctColumnNum, len(state.defaults))
			if len(segments) < target:
				segments.extend(state.defaults[j] if j < len(state.defaults) else '' for j in range(len(segments), target))
		if self.verbose:
			eprint(f"Read at position {pos}: {segments}")
		if key is not ...:
			if not segments:
				eprint(f"Error: No segments found at position {pos}")
			elif segments[0] != key:
				eprint(f"Warning: Key mismatch at position {pos}: expected {key}, got {segments[0]}")
			else:
				return segments
			if self.strict:
				eprint("Error: Key mismatch and strict mode enabled. Raising KeyError.")
				raise KeyError(key)
			else:
				eprint("Continuing despite key mismatch due to non-strict mode. Expect errors!")
		return segments

	def _specSetItem(self, key, value):
		"""__setitem__ for .tsvz: as TSVZed's, written immediately."""
		key, value = _specCells(key, value, self.delimiter)
		if not key:
			eprint('Error: Key cannot be empty')
			return
		if _MARKER_RE.match(key):
			self._specWriteMarker(key, value)
			return
		if len(value) == 1:
			del self[key]
			return
		_checkRow(value, self.delimiter, self.encoding, self.errors, self.header)  # before any state changes
		if self.strict and self.correctColumnNum > 0 and len(value) != self.correctColumnNum:
			eprint(f"Error: Value {value} does not have the correct number of columns: {self.correctColumnNum}. Refuse adding key...")
			return
		_specFill(value, self.defaults)
		offset = self._specWrite(_specFormatRecord(value, self.delimiter))
		self.correctColumnNum = _specPadRow(value, self._specState, self.correctColumnNum if self.correctColumnNum > 0 else -1)
		self.indexes[key] = value if offset is None else offset  # a row of a compressed part is held, as a read holds it

	def _specWriteMarker(self, key, value):
		_checkRow(value, self.delimiter, self.encoding, self.errors, self.header,
				  'defaults' if key == DEFAULTS_INDICATOR_KEY else 'row')  # before any state changes
		keyLower = key.lower()
		if keyLower in _SPEC_MARKER_KEYS:
			reporter = _Reporter(self._fileName)
			applied = _specApplyMarker(self._specState, keyLower, value[1:], reporter, None)
			reporter.flush()
			if not applied:
				return
			self.defaults = list(self._specState.defaults)
		self._specWrite(_specFormatRecord(value, self.delimiter, marker=True))

	def _specWrite(self, line):
		"""Append one record (or lines written together) to the active part; return its offset, or None for a compressed part."""
		data = _encodeText(line + '\n', 'utf-8', self.errors)
		reporter = _Reporter(self._activePath)
		try:
			if self.fileObj is None:
				_specAppendPayload(self._activePath, data, reporter, memberSize=self._specState.memberSize)
				return None
			size = _specAppendTo(self.fileObj, self._activePath, data, reporter)[0]
		finally:
			reporter.flush()
		if not self._transStates or self._specState.rowState is not self._transStates[-1]:
			self._transOffsets.append(size)
			self._transStates.append(self._specState.rowState)
		return size

	def setDefaults(self,defaults):
		defaults = _normalizeDefaults(defaults, self.delimiter)
		_checkRow(defaults, self.delimiter, self.encoding, self.errors, what='defaults')
		if self._spec and self._constructed:
			# Spec rows are written sparse and read back under the file's
			# #_defaults_#, so new defaults are written as that marker.
			self._specWriteMarker(DEFAULTS_INDICATOR_KEY, defaults)
			return
		self.defaults = defaults
		if self._constructed:
			self._constructorDefaults = list(defaults)  # (the defaults a whole read starts from)

	def load(self):
		if self._spec:
			if self.verbose:
				eprint(f"Loading {self._fileName}")
			return self._specLiteLoad(self.indexes)
		if self.verbose:
			eprint(f"Loading {self._fileName}")
		# (the read works on self.defaults: a row is read at its offset with the defaults the file ends with; it
		# starts from the store's own, as a fresh open does, not from those the last read left)
		self.defaults = list(self._constructorDefaults)
		lineOne = [None]  # (the line 1 the read met: what every row appended is padded to, _legacyAppendWidth)
		width = _legacyRead(self._fileName, None, self.header, self.createIfNotExist, False, self.verifyHeader, self.verbose,
							self.indexes, self.encoding if self.encoding else None, self.strict, self.delimiter, self.defaults,
							-1, not self._inMemoryRows, self.errors, lineOne)[1]
		if self._givenColumns == -1 and width > 0:
			# (as an open of this file: a one-column file is known from here on, and emptied it stays one column; any
			# other count is read from the rows, not kept from a one-column file this was before a delete or a reload)
			self.correctColumnNum = 1 if width == 1 else -1
		self._appendWidth = _legacyAppendWidth(self.header, lineOne[0], self.delimiter) if self.verifyHeader else 0
		return self

	def _lineOneWidth(self):
		"""_legacyAppendWidth of the file's line 1 as it is now, read (a clear's: load takes it from its own read): 0 without
		a header to verify, or a line 1 to read."""
		if not (any(self.header) and self.verifyHeader):
			return 0
		try:
			line = _legacyFirstLine(self._fileName, self.encoding or 'utf8', None) if os.path.isfile(self._fileName) else ''
		except Exception:  # (damage there: the read reported it)
			return 0
		return _legacyAppendWidth(self.header, line or None, self.delimiter)

	def positions(self):
		return self.indexes.values()

	def reload(self):
		self.indexes.clear()
		return self.load()

	def getListView(self):
		return getListView(self,header=self.header,delimiter=self.delimiter)

	def clear_file(self):
		"""Clear the store. For .tsvz, raise OSError when the clear was refused (the reason is
		reported as a warning); memory is reloaded from the store either way."""
		if self._spec:
			cleared = _specClearTabularFile(self._fileName, header=self.header, verbose=self.verbose, delimiter=self.delimiter,
											errors=self.errors)
			if self.fileObj is not None:
				self.fileObj.close()
			self.indexes.clear()
			self._specLiteLoad(self.indexes)
			self.fileObj = None if self._inMemoryRows else open(self._activePath, 'r+b')
			self._specFlushPendingDefaults()
			if not cleared:
				raise OSError('{} was not cleared: the clear was refused (see the warning above)'.format(self._fileName))
			return self
		if self.verbose:
			eprint(f"Clearing {self._fileName}")
		# Written over the file and cut under its lock, as TSVZed's rewrites are (the file object stays the file's).
		clearTabularFile(self._fileName, header=self.header, verbose=self.verbose,
						 encoding=self.encoding, delimiter=self.delimiter, errors=self.errors)
		if self.fileObj is not None:
			self.fileObj.seek(0, os.SEEK_END)  # (and drops what it had read ahead)
		self._appendWidth = self._lineOneWidth()  # (the line 1 the clear kept)
		return self

	def switchFile(self,newFileName,createIfNotExist = ...,verifyHeader = ...):
		if createIfNotExist is ...:
			createIfNotExist = self.createIfNotExist
		if verifyHeader is ...:
			verifyHeader = self.verifyHeader
		if self.fileObj is not None:
			self.fileObj.close()
		self._fileName = newFileName
		self.correctColumnNum = self._givenColumns  # (the new file's is read from it)
		self._spec = _isSpecPath(newFileName)
		if self._spec:
			self.delimiter = _EXTENSION_DELIMITERS[_parsePartName(newFileName).ext]
			self._specState = _SpecState(self._constructorDefaults)
			self._transOffsets, self._transStates = [], []
			self._activePath = newFileName
			self._pendingDefaults = False
			self.reload()
			self.fileObj = None if self._inMemoryRows else open(self._activePath, 'r+b')
			self._specFlushPendingDefaults()
		else:
			self._inMemoryRows = _isCompressedFile(newFileName)
			self.reload()
			self.fileObj = None if self._inMemoryRows else open(self._fileName,'r+b')
		self.createIfNotExist = createIfNotExist
		self.verifyHeader = verifyHeader
		return self

	# Private methods for reading and writing values for TSVZedLite

	def __headerLine(self):
		"""The header line, encoded: made only when an append writes it to an empty file (_appendOpen); the
		constructor checked it can be."""
		return _encodeRow(_sanitize(self.header,delimiter=self.delimiter),self.delimiter,self.encoding,self.errors,what='header')

	def __writeValues(self,data,headerFirst=True):
		# Encode first: a row that cannot be written must not even terminate the last line.
		encoded = _encodeRow(_sanitize(data,delimiter=self.delimiter),self.delimiter,self.encoding,self.errors,self.header)
		# An empty file gets the header first, as creating it writes it; not for clear_file()'s own. L1: a
		# hand-edited last line is terminated instead of glued onto. TSVZedLite does not lock a legacy file (see
		# the class): it is the only writer, and another process's write between the look and the write is not
		# excluded.
		header = self.__headerLine if headerFirst and any(self.header) else None
		reporter = _Reporter(self._fileName)
		try:
			if self.fileObj is None:
				codec = _fileCodec(self._fileName)
				_requireCodec(codec)
				with open(self._fileName, 'a+b') as f:
					_appendOpen(f, encoded + b'\n', codec, header, 'newline', reporter)
				return list(data)
			write_at = _appendOpen(self.fileObj, encoded + b'\n', None, header, 'newline', reporter)
		finally:
			reporter.flush()
		if self.verbose:
			eprint(f"Wrote {len(encoded) + 1} bytes at position {write_at}")
		return write_at

	def _checkDelete(self, key):
		"""Raise ``TSVZEncodeError`` now when the tombstone of ``key`` could not be written, before memory changes.

		A .tsvz key was checked when it was set, or decoded from UTF-8: its tombstone can always be written.
		"""
		if not self._spec and _mayRaise(self.errors) and (key == DEFAULTS_INDICATOR_KEY or not key.startswith('#')):
			_checkRow([key], self.delimiter, self.encoding, self.errors, self.header)

	def __mapDeleteToFile(self,key):
		if self._spec:
			self._specWrite(_specTombstoneLines(key, self.delimiter, self._specState.strip))
			return
		# Persist a deletion by appending a tombstone (a lone key with no
		# values); on read such a row deletes the key (see _processLine). The
		# key must already have been removed from self.indexes by the caller --
		# this method does NOT touch the index (previously it re-added the key,
		# pointing it at the tombstone line).
		if key == DEFAULTS_INDICATOR_KEY:
			self.defaults = [DEFAULTS_INDICATOR_KEY]
			if self.verbose:
				eprint("Defaults cleared")
			self.__writeValues([key])
			return
		if key.startswith('#'):
			# comment / internal keys are in-memory only; nothing to persist
			if self.verbose:
				eprint(f"Key {key} deleted in memory")
			return
		# (asked of the file unless the program gave a count of two or more: correctColumnNum is only what the last read took)
		if self._givenColumns <= 1 and _legacyDropKeys(self._fileName, [key], self.header, self.verifyHeader,
													   self.encoding, self.delimiter):
			# A lone key is a row in a one-column file: its lines went instead, and every offset after them moved.
			held = [(k, v) for k, v in self.indexes.items() if k.startswith('#') and k != DEFAULTS_INDICATOR_KEY]  # (the rows memory alone holds)
			self.reload()
			self.indexes.update(held)
			if self.fileObj is not None:
				self.fileObj.seek(0, os.SEEK_END)  # (and drops what it had read ahead)
			return
		if self.verbose:
			eprint(f"Writing tombstone line for {key}")
		self.__writeValues([key])  # (not padded to _appendWidth: only a row with a value is)

	def __readValuesAtPos(self,pos,key = ...):
		if self._spec:
			return self._specReadAt(pos, key)
		self.fileObj.seek(pos)
		line = self.fileObj.readline().decode(self.encoding,errors='replace')
		held = {}
		self.correctColumnNum, segments = _processLine(
						line=line,
						taskDic=held,
						correctColumnNum=self.correctColumnNum,
						strict=self.strict,
						delimiter=self.delimiter,
						defaults=self.defaults,
					)
		segments = segments or next(iter(held.values()), [])  # (_processLine returns no row for a lone key in a one-column file)
		if self.verbose:
			eprint(f"Read at position {pos}: {segments}")
		if key is not ...:
			if not segments:
				eprint(f"Error: No segments found at position {pos}")
			elif segments[0] != key:
				eprint(f"Warning: Key mismatch at position {pos}: expected {key}, got {segments[0]}")
			else:
				return segments
			if self.strict:
				eprint("Error: Key mismatch and strict mode enabled. Raising KeyError.")
				raise KeyError(key)
			else :
				eprint("Continuing despite key mismatch due to non-strict mode. Expect errors!")
		return segments

	# Implement basic __getitem__, __setitem__, __delitem__, __iter__, and __len__. needed for MutableMapping
	def __getitem__(self,key):
		key = self._nameKey(key)
		if key not in self.indexes:
			if key == DEFAULTS_INDICATOR_KEY:
				return self.defaults
			if self._spec and self._specState.returnDefaults:
				# Spec §14.5: a missing key reads as the active defaults.
				row = [key] + list(self.defaults[1:])
				if self.correctColumnNum > len(row):
					row += [''] * (self.correctColumnNum - len(row))
				return row
			raise KeyError(key)
		pos = self.indexes[key]
		if isinstance(pos, list):
			# L3: rows held in memory ('#' keys, compressed files, older parts).
			return pos
		return self.__readValuesAtPos(pos,key)

	def __setitem__(self,key,value):
		if self._spec:
			return self._specSetItem(key, value)
		key = str(key).rstrip()
		if not key:
			eprint('Error: Key cannot be empty')
			return
		if isinstance(value,str):
			value = value.split(self.delimiter)
		# sanitize the value
		value = [str(s).rstrip() if s else '' for s in value]
		# the first field in value should be the key
		# add it if it is not there
		if not value or value[0] != key:
			value = [key]+value
		# verify the value has the correct number of columns
		if self.correctColumnNum != 1 and len(value) == 1:
			# a lone key (no following values) means delete the key, per the
			# TSVZ spec. del is a no-op when the key is absent (tolerated).
			del self[key]
			return
		elif self.correctColumnNum > 0:
			if len(value) > self.correctColumnNum:
				value = _fitRow(value, self.correctColumnNum)  # (its empty cells past the column count go)
			if len(value) != self.correctColumnNum:
				if self.strict and len(value) < self.correctColumnNum:  # (5.5: a wider row is kept whole)
					eprint(f"Error: Value {value} does not have the correct number of columns: {self.correctColumnNum}. Refuse adding key...")
					return
				elif self.verbose:
					eprint(f"Warning: Value {value} does not have the correct number of columns: {self.correctColumnNum}, correcting...")
				value = _fitRow(value, self.correctColumnNum)  # (padded; a wider row is written whole)
			newColumnNum = None
		else:
			newColumnNum = len(value)  # taken once the row is known to be writable
		if self._appendWidth > len(value) and any(value[1:]):
			value = _fitRow(value, self._appendWidth)  # (as wide as line 1 counts its cells: _legacyAppendWidth)
		if self.defaults and len(self.defaults) > 1:
			for i in range(1,len(value)):
				if not value[i] and i < len(self.defaults) and self.defaults[i]:
					value[i] = self.defaults[i]
					if self.verbose:
						eprint(f"    Replacing empty value at {i} with default: {self.defaults[i]}")
		if (len(value) > 1 and not any(value[1:]) and key != DEFAULTS_INDICATOR_KEY
				and not key.startswith('#')):
			# L2: on disk this row is a delete; make memory agree with the file.
			_warnOnce(self, 'empty-row', self._fileName,
					  f'in .tsv files a row whose values are all empty is a delete; deleted {key!r} (use .tsvz to store empty rows)')
			del self[key]
			return
		if key == DEFAULTS_INDICATOR_KEY or not key.startswith('#'):
			# What is written is what is checked: the row as cut to the column count and filled with the defaults,
			# and before the index or the column count change.
			_checkRow(value, self.delimiter, self.encoding, self.errors, self.header, what='defaults' if key == DEFAULTS_INDICATOR_KEY else 'row')
		if newColumnNum is not None:
			self.correctColumnNum = newColumnNum
		if key == DEFAULTS_INDICATOR_KEY:
			self.defaults = value
			if self.verbose:
				eprint(f"Defaults set to {value}")
			return
		elif key.startswith('#'):
			if self.verbose:
				eprint(f"Key {key} updated in memory (data in index) as it starts with #")
			self.indexes[key] = value
			return
		if self.verbose:
			eprint(f"Writing {key}: {value}")
		self.indexes[key] = self.__writeValues(value)
		
	def __delitem__(self,key):
		key = self._nameKey(key)
		if self._spec:
			if _MARKER_RE.match(key):
				# A lone marker key resets that marker (spec §12.1).
				self._specWriteMarker(key, [key])
				return
			if key not in self.indexes:
				if self.verbose:
					eprint(f"Key {key} not found")
				return
			self._checkDelete(key)
			self.indexes.pop(key, None)
			self.__mapDeleteToFile(key)
			return
		if key == DEFAULTS_INDICATOR_KEY:
			self.__mapDeleteToFile(key)
			return
		if key not in self.indexes:
			if self.verbose:
				eprint(f"Key {key} not found")
			return
		self._checkDelete(key)
		# Persist the deletion first: a delete that fails (an OSError) leaves the key in memory, as in the file.
		self.__mapDeleteToFile(key)
		self.indexes.pop(key,None)

	def __iter__(self):
		return iter(self.indexes)

	def __len__(self):
		return len(self.indexes)

	# Implement additional methods for dict like interface (order of function are somewhat from OrderedDict)
	def __reversed__(self):
		return reversed(self.indexes)

	def clear(self):
		# clear the dictionary and update the file
		self.indexes.clear()
		self.clear_file()
		return self

	def popitem(self, last=True,return_pos = False):
		if last:
			key, pos = self.indexes.popitem()
			self.indexes[key] = pos  # (stays until the delete is written: a delete that fails leaves it, as the file does)
		else:
			try:
				key = next(iter(self.indexes))
			except StopIteration:
				raise KeyError("popitem(): dictionary is empty")
			pos = self.indexes[key]
		self._checkDelete(key)
		if return_pos or isinstance(pos, list):
			value = pos
		else:
			value = self.__readValuesAtPos(pos,key)
		self.__mapDeleteToFile(key)
		self.indexes.pop(key, None)
		return key, value

	__marker = object()
	def pop(self, key, default=__marker, return_pos = False):
		key = self._nameKey(key)
		if key in self.indexes:
			self._checkDelete(key)
		try:
			pos = self.indexes[key]
		except KeyError:
			if default is self.__marker:
				raise KeyError(key)
			elif default is ...:
				return self.defaults
			return default
		if return_pos or isinstance(pos, list):
			value = pos
		else:
			value = self.__readValuesAtPos(pos,key)
		self.__mapDeleteToFile(key)
		self.indexes.pop(key, None)
		return value

	def __sizeof__(self):
		sizeof = sys.getsizeof
		size = sizeof(super()) + sizeof(True) * 6  # for the booleans / integers
		size += sizeof(self._fileName)
		size += sizeof(self.header)
		size += sizeof(self.encoding)
		size += sizeof(self.delimiter)
		size += sizeof(self.defaults)
		size += sizeof(self.indexes)
		size += sizeof(self.fileObj)
		return size
	
	def __repr__(self):
		return f"""TSVZed at {hex(id(self))}(
file_name:{self._fileName}
index_count:{len(self.indexes)}
header:{self.header}
correctColumnNum:{self.correctColumnNum}
createIfNotExist:{self.createIfNotExist}
verifyHeader:{self.verifyHeader}
strict:{self.strict}
delimiter:{self.delimiter}
defaults:{self.defaults}
verbose:{self.verbose}
encoding:{self.encoding}
file_descriptor:{self.fileObj.fileno() if self.fileObj is not None else None}
)"""

	def __str__(self):
		return f"TSVZedLite({self._fileName})"
	
	def __reduce__(self):
		'Return state information for pickling'
		# Return minimal state needed to reconstruct
		return (
			self.__class__,
			(self._fileName, self.header, self.createIfNotExist, self.verifyHeader,
			 self.verbose, self.encoding, self.delimiter, self.defaults, self.strict, 
			 self.correctColumnNum, ..., ..., self.errors),
			None,
			None,
			None
		)
	def copy(self):
		'Return a shallow copy of the ordered dictionary.'
		new = self.__class__(
			self._fileName,
			self.header,
			self.createIfNotExist,
			self.verifyHeader,
			self.verbose,
			self.encoding,
			self.delimiter,
			self.defaults,
			self.strict,
			self.correctColumnNum,
			self.indexes,
			self.fileObj,
			self.errors,
		)
		eprint("""
		Warning: Copying TSVZedLite will share the same file object and indexes. 
		Changes in one will affect the other.
		There is likely very little reason to copy a TSVZedLite instance unless you are immadiately then calling switchFile() on it.
		""")
		return new

	@classmethod
	def fromkeys(cls, iterable, value=None,fileName = None,header = '',createIfNotExist = True,verifyHeader = True,verbose = False,encoding = 'utf8',
					delimiter = ...,defaults = None,strict = True,correctColumnNum = -1,errors = 'strict'):
		'''Create a new ordered dictionary with keys from iterable and values set to value.
		'''
		self = cls(fileName,header,createIfNotExist,verifyHeader,verbose,encoding,delimiter,defaults,strict,correctColumnNum,errors = errors)
		for key in iterable:
			self[key] = value
		return self
	
	def __eq__(self, other):
		if isinstance(other, TSVZedLite):
			eprint("Warning: Comparing two TSVZedLite instances will only compare their indexes. Data content is not compared.")
			return self.indexes == other.indexes
		return super().__eq__(other)
	
	def __ior__(self, other):
		self.update(other)
		return self

	# Implement context manager methods
	def __enter__(self):
		return self
	
	def close(self):
		if self.fileObj is not None:
			self.fileObj.close()
		return self

	_atExit = _finish = close  # what _stopLiveStores does with a store left open at exit

	def __exit__(self,exc_type,exc_value,traceback):
		return self.close()




# ===========================================================================
# Write handler (tsvz-spec-v1 §21): the store engine
# ===========================================================================
#: Bytes before the read offset that must be unchanged for an incremental read.
_SERVE_CHECK_BYTES = 64
#: A directory modified less than this long ago may change again within the same
#: timestamp tick, so its part list is checked on every sync until it is older.
_SERVE_RACY_SECONDS = 2.0


def _partRows(store, reporter):
	"""Rows for ``parts`` (spec §20.2): index, ordinal as the file name spells it, path, flags."""
	if _isSpecPath(store):
		parts, active = _storeParts(store, reporter)
	else:
		parts, active = [store], store  # a loose file is its only part (spec §20.2.4)
	rows = []
	for index, path in enumerate(parts):
		name = _parsePartName(path)
		ordinal = ''
		if name.ordinal is not None:
			base = os.path.basename(path)
			if name.codec:
				base = base[:-len(name.codec) - 1]
			ordinal = base.rpartition('.')[2]
		flags = (['active'] if path == active else []) + ([_CODEC_NAMES.get(name.codec, name.codec)] if name.codec else [])
		rows.append([str(index), ordinal, path, ','.join(flags)])
	return rows


#: Writers not closed yet; ``_serveCloseWriters`` closes them when the interpreter exits.
_SERVE_OPEN_WRITERS = weakref.WeakSet()


def _serveCloseWriters():
	"""At exit: write and fsync what still-open engines have queued (a TSVZClient never closed)."""
	for writer in list(_SERVE_OPEN_WRITERS):
		writer.close()


atexit.register(_serveCloseWriters)


#: A line not read yet (_ServeStore._payload): None is a read that found no line 1.
_UNREAD = object()


class _ServeWriter(object):
	"""The single appender of a store (spec §18.3): each batch of queued payloads is one write.

	``submit`` returns a job; ``wait`` blocks until it is written (and fsync'd, for a ``durable`` one) and raises its
	write error. The thread never takes the store's state lock, and no thread ever waits for a lock another one holds:
	jobs go through a deque, and each says it is done with a _Flag. 5.12's Condition was one lock that every thread
	took, and a Ctrl-C can leave a lock held (bpo-29988, see _Flag): the thread then waited for it for ever, and so did
	the exit's close() (a TSVZClient's, a CLI command's).
	"""

	def __init__(self, store):
		self.store = store
		self.queue = deque()  # the jobs: [payload, durable, done (a _Flag), error]; a barrier's has no payload
		self.wake = _Flag()  # set when a job is queued and when the store closes
		self.busy = False  # True from before the thread takes a batch until its jobs are done (barrier)
		self.stopped = False  # True once the thread takes its last batch (_queue)
		self.unsynced = False
		self.closing = False
		self.failing = None  # the last write error, until a batch is written again
		self.thread = threading.Thread(target=self._run, name='tsvz-writer')
		self.thread.daemon = True
		self.thread.start()
		_SERVE_OPEN_WRITERS.add(self)

	def _queue(self, job):
		"""Hand ``job`` to the thread: False when it had taken its last batch and the job was not in it."""
		self.queue.append(job)
		self.wake.set()
		if self.stopped:
			try:
				self.queue.remove(job)
				return False
			except ValueError:
				pass  # taken: in the last batch
		return True

	def submit(self, payload, durable):
		job = [payload, durable, _Flag(), None]
		if self.closing or not self._queue(job):
			raise RuntimeError('the store is closing')
		return job

	@staticmethod
	def wait(job):
		"""Block until ``job`` is written (a durable one: and fsync'd); raise its write error."""
		job[2].wait()
		if job[3] is not None:
			raise job[3]

	def barrier(self):
		"""Block until everything submitted so far is written (spec §21.8 read-your-writes)."""
		# The queue looked at before busy: a job queued before this that is not in it any more was taken while busy was
		# True, which stays True until the job is done.
		if self.queue or self.busy:
			job = [b'', False, _Flag(), None]  # done once every job before it is
			if self._queue(job):
				job[2].wait()
			else:
				self.thread.join()  # (its last batch: written once it ends)

	def close(self):
		"""Write everything queued, fsync it, and stop the thread."""
		self.closing = True
		self.wake.set()
		self.thread.join()
		_SERVE_OPEN_WRITERS.discard(self)
		self.store = None  # no cycle: a closed store's rows are freed now, not by the collector at exit

	def _run(self):
		while not self.stopped:
			self.wake.wait()
			self.wake.clear()
			# Closing: this batch is the last. A job queued from here on is in it or taken back by its submit (_queue);
			# one queued before is in it.
			self.stopped = self.closing
			self.busy = True
			batch = []
			while True:
				try:
					batch.append(self.queue.popleft())
				except IndexError:
					break
			writes = [job for job in batch if job[0]]
			error = None
			if writes:
				durable = any(job[1] for job in writes)
				try:
					self.store._append(b''.join(job[0] for job in writes), durable)
				except Exception as e:
					error = e
					_warn('TSVZ error: {}: a batch of {} writes failed: {}'.format(self.store.path, len(writes), e),
						  self.store.teeLogger)
				if error is not None:
					self.failing = str(error) or type(error).__name__
				else:
					self.failing = None
					if not durable:
						self.unsynced = True
			for job in batch:
				job[3] = error if job[0] else None
				job[2].set()
			self.busy = False
		if self.unsynced:
			try:
				self.store._fsync()
			except Exception as e:
				_warn('TSVZ error: {}: fsync failed: {}'.format(self.store.path, e), self.store.teeLogger)


class _ServeStore(object):
	"""One store's live view for the write handler (spec §21, design §3).

	``data`` maps each live key to its resolved row, in first-appearance
	order, and is always a replay of the store's files: writes are appended
	through ``_ServeWriter`` and come back through ``sync()`` like any other
	writer's. ``sync()`` replays the newly committed bytes of the active part
	and reloads everything when anything else changed. Every public method
	takes ``self.lock``; ``log`` is the teeLogger its messages go to.

	``header`` and ``createDefaults`` (default ``defaults``) are what a missing store is created with
	(``create``). ``header`` is also the CLI's ``--x-header``: a spec store's writes and ``clear`` check it as
	3.39 did, and a loose file takes it by one rule for every request (``_legacyHeader``). ``lazy``: the store is
	read at the first request that needs it, not here.
	"""

	def __init__(self, fileName, delimiter=..., header='', defaults=None, strict=False, teeLogger=None,
				 verbose=False, create=False, createDefaults=None, lazy=False):
		self.path = fileName
		self.spec = _isSpecPath(fileName)
		if self.spec:
			self.delimiter = _EXTENSION_DELIMITERS[_parsePartName(fileName).ext]
		else:
			self.delimiter = get_delimiter(delimiter, file_name=fileName)
		self.defaults = list(defaults) if defaults else []
		self.strict = strict
		self.teeLogger = teeLogger
		self.verbose = verbose
		self.lock = threading.RLock()
		self.mapping = dict if lazy else OrderedDict  # a one-shot read fills a dict faster; popitem reverses either
		self.data = self.mapping()
		self.unreadable = False
		self.stopped = False
		self.parts = self.state = None  # not read yet
		self.given = _formatHeader(header, teeLogger=teeLogger, delimiter=self.delimiter) if header else ['']
		self.header = self.given  # a loose file's: the header in effect, as _legacyHeader last found it
		self.mismatch = None  # the line 1 last warned of (_legacyHeader)
		if create:
			self._create(header, self.defaults if createDefaults is None else createDefaults)
		if not lazy:
			self.reload(teeLogger)
		self.writer = _ServeWriter(self)

	def _create(self, header, defaults):
		header = _formatHeader(header, teeLogger=self.teeLogger, delimiter=self.delimiter)
		if self.spec:
			_specEnsureStore(self.path, True, header, _normalizeDefaults(defaults, self.delimiter), False,
							 self.teeLogger, self.delimiter)
		else:
			_verifyFileExistence(self.path, createIfNotExist=True, teeLogger=self.teeLogger, header=header,
								 strict=False, delimiter=self.delimiter)

	# -- reading the files ---------------------------------------------------
	def reload(self, log):
		"""Replay the whole store."""
		with self.lock:
			reporter = _Reporter(self.path, log)
			try:
				self._noteDirMtime(_dirMtimeNs(self.path))
				if self.spec:
					self._loadSpec(reporter, log)
				else:
					self._loadLegacy(reporter, log)
				self.unreadable = reporter.incomplete()
				self.stopped = reporter.has('stopped')  # a stop policy ended the read (spec §4.10)
			except Exception:
				self.parts = None  # (a strict header mismatch): the next request reads again, and fails again
				raise
			finally:
				reporter.flush()

	def _legacyHeader(self, line, log):
		"""The header in effect for a loose file whose first line is ``line``: the one rule of ``--x-header``.

		``line`` is None when the file is missing, holds nothing, or its first line cannot be read. With a header
		given, line 1 is the header when it is that header (``_isHeaderLine``: the same cells, each without the
		whitespace around it), or when there is no line 1 yet (a write puts the header first). Otherwise 3.39's
		"Header mismatch" is printed and strict raises (nothing is written); loose, the file has no header and line 1
		is a row, for every request, the scrub included. Sets and returns ``self.header``."""
		header = self.given
		if any(header) and line is not None and not _isHeaderLine(header, line, self.delimiter):
			if line != self.mismatch or self.strict:  # (said once for the line, as 3.39 says it)
				self.mismatch = line
				_headerMismatch(header, line, log if log is not None else self.teeLogger, self.strict, self.delimiter)
			header = ['']
		self.header = header
		return header

	def _lineOneNow(self):
		"""The file's first line as it is now, read under the READ lock (_legacyFirstLine); None without a header given, for a
		file that is missing or holds nothing, or a first line that cannot be read."""
		line = None
		if any(self.given) and os.path.isfile(self.path):
			try:
				line = _legacyFirstLine(self.path, 'utf8', self.teeLogger) or None
			except Exception:  # a first line the codec cannot decode: the write or scrub that reads it reports it
				pass
		return line

	def _legacyHeaderNow(self, log):
		"""``_legacyHeader`` for the file as it is now (a write, ``clear`` or ``scrub`` that does not read it)."""
		return self._legacyHeader(self._lineOneNow(), log)

	def _noteDirMtime(self, dirMtime):
		self.dirMtime = dirMtime
		self.dirRacy = time.time() - dirMtime / 1e9 < _SERVE_RACY_SECONDS

	def _stamps(self, parts):
		stamps = {}
		for path in parts:
			try:
				stamps[path] = _specStamp(os.stat(path))
			except OSError:
				stamps[path] = None
		return stamps

	def _loadSpec(self, reporter, log):
		self.parts = _storeParts(self.path)[0]
		before = self._stamps(self.parts)
		load = _specLoad(self.path, self.delimiter, defaults=_normalizeDefaults(self.defaults, self.delimiter),
						 strict=self.strict, taskDic=self.mapping(), reporter=reporter, teeLogger=log,
						 verbose=self.verbose)
		self.data, self.state, self.width = load.data, load.state, load.correctColumnNum
		if load.parts != self.parts or reporter.has('torn'):
			# The part list changed while it was read, or the read may be torn: the next sync reads it again.
			self.parts = load.parts
			self.stamps = {}
		else:
			# As the read kept saw each part when it opened it (a read done again saw them as they are
			# now); a part it did not open (skipped, spec §19.10.3, or after a stop, §4.10) keeps the
			# stamp taken before the read.
			self.stamps = dict(before)
			self.stamps.update((info.path, info.stamp) for info in load.infos if info.stamp is not None)
		info = load.infos[-1] if load.infos else None
		self.offset = info.committed if info is not None else 0
		self.lineNo = info.lines if info is not None else 0
		self.tailSeen = info.tail if info is not None else b''
		self.label = os.path.basename(self.parts[-1]) + ' ' if len(self.parts) > 1 else ''
		self.check = info.check if info is not None else b''  # as read: the part may be rewritten since

	def _loadLegacy(self, reporter, log):
		self.parts = [self.path] if os.path.isfile(self.path) else []
		self.stamps = self._stamps(self.parts)
		self.data = self.mapping()
		self.legacyDefaults = list(self.defaults)
		self.width, self.offset, self.lineNo, self.tailSeen, self.check = -1, 0, 0, b'', b''
		self.header = self.given  # (no line 1: a write puts the header first)
		# A compressed file with no byte, or only empty streams, is an empty store, as readTabularFile reads it.
		if self.parts and not (_isCompressedFile(self.path) and _fileHoldsNothing(self.path)):
			try:
				with _readLock(self.path), openFileAsCompressed(self.path, mode='rb', teeLogger=self.teeLogger) as f:
					if any(self.given):
						try:
							raw = f.readline()
						except Exception as e:  # L5: a stream damaged inside line 1 holds nothing more to read
							if not _isCompressedFile(self.path):
								raise
							reporter.note('damaged', None, _streamDamage(e))
							return
						if raw and any(self._legacyHeader(raw.decode('utf8', errors='replace'), log)):
							self.width, self.lineNo = len(self.header), 1  # the header: not a row
						elif raw:
							self._apply(raw, reporter)  # line 1 is a row
					self.width, self.offset, self.lineNo = _legacyReadLoop(
						f, self.path, self.data, self.width, self.lineNo, self.strict, self.delimiter,
						self.legacyDefaults, False, 'utf8', reporter)
					self.check = self._readCheck()  # under the READ lock: of the bytes the view was read from
			except OSError as e:
				reporter.note('unreadable', None, 'could not read {} ({})'.format(self.path, e))
		self.label = ''

	def _readCheck(self):
		"""The bytes just before the read offset of the active part ('' when compressed or empty)."""
		if not self.parts or _parsePartName(self.parts[-1]).codec or not self.offset:
			return b''
		try:
			with open(self.parts[-1], 'rb') as f:
				start = max(0, self.offset - _SERVE_CHECK_BYTES)
				f.seek(start)
				return f.read(self.offset - start)
		except OSError:
			return b''

	def sync(self, log):
		"""Bring the view up to date with the files: follow appends, reload on anything else."""
		with self.lock:
			if self.parts is None:
				return self.reload(log)
			dirMtime = _dirMtimeNs(self.path)
			if dirMtime != self.dirMtime or self.dirRacy:
				parts = _storeParts(self.path)[0] if self.spec else ([self.path] if os.path.isfile(self.path) else [])
				if parts != self.parts:
					return self.reload(log)
				self._noteDirMtime(dirMtime)
			for path in self.parts:
				try:
					stamp = _specStamp(os.stat(path))
				except OSError:
					return self.reload(log)
				if stamp == self.stamps.get(path):
					continue
				if path != self.parts[-1] or not self._follow(path, stamp, log):
					return self.reload(log)

	def _follow(self, path, stamp, log):
		"""Replay what was appended to the active part; False when it changed some other way."""
		old = self.stamps.get(path)
		if (self.stopped or _parsePartName(path).codec or old is None or stamp[0] != old[0]
				or stamp[1] < self.offset or stamp[1] <= old[1] or (not self.spec and self.lineNo <= 1 and any(self.given))):
			# (a loose file's line 1 is read by a reload, which tells whether it is the header, also once it is whole)
			return False  # an append always grows the part; anything else is a rewrite (and a stopped read is read again)
		try:
			# A loose file is read under the READ lock (a .tsvz part: its steadiness is checked on reload).
			with (contextlib.suppress() if self.spec else _readLock(path)), open(path, 'rb') as f:
				f.seek(self.offset - len(self.check))
				if f.read(len(self.check)) != self.check:
					return False
				data = f.read()
		except OSError:
			return False
		end = data.rfind(b'\n') + 1
		if not self.spec and end < len(data):
			return False  # 3.39 reads an unterminated last line as a record; a reload does too
		reporter = _Reporter(self.path, log)
		try:
			if end and self.spec:
				self._replay(data[:end], path, reporter)
			elif end:
				for raw in data[:end].split(b'\n')[:-1]:
					self._apply(raw + b'\n', reporter)
			tail = data[end:]
			if tail and tail != self.tailSeen:
				reporter.note('tail', None, 'ignored uncommitted bytes after the last newline: {!r}'.format(tail[:80]))
			self.tailSeen = tail
		finally:
			reporter.flush()
		self.offset += end
		self.check = (self.check + data[:end])[-_SERVE_CHECK_BYTES:]
		self.stamps[path] = stamp[:1] + (self.offset + len(data) - end,) + stamp[2:]
		return True

	def _apply(self, raw, reporter):
		"""Replay one line appended to a loose file into the view, as a 3.39 read does."""
		self.lineNo += 1
		try:
			text = raw.decode('utf8')
		except UnicodeDecodeError:
			text = raw.decode('utf8', errors='replace')
			reporter.note('decode', 'line {}'.format(self.lineNo), 'invalid utf8 replaced with U+FFFD')
		self.width, _ = _processLine(text, self.data, self.width, strict=self.strict, delimiter=self.delimiter,
									 defaults=self.legacyDefaults, reporter=reporter)

	def _replay(self, data, path, reporter):
		"""Replay the committed lines ``data`` appended to the active part ``path`` into the view (spec §7)."""
		if self.stopped:
			return  # a stop policy ended the read: nothing after it is read (spec §4.10)
		lines = []
		offset = self.offset
		for raw in data.split(b'\n')[:-1]:
			lines.append((raw + b'\n', offset))
			offset += len(raw) + 1
		if self.offset == 0 and lines[0][0].startswith(b'\xef\xbb\xbf'):  # only at the start of a part (spec §4.7)
			lines[0] = (lines[0][0][3:], 3)
			reporter.note('bom', '{}line {}'.format(self.label, self.lineNo + 1), 'stripped a UTF-8 byte order mark')
		info = _PartInfo(path, '')  # a follower follows plain parts only
		for _, _, _, record in _specReplayLines(lines, info, self.label, self.state, self.delimiter, reporter, self.lineNo):
			if record is None:
				continue
			key, row = record
			if row is None:
				self.data.pop(key, None)
			else:
				self.width = _specPadRow(row, self.state, self.width)
				self.data[key] = row
		self.lineNo = info.lines
		if info.stopped:
			self.stopped = self.unreadable = True

	def _fresh(self, log):
		"""Read-your-writes (spec §21.8): wait for queued writes, then follow the files."""
		self.writer.barrier()
		self.sync(log)

	# -- reads ----------------------------------------------------------------
	def missing(self, log):
		"""True when the store has no part (spec §20.2.2); the store is not read for it."""
		with self.lock:
			self.writer.barrier()
			return not (_storeParts(self.path)[0] if self.spec else os.path.isfile(self.path))

	def resolve(self, key):
		"""The key a requested key names (spec §20.2.7; 3.39 rstrip for loose files)."""
		if self.spec:
			return _specNameKey(key, self.delimiter, self.state.strip, self.data)
		return key.rstrip()

	def read(self, log):
		with self.lock:
			self._fresh(log)
			return list(self.data.values())  # a row is replaced, never changed, once it is in ``data``

	def get(self, keys, log):
		"""Rows for ``keys`` and whether one was missing (spec §20.2 ``get``, §14.5)."""
		with self.lock:
			self._fresh(log)
			rows = []
			missing = False
			for key in keys:
				key = self.resolve(key)
				row = self.data.get(key)
				if row is None:
					missing = True
					if not (self.spec and self.state.returnDefaults):
						continue
					row = [key] + list(self.state.defaults[1:])
					row += [''] * (self.width - len(row))
				rows.append(list(row))
			return rows, missing

	def has(self, key, log):
		with self.lock:
			self._fresh(log)
			return self.resolve(key) in self.data

	def length(self, log):
		with self.lock:
			self._fresh(log)
			return len(self.data)

	def keys(self, log):
		with self.lock:
			self._fresh(log)
			return list(self.data)

	def verify(self, log):
		"""Checksum mismatches (spec §15) and whether every part could be read."""
		with self.lock:
			self.writer.barrier()
			if not self.spec:
				return [], True
			reporter = _Reporter(self.path, log)
			try:
				mismatches = _specVerify(self.path, self.delimiter, reporter)
				return mismatches, not reporter.incomplete()
			finally:
				reporter.flush()

	def partRows(self, log):
		with self.lock:
			self.writer.barrier()
			reporter = _Reporter(self.path, log)
			try:
				return _partRows(self.path, reporter)
			finally:
				reporter.flush()

	# -- writes ---------------------------------------------------------------
	def _payload(self, rows, log, first=_UNREAD):
		"""Bytes for ``rows`` (``[(cells, marker), ...]``), formatted as the direct writers format them. ``first``: the
		file's line 1 as _lineOneNow read it just before (a delete's, _dropKeys), else it is read here."""
		if self.spec:
			if rows and any(self.header):
				_specCheckHeader(self.path, self.header, self.delimiter, self.strict, self.verbose, log)
			return _specFormatRecords(rows, self.delimiter).encode('utf-8')
		if first is _UNREAD:
			first = self._lineOneNow()  # (read once: the payload's header check takes it from here)
		self._legacyHeader(first, log)
		# Rows for a file that holds nothing yet are fitted to the header it is about to begin with.
		assume = self.header if any(self.header) and _fileHoldsNothing(self.path) else None
		return _legacyFormatPayload(self.path, [cells for cells, marker in rows], log, self.header, True, self.verbose,
									'utf8', self.strict, self.delimiter, assume=assume, given=self.given, first=first)[0]

	def _tombstone(self, key):
		"""Rows that delete ``key``: written with stripping off when it would strip ``key`` (spec §9.6)."""
		return _specTombstones([key], self.delimiter, self.state.strip) if self.spec else [([key], False)]

	def _dropKeys(self, rows, log):
		"""The deletions ``rows`` that are still to be written as lines (caller holds the lock). In a loose file of one
		column, where a lone key is a row, the keys' lines are taken out of the file instead (_legacyDropKeys), once
		everything queued is in it. A reset of the defaults is a line at every width.

		The file's width is asked with the header given, as the library's readers take it, even when line 1 is not it and
		is a row to this command (_legacyHeader): a line is a deletion or a row by that one answer, so a reader given that
		header reads what this command deleted the same way, and the command's own count is kept too (_legacyDropKeys,
		``xHeader``).

		Returns ``(rows, first)``: ``first`` is the line 1 read here for the header check, which the payload's check takes
		too (_payload) when nothing was written since, or when no row is left to append; else _UNREAD (read again)."""
		if self.spec:
			return rows, _UNREAD
		keys = [cells[0] for cells, marker in rows if cells[0] != DEFAULTS_INDICATOR_KEY]
		first = self._lineOneNow()
		self._legacyHeader(first, log)  # (says a mismatch, and a strict store raises; whether line 1 is a row: _legacyDropKeys)
		width = _legacyWidthNow(self.path, self.given, True, 'utf8', self.delimiter)
		if not keys or width is None or width > 1:
			return rows, first  # (a look at the first lines: what is queued cannot change them, so no wait for it)
		self.writer.barrier()
		if not _legacyDropKeys(self.path, keys, self.given, True, 'utf8', self.delimiter, True):
			return rows, _UNREAD
		self.parts = None  # read again at the next request
		rows = [row for row in rows if row[0][0] == DEFAULTS_INDICATOR_KEY]
		return rows, (_UNREAD if rows else first)  # (a reset still to append is checked against line 1 as written)

	def _durable(self, sync):
		return sync or (self.spec and self.state is not None and self.state.writeAck == 'disk')

	def _submit(self, rows, sync, log, first=_UNREAD):
		"""Queue ``rows`` (caller holds the lock); return ``(job, durable)`` or None when there is nothing to write.
		``first``: see _payload."""
		payload = self._payload(rows, log, first)
		if not payload:
			return None
		durable = self._durable(sync)
		return self.writer.submit(payload, durable), durable

	def _ack(self, queued, written=False):
		"""Acknowledge per spec §21.9: after fsync for ``disk``; after the write when ``written``; else at once."""
		if queued is None:
			return
		if queued[1] or written:
			self.writer.wait(queued[0])  # (a durable job is done once it is fsync'd)

	def write(self, rows, sync, log, written=False):
		"""Append ``rows`` (``[(cells, marker), ...]``) as one batch: ``set`` and its bulk input."""
		with self.lock:
			queued = self._submit(rows, sync, log)
		self._ack(queued, written)
		self._wrote(rows, log)

	def _wrote(self, rows, log):
		if self.verbose and log is not None:  # as the direct writers report it
			log.teelog('Appended {} lines to {}'.format(len(rows), self.path) if rows else 'No lines to append to ' + self.path)

	def delete(self, rows, sync, log, written=False):
		"""Append the tombstones of ``delete`` (``[(cells, marker), ...]``) as one batch (spec §20.2.7), or in a loose file
		of one column take the keys' lines out (_dropKeys).

		Naming a key a reader would strip needs the store as it is now, read
		under the lock, so that delete is atomic (spec §21.8.2). Returns False,
		having written nothing, when that read could not be completed.
		"""
		with self.lock:
			if self.spec and _specNamesKeys(rows, self.delimiter):
				self._fresh(log)
				if self.unreadable:
					return False
				rows = _specDeleteRows(rows, self.delimiter, self.state.strip, self.data)
			rows, first = self._dropKeys(rows, log)
			queued = self._submit(rows, sync, log, first)
		self._ack(queued, written)
		self._wrote(rows, log)
		return True

	def pop(self, key, sync, log):
		"""Atomically return the row of ``key`` and append its tombstone; None when it is missing."""
		with self.lock:
			self._fresh(log)
			if self.unreadable:
				return _SERVE_PARTIAL
			key = self.resolve(key)
			row = self.data.get(key)
			if row is None:
				return None
			row = list(row)
			rows, first = self._dropKeys(self._tombstone(key), log)
			queued = self._submit(rows, sync, log, first)
		self._ack(queued, written=True)  # the row is only popped once its tombstone is written
		return row

	def popitem(self, last, sync, log):
		"""Atomically return the row of the last (or first) live key and delete it; None when empty."""
		with self.lock:
			self._fresh(log)
			if self.unreadable:
				return _SERVE_PARTIAL
			if not self.data:
				return None
			if not last:
				key = next(iter(self.data))
			else:  # a dict is reversible from Python 3.8
				key = next(reversed(self.data)) if hasattr(self.data, '__reversed__') else list(self.data)[-1]
			row = list(self.data[key])
			rows, first = self._dropKeys(self._tombstone(key), log)
			queued = self._submit(rows, sync, log, first)
		self._ack(queued, written=True)
		return row

	def setdefault(self, cells, sync, log):
		"""Atomically return the row of ``cells[0]``, appending ``cells`` first when the key is missing."""
		with self.lock:
			self._fresh(log)
			if self.unreadable:
				return _SERVE_PARTIAL
			key = self.resolve(cells[0])
			row = self.data.get(key)
			if row is not None:
				return list(row)
			queued = self._submit([(cells, False)], sync, log)
			if queued is not None:
				self.writer.wait(queued[0])
			self.sync(log)
			row = self.data.get(key)
			row = list(row) if row is not None else None
		self._ack(queued)
		return row

	def clear(self, log):
		"""``clear`` (spec §20.2) through the library; True when the store was cleared."""
		with self.lock:
			self.writer.barrier()
			try:
				if self.spec:
					return _specClearTabularFile(self.path, teeLogger=log, header=self.header, verifyHeader=self.strict,
												 verbose=self.verbose, delimiter=self.delimiter) is not False
				self._legacyHeaderNow(log)  # a strict mismatch is an error; else the file is left with the header given
				return clearTabularFile(self.path, teeLogger=log, header=self.given, verifyHeader=False,
										verbose=self.verbose, delimiter=self.delimiter) is not False
			finally:
				self.parts = None  # read again at the next request

	def scrub(self, log):
		"""``scrub`` (spec §20.2) through the library; True when done, False when refused, None when the read
		stopped at damaged data and the store was rewritten from what was read (spec §4.10)."""
		with self.lock:
			self.writer.barrier()
			try:
				if self.spec:
					return _specScrub(self.path, teeLogger=log, verifyHeader=False, verbose=self.verbose,
									  strict=self.strict, delimiter=self.delimiter, defaults=self.defaults)[1]
				# Line 1 is the header exactly when every read takes it for one, so the scrub keeps what they read; a
				# scrub never changes line 1, so a row equal to the header given never becomes line 1.
				header = self._legacyHeaderNow(log)
				return _legacyScrub(self.path, teeLogger=log, header=header, verifyHeader=True, verbose=self.verbose,
									strict=self.strict, delimiter=self.delimiter, defaults=self.defaults)[1]
			finally:
				self.parts = None  # read again at the next request

	def flush(self):
		"""Write and fsync everything submitted so far (``stop``, spec §21.9)."""
		self.writer.barrier()
		with self.lock:
			self._fsync()

	def close(self):
		"""Write and fsync everything queued and stop the writer."""
		self.writer.close()

	# -- called by the writer thread -----------------------------------------------
	def _append(self, payload, fsync):
		reporter = _Reporter(self.path, self.teeLogger)
		try:
			if self.spec:
				_specAppendPayload(_storeParts(self.path)[1], payload, reporter, fsync=fsync,
								   memberSize=getattr(self.state, 'memberSize', None))
			else:
				codec = _fileCodec(self.path)
				_requireCodec(codec)
				header = None  # a file that holds nothing gets the header given first (_appendOpen)
				if any(self.given):
					header = functools.partial(_encodeRow, _sanitize(self.given, delimiter=self.delimiter),
											   self.delimiter, 'utf8', 'strict', what='header')
				_lockedAppend(self.path, payload, 'newline', reporter, fsync=fsync, header=header, codec=codec)
		finally:
			reporter.flush()

	def _fsync(self):
		path = _storeParts(self.path)[1] if self.spec else self.path
		if os.path.exists(path):
			with open(path, 'ab') as f:
				os.fsync(f.fileno())


# ===========================================================================
# Write handler (tsvz-spec-v1 §21): the protocol
# ===========================================================================
_SERVE_PROTOCOL_VERSION = 1
#: A request or bulk line longer than this closes the connection (spec §21.10).
_SERVE_LINE_LIMIT = 64 << 20
#: operation -> (minimum, maximum) number of arguments (None: no maximum).
_SERVE_ARITY = {
	'read': (0, 0), 'len': (0, 0), 'keys': (0, 0), 'clear': (0, 0), 'scrub': (0, 0), 'verify': (0, 0),
	'parts': (0, 0), 'stop': (0, 0), 'get': (1, None), 'set': (1, None), 'append': (1, None),
	'delete': (1, None), 'has': (1, 1), 'pop': (1, 1), 'popitem': (0, 1), 'setdefault': (2, None),
}
_SERVE_UNREADABLE = 'could not read every part of the store'
#: What pop, popitem and setdefault return instead of acting on a store they could not read whole.
_SERVE_PARTIAL = object()
#: Operations that fail on a missing store (spec §20.2.2).
_SERVE_NEED_STORE = ('read', 'get', 'has', 'len', 'keys', 'pop', 'popitem', 'scrub', 'verify', 'parts')
#: A pointer file larger than this is not one (spec §21.2).
_SERVE_POINTER_LIMIT = 64 << 10


class _ServeUsage(Exception):
	"""A request that does not follow spec §21.5 / §21.7 (status 2)."""


def _serveEncode(value):
	"""A protocol field (spec §21.4): §13 with TAB, a leading '#' left as written."""
	return _specEncodeField(value, '\t')


def _serveRecordLine(cells, marker):
	"""A record as a protocol line: a marker key as written, a data key with ``<#>`` (spec §21.4)."""
	if marker:
		return '\t'.join([cells[0]] + [_serveEncode(cell) for cell in cells[1:]])
	return _specFormatRecord(cells, '\t')


def _serveDecodeLine(line):
	"""The fields of a protocol record line, decoded per §13."""
	return [_specDecodeField(field, '\t') for field in line.split('\t')]


def _serveParseRequest(line):
	"""Split a request line (spec §21.5) into ``(options, operation, args, rawArgs)``."""
	raw = line.split('\t')
	i = 0
	while i < len(raw) and raw[i].startswith('--'):
		i += 1
	if i >= len(raw) or not raw[i]:
		raise _ServeUsage('missing operation')
	return raw[:i], raw[i], [_specDecodeField(field, '\t') for field in raw[i + 1:]], raw[i + 1:]


def _serveNeedsBulk(line):
	"""True when ``line`` is ``set -`` / ``append -`` / ``delete -``: record lines up to ``#`` follow."""
	try:
		_, operation, _, raw = _serveParseRequest(line)
	except _ServeUsage:
		return False
	return operation in ('set', 'append', 'delete') and raw == ['-']


class _ServeLog(object):
	"""Per-request teeLogger: warnings and errors become ``#!`` lines and also go to ``logger``."""

	def __init__(self, logger=None):
		self.logger = logger
		self.lines = []

	def teelog(self, message, level='info', callerStackDepth=None):
		if level in ('warning', 'error', 'critical'):
			self.lines.append(str(message))
		if self.logger is not None:
			self.logger.teelog(message, level)


def _serveDispatch(store, line, bulk, log):
	"""Run one request against ``store`` (spec §21.7); return ``(status, rows, message)``.

	``rows`` hold the output's fields: each is one output line, as ``_specFormatRecord`` with TAB writes it.
	"""
	options, operation, args, raw = _serveParseRequest(line)
	for option in options:
		if option not in ('--sync', '--x-written'):
			raise _ServeUsage('unknown option {}'.format(option))
	sync = '--sync' in options
	written = '--x-written' in options  # TSVZ extension: acknowledge once written, report failures
	if operation not in _SERVE_ARITY:
		raise _ServeUsage('unknown operation {}'.format(operation))
	low, high = _SERVE_ARITY[operation]
	if len(args) < low or (high is not None and len(args) > high):
		raise _ServeUsage('wrong number of arguments for {}'.format(operation))
	isBulk = operation in ('set', 'append', 'delete') and raw == ['-']
	keys = args if operation in ('get', 'delete', 'has', 'pop') else args[:1]
	if operation not in ('read', 'len', 'keys', 'clear', 'scrub', 'verify', 'parts', 'stop', 'popitem') \
			and not isBulk and '' in keys:
		raise _ServeUsage('a KEY must not be empty')
	if operation in _SERVE_NEED_STORE and store.missing(log):
		return 1, [], 'no such store'  # spec §20.2.2
	if operation in ('read', 'get', 'has', 'len', 'keys'):
		# The status is taken under the store lock the read ran under: another request's reload may change
		# ``unreadable`` once the lock is free.
		with store.lock:
			missing = False
			if operation == 'read':
				rows = store.read(log)
			elif operation == 'get':
				rows, missing = store.get(args, log)
			elif operation == 'has':
				rows, missing = [], not store.has(args[0], log)
			elif operation == 'len':
				rows = [[str(store.length(log))]]
			else:
				rows = [[key] for key in store.keys(log)]
			if store.unreadable:  # what could be read, and status 1 (spec §4.10, §21.8.5)
				return 1, rows, _SERVE_UNREADABLE
		return (3 if missing else 0), rows, ''
	if operation in ('set', 'append', 'delete'):
		keysOnly = operation == 'delete'
		if isBulk:
			reporter = _Reporter('<request>', log)
			try:
				rows = _parseRecordLines(bulk or [], '\t', True, keysOnly, reporter)
			finally:
				reporter.flush()
		elif keysOnly:
			rows = [([key], bool(_MARKER_RE.match(field))) for key, field in zip(args, raw)]
		else:
			rows = [(args, bool(_MARKER_RE.match(raw[0])))]
		if not keysOnly:
			store.write(rows, sync, log, written)
		elif not store.delete(rows, sync, log, written):
			return 1, [], _SERVE_UNREADABLE + '; nothing was written'
		return 0, [], ''
	if operation in ('pop', 'popitem'):
		if operation == 'pop':
			row = store.pop(args[0], sync, log)
		else:
			if args and args[0] not in ('first', 'last'):
				raise _ServeUsage('popitem takes first or last')
			row = store.popitem(not args or args[0] == 'last', sync, log)
		if row is _SERVE_PARTIAL:
			return 1, [], _SERVE_UNREADABLE
		return (3, [], '') if row is None else (0, [row], '')
	if operation == 'setdefault':
		if _MARKER_RE.match(raw[0]):
			raise _ServeUsage('setdefault takes a data KEY, not a marker')
		row = store.setdefault(args, sync, log)
		if row is _SERVE_PARTIAL:
			return 1, [], _SERVE_UNREADABLE
		return (3, [], '') if row is None else (0, [row], '')
	if operation in ('clear', 'scrub'):
		done = store.clear(log) if operation == 'clear' else store.scrub(log)
		if done is None:  # spec §4.10: rewritten from a read that stopped at damaged data
			return 1, [], ('the read stopped at damaged data (#_on_corruption_# stop); the store was rewritten from what '
						   'was read, and the original part was kept as a .damaged- copy')
		return (0, [], '') if done else (1, [], '{} refused; nothing was written'.format(operation))
	if operation == 'verify':
		mismatches, readable = store.verify(log)
		rows = [[str(field) for field in entry] for entry in mismatches]
		return (1 if not readable else 4 if rows else 0), rows, '' if readable else _SERVE_UNREADABLE
	if operation == 'parts':
		return 0, store.partRows(log), ''
	return 1, [], 'not served by a handler'  # stop, sent to an in-process store


def _serveRespond(store, line, bulk=None, logger=None):
	"""The response to one request (spec §21.6), as lines without their '\\n'."""
	log = _ServeLog(logger)
	try:
		status, rows, message = _serveDispatch(store, line, bulk, log)
	except _ServeUsage as e:
		status, rows, message = 2, [], str(e)
	except Exception as e:
		status, rows, message = 1, [], str(e) or type(e).__name__
		if logger is not None:
			logger.teelog('tsvz: {}: request {!r} failed: {}'.format(store.path, line[:80], message), 'error')
	failing = store.writer.failing
	if failing:
		log.lines.append('TSVZ warning: {}: writes are failing ({}); writes acknowledged before they were written '
						 'may be lost'.format(store.path, failing))
	response = ['#!\t' + _serveEncode(text) for text in log.lines] + [_specFormatRecord(row, '\t') for row in rows]
	response.append('#{}\t{}'.format(status, _serveEncode(message)) if message else '#{}'.format(status))
	return response


def _serveParseResponse(lines):
	"""``(lines, diagnostics, status, message)`` from a response's lines (spec §21.6)."""
	out, diagnostics = [], []
	for line in lines:
		if line.startswith('#!'):
			diagnostics.append(_specDecodeField(line[3:], '\t'))
		elif line.startswith('#'):
			head, _, message = line[1:].partition('\t')
			status = int(head) if _ASCII_DIGITS_RE.match(head) else 1
			return out, diagnostics, status, _specDecodeField(message, '\t')
		else:
			out.append(line)
	raise _ServeLost('the response ended without a status line')


class _ServeLost(Exception):
	"""The connection to a handler broke during a request; whether it was applied is unknown."""


class _ServeLocal(object):
	"""The in-process stand-in for a handler connection: the same engine and requests, no socket."""

	def __init__(self, store):
		self.store = store

	def request(self, line, bulk=None):
		return _serveParseResponse(_serveRespond(self.store, line, bulk))

	def close(self):
		self.store.close()


# ===========================================================================
# Write handler (tsvz-spec-v1 §21): pointer files, connections and the server
# ===========================================================================
class _ServeBusy(Exception):
	"""Another handler holds the store's pointer file (spec §21.2); ``args[0]`` is its pointer, as a dict."""


class _ServeUnreachable(Exception):
	"""A pointer file whose handler cannot be used; ``quiet`` when that is expected (other host, no permission)."""

	def __init__(self, message, quiet=False):
		Exception.__init__(self, message)
		self.quiet = quiet


class _ServeSignal(Exception):
	"""SIGTERM arrived while ``tsvz serve`` was serving."""


def _servePointerPath(store):
	"""The pointer file of a store (spec §21.2): the store path plus ``.serve``."""
	return store + '.serve'


def _serveReadPointer(path):
	"""Read a pointer file without following links or blocking: ``(fields, stat, problem)``.

	``fields`` is None when there is no pointer file or it does not parse;
	``problem`` says why a file there cannot be a pointer file.
	"""
	try:
		fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
	except FileNotFoundError:
		return None, None, None
	except OSError as e:
		return None, None, '{} is not a regular file ({})'.format(path, e.strerror or e)
	with os.fdopen(fd, 'rb') as f:
		st = os.fstat(f.fileno())
		if not stat.S_ISREG(st.st_mode):
			return None, st, '{} is not a regular file'.format(path)
		data = f.read(_SERVE_POINTER_LIMIT + 1)
	if len(data) > _SERVE_POINTER_LIMIT:
		return None, st, '{} is too large to be a pointer file'.format(path)
	lines = data.decode('utf-8', 'replace').split('\n')
	if lines[0] != 'tsvz-handler\t{}'.format(_SERVE_PROTOCOL_VERSION):
		return None, st, None
	fields = {}
	for line in lines[1:]:
		name, tab, value = line.partition('\t')
		if tab:
			fields[name] = _specDecodeField(value, '\t')
	return fields, st, None


def _serveStoreOwner(store):
	"""The uid owning STORE (its unnumbered file, else its first part), or None."""
	parts = _storeParts(store)[0] if _isSpecPath(store) else ([store] if os.path.exists(store) else [])
	for path in parts:
		try:
			return os.stat(path).st_uid
		except OSError:
			continue
	return None


def _serveLookup(store):
	"""The pointer file of ``store``, checked as spec §21.2.4 requires: ``(info, problem)``.

	``info`` is None when there is no usable pointer file; ``problem`` then
	says why one that exists was not trusted (None: nothing to report).
	A pointer file is trusted only when it is a regular file owned by this
	user or by the store's owner, not writable by others, with plain values
	and a Unix-domain socket address.
	"""
	path = _servePointerPath(store)
	for attempt in range(2):
		if attempt:
			time.sleep(0.05)  # a handler may be writing it
		fields, st, problem = _serveReadPointer(path)
		if problem:
			return None, problem
		if st is None:
			return None, None  # no pointer file: no handler
		if fields is not None and 'address' in fields and 'host' in fields:
			break
	else:
		return None, None
	if hasattr(os, 'geteuid'):
		trusted = (os.geteuid(), _serveStoreOwner(store))
		if st.st_uid not in trusted:
			return None, '{} is owned by uid {}, neither you nor the owner of the store'.format(path, st.st_uid)
		if st.st_mode & 0o022:
			return None, '{} is writable by others than its owner'.format(path)
	for name, value in fields.items():
		if any(ord(c) < 32 for c in value):
			return None, '{} has control characters in its {}'.format(path, name)
	address = fields['address']
	if not address.startswith('unix:'):
		return None, '{} names {}, which is not a Unix-domain socket'.format(path, address)
	if 'x-delimiter' in fields:  # written with backslash escapes, so that a TAB or NUL is no control character
		fields['x-delimiter'] = codecs.decode(fields['x-delimiter'], 'unicode_escape')
	fields['owner'] = getattr(st, 'st_uid', None)
	return fields, None


def _serveFind(store):
	"""The trusted pointer file of ``store`` as a dict (spec §21.2), or None."""
	return _serveLookup(store)[0]


class _ServeConnection(object):
	"""A connection to the handler a trusted pointer file names (spec §21.3–§21.6)."""

	def __init__(self, info, timeout=None):
		import socket
		if info.get('host') != socket.gethostname():
			raise _ServeUnreachable('it runs on host {}'.format(info.get('host')), quiet=True)
		address = info.get('address', '')
		owner = info.get('owner')
		if not address.startswith('unix:') or not hasattr(socket, 'AF_UNIX'):
			raise _ServeUnreachable('{!r} is not a Unix-domain socket this platform can reach'.format(address),
									quiet=True)
		try:
			sock = self._connectUnix(socket, address[5:], owner)
		except OSError as e:
			raise _ServeUnreachable(str(e), quiet=e.errno in (errno.EACCES, errno.EPERM))
		sock.settimeout(timeout)
		self.sock = sock
		self.rfile = sock.makefile('rb')
		self.lock = threading.Lock()

	@staticmethod
	def _connectUnix(socket, path, owner):
		"""Connect to the Unix socket ``path``, which must belong to ``owner`` (spec §21.2.4)."""
		st = os.lstat(path)
		if not stat.S_ISSOCK(st.st_mode):
			raise _ServeUnreachable('{} is not a socket'.format(path))
		if owner is not None and st.st_uid != owner:
			raise _ServeUnreachable('the socket {} is owned by uid {}, not by the owner of the pointer file'.format(
				path, st.st_uid))
		deadline = time.monotonic() + 0.5
		while True:
			sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
			try:
				sock.settimeout(max(0.05, deadline - time.monotonic()))
				sock.connect(path)
				break
			except OSError as e:
				sock.close()
				if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK) and time.monotonic() < deadline:
					time.sleep(0.01)  # the listen queue is full for a moment
					continue
				raise
		peer = getattr(socket, 'SO_PEERCRED', None)
		if peer is not None and owner is not None:
			import struct
			uid = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, peer, struct.calcsize('3i')))[1]
			if uid != owner:
				sock.close()
				raise _ServeUnreachable('the process behind {} runs as uid {}, not as the owner of the pointer '
										'file'.format(path, uid))
		return sock

	def request(self, line, bulk=None):
		"""Send one request, followed by ``bulk`` record lines and ``#`` when given; return the parsed response."""
		text = line + '\n'
		if bulk is not None:
			text += ''.join(record + '\n' for record in bulk) + '#\n'
		with self.lock:
			try:
				self.sock.sendall(_encodeText(text, 'utf-8'))  # nothing is sent for text that cannot be written
				lines = []
				while True:
					raw = self.rfile.readline()
					if not raw.endswith(b'\n'):
						raise _ServeLost('the handler closed the connection')
					line = raw[:-1].decode('utf-8', 'replace')
					lines.append(line)
					if line.startswith('#') and not line.startswith('#!'):
						return _serveParseResponse(lines)
			except OSError as e:
				raise _ServeLost(str(e))

	def close(self):
		for thing in (self.rfile, self.sock):
			try:
				thing.close()
			except Exception:
				pass


_SERVE_CLASSES = {}


def _serveClasses():
	"""The socketserver classes of the handler; ``socketserver`` is imported on first use."""
	if not _SERVE_CLASSES:
		import socketserver

		class Handler(socketserver.StreamRequestHandler):
			def handle(self):
				self.server.serving.connection(self.rfile, self.wfile, self.request)

		_SERVE_CLASSES['handler'] = Handler
		if hasattr(socketserver, 'UnixStreamServer'):
			class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
				daemon_threads = True
				request_queue_size = 128

			_SERVE_CLASSES['unix'] = UnixServer
	return _SERVE_CLASSES


def _serveSupported():
	"""True when this Python can serve: it has Unix-domain sockets (spec §21.3)."""
	return 'unix' in _serveClasses()


def _serveGroup(group):
	"""A group name or number as a gid (None stays None)."""
	if group is None or group == '':
		return None
	if _ASCII_DIGITS_RE.match(str(group)):
		return int(group)
	import grp
	return grp.getgrnam(group).gr_gid


def _serveIsStop(line):
	"""True when ``line`` is a well-formed ``stop`` request."""
	try:
		options, operation, args, _ = _serveParseRequest(line)
	except _ServeUsage:
		return False
	return operation == 'stop' and not args and all(option == '--sync' for option in options)


class _ServeHandle(object):
	"""A running write handler for one store (spec §21; design §3).

	``lock()`` takes the pointer file, ``start(store)`` binds the socket and
	writes the pointer, ``serve()`` answers requests until ``stop()`` (or an
	exception in the serving thread), ``close()`` writes everything and
	removes the socket and the pointer file.
	"""

	def __init__(self, storePath, logger=None, group=None, mode=None, idleTimeout=0):
		self.storePath = storePath
		self.logger = logger
		self.group = group
		self.mode = mode
		self.idleTimeout = idleTimeout
		self.pointer = _servePointerPath(storePath)
		self.pointerFile = None
		self.store = None
		self.server = None
		self.socketDir = None
		self.address = None
		self.guard = threading.Lock()
		self.active = 0
		self.sockets = set()
		self.lastActivity = time.monotonic()
		self.stopping = threading.Event()

	def lock(self):
		"""Take over the pointer file (spec §21.2.3); raise ``_ServeBusy`` when another handler has it.

		An existing pointer file must be a regular file. When no handler holds
		its lock it is stale and is removed, never written into: the handler
		then creates its own with O_EXCL, without following symbolic links.
		Errors other than "busy" (a file system without locks) raise OSError.
		"""
		try:
			fd = os.open(self.pointer, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
		except FileNotFoundError:
			fd = None
		except OSError as e:
			raise OSError('{} is not a regular file ({}); remove it'.format(self.pointer, e.strerror or e))
		old = None
		if fd is not None:
			old = os.fdopen(fd, 'rb')
			try:
				if not stat.S_ISREG(os.fstat(old.fileno()).st_mode):
					raise OSError('{} is not a regular file; remove it'.format(self.pointer))
				if not _tryLockFile(old):
					raise _ServeBusy(_serveReadPointer(self.pointer)[0] or {})
				if self._isPointer(old):
					os.unlink(self.pointer)  # stale: replace it rather than write into it
			except BaseException:
				old.close()
				raise
		try:
			fd = os.open(self.pointer, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
		except FileExistsError:
			raise _ServeBusy(_serveReadPointer(self.pointer)[0] or {})
		finally:
			if old is not None:
				old.close()
		f = os.fdopen(fd, 'r+b')
		try:
			locked = _tryLockFile(f)
		except OSError:
			f.close()
			try:
				os.unlink(self.pointer)
			except OSError:
				pass
			raise
		if not locked or not self._isPointer(f):
			f.close()
			raise _ServeBusy(_serveReadPointer(self.pointer)[0] or {})
		self.pointerFile = f

	def _isPointer(self, f):
		"""True while the pointer path still names the open file ``f``."""
		try:
			return os.lstat(self.pointer).st_ino == os.fstat(f.fileno()).st_ino
		except OSError:
			return False

	def start(self, store):
		"""Bind the socket for ``store`` and write the pointer file."""
		import socket
		import tempfile
		self.store = store
		classes = _serveClasses()
		gid = _serveGroup(self.group)
		mode = self.mode if self.mode is not None else (0o660 if gid is not None else 0o600)
		shared = self.group is not None or self.mode is not None
		base = os.environ.get('XDG_RUNTIME_DIR', '')
		if shared or not base or not os.path.isdir(base) or not os.access(base, os.W_OK):
			base = tempfile.gettempdir()  # others cannot enter a private runtime directory
		self.socketDir = tempfile.mkdtemp(prefix='tsvz-', dir=base)
		path = os.path.join(self.socketDir, 's')
		self.server = classes['unix'](path, classes['handler'])
		if gid is not None:
			os.chown(self.socketDir, -1, gid)
			os.chown(path, -1, gid)
		os.chmod(path, mode)
		os.chmod(self.socketDir, 0o700 | (0o010 if mode & 0o070 else 0) | (0o001 if mode & 0o007 else 0))
		self.address = 'unix:' + path
		self.server.serving = self
		lines = ['tsvz-handler\t{}'.format(_SERVE_PROTOCOL_VERSION), 'address\t' + _serveEncode(self.address),
				 'host\t' + _serveEncode(socket.gethostname()), 'pid\t{}'.format(os.getpid())]
		if not store.spec:
			lines.append('x-delimiter\t' + _serveEncode(store.delimiter.encode('unicode_escape').decode('ascii')))
		f = self.pointerFile
		if hasattr(os, 'fchmod'):
			os.fchmod(f.fileno(), 0o644)  # readable by all, writable by its owner only
		f.seek(0)
		f.truncate()
		f.write(''.join(line + '\n' for line in lines).encode('utf-8'))
		f.flush()
		os.fsync(f.fileno())

	def serve(self):
		"""Answer requests in this thread until ``stop()`` or the idle timeout."""
		if self.idleTimeout:
			watcher = threading.Thread(target=self._watchIdle, name='tsvz-idle')
			watcher.daemon = True
			watcher.start()
		self.server.serve_forever(poll_interval=0.2)

	def stop(self):
		"""Make ``serve()`` return (from any other thread)."""
		self.stopping.set()
		if self.server is not None:
			stopper = threading.Thread(target=self.server.shutdown, name='tsvz-stop')
			stopper.daemon = True
			stopper.start()

	def close(self):
		"""Stop listening, end open connections, write and fsync everything, remove the socket and the pointer."""
		import socket
		if self.server is not None:
			self.server.server_close()
		with self.guard:
			sockets = list(self.sockets)
		for sock in sockets:
			try:
				sock.shutdown(socket.SHUT_RDWR)
			except OSError:
				pass
		if self.store is not None:
			self.store.close()
		if self.socketDir is not None:
			shutil.rmtree(self.socketDir, ignore_errors=True)
		if self.pointerFile is not None:
			try:
				if self._isPointer(self.pointerFile):
					os.unlink(self.pointer)
			except OSError:
				pass
			self.pointerFile.close()
			self.pointerFile = None

	def _watchIdle(self):
		while not self.stopping.wait(min(1.0, self.idleTimeout / 4.0)):
			with self.guard:
				idle = not self.active and time.monotonic() - self.lastActivity >= self.idleTimeout
			if idle:
				if self.logger is not None:
					self.logger.teelog('tsvz: {}: idle for {:g} s; stopping'.format(self.storePath, self.idleTimeout))
				self.stop()
				return

	def connection(self, rfile, wfile, sock=None):
		"""Answer the requests of one connection (spec §21.3–§21.10)."""
		with self.guard:
			self.active += 1
			if sock is not None:
				self.sockets.add(sock)
		try:
			while True:
				line = self._readLine(rfile)
				if line is None:
					return
				bulk = None
				if _serveNeedsBulk(line):
					bulk = []
					while True:
						record = self._readLine(rfile)
						if record is None:
							return
						if record == '#':
							break
						bulk.append(record)
				if _serveIsStop(line):
					self.store.flush()
					self._send(wfile, ['#0'])
					self.stop()
					return
				self._send(wfile, _serveRespond(self.store, line, bulk, self.logger))
		except OSError:
			return  # the client went away
		finally:
			with self.guard:
				self.active -= 1
				self.sockets.discard(sock)
				self.lastActivity = time.monotonic()

	def _readLine(self, rfile):
		"""One request line without its terminator; None at the end of the connection or past the limit."""
		raw = rfile.readline(_SERVE_LINE_LIMIT + 1)
		if not raw.endswith(b'\n'):
			return None
		text = raw[:-1].decode('utf-8', 'replace')
		return text[:-1] if text.endswith('\r') else text

	def _send(self, wfile, lines):
		wfile.write(''.join(line + '\n' for line in lines).encode('utf-8', 'replace'))
		wfile.flush()


def _serveSignals():
	"""Make SIGTERM and SIGHUP stop ``serve`` as SIGINT does; return a function that restores them.

	A signal that is ignored (``nohup``) stays ignored.
	"""
	import signal
	if threading.current_thread() is not threading.main_thread():
		return lambda: None

	def handler(signum, frame):
		raise _ServeSignal()
	saved = []
	for name in ('SIGTERM', 'SIGHUP'):
		number = getattr(signal, name, None)
		if number is not None and signal.getsignal(number) is not signal.SIG_IGN:
			saved.append((number, signal.signal(number, handler)))
	return lambda: [signal.signal(number, old) for number, old in saved]


def _serveHoldSignals():
	"""Ignore SIGINT, SIGTERM and SIGHUP while the handler closes; return a function that restores them."""
	import signal
	if threading.current_thread() is not threading.main_thread():
		return lambda: None
	saved = []
	for name in ('SIGINT', 'SIGTERM', 'SIGHUP'):
		number = getattr(signal, name, None)
		if number is not None:
			saved.append((number, signal.signal(number, signal.SIG_IGN)))
	return lambda: [signal.signal(number, old) for number, old in saved]


class TSVZClient(MutableMapping):
	"""A store kept by ``tsvz serve`` (spec §21), as a mapping with ``TSVZed``'s semantics.

	Every operation is one request to the handler that the store's pointer
	file names, so many processes share one loaded store and one writer.
	Without a running handler (or when it goes away) the client warns once
	and runs the same engine in this process: the code keeps working, it just
	does not share the loaded store.

	- ``c[key]`` returns the row; for a missing key of a ``.tsvz`` store the
	  defaults row while ``#_return_defaults_when_missing_#`` is true (spec
	  §14.5), else ``KeyError``. ``get``, ``in``, ``len`` and iteration behave
	  like a dict; ``keys()``, ``values()`` and ``items()`` return lists.
	- ``c[key] = value`` normalises ``value`` as ``TSVZed`` does (a string is
	  split on the delimiter, cells are right-stripped, the key goes first).
	- ``sync=True`` acknowledges every write only after it is on disk.
	- ``timeout``: seconds to wait for each answer (None: no limit).
	- ``errors``: 'strict' (the default) raises a ``ValueError`` (``TSVZEncodeError``) for a write whose
	  key or value cannot be encoded as UTF-8 (a lone surrogate from a non-UTF-8 file name), before
	  anything is sent; 'replace' writes '?' for it. Any codec error handler is taken (a name that is
	  not one is a ``ValueError`` at construction); text goes to the handler as UTF-8, so what
	  ``'surrogateescape'`` would write as a raw byte arrives as U+FFFD. With 'strict' a
	  key that no store can hold (a lone surrogate) is a missing key, answered here with or without a
	  handler as ``TSVZed`` answers it (``KeyError`` or, while the store returns defaults for a missing
	  key, the defaults row; ``False``; the default; a delete that does nothing); with another handler
	  the key is the one a write of it stores (``'k?'`` for ``replace``). ``setdefault`` of a live key
	  returns its row whatever the default holds.
	"""

	def __init__(self, fileName, sync=False, timeout=None, teeLogger=None, errors='strict'):
		self._errors = _checkErrors(errors)
		self._fileName = fileName
		self._sync = sync
		self._timeout = timeout
		self.teeLogger = teeLogger
		self._spec = _isSpecPath(fileName)
		self.delimiter = get_delimiter(..., file_name=fileName)
		self._backend = None
		self._lock = threading.RLock()
		self._connect()

	def _connect(self):
		info, problem = _serveLookup(self._fileName)
		reason = problem or 'no handler is running'
		if info is not None:
			try:
				self._backend = _ServeConnection(info, self._timeout)
				self.delimiter = info.get('x-delimiter', self.delimiter)  # a loose store's, as its handler splits it
				return
			except _ServeUnreachable as e:
				reason = 'its handler cannot be used ({})'.format(e)
		_warnOnce(self, 'local', self._fileName, '{}; working on the files in this process'.format(reason),
				  self.teeLogger)
		self._backend = _ServeLocal(_ServeStore(self._fileName, teeLogger=self.teeLogger, create=True))

	def _request(self, fields, write=False, bulk=None, retry=True):
		"""Send a request; return ``(lines, status, message)``. Usage errors raise ``ValueError``."""
		if write:
			if self._errors == 'strict':
				self._checkSendable(fields, bulk)
			else:
				fields = [self._encoded(text) for text in fields]
				bulk = None if bulk is None else [self._encoded(text) for text in bulk]
		options = (['--sync'] if write and self._sync else []) + (['--x-written'] if write else [])
		line = '\t'.join(options + fields)
		with self._lock:  # one request at a time on the shared connection
			try:
				lines, diagnostics, status, message = self._backend.request(line, bulk)
			except _ServeLost as e:
				self._backend.close()
				self._connect()
				if not retry:
					raise ConnectionError('the tsvz handler of {} went away ({}); {} may or may not have been '
										  'applied'.format(self._fileName, e, fields[0]))
				lines, diagnostics, status, message = self._backend.request(line, bulk)
		for text in diagnostics:
			_warn(text, self.teeLogger)
		if status == 2:
			raise ValueError(message)
		if status == 1 and write:
			raise OSError(message)
		if status == 1:
			_warn('TSVZ warning: {}: {}'.format(self._fileName, message), self.teeLogger)
		return lines, status, message

	@staticmethod
	def _checkSendable(fields, bulk=None):
		"""Raise ``TSVZEncodeError`` when a field of a request cannot be sent as UTF-8."""
		for text in fields + (bulk or []):
			_encodeText(text, 'utf-8')

	def _encoded(self, text):
		"""``text`` as this client's ``errors=`` handler turns it into UTF-8 text: what a writer would store."""
		return _encodeText(text, 'utf-8', self._errors).decode('utf-8', 'replace')

	def _cells(self, key, value):
		"""``value`` normalised as ``TSVZed.__setitem__`` does, with ``key`` first."""
		key = str(key).rstrip()
		if isinstance(value, str):
			value = value.split(self.delimiter)
		cells = [str(cell).rstrip() if cell else '' for cell in value]
		if not cells or cells[0] != key:
			cells = [key] + cells
		return cells

	@staticmethod
	def _fields(cells):
		return [_serveEncode(cell) for cell in cells]

	@property
	def dialect(self):
		"""'tsvz' for tsvz-spec-v1 files (.tsvz/.csvz/.nsvz/.psvz), 'tsv' for 3.39-format files."""
		return 'tsvz' if self._spec else 'tsv'

	def _named(self, key):
		"""``(text, absent)``: the key as it is sent, and whether no store can hold it.

		With ``errors='strict'`` text that is not UTF-8 (a lone surrogate) names no key: a writer
		refuses it and a reader reads the byte as U+FFFD, never as itself, so nothing is sent
		and the answer is that of a missing key, as ``TSVZed`` and ``TSVZedLite`` answer it.
		With another handler the key is what a write of it would store (``'k?'`` for ``replace``),
		as a ``TSVZed`` with that ``errors=`` writes the tombstone of it.
		"""
		key = str(key)
		try:
			key.encode('utf-8')
		except UnicodeEncodeError:
			if self._errors == 'strict':
				return key, True
			return self._encoded(key), False
		return key, False

	def __getitem__(self, key):
		name, absent = self._named(key)
		if absent:
			# A missing key reads as the defaults while the store says so (spec §14.5), which only the handler
			# knows: ask for a key that is not in the store (nobody writes 96 random bits) and name the row as asked.
			lines = self._request(['get', 'tsvz-absent-' + os.urandom(12).hex()])[0]
			if not lines:
				raise KeyError(key)
			row = _serveDecodeLine(lines[0])
			row[0] = name.rstrip()
			return row
		lines = self._request(['get', _serveEncode(name)])[0]
		if not lines:
			raise KeyError(key)
		return _serveDecodeLine(lines[0])

	def get(self, key, default=None):
		name, absent = self._named(key)
		if absent:
			return default
		lines, status, _ = self._request(['get', _serveEncode(name)])
		return _serveDecodeLine(lines[0]) if status == 0 and lines else default

	def __contains__(self, key):
		name, absent = self._named(key)
		if absent:
			return False
		return self._request(['has', _serveEncode(name)])[1] == 0

	def __len__(self):
		return int(self._request(['len'])[0][0])

	def __iter__(self):
		return iter(self.keys())

	def keys(self):
		return [_specDecodeField(line, '\t') for line in self._request(['keys'])[0]]

	def values(self):
		return [_serveDecodeLine(line) for line in self._request(['read'])[0]]

	def items(self):
		return [(row[0], row) for row in self.values()]

	def __setitem__(self, key, value):
		cells = self._cells(key, value)
		if not cells[0]:
			_warn('TSVZ error: {}: a key cannot be empty'.format(self._fileName), self.teeLogger)
			return
		self._request(['set'] + self._fields(cells), write=True)

	def __delitem__(self, key):
		"""Delete ``key``; a missing key is not an error (as in ``TSVZed``)."""
		key, absent = self._named(key)
		if absent:
			return  # no such key: a missing key is not an error
		# Resending the delete of a key ending in whitespace could name, and delete, the stripped key (spec §20.2.7).
		retry = not (self._spec and _specStrippable(key, self.delimiter))
		self._request(['delete', _serveEncode(key)], write=True, retry=retry)

	_marker = object()

	def pop(self, key, default=_marker):
		name, absent = self._named(key)
		if absent:
			if default is TSVZClient._marker:
				raise KeyError(key)
			return default
		lines, status, _ = self._request(['pop', _serveEncode(name)], write=True, retry=False)
		if lines:
			return _serveDecodeLine(lines[0])
		if default is TSVZClient._marker:
			raise KeyError(key)
		return default

	def popitem(self, last=True):
		lines = self._request(['popitem', 'last' if last else 'first'], write=True, retry=False)[0]
		if not lines:
			raise KeyError('popitem(): {} is empty'.format(self._fileName))
		row = _serveDecodeLine(lines[0])
		return row[0], row

	def setdefault(self, key, default=None):
		"""The row of ``key``; when it is missing, first set it to ``default`` (None: only look it up)."""
		if default is None:
			return self.get(key)
		cells = self._cells(key, default)
		if len(cells) < 2:
			return self.get(key)
		if str(key) != cells[0]:
			# cells[0] is stripped as set strips it; a live key equal to ``key`` wins first (spec §20.2.7).
			row = self.get(key)
			if row is not None:
				return row
		if self._errors == 'strict':
			try:
				self._checkSendable(['setdefault'] + self._fields(cells))
			except TSVZEncodeError:
				# Nothing is written for it, so the handler need not decide: when the key is live the default is
				# never looked at (as TSVZed.setdefault, which asks `in` first), else the write is refused.
				row = self.get(cells[0])
				if row is not None:
					return row
				raise
		lines = self._request(['setdefault'] + self._fields(cells), write=True, retry=False)[0]
		return _serveDecodeLine(lines[0]) if lines else None

	def update(self, other=(), **kwds):
		"""Set many keys with one request (one batch append)."""
		pairs = list(other.items()) if hasattr(other, 'items') else list(other)
		pairs += list(kwds.items())
		bulk = []
		for key, value in pairs:
			cells = self._cells(key, value)
			if cells[0]:
				bulk.append(_serveRecordLine(cells, bool(_MARKER_RE.match(cells[0]))))
		if bulk:
			self._request(['set', '-'], write=True, bulk=bulk)

	def clear(self):
		self._request(['clear'], write=True)

	def setDefaults(self, defaults):
		"""Write ``defaults`` as the store's ``#_defaults_#`` marker (as ``TSVZed.setDefaults``)."""
		if isinstance(defaults, str):
			defaults = defaults.split(self.delimiter)
		cells = [str(cell).rstrip() if cell else '' for cell in (defaults or [])]
		if not cells or cells[0] != DEFAULTS_INDICATOR_KEY:
			cells = [DEFAULTS_INDICATOR_KEY] + cells
		self._request(['set'] + self._fields(cells), write=True)

	def move_to_end(self, key, last=True):
		raise NotImplementedError('a served store keeps first-appearance order (spec §3.4)')

	def rewrite(self, *args, **kwargs):
		raise NotImplementedError('a served store is compacted with `tsvz scrub`')

	mapToFile = hardMapToFile = rewrite

	def close(self):
		with self._lock:
			if self._backend is not None:
				self._backend.close()
				self._backend = None

	def __del__(self):
		try:
			self.close()
		except Exception:
			pass

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		self.close()

	def __repr__(self):
		return 'TSVZClient({!r}, {})'.format(self._fileName, 'local' if isinstance(self._backend, _ServeLocal)
											   else 'served')


# ===========================================================================
# Command-line interface (tsvz-spec-v1 §20)
# ===========================================================================
_CLI_OPERATIONS = ('read', 'get', 'set', 'append', 'delete', 'clear', 'scrub', 'verify', 'parts',
				   'has', 'len', 'keys', 'pop', 'popitem', 'setdefault', 'serve', 'stop')
#: option -> (destination, takes a value). The 3.39 spellings -c/--header,
#: --defaults, -s/--strict and -f/--force are undocumented aliases of the
#: --x- extensions (spec §20.7.4).
_CLI_OPTIONS = {
	'-h': ('help', False), '--help': ('help', False),
	'-V': ('version', False), '--version': ('version', False),
	'-q': ('quiet', False), '--quiet': ('quiet', False),
	'-v': ('verbose', False), '--verbose': ('verbose', False),
	'--format': ('format', True),
	'-d': ('delimiter', True), '--delimiter': ('delimiter', True),
	'--x-header': ('header', True), '-c': ('header', True), '--header': ('header', True),
	'--x-defaults': ('defaults', True), '--defaults': ('defaults', True),
	'--x-strict': ('strict', False), '-s': ('strict', False), '--strict': ('strict', False),
	'--x-force': ('force', False), '-f': ('force', False), '--force': ('force', False),
	'--x-direct': ('direct', False), '--x-idle-timeout': ('idleTimeout', True),
	'--x-group': ('group', True), '--x-mode': ('mode', True),
}
_CLI_NEGATIVE_NUMBER_RE = re.compile(r'^-[0-9.]')
_CLI_USAGE = 'usage: tsvz [OPTION ...] OPERATION STORE [ARG ...]   (tsvz -h for help)'
_CLI_HELP = '''usage: tsvz [OPTION ...] OPERATION STORE [ARG ...]
       tsvz [OPTION ...] STORE [OPERATION] [ARG ...]   (3.39 form; OPERATION defaults to read)

TSVZ {version}: key-value stores in .tsv/.csv/.nsv/.psv files (TSVZ 3.39 rules)
and in .tsvz/.csvz/.nsvz/.psvz files (tsvz-spec-v1).

operations:
  read STORE                   print every live row
  get STORE KEY [KEY ...]      print the rows of KEYs (exit 3 if one is missing)
  set STORE KEY [VALUE ...]    write a row; KEY alone deletes it; '-' reads rows from stdin
  append ...                   same as set
  delete STORE KEY [KEY ...]   delete KEYs; '-' reads keys from stdin
  clear STORE                  empty the store; a .tsvz keeps its header and
                               markers, a .tsv the --x-header it is given
  scrub STORE                  compact the store in place (archival maintenance)
  verify STORE                 check #_checksum_*_# segments (exit 4 on a mismatch)
  parts STORE                  list the parts of a multi-part store
  has STORE KEY                exit 0 if KEY is live, 3 if not
  len STORE                    print the number of live keys
  keys STORE                   print the live keys
  pop STORE KEY                print the row of KEY and delete it (exit 3 if missing)
  popitem STORE [first|last]   pop the first or last live key (default: last)
  setdefault STORE KEY VALUE [VALUE ...]
                               print the row of KEY, setting it to the VALUEs if missing
  serve STORE                  keep STORE loaded and answer requests on a local socket
  stop STORE                   stop the server of STORE

options:
  -h, --help                   show this help
  -V, --version                show the version
  --format table|records       output format (default: table on a terminal, else records)
  -q, --quiet                  print errors only
  -v, --verbose                print more detail
  -d, --delimiter D            .tsv-family delimiter: tab, comma, pipe, null or one character
  --x-header H                 a .tsv's header: line 1 when it is H, else a row
                               (--x-strict: an error); a new file begins with H
  --x-defaults D               defaults row KEY D1 D2 ... joined by the delimiter (KEY is ignored)
  --x-strict, --x-force        turn the 3.39 column and header checks on or off (default: off)
  --x-direct                   work on the files even when a server runs
  --x-idle-timeout S           serve: stop after S seconds without connections
  --x-group G, --x-mode M      serve: let group G connect / set the socket's octal mode
  --                           every argument after this is positional
  (--x-header and --x-defaults decode backslash escapes such as \\t)

exit status: 0 done, 1 failed or refused, 2 usage error, 3 missing key, 4 checksum mismatch
'''


class _CliUsageError(Exception):
	"""A command line that does not follow spec §20.1 / §20.7 (exit status 2)."""


class _CliArgs(object):
	"""A parsed command line (spec §20.1)."""

	def __init__(self):
		self.help = False
		self.version = False
		self.quiet = False
		self.verbose = False
		self.format = None
		self.delimiter = ...
		self.header = ''
		self.defaults = None
		self.strict = False
		self.direct = False
		self.idleTimeout = None
		self.group = None
		self.mode = None
		self.operation = None
		self.store = None
		self.args = []
		self.writeArgs = None  # setdefault: the arguments as a writer must see them (a non-UTF-8 byte kept), when they differ from args


_CLI_UNDECODED_RE = re.compile('[\udc80-\udcff]')


def _cliDecodeArgv(argv, errors='replace'):
	"""Re-decode as UTF-8 the arguments holding bytes the locale could not decode (Python 3.6 under LANG=C).

	Only data arguments go through here: a STORE path stays as the OS decoded
	it, so that ``open()`` encodes it back to the same bytes. A byte that is
	not UTF-8 becomes U+FFFD, as a reader reads it (spec §4.8), so a lookup
	names the key that was stored; for ``errors='surrogateescape'`` it stays
	a lone surrogate, which a writer refuses.
	"""
	return [os.fsencode(arg).decode('utf-8', errors) if _CLI_UNDECODED_RE.search(arg) else arg for arg in argv]


def _cliParseArgs(argv):
	"""Split ``argv`` per spec §20.1 into a ``_CliArgs``; raise ``_CliUsageError`` if it does not parse.

	Options may appear anywhere before a lone ``--``. ``-`` and negative numbers
	(``-5``, ``-.5``) are positional. Single-letter flags combine (``-qv``) and a
	single-letter option takes its value attached (``-dcomma``). The first
	positional argument decides the form: an operation name selects
	``OPERATION STORE [ARG ...]``, anything else the 3.39
	``STORE [OPERATION] [ARG ...]`` form.
	"""
	args = _CliArgs()
	argv = list(argv)
	positionals = []
	i = 0
	while i < len(argv):
		arg = argv[i]
		i += 1
		if arg == '--':
			positionals.extend(argv[i:])
			break
		if not arg.startswith('-') or arg == '-' or _CLI_NEGATIVE_NUMBER_RE.match(arg):
			positionals.append(arg)
			continue
		if arg.startswith('--'):
			name, eq, value = arg.partition('=')
		elif len(arg) > 2 and _CLI_OPTIONS.get(arg[:2], (None, False))[1]:
			value = arg[2:]
			name, eq, value = arg[:2], '=', value[1:] if value.startswith('=') else value  # -dcomma, -d=,
		elif len(arg) > 2:
			name, eq, value = arg[:2], '', ''
			argv.insert(i, '-' + arg[2:])  # -qv: take one flag at a time
		else:
			name, eq, value = arg, '', ''
		if name not in _CLI_OPTIONS and name.startswith('--'):
			# A unique prefix of a long option, as 3.39's argparse accepted (--verb, --delim).
			matches = sorted(option for option in _CLI_OPTIONS if option.startswith(name) and option.startswith('--'))
			if len(set(_CLI_OPTIONS[option] for option in matches)) > 1:
				raise _CliUsageError('ambiguous option {}: {}'.format(name, ', '.join(matches)))
			if matches:
				name = matches[0]
		if name not in _CLI_OPTIONS:
			raise _CliUsageError('unknown option {}'.format(name))
		dest, takesValue = _CLI_OPTIONS[name]
		if takesValue:
			if not eq:
				if i >= len(argv):
					raise _CliUsageError('option {} needs a value'.format(name))
				value = argv[i]
				i += 1
			setattr(args, dest, value)
		elif eq:
			raise _CliUsageError('option {} takes no value'.format(name))
		elif dest == 'force':
			args.strict = False
		else:
			setattr(args, dest, True)
	if args.help or args.version:
		return args
	if args.format not in (None, 'table', 'records'):
		raise _CliUsageError('--format must be table or records, not {!r}'.format(args.format))
	if not positionals:
		raise _CliUsageError('missing OPERATION and STORE')
	if positionals[0] in _CLI_OPERATIONS:
		if len(positionals) < 2:
			raise _CliUsageError('{} needs a STORE'.format(positionals[0]))
		args.operation, args.store, args.args = positionals[0], positionals[1], positionals[2:]
	else:
		args.store = positionals[0]
		if len(positionals) == 1:
			args.operation = 'read'
		elif positionals[1] in _CLI_OPERATIONS:
			args.operation, args.args = positionals[1], positionals[2:]
		else:
			raise _CliUsageError('unknown operation: neither {!r} nor {!r} is one of {}'.format(
				positionals[0], positionals[1], ', '.join(_CLI_OPERATIONS)))
	if args.operation in ('read', 'clear', 'scrub', 'verify', 'parts', 'len', 'keys', 'serve', 'stop') and args.args:
		raise _CliUsageError('{} takes no arguments after STORE'.format(args.operation))
	if args.operation == 'popitem' and args.args not in ([], ['first'], ['last']):
		raise _CliUsageError('popitem takes first or last')
	if args.operation in ('has', 'pop') and len(args.args) > 1:
		raise _CliUsageError('{} takes one KEY'.format(args.operation))
	if args.operation == 'setdefault' and len(args.args) == 1:
		raise _CliUsageError('setdefault needs a KEY and a VALUE')
	if args.operation in ('get', 'set', 'append', 'delete', 'has', 'pop', 'setdefault'):
		if not args.args:
			raise _CliUsageError('{} needs a KEY'.format(args.operation))
		keys = args.args if args.operation in ('get', 'delete') else args.args[:1]
		if '' in keys:
			raise _CliUsageError('{}: a KEY must not be empty'.format(args.operation))
	return args


def _cliWrite(stream, text):
	"""Write ``text`` to ``stream`` as UTF-8 and flush (Python 3.6 under LANG=C cannot encode non-ASCII)."""
	buffer = getattr(stream, 'buffer', None)
	if buffer is None:
		stream.write(text)
	else:
		stream.flush()  # keep the order of text already written through the text layer
		try:
			data = text.encode('utf-8', 'surrogateescape')  # a STORE path the locale could not decode
		except UnicodeEncodeError:
			data = text.encode('utf-8', 'replace')
		buffer.write(data)
	stream.flush()


class _CliLogger(object):
	"""``teeLogger`` for the CLI: every diagnostic on stderr (spec §20.5); ``quiet`` keeps errors only."""

	def __init__(self, stream, quiet=False):
		self.stream = stream
		self.quiet = quiet

	def teelog(self, message, level='info', callerStackDepth=None):
		if self.quiet and level not in ('error', 'critical'):
			return
		_cliWrite(self.stream, '{}\n'.format(message))


def _cliFormat(args, stdout):
	"""The output format (spec §20.4.3): ``--format``, else table on a terminal and records otherwise."""
	if args.format:
		return args.format
	try:
		return 'table' if stdout.isatty() else 'records'
	except Exception:
		return 'records'


#: Control characters, plus the bidi controls and line separators a terminal may act on (Trojan Source).
_CLI_CONTROL_RE = re.compile('[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]')
_CLI_CONTROL_ESCAPES = {'\t': '\\t', '\n': '\\n', '\r': '\\r'}


def _cliTableCells(rows):
	"""``rows`` as a table shows them: control characters as ``\\r``, ``\\n``, ``\\t``, ``\\xNN`` or
	``\\uNNNN``, so no cell moves the cursor, reorders the line or sends the terminal an escape sequence.
	The table is for reading; records are exact (a backslash in a value is not escaped here)."""
	def escape(match):
		ch = match.group()
		return _CLI_CONTROL_ESCAPES.get(ch) or ('\\x{:02x}' if ch < '\u0100' else '\\u{:04x}').format(ord(ch))
	return [[_CLI_CONTROL_RE.sub(escape, str(cell)) for cell in row] for row in rows]


def _cliEmitRows(rows, args, stdout, spec, delimiter, headerRow=False, header=None):
	"""Print store rows for ``read`` / ``get`` (spec §20.4): §13 records, 3.39 records or a table.

	The table shows the rows only, each padded to the widest and none truncated to the
	terminal's width. ``headerRow`` keeps 3.39's ``read`` table of a loose file, whose
	first row is the file's header line; ``header``, a loose file's header line that is not
	a row (``--x-header``), is shown there instead. Records never hold it.
	"""
	if _cliFormat(args, stdout) == 'table':
		if headerRow and header:
			rows = [header] + rows
		rows = _cliTableCells(rows)
		if headerRow or not rows:
			if rows:  # (the header row as wide as the widest row: a row is shown whole, never cut to the header)
				width = max(len(row) for row in rows)
				rows[0] = rows[0] + [''] * (width - len(rows[0]))
			_cliWrite(stdout, pretty_format_table(rows, delimiter=delimiter) + '\n')
			return
		width = max(len(row) for row in rows)
		rows = [row + [''] * (width - len(row)) for row in rows]
		_cliWrite(stdout, pretty_format_table(rows, delimiter=delimiter, header=False, full=True) + '\n')
		return
	if spec:
		lines = [_specFormatRecord(row, delimiter) for row in rows]
	else:
		lines = [delimiter.join(_sanitize(row, delimiter=delimiter)) for row in rows]
	_cliWrite(stdout, ''.join(line + '\n' for line in lines))


def _cliEmitFields(rows, header, args, stdout):
	"""Print ``verify`` / ``parts`` rows: TAB-joined §13-encoded fields, or a table under ``header``."""
	if _cliFormat(args, stdout) == 'table':
		if rows:
			_cliWrite(stdout, pretty_format_table(_cliTableCells([header] + rows)) + '\n')
		return
	_cliWrite(stdout, ''.join('\t'.join(_specEncodeField(str(field), '\t') for field in row) + '\n' for row in rows))


class _CliStoreMissing(Exception):
	"""An operation that reads a store found none at STORE (spec §20.2.2, exit status 1)."""


def _cliDecodeEscapes(text, label, logger):
	"""Decode backslash escapes in an extension option's value as 3.39 did (``-c 'id\\tval'``).

	Unlike 3.39, non-ASCII text survives. A value that does not decode is
	reported and ignored ('' is returned), as in 3.39.
	"""
	if text.endswith('\\'):
		text += '\\'
	try:
		return codecs.decode(text.encode('latin-1', 'backslashreplace'), 'unicode_escape')
	except Exception:
		logger.teelog('tsvz: failed to decode {} {!r}; ignored'.format(label, text), 'warning')
		return ''


def _cliDelimiter(args, logger):
	"""STORE's delimiter: a strict extension's own (spec §5.1, §20.7), else ``-d``, else inferred from the name.

	A ``-d`` that does not decode is a usage error; one longer than a
	character is used, as 3.39 did, with a warning.
	"""
	given = None
	if args.delimiter is not ... and args.delimiter:
		try:
			given = get_delimiter(args.delimiter)
		except Exception:
			raise _CliUsageError('-d {!r} is not a delimiter'.format(args.delimiter))
	if _isSpecPath(args.store):
		expected = _EXTENSION_DELIMITERS[_parsePartName(args.store).ext]
		if given is not None and given != expected:
			logger.teelog('TSVZ warning: {}: -d {!r} conflicts with the file extension; using {!r} (spec §5.1)'.format(
				args.store, args.delimiter, expected), 'warning')
		return expected
	if given is None:
		return get_delimiter(args.delimiter, file_name=args.store)
	if len(given) != 1:
		logger.teelog('TSVZ warning: {}: -d {!r} is {} characters long; a delimiter should be one character'.format(
			args.store, args.delimiter, len(given)), 'warning')
	return given


def _cliStoreExists(store):
	"""True when STORE exists; a spec store may consist of numbered parts only (spec §17.1)."""
	if _isSpecPath(store):
		return bool(_storeParts(store)[0])
	return os.path.isfile(store)


def _cliStdinLines(stdin, reporter):
	"""The committed lines of ``stdin`` (spec §4.3, §20.3), decoded as UTF-8, without terminators.

	An unterminated last line is dropped and reported; a leading byte order
	mark is dropped.
	"""
	source = getattr(stdin, 'buffer', stdin)
	data = source.read()
	if isinstance(data, str):
		data = data.encode('utf-8', 'surrogateescape')
	lines = data.split(b'\n')
	tail = lines.pop()
	if tail:
		reporter.note('stdin-tail', None, 'ignored an unterminated last line on standard input: {!r}'.format(tail[:80]))
	if lines and lines[0].startswith(b'\xef\xbb\xbf'):
		lines[0] = lines[0][3:]
	return [_decodeLine(raw, reporter, 'line {}'.format(lineNo)) for lineNo, raw in enumerate(lines, 1)]


def _parseRecordLines(texts, delimiter, spec, keysOnly, reporter):
	"""Parse record lines of a stream (spec §20.3) into ``[(cells, marker), ...]``.

	A line whose first field matches the reserved pattern is a marker line
	(``marker`` True, key kept as written). Other fields are decoded per §13
	for a spec store, by 3.39's rules otherwise. Comment lines and empty lines
	are skipped, and so is a line with an empty key (reported on
	``reporter``). ``keysOnly`` keeps the first field only.
	"""
	rows = []
	for lineNo, text in enumerate(texts, 1):
		fields = text.split(delimiter)
		if keysOnly:
			fields = fields[:1]
		first = fields[0]
		marker = bool(_MARKER_RE.match(first))
		if not marker and (first.startswith('#') or not text):
			continue
		if spec:
			cells = [first] + [_specDecodeField(field, delimiter) for field in fields[1:]] if marker else \
				[_specDecodeField(field, delimiter) for field in fields]
		else:
			cells = _unsanitize(fields, delimiter)
		if not cells[0]:
			reporter.note('empty-key', 'line {}'.format(lineNo), 'skipped a line with an empty key')
			continue
		rows.append((cells, marker))
	return rows


def _cliRequestLine(args):
	"""The protocol request (spec §21.5) for a parsed command line: its arguments are literal."""
	return '\t'.join([args.operation] + [_serveEncode(arg) for arg in args.args])


def _cliEmitServed(args, delimiter, logger, stdout, response, header=None):
	"""Print a response (``(rows, diagnostics, status, message)``) as the operation prints on the files (§20.8);
	return the exit status. ``header``: a loose file's header line in effect (``read``'s table shows it)."""
	rows, diagnostics, status, message = response
	for text in diagnostics:
		logger.teelog(text, 'warning')
	spec = _isSpecPath(args.store)
	operation = args.operation
	if operation in ('read', 'get', 'keys', 'pop', 'popitem', 'setdefault'):
		_cliEmitRows(rows, args, stdout, spec, delimiter, headerRow=operation == 'read' and not spec, header=header)
	elif operation == 'len':
		_cliWrite(stdout, ''.join(row[0] + '\n' for row in rows))
	elif operation == 'verify':
		_cliEmitFields(rows, ['path', 'line', 'algorithm', 'expected', 'computed'], args, stdout)
	elif operation == 'parts':
		_cliEmitFields(rows, ['index', 'ordinal', 'path', 'flags'], args, stdout)
	if message and status in (1, 2):
		logger.teelog('tsvz: {}: {}'.format(args.store, message), 'error')
	return status


def _cliSetdefaultLookup(args, delimiter, logger, stdout, request):
	"""``setdefault`` whose arguments hold a byte that is not UTF-8 (spec §20.2.5): it never writes.

	A live KEY is printed as ``setdefault`` prints it and nothing is written; a missing one would have to be
	written with the byte, so it is refused. KEY is looked up as a reader reads it (U+FFFD for the byte, §20.2.7)
	with one ``get`` request (``request``): through a handler that is the handler's own view, in one request, never
	a write with the byte replaced; on the files it is the in-process engine's.
	"""
	rows, diagnostics, status, message = request('\t'.join(['get', _serveEncode(args.args[0])]))
	if status == 3:
		for text in diagnostics:
			logger.teelog(text, 'warning')
		_checkRow(_sanitize(args.writeArgs, delimiter=delimiter), delimiter, 'utf8', 'strict', None)  # raises
	return _cliEmitServed(args, delimiter, logger, stdout, (rows[:1] if status == 0 else [], diagnostics, status, message))


def _cliBulk(args, delimiter, logger, stdin):
	"""The record lines ``set -`` / ``delete -`` read from standard input (spec §20.3), else None."""
	if args.operation not in ('set', 'append', 'delete') or args.args != ['-']:
		return None
	source = _Reporter('<stdin>', logger)
	try:
		rows = _parseRecordLines(_cliStdinLines(stdin, source), delimiter, _isSpecPath(args.store),
								 args.operation == 'delete', source)
	finally:
		source.flush()
	return [_serveRecordLine(cells, marker) for cells, marker in rows]


def _cliEngineOp(args, delimiter, logger, stdin, stdout):
	"""Every operation but ``serve`` and ``stop``, on the files (spec §20.2).

	It is one request to an in-process write-handler engine, so it behaves as it does through a handler
	(§20.8), except that ``pop``, ``popitem``, ``setdefault`` and a ``delete`` that reads the store are not
	atomic against other writers (§20.2.5).
	"""
	operation = args.operation
	exists = _cliStoreExists(args.store)
	if not exists and operation not in _CLI_CREATES:
		raise _CliStoreMissing()
	if operation == 'setdefault' and not exists:
		# What a refused setdefault would write is looked at before the store is created, so it creates nothing.
		header = _formatHeader(args.header, delimiter=delimiter)
		if any(header):
			_checkRow(header, delimiter, 'utf8', 'strict', what='header')
		if args.defaults and len(args.defaults) > 1:
			_checkRow(args.defaults, delimiter, 'utf8', 'strict', what='defaults')
		if args.writeArgs is not None:
			_checkRow(_sanitize(args.writeArgs, delimiter=delimiter), delimiter, 'utf8', 'strict', None)  # raises
	bulk = _cliBulk(args, delimiter, logger, stdin)
	store = _ServeStore(args.store, delimiter=delimiter, header=args.header, defaults=args.defaults,
						strict=args.strict, teeLogger=logger, verbose=args.verbose,
						create=not exists and operation != 'clear',  # clear creates the store itself
						createDefaults=None if operation == 'setdefault' else [], lazy=True)

	def request(line, bulk=None):
		"""The response to ``line``; the engine's messages go to stderr as they come, at their own level."""
		try:
			status, rows, message = _serveDispatch(store, line, bulk, logger)
		except _ServeUsage as e:
			status, rows, message = 2, [], str(e)
		return rows, [], status, message
	try:
		if args.writeArgs is not None:
			return _cliSetdefaultLookup(args, delimiter, logger, stdout, request)
		written = ['--x-written'] if operation in ('set', 'append', 'delete') else []  # a failed write fails
		response = request('\t'.join(written + [_cliRequestLine(args)]), bulk)
	finally:
		store.close()
	return _cliEmitServed(args, delimiter, logger, stdout, response, header=store.header if any(store.header) else None)


def _cliServe(args, delimiter, logger, stdin, stdout):
	"""``serve STORE`` (spec §21.13): run a write handler for STORE in the foreground until it is stopped."""
	try:
		idle = float(args.idleTimeout) if args.idleTimeout else 0.0
		mode = int(args.mode, 8) if args.mode else None
		_serveGroup(args.group)
	except (ValueError, KeyError) as e:
		raise _CliUsageError('serve: bad --x-idle-timeout, --x-mode or --x-group ({})'.format(e))
	if not _serveSupported():
		logger.teelog('tsvz: {}: tsvz serve needs Unix-domain sockets, which Python does not provide on this '
					  'platform; tsvz commands work on the files'.format(args.store), 'error')
		return 1
	handle = _ServeHandle(args.store, logger=logger, group=args.group, mode=mode, idleTimeout=idle)
	try:
		handle.lock()
	except _ServeBusy as e:
		info = e.args[0]
		logger.teelog('tsvz: {}: already served by pid {} on host {}'.format(
			args.store, info.get('pid', '?'), info.get('host', '?')), 'error')
		return 1
	except OSError as e:
		logger.teelog('tsvz: {}: cannot lock the pointer file {}: {}'.format(
			args.store, handle.pointer, e), 'error')
		return 1
	restore = _serveSignals()  # before the pointer file announces this handler
	try:
		store = _ServeStore(args.store, delimiter=delimiter, header=args.header, createDefaults=args.defaults,
							strict=args.strict, teeLogger=logger, verbose=args.verbose, create=True)
		handle.start(store)
		logger.teelog('tsvz: serving {} at {} (pid {})'.format(args.store, handle.address, os.getpid()))
		handle.serve()
	except (KeyboardInterrupt, _ServeSignal):
		pass
	finally:
		held = _serveHoldSignals()  # a second signal must not cut the final writes short
		try:
			handle.close()
		finally:
			held()
			restore()
	logger.teelog('tsvz: stopped serving {}'.format(args.store))
	return 0


def _cliStop(args, delimiter, logger, stdin, stdout):
	"""``stop STORE`` (spec §21.13): ask the handler of STORE to stop, and wait until it has."""
	info, problem = _serveLookup(args.store)
	if info is None:
		logger.teelog('tsvz: {}: {}'.format(args.store, problem or 'no handler is running'), 'error')
		return 1
	try:
		connection = _ServeConnection(info)
	except _ServeUnreachable as e:
		logger.teelog('tsvz: {}: the handler does not answer ({})'.format(args.store, e), 'error')
		return 1
	try:
		status = connection.request('stop')[2]
	except _ServeLost as e:
		logger.teelog('tsvz: {}: {}'.format(args.store, e), 'error')
		return 1
	finally:
		connection.close()
	deadline = time.monotonic() + 10
	while time.monotonic() < deadline and (_serveFind(args.store) or {}).get('pid') == info.get('pid'):
		time.sleep(0.05)
	return 0 if status == 0 else 1


_CLI_WRITES_HEADER = ('set', 'append', 'setdefault', 'clear', 'delete', 'scrub', 'serve')
_CLI_CREATES = ('set', 'append', 'delete', 'clear', 'setdefault')  # a missing store (spec §20.2.2)
_CLI_READ_ONLY = ('read', 'get', 'has', 'len', 'keys', 'verify', 'parts')


def _cliHandler(args, delimiter, logger):
	"""A connection to STORE's handler when this command line may go through it (spec §20.8), else None.

	TSVZ routes a command line only when its result does not depend on
	options the handler does not share: no --x-direct, --x-header,
	--x-defaults or --x-strict, and for a loose file the handler's own
	delimiter. A setdefault whose arguments hold a byte that is not UTF-8, which a
	request cannot carry, is answered with one forwarded get of KEY (see
	_cliSetdefaultLookup): it never writes. A pointer whose
	handler does not answer is reported and the files are used; another host's
	handler, or one this user may not connect to, is skipped quietly.
	"""
	if args.operation in ('serve', 'stop', 'parts', 'verify') or args.direct or args.header or args.defaults \
			or args.strict:
		return None  # parts and verify read the files themselves, so they gain nothing from a handler
	info, problem = _serveLookup(args.store)
	if problem:
		logger.teelog('TSVZ warning: {}: ignoring the pointer file: {}; using the files directly'.format(
			args.store, problem), 'warning')
	if info is None:
		return None
	if not _isSpecPath(args.store) and info.get('x-delimiter', delimiter) != delimiter:
		return None
	try:
		return _ServeConnection(info)
	except _ServeUnreachable as e:
		if not e.quiet:
			logger.teelog('TSVZ warning: {}: the handler in {} does not answer ({}); using the files directly'.format(
				args.store, _servePointerPath(args.store), e), 'warning')
		return None


def _cliRouted(connection, args, delimiter, logger, stdin, stdout):
	"""Run a command line through STORE's handler; print the response as the files would give it (§20.8)."""
	def request(line, bulk=None):
		lines, diagnostics, status, message = connection.request(line, bulk)
		return [_serveDecodeLine(line) for line in lines], diagnostics, status, message
	if args.writeArgs is not None:
		try:
			return _cliSetdefaultLookup(args, delimiter, logger, stdout, request)
		except _ServeLost as e:  # a lookup only: the files answer it as well
			logger.teelog('TSVZ warning: {}: lost the handler ({}); using the files directly'.format(args.store, e),
						  'warning')
			return _cliEngineOp(args, delimiter, logger, stdin, stdout)
	bulk = _cliBulk(args, delimiter, logger, stdin)
	try:
		written = ['--x-written'] if args.operation in ('set', 'append', 'delete') else []
		response = request('\t'.join(written + [_cliRequestLine(args)]), bulk)
	except _ServeLost as e:
		if args.operation in _CLI_READ_ONLY:
			logger.teelog('TSVZ warning: {}: lost the handler ({}); using the files directly'.format(args.store, e),
						  'warning')
			return _cliEngineOp(args, delimiter, logger, stdin, stdout)
		logger.teelog('tsvz: {}: lost the handler ({}); the {} may or may not have been applied'.format(
			args.store, e, args.operation), 'error')
		return 1
	return _cliEmitServed(args, delimiter, logger, stdout, response)


def _cliMain(argv, stdin=None, stdout=None, stderr=None):
	"""Run one ``tsvz`` command line (spec §20) and return its exit status (§20.6).

	The streams default to ``sys.stdin`` / ``sys.stdout`` / ``sys.stderr``.
	Library messages printed to stdout for 3.39 compatibility go to stderr.
	"""
	stdin = sys.stdin if stdin is None else stdin
	stdout = sys.stdout if stdout is None else stdout
	stderr = sys.stderr if stderr is None else stderr
	try:
		args = _cliParseArgs(argv)
	except _CliUsageError as e:
		_cliWrite(stderr, 'tsvz: {}\n{}\n'.format(e, _CLI_USAGE))
		return 2
	# What a write is given must not be changed silently: a byte that is not UTF-8 stays a lone surrogate
	# and the writer says so (a lookup names U+FFFD, as the reader reads the byte).
	# A lone KEY of set or append is a delete, and setdefault looks KEY up first: those name a key as a reader
	# reads it (U+FFFD); the data to be written keeps the byte. A header or defaults is written by every
	# operation that can create or rewrite a store.
	decoding = 'surrogateescape' if args.operation in ('set', 'append') and len(args.args) > 1 else 'replace'
	if args.operation == 'setdefault':
		raw = _cliDecodeArgv(args.args, 'surrogateescape')
		args.args = _cliDecodeArgv(args.args, 'replace')
		args.writeArgs = raw if raw != args.args else None
	else:
		args.args = _cliDecodeArgv(args.args, decoding)
	headerDecoding = 'surrogateescape' if args.operation in _CLI_WRITES_HEADER else 'replace'
	for name in ('header', 'defaults', 'delimiter'):
		if isinstance(getattr(args, name), str):
			setattr(args, name, _cliDecodeArgv([getattr(args, name)], headerDecoding if name != 'delimiter' else 'replace')[0])
	if args.help:
		_cliWrite(stdout, _CLI_HELP.format(version=version))
		return 0
	if args.version:
		prog = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else 'tsvz'
		_cliWrite(stdout, '{} {} @ {} by {}\n'.format(prog, version, COMMIT_DATE, author))
		return 0
	logger = _CliLogger(stderr, quiet=args.quiet)
	try:
		with contextlib.redirect_stdout(stderr):
			delimiter = _cliDelimiter(args, logger)
			args.header = _cliDecodeEscapes(args.header, '--x-header', logger) if args.header else ''
			if args.defaults:
				# 3.39's --defaults names the key column first; it is never used.
				values = _cliDecodeEscapes(args.defaults, '--x-defaults', logger).split(delimiter)
				args.defaults = [DEFAULTS_INDICATOR_KEY] + values[1:]
			else:
				args.defaults = []
			if args.operation in ('set', 'append') and len(args.args) > 1:
				# A value that is not UTF-8 is refused, naming the cell, before a store is created or a request
				# (which cannot carry it) is sent.
				_checkRow(_sanitize(args.args, delimiter=delimiter), delimiter, 'utf8', 'strict',
						  _formatHeader(args.header, delimiter=delimiter))
			connection = _cliHandler(args, delimiter, logger)
			if connection is not None:
				try:
					return _cliRouted(connection, args, delimiter, logger, stdin, stdout)
				finally:
					connection.close()
			run = {'serve': _cliServe, 'stop': _cliStop}.get(args.operation, _cliEngineOp)
			return run(args, delimiter, logger, stdin, stdout)
	except _CliUsageError as e:
		_cliWrite(stderr, 'tsvz: {}\n{}\n'.format(e, _CLI_USAGE))
		return 2
	except _CliStoreMissing:
		logger.teelog('tsvz: {}: no such store'.format(args.store), 'error')
	except BrokenPipeError:
		# The reader went away (``tsvz read big | head -1``). Point stdout at
		# devnull so the interpreter's final flush cannot raise again.
		try:
			os.dup2(os.open(os.devnull, os.O_WRONLY), stdout.fileno())
		except Exception:
			pass
	except KeyboardInterrupt:
		return 130
	except Exception as e:
		# A refusal's advice names options of the library (errors=), which this command does not take.
		text = e.reason if isinstance(e, TSVZEncodeError) else str(e)
		logger.teelog('tsvz: {}: {}'.format(args.store, text or type(e).__name__), 'error')
		if args.verbose:
			import traceback
			logger.teelog(traceback.format_exc().rstrip(), 'error')
	return 1


def _cliComplete():
	"""Shell completion through argcomplete when it is installed and driving this process (as in 3.39)."""
	if '_ARGCOMPLETE' not in os.environ:
		return
	try:
		import argparse
		import argcomplete
	except ImportError:
		return
	parser = argparse.ArgumentParser(prog='tsvz')
	parser.add_argument('operation', choices=_CLI_OPERATIONS)
	parser.add_argument('store')
	parser.add_argument('args', nargs='*')
	parser.add_argument('-V', '--version', action='store_true')
	parser.add_argument('--format', choices=('table', 'records'))
	parser.add_argument('-q', '--quiet', action='store_true')
	parser.add_argument('-v', '--verbose', action='store_true')
	parser.add_argument('-d', '--delimiter')
	parser.add_argument('--x-header')
	parser.add_argument('--x-defaults')
	parser.add_argument('--x-strict', action='store_true')
	parser.add_argument('--x-force', action='store_true')
	argcomplete.autocomplete(parser, always_complete_options='long')


def __main__():
	_cliComplete()
	sys.exit(_cliMain(sys.argv[1:]))
if __name__ == '__main__':
	__main__()
