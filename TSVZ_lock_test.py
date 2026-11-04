"""Inter-process and inter-thread file locks of .tsv-family files (5.3): readers take a shared lock while they read,
and the file lock is split into byte ranges (Linux open file description locks) or is a whole-file flock (macOS and
other POSIX systems). Separate from TSVZ_test.py so the macOS CI job runs exactly these.

Children run with ``sys.executable``; a child that waits for a release file gives up after 30 s."""
import errno
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import TSVZ  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX file locks')

LINUX = sys.platform.startswith('linux')
KINDS = ['ofd', 'flock'] if LINUX else ['flock']
NATIVE = getattr(TSVZ, '_LOCK_KIND', None)
linux_only = pytest.mark.skipif(not LINUX, reason='open file description locks are Linux-only')
needs_fork_hook = pytest.mark.skipif(not hasattr(os, 'register_at_fork'), reason='os.register_at_fork (Python 3.7+)')


def _tsv(tmp_path, n=1000, name='f.tsv'):
	"""A .tsv with the header ``id\\tv`` and ``n`` rows ``k%04d\\tvalue-k%04d``."""
	p = str(tmp_path / name)
	with open(p, 'wb') as f:
		f.write(b'id\tv\n' + b''.join(('k%04d\tvalue-k%04d\n' % (i, i)).encode() for i in range(n)))
	return p


PRELUDE = r'''
import fcntl, os, struct, sys, threading, time
sys.path.insert(0, {here!r})
import TSVZ as T
if sys.argv[1] != '-':
	T._LOCK_KIND = sys.argv[1]
ARGS = sys.argv[2:]

def ofd(f, kind, start, length, wait=True):
	fcntl.fcntl(f.fileno(), 38 if wait else 37, struct.pack('@hhqqi', kind, 0, start, length, 0) + b'\0' * 8)

def say(word):
	sys.stdout.write(word + '\n')
	sys.stdout.flush()

def released(path):
	end = time.time() + 30
	while not os.path.exists(path):
		if time.time() > end:
			sys.exit(2)
		time.sleep(0.01)
'''.format(here=HERE)


def _child(code, *args, **kwargs):
	"""A child running PRELUDE + ``code`` with ``args`` (ARGS); ``kind`` (keyword) sets its _LOCK_KIND."""
	kind = kwargs.pop('kind', '-')
	return subprocess.Popen([sys.executable, '-c', PRELUDE + code, kind] + [str(a) for a in args],
							stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


def _expect(child, word, timeout=20):
	"""Read the child's next line, which must be ``word``."""
	result = []
	reader = threading.Thread(target=lambda: result.append(child.stdout.readline()))
	reader.daemon = True
	reader.start()
	reader.join(timeout)
	line = result[0].decode().strip() if result else None
	if line != word:
		child.kill()
		raise AssertionError('child said {!r}, not {!r}: {}'.format(line, word, child.stderr.read().decode()[-2000:]))


def _kill(*children):
	for child in children:
		if child is not None and child.poll() is None:
			child.kill()
		if child is not None:
			child.wait()


OTHER = r'''
import errno, fcntl, struct, sys
p, how = sys.argv[1], sys.argv[2]
f = open(p, 'r+b')
try:
	if how == 'lockf':
		fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
	elif how == 'flock':
		fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
	elif how == 'flock-sh':
		fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
	else:
		kind, start, length = {'ofdw': (fcntl.F_WRLCK, 0, 0), 'ofdw1': (fcntl.F_WRLCK, 1, 1),
							   'ofdr0': (fcntl.F_RDLCK, 0, 1)}[how]
		fcntl.fcntl(f.fileno(), 37, struct.pack('@hhqqi', kind, 0, start, length, 0) + b'\0' * 8)
	print('free')
except OSError as e:
	print('busy' if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK) else 'error %d' % e.errno)
'''


def _other(path, how):
	"""Whether another process can take the lock ``how`` on ``path`` at once: 'free' or 'busy'."""
	out = subprocess.check_output([sys.executable, '-c', OTHER, path, how])
	return out.decode().strip()


#: A child holding a lock on ARGS[0] until ARGS[0] + '.release' exists. ARGS[1]: 'native' (T._lockFile, its default
#: mode), 'read' (T._readLock), 'lockf' (3.39), 'ofdw' (a whole-file open file description write lock, 4.9 to 5.2).
HOLDER = r'''
p, how = ARGS[0], ARGS[1]
f = open(p, 'r+b')
if how == 'native':
	T._lockFile(f)
elif how == 'read':
	cm = T._readLock(p)
	cm.__enter__()
elif how == 'lockf':
	fcntl.lockf(f, fcntl.LOCK_EX)
elif how == 'ofdw':
	ofd(f, fcntl.F_WRLCK, 0, 0)
say('held')
released(p + '.release')
'''


def _hold(path, how, kind='-'):
	child = _child(HOLDER, path, how, kind=kind)
	_expect(child, 'held')
	return child


def _release(path):
	open(path + '.release', 'w').close()


def _done_within(thread, seconds):
	thread.join(seconds)
	return not thread.is_alive()


@pytest.fixture(params=KINDS)
def kind(request, monkeypatch):
	"""The lock kind this test runs under (forced; children get it too)."""
	monkeypatch.setattr(TSVZ, '_LOCK_KIND', request.param, raising=False)
	return request.param


@pytest.fixture(autouse=True)
def registry_swept():
	"""After every test, no descriptor is left registered as locked."""
	yield
	held = getattr(TSVZ, '_HELD', None)
	if held:
		leftover = dict(held)
		held.clear()
		raise AssertionError('descriptors left registered as locked: {}'.format(leftover))


def _here():
	"""This thread's set of the files it holds a TSVZ lock on (5.3)."""
	return TSVZ._heldHere()


# ---------------------------------------------------------------------------
# The lock layer
# ---------------------------------------------------------------------------
@linux_only
def test_lock_modes_take_their_byte_ranges(tmp_path, monkeypatch):
	monkeypatch.setattr(TSVZ, '_LOCK_KIND', 'ofd', raising=False)
	p = _tsv(tmp_path, 3)
	with open(p, 'r+b') as f:
		assert TSVZ._lockFile(f, TSVZ._APPEND)
		assert [_other(p, h) for h in ('ofdr0', 'ofdw1', 'lockf', 'ofdw')] == ['free', 'busy', 'busy', 'busy']
		TSVZ._unlockFile(f)
		assert TSVZ._lockFile(f, TSVZ._REWRITE)
		assert [_other(p, h) for h in ('ofdr0', 'ofdw1', 'lockf')] == ['busy', 'busy', 'busy']
		TSVZ._unlockFile(f)
	with open(p, 'rb') as r:
		assert TSVZ._lockFile(r, TSVZ._READ)
		assert [_other(p, h) for h in ('ofdr0', 'ofdw1', 'ofdw', 'lockf')] == ['free', 'free', 'busy', 'busy']
		TSVZ._unlockFile(r)
	assert [_other(p, h) for h in ('ofdr0', 'ofdw1', 'ofdw', 'lockf', 'flock')] == ['free'] * 5


@linux_only
def test_a_refused_open_file_description_lock_falls_back_to_lockf_for_writers_and_no_lock_for_reads(tmp_path, monkeypatch):
	import fcntl
	monkeypatch.setattr(TSVZ, '_LOCK_KIND', 'ofd', raising=False)
	p = _tsv(tmp_path, 3)
	real = fcntl.fcntl

	def refusing(fd, cmd, arg=0):
		if cmd in TSVZ._OFD_COMMANDS:
			raise OSError(errno.EINVAL, 'Invalid argument')  # a kernel before 3.15, or a file system without them
		return real(fd, cmd, arg)
	monkeypatch.setattr(fcntl, 'fcntl', refusing)
	with open(p, 'r+b') as f:
		assert TSVZ._lockFile(f, TSVZ._APPEND)
		assert TSVZ._HELD[f.fileno()][0] == 'lockf'
		assert _other(p, 'lockf') == 'busy'
		TSVZ._unlockFile(f)
		assert _other(p, 'lockf') == 'free'
	with open(p, 'rb') as r:
		assert TSVZ._lockFile(r, TSVZ._READ) is False
		assert r.fileno() not in TSVZ._HELD and not _here()
	with TSVZ._readLock(p):
		assert not TSVZ._HELD
	assert TSVZ.readTabularFile(p, header='id\tv')['k0002'] == ['k0002', 'value-k0002']


def test_the_registry_holds_a_descriptor_only_while_it_is_locked(tmp_path, monkeypatch, kind):
	import fcntl
	p = _tsv(tmp_path, 3)
	with open(p, 'r+b') as f:
		for mode in (TSVZ._APPEND, TSVZ._REWRITE):
			assert TSVZ._lockFile(f, mode)
			assert TSVZ._HELD[f.fileno()][0] == kind and _here()
			TSVZ._unlockFile(f)
			assert f.fileno() not in TSVZ._HELD and not _here()

		def failing(*args, **kwargs):
			raise OSError(errno.ENOLCK, 'No locks available')  # NFS without a lock daemon
		monkeypatch.setattr(fcntl, 'fcntl' if kind == 'ofd' else 'flock', failing)
		with pytest.raises(OSError):
			TSVZ._lockFile(f, TSVZ._REWRITE)
		assert not TSVZ._HELD and not _here()
		assert TSVZ._lockFile(f, TSVZ._READ) is False  # a read never fails because the lock did
		assert not TSVZ._HELD and not _here()


@pytest.mark.parametrize('name', ['ENOTSUP', 'EOPNOTSUPP', 'EINVAL'])
def test_flock_kind_refused_falls_back_to_lockf_for_writers_and_no_lock_for_reads(tmp_path, monkeypatch, name):
	import fcntl
	monkeypatch.setattr(TSVZ, '_LOCK_KIND', 'flock', raising=False)
	p = _tsv(tmp_path, 3)

	def refusing(*args, **kwargs):
		raise OSError(getattr(errno, name), name)
	monkeypatch.setattr(fcntl, 'flock', refusing)
	with open(p, 'r+b') as f:
		assert TSVZ._lockFile(f, TSVZ._APPEND)
		assert TSVZ._HELD[f.fileno()][0] == 'lockf'
		assert _other(p, 'lockf') == 'busy'
		TSVZ._unlockFile(f)
		assert _other(p, 'lockf') == 'free'
	with open(p, 'rb') as r:
		assert TSVZ._lockFile(r, TSVZ._READ) is False
		assert not TSVZ._HELD
	assert len(TSVZ.readTabularFile(p, header='id\tv')) == 3


def test_flock_kind_reads_share_and_writes_exclude(tmp_path, monkeypatch):
	monkeypatch.setattr(TSVZ, '_LOCK_KIND', 'flock', raising=False)
	p = _tsv(tmp_path, 3)
	with open(p, 'rb') as r:
		assert TSVZ._lockFile(r, TSVZ._READ)
		assert [_other(p, 'flock-sh'), _other(p, 'flock')] == ['free', 'busy']
		TSVZ._unlockFile(r)
	f, g = open(p, 'r+b'), open(p, 'r+b')
	try:
		assert TSVZ._lockFile(f, TSVZ._APPEND)
		assert _other(p, 'flock-sh') == 'busy'
		second = threading.Thread(target=TSVZ._lockFile, args=(g,), kwargs={'mode': TSVZ._APPEND})
		second.start()
		assert not _done_within(second, 0.5), 'two threads of one process held the write lock at once'
		TSVZ._unlockFile(f)
		assert _done_within(second, 5)
		assert _other(p, 'flock') == 'busy'
		TSVZ._unlockFile(g)
		assert _other(p, 'flock') == 'free'
	finally:
		f.close()
		g.close()


SAME_THREAD = r'''
p = ARGS[0]
f = open(p, 'r+b')
T._lockFile(f, T._REWRITE)
start = time.time()
with T._readLock(p):
	inside = len(T._HELD)
say('own %s %d' % ('fast' if time.time() - start < 2 else 'slow', inside))
done = []
def other():
	with T._readLock(p):
		done.append(time.time())
t = threading.Thread(target=other)
t.start()
t.join(0.5)
say('other ' + ('waits' if t.is_alive() else 'went through'))
T._unlockFile(f)
t.join(5)
say('other ' + ('done' if done else 'stuck'))
'''


def test_a_thread_holding_a_write_lock_reads_its_file_without_waiting(tmp_path, kind):
	p = _tsv(tmp_path, 3)
	child = _child(SAME_THREAD, p, kind=kind)
	try:
		out, err = child.communicate(timeout=10)
	finally:
		_kill(child)
	assert out.decode().split('\n')[:3] == ['own fast 1', 'other waits', 'other done'], err.decode()


def test_a_read_inside_a_read_in_one_thread_takes_no_second_lock(tmp_path, kind):
	p = _tsv(tmp_path, 3)
	with TSVZ._readLock(p):
		assert len(TSVZ._HELD) == 1 and len(_here()) == 1
		with TSVZ._readLock(p):
			assert len(TSVZ._HELD) == 1
		assert len(TSVZ._HELD) == 1 and len(_here()) == 1
	assert not TSVZ._HELD and not _here()


def test_a_keyboardinterrupt_inside_a_read_lock_leaves_nothing_registered(tmp_path, kind):
	p = _tsv(tmp_path, 3)
	with pytest.raises(KeyboardInterrupt):
		with TSVZ._readLock(p):
			assert TSVZ._HELD
			raise KeyboardInterrupt
	assert not TSVZ._HELD and not _here()
	assert _other(p, 'ofdw' if kind == 'ofd' else 'flock') == 'free'


@needs_fork_hook
@pytest.mark.filterwarnings('ignore::DeprecationWarning')  # fork() in a process with threads: here on purpose
@pytest.mark.parametrize('mode', ['READ', 'REWRITE'])
def test_a_forked_child_does_not_keep_a_lock_its_parent_held(tmp_path, mode, kind):
	p = _tsv(tmp_path, 3)
	held, release = threading.Event(), threading.Event()

	def holder():
		if mode == 'READ':
			with TSVZ._readLock(p):
				held.set()
				release.wait(20)
		else:
			with open(p, 'r+b') as f:
				TSVZ._lockFile(f)
				held.set()
				release.wait(20)
				TSVZ._unlockFile(f)
	t = threading.Thread(target=holder)
	t.start()
	assert held.wait(10)
	pid = os.fork()
	if pid == 0:
		try:
			time.sleep(3)
		finally:
			os._exit(0)
	try:
		release.set()
		t.join(10)
		assert _other(p, 'ofdw' if kind == 'ofd' else 'flock') == 'free', 'the forked child kept the lock'
		assert os.waitpid(pid, os.WNOHANG) == (0, 0)  # (the child was still running)
	finally:
		os.waitpid(pid, 0)
