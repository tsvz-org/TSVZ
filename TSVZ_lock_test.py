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
	"""Read the child's lines until one is ``word`` (a store's messages, such as "External changes detected", may
	come first)."""
	seen = []

	def read():
		for line in iter(child.stdout.readline, b''):
			seen.append(line.decode().strip())
			if seen[-1] == word:
				return
	reader = threading.Thread(target=read)
	reader.daemon = True
	reader.start()
	reader.join(timeout)
	if not seen or seen[-1] != word:
		child.kill()
		raise AssertionError('child said {!r}, not {!r}: {}'.format(seen, word, child.stderr.read().decode()[-2000:]))


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


def test_a_file_closed_without_an_unlock_does_not_stop_later_reads_from_locking(tmp_path, kind):
	# Closing a locked file lets go of its lock: this thread's later reads of the file lock again.
	p = _tsv(tmp_path, 3)
	f = open(p, 'r+b')
	TSVZ._lockFile(f)
	fd = f.fileno()
	f.close()  # (a get_file_obj() never released, a Ctrl-C between the lock and its try)
	try:
		assert not _here()
		with TSVZ._readLock(p) as r:
			assert r.fileno() in TSVZ._HELD
			assert _other(p, 'ofdw' if kind == 'ofd' else 'flock') == 'busy'
	finally:
		TSVZ._HELD.pop(fd, None)


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
	# The child shares the locked open file description. 5.2: its unlock of the inherited descriptor (a copy of the
	# holder's release, a finalizer) dropped the parent's lock while the parent still held it, and a close of it
	# without an unlock kept the lock after the parent let go. 5.3 points it at /dev/null in the child.
	p = _tsv(tmp_path, 3)
	held, release = threading.Event(), threading.Event()
	box = {}
	how = 'ofdw' if kind == 'ofd' else 'flock'

	def holder():
		if mode == 'READ':
			with TSVZ._readLock(p) as f:
				box['f'] = f
				held.set()
				release.wait(20)
		else:
			with open(p, 'r+b') as f:
				TSVZ._lockFile(f)
				box['f'] = f
				held.set()
				release.wait(20)
				TSVZ._unlockFile(f)
	t = threading.Thread(target=holder)
	t.start()
	assert held.wait(10)
	pid = os.fork()
	if pid == 0:
		try:
			TSVZ._unlockFile(box['f'])
			open(p + '.unlocked', 'w').close()
			time.sleep(3)
		finally:
			os._exit(0)
	try:
		end = time.time() + 10
		while not os.path.exists(p + '.unlocked') and time.time() < end:
			time.sleep(0.01)
		assert _other(p, how) == 'busy', "the forked child's unlock dropped the parent's lock"
		release.set()
		t.join(10)
		assert _other(p, how) == 'free', 'the forked child kept the lock'
		assert os.waitpid(pid, os.WNOHANG) == (0, 0)  # (the child was still running)
	finally:
		release.set()
		os.waitpid(pid, 0)


# ---------------------------------------------------------------------------
# Writers take APPEND or REWRITE; older locks
# ---------------------------------------------------------------------------
#: A child holding a raw shared lock, as a 5.3 reader does: ARGS[0] the file, ARGS[1] 'ofdr0' or 'flock-sh'.
READER = r'''
p, how = ARGS[0], ARGS[1]
f = open(p, 'rb')
if how == 'ofdr0':
	ofd(f, fcntl.F_RDLCK, 0, 1)
else:
	fcntl.flock(f, fcntl.LOCK_SH)
say('held')
released(p + '.release')
'''


def test_an_append_goes_through_while_another_process_reads_and_a_rewrite_waits(tmp_path, kind):
	p = _tsv(tmp_path, 20)
	t = TSVZ.TSVZed(p, header='id\tv', rewrite_on_load=False, verbose=False)
	reader = _child(READER, p, 'ofdr0' if kind == 'ofd' else 'flock-sh')
	try:
		_expect(reader, 'held')
		append = threading.Thread(target=TSVZ.appendTabularFile, args=(p, ['k9999', 'x']), kwargs={'header': 'id\tv'})
		append.start()
		if kind == 'ofd':
			assert _done_within(append, 2), 'an append waited for a reader'
		else:
			assert not _done_within(append, 0.5), 'flock: an append waits for readers'
		rewrite = threading.Thread(target=t.hardMapToFile)
		rewrite.start()
		assert not _done_within(rewrite, 0.5), 'a rewrite went on while another process read the file'
		_release(p)
		assert _done_within(rewrite, 5) and _done_within(append, 5)
	finally:
		_kill(reader)
		t.close()


def test_older_lockf_writers_and_tsvz_locks_exclude_each_other(tmp_path):
	# Design T6, and the macOS check of design 4.6: 3.39's whole-file lockf and every 5.3 mode exclude each other.
	p = _tsv(tmp_path, 20)
	t = TSVZ.TSVZed(p, header='id\tv', rewrite_on_load=False, verbose=False)
	holder = _hold(p, 'lockf')
	try:
		def read():
			with TSVZ._readLock(p):
				pass
		threads = [threading.Thread(target=TSVZ.appendTabularFile, args=(p, ['k9999', 'x']), kwargs={'header': 'id\tv'}),
				   threading.Thread(target=t.hardMapToFile), threading.Thread(target=read)]
		for thread in threads:
			thread.start()
		time.sleep(0.5)
		assert [thread.is_alive() for thread in threads] == [True, True, True], 'went on under a lockf writer'
		_release(p)
		assert all(_done_within(thread, 5) for thread in threads)
	finally:
		_kill(holder)
		t.close()
	with open(p, 'r+b') as f:
		for mode in (TSVZ._APPEND, TSVZ._REWRITE):
			assert TSVZ._lockFile(f, mode=mode)
			assert _other(p, 'lockf') == 'busy'
			TSVZ._unlockFile(f)
	assert _other(p, 'lockf') == 'free'


# ---------------------------------------------------------------------------
# Entry 1: a delete during another process's rewrite lands
# ---------------------------------------------------------------------------
#: A child that rewrites ARGS[0] under a lock (ARGS[1]: 'lockf', 3.39; 'ofdw', 4.9 to 5.2; 'native', 5.3's own):
#: it empties the file, writes 500 of its 1000 rows, says 'half', and writes the rest once released.
REWRITER = r'''
p, how = ARGS[0], ARGS[1]
rows = [('k%04d\tvalue-k%04d\n' % (i, i)).encode() for i in range(1000)]
f = open(p, 'r+b')
if how == 'lockf':
	fcntl.lockf(f, fcntl.LOCK_EX)
elif how == 'ofdw':
	ofd(f, fcntl.F_WRLCK, 0, 0)
else:
	T._lockFile(f)
f.truncate(0)
f.write(b'id\tv\n' + b''.join(rows[:500]))
f.flush()
say('half')
released(p + '.release')
f.write(b''.join(rows[500:]))
f.flush()
os.fsync(f.fileno())
if how == 'native':
	T._unlockFile(f)
f.close()
'''

#: A child whose TSVZed (5.3's own rewrite: write over, then cut) pauses half way through a whole rewrite, after
#: k0001 was deleted and set again, which moves it to the end of the file.
STORE_REWRITER = r'''
p = ARGS[0]
t = T.TSVZed(p, header='id\tv', rewrite_on_load=False, verbose=False)
real = T._writeLines
def paused(file, header, lines, chunk=1 << 20, codec=None):
	lines = list(lines)
	half = len(lines) // 2
	real(file, header, lines[:half], chunk, codec)
	file.flush()
	say('half')
	released(p + '.release')
	real(file, None, lines[half:], chunk, codec)
T._writeLines = paused
t.memoryOnly = True  # nothing queued: the rewrite writes the moved key
del t['k0001']
t['k0001'] = ['k0001', 'value-k0001']
t.memoryOnly = False
t.hardMapToFile()
t.close()
'''

WRITERS = ['lockf', 'ofdw', 'native']


def _skipUnexcluded(kind, writer):
	if writer == 'ofdw' and not LINUX:
		pytest.skip('open file description locks are Linux-only')
	if kind == 'flock' and LINUX and writer != 'native':
		pytest.skip('flock and POSIX locks do not exclude each other on Linux (why Linux keeps OFD locks)')


def _deleteDuring(tmp_path, kind, code, args, victim):
	p = _tsv(tmp_path)
	t = TSVZ.TSVZed(p, header='id\tv', rewrite_on_load=False, verbose=False)
	assert len(t) == 1000
	child = _child(code, p, *args, kind=kind)
	try:
		_expect(child, 'half')
		time.sleep(1)  # the store's background read meets the rewrite now
		assert victim in t, 'key missing during the rewrite'
		del t[victim]
		_release(p)
		assert child.wait(20) == 0, child.stderr.read().decode()
		t.commitAppendToFile()
		assert victim not in t
		assert victim not in TSVZ.readTabularFile(p, header='id\tv', strict=False), 'delete lost'
	finally:
		_kill(child)
		t.close()


@pytest.mark.parametrize('writer', WRITERS)
def test_a_delete_during_another_process_locked_rewrite_lands(tmp_path, kind, writer):
	_skipUnexcluded(kind, writer)
	_deleteDuring(tmp_path, kind, REWRITER, [writer], 'k0900')


def test_a_delete_during_another_tsvzed_whole_rewrite_lands(tmp_path, kind):
	_deleteDuring(tmp_path, kind, STORE_REWRITER, [], 'k0001')


# ---------------------------------------------------------------------------
# One-shot reads
# ---------------------------------------------------------------------------
CLI = r'''
sys.exit(T._cliMain(ARGS))
'''


@pytest.mark.parametrize('writer', WRITERS)
def test_one_shot_reads_wait_out_another_process_locked_rewrite(tmp_path, kind, writer):
	_skipUnexcluded(kind, writer)
	p = _tsv(tmp_path)
	child = _child(REWRITER, p, writer, kind=kind)
	cli = None
	results = {}
	try:
		_expect(child, 'half')
		read = threading.Thread(target=lambda: results.update(read=TSVZ.readTabularFile(p, header='id\tv', strict=False)))
		store = threading.Thread(target=lambda: results.update(
			store=TSVZ.TSVZed(p, header='id\tv', rewrite_on_load=False, verbose=False)))
		read.start()
		store.start()
		cli = _child(CLI, 'get', p, 'k0900', kind=kind)
		time.sleep(1)
		assert read.is_alive() and store.is_alive() and cli.poll() is None, 'a read went on during the rewrite'
		_release(p)
		assert _done_within(read, 10) and _done_within(store, 10)
		out, err = cli.communicate(timeout=20)
		assert child.wait(20) == 0
		assert len(results['read']) == 1000 and 'k0900' in results['read']
		assert len(results['store']) == 1000
		assert b'value-k0900' in out, err
	finally:
		_kill(child, cli)
		if 'store' in results:
			results['store'].close()


GET_FILE_OBJ = r'''
p = ARGS[0]
t = T.TSVZed(p, header='id\tv', rewrite_on_load=False, verbose=False)
f = t.get_file_obj('r+b')
say('read %d' % len(T.readTabularFile(p, header='id\tv')))
t.release_file_obj(f)
t.close()
'''


def test_a_get_file_obj_holder_reads_its_file_with_readtabularfile(tmp_path, kind):
	# Design T4: the same-thread rule. Without it the read would wait for the thread's own lock, for ever.
	p = _tsv(tmp_path, 20)
	child = _child(GET_FILE_OBJ, p, kind=kind)
	try:
		out, err = child.communicate(timeout=10)
	finally:
		_kill(child)
	assert out.decode().strip() == 'read 20', err.decode()


def test_a_reader_waiting_on_a_writer_is_released_when_it_unlocks(tmp_path, kind):
	# H1
	p = _tsv(tmp_path, 20)
	holder = _hold(p, 'native', kind=kind)
	try:
		results = []
		read = threading.Thread(target=lambda: results.append(TSVZ.readTabularFile(p, header='id\tv')))
		read.start()
		assert not _done_within(read, 0.5), 'a read went on while another process held the write lock'
		_release(p)
		assert _done_within(read, 5)
		assert len(results[0]) == 20
	finally:
		_kill(holder)


READ_LIBRARY = r'''
say('reading')
T.readTabularFile(ARGS[0], header='id\tv')
say('done')
'''


@pytest.mark.parametrize('how', ['library', 'cli'])
def test_ctrl_c_ends_a_read_waiting_on_another_process(tmp_path, kind, how):
	# H2
	p = _tsv(tmp_path, 20)
	holder = _hold(p, 'native', kind=kind)
	reader = None
	try:
		if how == 'library':
			reader = _child(READ_LIBRARY, p, kind=kind)
		else:
			reader = _child("say('reading')\n" + CLI, 'read', p, kind=kind)
		_expect(reader, 'reading')
		time.sleep(0.5)
		assert reader.poll() is None, 'the read did not wait'
		reader.send_signal(signal.SIGINT)
		out, err = reader.communicate(timeout=5)
		assert reader.returncode != 0
		if how == 'library':
			assert b'KeyboardInterrupt' in err, err
		assert _other(p, 'ofdw' if kind == 'ofd' else 'flock') == 'busy', 'the holder lost its lock'
	finally:
		_release(p)
		_kill(reader)
		if holder.poll() is None:
			holder.wait(10)
		_kill(holder)
	assert _other(p, 'ofdw' if kind == 'ofd' else 'flock') == 'free', 'the interrupted read left a lock behind'


# ---------------------------------------------------------------------------
# The tsvz serve handler
# ---------------------------------------------------------------------------
def test_a_handler_pop_during_another_process_locked_rewrite_returns_and_deletes_the_row(tmp_path, kind):
	p = _tsv(tmp_path)
	handle = TSVZ._ServeHandle(p)
	handle.lock()
	handle.start(TSVZ._ServeStore(p, create=True))
	serving = threading.Thread(target=handle.serve)
	serving.start()
	child = None
	try:
		with TSVZ.TSVZClient(p) as c:
			assert c.get('k0900') == ['k0900', 'value-k0900']
			child = _child(REWRITER, p, 'native', kind=kind)
			_expect(child, 'half')
			results = []
			pop = threading.Thread(target=lambda: results.append(c.pop('k0900', None)))
			pop.start()
			assert not _done_within(pop, 1), 'the handler answered during the rewrite'
			_release(p)
			assert _done_within(pop, 10)
			assert child.wait(20) == 0
		assert results == [['k0900', 'value-k0900']]
	finally:
		_kill(child)
		handle.stop()
		serving.join(10)
		handle.close()
	assert 'k0900' not in TSVZ.readTabularFile(p, header='id\tv', strict=False)


# ---------------------------------------------------------------------------
# Hangs and starvation
# ---------------------------------------------------------------------------
STORE = r'''
import signal
p, queued = ARGS[0], ARGS[1] == 'queued'
if queued:
	t = T.TSVZed(p, header='id\tv', rewrite_on_load=False, verbose=False)
	say('open')
	released(p + '.go')
	t['new'] = ['new', 'N']
	say('closing')
	t.close()
else:
	say('opening')
	t = T.TSVZed(p, header='id\tv', rewrite_on_load=False, verbose=False)
say('done')
'''


def test_ctrl_c_in_a_store_waiting_on_another_process_lets_the_program_exit(tmp_path, kind):
	# H3: one Ctrl-C ends a program whose store waits in its load for another process's rewrite.
	p = _tsv(tmp_path, 20)
	holder = _hold(p, 'native', kind=kind)
	store = None
	try:
		store = _child(STORE, p, 'load', kind=kind)
		_expect(store, 'opening')
		time.sleep(0.5)
		assert store.poll() is None, 'the load did not wait'
		store.send_signal(signal.SIGINT)
		try:
			out, err = store.communicate(timeout=5)
		except subprocess.TimeoutExpired:
			raise AssertionError('one Ctrl-C did not end the program')
		assert store.returncode != 0 and b'done' not in out
		assert _other(p, 'ofdw' if kind == 'ofd' else 'flock') == 'busy'
	finally:
		_release(p)
		_kill(store, holder)


def test_ctrl_c_at_close_of_a_store_with_a_queued_row_ends_after_three(tmp_path, kind):
	# The store has a row to write and waits for another process's lock. A Ctrl-C stops close()'s wait; at exit the
	# write in progress is waited for again, a Ctrl-C there says it is being finished, and the next abandons it: three
	# in all (as in 5.2, where the wait was for another writer only; documented here, not a new rule). 5.2 on Python
	# 3.9 to 3.12 ended at one: a Ctrl-C in Thread.join() marks the thread as stopped while it runs, so the exit did not
	# wait for it and dropped the row.
	p = _tsv(tmp_path, 20)
	store = _child(STORE, p, 'queued', kind=kind)
	holder = None
	try:
		_expect(store, 'open')
		holder = _hold(p, 'native', kind=kind)
		open(p + '.go', 'w').close()
		_expect(store, 'closing')
		time.sleep(0.5)
		signals = 0
		for _ in range(3):
			if store.poll() is not None:
				break
			store.send_signal(signal.SIGINT)
			signals += 1
			try:
				store.wait(3)
			except subprocess.TimeoutExpired:
				pass
		assert store.poll() is not None, 'three Ctrl-Cs did not end the program'
		err = store.stderr.read() + store.stdout.read()
		assert signals == 3 and b'being finished' in err, (signals, err)
	finally:
		_release(p)
		_kill(store, holder)


EXIT_QUEUED = r'''
p = ARGS[0]
t = T.TSVZed(p, header='id\tv', rewrite_on_load=False, verbose=False)
say('open')
released(p + '.go')
t['new'] = ['new', 'N']
say('exiting')
'''


def test_ctrl_c_at_exit_of_a_store_with_a_queued_row_keeps_waiting_and_writes_it(tmp_path, kind):
	# At exit a first Ctrl-C says the write is being finished and keeps waiting: the row is written once the other
	# process lets go. 5.2 on Python 3.9 to 3.12 ended there and dropped the row (Thread.join()'s Ctrl-C quirk).
	p = _tsv(tmp_path, 20)
	store = _child(EXIT_QUEUED, p, kind=kind)
	holder = None
	try:
		_expect(store, 'open')
		holder = _hold(p, 'native', kind=kind)
		open(p + '.go', 'w').close()
		_expect(store, 'exiting')
		time.sleep(0.7)
		store.send_signal(signal.SIGINT)
		time.sleep(1.5)
		assert store.poll() is None, 'the exit did not wait for the queued row'
		_release(p)
		out, err = store.communicate(timeout=20)
		assert b'being finished' in err, err
	finally:
		_release(p)
		_kill(store, holder)
	assert TSVZ.readTabularFile(p, header='id\tv').get('new') == ['new', 'N']


EXIT_REWRITE = r'''
p = ARGS[0]
t = T.TSVZed(p, header='id\tv', rewrite_on_load=False, verbose=False)
say('open')
released(p + '.go')
say('rewriting')
t.hardMapToFile()
'''


def test_ctrl_c_while_an_explicit_rewrite_waits_for_another_process_finishes_it_at_exit(tmp_path, kind):
	# The exit used to let go of a store whose hardMapToFile() waited for another process's lock (nothing was queued
	# or dirty yet): when the lock came free during the rest of the exit, the thread began the rewrite and was cut as
	# the interpreter ended, leaving a compressed file unreadable. Now the exit waits for the job.
	p = str(tmp_path / 'f.tsv.gz')
	with TSVZ.openFileAsCompressed(p, 'wb') as f:
		f.write(b'id\tv\n' + b''.join(('k%04d\tvalue-k%04d\n' % (i, i)).encode() for i in range(20000)))
	store = _child(EXIT_REWRITE, p, kind=kind)
	holder = None
	try:
		_expect(store, 'open')
		holder = _hold(p, 'native', kind=kind)
		open(p + '.go', 'w').close()
		_expect(store, 'rewriting')
		time.sleep(0.7)
		store.send_signal(signal.SIGINT)
		time.sleep(1)
		assert store.poll() is None, 'the exit let go of a store whose rewrite was waiting'
		_release(p)
		out, err = store.communicate(timeout=30)
	finally:
		_release(p)
		_kill(store, holder)
	rows = TSVZ.readTabularFile(p, header='id\tv', strict=False)
	assert len(rows) == 20000, err


def test_a_store_the_exit_let_go_of_starts_no_write(tmp_path):
	p = _tsv(tmp_path, 20)
	before = open(p, 'rb').read()
	t = TSVZ.TSVZed(p, header='id\tv', rewrite_on_load=False, verbose=False)
	try:
		assert t._atExit() is None and t._letGo  # nothing to write: let go of
		t.dirty = True
		t.appendQueue.append(['late', 'L'])  # a row queued after the exit let go (another exit handler's)
		assert t._writeFile(whole=True) is False
		t._commitOnce()
		assert open(p, 'rb').read() == before
	finally:
		t.appendQueue.clear()
		t.dirty = False
		t._finish()


def test_a_handler_takes_its_check_bytes_under_the_read_lock(tmp_path, monkeypatch, kind):
	# The bytes a handler follows appends from must be of the file its view was read from: taken under the READ lock.
	p = _tsv(tmp_path, 20)
	seen = []
	real = TSVZ._ServeStore._readCheck

	def checking(self):
		seen.append(len(TSVZ._HELD))
		return real(self)
	monkeypatch.setattr(TSVZ._ServeStore, '_readCheck', checking)
	store = TSVZ._ServeStore(p, create=True)
	store.reload(None)
	assert seen and seen[-1] == 1, seen


def test_a_held_read_lock_makes_an_older_lockf_writer_wait(tmp_path):
	# Native kind: on macOS this is flock against 3.39 to 5.2's lockf (design 4.6).
	p = _tsv(tmp_path, 3)
	with TSVZ._readLock(p) as f:
		assert f is not None and TSVZ._HELD
		assert _other(p, 'lockf') == 'busy'
	assert _other(p, 'lockf') == 'free'


SETTER = r'''
p, seconds = ARGS[0], float(ARGS[1])
t = T.TSVZed(p, header='id\tv', rewrite_on_load=False, verbose=False, append_check_delay=0.001)
say('started')
end = time.time() + seconds
i = 0
while time.time() < end:
	t['s%d-%d' % (os.getpid(), i % 50)] = ['x', str(i)]
	i += 1
	time.sleep(0.001)
t.close()
'''


def test_a_rewrite_is_not_starved_by_two_appending_stores(tmp_path, kind):
	# Design T2: with B and C appending and reading all the time, A's rewrite still gets the file.
	p = str(tmp_path / 'big.tsv')
	with open(p, 'wb') as f:
		f.write(b'id\tv\n')
		row = b'\t' + b'v' * 200 + b'\n'
		f.write(b''.join(b'k%07d' % i + row for i in range(100000)))  # ~20 MB
	a = TSVZ.TSVZed(p, header='id\tv', rewrite_on_load=False, verbose=False)
	children = []
	try:
		for _ in range(2):
			children.append(_child(SETTER, p, 15, kind=kind))
		for child in children:
			_expect(child, 'started', timeout=60)
		time.sleep(1)
		start = time.time()
		a.hardMapToFile()
		took = time.time() - start
		assert took < 10, 'the rewrite was starved: {:.1f} s'.format(took)
	finally:
		_kill(*children)
		a.close()


# ---------------------------------------------------------------------------
# Entry 2: flock (macOS and other systems; forced on Linux)
# ---------------------------------------------------------------------------
def test_flock_write_lock_survives_other_threads_opening_reading_and_closing_the_file(tmp_path, monkeypatch):
	# Design T5a: a lockf lock was dropped by any close of the file in the process (p1 a).
	monkeypatch.setattr(TSVZ, '_LOCK_KIND', 'flock', raising=False)
	p = _tsv(tmp_path, 20)
	inside, go = threading.Event(), threading.Event()
	real = TSVZ._appendOpen

	def paused(*args, **kwargs):
		inside.set()
		go.wait(20)
		return real(*args, **kwargs)
	monkeypatch.setattr(TSVZ, '_appendOpen', paused)
	writer = threading.Thread(target=TSVZ.appendTabularFile, args=(p, ['k9999', 'x']), kwargs={'header': 'id\tv'})
	writer.start()
	results = []
	try:
		assert inside.wait(10)
		assert _other(p, 'flock') == 'busy'
		open(p, 'rb').close()
		TSVZ._fileHoldsNothing(p)
		assert _other(p, 'flock') == 'busy', 'another thread closing the file dropped the write lock'
		reader = threading.Thread(target=lambda: results.append(TSVZ.readTabularFile(p, header='id\tv')))
		reader.start()
		assert not _done_within(reader, 0.5), 'a read in another thread went on during the write'
		assert _other(p, 'flock') == 'busy'
	finally:
		go.set()
		writer.join(10)
	assert _done_within(reader, 5)
	assert 'k9999' in results[0]


def test_a_store_commit_and_an_append_in_another_thread_write_one_header(tmp_path, monkeypatch):
	# Design T5b (probe p6): under lockf both found the emptied file empty and both wrote the header.
	monkeypatch.setattr(TSVZ, '_LOCK_KIND', 'flock', raising=False)
	p = str(tmp_path / 'f.tsv')
	t = TSVZ.TSVZed(p, header='id\tv', verbose=False, rewrite_on_load=False)
	real = TSVZ._holdsNothing

	def slow(*args, **kwargs):
		found = real(*args, **kwargs)
		if found and threading.current_thread() is t.appendThread:
			time.sleep(0.5)  # only the store's writer thread pauses, inside its lock
		return found
	monkeypatch.setattr(TSVZ, '_holdsNothing', slow)
	try:
		open(p, 'wb').close()  # emptied (a rewrite cut short, another writer): the next append writes the header first
		t.externalFileUpdateTime = TSVZ.getFileUpdateTimeNs(p)
		t._known = TSVZ._KNOWN_NOTHING
		t.memoryOnly = True
		t['a'] = ['a', '1']
		t.memoryOnly = False
		t.appendQueue.append(['a', '1'])
		a = threading.Thread(target=t.commitAppendToFile)
		b = threading.Thread(target=lambda: TSVZ.appendTabularFile(p, ['b', '2'], header='id\tv'))
		a.start()
		time.sleep(0.1)
		b.start()
		a.join(10)
		b.join(10)
	finally:
		t.close()
	data = open(p, 'rb').read()
	assert data.count(b'id\tv\n') == 1, data
