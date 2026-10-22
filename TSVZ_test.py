#! /usr/bin/env python3
"""Tests for TSVZ 4.3 (TSVZ.py).

Plain ``test_*`` functions (pytest-style, no class boilerplate), matching the
repo convention. Run them with ``python3 -m pytest TSVZ_test.py -q``, or with
``python3 TSVZ_test.py``, which hands its arguments to pytest. The
differential tests compare the legacy dialect against the frozen 3.39
reference ``TSVZ_old.py`` (``-k 339`` selects them).
"""
import os
import sys
import tempfile
import time
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import TSVZ


def _tmp(suffix='.tsv'):
	fd, path = tempfile.mkstemp(suffix=suffix)
	os.close(fd)
	os.remove(path)  # we only want a unique name; let TSVZ create it
	return path


# --------------------------------------------------------------------------
# Issue 1 + 3: deletion must survive a reload (tombstone correctness)
# --------------------------------------------------------------------------
def test_tsvzed_delete_persists_after_reload():
	f = _tmp()
	try:
		t = TSVZ.TSVZed(f, header='id\tname\tval', rewrite_on_load=False)
		t['a'] = ['a', 'alice', '1']
		t['b'] = ['b', 'bob', '2']
		time.sleep(0.05)
		del t['a']
		t.close()  # flushes the append queue

		t2 = TSVZ.TSVZed(f, header='id\tname\tval', rewrite_on_load=False)
		assert 'a' not in t2, f"deleted key resurrected as {t2.get('a')!r}"
		assert 'b' in t2
		t2.close()
	finally:
		os.path.exists(f) and os.remove(f)


# --------------------------------------------------------------------------
# Issue 2: `tsv[key] = key` (a lone key with no values) must delete, not re-add
# --------------------------------------------------------------------------
def test_tsvzed_setitem_lone_key_deletes():
	f = _tmp()
	try:
		t = TSVZ.TSVZed(f, header='id\tname\tval', rewrite_on_load=False)
		t['a'] = ['a', 'alice', '1']
		time.sleep(0.05)
		t['a'] = 'a'  # lone key -> delete
		assert 'a' not in t, f"expected delete, got {t.get('a')!r}"
		t.close()
	finally:
		os.path.exists(f) and os.remove(f)


def test_tsvzedlite_setitem_lone_key_deletes():
	f = _tmp()
	try:
		with open(f, 'w') as fh:
			fh.write('id\tname\tval\n')
		lite = TSVZ.TSVZedLite(f, header='id\tname\tval', strict=False)
		lite['a'] = ['a', 'alice', '1']
		lite['a'] = 'a'  # lone key -> delete
		assert 'a' not in lite, f"expected delete, got index {lite.indexes.get('a')!r}"
		lite.close()
	finally:
		os.path.exists(f) and os.remove(f)


# --------------------------------------------------------------------------
# Issue 1 (Lite variant): del lite[key] must persist across a reload
# --------------------------------------------------------------------------
def test_tsvzedlite_delete_persists_after_reload():
	f = _tmp()
	try:
		with open(f, 'w') as fh:
			fh.write('id\tname\tval\n')
		lite = TSVZ.TSVZedLite(f, header='id\tname\tval', strict=False)
		lite['a'] = ['a', 'alice', '1']
		lite['b'] = ['b', 'bob', '2']
		del lite['a']
		assert 'a' not in lite
		lite.close()

		lite2 = TSVZ.TSVZedLite(f, header='id\tname\tval', strict=False)
		assert 'a' not in lite2, f"deleted key resurrected as {lite2.get('a')!r}"
		assert 'b' in lite2
		lite2.close()
	finally:
		os.path.exists(f) and os.remove(f)


# --------------------------------------------------------------------------
# Issue 3: deleting must not raise when the column count is unknown
# --------------------------------------------------------------------------
def test_delete_no_keyerror_when_column_count_unknown():
	f = _tmp()
	try:
		t = TSVZ.TSVZed(f, header='', rewrite_on_load=False)
		t['a'] = ['a', 'b']
		time.sleep(0.05)
		t.correctColumnNum = 0  # simulate undetermined column count
		del t['a']  # must not raise KeyError
		assert 'a' not in t
		t.close()
	finally:
		os.path.exists(f) and os.remove(f)


# --------------------------------------------------------------------------
# Issue 4: get_delimiter must be pure (no module-global leakage)
# --------------------------------------------------------------------------
def test_get_delimiter_is_pure():
	# Resolving a csv name must not change what a later bare call returns.
	assert TSVZ.get_delimiter(..., 'a.csv') == ','
	assert TSVZ.get_delimiter(...) == '\t', "bare get_delimiter() leaked the csv delimiter"
	assert TSVZ.get_delimiter(..., 'b.psv') == '|'
	assert TSVZ.get_delimiter(...) == '\t'
	assert TSVZ.DEFAULT_DELIMITER == '\t'


def test_get_delimiter_compressed_extension():
	assert TSVZ.get_delimiter(..., 'data.csv.gz') == ','
	assert TSVZ.get_delimiter(..., 'data.psv.zst') == '|'
	assert TSVZ.get_delimiter(..., 'data.tsv.xz') == '\t'
	assert TSVZ.get_delimiter(..., 'DATA.CSV') == ','  # case-insensitive


# --------------------------------------------------------------------------
# Issue 9: backward last-line reader must report the correct byte offset
# --------------------------------------------------------------------------
def test_read_last_valid_line_offset_across_chunks():
	f = _tmp()
	try:
		header = 'id\tname\tval\n'
		dataline = 'k1\talice\t100\n'
		with open(f, 'wb') as fh:
			fh.write(header.encode())
			fh.write(dataline.encode())
			fh.write(b'\n' * 3000)  # force multiple backward chunks
		true_offset = len(header.encode())
		got = TSVZ.read_last_valid_line(f, {}, correctColumnNum=3, delimiter='\t', storeOffset=True)
		assert got == true_offset, f"offset {got} != {true_offset}"
		with open(f, 'rb') as fh:
			fh.seek(got)
			assert fh.readline() == dataline.encode()
		# non-offset path still returns the right line
		line = TSVZ.read_last_valid_line(f, {}, correctColumnNum=3, delimiter='\t')
		assert line == ['k1', 'alice', '100']
	finally:
		os.path.exists(f) and os.remove(f)


# --------------------------------------------------------------------------
# Issue 6: rewrite must not emit '#' comment keys but must keep #_defaults_#
# --------------------------------------------------------------------------
def test_hardmap_skips_comments_keeps_defaults():
	f = _tmp()
	try:
		t = TSVZ.TSVZed(f, header='id\tname\tval', rewrite_on_load=False, defaults='#_defaults_#\t\tNA')
		t['a'] = ['a', 'alice', '1']
		t['#note'] = ['#note', 'should', 'stay-in-ram']
		t.hardMapToFile()
		t.close()
		with open(f) as fh:
			raw = fh.read()
		assert '#note' not in raw, "comment key was written to file"
		assert '#_defaults_#' in raw, "defaults line missing after rewrite"
	finally:
		os.path.exists(f) and os.remove(f)


def test_mapto_skips_comments_keeps_defaults():
	f = _tmp()
	try:
		t = TSVZ.TSVZed(f, header='id\tname\tval', rewrite_on_load=False, defaults='#_defaults_#\t\tNA')
		t['a'] = ['a', 'alice', '1']
		t['b'] = ['b', 'bob', '2']
		t['#note'] = ['#note', 'x', 'y']
		t.dirty = True
		t.mapToFile()
		t.close()
		with open(f) as fh:
			raw = fh.read()
		assert '#note' not in raw
		assert '#_defaults_#' in raw
		# data still readable + intact after the in-place rewrite
		t2 = TSVZ.TSVZed(f, header='id\tname\tval', rewrite_on_load=False)
		assert t2['a'] == ['a', 'alice', '1']
		assert t2['b'] == ['b', 'bob', '2']
		t2.close()
	finally:
		os.path.exists(f) and os.remove(f)


# --------------------------------------------------------------------------
# Issue 7: appendLinesTabularFile must not mutate the caller's lists
# --------------------------------------------------------------------------
def test_append_does_not_mutate_input():
	f = _tmp()
	try:
		rows = [['a', 1, 2], ['b', 3, 4]]  # non-str values on purpose
		snapshot = [list(r) for r in rows]
		TSVZ.appendLinesTabularFile(f, rows, header='id\tx\ty', createIfNotExist=True)
		assert rows == snapshot, f"caller list mutated: {rows!r}"
	finally:
		os.path.exists(f) and os.remove(f)


# --------------------------------------------------------------------------
# Issue 8 / format spec: sanitize <-> unsanitize round-trips per definition
# --------------------------------------------------------------------------
def test_sanitize_roundtrip():
	d = '\t'
	# tab char and newline survive a round trip
	for value in ['plain', 'has\ttab', 'has\nlf', 'ends with spaces   ',
				  'literal<sep>here', 'literal<LF>here']:
		san = TSVZ._sanitize(value, delimiter=d)
		back = TSVZ._unsanitize(san, delimiter=d)
		# trailing whitespace is intentionally stripped by the format
		assert back == value.rstrip(), f"{value!r} -> {san!r} -> {back!r}"
	# reserved tokens in file map back to the bare token form
	assert TSVZ._unsanitize('</sep/>', delimiter=d) == '<sep>'
	assert TSVZ._unsanitize('</LF/>', delimiter=d) == '<LF>'


# --------------------------------------------------------------------------
# Issue 12: TSVZedLite with external indexes must create a missing file
# --------------------------------------------------------------------------
def test_tsvzedlite_external_indexes_creates_file():
	f = _tmp()
	try:
		# pass an explicit (empty) index dict so load() is skipped
		lite = TSVZ.TSVZedLite(f, header='id\tname\tval', createIfNotExist=True, indexes={})
		assert os.path.isfile(f)
		lite['a'] = ['a', 'alice', '1']
		assert lite['a'] == ['a', 'alice', '1']
		lite.close()
	finally:
		os.path.exists(f) and os.remove(f)


# --------------------------------------------------------------------------
# Sanity: a normal write/read cycle still works (no regressions)
# --------------------------------------------------------------------------
def test_basic_roundtrip():
	f = _tmp()
	try:
		t = TSVZ.TSVZed(f, header='id\tname\tval')
		t['a'] = ['a', 'alice', '1']
		t['b'] = 'b\tbob\t2'
		t.close()
		t2 = TSVZ.TSVZed(f, header='id\tname\tval')
		assert t2['a'] == ['a', 'alice', '1']
		assert t2['b'] == ['b', 'bob', '2']
		t2.close()
	finally:
		os.path.exists(f) and os.remove(f)


def test_version():
	assert TSVZ.version == '4.9'
	assert TSVZ.__version__ == '4.9'
	assert not hasattr(TSVZ, 'WalStore')


# ==========================================================================
# Shared helpers for the 4.1 tests
# ==========================================================================
import contextlib
import gzip
import zlib
import io
import random
import errno
import stat
import subprocess
import threading
from collections import OrderedDict

import pytest

import TSVZ_old

HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY_HEADER = ['id', 'c1', 'c2']
LEGACY_VALUES = ['a', 'b c', 'x<y', '#hash', '<sep>', '</sep/>', '<LF>', 'multi\nline',
				 'trail  ', ' lead', 'é ü 中', 'tab\there', 'comma,here', 'pipe|here', '']


def _touch(path, content=b''):
	with open(path, 'wb') as f:
		f.write(content)


def _tsvz_warnings(capsys):
	return [line for line in capsys.readouterr().err.splitlines() if line.startswith('TSVZ warning:')]


def _content(path):
	with open(path, 'rb') as f:
		data = f.read()
	return gzip.decompress(data) if path.endswith('.gz') else data


def _twin_paths(base, suffix):
	old_dir, new_dir = base / 'old', base / 'new'
	old_dir.mkdir(parents=True, exist_ok=True)
	new_dir.mkdir(parents=True, exist_ok=True)
	return str(old_dir / ('data' + suffix)), str(new_dir / ('data' + suffix))


def _legacy_ops(seed, count=40, hash_keys=True):
	rng = random.Random(seed)
	keys = ['k%d' % i for i in range(6)] + (['#memo'] if hash_keys else [])
	ops = []
	for _ in range(count):
		roll = rng.random()
		key = rng.choice(keys)
		if roll < 0.55:
			cells = [rng.choice(LEGACY_VALUES) for _ in range(rng.randint(1, 3))]
			cells[0] = cells[0] or 'v'  # never all-empty: that case is fix L2
			ops.append(('set', key, [key] + cells))
		elif roll < 0.75:
			ops.append(('del', key))
		elif roll < 0.85:
			ops.append(('lone', key))
		elif roll < 0.92:
			ops.append(('defaults', ['#_defaults_#', '', rng.choice(['NA', 'x y'])]))
		else:
			ops.append(('reopen',))
	return ops


def _apply_op(store, op):
	if op[0] == 'set':
		store[op[1]] = list(op[2])
	elif op[0] == 'del':
		del store[op[1]]
	elif op[0] == 'lone':
		store[op[1]] = op[1]
	elif op[0] == 'defaults':
		store['#_defaults_#'] = list(op[1])


# ==========================================================================
# Legacy dialect: differential tests against the frozen 3.39 module
# ==========================================================================
LEGACY_SUFFIXES = ['.tsv', '.csv', '.nsv', '.psv', '.tsv.gz']
SUFFIX_DELIMITERS = {'.tsv': '\t', '.csv': ',', '.nsv': '\x00', '.psv': '|'}


# 3.39's background append worker races the test's own writes, so these lines
# appear or vanish with thread timing. They are dropped before comparison; every
# other stdout line is still compared.
_RACY_STDOUT_PREFIXES = ('External changes detected in ', 'Time anomalies detected in ',
						 'Warning: Overwriting external changes in ')


def _observe(fn, twin_dir):
	"""Run fn with stdout captured; return (outcome, stdout) with the twin dir masked."""
	buf = io.StringIO()
	try:
		with contextlib.redirect_stdout(buf):
			outcome = ('ok', fn())
	except Exception as e:
		outcome = ('raise', type(e).__name__)
	text = buf.getvalue().replace(twin_dir, '<dir>')
	return outcome, ''.join(line for line in text.splitlines(True)
							if not line.startswith(_RACY_STDOUT_PREFIXES))


def _tsvzed_snapshot(mod, path):
	t = mod.TSVZed(path, header=LEGACY_HEADER, rewrite_on_load=False)
	snap = dict(t)
	t.close()
	return snap


def _lite_snapshot(mod, path):
	lite = mod.TSVZedLite(path, header=LEGACY_HEADER, strict=False)
	snap = {k: lite[k] for k in list(lite.indexes)}
	lite.close()
	return snap


def _legacy_read_observations(mod, path, delimiter, twin_dir, compressed):
	obs = []
	for header in (LEGACY_HEADER, ''):
		for strict in (True, False):
			for verify in (True, False):
				obs.append(_observe(lambda: mod.readTabularFile(
					path, header=header, strict=strict, verifyHeader=verify), twin_dir))
	if not compressed:
		obs.append(_observe(lambda: mod.readTabularFile(
			path, header=LEGACY_HEADER, storeOffset=True), twin_dir))
	obs.append(_observe(lambda: mod.read_last_valid_line(path, {}, -1, delimiter=delimiter), twin_dir))
	obs.append(_observe(lambda: _tsvzed_snapshot(mod, path), twin_dir))
	if not compressed:
		obs.append(_observe(lambda: _lite_snapshot(mod, path), twin_dir))
	return obs


def _legacy_corpora(d):
	"""Hand-written legacy files (bytes) using delimiter d."""
	def line(*cells):
		return d.join(cells).encode('utf-8')
	main = [
		line('id', 'c1', 'c2'),
		line('a', 'b c', 'x'),
		line('dup', 'one', 'two'),
		line('dup'),                       # lone key: delete
		line('dup', 'three', 'four'),      # re-set after the delete
		line('k2', 'z', 'w'),
		line('k3', '', ''),                # key followed by empty cells
		line('wide', 'a', 'b', 'c', 'd'),  # wider than header
		line('narrow', 'a'),               # narrower than header
		b'',
		b'   ',
		line('crlf', 'x', 'y') + b'\r',
		b'# a comment',
		line('#note', 'q', 'r'),
		line('#_defaults_#', '', 'NA'),
		line('#_defaults_#'),              # empty defaults line
		line('ts', 'trail  ', 'x '),
		line('tok', '</sep/>', '<sep>'),
		line('lf', '<LF>', 'a'),
		line('late', 'v', 'w'),            # last line, no trailing newline
	]
	return {
		'main': b'\n'.join(main),
		'badheader': b'\n'.join([line('ID', 'X', 'Y'), line('a', 'b', 'c'), line('b', 'c', 'd')]) + b'\n',
		'utf8': b'\n'.join([line('id', 'c1', 'c2'), line('u', 'ok', 'fine'),
		                    b'bad' + d.encode('utf-8') + b'\xff' + d.encode('utf-8') + b'z']) + b'\n',
		'nul': b'\n'.join([line('id', 'c1', 'c2'), line('nul', 'v', 'w') + b'\x00', line('n2', 'p', 'q')]) + b'\n',
	}




def _drive_tsvzed(mod, path, ops):
	seen = []
	# monitor_external_changes=False: with it on, the append worker can mistake this
	# instance's own write for an external change and rewrite queued rows that the
	# same tick then appends again -- a timing race 3.39 has too, so the two modules'
	# bytes could differ by luck. Both modules get the same settings.
	kwargs = dict(header=LEGACY_HEADER, append_check_delay=0.001, monitor_external_changes=False)
	t = mod.TSVZed(path, **kwargs)
	for op in ops:
		if op[0] == 'reopen':
			t.close()
			seen.append(dict(t))
			t = mod.TSVZed(path, **kwargs)
			seen.append(dict(t))
		else:
			_apply_op(t, op)
	t.close()
	seen.append(dict(t))
	t = mod.TSVZed(path, **kwargs)
	seen.append(dict(t))
	t.close()
	return seen


def _drive_lite(mod, path, ops):
	seen = []
	kwargs = dict(header=LEGACY_HEADER, strict=False)
	lite = mod.TSVZedLite(path, **kwargs)
	for op in ops:
		if op[0] == 'reopen':
			lite.close()
			lite = mod.TSVZedLite(path, **kwargs)
			seen.append({key: lite[key] for key in list(lite.indexes)})
		else:
			_apply_op(lite, op)
	seen.append({key: lite[key] for key in list(lite.indexes)})
	lite.close()
	return seen


def _drive_stateless(mod, path, seed):
	rng = random.Random(seed)
	out = []
	delimiter = mod.get_delimiter(..., path)
	for _ in range(6):
		rows = []
		for _ in range(rng.randint(1, 4)):
			key = 'k%d' % rng.randint(0, 5)
			rows.append([key] + [rng.choice(LEGACY_VALUES) or 'v' for _ in range(rng.randint(0, 3))])
		mod.appendLinesTabularFile(path, rows, header=LEGACY_HEADER, createIfNotExist=True)
		out.append(dict(mod.readTabularFile(path, header=LEGACY_HEADER, strict=False)))
		out.append(dict(mod.readTabularFile(path, header=LEGACY_HEADER, strict=True)))
		out.append(mod.read_last_valid_line(path, {}, -1, delimiter=delimiter))
	out.append(dict(mod.scrubTabularFile(path, header=LEGACY_HEADER)))
	out.append(_content(path))
	mod.clearTabularFile(path, header=LEGACY_HEADER)
	out.append(_content(path))
	return out


def test_legacy_tsvzed_matches_339(tmp_path):
	for suffix in LEGACY_SUFFIXES:
		for seed in range(4):
			old_path, new_path = _twin_paths(tmp_path / ('%s-%d' % (suffix.replace('.', ''), seed)), suffix)
			ops = _legacy_ops(seed)
			assert _observe(lambda: _drive_tsvzed(TSVZ, new_path, ops), os.path.dirname(new_path)) == \
				_observe(lambda: _drive_tsvzed(TSVZ_old, old_path, ops), os.path.dirname(old_path)), (suffix, seed)
			assert _content(new_path) == _content(old_path), (suffix, seed)


def test_legacy_tsvzedlite_matches_339(tmp_path):
	for suffix in ['.tsv', '.csv', '.nsv', '.psv']:
		for seed in range(4):
			old_path, new_path = _twin_paths(tmp_path / ('%s-%d' % (suffix.replace('.', ''), seed)), suffix)
			ops = _legacy_ops(seed, hash_keys=False)
			assert _observe(lambda: _drive_lite(TSVZ, new_path, ops), os.path.dirname(new_path)) == \
				_observe(lambda: _drive_lite(TSVZ_old, old_path, ops), os.path.dirname(old_path)), (suffix, seed)
			assert _content(new_path) == _content(old_path), (suffix, seed)


def test_legacy_stateless_helpers_match_339(tmp_path):
	for suffix in LEGACY_SUFFIXES:
		for seed in range(3):
			old_path, new_path = _twin_paths(tmp_path / ('%s-%d' % (suffix.replace('.', ''), seed)), suffix)
			assert _observe(lambda: _drive_stateless(TSVZ, new_path, seed), os.path.dirname(new_path)) == \
				_observe(lambda: _drive_stateless(TSVZ_old, old_path, seed), os.path.dirname(old_path)), (suffix, seed)

def test_legacy_read_corpus_matches_339(tmp_path):
	for suffix, d in SUFFIX_DELIMITERS.items():
		for name, content in _legacy_corpora(d).items():
			old_path, new_path = _twin_paths(tmp_path / ('%s-%s' % (name, suffix.replace('.', ''))), suffix)
			_touch(old_path, content)
			_touch(new_path, content)
			old_obs = _legacy_read_observations(TSVZ_old, old_path, d, os.path.dirname(old_path), False)
			new_obs = _legacy_read_observations(TSVZ, new_path, d, os.path.dirname(new_path), False)
			assert new_obs == old_obs, (name, suffix)
			assert _content(new_path) == _content(old_path), (name, suffix)
	# gzip-compressed intact copy of the main corpus file (tab-delimited)
	main = _legacy_corpora('\t')['main']
	old_path, new_path = _twin_paths(tmp_path / 'main-gz', '.tsv.gz')
	_touch(old_path, gzip.compress(main))
	_touch(new_path, gzip.compress(main))
	old_obs = _legacy_read_observations(TSVZ_old, old_path, '\t', os.path.dirname(old_path), True)
	new_obs = _legacy_read_observations(TSVZ, new_path, '\t', os.path.dirname(new_path), True)
	assert new_obs == old_obs
	assert _content(new_path) == _content(old_path)


# ==========================================================================
# Infrastructure
# ==========================================================================
def test_parse_part_name():
	P = TSVZ._parsePartName
	assert P('/a/b.c/data.tsvz.1a2b.rotated.gz') == ('/a/b.c/data.tsvz', 'tsvz', 0x1a2b, True, 'gz')
	assert P('data.tsvz') == ('data.tsvz', 'tsvz', None, False, '')
	assert P('data.TSVZ.GZ') == ('data.TSVZ', 'tsvz', None, False, 'gz')
	assert P('data.txt.gz') == ('data.txt', '', None, False, 'gz')
	assert P('data.add') == ('data.add', '', None, False, '')
	assert P('/x.tsv/data') == ('/x.tsv/data', '', None, False, '')
	assert P('tsv') == ('tsv', '', None, False, '')
	assert P('data.tsv.1') == ('data.tsv', 'tsv', 1, False, '')
	assert P('data.tsvz.f.xz').ordinal == 15


def test_is_spec_path():
	assert TSVZ._isSpecPath('a.tsvz') and TSVZ._isSpecPath('a.csvz.10.zst') and TSVZ._isSpecPath('A.NSVZ')
	assert not TSVZ._isSpecPath('a.tsv') and not TSVZ._isSpecPath('a.csv.gz') and not TSVZ._isSpecPath('a.txt')


def test_get_delimiter_strict_extensions():
	assert TSVZ.get_delimiter(..., 'a.tsvz') == '\t'
	assert TSVZ.get_delimiter(..., 'a.csvz') == ','
	assert TSVZ.get_delimiter(..., 'a.nsvz.gz') == '\0'
	assert TSVZ.get_delimiter(..., 'a.psvz.1f') == '|'
	assert TSVZ.get_delimiter(..., 'a.csv') == ','


def test_reporter_summarises_once_per_kind(capsys):
	reporter = TSVZ._Reporter('f.tsvz')
	reporter.note('utf8', 'line 3', 'invalid UTF-8 replaced with U+FFFD')
	reporter.note('utf8', 'line 9', 'invalid UTF-8 replaced with U+FFFD')
	reporter.note('tail', None, "ignored uncommitted bytes after the last newline: b'x'")
	reporter.flush()
	reporter.flush()
	assert _tsvz_warnings(capsys) == [
		'TSVZ warning: f.tsvz: invalid UTF-8 replaced with U+FFFD (2 occurrences, first at line 3)',
		"TSVZ warning: f.tsvz: ignored uncommitted bytes after the last newline: b'x'",
	]


def test_reporter_uses_tee_logger(capsys):
	class Logger(object):
		def __init__(self):
			self.calls = []

		def teelog(self, message, level):
			self.calls.append((message, level))
	log = Logger()
	reporter = TSVZ._Reporter('f.tsvz', log)
	reporter.note('x', 'line 1', 'detail')
	reporter.flush()
	assert log.calls == [('TSVZ warning: f.tsvz: detail (line 1)', 'warning')]
	assert capsys.readouterr().err == ''


def test_warn_once_per_owner(capsys):
	class Owner(object):
		pass
	owner = Owner()
	TSVZ._warnOnce(owner, 'k', 'p.tsv', 'said once')
	TSVZ._warnOnce(owner, 'k', 'p.tsv', 'said once')
	assert _tsvz_warnings(capsys) == ['TSVZ warning: p.tsv: said once']


def test_locked_append_newline_policy(tmp_path, capsys):
	p = str(tmp_path / 'a.tsv')
	_touch(p, b'a\t1')
	reporter = TSVZ._Reporter(p)
	TSVZ._lockedAppend(p, b'b\t2\n', 'newline', reporter)
	reporter.flush()
	assert open(p, 'rb').read() == b'a\t1\nb\t2\n'
	assert _tsvz_warnings(capsys) == ['TSVZ warning: %s: last line had no trailing newline; added one before appending' % p]


def test_locked_append_truncate_policy(tmp_path, capsys):
	p = str(tmp_path / 'a.tsvz')
	_touch(p, b'a\t1\nbo')
	reporter = TSVZ._Reporter(p)
	TSVZ._lockedAppend(p, b'c\t3\n', 'truncate', reporter)
	reporter.flush()
	assert open(p, 'rb').read() == b'a\t1\nc\t3\n'
	assert _tsvz_warnings(capsys) == ["TSVZ warning: %s: removed uncommitted tail b'bo' before appending" % p]


def test_locked_append_whole_file_uncommitted_and_missing_file(tmp_path):
	p = str(tmp_path / 'a.tsvz')
	_touch(p, b'no newline at all')
	TSVZ._lockedAppend(p, b'c\t3\n', 'truncate', TSVZ._Reporter(p))
	assert open(p, 'rb').read() == b'c\t3\n'
	q = str(tmp_path / 'new.tsvz')
	TSVZ._lockedAppend(q, b'd\t4\n', 'truncate', TSVZ._Reporter(q))
	assert open(q, 'rb').read() == b'd\t4\n'


def test_last_newline_end_spans_blocks(tmp_path):
	p = str(tmp_path / 'big')
	_touch(p, b'x\n' + b'y' * 200000)
	with open(p, 'rb') as f:
		assert TSVZ._lastNewlineEnd(f, os.path.getsize(p)) == 2
	_touch(p, b'y' * 70000)
	with open(p, 'rb') as f:
		assert TSVZ._lastNewlineEnd(f, os.path.getsize(p)) == 0


def test_compress_bytes_roundtrip():
	import bz2
	import lzma
	assert gzip.decompress(TSVZ._compressBytes('gz', b'abc\n')) == b'abc\n'
	assert bz2.decompress(TSVZ._compressBytes('bz2', b'abc\n')) == b'abc\n'
	assert lzma.decompress(TSVZ._compressBytes('xz', b'abc\n')) == b'abc\n'


def test_path_lock_is_shared_per_real_path(tmp_path):
	p = str(tmp_path / 'a.tsvz')
	assert TSVZ._pathLock(p) is TSVZ._pathLock(os.path.join(str(tmp_path), '.', 'a.tsvz'))


# ==========================================================================
# Legacy fixes L1 and L6
# ==========================================================================
def test_l1_stateless_append_adds_missing_newline(tmp_path, capsys):
	p = str(tmp_path / 'a.tsv')
	_touch(p, b'id\tv\nk1\tx')
	TSVZ.appendTabularFile(p, ['k2', 'y'], header='id\tv')
	assert open(p, 'rb').read() == b'id\tv\nk1\tx\nk2\ty\n'
	assert dict(TSVZ.readTabularFile(p, header='id\tv')) == {'k1': ['k1', 'x'], 'k2': ['k2', 'y']}
	assert _tsvz_warnings(capsys) == ['TSVZ warning: %s: last line had no trailing newline; added one before appending' % p]


def test_l1_tsvzed_append_adds_missing_newline(tmp_path):
	p = str(tmp_path / 'a.tsv')
	_touch(p, b'id\tv\nk1\tx')
	t = TSVZ.TSVZed(p, header='id\tv', rewrite_on_load=False)
	t['k2'] = ['k2', 'y']
	t.close()
	assert open(p, 'rb').read() == b'id\tv\nk1\tx\nk2\ty\n'


def test_l1_tsvzedlite_append_adds_missing_newline(tmp_path):
	p = str(tmp_path / 'a.tsv')
	_touch(p, b'id\tv\nk1\tx')
	lite = TSVZ.TSVZedLite(p, header='id\tv', strict=False)
	lite['k2'] = ['k2', 'y']
	assert lite['k2'] == ['k2', 'y'] and lite['k1'] == ['k1', 'x']
	lite.close()
	assert open(p, 'rb').read() == b'id\tv\nk1\tx\nk2\ty\n'


def test_l6_map_to_file_keeps_newline_at_end_of_file(tmp_path):
	p = str(tmp_path / 'a.tsv')
	_touch(p, b'id\ta\tb\nk1\txx\tyy\n')
	t = TSVZ.TSVZed(p, header='id\ta\tb', rewrite_on_load=False)
	t.memoryOnly = True  # keep these rows out of the append queue
	t['k1'] = ['k1', 'x2', 'y2']  # same length: rewritten in place
	t['k2'] = ['k2', 'p', 'q']    # beyond the end of the file
	t['k3'] = ['k3', 'r', 's']
	t.memoryOnly = False
	t.mapToFile()
	t.close()
	assert open(p, 'rb').read() == b'id\ta\tb\nk1\tx2\ty2\nk2\tp\tq\nk3\tr\ts\n'


# ==========================================================================
# Legacy fixes L4 and L5
# ==========================================================================
def test_l4_invalid_utf8_is_reported_once(tmp_path, capsys):
	p = str(tmp_path / 'a.tsv')
	_touch(p, b'id\tv\nk1\t\xff\nk2\t\xfe\n')
	data = TSVZ.readTabularFile(p, header='id\tv')
	assert data['k1'] == ['k1', '�']
	assert _tsvz_warnings(capsys) == ['TSVZ warning: %s: invalid utf8 replaced with U+FFFD (2 occurrences, first at line 2)' % p]


def test_l4_strict_drops_are_reported(tmp_path, capsys):
	p = str(tmp_path / 'a.tsv')
	_touch(p, b'id\tv\nk1\tx\nk2\ty\tEXTRA\n')
	data = TSVZ.readTabularFile(p, header='id\tv', strict=True)
	assert list(data) == ['k1']
	assert _tsvz_warnings(capsys) == ['TSVZ warning: %s: dropped a line whose column count is not 2 (strict mode)' % p]


def test_l5_damaged_gzip_reads_up_to_the_damage(tmp_path, capsys):
	p = str(tmp_path / 'a.tsv.gz')
	_touch(p, gzip.compress(b'id\tv\nk1\tx\n') + gzip.compress(b'k2\ty\n')[:-6])
	data = TSVZ.readTabularFile(p, header='id\tv')
	assert data['k1'] == ['k1', 'x']
	warnings = _tsvz_warnings(capsys)
	assert len(warnings) == 1 and 'compressed stream is damaged' in warnings[0]


def test_l5_corrupt_uncompressed_errors_still_raise(tmp_path):
	with pytest.raises(FileNotFoundError):
		TSVZ.readTabularFile(str(tmp_path / 'missing.tsv'))


def test_l5_damaged_header_line_reads_up_to_the_damage(tmp_path, capsys):
	p = str(tmp_path / 'a.tsv.gz')
	_touch(p, gzip.compress(b'id\tv\nk1\tx\n')[:12])
	data = TSVZ.readTabularFile(p, header='id\tv')
	assert data == {}
	warnings = _tsvz_warnings(capsys)
	assert len(warnings) == 1 and 'compressed stream is damaged' in warnings[0]


def test_l4_invalid_utf8_in_header_is_reported_and_matches_339(tmp_path, capsys):
	p = str(tmp_path / 'a.tsv')
	_touch(p, b'id\tv\xff\nk1\tx\n')
	data = TSVZ.readTabularFile(p, header='id\tv')
	assert data == TSVZ_old.readTabularFile(p, header='id\tv')
	assert _tsvz_warnings(capsys) == ['TSVZ warning: %s: invalid utf8 replaced with U+FFFD (line 1)' % p]


# ==========================================================================
# Legacy fixes L2 and L3
# ==========================================================================
def test_l2_tsvzed_all_empty_row_is_a_delete(tmp_path, capsys):
	p = str(tmp_path / 'a.tsv')
	t = TSVZ.TSVZed(p, header='id\ta\tb', rewrite_on_load=False)
	t['k'] = ['k', 'x', 'y']
	t['k'] = ['k', '', '']
	assert 'k' not in t
	t['j'] = ['j', '', '']
	assert 'j' not in t
	t['#scratch'] = ['#scratch', '', '']  # '#' keys stay memory-only, untouched by L2
	assert '#scratch' in t
	t.close()
	assert 'k' not in TSVZ.readTabularFile(p, header='id\ta\tb')
	warnings = _tsvz_warnings(capsys)
	assert len(warnings) == 1 and 'values are all empty is a delete' in warnings[0]


def test_l2_tsvzedlite_all_empty_row_is_a_delete(tmp_path, capsys):
	p = str(tmp_path / 'a.tsv')
	lite = TSVZ.TSVZedLite(p, header='id\ta\tb', strict=False)
	lite['k'] = ['k', 'x', 'y']
	lite['k'] = ['k', '', '']
	assert 'k' not in lite.indexes
	lite.close()
	assert 'k' not in TSVZ.readTabularFile(p, header='id\ta\tb')
	assert len(_tsvz_warnings(capsys)) == 1


def test_l3_tsvzedlite_on_gzip_keeps_rows_in_memory(tmp_path, capsys):
	p = str(tmp_path / 'a.tsv.gz')
	TSVZ.appendLinesTabularFile(p, [['k1', 'x']], header='id\tv', createIfNotExist=True)
	lite = TSVZ.TSVZedLite(p, header='id\tv', strict=False)
	assert lite['k1'] == ['k1', 'x']
	lite['k2'] = ['k2', 'y']
	assert lite['k2'] == ['k2', 'y']
	del lite['k1']
	repr(lite)
	lite.close()
	assert dict(TSVZ.readTabularFile(p, header='id\tv')) == {'k2': ['k2', 'y']}
	assert any('keeps rows in memory' in w for w in _tsvz_warnings(capsys))


def test_l3_tsvzedlite_gzip_clear(tmp_path):
	p = str(tmp_path / 'a.tsv.gz')
	TSVZ.appendLinesTabularFile(p, [['k1', 'x']], header='id\tv', createIfNotExist=True)
	lite = TSVZ.TSVZedLite(p, header='id\tv', strict=False)
	lite.clear()
	lite.close()
	assert gzip.decompress(open(p, 'rb').read()) == b'id\tv\n'


def test_l3_tsvzedlite_hash_keys_are_readable(tmp_path):
	p = str(tmp_path / 'a.tsv')
	lite = TSVZ.TSVZedLite(p, header='id\tv', strict=False)
	lite['#memo'] = ['#memo', 'ram only']
	assert lite['#memo'] == ['#memo', 'ram only']
	assert lite.pop('#memo') == ['#memo', 'ram only']
	lite['#memo'] = ['#memo', 'again']
	assert lite.popitem() == ('#memo', ['#memo', 'again'])
	lite.close()


# ==========================================================================
# Spec dialect: codec, reader state, record pipeline (spec §7–§14)
# ==========================================================================
def test_spec_field_codec_roundtrip_every_variant():
	for d in ('\t', ',', '\0', '|'):
		for value in ['a' + d + 'b', '<sep>', '<LF>', 'a<b', '#x', 'x\ny', '<lt>', '<#>', '', 'é<中>', 'c\rr', 'x\r', '<CR>']:
			assert TSVZ._specDecodeField(TSVZ._specEncodeField(value, d), d) == value, (d, value)
			assert TSVZ._specDecodeField(TSVZ._specEncodeField(value, d, isKey=True), d) == value, (d, value)


def test_spec_13_5_examples():
	E, D = TSVZ._specEncodeField, TSVZ._specDecodeField
	assert E('a\tb', '\t') == 'a<sep>b'
	assert E('<sep>', '\t') == '<lt>sep>'
	assert E('<LF>', '\t') == '<lt>LF>'
	assert E('#foo', '\t', isKey=True) == '<#>foo'
	assert E('#foo', '\t') == '#foo'
	assert E('a<b', '\t') == 'a<lt>b'
	assert D('<future>', '\t') == '<future>'
	assert D('</sep/>', '\t') == '</sep/>'
	assert E('a\rb', '\t') == 'a<CR>b' and E('x\r', '\t') == 'x<CR>' and E('<CR>', '\t') == '<lt>CR>'
	assert D('a<CR>b', '\t') == 'a\rb' and D('<cr>', '\t') == '<cr>'


def test_spec_format_record():
	F = TSVZ._specFormatRecord
	assert F(['#k', 'v'], '\t') == '<#>k\tv'
	assert F(['k'], '\t') == 'k'
	assert F(['#_defaults_#', 'a<b', ''], '\t', marker=True) == '#_defaults_#\ta<lt>b\t'
	assert F(['#_defaults_#', 'v'], '\t') == '<#>_defaults_#\tv'


def _process(lines, d='\t', state=None):
	state = state or TSVZ._SpecState()
	reporter = TSVZ._Reporter('t')
	return [TSVZ._specProcessRecord(text, state, d, reporter, None) for text in lines], state, reporter


def test_spec_tombstone_only_without_delimiter():
	out, _, _ = _process(['k', 'k\t', 'k\t\t', '\tv', '', '#c', ' #notcomment\tv', '<#>h\tv'])
	assert out == [('k', None), ('k', ['k', '']), ('k', ['k', '', '']), None, None, None,
				   (' #notcomment', [' #notcomment', 'v']), ('#h', ['#h', 'v'])]


def test_spec_strip_trailing_whites_marker():
	out, _, _ = _process(['k \tv \t', '#_strip_trailing_whites_#\tFALSE', 'k \tv ', '#_STRIP_TRAILING_WHITES_#', 'j\tv '])
	assert out[0] == ('k', ['k', 'v', ''])
	assert out[2] == ('k ', ['k ', 'v '])
	assert out[4] == ('j', ['j', 'v'])


def test_spec_fill_empty_and_defaults_binding():
	out, state, _ = _process(['#_defaults_#\tg\t0', 'a\t\t5', '#_fill_empty_with_default_#\ttrue', 'b\t\t5', '#_defaults_#', 'c\t\t5'])
	assert out[1] == ('a', ['a', '', '5'])
	assert out[3] == ('b', ['b', 'g', '5'])
	assert out[5] == ('c', ['c', '', '5'])
	assert state.defaults == ['#_defaults_#']


def test_spec_bad_marker_values_keep_state(capsys):
	_, state, reporter = _process(['#_fill_empty_with_default_#\tmaybe', '#_version_#\tx',
								   '#_return_defaults_when_missing_#\tno', '#_version_#\t3',
								   '#__custom__#\tanything', '#_unknown_#\t1', '#_rotate_#\tRENAME'])
	reporter.flush()
	assert state.fillEmpty is False and state.returnDefaults is False and state.version == 3
	assert state.rotate == 'rename'
	warnings = _tsvz_warnings(capsys)
	assert len(warnings) == 2
	assert 'invalid value' in warnings[0] and '2 occurrences' in warnings[0]
	assert 'declares spec version 3' in warnings[1]


def test_spec_marker_keys_are_case_insensitive_and_values_decoded():
	_, state, _ = _process(['#_DEFAULTS_#\ta<sep>b\t<lt>'])
	assert state.defaults == ['#_defaults_#', 'a\tb', '<']
	assert state.sawDefaults and state.rowState.defaults == ('#_defaults_#', 'a\tb', '<')


def test_spec_state_from_row_state():
	state = TSVZ._SpecState(['#_defaults_#', 'x'])
	state.strip = False
	state.refresh()
	copy = TSVZ._SpecState.fromRowState(state.rowState)
	assert copy.defaults == ['#_defaults_#', 'x'] and copy.strip is False and copy.fillEmpty is False


def test_spec_digests():
	crc = TSVZ._newDigest('crc32')
	crc.update(b'abc')
	assert crc.hexdigest() == '352441c2'
	assert TSVZ._newDigest('sha256') is not None
	assert TSVZ._newDigest('shake_128') is None and TSVZ._newDigest('crc32c') is None and TSVZ._newDigest('blake3') is None
	assert TSVZ._digestSupported('md5') and not TSVZ._digestSupported('nope')


def test_normalize_defaults():
	N = TSVZ._normalizeDefaults
	assert N(None, '\t') == ['#_defaults_#']
	assert N(..., '\t') == ['#_defaults_#']
	assert N('#_defaults_#\tNA\t0', '\t') == ['#_defaults_#', 'NA', '0']
	assert N(['NA', ' 0 '], '\t') == ['#_defaults_#', 'NA', ' 0']
	assert N(['', ''], '\t') == ['#_defaults_#']
	assert N('NA,0', ',') == ['#_defaults_#', 'NA', '0']


def test_spec_version_marker_with_unicode_digits_is_rejected(capsys):
	_, state, reporter = _process(['#_version_#\t²', '#_version_#\t٣'])
	assert state.version == 1
	reporter.flush()
	warnings = _tsvz_warnings(capsys)
	assert len(warnings) == 1
	assert 'invalid value' in warnings[0] and '2 occurrences' in warnings[0]


# ==========================================================================
# Spec dialect: part reader, checksums, multi-part assembly (§4, §15–§17)
# ==========================================================================
def _replay(paths, d='\t'):
	state = TSVZ._SpecState()
	reporter = TSVZ._Reporter('t')
	infos = []
	data = OrderedDict()
	for _, _, _, record in TSVZ._specReplay(paths, d, state, reporter, infos):
		if record is None:
			continue
		key, row = record
		if row is None:
			data.pop(key, None)
		else:
			data[key] = row
	reporter.flush()
	return data, state, infos


def test_spec_commit_rule_crlf_and_bom(tmp_path, capsys):
	p = str(tmp_path / 'a.tsvz')
	_touch(p, b'\xef\xbb\xbfa\t1\r\nb\t2\r\nc\t3')
	data, _, infos = _replay([p])
	assert dict(data) == {'a': ['a', '1'], 'b': ['b', '2']}
	assert infos[0].tail == b'c\t3'
	assert sorted(_tsvz_warnings(capsys)) == sorted([
		'TSVZ warning: t: stripped a UTF-8 byte order mark (line 1)',
		"TSVZ warning: t: ignored uncommitted bytes after the last newline: b'c\\t3'",
	])


def test_spec_invalid_utf8_is_replaced_and_reported(tmp_path, capsys):
	p = str(tmp_path / 'u.tsvz')
	_touch(p, b'a\t\xff\nb\t\xfe\n')
	data, _, _ = _replay([p])
	assert dict(data) == {'a': ['a', '�'], 'b': ['b', '�']}
	assert _tsvz_warnings(capsys) == ['TSVZ warning: t: invalid UTF-8 replaced with U+FFFD (2 occurrences, first at line 1)']


def test_spec_lines_split_across_chunks(tmp_path, monkeypatch):
	monkeypatch.setattr(TSVZ, '_CHUNK', 3)
	p = str(tmp_path / 'a.tsvz')
	_touch(p, 'ключ\tзначение\r\nk2\t中文\n'.encode('utf-8'))
	data, _, _ = _replay([p])
	assert dict(data) == {'ключ': ['ключ', 'значение'], 'k2': ['k2', '中文']}
	g = str(tmp_path / 'b.tsvz.gz')
	_touch(g, gzip.compress('ключ\tзначение\r\n'.encode('utf-8')) + gzip.compress(b'k2\tv\n'))
	data, _, infos = _replay([g])
	assert dict(data) == {'ключ': ['ключ', 'значение'], 'k2': ['k2', 'v']}
	assert not infos[0].damaged


def test_spec_gzip_damage(tmp_path, capsys):
	g = str(tmp_path / 'g.tsvz.gz')
	_touch(g, gzip.compress(b'a\t1\n') + b'\0\0\0' + gzip.compress(b'b\t2\n') + gzip.compress(b'c\t3\n')[:-6])
	data, _, infos = _replay([g])
	assert dict(data) == {'a': ['a', '1'], 'b': ['b', '2'], 'c': ['c', '3']}  # a member cut short keeps its lines
	assert not infos[0].lost and 'cut short' in infos[0].damaged[0]
	_touch(g, gzip.compress(b'a\t1\n') + b'garbage!')
	data, _, infos = _replay([g])
	assert dict(data) == {'a': ['a', '1']} and infos[0].lost  # nothing decodes from the garbage to recover
	assert len(_tsvz_warnings(capsys)) == 2


def test_spec_bz2_and_xz_parts(tmp_path):
	import bz2
	import lzma
	b = str(tmp_path / 'a.tsvz.bz2')
	x = str(tmp_path / 'b.tsvz.xz')
	_touch(b, bz2.compress(b'a\t1\n') + bz2.compress(b'b\t2\n'))
	_touch(x, lzma.compress(b'a\t1\n') + lzma.compress(b'b\t2\n'))
	assert dict(_replay([b])[0]) == dict(_replay([x])[0]) == {'a': ['a', '1'], 'b': ['b', '2']}


@pytest.mark.parametrize('suffix', TSVZ.COMPRESSED_FILE_EXTENSIONS)
def test_every_codec_suffix_writes_its_own_codec(tmp_path, suffix):
	# Each spelling of a codec (.xz and .lzma, .gz and .gzip, ...) compresses with it, through every writer.
	if not TSVZ._codecSupported(suffix):
		pytest.skip('no {} support in this Python'.format(suffix))
	p = str(tmp_path / ('a.tsv.' + suffix))
	with TSVZ.openFileAsCompressed(p, 'wb') as f:
		f.write(b'k\tv\n')
	with open(p, 'rb') as f:
		assert TSVZ._newDecompressor(suffix).decompress(f.read()) == b'k\tv\n'
	assert TSVZ._newDecompressor(suffix).decompress(TSVZ._compressBytes(suffix, b'x\n')) == b'x\n'


def _block_zstd(monkeypatch):
	"""Make this Python look like one without compression.zstd (before 3.14)."""
	monkeypatch.setitem(sys.modules, 'compression.zstd', None)
	if 'compression' in sys.modules:  # Python 3.14+: an earlier import left zstd as an attribute of the package
		monkeypatch.delattr(sys.modules['compression'], 'zstd', raising=False)
	monkeypatch.setattr(TSVZ, '_CODEC_SUPPORT', {}, raising=False)


ZST_JUNK = b'\x28\xb5\x2f\xfdjunk'


def test_spec_part_in_an_unsupported_codec_is_unreadable_and_never_repaired(tmp_path, monkeypatch):
	# Spec §16.5: a reader without a part's codec fails; it neither skips the part nor reads or repairs its bytes.
	_block_zstd(monkeypatch)
	z = str(tmp_path / 'a.tsvz.zst')
	raw = b'\x28\xb5\x2f\xfdjunk'
	_touch(z, raw)
	status, out, err = _run('read', z)
	assert status == 1 and out == '' and 'no zstd support' in err
	status, _, err = _run('set', z, 'k', 'v')
	assert status == 1 and 'no zstd support' in err
	for operation in ('clear', 'scrub'):
		assert _run(operation, z)[0] == 1, operation
	with open(z, 'rb') as f:
		assert f.read() == raw
	assert os.listdir(str(tmp_path)) == ['a.tsvz.zst']


def test_spec_a_store_is_not_created_in_an_unsupported_codec(tmp_path, monkeypatch):
	_block_zstd(monkeypatch)
	new = str(tmp_path / 'new.tsvz.zst')
	status, _, err = _run('set', new, 'k', 'v')
	assert status == 1 and 'no zstd support' in err and not os.path.exists(new)


def test_spec_clear_writes_nothing_when_a_part_cannot_be_read(tmp_path, monkeypatch):
	# Tombstones for the readable parts alone would leave the unreadable part's keys alive.
	_block_zstd(monkeypatch)
	base = str(tmp_path / 'm.tsvz')
	_touch(base + '.1.zst', ZST_JUNK)
	_touch(base + '.2', b'b\t2\n')
	assert _run('clear', base)[0] == 1
	with open(base + '.1.zst', 'rb') as f:
		assert f.read() == ZST_JUNK
	with open(base + '.2', 'rb') as f:
		assert f.read() == b'b\t2\n'


def _try(call):
	try:
		call()
	except Exception:
		pass


def test_legacy_writes_refuse_a_codec_this_python_lacks(tmp_path, monkeypatch):
	# The older .tsv-family writers used to fall back to plain file I/O over a .zst file and destroy it.
	_block_zstd(monkeypatch)
	p = str(tmp_path / 'a.tsv.zst')
	_touch(p, ZST_JUNK)
	_try(lambda: TSVZ.appendLinesTabularFile(p, [['k', 'v']]))
	_try(lambda: TSVZ.scrubTabularFile(p))
	_try(lambda: TSVZ.TSVZed(p).close())
	with open(p, 'rb') as f:
		assert f.read() == ZST_JUNK
	new = str(tmp_path / 'new.tsv.zst')
	_try(lambda: TSVZ.appendLinesTabularFile(new, [['k', 'v']], createIfNotExist=True))
	assert not os.path.exists(new)


def test_spec_constructor_defaults_are_not_written_when_a_part_cannot_be_read(tmp_path, monkeypatch):
	# The store's own #_defaults_# may be in the part that could not be read.
	_block_zstd(monkeypatch)
	base = str(tmp_path / 'm.tsvz')
	_touch(base + '.1.zst', ZST_JUNK)
	_touch(base + '.2', b'b\t2\n')
	TSVZ.TSVZed(base, defaults=['k', 'dflt']).close()
	lite = TSVZ.TSVZedLite(base, defaults=['k', 'dflt'])
	assert not lite._pendingDefaults
	lite.close()
	with open(base + '.2', 'rb') as f:
		assert f.read() == b'b\t2\n'


def test_spec_read_then_write_operations_refuse_an_unreadable_store(tmp_path, monkeypatch):
	# setdefault on a key only the unreadable part holds would hide its value; pop and popitem would act on a partial view.
	_block_zstd(monkeypatch)
	base = str(tmp_path / 'm.tsvz')
	_touch(base + '.1.zst', ZST_JUNK)
	_touch(base + '.2', b'b\t2\n')
	assert _run('setdefault', base, 'k', 'new')[0] == 1
	assert _run('pop', base, 'b')[0] == 1
	assert _run('popitem', base)[0] == 1
	with open(base + '.2', 'rb') as f:
		assert f.read() == b'b\t2\n'


def test_spec_strict_scrub_reports_an_unreadable_first_part_not_a_header_mismatch(tmp_path, monkeypatch, capsys):
	# The header lives in part 0: when part 0 cannot be read, there is no header to compare.
	_block_zstd(monkeypatch)
	base = str(tmp_path / 'm.tsvz')
	_touch(base + '.1.zst', ZST_JUNK)
	_touch(base + '.2', b'b\t2\n')
	TSVZ.scrubTabularFile(base, header='key\tvalue', strict=True)
	assert 'could not be read' in capsys.readouterr().err
	with open(base + '.1.zst', 'rb') as f:
		assert f.read() == ZST_JUNK
	with open(base + '.2', 'rb') as f:
		assert f.read() == b'b\t2\n'


GZ_CORRUPT = b'\x1f\x8b\x08\x00' + b'\xff' * 40  # a gzip header, then nothing that decodes


def test_spec_scrub_keeps_the_bytes_of_a_damaged_compressed_part(tmp_path, capsys):
	# A scrub compacts what can be read; the damaged original is kept as <part>.damaged-<time>, as an append does.
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, gzip.compress(b'a\t1\n') + GZ_CORRUPT)
	TSVZ.scrubTabularFile(p)
	backups = [name for name in os.listdir(str(tmp_path)) if name.startswith('m.tsvz.gz.damaged-')]
	assert len(backups) == 1 and 'damaged' in capsys.readouterr().err
	with open(str(tmp_path / backups[0]), 'rb') as f:
		assert f.read() == gzip.compress(b'a\t1\n') + GZ_CORRUPT
	with open(p, 'rb') as f:
		assert gzip.decompress(f.read()).splitlines()[-1] == b'a\t1'


def test_spec_strict_read_reports_a_damaged_first_part_not_a_header_mismatch(tmp_path, capsys):
	# Nothing of part 0 could be decoded, so there is no header to compare.
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, GZ_CORRUPT)
	assert TSVZ.readTabularFile(p, header='key\tvalue', strict=True) == {}
	assert 'corrupt' in capsys.readouterr().err


GZ_TRUNCATED_B = gzip.compress(b'b\t2\n')[:-6]  # a member cut short in its trailer, as a crash leaves it


def test_spec_one_shot_append_after_a_cut_member_keeps_its_lines(tmp_path):
	# Spec §16.6: a member cut short keeps its whole lines, also once something is appended after it.
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, gzip.compress(b'a\t1\n') + GZ_TRUNCATED_B)
	TSVZ.appendLinesTabularFile(p, [['c', '3']])
	assert set(TSVZ.readTabularFile(p)) == {'a', 'b', 'c'}


def test_spec_append_to_a_compressed_part_reads_none_of_it(tmp_path, monkeypatch):
	# An append costs the same whatever the part's size: it does not read the part (spec §16.4).
	p = str(tmp_path / 'big.tsvz.gz')
	_touch(p, gzip.compress(b''.join(b'k%d\t%s\n' % (i, os.urandom(16).hex().encode()) for i in range(40000))))
	read = []
	real_open = open

	class Counting(object):
		def __init__(self, f):
			self._f = f
		def read(self, *args):
			data = self._f.read(*args)
			read.append(len(data))
			return data
		def __getattr__(self, name):
			return getattr(self._f, name)
		def __enter__(self):
			return self
		def __exit__(self, *exc):
			self._f.close()

	monkeypatch.setattr(TSVZ, 'open', lambda *args, **kwargs: Counting(real_open(*args, **kwargs)), raising=False)
	TSVZ.appendLinesTabularFile(p, [['new', 'v']])
	monkeypatch.undo()
	assert sum(read) == 0
	assert TSVZ.readTabularFile(p)['new'] == ['new', 'v']

def test_engine_writes_a_cut_members_lines_again(tmp_path):
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, gzip.compress(b'a\t1\n') + GZ_TRUNCATED_B)
	store = TSVZ._ServeStore(p, create=True)
	store.write([(['c', '3'], False)], False, None)
	store.close()
	assert set(TSVZ.readTabularFile(p)) == {'a', 'b', 'c'}

def _crc_only_flip(member):
	"""A copy of gzip ``member`` with one bit flipped that only its checksum catches."""
	for pos in range(10, len(member) - 8):
		for bit in range(8):
			bad = bytearray(member)
			bad[pos] ^= 1 << bit
			try:
				zlib.decompressobj(wbits=47).decompress(bytes(bad))
			except zlib.error as e:
				if 'incorrect data check' in str(e):
					return bytes(bad)
	raise AssertionError('no bit flip that only the checksum catches')


def test_spec_a_corrupt_member_is_recovered_by_default_and_scrub_keeps_a_copy(tmp_path, monkeypatch):
	# Spec §4.10 recover: what the member decoded before it failed is used, a byte at a time, so even the
	# piece that failed gives up its output; a scrub, which re-checksums it, keeps the original bytes.
	records = b''.join(b'key%05d\tv\n' % i for i in range(2000))
	bad = _crc_only_flip(gzip.compress(records))
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, gzip.compress(b'a\t1\n') + bad)
	monkeypatch.setattr(TSVZ, '_CHUNK', 64)  # many small reads: the recovery must not depend on them
	assert len(TSVZ.readTabularFile(p)) == 2001
	monkeypatch.undo()
	TSVZ.scrubTabularFile(p)
	assert any(n.startswith('m.tsvz.gz.damaged-') for n in os.listdir(str(tmp_path)))


def test_spec_a_corrupt_member_is_neither_read_nor_scrubbed_under_skip(tmp_path):
	bad = _crc_only_flip(gzip.compress(b''.join(b'key%05d\tv\n' % i for i in range(2000))))
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, gzip.compress(b'#_on_corruption_#\tskip\na\t1\n') + bad)
	assert set(TSVZ.readTabularFile(p)) == {'a'}
	TSVZ.scrubTabularFile(p)
	with open(p, 'rb') as f:
		assert b'key' not in gzip.decompress(f.read())


def test_spec_records_appended_after_damage_are_read(tmp_path):
	# Spec §16.6: reading goes on after a damaged member, so what was appended after it reads at once.
	p = str(tmp_path / 'm.tsvz.gz')
	big = gzip.compress(b''.join(b'k%05d\t%s\n' % (i, os.urandom(8).hex().encode()) for i in range(8000)))
	_touch(p, gzip.compress(b'a\t1\n') + big[:-1000] + GZ_CORRUPT)
	TSVZ.appendLinesTabularFile(p, [['new', 'row']])
	before = TSVZ.readTabularFile(p)
	assert before['new'] == ['new', 'row'] and 'a' in before and 'k00000' in before  # the cut member keeps its lines
	TSVZ.scrubTabularFile(p)
	assert TSVZ.readTabularFile(p) == before
	assert any(name.startswith('m.tsvz.gz.damaged-') for name in os.listdir(str(tmp_path)))


def test_spec_strict_scrub_of_a_part_damaged_before_any_line(tmp_path, capsys):
	# The repair leaves the part empty; the scrub does not then call that a header mismatch.
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, GZ_CORRUPT)
	TSVZ.scrubTabularFile(p, header='key\tvalue', strict=True)
	assert any(name.startswith('m.tsvz.gz.damaged-') for name in os.listdir(str(tmp_path)))


def test_spec_scrub_of_a_part_ending_in_a_cut_member_keeps_its_lines(tmp_path, monkeypatch):
	monkeypatch.setattr(TSVZ, '_SPEC_REWRITE_ATTEMPTS', 1)
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, gzip.compress(b'a\t1\n') + GZ_TRUNCATED_B)
	assert TSVZ._specScrub(p, delimiter='\t')[1] is True
	assert set(TSVZ.readTabularFile(p)) == {'a', 'b'}


def test_spec_scrub_refuses_when_the_damaged_part_cannot_be_backed_up(tmp_path, monkeypatch, capsys):
	# No room for <part>.damaged-<time> (a full disk, a read-only directory): nothing is written.
	def no_room(path, reporter):
		raise OSError(28, 'No space left on device')
	monkeypatch.setattr(TSVZ, '_specBackupPart', no_room)
	p = str(tmp_path / 'm.tsvz.gz')
	raw = gzip.compress(b'a\t1\n') + GZ_CORRUPT
	_touch(p, raw)
	assert TSVZ._specScrub(p, delimiter='\t')[1] is False
	assert 'could not keep a copy' in capsys.readouterr().err
	with open(p, 'rb') as f:
		assert f.read() == raw

def test_spec_backup_leaves_no_partial_copy_when_the_copy_fails(tmp_path, monkeypatch):
	def full(source, target, *args):
		target.write(b'partial')
		raise OSError(28, 'No space left on device')
	monkeypatch.setattr(TSVZ.shutil, 'copyfileobj', full)
	p = str(tmp_path / 'm.tsvz.gz')
	raw = gzip.compress(b'a\t1\n') + GZ_CORRUPT
	_touch(p, raw)
	with pytest.raises(OSError):
		TSVZ._specBackupPart(p, TSVZ._Reporter(p))
	assert os.listdir(str(tmp_path)) == ['m.tsvz.gz']
	with open(p, 'rb') as f:
		assert f.read() == raw

def test_spec_read_stopped_by_an_io_error_counts_as_unreadable(tmp_path, monkeypatch, capsys):
	# A part that opens but cannot be read (EIO) hides its header too: report it, not a header mismatch.
	p = str(tmp_path / 'm.tsvz')
	_touch(p, b'a\t1\n')

	class Failing(object):
		def __init__(self, path, mode):
			pass
		def read(self, size=-1):
			raise OSError(5, 'Input/output error')
		def close(self):
			pass
		def __enter__(self):
			return self
		def __exit__(self, *exc):
			self.close()

	monkeypatch.setattr(TSVZ, 'open', Failing, raising=False)
	assert TSVZ.readTabularFile(p, header='key\tvalue', strict=True) == {}
	assert 'stopped reading' in capsys.readouterr().err


def test_spec_lite_refused_clear_still_writes_pending_defaults(tmp_path, monkeypatch):
	p = str(tmp_path / 'd.tsvz')
	lite = TSVZ.TSVZedLite(p, defaults=['key', 'dflt'])
	lite.close()
	_touch(p, b'a\t1\n')  # the store lost its #_defaults_# marker
	lite = TSVZ.TSVZedLite(p, defaults=['key', 'dflt'])
	_touch(p, b'a\t1\n')
	monkeypatch.setattr(TSVZ, '_specClearTabularFile', lambda *args, **kwargs: False)
	try:
		with pytest.raises(OSError):
			lite.clear()
	finally:
		lite.close()
	with open(p, 'rb') as f:
		assert b'#_defaults_#' in f.read()


def _gzip_members(raw):
	members = []
	while raw:
		d = zlib.decompressobj(31)
		members.append(d.decompress(raw))
		assert d.eof
		raw = d.unused_data
	return members


def test_compressed_writes_are_split_into_members_at_record_boundaries():
	# Spec §16.7: at most memberSize bytes of records in a member; a longer record is a member of its own.
	data = b''.join(b'k%05d\t%s\n' % (i, b'v' * 30) for i in range(3000))
	members = _gzip_members(TSVZ._compressBytes('gz', data, memberSize=10000))
	assert len(members) > 1 and b''.join(members) == data
	assert all(len(m) <= 10000 and m.endswith(b'\n') for m in members)
	long = b'x' * 25000 + b'\n'
	assert _gzip_members(TSVZ._compressBytes('gz', long + b'a\tb\n', memberSize=10000)) == [long, b'a\tb\n']
	assert len(_gzip_members(TSVZ._compressBytes('gz', data))) == 1  # 1 MiB by default


def test_member_size_marker_sets_the_member_size_of_writes_and_scrubs(tmp_path):
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, gzip.compress(b'#_member_size_#\t8192\n#_member_size_#\tbig\na\t1\n'))  # an invalid value is ignored
	t = TSVZ.TSVZed(p)
	for i in range(2000):
		t['k%04d' % i] = ['k%04d' % i, 'v' * 20]
	t.close()
	with open(p, 'rb') as f:
		members = _gzip_members(f.read())
	assert len(members) > 3 and all(len(m) <= 8192 for m in members[1:])
	TSVZ.scrubTabularFile(p)
	with open(p, 'rb') as f:
		members = _gzip_members(f.read())
	assert len(members) > 3 and all(len(m) <= 8192 for m in members)
	assert b'#_member_size_#\t8192\n' in members[0] and len(TSVZ.readTabularFile(p)) == 2001


@pytest.mark.skipif(sys.version_info < (3, 14), reason='zstd needs Python 3.14')
def test_zstd_frames_carry_a_content_checksum(tmp_path):
	# Spec §16.8: the frame header descriptor's content checksum flag (0x04).
	raw = TSVZ._compressBytes('zst', b''.join(b'k%05d\tv\n' % i for i in range(300000)))
	assert raw[4] & 0x04
	p = str(tmp_path / 'z.tsv.zst')
	with TSVZ.openFileAsCompressed(p, 'wb') as f:
		f.write(b'a\tb\n')
	with open(p, 'rb') as f:
		assert f.read()[4] & 0x04


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='open file description locks are Linux-only')
def test_lock_survives_another_descriptor_of_the_part_closing(tmp_path):
	# Spec §18.6: a process-owned POSIX lock would be dropped when any descriptor of the file closes.
	p = str(tmp_path / 'l.tsvz')
	_touch(p, b'a\t1\n')
	with open(p, 'r+b') as held:
		TSVZ._lockFile(held)
		open(p, 'rb').close()  # e.g. a reader in another thread of this process
		probe = ('import fcntl, sys\nf = open(sys.argv[1], "r+b")\n'
				 'try:\n    fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\nexcept OSError:\n    sys.exit(3)\nsys.exit(0)\n')
		code = subprocess.call([sys.executable, '-c', probe, p])
		TSVZ._unlockFile(held)
	assert code == 3


_LOCK_PROBE = ('import fcntl, sys\nf = open(sys.argv[1], "r+b")\n'
			   'try:\n    fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\nexcept OSError:\n    sys.exit(3)\nsys.exit(0)\n')


@pytest.mark.skipif(not hasattr(os, 'register_at_fork'), reason='os.register_at_fork (Python 3.7+)')
@pytest.mark.filterwarnings('ignore::DeprecationWarning')  # fork() in a process with threads: here on purpose
def test_a_forked_child_does_not_wait_for_a_path_lock_its_parent_held(tmp_path, monkeypatch):
	# A parent thread waits for another process's lock, holding the in-process lock of the path:
	# that thread is not in the child.
	import signal
	monkeypatch.setattr(TSVZ, '_OFD_COMMANDS', None)
	p = str(tmp_path / 'l.tsvz')
	_touch(p, b'a\t1\n')
	hold = ('import fcntl, sys, time\nf = open(sys.argv[1], "r+b")\nfcntl.lockf(f, fcntl.LOCK_EX)\n'
			'print("held", flush=True)\ntime.sleep(1.5)\n')
	holder = subprocess.Popen([sys.executable, '-c', hold, p], stdout=subprocess.PIPE)
	try:
		assert holder.stdout.readline().strip() == b'held'
		writer = threading.Thread(target=TSVZ._lockedAppend, args=(p, b'b\t2\n', 'truncate', TSVZ._Reporter(p)))
		writer.start()
		time.sleep(0.2)  # waiting for the lock, holding the path's in-process lock
		pid = os.fork()
		if pid == 0:
			try:
				signal.alarm(8)
				TSVZ._lockedAppend(p, b'c\t3\n', 'truncate', TSVZ._Reporter(p))
				os._exit(0)
			except BaseException:
				os._exit(1)
		_, status = os.waitpid(pid, 0)
		writer.join(10)
	finally:
		holder.wait(10)
		holder.stdout.close()
	assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def _counting_reads(monkeypatch):
	read = []
	real_open = open

	class Counting(object):
		def __init__(self, f):
			self._f = f
		def read(self, *args):
			data = self._f.read(*args)
			read.append(len(data))
			return data
		def __getattr__(self, name):
			return getattr(self._f, name)
		def __enter__(self):
			return self
		def __exit__(self, *exc):
			self._f.close()
	monkeypatch.setattr(TSVZ, 'open', lambda *args, **kwargs: Counting(real_open(*args, **kwargs)), raising=False)
	return read


def test_spec_reading_many_small_members_reads_the_part_once(tmp_path, monkeypatch):
	# One member per record is what per-record appends write; reading must stay linear in the part's size.
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, b''.join(gzip.compress(b'k%05d\tv\n' % i) for i in range(5000)))
	read = _counting_reads(monkeypatch)
	assert len(TSVZ.readTabularFile(p)) == 5000
	assert sum(read) <= 2 * os.path.getsize(p) + TSVZ._CHUNK


def _tear_first_append(monkeypatch, path):
	"""Make the next append to ``path`` write half its bytes and then fail, as a full disk would."""
	torn = []
	real_open = open

	class Tearing(object):
		def __init__(self, f):
			self._f = f
		def write(self, data):
			if not torn:
				torn.append(len(data))
				self._f.write(data[:len(data) // 2])
				self._f.flush()
				raise OSError(28, 'No space left on device')
			return self._f.write(data)
		def __getattr__(self, name):
			return getattr(self._f, name)
		def __enter__(self):
			return self
		def __exit__(self, *exc):
			self._f.close()

	def opener(name, mode='r', *args, **kwargs):
		f = real_open(name, mode, *args, **kwargs)
		return Tearing(f) if name == path and 'a' in mode else f
	monkeypatch.setattr(TSVZ, 'open', opener, raising=False)
	return torn


def test_spec_lite_repeats_the_cut_lines_after_its_append_tore(tmp_path, monkeypatch):
	# Spec §16.4.2: the torn member that held the copy is debris too, so the next append repeats the lines.
	g = str(tmp_path / 'g.tsvz.gz')
	_touch(g, gzip.compress(b'a\t1\n') + GZ_TRUNCATED_B)
	lite = TSVZ.TSVZedLite(g)
	torn = _tear_first_append(monkeypatch, g)
	with pytest.raises(OSError):
		lite['c'] = ['c', '3']
	monkeypatch.undo()
	assert torn
	lite['d'] = ['d', '4']
	lite.close()
	assert set(TSVZ.readTabularFile(g)) == {'a', 'b', 'd'}


def test_engine_keeps_the_cut_lines_when_an_append_fails(tmp_path, monkeypatch):
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, gzip.compress(b'a\t1\n') + GZ_TRUNCATED_B)
	store = TSVZ._ServeStore(p, create=True)
	real = TSVZ._specAppendPayload

	def refuse(*args, **kwargs):
		raise OSError(24, 'Too many open files')
	monkeypatch.setattr(TSVZ, '_specAppendPayload', refuse)
	with pytest.raises(OSError):
		store._append(b'c\t3\n', False)
	monkeypatch.setattr(TSVZ, '_specAppendPayload', real)
	store._append(b'd\t4\n', False)
	store.close()
	assert set(TSVZ.readTabularFile(p)) == {'a', 'b', 'd'}


def test_engine_does_not_repeat_cut_lines_written_before_a_failed_sync(tmp_path, monkeypatch):
	# The copy was written whole and is read; only the sync failed. Repeating it later would undo b=9.
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, gzip.compress(b'a\t1\n') + GZ_TRUNCATED_B)
	store = TSVZ._ServeStore(p, create=True)
	real = os.fsync

	def failing(fd):
		raise OSError(5, 'Input/output error')
	monkeypatch.setattr(TSVZ.os, 'fsync', failing)
	with pytest.raises(OSError):
		store._append(b'b\t9\n', True)
	monkeypatch.setattr(TSVZ.os, 'fsync', real)
	store._append(b'd\t4\n', False)
	store.close()
	assert TSVZ.readTabularFile(p)['b'] == ['b', '9']


def test_spec_header_check_skips_a_first_part_that_cannot_be_read(tmp_path, monkeypatch):
	# An I/O error reading part 0 (a bad sector) hides the header, as a read is told; the append goes on.
	base = str(tmp_path / 'm.tsvz')
	_touch(base, b'#key\tvalue\na\t1\n')
	_touch(base + '.1', b'b\t2\n')
	real_open = open

	class Failing(object):
		def __init__(self, f):
			self._f = f
		def read(self, *args):
			raise OSError(5, 'Input/output error')
		def __iter__(self):
			raise OSError(5, 'Input/output error')
		def __getattr__(self, name):
			return getattr(self._f, name)
		def __enter__(self):
			return self
		def __exit__(self, *exc):
			self._f.close()
	monkeypatch.setattr(TSVZ, 'open', lambda name, mode='r', *a, **k: Failing(real_open(name, mode, *a, **k)) if name == base and 'r' in mode and '+' not in mode else real_open(name, mode, *a, **k), raising=False)
	TSVZ.appendLinesTabularFile(base, [['c', '3']], header='key\tvalue', verifyHeader=True, strict=True)
	monkeypatch.undo()
	assert TSVZ.readTabularFile(base)['c'] == ['c', '3']


def test_spec_backup_does_not_fail_when_the_directory_cannot_be_synced(tmp_path, monkeypatch):
	# Some network and FUSE file systems refuse fsync on a directory.
	real = os.fsync

	def no_directories(fd):
		if stat.S_ISDIR(os.fstat(fd).st_mode):
			raise OSError(errno.EINVAL, 'Invalid argument')
		return real(fd)
	monkeypatch.setattr(TSVZ.os, 'fsync', no_directories)
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, gzip.compress(b'a\t1\n') + GZ_CORRUPT)
	backup = TSVZ._specBackupPart(p, TSVZ._Reporter(p))
	assert os.path.exists(backup)


def test_spec_header_check_of_an_append_reads_only_the_first_lines(tmp_path, monkeypatch):
	# An append's cost must not grow with the store: the 3.39 header check decodes only what it needs.
	p = str(tmp_path / 'big.tsvz.gz')
	_touch(p, gzip.compress(b'#key\tvalue\n' + b''.join(b'k%d\t%s\n' % (i, os.urandom(16).hex().encode()) for i in range(200000))))
	assert os.path.getsize(p) > 2 * TSVZ._CHUNK
	read = _counting_reads(monkeypatch)
	TSVZ.appendLinesTabularFile(p, [['new', 'v']], header='key\tvalue', verifyHeader=True, strict=True)
	monkeypatch.undo()
	assert sum(read) <= TSVZ._CHUNK + (1 << 16)


def test_spec_header_check_of_an_append_skips_gzip_zero_padding(tmp_path):
	p = str(tmp_path / 's.tsvz.gz')
	_touch(p, b'\0' * 16 + gzip.compress(b'#key\tvalue\na\t1\n'))
	TSVZ.appendLinesTabularFile(p, [['new', 'v']], header='key\tvalue', verifyHeader=True, strict=True)
	assert TSVZ.readTabularFile(p)['new'] == ['new', 'v']


def test_spec_header_check_of_an_append_past_a_damaged_first_member(tmp_path):
	# The reader skips that member (spec §16.6); the header it may have held cannot be compared, so the
	# check is skipped, as a read skips it, instead of crashing the append.
	bad = bytearray(gzip.compress(b''.join(b'#key\tvalue\n' + b'k%04d\tv\n' % i for i in range(500))))
	bad[12] ^= 0xff  # inside the deflate data: a decode error
	p = str(tmp_path / 's.tsvz.gz')
	_touch(p, bytes(bad) + gzip.compress(b'a\t1\n'))
	TSVZ.appendLinesTabularFile(p, [['new', 'v']], header='key\tvalue', verifyHeader=True, strict=True)
	assert TSVZ.readTabularFile(p)['new'] == ['new', 'v']


def test_spec_strict_header_check_still_applies_when_only_the_last_member_is_cut(tmp_path):
	# Part 0 has no line besides a marker, and its last member was cut before it decoded anything: nothing
	# was lost, so the missing header is still a mismatch.
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, gzip.compress(b'#_version_#\t1\n') + gzip.compress(b'a\t1\n')[:12])
	with pytest.raises(ValueError):
		TSVZ.readTabularFile(p, header='key\tvalue', strict=True)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='open file description locks are Linux-only')
def test_unlock_uses_the_lock_kind_that_took_the_lock(tmp_path, monkeypatch):
	# A refused open file description lock on one file must not change how another file is unlocked.
	import fcntl
	monkeypatch.setattr(TSVZ, '_OFD_COMMANDS', TSVZ._OFD_COMMANDS)
	p1, p2 = str(tmp_path / 'one.tsvz'), str(tmp_path / 'two.tsvz')
	_touch(p1, b'a\t1\n')
	_touch(p2, b'b\t2\n')
	f1, f2 = open(p1, 'r+b'), open(p2, 'r+b')
	try:
		TSVZ._lockFile(f1)
		real = fcntl.fcntl

		def refuse_two(fd, cmd, arg=0):
			if fd == f2.fileno():
				raise OSError(errno.EINVAL, 'Invalid argument')
			return real(fd, cmd, arg)
		monkeypatch.setattr(fcntl, 'fcntl', refuse_two)
		TSVZ._lockFile(f2)
		TSVZ._unlockFile(f2)
		monkeypatch.setattr(fcntl, 'fcntl', real)
		TSVZ._unlockFile(f1)
		probe = ('import fcntl, sys\nf = open(sys.argv[1], "r+b")\n'
				 'try:\n    fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\nexcept OSError:\n    sys.exit(3)\nsys.exit(0)\n')
		assert subprocess.call([sys.executable, '-c', probe, p1]) == 0
	finally:
		f1.close()
		f2.close()


def test_spec_scrub_backs_up_again_when_the_part_changed(tmp_path, monkeypatch):
	# A writer appended a damaged member between the first backup and the rewrite: it needs a copy too.
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, gzip.compress(b'a\t1\n') + GZ_CORRUPT + gzip.compress(b'b\t2\n'))
	real = TSVZ._specRewriteInPlace
	calls = []

	def changed_once(path, content, expect=None, memberSize=None):
		if not calls:
			calls.append(1)
			with open(path, 'ab') as f:
				f.write(gzip.compress(b'Q\tq\n')[:-6] + gzip.compress(b'c\t3\n'))
			return None
		return real(path, content, expect=expect, memberSize=memberSize)
	monkeypatch.setattr(TSVZ, '_specRewriteInPlace', changed_once)
	assert TSVZ._specScrub(p, delimiter='\t')[1] is True
	backups = sorted(n for n in os.listdir(str(tmp_path)) if n.startswith('m.tsvz.gz.damaged-'))
	assert len(backups) == 2
	with open(str(tmp_path / backups[-1]), 'rb') as f:
		assert gzip.compress(b'c\t3\n') in f.read()


def test_cli_stop_policy_fails_the_read_but_lets_scrub_rewrite(tmp_path):
	# Spec §4.10 stop: the read fails (exit 1) and changes based on it are refused; a scrub may rewrite
	# what was read, keeping the original bytes, and still exits 1.
	p = str(tmp_path / 's.tsvz')
	raw = b'#_on_corruption_#\tstop\na\t1\n\0\0c\t3\nd\t4\n'
	_touch(p, raw)
	status, out, err = _run('read', p)
	assert (status, out) == (1, 'a\t1\n') and 'stopped' in err
	assert _run('clear', p)[0] == 1
	with open(p, 'rb') as f:
		assert f.read() == raw
	assert _run('scrub', p)[0] == 1
	assert TSVZ.readTabularFile(p) == {'a': ['a', '1']}
	assert any(n.startswith('s.tsvz.damaged-') for n in os.listdir(str(tmp_path)))


def test_cli_every_reading_operation_fails_after_a_stop(tmp_path):
	# Spec §4.10, §20.6: a read that stopped at damaged data has failed, whatever the operation;
	# it still prints what was read before the damage (§21.8.5).
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_on_corruption_#\tstop\na\t1\n\0\0c\t3\nd\t4\n')
	for args, out in ((('read',), 'a\t1\n'), (('get', 'a'), 'a\t1\n'), (('has', 'a'), ''), (('len',), '1\n'),
					  (('keys',), 'a\n'), (('verify',), '')):
		status, got, _ = _run(args[0], p, *args[1:])
		assert (status, got) == (1, out), args


@pytest.mark.skipif(os.name != 'posix', reason='POSIX permission bits')
@pytest.mark.parametrize('part_mode', [0o600, 0o6777])
def test_a_backup_copy_is_never_more_open_than_its_part(tmp_path, part_mode):
	# The <part>.damaged-<time> copy holds the part's data: readable only by whoever could read the
	# part, writable only by its owner, and never setuid, setgid, sticky or executable.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_on_corruption_#\tstop\na\t1\n\0\0c\t3\n')
	os.chmod(p, part_mode)
	assert _run('scrub', p)[0] == 1
	backup = [n for n in os.listdir(str(tmp_path)) if n.startswith('s.tsvz.damaged-')][0]
	mode = os.stat(str(tmp_path / backup)).st_mode & 0o7777
	assert mode & ~(part_mode & 0o644) == 0, '%o' % mode


@pytest.mark.skipif(getattr(os, 'geteuid', lambda: -1)() != 0, reason='needs root to give the part another owner')
def test_a_backup_copy_belongs_to_the_owner_and_group_of_its_part(tmp_path):
	# A copy root makes of alice's part stays alice's, in the part's group, not root's.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_on_corruption_#\tstop\na\t1\n\0\0c\t3\n')
	os.chown(p, 12345, 23456)
	os.chmod(p, 0o640)
	assert _run('scrub', p)[0] == 1
	backup = [n for n in os.listdir(str(tmp_path)) if n.startswith('s.tsvz.damaged-')][0]
	st = os.stat(str(tmp_path / backup))
	assert (st.st_uid, st.st_gid) == (12345, 23456)


@pytest.mark.skipif(not hasattr(os, 'fchown'), reason='POSIX owners')
def test_a_backup_copy_left_in_another_group_loses_its_group_bits(tmp_path, monkeypatch):
	# Where the copy cannot be given the part's group, that group's members must not read it.
	p = str(tmp_path / 'copy')
	_touch(p)
	os.chmod(p, 0o640)
	part = os.stat(p)
	stranger = os.stat_result((part.st_mode, 0, 0, 0, part.st_uid, part.st_gid + 1) + tuple(part)[6:])

	def refuse(*args):
		raise PermissionError(errno.EPERM, 'not permitted')
	monkeypatch.setattr(os, 'fchown', refuse)
	for created in (0o640, 0o600):  # 0o600: what a umask of 077 leaves; it must not be widened
		os.chmod(p, created)
		fd = os.open(p, os.O_RDONLY)
		try:
			TSVZ._matchOwner(fd, stranger)
		finally:
			os.close(fd)
		assert os.stat(p).st_mode & 0o777 == 0o600, '%o' % created


@pytest.mark.skipif(not hasattr(os, 'posix_fallocate'), reason='needs posix_fallocate and RLIMIT_FSIZE')
def test_a_rewrite_that_cannot_get_its_space_leaves_the_part_as_it_was(tmp_path):
	# Spec §19.8: reserve the space first, so a full disk (here a file-size limit) refuses the rewrite
	# before any byte of the part changes.
	import resource
	import signal
	p = str(tmp_path / 's.tsvz')
	original = ('#_defaults_#\tdefault-one\tdefault-two\n' + ''.join('k%d\tv\n' % i for i in range(2000))).encode()
	_touch(p, original)  # scrubbing resolves the defaults into every row: the part grows
	limit = len(original) + 100

	def limited():
		signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
		resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

	done = subprocess.run([sys.executable, os.path.join(HERE, 'TSVZ.py'), 'scrub', p], preexec_fn=limited,
						  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	assert done.returncode == 1, done.stderr
	with open(p, 'rb') as f:
		assert f.read() == original, 'the part was changed by a rewrite that could not finish'


@pytest.mark.parametrize('call', ['write', 'ftruncate'])
@pytest.mark.parametrize('lines', [5, 2000], ids=['shrinks', 'grows'])
def test_a_rewrite_interrupted_after_a_call_puts_the_old_bytes_back(tmp_path, monkeypatch, call, lines):
	# A Ctrl-C can land after a write or the cut has happened and before the code notes it.
	p = str(tmp_path / 's.tsvz')
	original = ''.join('k%d\tv\n' % i for i in range(100)).encode()
	_touch(p, original)
	real = getattr(os, call)
	fired = []

	def interrupted(*args):
		result = real(*args)
		if not fired:
			fired.append(call)
			raise KeyboardInterrupt
		return result
	monkeypatch.setattr(os, call, interrupted)
	with pytest.raises(KeyboardInterrupt):
		TSVZ._specRewriteInPlace(p, b'x\n' * lines)
	monkeypatch.undo()
	with open(p, 'rb') as f:
		assert f.read() == original


@pytest.mark.skipif(not hasattr(os, 'posix_fallocate'), reason='needs RLIMIT_FSIZE')
def test_a_rewrite_that_fails_part_way_puts_the_old_bytes_back(tmp_path):
	# Where no space can be reserved, the write itself fails part way: the bytes it overwrote and
	# the part's size are put back.
	p = str(tmp_path / 's.tsvz')
	original = ''.join('k%d\tv\n' % i for i in range(2000)).encode()
	_touch(p, original)
	limit = len(original) + 100
	code = '\n'.join([
		'import resource, signal, sys',
		'sys.path.insert(0, sys.argv[1])',
		'import TSVZ',
		'TSVZ._reserveSpace = lambda fd, length: None  # a file system that cannot reserve space',
		'signal.signal(signal.SIGXFSZ, signal.SIG_IGN)',
		'resource.setrlimit(resource.RLIMIT_FSIZE, (int(sys.argv[3]),) * 2)',
		'try:',
		'    TSVZ._specRewriteInPlace(sys.argv[2], b"x\\n" * int(sys.argv[3]))',
		'except OSError as e:',
		'    print(e.errno)'])
	done = subprocess.run([sys.executable, '-c', code, HERE, p, str(limit)], stdout=subprocess.PIPE,
						  stderr=subprocess.PIPE, universal_newlines=True)
	assert done.stdout.strip() == str(errno.EFBIG), done.stderr
	with open(p, 'rb') as f:
		assert f.read() == original, 'the part was left half-rewritten'



def test_cli_tombstones_of_keys_ending_in_space_delete_that_key(tmp_path):
	# While stripping is on, a reader strips a tombstone's key (§9.1): a stored key `k ` (written while
	# stripping was off) must be deleted without deleting `k`.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_strip_trailing_whites_#\tfalse\nk \tv1\n#_strip_trailing_whites_#\ttrue\nk\tv2\n')
	assert _run('popitem', p, 'first')[:2] == (0, 'k \tv1\n')
	assert _run('read', p)[:2] == (0, 'k\tv2\n')
	m = str(tmp_path / 'm.tsvz')
	_touch(m, b'#_strip_trailing_whites_#\tfalse\nk \tv1\n#_strip_trailing_whites_#\ttrue\n')
	_touch(m + '.1', b'k\tv2\n')
	assert _run('clear', m)[0] == 0
	assert _run('read', m)[:2] == (0, '')


def test_only_a_tombstone_stripping_would_change_is_guarded():
	# Spec §9.6, §10.2: in .tsvz a key's trailing tab is written <sep>, which stripping leaves alone.
	guard = [(['#_strip_trailing_whites_#', 'false'], True), (['k '], False), (['#_strip_trailing_whites_#', 'true'], True)]
	assert TSVZ._specTombstones(['k '], '\t', True) == guard
	assert TSVZ._specTombstones(['k '], '\t', False) == [(['k '], False)]
	assert TSVZ._specTombstones(['k\t'], '\t', True) == [(['k\t'], False)]
	assert TSVZ._specTombstones(['k\t'], ',', True)[0] == guard[0]
	assert TSVZ._specTombstoneLines('k ', '\t', True) == '#_strip_trailing_whites_#\tfalse\nk \n#_strip_trailing_whites_#\ttrue'


def test_a_requested_key_names_the_live_key_equal_to_it():
	# Spec §20.2.7: while stripping is on, the live key equal to a KEY wins; else the key a reader resolves it to.
	assert TSVZ._specNameKey('k ', '\t', True, {'k ': 1, 'k': 2}) == 'k '
	assert TSVZ._specNameKey('k ', '\t', True, {'k': 2}) == 'k'
	assert TSVZ._specNameKey('k \t ', ',', True, {}) == 'k'
	assert TSVZ._specNameKey('k\t', '\t', True, {}) == 'k\t'  # written k<sep>: not stripped
	assert TSVZ._specNameKey('#h <', '\t', True, {}) == '#h <'  # encoded <#>h <lt>: nothing to strip
	assert TSVZ._specNameKey('#h ', '\t', True, {}) == '#h'  # encoded <#>h<space>, decoded again
	assert TSVZ._specNameKey('k ', '\t', False, {}) == 'k '


def test_delete_rows_name_keys_before_the_batch_and_guard_only_what_stripping_changes():
	off, on = (['#_strip_trailing_whites_#', 'false'], True), (['#_strip_trailing_whites_#', 'true'], True)
	reset = (['#_Strip_Trailing_Whites_#'], True)
	live = {'k ': 1, 'k': 2}
	# Both name k<space> (the store before the batch), inside one guard; a plain key joins it unchanged.
	assert TSVZ._specDeleteRows([(['k '], False), (['a'], False), (['k '], False)], '\t', True, live) == \
		[off, (['k '], False), (['a'], False), (['k '], False), on]
	assert TSVZ._specDeleteRows([(['x '], False)], '\t', True, live) == [(['x'], False)]
	# Stripping off before the batch: k<space> names itself, and needs a guard only after a reset turns stripping on.
	assert TSVZ._specDeleteRows([(['k '], False), reset, (['k '], False)], '\t', False, {}) == \
		[(['k '], False), reset, off, (['k '], False), on]
	# A reset inside a guard closes it; the next key that needs one opens another.
	assert TSVZ._specDeleteRows([(['k '], False), reset, (['k '], False)], '\t', True, live) == \
		[off, (['k '], False), reset, off, (['k '], False), on]
	assert not TSVZ._specNamesKeys([(['k\t'], False), (['#_x_# '], True)], '\t')
	assert TSVZ._specNamesKeys([(['k\t'], False)], ',')


def test_cli_delete_names_keys_and_refuses_a_store_it_cannot_read(tmp_path):
	# Spec §20.2.7: naming 'k ' reads the store; a read stopped at damaged data writes nothing and exits 1.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_strip_trailing_whites_#\tfalse\nk \tv1\n#_strip_trailing_whites_#\ttrue\nk\tv2\n')
	assert _run('keys', p)[:2] == (0, 'k \nk\n')
	assert _run('delete', p, '-', stdin='k \nk\n')[0] == 0
	assert _run('read', p)[:2] == (0, '')
	assert _run('set', p, 'k', 'v3')[0] == 0
	assert _run('read', p)[:2] == (0, 'k\tv3\n'), 'stripping is on again after the guard'
	damaged = str(tmp_path / 'd.tsvz')
	original = b'#_on_corruption_#\tstop\nk\t1\n\x00\x00c\t3\n'
	_touch(damaged, original)
	assert _run('delete', damaged, 'k ')[0] == 1
	with open(damaged, 'rb') as f:
		assert f.read() == original
	assert _run('delete', damaged, 'k')[0] == 0, 'a key that names itself needs no read'


@pytest.mark.parametrize('make', [lambda p: TSVZ.TSVZed(p, rewrite_on_load=False), TSVZ.TSVZedLite],
						 ids=['TSVZed', 'TSVZedLite'])
def test_classes_pop_a_key_ending_in_space_by_its_own_tombstone(tmp_path, make):
	# Spec §9.6, through the library classes.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_strip_trailing_whites_#\tfalse\nk \tv1\n#_strip_trailing_whites_#\ttrue\nk\tv2\n')
	t = make(p)
	assert t.popitem(last=False) == ('k ', ['k ', 'v1'])
	t.close()
	assert dict(TSVZ.readTabularFile(p)) == {'k': ['k', 'v2']}


@pytest.mark.parametrize('make', [lambda p: TSVZ.TSVZed(p, rewrite_on_load=False), TSVZ.TSVZedLite,
								  lambda p: TSVZ.TSVZClient(p)], ids=['TSVZed', 'TSVZedLite', 'TSVZClient'])
def test_classes_name_the_live_key_equal_to_a_requested_key(tmp_path, make):
	# Spec §20.2.7: a lookup names the live key equal to it, else the key right-stripped; setting still strips.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_strip_trailing_whites_#\tfalse\nk \tv1\n#_strip_trailing_whites_#\ttrue\nk\tv2\nx\tv3\n')
	t = make(p)
	try:
		assert t['k '] == ['k ', 'v1'] and t['k'] == ['k', 'v2'] and t['x  '] == ['x', 'v3']
		assert 'k ' in t and t.get('k ') == ['k ', 'v1']
		assert 'x  ' in t and t.get('x  ') == ['x', 'v3'] and 'y ' not in t and t.get('y ') is None
		assert t.setdefault('k ', ['z']) == ['k ', 'v1']
		assert t.setdefault('x  ', ['z']) == ['x', 'v3'], 'setdefault must not overwrite the key it names'
		assert t.pop('k ') == ['k ', 'v1']
		assert list(t.keys()) == ['k', 'x']
		t['n '] = ['v4']
		assert list(t.keys()) == ['k', 'x', 'n']
		for key in list(t.keys()):
			del t[key]
		assert len(t) == 0
	finally:
		t.close()
	assert dict(TSVZ.readTabularFile(p)) == {}


def test_client_does_not_resend_a_delete_that_could_name_another_key(tmp_path, monkeypatch):
	# Spec §20.2.7: once k<space> is deleted, a resent `delete 'k '` would name, and delete, k.
	sent = []
	for name in ('s.tsvz', 's.tsv'):
		c = TSVZ.TSVZClient(str(tmp_path / name))
		monkeypatch.setattr(c, '_request', lambda fields, write=False, bulk=None, retry=True: sent.append((fields, retry)))
		del c['k ']
		del c['k\t']  # .tsvz writes k<sep>, never stripped
		del c['k']
		c.close()
	assert [retry for fields, retry in sent] == [False, True, True, True, True, True]


@pytest.mark.parametrize('name', ['s.tsvz', 's.tsv'])
@pytest.mark.parametrize('make', [lambda p: TSVZ.TSVZed(p, rewrite_on_load=False), TSVZ.TSVZedLite],
						 ids=['TSVZed', 'TSVZedLite'])
def test_classes_name_keys_by_their_str(tmp_path, make, name):
	# A key is its str(), as __setitem__ stores it: a (str, Enum) member is not looked up by its value.
	import enum

	class Color(str, enum.Enum):
		RED = 'red'

	p = str(tmp_path / name)
	t = make(p)
	try:
		t['red'] = ['r']
		t[Color.RED] = ['c']
		assert t[Color.RED][:2] == [str(Color.RED), 'c'] and Color.RED in t and t.get(Color.RED)[1] == 'c'
		del t[Color.RED]
		assert Color.RED not in t and list(t.keys()) == ['red']
		assert t.pop(['a'], None) is None and ['a'] not in t  # unhashable keys are named by their str() too
	finally:
		t.close()


def test_tsvzed_pop_writes_one_tombstone(tmp_path):
	# Before Python 3.11 OrderedDict.pop deletes through del self[key], which writes a tombstone of its own.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_strip_trailing_whites_#\tfalse\nk \tv1\n#_strip_trailing_whites_#\ttrue\nx\tv2\n')
	t = TSVZ.TSVZed(p, rewrite_on_load=False)
	try:
		assert t.pop('x') == ['x', 'v2'] and t.pop('k ') == ['k ', 'v1']
	finally:
		t.close()
	with open(p, 'rb') as f:
		assert f.read().split(b'\n')[4:] == [b'x', b'#_strip_trailing_whites_#\tfalse', b'k ',
											 b'#_strip_trailing_whites_#\ttrue', b'']


def test_tsvzed_lookups_by_key_iterate_and_delete_every_key(tmp_path):
	# The bug §20.2.7 fixes in the classes: deleting each key by name deleted k twice and left k<space>.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_strip_trailing_whites_#\tfalse\nk \tv1\n#_strip_trailing_whites_#\ttrue\nk\tv2\n')
	t = TSVZ.TSVZed(p, rewrite_on_load=False)
	try:
		for key in list(t):
			del t[key]
		assert dict(t) == {}
	finally:
		t.close()
	assert dict(TSVZ.readTabularFile(p)) == {}


def test_engine_stops_following_at_a_zero_filled_line_under_stop(tmp_path):
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_on_corruption_#\tstop\na\t1\n')
	store = TSVZ._ServeStore(p, create=True)
	with open(p, 'ab') as f:
		f.write(b'\0\0b\t2\nc\t3\n')
	store.sync(None)
	assert store.unreadable and store.read(None) == [['a', '1']]
	store.close()


def test_lite_reads_the_record_after_zero_fill_by_its_offset(tmp_path):
	# TSVZedLite reads rows again from their offsets: the offset is that of the bytes after the zeros.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'a\t1\n\0\0\0c\t3\n')
	lite = TSVZ.TSVZedLite(p)
	try:
		assert lite['c'] == ['c', '3'] and lite['a'] == ['a', '1']
	finally:
		lite.close()


def test_a_zero_byte_in_a_value_round_trips_as_a_token(tmp_path):
	p = str(tmp_path / 's.tsvz')
	t = TSVZ.TSVZed(p)
	t['k'] = ['k', 'x\0y']
	t.close()
	with open(p, 'rb') as f:
		assert b'x<NUL>y' in f.read()
	assert TSVZ.readTabularFile(p)['k'] == ['k', 'x\0y']


@pytest.mark.parametrize('length', [5000, 0xff00, 0xfffe, 0xffff])
def test_spec_gzip_unpad_keeps_every_zero_byte_the_compressed_data_needs(length):
	# A final stored block of zero bytes, cut before its trailer: the zeros after its last nonzero byte are its
	# own, up to 2 + 65535 of them when LEN is 65535 (NLEN is then zero); the zeros after it are padding (§16.6).
	header = b'\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff'
	stored = b'\x01' + length.to_bytes(2, 'little') + (0xffff - length).to_bytes(2, 'little') + b'\0' * length
	region = header + stored + b'\0' * 100  # no trailer: cut short, then padding
	assert len(TSVZ._gzipUnpad(region)) == len(header + stored)
	import tsvzconform
	assert tsvzconform._gzip_unpad(region) == TSVZ._gzipUnpad(region)


def test_spec_decode_alone_tells_a_cut_member_from_a_corrupt_one():
	member = gzip.compress(b''.join(b'k%05d\tv\n' % i for i in range(3000)))
	cutShort, out, error = TSVZ._decodeAlone(member[:-8], 'gz')
	assert cutShort and error is None and out.endswith(b'k02999\tv\n')
	cutShort, out, error = TSVZ._decodeAlone(_crc_only_flip(member), 'gz')
	assert not cutShort and 'incorrect data check' in error and out.endswith(b'k02999\tv\n')


def test_engine_reloads_a_stopped_store_that_was_rewritten(tmp_path):
	# Reading stopped at the zero-filled line, so a rewrite of the bytes before it must be noticed.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_on_corruption_#\tstop\na\t1\nb\t2\n\0\0\0c\t3\nd\t4\n')
	store = TSVZ._ServeStore(p, create=True)
	assert store.stopped and store.offset > 0
	TSVZ.scrubTabularFile(p)  # in place, same file: what was read, without the damage
	store.sync(None)
	assert not store.stopped and store.read(None) == [['a', '1'], ['b', '2']]
	store.close()


def test_lite_offset_after_a_byte_order_mark_and_zero_fill(tmp_path):
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'\xef\xbb\xbf\0\0\0\0a\t1\n')
	lite = TSVZ.TSVZedLite(p)
	try:
		assert lite['a'] == ['a', '1']
	finally:
		lite.close()


def test_marker_values_fold_ascii_case_only(tmp_path):
	# KELVIN SIGN lowercases to k under Unicode rules; spec §12.4.1 folds ASCII only, so 'sKip' is no policy.
	p = str(tmp_path / 's.tsvz')
	_touch(p, '#_on_corruption_#\ts\u212aip\na\t1\n\0\0c\t3\n'.encode('utf-8'))
	assert set(TSVZ.readTabularFile(p)) == {'a', 'c'}


def test_spec_a_byte_order_mark_after_dropped_bytes_is_data(tmp_path):
	# Spec §4.7: only a mark at the start of a part's bytes is removed.
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, gzip.compress(b'abc')[:-8] + gzip.compress(b'\xef\xbb\xbfk\t1\n'))
	assert list(TSVZ.readTabularFile(p)) == ['\ufeffk']


def test_engine_reloads_a_stopped_store_scrubbed_down_to_the_stop(tmp_path):
	# The scrub leaves exactly the bytes before the stop: same size as the follower's offset, but a rewrite.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_version_#\t1\n#_on_corruption_#\tstop\na\t1\nb\t2\n\0\0\0c\t3\nd\t4\n')
	store = TSVZ._ServeStore(p, create=True)
	TSVZ.scrubTabularFile(p)
	with open(p, 'ab') as f:
		f.write(b'e\t5\n')
	store.sync(None)
	assert not store.stopped and store.read(None) == [['a', '1'], ['b', '2'], ['e', '5']]
	store.close()


def test_cli_scrub_of_a_stopped_store_says_it_rewrote(tmp_path):
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_on_corruption_#\tstop\na\t1\n\0\0c\t3\nd\t4\n')
	status, _, err = _run('scrub', p)
	assert status == 1 and 'rewritten' in err and 'nothing was written' not in err


def test_spec_a_zero_fill_stop_still_reports_damaged_members(tmp_path, capsys):
	bad = _crc_only_flip(gzip.compress(b''.join(b'k%04d\tv\n' % i for i in range(500))))
	p = str(tmp_path / 'm.tsvz.gz')
	_touch(p, gzip.compress(b'a\t1\n') + bad + gzip.compress(b'#_on_corruption_#\tstop\n\0\0z\t9\n'))
	TSVZ.readTabularFile(p)
	err = capsys.readouterr().err
	assert 'not verified' in err and 'stopped' in err


def test_engine_follower_removes_a_byte_order_mark_only_at_the_part_start(tmp_path):
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'\xef\xbb\xbf')
	store = TSVZ._ServeStore(p, create=True)
	with open(p, 'ab') as f:
		f.write(b'\xef\xbb\xbfk\t1\n')
	store.sync(None)
	assert store.read(None) == [['\ufeffk', '1']] == list(TSVZ.readTabularFile(p).values())
	store.close()


def test_spec_lite_clear_raises_when_the_clear_is_refused(tmp_path, monkeypatch):
	# A refused clear leaves the store as it was; the caller is told, and memory still matches the store.
	_block_zstd(monkeypatch)
	base = str(tmp_path / 'm.tsvz')
	_touch(base + '.1.zst', ZST_JUNK)
	_touch(base + '.2', b'b\t2\n')
	lite = TSVZ.TSVZedLite(base)
	try:
		with pytest.raises(OSError, match='not cleared'):
			lite.clear()
		assert dict(lite) == {'b': ['b', '2']}
	finally:
		lite.close()
	with open(base + '.2', 'rb') as f:
		assert f.read() == b'b\t2\n'


@pytest.mark.parametrize('operation', ['clear', 'scrub'])
def test_spec_clear_and_scrub_retry_when_a_part_vanishes_during_the_read(tmp_path, monkeypatch, operation):
	# A part removed between listing and reading (a rotation) changes the store: read again, do not refuse.
	base = str(tmp_path / 'm.tsvz')
	_touch(base, b'a\t1\n')
	_touch(base + '.1', b'b\t2\n')
	original = TSVZ._iterPartLines
	gone = []

	def vanishing(info, reporter, *args):
		if info.path.endswith('.1') and not gone:
			gone.append(info.path)
			os.remove(info.path)
		return original(info, reporter, *args)

	monkeypatch.setattr(TSVZ, '_iterPartLines', vanishing)
	if operation == 'clear':
		assert TSVZ._specClearTabularFile(base, delimiter='\t') is True
	else:
		assert TSVZ._specScrub(base, delimiter='\t')[1] is True
	assert gone


def test_spec_non_strict_creation_in_a_missing_codec_returns_instead_of_raising(tmp_path, monkeypatch):
	_block_zstd(monkeypatch)
	new = str(tmp_path / 'new.tsvz.zst')
	assert TSVZ.readTabularFile(new, createIfNotExist=True, strict=False) == {}
	assert not os.path.exists(new)


def test_spec_checksums_arm_verify_and_span_parts(tmp_path, capsys):
	import zlib
	base = str(tmp_path / 'm.tsvz')
	body = b'b\t2\n'
	crc = '%08x' % (zlib.crc32(b'a\t1\r\n' + body) & 0xffffffff)
	_touch(base, b'#_checksum_crc32_#\tIGNORED\na\t1\r\n')
	_touch(base + '.f', body + ('#_checksum_crc32_#\t' + crc.upper() + '\n').encode()
		   + b'#_checksum_crc32_#\tdeadbeef\n#_checksum_nope_#\t1\n')
	data, _, _ = _replay([base, base + '.f'])
	assert dict(data) == {'a': ['a', '1'], 'b': ['b', '2']}
	warnings = _tsvz_warnings(capsys)
	assert len(warnings) == 1
	assert 'mismatch: expected deadbeef, computed 00000000' in warnings[0] and 'm.tsvz.f line 3' in warnings[0]


def test_spec_marker_values_are_not_stripped(capsys):
	# Marker lines are not stripped (spec §10.4), so a value with a space around it is invalid and ignored (§12.4.5).
	_, state, reporter = _process(['#_strip_trailing_whites_#\tfalse ', '#_fill_empty_with_default_#\t true'])
	reporter.flush()
	assert state.strip is True and state.fillEmpty is False
	assert 'invalid value' in _tsvz_warnings(capsys)[0]


def test_spec_a_checksum_value_with_a_space_is_a_mismatch(tmp_path, capsys):
	import zlib
	p = str(tmp_path / 'a.tsvz')
	crc = '%08x' % (zlib.crc32(b'a\t1\n') & 0xffffffff)
	_touch(p, ('#_checksum_crc32_#\na\t1\n#_checksum_crc32_#\t%s \n' % crc).encode())
	_replay([p])
	warnings = _tsvz_warnings(capsys)
	assert len(warnings) == 1 and 'mismatch: expected %s , computed %s' % (crc, crc) in warnings[0]


def test_spec_checksum_covers_other_algorithms_markers(tmp_path, capsys):
	import zlib
	p = str(tmp_path / 'a.tsvz')
	sha_line = b'#_checksum_sha256_#\n'
	crc = '%08x' % (zlib.crc32(sha_line + b'a\t1\n') & 0xffffffff)
	_touch(p, b'#_checksum_crc32_#\n' + sha_line + b'a\t1\n' + ('#_checksum_crc32_#\t%s\n' % crc).encode())
	_replay([p])
	assert _tsvz_warnings(capsys) == []


def test_store_parts_order_and_exclusions(tmp_path, capsys):
	base = str(tmp_path / 'm.tsvz')
	for name in ['m.tsvz', 'm.tsvz.10', 'm.tsvz.f', 'm.tsvz.0', 'm.tsvz.11.rotated', 'm.tsvz.12.rotated.gz',
				 'm.tsvz.zz', 'other.tsvz.1', 'm.tsv.2']:
		_touch(str(tmp_path / name))
	reporter = TSVZ._Reporter(base)
	parts, active = TSVZ._storeParts(base, reporter)
	assert [os.path.basename(p) for p in parts] == ['m.tsvz', 'm.tsvz.0', 'm.tsvz.f', 'm.tsvz.10']
	assert os.path.basename(active) == 'm.tsvz.10'
	reporter.flush()
	assert _tsvz_warnings(capsys) == []


def test_store_parts_without_part_zero_and_ties(tmp_path, capsys):
	base = str(tmp_path / 'm.tsvz')
	_touch(base + '.1a')
	_touch(base + '.1A.gz')
	reporter = TSVZ._Reporter(base)
	parts, active = TSVZ._storeParts(base, reporter)
	assert [os.path.basename(p) for p in parts] == ['m.tsvz.1A.gz', 'm.tsvz.1a']
	assert active == parts[-1]
	reporter.flush()
	assert len(_tsvz_warnings(capsys)) == 1


def test_store_parts_ambiguous_part_zero(tmp_path, capsys):
	base = str(tmp_path / 'm.tsvz')
	_touch(base)
	_touch(base + '.gz')
	reporter = TSVZ._Reporter(base)
	assert TSVZ._storeParts(base, reporter) == ([base], base)
	reporter.flush()
	assert len(_tsvz_warnings(capsys)) == 1


def test_store_parts_fragment_and_missing(tmp_path, capsys):
	base = str(tmp_path / 'm.tsvz')
	_touch(base)
	_touch(base + '.1')
	reporter = TSVZ._Reporter(base + '.1')
	assert TSVZ._storeParts(base + '.1', reporter) == ([base + '.1'], base + '.1')
	reporter.flush()
	assert 'one part of a multi-part store' in _tsvz_warnings(capsys)[0]
	missing = str(tmp_path / 'none.tsvz')
	assert TSVZ._storeParts(missing) == ([], missing)


def test_spec_unreadable_part_is_skipped(tmp_path, capsys):
	base = str(tmp_path / 'm.tsvz')
	_touch(base, b'a\t1\n')
	os.mkdir(base + '.1')  # a directory named like a part cannot be read
	parts, _ = TSVZ._storeParts(base)
	data, _, _ = _replay(parts)
	assert dict(data) == {'a': ['a', '1']}
	assert 'skipped unreadable part' in _tsvz_warnings(capsys)[0]


# ==========================================================================
# Spec dialect: loading a store through readTabularFile
# ==========================================================================
APPENDIX_C = b'#_version_#\t1\n#_defaults_#\tguest\t0\nalice\tAlice\t30\nbob\tBob\ncarol\t\t25\nalice\tAlice\t31\nbob\n'


def test_spec_appendix_c_via_read_tabular_file(tmp_path):
	p = str(tmp_path / 'c.tsvz')
	_touch(p, APPENDIX_C)
	data = TSVZ.readTabularFile(p)
	assert list(data) == ['alice', 'carol']
	assert data['alice'] == ['alice', 'Alice', '31']
	assert data['carol'] == ['carol', '', '25']


def test_spec_absent_columns_use_row_bound_defaults(tmp_path):
	p = str(tmp_path / 'd.tsvz')
	_touch(p, b'a\t1\n#_defaults_#\tX\tY\tZ\nb\t2\n#_defaults_#\tQ\nc\t3\n')
	data = TSVZ.readTabularFile(p)
	assert data['a'] == ['a', '1']
	assert data['b'] == ['b', '2', 'Y', 'Z']
	assert data['c'] == ['c', '3']


def test_spec_wide_rows_are_kept_and_strict_does_not_drop(tmp_path):
	p = str(tmp_path / 'w.tsvz')
	_touch(p, b'#id\tv\na\t1\nb\t2\t3\t4\nc\n')
	assert TSVZ.readTabularFile(p, header='id\tv', strict=True) == {'a': ['a', '1'], 'b': ['b', '2', '3', '4']}


def test_spec_header_comment_after_markers(tmp_path):
	p = str(tmp_path / 'h.tsvz')
	_touch(p, b'#_version_#\t1\n#id\tv\na\t1\n')
	assert TSVZ.readTabularFile(p, header='id\tv', strict=True) == {'a': ['a', '1']}
	with pytest.raises(ValueError):
		TSVZ.readTabularFile(p, header='other\tcols', strict=True)


def test_spec_header_written_as_data_is_data(tmp_path, capsys):
	p = str(tmp_path / 'h.tsvz')
	_touch(p, b'id\tv\na\t1\n')
	assert TSVZ.readTabularFile(p, header='id\tv') == {'id': ['id', 'v'], 'a': ['a', '1']}
	assert any('not a # comment' in w for w in _tsvz_warnings(capsys))


def test_spec_read_creates_store(tmp_path):
	p = str(tmp_path / 'n.tsvz')
	assert TSVZ.readTabularFile(p, header='id\tv', createIfNotExist=True) == {}
	assert open(p, 'rb').read() == b'#id\tv\n'
	q = str(tmp_path / 'e.tsvz')
	TSVZ.readTabularFile(q, createIfNotExist=True, defaults=['NA'])
	assert open(q, 'rb').read() == b'#_defaults_#\tNA\n'
	z = str(tmp_path / 'z.tsvz.gz')
	TSVZ.readTabularFile(z, header='id\tv', createIfNotExist=True)
	assert gzip.decompress(open(z, 'rb').read()) == b'#id\tv\n'
	e = str(tmp_path / 'empty.tsvz')
	TSVZ.readTabularFile(e, createIfNotExist=True)
	assert open(e, 'rb').read() == b''
	with pytest.raises(FileNotFoundError):
		TSVZ.readTabularFile(str(tmp_path / 'missing.tsvz'))
	assert TSVZ.readTabularFile(str(tmp_path / 'missing.tsvz'), strict=False) == {}


def test_spec_conflicting_delimiter_and_encoding(tmp_path, capsys):
	p = str(tmp_path / 'c.csvz')
	_touch(p, 'k,v<sep>w\n'.encode())
	assert TSVZ.readTabularFile(p, delimiter='\t', encoding='latin-1') == {'k': ['k', 'v,w']}
	warnings = _tsvz_warnings(capsys)
	assert any("delimiter '\\t' conflicts" in w for w in warnings) and any('encoding' in w for w in warnings)
	assert TSVZ.readTabularFile(p, delimiter='comma', encoding='UTF-8') == {'k': ['k', 'v,w']}
	assert _tsvz_warnings(capsys) == []


def test_spec_defaults_argument_is_a_preamble_updated_in_place(tmp_path):
	p = str(tmp_path / 'p.tsvz')
	_touch(p, b'a\t1\n#_defaults_#\tX\tY\nb\t2\n')
	defaults = ['#_defaults_#', 'P', 'Q']
	data = TSVZ.readTabularFile(p, defaults=defaults)
	assert data['a'] == ['a', '1', 'Q'] and data['b'] == ['b', '2', 'Y']
	assert defaults == ['#_defaults_#', 'X', 'Y']


def test_spec_last_line_only_and_offsets(tmp_path):
	p = str(tmp_path / 'l.tsvz')
	_touch(p, b'#_defaults_#\tD\na\t1\nb\nb\t2\n#_defaults_#\tE\nc\n')
	assert TSVZ.readTabularFile(p, lastLineOnly=True) == ['b', '2']
	assert TSVZ.read_last_valid_line(p, {}, -1) == ['b', '2']
	b_offset = len(b'#_defaults_#\tD\na\t1\nb\n')
	assert TSVZ.readTabularFile(p, lastLineOnly=True, storeOffset=True) == b_offset
	assert TSVZ.readTabularFile(p, storeOffset=True) == {'a': len(b'#_defaults_#\tD\n'), 'b': b_offset}


def test_spec_multipart_read_and_fragment(tmp_path, capsys):
	base = str(tmp_path / 'm.tsvz')
	_touch(base, b'#_defaults_#\tD\na\t1\n')
	_touch(base + '.f', b'b\t2\n')
	_touch(base + '.10', b'a\nc\n')
	_touch(base + '.11.rotated', b'z\t9\n')
	assert TSVZ.readTabularFile(base) == {'b': ['b', '2']}
	assert TSVZ.readTabularFile(base + '.f') == {'b': ['b', '2']}
	assert any('one part of a multi-part store' in w for w in _tsvz_warnings(capsys))


def test_spec_empty_and_marker_only_stores(tmp_path):
	for content in (b'', b'#only a comment\n', b'#_defaults_#\tD\n#_version_#\t1\n'):
		p = str(tmp_path / 'e.tsvz')
		_touch(p, content)
		assert TSVZ.readTabularFile(p) == {}
		assert TSVZ.readTabularFile(p, lastLineOnly=True) == []


# ==========================================================================
# Spec dialect: appends, compressed repair, clear
# ==========================================================================
def test_spec_append_writes_rows_as_given(tmp_path):
	p = str(tmp_path / 'a.tsvz')
	TSVZ.appendLinesTabularFile(p, [['k1', 'x'], ['k2', '', ''], ['#hash', 'a<b'], ['k3'], 'k4\tv4'],
								header='id\tv\tw', createIfNotExist=True)
	assert open(p, 'rb').read() == b'#id\tv\tw\nk1\tx\nk2\t\t\n<#>hash\ta<lt>b\nk3\nk4\tv4\n'
	assert TSVZ.readTabularFile(p, header='id\tv\tw') == {
		'k1': ['k1', 'x', ''], 'k2': ['k2', '', ''], '#hash': ['#hash', 'a<b', ''], 'k4': ['k4', 'v4', '']}


def test_spec_append_dict_and_markers(tmp_path):
	p = str(tmp_path / 'a.tsvz')
	TSVZ.appendLinesTabularFile(p, OrderedDict([('k1', ['x']), ('#_defaults_#', ['D'])]), createIfNotExist=True)
	assert open(p, 'rb').read() == b'k1\tx\n#_defaults_#\tD\n'


def test_spec_append_truncates_torn_tail(tmp_path, capsys):
	p = str(tmp_path / 'a.tsvz')
	_touch(p, b'a\t1\nb\t')
	TSVZ.appendTabularFile(p, ['c', '3'])
	assert open(p, 'rb').read() == b'a\t1\nc\t3\n'
	assert _tsvz_warnings(capsys)[-1].endswith("removed uncommitted tail b'b\\t' before appending")


def test_spec_append_goes_to_active_part(tmp_path):
	base = str(tmp_path / 'm.tsvz')
	_touch(base, b'a\t1\n')
	_touch(base + '.2', b'b\t2\n')
	TSVZ.appendTabularFile(base, ['c', '3'])
	assert open(base + '.2', 'rb').read() == b'b\t2\nc\t3\n'
	assert open(base, 'rb').read() == b'a\t1\n'


def test_spec_append_compressed_adds_member(tmp_path):
	p = str(tmp_path / 'a.tsvz.gz')
	TSVZ.appendLinesTabularFile(p, [['a', '1']], createIfNotExist=True)
	TSVZ.appendLinesTabularFile(p, [['b', '2']])
	assert gzip.decompress(open(p, 'rb').read()) == b'a\t1\nb\t2\n'


def test_spec_append_header_mismatch_strict(tmp_path):
	p = str(tmp_path / 'a.tsvz')
	_touch(p, b'#id\tv\n')
	with pytest.raises(ValueError):
		TSVZ.appendTabularFile(p, ['k', 'v'], header='x\ty', strict=True)
	TSVZ.appendTabularFile(p, ['k', 'v'], header='id\tv', strict=True)
	assert open(p, 'rb').read() == b'#id\tv\nk\tv\n'


def test_spec_rewrite_in_place_keeps_inode_and_skips_identical(tmp_path):
	p = str(tmp_path / 'r.tsvz')
	_touch(p, b'a\t1\nb\t2\n')
	ino = os.stat(p).st_ino
	assert TSVZ._specRewriteInPlace(p, b'a\t1\n') is True
	assert open(p, 'rb').read() == b'a\t1\n' and os.stat(p).st_ino == ino
	assert TSVZ._specRewriteInPlace(p, b'a\t1\n') is False


def test_spec_clear_keeps_header_and_markers(tmp_path):
	p = str(tmp_path / 'c.tsvz')
	_touch(p, b'#id\tv\n#_defaults_#\tD\n#_fill_empty_with_default_#\ttrue\na\t1\n# note\nb\t2\n')
	ino = os.stat(p).st_ino
	TSVZ.clearTabularFile(p)
	assert open(p, 'rb').read() == b'#id\tv\n#_fill_empty_with_default_#\ttrue\n#_defaults_#\tD\n'
	assert os.stat(p).st_ino == ino


def test_spec_clear_multipart_appends_tombstones(tmp_path, capsys):
	base = str(tmp_path / 'm.tsvz')
	_touch(base, b'a\t1\n')
	_touch(base + '.2', b'b\t2\n')
	TSVZ.clearTabularFile(base)
	assert TSVZ.readTabularFile(base) == {}
	assert open(base, 'rb').read() == b'a\t1\n'
	assert open(base + '.2', 'rb').read() == b'b\t2\na\nb\n'
	assert any('older parts are not compacted' in w for w in _tsvz_warnings(capsys))


def test_spec_clear_refuses_a_part_declaring_a_newer_spec_version(tmp_path, capsys):
	# Clearing in place keeps the active markers, which TSVZ cannot know for version 2 (spec §19.8).
	p = str(tmp_path / 'v.tsvz')
	for content in (b'#id\tv\n#_version_#\t2\na\t1\n',
					b'#_version_#\t' + b'9' * 5000 + b'\na\t1\n',  # too long for int() on Python 3.11+
					b'#_version_#\t2\na\t1\n#_version_#\t1\nb\t2\n'):  # declared, even when set back to 1
		_touch(p, content)
		TSVZ.clearTabularFile(p)
		assert open(p, 'rb').read() == content
		assert any('clear skipped' in w and 'newer' in w for w in _tsvz_warnings(capsys))
	status, _, err = _run('clear', p)
	assert status == 1 and 'clear refused' in err
	assert open(p, 'rb').read() == content
	base = str(tmp_path / 'm.tsvz')  # a multi-part store is cleared by appending tombstones (§18.8)
	_touch(base, b'#_version_#\t2\na\t1\n')
	_touch(base + '.1', b'b\t2\n')
	assert _run('clear', base)[0] == 0
	assert open(base, 'rb').read() == b'#_version_#\t2\na\t1\n' and open(base + '.1', 'rb').read() == b'b\t2\na\nb\n'


def test_spec_clear_creates_missing_and_empty_files(tmp_path):
	p = str(tmp_path / 'n.tsvz')
	TSVZ.clearTabularFile(p, header='id\tv')
	assert open(p, 'rb').read() == b'#id\tv\n'
	e = str(tmp_path / 'e.tsvz')
	_touch(e)
	TSVZ.clearTabularFile(e)
	assert open(e, 'rb').read() == b''



# ==========================================================================
# Spec dialect: archival scrub (design §5.7)
# ==========================================================================
def test_spec_scrub_compacts_in_place(tmp_path):
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#id\tv\n#_defaults_#\tD\na\t1\nb\t2\n# comment\na\t3\nb\n#__tool__#\tmeta\n#_checksum_crc32_#\n')
	ino = os.stat(p).st_ino
	before = TSVZ.readTabularFile(p, header='id\tv')
	assert TSVZ.scrubTabularFile(p, header='id\tv') == before
	assert open(p, 'rb').read() == b'#id\tv\n#_version_#\t1\n#_defaults_#\tD\na\t3\n'
	assert os.stat(p).st_ino == ino
	assert TSVZ.readTabularFile(p, header='id\tv') == before
	mtime = os.stat(p).st_mtime_ns
	TSVZ.scrubTabularFile(p, header='id\tv')
	assert os.stat(p).st_mtime_ns == mtime  # identical content is not rewritten


def test_spec_scrub_fidelity_when_markers_changed(tmp_path):
	p = str(tmp_path / 'f.tsvz')
	_touch(p, b'#id\tname\tage\n#_strip_trailing_whites_#\tfalse\na\tx  \t1\n#_strip_trailing_whites_#\ttrue\n'
			  b'b\tq\n#_defaults_#\tD1\tD2\tD3\n#_fill_empty_with_default_#\ttrue\nc\t\t5\n')
	before = TSVZ.readTabularFile(p, header='id\tname\tage')
	TSVZ.scrubTabularFile(p, header='id\tname\tage')
	after = TSVZ.readTabularFile(p, header='id\tname\tage')
	assert list(after) == list(before)
	for key, row in before.items():
		assert after[key][:len(row)] == row and not any(after[key][len(row):]), (key, row, after[key])
	text = open(p, 'rb').read()
	assert text.index(b'#_fill_empty_with_default_#\tfalse') < text.index(b'a\tx  \t1') < text.index(b'#_fill_empty_with_default_#\ttrue')
	load = TSVZ._specLoad(p, '\t')
	assert load.state.strip and load.state.fillEmpty and load.state.defaults == ['#_defaults_#', 'D1', 'D2', 'D3']


def test_spec_scrub_keeps_marker_like_data_keys_as_data(tmp_path):
	p = str(tmp_path / 'k.tsvz')
	_touch(p, b'<#>_defaults_#\tdata\n<#>plain\tv\n')
	TSVZ.scrubTabularFile(p)
	assert open(p, 'rb').read() == b'#_version_#\t1\n<#>_defaults_#\tdata\n<#>plain\tv\n'
	assert TSVZ.readTabularFile(p) == {'#_defaults_#': ['#_defaults_#', 'data'], '#plain': ['#plain', 'v']}


def test_spec_scrub_refuses_multipart_and_fragments(tmp_path, capsys):
	base = str(tmp_path / 'm.tsvz')
	_touch(base, b'a\t1\na\n')
	_touch(base + '.2', b'b\t2\n')
	assert TSVZ.scrubTabularFile(base) == {'b': ['b', '2']}
	assert TSVZ.scrubTabularFile(base + '.2') == {'b': ['b', '2']}
	assert open(base, 'rb').read() == b'a\t1\na\n' and open(base + '.2', 'rb').read() == b'b\t2\n'
	assert sum('scrub skipped' in w for w in _tsvz_warnings(capsys)) == 2


def test_spec_scrub_refuses_a_store_declaring_a_newer_spec_version(tmp_path, capsys):
	# TSVZ reads version-2 lines as version 1 (spec §6.3): a rewrite would make that reading permanent.
	p = str(tmp_path / 'v.tsvz')
	for content in (b'#_version_#\t2\na\t1\na\t2\n',
					b'#_version_#\t2\na\t1\n#_version_#\t1\na\t2\n'):  # declared, even when set back to 1
		_touch(p, content)
		assert TSVZ.scrubTabularFile(p) == {'a': ['a', '2']}
		assert open(p, 'rb').read() == content
		assert any('scrub skipped' in w and 'newer' in w for w in _tsvz_warnings(capsys))
	status, _, err = _run('scrub', p)
	assert status == 1 and 'scrub refused' in err
	assert open(p, 'rb').read() == content
	_touch(p, b'#_version_#\t' + b'9' * 5000 + b'\na\t1\n')  # too long for int() on Python 3.11+, still a version
	assert TSVZ.scrubTabularFile(p) == {'a': ['a', '1']}
	assert open(p, 'rb').read() == b'#_version_#\t' + b'9' * 5000 + b'\na\t1\n'
	assert any('scrub skipped' in w and 'newer' in w for w in _tsvz_warnings(capsys))
	_touch(p, b'#_version_#\t1\na\t1\na\t2\n')  # version 1, declared: compacted
	TSVZ.scrubTabularFile(p)
	assert open(p, 'rb').read() == b'#_version_#\t1\na\t2\n'


def test_spec_scrub_compressed_in_place(tmp_path):
	p = str(tmp_path / 's.tsvz.gz')
	_touch(p, gzip.compress(b'a\t1\n') + gzip.compress(b'a\t2\nb\t3\nb\n'))
	ino = os.stat(p).st_ino
	TSVZ.scrubTabularFile(p)
	assert gzip.decompress(open(p, 'rb').read()) == b'#_version_#\t1\na\t2\n'
	assert os.stat(p).st_ino == ino


def test_spec_scrub_empty_stores(tmp_path):
	for content in (b'', b'#id\tv\n', b'a\t1\na\n'):
		p = str(tmp_path / 'e.tsvz')
		_touch(p, content)
		assert TSVZ.scrubTabularFile(p) == {}
		assert TSVZ.readTabularFile(p) == {}


# ==========================================================================
# TSVZed on spec stores: loading and reading (design §5.6, §6)
# ==========================================================================
def _wait_for(predicate, timeout=3.0):
	deadline = time.time() + timeout
	while not predicate() and time.time() < deadline:
		time.sleep(0.01)
	return predicate()


def test_tsvzed_spec_loads_without_rewriting(tmp_path):
	p = str(tmp_path / 'c.tsvz')
	_touch(p, APPENDIX_C)
	mtime = os.stat(p).st_mtime_ns
	t = TSVZ.TSVZed(p)
	assert t.dialect == 'tsvz'
	assert list(t) == ['alice', 'carol'] and t['alice'] == ['alice', 'Alice', '31']
	assert t.defaults == ['#_defaults_#', 'guest', '0']
	t.close()
	assert open(p, 'rb').read() == APPENDIX_C and os.stat(p).st_mtime_ns == mtime


def test_tsvzed_dialect_attribute_for_tsv(tmp_path):
	t = TSVZ.TSVZed(str(tmp_path / 'a.tsv'))
	assert t.dialect == 'tsv'
	t.close()


def test_tsvzed_spec_missing_key_returns_defaults(tmp_path):
	p = str(tmp_path / 'c.tsvz')
	_touch(p, APPENDIX_C)
	t = TSVZ.TSVZed(p)
	assert t['bob'] == ['bob', 'guest', '0']
	assert 'bob' not in t and t.get('bob') is None and t.get('bob', 7) == 7
	assert t.pop('bob', 'x') == 'x'
	t.close()
	_touch(p, APPENDIX_C + b'#_return_defaults_when_missing_#\tfalse\n')
	t = TSVZ.TSVZed(p)
	with pytest.raises(KeyError):
		t['bob']
	t.close()


def test_tsvzed_spec_empty_stores(tmp_path):
	t = TSVZ.TSVZed(str(tmp_path / 'e.tsvz'), header='id\ta\tb')
	assert dict(t) == {} and t['nobody'] == ['nobody', '', '']
	t.close()
	for content in (b'', b'#id\tv\n', b'#_defaults_#\tD\n'):
		p = str(tmp_path / 'm.tsvz')
		_touch(p, content)
		t = TSVZ.TSVZed(p)
		assert dict(t) == {}
		t.close()


def test_tsvzed_legacy_missing_key_still_raises(tmp_path):
	t = TSVZ.TSVZed(str(tmp_path / 'a.tsv'))
	with pytest.raises(KeyError):
		t['nobody']
	t.close()


def test_tsvzed_spec_rewrite_features_warn_and_do_nothing(tmp_path, capsys):
	p = str(tmp_path / 'r.tsvz')
	_touch(p, b'a\t1\na\t2\n')
	t = TSVZ.TSVZed(p, rewrite_on_load=True, rewrite_on_exit=True, rewrite_interval=5)
	assert t.rewrite(force=True) is False
	t.mapToFile()
	t.hardMapToFile()
	t.close()
	assert open(p, 'rb').read() == b'a\t1\na\t2\n'
	warnings = _tsvz_warnings(capsys)
	assert len(warnings) == 6 and all('ignored for .tsvz' in w for w in warnings)


def test_tsvzed_spec_default_constructor_is_silent(tmp_path, capsys):
	t = TSVZ.TSVZed(str(tmp_path / 'q.tsvz'))
	t.close()
	assert _tsvz_warnings(capsys) == []


def test_tsvzed_spec_move_to_end_is_memory_only(tmp_path, capsys):
	p = str(tmp_path / 'o.tsvz')
	_touch(p, b'a\t1\nb\t2\n')
	t = TSVZ.TSVZed(p)
	t.move_to_end('a')
	assert list(t) == ['b', 'a'] and t.rewrite_on_exit is False
	t.close()
	assert open(p, 'rb').read() == b'a\t1\nb\t2\n'
	assert any('move_to_end only reorders memory' in w for w in _tsvz_warnings(capsys))


def test_tsvzed_spec_reloads_on_external_append(tmp_path):
	p = str(tmp_path / 'x.tsvz')
	_touch(p, b'a\t1\n')
	t = TSVZ.TSVZed(p, append_check_delay=0.01)
	time.sleep(0.05)
	with open(p, 'ab') as f:
		f.write(b'b\t2\n')
	st = os.stat(p)
	os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
	assert _wait_for(lambda: 'b' in t)
	assert t['b'] == ['b', '2']
	t.close()


def test_tsvzed_spec_reloads_when_a_new_part_appears(tmp_path):
	base = str(tmp_path / 'm.tsvz')
	_touch(base, b'a\t1\n')
	t = TSVZ.TSVZed(base, append_check_delay=0.01)
	time.sleep(0.05)
	_touch(base + '.5', b'b\t2\n')
	assert _wait_for(lambda: 'b' in t)
	assert t._activePath == base + '.5'
	t.close()


def test_tsvzed_spec_header_and_defaults_arguments(tmp_path):
	p = str(tmp_path / 'h.tsvz')
	_touch(p, b'#id\tname\tage\nk\tv\n')
	t = TSVZ.TSVZed(p, header='id\tname\tage', defaults=['x', 'y'])
	assert t.correctColumnNum == 3 and t['k'] == ['k', 'v', 'y']
	t.close()


# ==========================================================================
# TSVZed on spec stores: writing (design §5.2–§5.5, §5.8)
# ==========================================================================
def test_tsvzed_spec_writes_rows_tombstones_and_hash_keys(tmp_path):
	p = str(tmp_path / 'w.tsvz')
	t = TSVZ.TSVZed(p, header='id\ta\tb')
	t['k1'] = ['k1', 'x']
	t['k2'] = ['k2', '', '']
	t['#hash'] = 'v<1>'
	t['k3'] = 'k3\ty\tz'
	del t['k3']
	t['k1'] = 'k1'
	assert dict(t) == {'k2': ['k2', '', ''], '#hash': ['#hash', 'v<1>', '']}
	t.close()
	assert open(p, 'rb').read() == b'#id\ta\tb\nk1\tx\nk2\t\t\n<#>hash\tv<lt>1>\nk3\ty\tz\nk3\nk1\n'
	t = TSVZ.TSVZed(p, header='id\ta\tb')
	assert dict(t) == {'k2': ['k2', '', ''], '#hash': ['#hash', 'v<1>', '']}
	t.close()


def test_tsvzed_spec_markers_through_the_api(tmp_path):
	p = str(tmp_path / 'm.tsvz')
	t = TSVZ.TSVZed(p)
	t['#_defaults_#'] = ['G', '0']
	t['a'] = ['a', '', '']
	t['#_return_defaults_when_missing_#'] = 'false'
	t['#__tool__#'] = 'meta'
	t['#_fill_empty_with_default_#'] = 'maybe'  # rejected: not written
	assert t['a'] == ['a', 'G', '0'] and '#__tool__#' not in t
	with pytest.raises(KeyError):
		t['missing']
	del t['#_defaults_#']
	assert t.defaults == ['#_defaults_#']
	t.close()
	assert open(p, 'rb').read() == (b'#_defaults_#\tG\t0\na\tG\t0\n#_return_defaults_when_missing_#\tfalse\n'
									b'#__tool__#\tmeta\n#_defaults_#\n')


def test_tsvzed_spec_wide_values_are_kept(tmp_path):
	p = str(tmp_path / 'w.tsvz')
	t = TSVZ.TSVZed(p, header='id\tv')
	t['k'] = ['k', '1', 'extra']
	assert t['k'] == ['k', '1', 'extra']
	t.close()
	assert TSVZ.readTabularFile(p, header='id\tv') == {'k': ['k', '1', 'extra']}
	s = TSVZ.TSVZed(p, header='id\tv', strict=True)
	s['j'] = ['j', '1', 'extra']  # strict keeps 3.39's refusal
	assert 'j' not in s
	s.close()


def test_tsvzed_spec_constructor_defaults_are_persisted_once(tmp_path):
	p = str(tmp_path / 'd.tsvz')
	_touch(p, b'a\t1\n')
	for _ in range(2):
		t = TSVZ.TSVZed(p, defaults=['X', 'Y'])
		t.close()
	assert open(p, 'rb').read() == b'a\t1\n#_defaults_#\tX\tY\n'
	n = str(tmp_path / 'n.tsvz')
	t = TSVZ.TSVZed(n, header='id\tv', defaults=['X'])
	t.close()
	assert open(n, 'rb').read() == b'#id\tv\n#_defaults_#\tX\n'


def test_tsvzed_spec_append_truncates_torn_tail(tmp_path, capsys):
	p = str(tmp_path / 't.tsvz')
	_touch(p, b'a\t1\nhalf')
	t = TSVZ.TSVZed(p)
	t['b'] = ['b', '2']
	t.close()
	assert open(p, 'rb').read() == b'a\t1\nb\t2\n'
	warnings = _tsvz_warnings(capsys)
	assert any('ignored uncommitted bytes' in w for w in warnings)
	assert any("removed uncommitted tail b'half'" in w for w in warnings)


@pytest.mark.parametrize('cls', ['TSVZed', 'TSVZedLite'])
def test_long_running_writer_writes_a_cut_members_lines_again(tmp_path, cls):
	# Spec §16.4.2: the first append after loading a part that ends in a member cut short repeats its whole lines.
	g = str(tmp_path / 'g.tsvz.gz')
	_touch(g, gzip.compress(b'a\t1\n') + gzip.compress(b'b\t2\nbr')[:-6])
	t = getattr(TSVZ, cls)(g)
	t['c'] = ['c', '3']
	t.close()
	assert TSVZ.readTabularFile(g) == {'a': ['a', '1'], 'b': ['b', '2'], 'c': ['c', '3']}
	assert not any('.damaged-' in name for name in os.listdir(str(tmp_path)))

def test_tsvzed_spec_appends_go_to_the_active_part(tmp_path):
	base = str(tmp_path / 'm.tsvz')
	_touch(base, b'a\t1\n')
	_touch(base + '.2', b'b\t2\n')
	t = TSVZ.TSVZed(base)
	t['c'] = ['c', '3']
	del t['a']
	t.close()
	assert open(base, 'rb').read() == b'a\t1\n'
	assert open(base + '.2', 'rb').read() == b'b\t2\nc\t3\na\n'


def test_tsvzed_spec_clear_keeps_markers_queued_before_it(tmp_path):
	p = str(tmp_path / 'c.tsvz')
	t = TSVZ.TSVZed(p, header='id\tv')
	t['a'] = ['a', '1']
	t['#_defaults_#'] = ['D']
	t.clear()
	t['b'] = ['b', '']
	t.close()
	assert open(p, 'rb').read() == b'#id\tv\n#_defaults_#\tD\nb\tD\n'


def test_tsvzed_spec_keeps_queue_when_writes_fail(tmp_path, capsys):
	d = tmp_path / 'sub'
	d.mkdir()
	p = str(d / 'f.tsvz')
	t = TSVZ.TSVZed(p, append_check_delay=0.01)
	t._activePath = str(d / 'missing-dir' / 'f.tsvz')  # every write now fails
	t['a'] = ['a', '1']
	time.sleep(0.1)
	assert _wait_for(lambda: len(t.appendQueue) == 1)
	assert open(p, 'rb').read() == b''
	t._activePath = p
	assert _wait_for(lambda: not t.appendQueue)
	t.close()
	assert open(p, 'rb').read() == b'a\t1\n'
	out = capsys.readouterr()
	assert out.out.count('will retry') == 1 and 'writes recovered' in out.err


def test_two_tsvzed_handles_on_one_tsvz_file(tmp_path):
	p = str(tmp_path / 'shared.tsvz')
	a = TSVZ.TSVZed(p, append_check_delay=0.002)
	b = TSVZ.TSVZed(p, append_check_delay=0.002)
	for i in range(50):
		a['a%d' % i] = ['a%d' % i, str(i)]
		b['b%d' % i] = ['b%d' % i, str(i)]
	a.close()
	b.close()
	data = TSVZ.readTabularFile(p)
	assert len(data) == 100 and data['a7'] == ['a7', '7'] and data['b49'] == ['b49', '49']
	assert open(p, 'rb').read().count(b'\n') == 100


def test_tsvzed_spec_recreates_a_deleted_file_on_append(tmp_path):
	p = str(tmp_path / 'gone.tsvz')
	t = TSVZ.TSVZed(p, monitor_external_changes=False)
	os.remove(p)
	t['a'] = ['a', '1']
	t.close()
	assert open(p, 'rb').read() == b'a\t1\n'


def test_tsvzed_spec_survives_a_failed_external_reload(tmp_path, capsys):
	p = str(tmp_path / 'x.tsvz')
	_touch(p, b'#id\tv\n')
	t = TSVZ.TSVZed(p, header='id\tv', strict=True, append_check_delay=0.01)
	time.sleep(0.05)
	_touch(p, b'#other\tcols\n')
	st = os.stat(p)
	os.utime(p, (st.st_atime + 1, st.st_mtime + 1))
	assert _wait_for(lambda: not t.deSynced and t._specLoaded)
	time.sleep(0.3)
	t['k'] = ['k', 'v']
	assert t.appendThread.is_alive()
	assert _wait_for(lambda: b'k\tv\n' in open(p, 'rb').read())
	t.close()
	assert capsys.readouterr().out.count('Failed to reload') == 1


def test_tsvzed_spec_lone_surrogate_does_not_kill_the_worker(tmp_path):
	p = str(tmp_path / 's.tsvz')
	t = TSVZ.TSVZed(p, append_check_delay=0.01, errors='replace')
	t['bad'] = ['bad', b'\xff'.decode('utf-8', 'surrogateescape')]
	assert _wait_for(lambda: b'bad\t?\n' in open(p, 'rb').read())
	t['c'] = ['c', '1']
	assert _wait_for(lambda: b'c\t1\n' in open(p, 'rb').read())
	assert t.appendThread.is_alive()
	t.close()


def test_tsvzed_spec_clear_after_set_leaves_nothing(tmp_path):
	p = str(tmp_path / 'o.tsvz')
	for _ in range(20):
		t = TSVZ.TSVZed(p, header='id\tv', append_check_delay=0.001)
		t['a'] = ['a', '1']
		t.clear()
		t.close()
		assert TSVZ.readTabularFile(p, header='id\tv') == {}


def test_tsvzed_spec_width_matches_a_reload(tmp_path):
	p = str(tmp_path / 'wd.tsvz')
	t = TSVZ.TSVZed(p, defaults=['X'])
	t['a'] = ['a', '1', '2']
	t['b'] = ['b', '5']
	mem = dict(t)
	t.close()
	assert mem == dict(TSVZ.TSVZed(p, defaults=['X']))


def test_tsvzed_spec_sync_keeps_memory_while_writes_fail(tmp_path):
	d = tmp_path / 'sub'
	d.mkdir()
	p = str(d / 'f.tsvz')
	t = TSVZ.TSVZed(p, append_check_delay=0.01, monitor_external_changes=False)
	t._activePath = str(d / 'nodir' / 'f.tsvz')
	t['a'] = ['a', '1']
	t.deSynced = True
	assert t._specSync(True) is False
	assert t['a'] == ['a', '1'] and t.deSynced
	t._activePath = p
	t.close()


# ==========================================================================
# TSVZedLite on spec stores
# ==========================================================================
def test_lite_spec_reads_and_writes(tmp_path):
	p = str(tmp_path / 'l.tsvz')
	_touch(p, APPENDIX_C)
	lite = TSVZ.TSVZedLite(p, strict=False)
	assert lite.dialect == 'tsvz'
	assert list(lite) == ['alice', 'carol']
	assert lite['carol'] == ['carol', '', '25'] and lite['alice'] == ['alice', 'Alice', '31']
	assert lite['bob'] == ['bob', 'guest', '0']
	assert 'bob' not in lite and lite.get('bob') is None and lite.get('alice') == ['alice', 'Alice', '31']
	assert lite.setdefault('carol', ['x']) == ['carol', '', '25']
	lite['dave'] = ['dave', 'Dave']
	assert lite['dave'] == ['dave', 'Dave', '0']
	lite['#_defaults_#'] = ['G', '9']
	lite['erin'] = ['erin', 'Erin']
	assert lite['erin'] == ['erin', 'Erin', '9'] and lite['dave'] == ['dave', 'Dave', '0']
	del lite['alice']
	lite.close()
	assert TSVZ.readTabularFile(p) == {'carol': ['carol', '', '25'], 'dave': ['dave', 'Dave', '0'],
									   'erin': ['erin', 'Erin', '9']}


def test_lite_spec_multipart_older_parts_in_memory(tmp_path, capsys):
	base = str(tmp_path / 'm.tsvz')
	_touch(base, b'a\t1\nb\t2\n')
	_touch(base + '.3', b'c\t3\n')
	lite = TSVZ.TSVZedLite(base, strict=False)
	assert lite.indexes['a'] == ['a', '1'] and isinstance(lite.indexes['c'], int)
	lite['d'] = ['d', '4']
	del lite['a']
	lite.close()
	assert open(base + '.3', 'rb').read() == b'c\t3\nd\t4\na\n'
	assert any('held in memory' in w for w in _tsvz_warnings(capsys))


def test_lite_spec_gzip(tmp_path):
	g = str(tmp_path / 'g.tsvz.gz')
	TSVZ.appendLinesTabularFile(g, [['a', '1']], createIfNotExist=True)
	lite = TSVZ.TSVZedLite(g, strict=False)
	lite['b'] = ['b', '2']
	assert lite['a'] == ['a', '1'] and lite['b'] == ['b', '2']
	lite.close()
	assert TSVZ.readTabularFile(g) == {'a': ['a', '1'], 'b': ['b', '2']}


def test_lite_spec_external_indexes(tmp_path):
	p = str(tmp_path / 'x.tsvz')
	_touch(p, b'a\t1\n#_defaults_#\tQ\tR\nb\t2\n')
	offsets = TSVZ.readTabularFile(p, storeOffset=True)
	lite = TSVZ.TSVZedLite(p, indexes=offsets, strict=False)
	assert lite['b'] == ['b', '2', 'R'] and lite['a'] == ['a', '1']
	lite.close()


def test_lite_spec_torn_tail_and_clear(tmp_path):
	p = str(tmp_path / 't.tsvz')
	_touch(p, b'#id\tv\na\t1\nzz')
	lite = TSVZ.TSVZedLite(p, header='id\tv', strict=False)
	lite['b'] = ['b', '2']
	assert open(p, 'rb').read() == b'#id\tv\na\t1\nb\t2\n'
	lite.clear()
	assert open(p, 'rb').read() == b'#id\tv\n'
	lite['c'] = ['c', '3']
	assert lite['c'] == ['c', '3'] and list(lite) == ['c']
	lite.close()


def test_lite_spec_constructor_defaults_persisted(tmp_path):
	p = str(tmp_path / 'd.tsvz')
	_touch(p, b'a\t1\n')
	lite = TSVZ.TSVZedLite(p, defaults=['X', 'Y'], strict=False)
	lite.close()
	assert open(p, 'rb').read() == b'a\t1\n#_defaults_#\tX\tY\n'


def test_lite_switch_file_between_dialects(tmp_path):
	tsv = str(tmp_path / 'a.tsv')
	tsvz = str(tmp_path / 'b.tsvz')
	_touch(tsv, b'k\tv\n')
	_touch(tsvz, b'k\tw\n')
	lite = TSVZ.TSVZedLite(tsv, strict=False)
	assert lite['k'] == ['k', 'v'] and lite.dialect == 'tsv'
	lite.switchFile(tsvz)
	assert lite['k'] == ['k', 'w'] and lite.dialect == 'tsvz'
	lite.close()

# ==========================================================================
# CLI
# ==========================================================================
def _cli(*args, script='TSVZ.py'):
	return subprocess.run([sys.executable, os.path.join(HERE, script)] + list(args),
						  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


def test_cli_spec_operations(tmp_path):
	p = str(tmp_path / 'c.tsvz')
	assert _cli(p, 'append', 'alice', 'Alice', '30').returncode == 0
	assert _cli(p, 'append', 'bob', 'Bob', '7').returncode == 0
	assert _cli(p, 'delete', 'bob').returncode == 0
	out = _cli(p, 'read')
	assert out.returncode == 0 and 'Alice' in out.stdout and 'Bob' not in out.stdout
	assert open(p, 'rb').read() == b'alice\tAlice\t30\nbob\tBob\t7\nbob\n'
	assert _cli(p, 'scrub').returncode == 0
	assert open(p, 'rb').read() == b'#_version_#\t1\nalice\tAlice\t30\n'
	assert _cli(p, 'clear').returncode == 0
	assert open(p, 'rb').read() == b''


def test_cli_reads_a_store_made_of_numbered_parts_only(tmp_path):
	base = str(tmp_path / 'm.tsvz')
	_touch(base + '.1', b'key1\tvalue1\n')
	out = _cli(base, 'read')
	assert out.returncode == 0 and 'File not found' not in out.stdout and 'value1' in out.stdout


def test_cli_legacy_matches_339(tmp_path):
	"""3.39 file-first command lines leave 3.39's bytes and print 3.39's text (spec §20.1.5).

	Two listed CLI changes apply: 4.1 prints diagnostics on stderr, and prints
	records into a pipe (``--format table`` asks for 3.39's table). Options
	follow the positionals: 3.39's argparse rejects ``STORE -c H append ...``
	on Python 3.6 and 3.7.
	"""
	steps = (['append', 'k', 'v', '-c', 'id\\tval'], ['append', 'j', 'w'], ['delete', 'k'], ['read'],
			 ['scrub'], ['read'], ['clear', '-c', 'id\\tval'])
	results = []
	for script in ('TSVZ.py', 'TSVZ_old.py'):
		d = tmp_path / script.split('.')[0]
		d.mkdir()
		p = str(d / 'c.tsv')
		outputs = []
		for args in steps:
			if script == 'TSVZ.py':
				r = _cli(p, *(args + ['--format', 'table']), script=script)
				assert 'Created' not in r.stdout
				text = r.stdout + r.stderr
			else:
				r = _cli(p, *args, script=script)
				text = r.stdout
			outputs.append((r.returncode, text.replace(str(d), 'D')))
		results.append((outputs, open(p, 'rb').read()))
	assert results[0] == results[1]


def test_cli_version():
	out = _cli('-V')
	assert out.returncode == 0 and '4.9' in out.stdout


# ==========================================================================
# Round-trip fuzz: memory == reload == scrub + reload (design §8.6)
# ==========================================================================
SPEC_ALPHABET = ['a', 'b', ' ', '\t', ',', '|', '\0', '\n', '<', '>', '#', '_', 'é', '中', '\U0001F600',
				 'sep', 'LF', 'lt', '\r']
LEGACY_ALPHABET = ['a', 'b', ' ', '\t', ',', '|', '\n', '<', '>', 'é', '中', 'sep', 'LF', '\r']


def _fuzz_text(rng, alphabet):
	return ''.join(rng.choice(alphabet) for _ in range(rng.randint(0, 6))).rstrip()


def _trim_empty_tail(row):
	row = list(row)
	while len(row) > 2 and row[-1] == '':
		row.pop()
	return row


def _normalised(items):
	return [(key, _trim_empty_tail(row)) for key, row in items]


def _fuzz_session(path, seed, alphabet, legacy):
	rng = random.Random(seed)
	t = TSVZ.TSVZed(path, append_check_delay=0.001)
	for _ in range(40):
		key = _fuzz_text(rng, alphabet) or 'k'
		if legacy:
			key = key.lstrip('#') or 'k'  # '#' keys are memory-only in 3.39 files
		elif key.startswith('#_') and key.endswith('_#'):
			key = 'x' + key  # the reserved marker namespace is not data
		roll = rng.random()
		if roll < 0.6:
			t[key] = [key] + [_fuzz_text(rng, alphabet) for _ in range(rng.randint(1, 3))]
		elif roll < 0.85:
			del t[key]
		else:
			t[key] = key
	t.close()
	return list(t.items())


def _reload_items(path):
	t = TSVZ.TSVZed(path)
	items = list(t.items())
	t.close()
	return items


def test_spec_roundtrip_fuzz(tmp_path):
	for suffix in ('.tsvz', '.csvz', '.nsvz', '.psvz', '.tsvz.gz'):
		for seed in range(6):
			p = str(tmp_path / ('f%d%s' % (seed, suffix)))
			memory = _normalised(_fuzz_session(p, seed, SPEC_ALPHABET, legacy=False))
			assert _normalised(_reload_items(p)) == memory, (suffix, seed)
			lite = TSVZ.TSVZedLite(p, strict=False)
			assert _normalised((key, lite[key]) for key in list(lite.indexes)) == memory, (suffix, seed)
			lite.close()
			TSVZ.scrubTabularFile(p)
			assert _normalised(_reload_items(p)) == memory, (suffix, seed)


def test_legacy_roundtrip_fuzz(tmp_path):
	for suffix in ('.tsv', '.csv', '.nsv', '.psv', '.tsv.gz'):
		for seed in range(6):
			p = str(tmp_path / ('f%d%s' % (seed, suffix)))
			memory = _fuzz_session(p, seed, LEGACY_ALPHABET, legacy=True)
			assert _reload_items(p) == memory, (suffix, seed)
			TSVZ.scrubTabularFile(p)
			assert _reload_items(p) == memory, (suffix, seed)


# ==========================================================================
# Final review fixes: concurrent writers during scrub / clear, TSVZedLite
# locking, setDefaults persistence, part-path clear, huge version, backups
# ==========================================================================
def _append_after_loads(monkeypatch, path, records, times):
	"""Make the first ``times`` calls of ``_specLoad`` commit ``records`` right after they read."""
	real = TSVZ._specLoad
	calls = []

	def loadThenAppend(*args, **kwargs):
		result = real(*args, **kwargs)
		calls.append(1)
		if len(calls) <= times:
			TSVZ.appendLinesTabularFile(path, records)  # another writer commits after the read
		return result
	monkeypatch.setattr(TSVZ, '_specLoad', loadThenAppend)
	return calls


def test_spec_scrub_keeps_a_record_committed_while_it_runs(tmp_path, monkeypatch):
	p = str(tmp_path / 'live.tsvz')
	_touch(p, b'a\t1\na\t2\nb\t1\n')
	calls = _append_after_loads(monkeypatch, p, [['c', 'during']], times=1)
	assert TSVZ.scrubTabularFile(p) == {'a': ['a', '2'], 'b': ['b', '1'], 'c': ['c', 'during']}
	assert len(calls) == 2  # the first attempt saw the store change and was retried
	monkeypatch.undo()
	assert open(p, 'rb').read() == b'#_version_#\t1\na\t2\nb\t1\nc\tduring\n'


def test_spec_scrub_skips_when_the_store_keeps_changing(tmp_path, monkeypatch, capsys):
	p = str(tmp_path / 'busy.tsvz')
	_touch(p, b'a\t1\na\t2\n')
	calls = _append_after_loads(monkeypatch, p, [['c', 'x']], times=99)
	TSVZ.scrubTabularFile(p)
	monkeypatch.undo()
	assert len(calls) == 3
	assert open(p, 'rb').read() == b'a\t1\na\t2\n' + b'c\tx\n' * 3  # nothing was rewritten
	warnings = _tsvz_warnings(capsys)
	assert any('scrub skipped: the store kept changing while it was being compacted; nothing was written' in w
			   for w in warnings)


def test_spec_clear_retries_when_the_store_changes_after_its_read(tmp_path, monkeypatch):
	# The record and the marker committed after the first read are not lost
	# silently: the clear re-reads, so the record is cleared too and the new
	# #_defaults_# survives the clear (a stale preamble would have dropped it).
	p = str(tmp_path / 'live.tsvz')
	_touch(p, b'#id\tv\na\t1\n')
	calls = _append_after_loads(monkeypatch, p, [['c', 'during'], ['#_defaults_#', 'NEW']], times=1)
	TSVZ.clearTabularFile(p)
	monkeypatch.undo()
	assert len(calls) == 2
	assert open(p, 'rb').read() == b'#id\tv\n#_defaults_#\tNEW\n'
	assert TSVZ.readTabularFile(p) == {}


def test_spec_clear_skips_when_the_store_keeps_changing(tmp_path, monkeypatch, capsys):
	p = str(tmp_path / 'busy.tsvz')
	_touch(p, b'a\t1\n')
	calls = _append_after_loads(monkeypatch, p, [['c', 'x']], times=99)
	TSVZ.clearTabularFile(p)
	monkeypatch.undo()
	assert len(calls) == 3
	assert open(p, 'rb').read() == b'a\t1\n' + b'c\tx\n' * 3
	assert any('clear skipped: the store kept changing while it was being cleared; nothing was written' in w
			   for w in _tsvz_warnings(capsys))


def test_tsvzed_spec_clear_reloads_when_the_clear_was_skipped(tmp_path, monkeypatch, capsys):
	p = str(tmp_path / 'busy.tsvz')
	t = TSVZ.TSVZed(p, append_check_delay=0.002)
	t['a'] = ['a', '1']
	t.commitAppendToFile()
	_append_after_loads(monkeypatch, p, [['c', 'x']], times=99)
	t.clear()
	monkeypatch.undo()
	assert _wait_for(lambda: 'a' in t and 'c' in t)  # memory follows the store the clear left alone
	t.close()
	assert TSVZ.readTabularFile(p) == {'a': ['a', '1'], 'c': ['c', 'x']}


# --------------------------------------------------------------------------
# Readers take no lock: a part rewritten in place under a read (spec §19.8)
# --------------------------------------------------------------------------
def _during_reads(monkeypatch, trigger, action, afterLines=0):
	"""Run ``action()`` whenever a read of a spec store reaches the part ``trigger``, or once it has read
	``afterLines`` lines of it. Parts are then read unbuffered, 8 bytes at a time, so what a read takes
	after ``action`` comes from the bytes it left. Returns a list with one entry per read of ``trigger``.
	The look at a part's first lines for a snapshot marker (spec §19.10) is no read of it here."""
	real = TSVZ._iterPartLines
	realStart = TSVZ._specSnapshotStart
	realOpen = open
	reads = []
	probing = []

	def start(*args):
		probing.append(1)
		try:
			return realStart(*args)
		finally:
			probing.pop()

	def lines(info, reporter, label=''):
		if info.path == trigger and not probing:
			reads.append(1)
			if not afterLines:
				action()
		for n, item in enumerate(real(info, reporter, label), 1):
			yield item
			if info.path == trigger and n == afterLines and not probing:
				action()

	def unbuffered(path, mode='r', *args, **kwargs):
		return realOpen(path, mode, 0) if mode == 'rb' else realOpen(path, mode, *args, **kwargs)
	monkeypatch.setattr(TSVZ, '_iterPartLines', lines)
	monkeypatch.setattr(TSVZ, '_specSnapshotStart', start)
	monkeypatch.setattr(TSVZ, 'open', unbuffered, raising=False)
	monkeypatch.setattr(TSVZ, '_CHUNK', 8)
	return reads


def _rewrites(path, contents):
	"""An action that rewrites the part ``path`` in place, as a scrub does, with the next of ``contents``
	while any are left (the list can be emptied to stop it). Give each another size than the bytes it replaces:
	the (inode, size, mtime) stamp cannot tell a same-size rewrite within one timestamp tick."""
	return lambda: contents and TSVZ._specRewriteInPlace(path, contents.pop(0))


_TORN_EARLIER = 'the parts before the last kept changing while they were read (3 reads); the read may be torn'
_TORN_LAST = 'the last part kept changing other than by appends while it was read (3 reads); the read may be torn'


def test_spec_a_part_rewritten_while_the_store_is_read_is_read_again(tmp_path, monkeypatch, capsys):
	# A multi-part scrub may rewrite an earlier part in place: the first read saw part 0 as it was, then
	# part 1, a view the store never had. It is read again.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'a\t1\nb\t2\n')
	_touch(p + '.1', b'z\t9\nunfinished')
	reads = _during_reads(monkeypatch, p + '.1', _rewrites(p, [b'c\t3\n']))
	assert TSVZ.readTabularFile(p) == {'c': ['c', '3'], 'z': ['z', '9']}
	assert len(reads) == 2
	warnings = _tsvz_warnings(capsys)  # only the read kept reports what it found
	assert len(warnings) == 1 and 'uncommitted bytes' in warnings[0] and 'occurrences' not in warnings[0]
	monkeypatch.undo()
	_during_reads(monkeypatch, p + '.1', _rewrites(p, [b'a\t10\n']))
	store = TSVZ._ServeStore(p)  # the handler's reload reads again too
	assert not store.unreadable and store.data == {'a': ['a', '10'], 'z': ['z', '9']}
	store.close()


def test_spec_verify_reads_a_part_rewritten_under_it_again(tmp_path, monkeypatch):
	p = str(tmp_path / 'v.tsvz')
	_touch(p, b'#_checksum_crc32_#\na\t1\n')
	_touch(p + '.1', ('#_checksum_crc32_#\t' + _crc32(b'b\t22\n') + '\n').encode())
	_during_reads(monkeypatch, p + '.1', _rewrites(p, [b'#_checksum_crc32_#\nb\t22\n']))
	assert _run('verify', p) == (0, '', '')  # not the mismatch the first read computed


def test_spec_a_store_that_keeps_changing_under_its_reads_is_torn_and_nothing_acts_on_it(tmp_path, monkeypatch, capsys):
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'a\t1\nb\t2\n')
	_touch(p + '.1', b'z\t9\n')
	flips = [b'c\t3\n', b'a\t1\nb\t2\n'] * 50
	reads = _during_reads(monkeypatch, p + '.1', _rewrites(p, flips))
	TSVZ.readTabularFile(p)
	assert len(reads) == 3
	assert any(_TORN_EARLIER in w for w in _tsvz_warnings(capsys))
	assert _run('read', p)[0] == 1  # a torn read is an incomplete one
	TSVZ.clearTabularFile(p)  # would append tombstones for the keys one read saw, leaving the others live
	assert any('clear skipped: the store kept changing while it was being cleared; nothing was written' in w
			   for w in _tsvz_warnings(capsys))
	store = TSVZ._ServeStore(p)
	assert store.unreadable
	assert store.pop('z', False, None) is TSVZ._SERVE_PARTIAL
	assert store.popitem(True, False, None) is TSVZ._SERVE_PARTIAL
	assert store.setdefault(['q', '1'], False, None) is TSVZ._SERVE_PARTIAL
	del flips[:]  # the store holds still again
	assert store.read(None) in ([['a', '1'], ['b', '2'], ['z', '9']], [['c', '3'], ['z', '9']])
	assert not store.unreadable
	store.close()
	assert open(p + '.1', 'rb').read() == b'z\t9\n'  # nothing was written


def test_spec_a_part_rewritten_to_its_size_with_its_mtime_put_back_is_read_again(tmp_path):
	# cp -p, rsync --inplace -a: the same size, and the modification time set back; the change time,
	# which no program sets back, still moves.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'a\t1\n')
	_touch(p + '.1', b'b\t2\n')
	store = TSVZ._ServeStore(p)
	st = os.stat(p)
	time.sleep(0.01)
	with open(p, 'r+b') as f:
		f.write(b'a\t9\n')
	os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
	try:
		assert store.get(['a'], None) == ([['a', '9']], False)
	finally:
		store.close()


def test_spec_a_header_garbled_by_a_torn_read_is_judged_on_the_read_kept(tmp_path, monkeypatch, capsys):
	# Part 0 is rewritten while its first line is read: the line seen is not the store's, and the
	# change shows, so the mismatch is no reason to raise; only the read kept counts.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#x\ty\na\t1\n')
	real = TSVZ._iterPartLines
	done = []

	def lines(info, reporter, label=''):
		for item in real(info, reporter, label):
			if info.path == p and not done:
				done.append(1)
				TSVZ._specRewriteInPlace(p, b'#k\tv\na\t11\n')
			yield item
	monkeypatch.setattr(TSVZ, '_iterPartLines', lines)
	data = TSVZ.readTabularFile(p, header='k\tv', strict=True, verbose=False)
	assert data['a'] == ['a', '11']
	assert 'mismatch' not in capsys.readouterr().err


def test_spec_the_handler_does_not_read_again_what_a_read_done_again_saw(tmp_path, monkeypatch):
	# Its first read was torn by an in-place rewrite of part 0, the second one held: the parts as the
	# kept read saw them are the parts now, so the next request reads nothing again.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'a\t1\n')
	_touch(p + '.1', b'b\t2\n')
	reads = _during_reads(monkeypatch, p + '.1', _rewrites(p, [b'a\t99\n']))
	store = TSVZ._ServeStore(p)
	assert len(reads) == 2
	loads = []
	real = TSVZ._specLoad

	def load(*args, **kwargs):
		loads.append(1)
		return real(*args, **kwargs)
	monkeypatch.setattr(TSVZ, '_specLoad', load)
	try:
		assert store.get(['a'], None) == ([['a', '99']], False)
		assert not loads
	finally:
		store.close()


def test_spec_a_compressed_snapshot_part_whose_first_member_is_corrupt_is_no_snapshot_part(tmp_path):
	# Spec §19.10.3: a part whose first lines cannot be read is not a snapshot part. The member is
	# larger than a first read, so its lines decode before its check fails.
	import random as _random
	rng = _random.Random(7)
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'a\t1\n')
	rows = b''.join(b'k%d\t%d\n' % (i, rng.getrandbits(64)) for i in range(20000))
	member = bytearray(gzip.compress(b'#_snapshot_#\n' + rows))
	assert len(member) > 1 << 17
	member[-8] ^= 0xFF  # its CRC
	_touch(p + '.1.gz', bytes(member))
	assert not TSVZ._specIsSnapshotPart(p + '.1.gz', '\t')
	assert 'a' in TSVZ.readTabularFile(p, verbose=False)


def test_spec_a_compressed_snapshot_part_whose_first_member_is_cut_short_is_no_snapshot_part(tmp_path):
	# A part cut short holds no whole state (a crash, or a snapshot still being written).
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'a\t1\n')
	member = gzip.compress(b'#_snapshot_#\n' + b''.join(b'k%d\t%d\n' % (i, i) for i in range(5000)))
	_touch(p + '.1.gz', member[:len(member) // 3])
	assert not TSVZ._specIsSnapshotPart(p + '.1.gz', '\t')
	assert 'a' in TSVZ.readTabularFile(p, verbose=False)


def test_spec_a_torn_tsvzed_load_whose_reload_fails_is_not_read_again_at_every_check(tmp_path, monkeypatch):
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'a\t1\nb\t2\n')
	_touch(p + '.1', b'z\t9\n')
	flips = [b'c\t3\n', b'a\t1\nb\t2\n'] * 50
	_during_reads(monkeypatch, p + '.1', _rewrites(p, flips))
	t = TSVZ.TSVZed(p)
	t.stopAppendThread()
	del flips[:]

	def fail(*args, **kwargs):
		raise OSError(errno.EIO, 'Input/output error')
	monkeypatch.setattr(TSVZ, '_specLoad', fail)
	clock = [time.monotonic()]
	monkeypatch.setattr(TSVZ, '_specClock', lambda: clock[0])
	try:
		t.checkExternalChanges()
		assert t.deSynced
		t.rewrite()  # reloads, which fails: reported, then tried again only after a while
		t.checkExternalChanges()
		assert not t.deSynced
		monkeypatch.undo()  # the store reads again, and the time has come
		clock[0] += 120
		monkeypatch.setattr(TSVZ, '_specClock', lambda: clock[0])
		t.checkExternalChanges()
		assert t.deSynced
		t.rewrite()
		assert dict(t) == dict(TSVZ.readTabularFile(p, verbose=False))
		assert not t._specTornWait, 'a load that worked starts the waits over'
	finally:
		t.close()


def test_spec_a_strict_header_mismatch_is_raised_though_the_next_read_cannot_read_the_header(tmp_path, monkeypatch):
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#x\ty\na\t1\n')
	real = open
	opened = []

	def opening(path, mode='r', *args, **kwargs):
		if path == p and mode == 'rb':
			opened.append(1)
			if len(opened) == 2:
				raise OSError(errno.EMFILE, 'Too many open files')
		return real(path, mode, *args, **kwargs)
	monkeypatch.setattr(TSVZ, 'open', opening, raising=False)
	with pytest.raises(ValueError):
		TSVZ.readTabularFile(p, header='k\tv', strict=True, verbose=False)


def test_spec_a_store_with_no_header_line_under_a_torn_read_is_judged_on_the_read_kept(tmp_path, monkeypatch):
	# The first read sees part 0 empty, cut short by a rewrite that then completes.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'')
	_touch(p + '.1', b'b\t2\n')
	_during_reads(monkeypatch, p + '.1', _rewrites(p, [b'#k\tv\na\t1\n']))
	assert sorted(TSVZ.readTabularFile(p, header='k\tv', strict=True, verbose=False)) == ['a', 'b']


def test_spec_a_strict_header_mismatch_read_steady_is_raised_at_once(tmp_path):
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#k\tv\n' + b''.join(b'r%d\t%d\n' % (i, i) for i in range(1000)))
	rows = {}
	with pytest.raises(ValueError):
		TSVZ.readTabularFile(p, header='x\ty', strict=True, verbose=False, taskDic=rows)
	assert len(rows) <= 1, 'raised at the header, not after the read'


@pytest.mark.parametrize('damage', ['cut', 'corrupt'])
def test_spec_appends_to_a_compressed_part_with_old_damage_do_not_make_it_read_again(tmp_path, monkeypatch, capsys, damage):
	# Damage from an earlier crash stays in the part: appends after it are appends all the same.
	p = str(tmp_path / 's.tsvz.gz')
	old = gzip.compress(b'a\t1\n' * 50)
	old = old[:len(old) // 2] if damage == 'cut' else old[:20] + b'\xff' * 8 + old[28:]
	_touch(p, old + gzip.compress(b'b\t2\n'))

	def append():
		with open(p, 'ab') as f:
			f.write(gzip.compress(b'c\t3\n'))
	reads = _during_reads(monkeypatch, p, append, afterLines=1)
	TSVZ.readTabularFile(p, verbose=False)
	assert len(reads) == 1
	assert _TORN_LAST not in capsys.readouterr().err


def test_spec_the_handler_keeps_stamps_for_the_parts_a_stop_left_unread(tmp_path, monkeypatch):
	# #_on_corruption_# stop ends the read in part 0: part 1 is not read, but it is no change either.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_on_corruption_#\tstop\na\t1\n\0\0\0\0\n')
	_touch(p + '.1', b'b\t2\n')
	store = TSVZ._ServeStore(p)
	loads = []
	real = TSVZ._specLoad

	def load(*args, **kwargs):
		loads.append(1)
		return real(*args, **kwargs)
	monkeypatch.setattr(TSVZ, '_specLoad', load)
	try:
		for _ in range(3):
			store.get(['a'], None)
		assert not loads
	finally:
		store.close()


def test_spec_a_torn_tsvzed_load_is_read_again_after_a_commit_too(tmp_path, monkeypatch):
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'a\t1\nb\t2\n')
	_touch(p + '.1', b'z\t9\n')
	flips = [b'c\t3\n', b'a\t1\nb\t2\n'] * 50
	_during_reads(monkeypatch, p + '.1', _rewrites(p, flips))
	t = TSVZ.TSVZed(p)
	t.stopAppendThread()  # while the store still changes: whatever the worker read again was torn too
	del flips[:]
	t['q'] = ['q', '7']
	t.commitAppendToFile()
	t.checkExternalChanges()
	try:
		assert t.deSynced, 'the torn view is still read again'
	finally:
		t.close()


def test_spec_tsvzed_reads_a_torn_store_again(tmp_path, monkeypatch, capsys):
	# A TSVZed whose load was torn does not keep that view: its next check reads the store again,
	# though no part it watches changed since.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'a\t1\nb\t2\n')
	_touch(p + '.1', b'z\t9\n')
	flips = [b'c\t3\n', b'a\t1\nb\t2\n'] * 50
	_during_reads(monkeypatch, p + '.1', _rewrites(p, flips))
	t = TSVZ.TSVZed(p)
	del flips[:]  # the store holds still again
	steady = dict(TSVZ.readTabularFile(p, verbose=False))
	deadline = time.monotonic() + 10
	while dict(t) != steady and time.monotonic() < deadline:
		time.sleep(0.05)
	try:
		assert dict(t) == steady
	finally:
		t.close()


@pytest.mark.parametrize('name, new', [
	('s.tsvz', b'x\t9\n'),  # shorter: the read ends early, with a and b but not c
	('s.tsvz', b'c\t3\nd\t4\ne\t5\nf\t6\n'),  # longer: the read takes a and b, then e and f
	('s.tsvz.gz', b'x\t9\n'),
	# longer, compressed: the part grows on the same inode, which is no append here
	('s.tsvz.gz', b''.join(b'%c%d\t%d\n' % (97 + i % 26, i, i * 7919) for i in range(60))),
])
def test_spec_a_single_part_rewritten_while_it_is_read_is_read_again(tmp_path, monkeypatch, capsys, name, new):
	# TSVZ's own scrub rewrites a one-part store in place: the part is also the last one.
	p = str(tmp_path / name)
	old = b'a\t1\nb\t2\nc\t3\n'
	_touch(p, gzip.compress(old) if name.endswith('.gz') else old)
	reads = _during_reads(monkeypatch, p, _rewrites(p, [new]), afterLines=1)
	assert list(TSVZ.readTabularFile(p)) == [line.split(b'\t')[0].decode() for line in new.splitlines()]
	assert len(reads) == 2
	assert not _tsvz_warnings(capsys)


@pytest.mark.parametrize('name', ['s.tsvz', 's.tsvz.gz'])
def test_spec_records_appended_while_the_store_is_read_do_not_make_it_read_again(tmp_path, monkeypatch, capsys, name):
	p = str(tmp_path / name)
	old = b'a\t1\nb\t2\n'
	_touch(p, gzip.compress(old) if name.endswith('.gz') else old)
	appends = [[['n', 'new']]]
	reads = _during_reads(monkeypatch, p, lambda: appends and TSVZ.appendLinesTabularFile(p, appends.pop()),
						  afterLines=1)
	assert TSVZ.readTabularFile(p) == {'a': ['a', '1'], 'b': ['b', '2'], 'n': ['n', 'new']}
	assert len(reads) == 1
	assert not _tsvz_warnings(capsys)


def test_spec_a_single_part_rewritten_under_every_read_is_torn_and_scrub_and_clear_refuse(tmp_path, monkeypatch, capsys):
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'a\t1\nb\t2\nc\t3\n')
	flips = [b'x\t9\n', b'a\t1\nb\t2\nc\t3\n'] * 50
	reads = _during_reads(monkeypatch, p, _rewrites(p, flips), afterLines=1)
	TSVZ.readTabularFile(p)
	assert len(reads) == 3
	assert any(_TORN_LAST in w for w in _tsvz_warnings(capsys))
	TSVZ.scrubTabularFile(p)
	assert any('scrub skipped: the store kept changing while it was being compacted; nothing was written' in w
			   for w in _tsvz_warnings(capsys))
	TSVZ.clearTabularFile(p)
	assert any('clear skipped: the store kept changing while it was being cleared; nothing was written' in w
			   for w in _tsvz_warnings(capsys))


def test_engine_reads_a_torn_store_again_rather_than_follow_it(tmp_path, monkeypatch):
	# Each rewrite lengthens the part, so the last one looks like appends after the torn read's offset:
	# following them would keep the torn view. The next sync reads the store again instead.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'a\t0\nb\t0\n')
	grown = [b''.join(b'%c\t%d\n' % (97 + j, i) for j in range(2 + 2 * i)) for i in (1, 2, 3)]
	_during_reads(monkeypatch, p, _rewrites(p, grown), afterLines=1)
	store = TSVZ._ServeStore(p)
	assert store.unreadable and not grown
	assert store.read(None) == [[chr(97 + j), '3'] for j in range(8)]
	assert not store.unreadable
	store.close()


def test_engine_follows_only_after_the_bytes_its_read_saw(tmp_path, monkeypatch):
	# A rewrite that lands just after the reload's read: the follower compares the bytes that read saw, not
	# the rewritten ones, so it reads the store again instead of adding the new bytes' tail to the old view.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'a\t1\nb\t2\n')
	real = TSVZ._specLoad

	def loadThenRewrite(*args, **kwargs):
		result = real(*args, **kwargs)
		TSVZ._specRewriteInPlace(p, b'c\t3\nd\t4\ne\t5\nf\t6\n')
		return result
	monkeypatch.setattr(TSVZ, '_specLoad', loadThenRewrite)
	store = TSVZ._ServeStore(p)
	monkeypatch.undo()
	assert store.read(None) == [['c', '3'], ['d', '4'], ['e', '5'], ['f', '6']]
	store.close()


def test_lite_spec_and_another_process_append_without_losing_records(tmp_path):
	p = str(tmp_path / 'race.tsvz')
	_touch(p, b'seed\tx\n')
	go = str(tmp_path / 'go')
	n = 200
	code = ('import os, sys, time\nsys.path.insert(0, {!r})\nimport TSVZ\n'
			'deadline = time.time() + 20\n'
			'while not os.path.exists({!r}) and time.time() < deadline:\n\tpass\n'
			'for i in range({}):\n\tTSVZ.appendLinesTabularFile({!r}, [["H%d" % i, "v" * 50]])\n').format(HERE, go, n, p)
	helper = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
	written = 0
	try:
		lite = TSVZ.TSVZedLite(p, strict=False)
		_touch(go)  # both writers start now
		# Keep writing for as long as the helper does, so the two writers overlap.
		while written < n or (helper.poll() is None and written < 100000):
			lite['L%d' % written] = ['L%d' % written, 'v' * 50]
			written += 1
		lite.close()
	finally:
		helper.wait()
	data = TSVZ.readTabularFile(p, strict=False)
	missing = [key for key in ['H%d' % i for i in range(n)] + ['L%d' % i for i in range(written)] if key not in data]
	assert not missing, (len(missing), missing[:10])


def test_tsvzed_spec_set_defaults_is_persisted(tmp_path):
	p = str(tmp_path / 'sd.tsvz')
	t = TSVZ.TSVZed(p, header='id\tname\tscore')
	t.setDefaults(['#_defaults_#', 'NONAME', '0'])
	t['k'] = ['k', 'alice']
	t['j'] = ['j', '', '']
	memory = (t['k'], t['j'], list(t.defaults))
	t.close()
	assert memory == (['k', 'alice', '0'], ['j', 'NONAME', '0'], ['#_defaults_#', 'NONAME', '0'])
	assert open(p, 'rb').read() == b'#id\tname\tscore\n#_defaults_#\tNONAME\t0\nk\talice\nj\tNONAME\t0\n'
	t = TSVZ.TSVZed(p, header='id\tname\tscore')
	assert (t['k'], t['j'], list(t.defaults)) == memory
	t.setDefaults(None)  # a reset is written as a lone marker
	assert t.defaults == ['#_defaults_#'] and t['z'] == ['z', '', '']
	t.close()
	t = TSVZ.TSVZed(p, header='id\tname\tscore')
	assert t.defaults == ['#_defaults_#'] and t['k'] == ['k', 'alice', '0']
	t.close()


def test_lite_spec_set_defaults_is_persisted(tmp_path):
	p = str(tmp_path / 'sd.tsvz')
	lite = TSVZ.TSVZedLite(p, header='id\tname\tscore', strict=False)
	lite.setDefaults(['NONAME', '0'])
	lite['k'] = ['k', 'alice']
	lite['j'] = ['j', '', '']
	memory = (lite['k'], lite['j'], list(lite.defaults))
	lite.close()
	assert memory == (['k', 'alice', '0'], ['j', 'NONAME', '0'], ['#_defaults_#', 'NONAME', '0'])
	lite = TSVZ.TSVZedLite(p, header='id\tname\tscore', strict=False)
	assert (lite['k'], lite['j'], list(lite.defaults)) == memory
	lite.close()
	assert open(p, 'rb').read() == b'#id\tname\tscore\n#_defaults_#\tNONAME\t0\nk\talice\nj\tNONAME\t0\n'


def test_set_defaults_during_construction_is_not_written(tmp_path):
	p = str(tmp_path / 'c.tsvz')
	_touch(p, b'#_defaults_#\tF\na\t1\n')
	t = TSVZ.TSVZed(p, defaults=['X'])  # the file's own #_defaults_# overrides the constructor's
	t.close()
	lite = TSVZ.TSVZedLite(p, defaults=['Y'], strict=False)
	lite.close()
	assert open(p, 'rb').read() == b'#_defaults_#\tF\na\t1\n'


def test_spec_clear_refuses_a_part_path(tmp_path, capsys):
	base = str(tmp_path / 'm.tsvz')
	_touch(base, b'k\tv1\n')
	_touch(base + '.1', b'k\tv2\n')
	_touch(base + '.rotated', b'k\tv0\n')
	TSVZ.clearTabularFile(base + '.1')
	TSVZ.clearTabularFile(base + '.rotated')
	TSVZ.clearTabularFile(base + '.5')  # a part that does not exist is not created
	assert open(base + '.1', 'rb').read() == b'k\tv2\n' and open(base + '.rotated', 'rb').read() == b'k\tv0\n'
	assert not os.path.exists(base + '.5')
	assert TSVZ.readTabularFile(base) == {'k': ['k', 'v2']}
	warnings = [w for w in _tsvz_warnings(capsys) if base in w]
	assert len(warnings) == 3
	assert all('clear skipped: ' in w and 'names one part of a multi-part store; clear the store path instead; nothing was written' in w
			   for w in warnings)


def test_spec_version_marker_with_too_many_digits_does_not_crash(tmp_path, capsys):
	p = str(tmp_path / 'v.tsvz')
	_touch(p, b'#_version_#\t' + b'1' * 5000 + b'\nk\tv\n')
	assert TSVZ.readTabularFile(p) == {'k': ['k', 'v']}
	_, state, reporter = _process(['#_version_#\t' + '1' * 5000])
	reporter.flush()
	warnings = [w for w in _tsvz_warnings(capsys) if w.startswith(('TSVZ warning: t:', 'TSVZ warning: ' + p))]
	# On every Python, int()'s digit limit (3.11+) included: a version above every implemented one.
	assert state.version > TSVZ.MAX_SPEC_VERSION and state.newestVersion > TSVZ.MAX_SPEC_VERSION
	assert len(warnings) == 2 and all('declares spec version' in w for w in warnings)
	assert all(len(w) < 300 for w in warnings)  # the value is shortened, not printed whole


def test_spec_integer_marker_values_are_digits_only_with_no_trailing_newline(tmp_path, capsys):
	# '<LF>' decodes to a newline, which is not a digit (spec §12.4.1): the marker is ignored, never a crash.
	for value in ('0<LF>', '2<LF>'):
		_, state, reporter = _process(['#_version_#\t' + value])
		assert (state.version, state.newestVersion) == (1, 1), value
		assert reporter.has('bad-marker'), value
	_, state, reporter = _process(['#_member_size_#\t5<LF>'])
	assert state.memberSize == TSVZ._DEFAULT_MEMBER_SIZE and reporter.has('bad-marker')
	p = str(tmp_path / 'z.tsvz')
	_touch(p, b'#_version_#\t0<LF>\na\t1\na\t2\n')
	assert TSVZ.readTabularFile(p) == {'a': ['a', '2']}
	assert _run('scrub', p)[0] == 0 and open(p, 'rb').read() == b'#_version_#\t1\na\t2\n'


def test_spec_backup_never_overwrites_a_backup(tmp_path, monkeypatch, capsys):
	g = str(tmp_path / 'g.tsvz.gz')
	monkeypatch.setattr(TSVZ.time, 'strftime', lambda *args: '20261008T120000')
	synced = []
	realFsync = os.fsync
	me = threading.current_thread()

	def fsync(fd):
		if threading.current_thread() is me:  # not a worker left over from another test
			synced.append(os.fstat(fd).st_ino)
		return realFsync(fd)
	monkeypatch.setattr(TSVZ.os, 'fsync', fsync)
	first = gzip.compress(b'a\t1\n') + GZ_CORRUPT
	second = gzip.compress(b'c\t3\n') + GZ_CORRUPT
	for damaged in (first, second):
		_touch(g, damaged)
		reporter = TSVZ._Reporter(g)
		TSVZ._specBackupPart(g, reporter)
		reporter.flush()
	monkeypatch.undo()
	backup = g + '.damaged-20261008T120000'
	assert open(backup, 'rb').read() == first and open(backup + '.1', 'rb').read() == second
	directory = os.stat(str(tmp_path)).st_ino  # the new name is durable too
	assert synced == [os.stat(backup).st_ino, directory, os.stat(backup + '.1').st_ino, directory]
	warnings = [w for w in _tsvz_warnings(capsys) if g in w]
	assert len(warnings) == 2 and warnings[0].endswith(backup) and warnings[1].endswith(backup + '.1')

def test_lite_spec_write_locks_and_unlocks_at_raw_offset_zero(tmp_path, monkeypatch):
	# msvcrt.locking locks from the raw fd position, so the lock and the unlock in
	# TSVZedLite._specWrite must both start at raw offset 0, even after a read has
	# filled the buffered file object's read buffer from offset 0.
	p = str(tmp_path / 'w.tsvz')
	_touch(p, b'k0\t' + b'v' * 200 + b'\nk1\tx\n')
	positions = []
	real_lock, real_unlock = TSVZ._lockFile, TSVZ._unlockFile

	def lock(f):
		positions.append(('lock', os.lseek(f.fileno(), 0, os.SEEK_CUR)))
		real_lock(f)

	def unlock(f):
		positions.append(('unlock', os.lseek(f.fileno(), 0, os.SEEK_CUR)))
		real_unlock(f)
	monkeypatch.setattr(TSVZ, '_lockFile', lock)
	monkeypatch.setattr(TSVZ, '_unlockFile', unlock)
	lite = TSVZ.TSVZedLite(p, strict=False)
	assert lite['k0'][0] == 'k0'
	lite['k2'] = ['k2', 'y']
	lite.close()
	assert positions == [('lock', 0), ('unlock', 0)]


# ==========================================================================
# CLI (spec §20): parser, logger, writers
# ==========================================================================
def test_cli_parse_canonical_and_file_first_forms():
	P = TSVZ._cliParseArgs
	a = P(['get', 'data.tsvz', 'alice', 'bob'])
	assert (a.operation, a.store, a.args) == ('get', 'data.tsvz', ['alice', 'bob'])
	a = P(['data.tsvz', 'append', 'k', 'v'])
	assert (a.operation, a.store, a.args) == ('append', 'data.tsvz', ['k', 'v'])
	a = P(['data.tsvz'])
	assert (a.operation, a.store, a.args) == ('read', 'data.tsvz', [])
	a = P(['read', './read'])
	assert (a.operation, a.store) == ('read', './read')
	a = P(['./read'])
	assert (a.operation, a.store) == ('read', './read')
	a = P(['set', 'my store.tsvz', 'k', 'v'])
	assert (a.operation, a.store, a.args) == ('set', 'my store.tsvz', ['k', 'v'])


def test_cli_parse_options_anywhere_and_double_dash():
	P = TSVZ._cliParseArgs
	a = P(['-q', 'set', '--format', 'records', 'd.tsvz', 'k', '-v', 'v1', '--x-header=id\\tv'])
	assert a.quiet and a.verbose and a.format == 'records' and a.header == 'id\\tv'
	assert (a.operation, a.store, a.args) == ('set', 'd.tsvz', ['k', 'v1'])
	a = P(['set', 'd.tsvz', 'k', '-5', '-.5', '-'])
	assert a.args == ['k', '-5', '-.5', '-']
	a = P(['set', 'd.tsvz', '--', 'k', '-v', '--format'])
	assert a.args == ['k', '-v', '--format'] and not a.verbose and a.format is None
	assert P(['-dcomma', 'read', 'd.csv']).delimiter == 'comma'
	assert P(['read', 'd.csv', '--delimiter', '|']).delimiter == '|'
	a = P(['-qv', 'read', 'd.tsvz'])
	assert a.quiet and a.verbose
	assert P(['set', 'd.tsvz', '-']).args == ['-']
	assert P(['set', 'd.tsvz', 'k', '']).args == ['k', '']


def test_cli_parse_legacy_aliases():
	P = TSVZ._cliParseArgs
	a = P(['d.tsv', '-c', 'id\\tv', '-s', 'append', 'k', 'v', '--defaults', 'NA'])
	assert a.header == 'id\\tv' and a.strict is True and a.defaults == 'NA'
	assert P(['d.tsv', '-s', '-f', 'read']).strict is False
	assert P(['d.tsv', '--x-strict', 'read']).strict is True
	assert P(['-cid', 'read', 'd.tsv']).header == 'id'


def test_cli_parse_usage_errors():
	P = TSVZ._cliParseArgs
	bad = ([], ['read'], ['frob', 'd.tsvz'], ['d.tsvz', 'frob'], ['read', 'd.tsvz', 'extra'],
		   ['get', 'd.tsvz'], ['set', 'd.tsvz'], ['delete', 'd.tsvz'], ['--bogus', 'read', 'd.tsvz'],
		   ['x-export', 'd.tsvz'], ['read', 'd.tsvz', '--x-bogus'], ['read', 'd.tsvz', '--format', 'json'],
		   ['read', 'd.tsvz', '--format'], ['read', 'd.tsvz', '--quiet=yes'], ['set', 'd.tsvz', '', 'v'],
		   ['get', 'd.tsvz', 'a', ''], ['delete', 'd.tsvz', ''])
	for argv in bad:
		with pytest.raises(TSVZ._CliUsageError):
			P(argv)
	assert P(['-h']).help and P(['read', '--version']).version


def test_cli_logger_and_writers():
	err = io.StringIO()
	log = TSVZ._CliLogger(err)
	log.teelog('w', 'warning')
	log.teelog('i', callerStackDepth=3)
	assert err.getvalue() == 'w\ni\n'
	err = io.StringIO()
	log = TSVZ._CliLogger(err, quiet=True)
	log.teelog('w', 'warning')
	log.teelog('e', 'error')
	log.teelog('i')
	assert err.getvalue() == 'e\n'
	args = TSVZ._CliArgs()
	out = io.StringIO()
	TSVZ._cliEmitRows([['#k', 'a<b', 'x\ty']], args, out, True, '\t')
	assert out.getvalue() == '<#>k\ta<lt>b\tx<sep>y\n'
	out = io.StringIO()
	TSVZ._cliEmitRows([['k', '<sep>']], args, out, False, '\t')
	assert out.getvalue() == 'k\t</sep/>\n'
	out = io.StringIO()
	TSVZ._cliEmitFields([['0', '', 'a\tb.tsvz', 'active']], ['index', 'ordinal', 'path', 'flags'], args, out)
	assert out.getvalue() == '0\t\ta<sep>b.tsvz\tactive\n'
	args.format = 'table'
	out = io.StringIO()
	TSVZ._cliEmitRows([['k', 'v']], args, out, True, '\t')
	assert out.getvalue() == 'k | v\n\n'  # a store row is not a header
	assert TSVZ._cliFormat(TSVZ._CliArgs(), io.StringIO()) == 'records'


# ==========================================================================
# CLI (spec §20): operations
# ==========================================================================
def _run(*argv, stdin=''):
	"""Run the CLI in-process; return (exit status, stdout, stderr)."""
	out, err = io.StringIO(), io.StringIO()
	status = TSVZ._cliMain(list(argv), stdin=io.StringIO(stdin), stdout=out, stderr=err)
	return status, out.getvalue(), err.getvalue()


def test_cli_read_and_get_a_spec_store(tmp_path):
	p = str(tmp_path / 'g.tsvz')
	_touch(p, b'#_defaults_#\t\tNA\nalice\tAlice\t30\nbob\tBob\n<#>tag\ta<sep>b\n')
	assert _run('read', p) == (0, 'alice\tAlice\t30\nbob\tBob\tNA\n<#>tag\ta<sep>b\tNA\n', '')
	assert _run(p) == _run('read', p)  # file-first form; read is the default
	assert _run('get', p, 'bob', 'alice  ', '#tag') == (0, 'bob\tBob\tNA\nalice\tAlice\t30\n<#>tag\ta<sep>b\tNA\n', '')
	# Spec §14.5: a missing key prints the key and the active defaults; exit 3.
	assert _run('get', p, 'carol', 'alice') == (3, 'carol\t\tNA\nalice\tAlice\t30\n', '')
	with open(p, 'ab') as f:
		f.write(b'#_return_defaults_when_missing_#\tfalse\n')
	assert _run('get', p, 'carol') == (3, '', '')
	q = str(tmp_path / 'd.tsvz')
	_touch(q, b'a\tx\n')
	assert _run('read', q, '--x-defaults', 'key\\tD1\\tD2') == (0, 'a\tx\tD2\n', '')
	assert _run('read', q, '--defaults', 'key\\tD1\\tD2') == (0, 'a\tx\tD2\n', '')


def test_cli_read_and_get_a_loose_file_and_missing_stores(tmp_path):
	q = str(tmp_path / 'g.csv')
	_touch(q, b'alice,Alice,30\nbob,B<sep>ob,\n')
	assert _run('read', q) == (0, 'alice,Alice,30\nbob,B<sep>ob,\n', '')
	assert _run('get', q, 'bob ', 'nobody') == (3, 'bob,B<sep>ob,\n', '')
	for argv in (['read', 'none.tsvz'], ['get', 'none.tsvz', 'k'], ['scrub', 'none.tsvz'],
				 ['read', 'none.tsv'], ['get', 'none.tsv', 'k'], ['scrub', 'none.tsv']):
		argv[1] = str(tmp_path / argv[1])
		status, out, err = _run(*argv)
		assert (status, out) == (1, '') and 'no such store' in err
	assert sorted(os.listdir(str(tmp_path))) == ['g.csv']


def test_cli_set_append_and_delete(tmp_path):
	p = str(tmp_path / 's.tsvz')
	for argv in (['set', p, 'alice', 'Alice', '30'], ['append', p, 'bob', ''], ['set', p, '#_defaults_#', '', 'NA'],
				 ['set', p, '#tag', 'a\tb', 'x<y'], ['set', p, 'carol', '-5'], ['set', p, 'dave'],
				 ['delete', p, 'alice', 'nobody']):
		status, out, _ = _run(*argv)
		assert (status, out) == (0, '')
	assert open(p, 'rb').read() == (b'alice\tAlice\t30\nbob\t\n#_defaults_#\t\tNA\n<#>tag\ta<sep>b\tx<lt>y\n'
									b'carol\t-5\ndave\nalice\nnobody\n')
	assert _run('read', p) == (0, 'bob\t\t\n<#>tag\ta<sep>b\tx<lt>y\ncarol\t-5\tNA\n', '')
	fresh = str(tmp_path / 'fresh.tsvz')
	assert _run('delete', fresh, 'k')[0] == 0  # delete creates a missing store (spec §20.2.2)
	assert open(fresh, 'rb').read() == b'k\n'
	q = str(tmp_path / 's.tsv')
	assert _run(q, 'append', 'k', 'v', '-c', 'id\\tval')[0] == 0  # 3.39 file-first form and -c
	assert _run(q, 'delete', 'k', '-c', 'id\\tval')[0] == 0
	assert open(q, 'rb').read() == b'id\tval\nk\tv\nk\t\n'


def test_cli_store_paths_and_values_that_look_like_options(tmp_path, monkeypatch):
	"""Review focus 2 and 3: values after '--', paths with spaces, a store named like an operation."""
	monkeypatch.chdir(str(tmp_path))
	assert _run('set', 'my store.tsvz', 'k', 'v')[0] == 0
	assert _run('set', 'my store.tsvz', '--', 'e', '-v', '--x')[0] == 0
	assert _run('read', 'my store.tsvz') == (0, 'k\tv\ne\t-v\t--x\n', '')
	assert _run('set', './read', 'k', 'v')[0] == 0
	# [:2]: 3.39 warns on stderr that the name does not end with .tsv
	assert _run('./read')[:2] == (0, 'k\tv\n')
	assert _run('read', './read')[:2] == (0, 'k\tv\n')


def test_cli_clear_and_scrub(tmp_path):
	p = str(tmp_path / 'c.tsvz')
	_touch(p, b'#id\tval\n#_strip_trailing_whites_#\tfalse\na\t1\nb\t2\na\n')
	assert _run('scrub', p) == (0, '', '')
	assert open(p, 'rb').read() == b'#id\tval\n#_version_#\t1\n#_strip_trailing_whites_#\tfalse\nb\t2\n'
	assert _run('clear', p) == (0, '', '')
	assert open(p, 'rb').read() == b'#id\tval\n#_strip_trailing_whites_#\tfalse\n'
	fresh = str(tmp_path / 'fresh.tsvz')
	assert _run('clear', fresh)[0] == 0 and open(fresh, 'rb').read() == b''
	m = str(tmp_path / 'm.tsvz')
	_touch(m, b'a\t1\n')
	_touch(m + '.1', b'b\t2\n')
	status, out, err = _run('scrub', m)  # 4.1 refuses to compact a multi-part store
	assert (status, out) == (1, '') and 'scrub skipped' in err and 'scrub refused' in err
	assert open(m, 'rb').read() == b'a\t1\n' and open(m + '.1', 'rb').read() == b'b\t2\n'
	assert _run('-q', 'scrub', m) == (1, '', 'tsvz: {}: scrub refused; nothing was written\n'.format(m))
	status, _, err = _run('clear', m + '.1')  # one part of a multi-part store
	assert status == 1 and 'clear skipped' in err
	assert _run('clear', m)[0] == 0  # multi-part: a tombstone for every live key, appended
	assert open(m + '.1', 'rb').read() == b'b\t2\na\nb\n'


def test_cli_usage_errors_help_and_version(tmp_path):
	p = str(tmp_path / 'u.tsvz')
	for argv in (['frob', p], ['x-export', p], ['read', p, '--x-bogus'], ['--format', 'json', 'read', p],
				 ['get', p], ['set', p, '', 'v']):
		r = _cli(*argv)
		assert (r.returncode, r.stdout) == (2, '') and 'usage: tsvz' in r.stderr
	assert not os.path.exists(p)
	r = _cli('-h')
	assert (r.returncode, r.stderr) == (0, '') and r.stdout.startswith('usage: tsvz')
	r = _cli('read', p, '--version')
	assert r.returncode == 0 and '4.9' in r.stdout


def test_cli_streams_and_verbosity(tmp_path):
	p = str(tmp_path / 'o.tsvz')
	r = _cli('set', p, 'k', 'v')
	assert (r.returncode, r.stdout) == (0, '') and 'Created' in r.stderr
	r = _cli('-q', 'set', p, 'j', 'w')
	assert (r.returncode, r.stdout, r.stderr) == (0, '', '')
	r = _cli('-v', 'set', p, 'i', 'x')
	assert (r.returncode, r.stdout) == (0, '') and 'Appended 1 lines' in r.stderr
	with open(p, 'ab') as f:
		f.write(b'torn')
	r = _cli('read', p)
	assert (r.returncode, r.stdout) == (0, 'k\tv\nj\tw\ni\tx\n') and 'TSVZ warning' in r.stderr and 'torn' in r.stderr
	r = _cli('-q', 'read', p)
	assert (r.returncode, r.stdout, r.stderr) == (0, 'k\tv\nj\tw\ni\tx\n', '')
	status, out, err = _run('read', p, '-d', 'comma')  # in-process: the warning text is not ASCII
	assert (status, out) == (0, 'k\tv\nj\tw\ni\tx\n') and 'conflicts with the file extension' in err


def test_cli_output_into_a_closed_pipe(tmp_path):
	"""Review focus 5: a reader that closes the pipe early gets no traceback; the status is 1."""
	p = str(tmp_path / 'big.tsvz')
	_touch(p, b''.join(b'k%d\tv%d\n' % (i, i) for i in range(200000)))
	proc = subprocess.Popen([sys.executable, os.path.join(HERE, 'TSVZ.py'), 'read', p],
							stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	first = proc.stdout.readline()
	proc.stdout.close()
	err = proc.stderr.read()
	proc.wait()
	assert first == b'k0\tv0\n'
	assert proc.returncode == 1 and b'Traceback' not in err and b'Exception ignored' not in err


def test_cli_table_prints_rows_not_a_header(tmp_path):
	"""--format table never draws a store row as a header, and no row loses columns.

	Only ``read`` of a loose file keeps 3.39's table, whose first row is the file's header line.
	"""
	p = str(tmp_path / 't.tsvz')
	_touch(p, b'alice\tAlice\t30\nbob\tBob\t25\tx\n')
	assert _run('get', p, 'alice', '--format', 'table') == (0, 'alice | Alice | 30\n\n', '')
	assert _run('read', p, '--format', 'table') == (0, 'alice | Alice | 30 |  \nbob   | Bob   | 25 | x\n\n', '')
	assert _run('keys', p, '--format', 'table') == (0, 'alice\nbob  \n\n', '')
	q = str(tmp_path / 't.tsv')
	_touch(q, b'id\tname\nalice\tAlice\n')
	assert _run('read', q, '--format', 'table') == (0, 'id    | name \n------+------\nalice | Alice\n\n', '')
	assert _run('get', q, 'alice', '--format', 'table') == (0, 'alice | Alice\n\n', '')


def test_cli_table_shows_control_characters_as_escapes(tmp_path):
	"""A table cell never moves the cursor or styles the terminal: control characters print as escapes."""
	p = str(tmp_path / 'c.tsvz')
	_touch(p, b'k\tx<CR>y<LF>z<sep>w\t\x1b[31mred\x07\n')
	row = 'k | x\\ry\\nz\\tw | \\x1b[31mred\\x07\n\n'
	assert _run('read', p, '--format', 'table') == (0, row, '')
	assert _run('get', p, 'k', '--format', 'table') == (0, row, '')


@pytest.mark.skipif(os.name != 'posix', reason='needs a control character in a file name')
def test_cli_parts_table_shows_control_characters_in_paths_as_escapes(tmp_path):
	d = tmp_path / 'a\rb'
	d.mkdir()
	p = str(d / 'c.tsvz')
	_touch(p, b'k\tv\n')
	status, out, _ = _run('parts', p, '--format', 'table')
	assert status == 0 and '\r' not in out and 'a\\rb' in out


def test_cli_table_shows_bidi_and_line_separator_characters_as_escapes(tmp_path):
	"""U+202E and its kin would reorder the rest of the line on a bidi terminal; U+2028/9 can break it."""
	p = str(tmp_path / 'b.tsvz')
	_touch(p, 'k\t\u202eabc\u2066d\u2028e\u200f\n'.encode('utf-8'))
	assert _run('read', p, '--format', 'table') == (0, 'k | \\u202eabc\\u2066d\\u2028e\\u200f\n\n', '')


def test_pretty_format_table_without_a_header():
	assert TSVZ.pretty_format_table([['a', 'bb'], ['ccc', 'd']], header=False, full=True) == 'a   | bb\nccc | d \n'


def test_pretty_format_table_without_a_header_drops_dict_keys():
	assert TSVZ.pretty_format_table([{'k': 'a', 'v': '1'}], header=False, full=True) == 'a | 1\n'
	assert TSVZ.pretty_format_table({'a': {'v': '1'}}, header=False, full=True) == 'a | 1\n'


def test_pretty_format_table_with_a_header_drops_dict_keys():
	assert TSVZ.pretty_format_table([{'a': 1, 'b': 2}], header=['A', 'B'], full=True) == 'A | B\n--+--\n1 | 2\n'


def test_pretty_format_table_pads_wide_characters_by_display_width():
	# East Asian wide characters take two terminal columns, a combining mark none.
	table = TSVZ.pretty_format_table([['name', 'v'], ['中文', '1'], ['abcd', '2'], ['é', '3']], full=True)
	assert table == 'name | v\n-----+--\n中文 | 1\nabcd | 2\né    | 3\n'


def test_pretty_format_table_truncates_wide_characters_to_the_terminal(monkeypatch):
	monkeypatch.setattr(TSVZ, 'get_terminal_size', lambda: (12, 24))
	table = TSVZ.pretty_format_table([['k', 'v'], ['a', '中文中文中文']])
	assert table == 'k|v         \n-+----------\na|中文中文..\n'


def test_pretty_format_table_pads_colored_cells_by_visible_width():
	table = TSVZ.pretty_format_table([['k', 'v'], ['\x1b[31mred\x1b[0m', '1'], ['plain', '2']], full=True)
	assert table == 'k     | v\n------+--\n\x1b[31mred\x1b[0m   | 1\nplain | 2\n'


@pytest.mark.skipif(os.name != 'posix', reason='needs a pseudo-terminal')
def test_cli_prints_a_table_on_a_terminal(tmp_path):
	"""Spec §20.4.3: without --format, a terminal gets the table and a pipe gets records."""
	import pty
	p = str(tmp_path / 't.tsvz')
	_touch(p, b'alice\tAlice\t30\n')
	master, slave = pty.openpty()
	try:
		proc = subprocess.Popen([sys.executable, os.path.join(HERE, 'TSVZ.py'), 'read', p],
								stdout=slave, stderr=subprocess.DEVNULL)
		os.close(slave)
		out = b''
		while True:
			try:
				chunk = os.read(master, 4096)
			except OSError:  # EIO: the child closed the terminal
				break
			if not chunk:
				break
			out += chunk
		proc.wait()
	finally:
		os.close(master)
	assert proc.returncode == 0
	assert b'Alice' in out and b'|' in out and b'\t' not in out
	assert _cli('read', p).stdout == 'alice\tAlice\t30\n'


@pytest.mark.skipif(os.name != 'posix', reason='needs a pseudo-terminal')
def test_get_terminal_size_reads_columns_from_stdin_when_stdout_is_piped():
	"""`tsvz read --format table f | less` on a 200x40 terminal: os.get_terminal_size() fails on the
	pipe, so the TIOCGWINSZ fallback reads stdin, whose winsize struct holds the rows before the columns."""
	import fcntl
	import pty
	import struct
	import termios
	master, slave = pty.openpty()
	try:
		fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 200, 0, 0))
		out = subprocess.run([sys.executable, '-c', 'import TSVZ; print(*TSVZ.get_terminal_size()[:2])'],
							 stdin=slave, stdout=subprocess.PIPE, cwd=HERE, universal_newlines=True, check=True).stdout
	finally:
		os.close(slave)
		os.close(master)
	assert out.split() == ['200', '40']


@pytest.mark.skipif(os.name != 'posix', reason='POSIX locales')
def test_cli_non_ascii_under_the_c_locale(tmp_path):
	"""Review focus 1: UTF-8 arguments, files and output survive LANG=C (ASCII argv and stdio on Python 3.6).

	PYTHONUTF8=0 and PYTHONCOERCECLOCALE=0 give Python 3.7+ the 3.6 behaviour.
	"""
	env = dict(os.environ, LANG='C', LC_ALL='C', PYTHONUTF8='0', PYTHONCOERCECLOCALE='0')
	env.pop('PYTHONIOENCODING', None)
	p = str(tmp_path / 'u.tsvz')

	def run(*args):
		argv = [sys.executable, os.path.join(HERE, 'TSVZ.py')] + [a.encode('utf-8') for a in args]
		return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
	r = run('set', p, 'é', '中', '\U0001F600')
	assert r.returncode == 0 and b'Traceback' not in r.stderr
	assert open(p, 'rb').read() == 'é\t中\t\U0001F600\n'.encode('utf-8')
	r = run('get', p, 'é')
	assert (r.returncode, r.stdout) == (0, 'é\t中\t\U0001F600\n'.encode('utf-8'))
	r = run('read', p, '--format', 'table')
	assert r.returncode == 0 and '中'.encode('utf-8') in r.stdout and b'Traceback' not in r.stderr


# ==========================================================================
# CLI (spec §20.3): bulk input
# ==========================================================================
def test_cli_bulk_set_and_delete(tmp_path):
	p = str(tmp_path / 'b.tsvz')
	data = ('# a comment\n'
			'\n'
			'alice\tAlice\t30\n'
			'<#>_x_#\tdata, not a marker\n'
			'#_defaults_#\t\tNA\n'
			'bob\n'
			'carol\ta<sep>b\tx<lt>y\n'
			'\tno key\n'
			'dave\tunterminated')
	status, out, err = _run('set', p, '-', stdin=data)
	assert (status, out) == (0, '')
	assert open(p, 'rb').read() == (b'alice\tAlice\t30\n<#>_x_#\tdata, not a marker\n#_defaults_#\t\tNA\nbob\n'
									b'carol\ta<sep>b\tx<lt>y\n')
	assert 'unterminated' in err and 'empty key' in err
	status, out, err = _run('delete', p, '-', stdin='alice\tignored\n# comment\n<#>_x_#\n#_defaults_#\n')
	assert (status, out, err) == (0, '', '')
	assert open(p, 'rb').read().endswith(b'carol\ta<sep>b\tx<lt>y\nalice\n<#>_x_#\n#_defaults_#\n')
	assert _run('read', p) == (0, 'carol\ta<sep>b\tx<lt>y\n', '')
	q = str(tmp_path / 'b.csv')
	assert _run('set', q, '-', stdin='k,a<sep>b\n# c\nj,x\n')[0] == 0
	assert open(q, 'rb').read() == b'\nk,a<sep>b\nj,x\n'  # 3.39 starts a header-less file with '\n'
	assert _run('delete', q, '-', stdin='k,whatever\n')[0] == 0
	assert _run('read', q) == (0, 'j,x\n', '')
	fresh = str(tmp_path / 'fresh.tsvz')
	assert _run('set', fresh, '-', stdin='')[0] == 0 and open(fresh, 'rb').read() == b''


def test_cli_bulk_set_is_one_batch(tmp_path, monkeypatch):
	"""Review focus 4: 10 000 records on stdin reach the store in one append (spec §18.2, §20.3)."""
	p = str(tmp_path / 'big.tsvz')
	payloads = []
	real = TSVZ._specAppendPayload

	def spy(path, payload, reporter, **kwargs):
		payloads.append(payload)
		return real(path, payload, reporter, **kwargs)
	monkeypatch.setattr(TSVZ, '_specAppendPayload', spy)
	data = ''.join('k{}\tv{}\n'.format(i, i) for i in range(10000))
	assert _run('set', p, '-', stdin=data)[0] == 0
	assert len(payloads) == 1 and payloads[0] == data.encode()
	assert open(p, 'rb').read() == data.encode()
	assert _run('delete', p, '-', stdin=data)[0] == 0
	assert len(payloads) == 2 and _run('read', p) == (0, '', '')


@pytest.mark.parametrize('ext', ['tsvz', 'csvz', 'nsvz', 'psvz'])
def test_cli_read_piped_into_set_copies_the_store(tmp_path, ext):
	"""Spec §20.3.2: `tsvz read A | tsvz set B -` copies every live row of a same-variant store."""
	src = str(tmp_path / ('a.' + ext))
	dst = str(tmp_path / ('b.' + ext))
	rows = [['alice', 'A, l|i\tce', '30'], ['#hash', '<sep>', 'multi\nline'], ['k', '', 'x<y'],
			['nul', 'a\0b', 'é 中 \U0001F600'], ['#_x_#', 'a key, not a marker', '']]
	TSVZ.appendLinesTabularFile(src, rows[:4], createIfNotExist=True)
	with open(src, 'ab') as f:  # a '#_x_#' data key can only be written in its encoded form
		f.write(TSVZ._specFormatRecord(rows[4], TSVZ._EXTENSION_DELIMITERS[ext]).encode('utf-8') + b'\n')
	status, out, _ = _run('read', src)
	assert status == 0
	assert _run('set', dst, '-', stdin=out)[0] == 0
	expected = list(TSVZ.readTabularFile(src, verifyHeader=False).items())
	assert '#_x_#' in dict(expected)
	assert list(TSVZ.readTabularFile(dst, verifyHeader=False).items()) == expected


# ==========================================================================
# CLI (spec §20.2): verify and parts
# ==========================================================================
def _crc32(data):
	import zlib
	return '%08x' % (zlib.crc32(data) & 0xffffffff)


def test_cli_verify(tmp_path):
	p = str(tmp_path / 'v.tsvz')
	_touch(p, ('#_checksum_crc32_#\na\t1\nb\t2\n#_checksum_crc32_#\t' + _crc32(b'a\t1\nb\t2\n') + '\n'
			   'c\t3\n#_checksum_crc32_#\tdeadbeef\n').encode())
	assert _run('verify', p) == (4, '{}\t6\tcrc32\tdeadbeef\t{}\n'.format(p, _crc32(b'c\t3\n')), '')
	status, out, _ = _run('verify', p, '--format', 'table')
	assert status == 4 and 'expected' in out and 'deadbeef' in out
	m = str(tmp_path / 'm.tsvz')  # a digest runs across parts (spec §17.4); lines count per part
	_touch(m, b'#_checksum_crc32_#\na\t1\n')
	_touch(m + '.1', b'b\t2\n#_checksum_crc32_#\t00000000\n')
	assert _run('verify', m) == (4, '{}.1\t2\tcrc32\t00000000\t{}\n'.format(m, _crc32(b'a\t1\nb\t2\n')), '')
	clean = str(tmp_path / 'clean.tsvz')
	_touch(clean, ('#_checksum_crc32_#\na\t1\n#_checksum_crc32_#\t' + _crc32(b'a\t1\n') + '\n').encode())
	assert _run('verify', clean) == (0, '', '')
	plain = str(tmp_path / 'plain.tsvz')
	_touch(plain, b'a\t1\n')
	assert _run('verify', plain) == (0, '', '')  # no markers: nothing to check (spec §20.2.3)
	loose = str(tmp_path / 'loose.tsv')
	_touch(loose, b'#_checksum_crc32_#\na\t1\n#_checksum_crc32_#\t00000000\n')
	assert _run('verify', loose) == (0, '', '')  # a loose file has no checksums (spec §20.2.4)
	odd = str(tmp_path / 'odd.tsvz')
	_touch(odd, b'#_checksum_nosuchalgo_#\na\t1\n#_checksum_nosuchalgo_#\tff\n')
	status, out, err = _run('verify', odd)
	assert (status, out) == (0, '') and 'not verified' in err
	status, out, err = _run('verify', str(tmp_path / 'none.tsvz'))
	assert (status, out) == (1, '') and 'no such store' in err


def test_cli_parts(tmp_path):
	base = str(tmp_path / 'e.tsvz')
	_touch(base, b'a\t1\n')
	_touch(base + '.0190c3a1', b'b\t2\n')
	_touch(base + '.0a.gz', gzip.compress(b'c\t3\n'))
	_touch(base + '.2.rotated', b'')
	assert _run('parts', base) == (0, '0\t\t{0}\t\n1\t0a\t{0}.0a.gz\tgzip\n2\t0190c3a1\t{0}.0190c3a1\tactive\n'.format(base), '')
	status, out, _ = _run('parts', base, '--format', 'table')
	assert status == 0 and 'ordinal' in out and '|' in out
	numbered = str(tmp_path / 'n.tsvz')
	_touch(numbered + '.1', b'k\tv\n')
	assert _run('parts', numbered) == (0, '0\t1\t{}.1\tactive\n'.format(numbered), '')
	loose = str(tmp_path / 'x.tsv')
	_touch(loose, b'k\tv\n')
	assert _run('parts', loose) == (0, '0\t\t{}\tactive\n'.format(loose), '')
	packed = str(tmp_path / 'y.tsv.gz')
	_touch(packed, gzip.compress(b'k\tv\n'))
	assert _run('parts', packed) == (0, '0\t\t{}\tactive,gzip\n'.format(packed), '')
	status, out, err = _run('parts', str(tmp_path / 'none.tsvz'))
	assert (status, out) == (1, '') and 'no such store' in err


# ==========================================================================
# CLI: final-review fixes
# ==========================================================================
def test_cli_write_that_cannot_create_the_store_fails(tmp_path):
	"""I1: a write whose store cannot be created exits 1 with an error that -q keeps (spec §20.6)."""
	(tmp_path / 'd.tsv').mkdir()
	for store in (str(tmp_path / 'nodir' / 's.tsvz'), str(tmp_path / 'nodir' / 's.tsv'), str(tmp_path / 'd.tsv')):
		for argv, stdin in ((['set', store, 'k', 'v'], ''), (['append', store, 'k'], ''), (['delete', store, 'k'], ''),
							(['set', store, '-'], 'k\tv\n'), (['delete', store, '-'], 'k\n')):
			status, out, err = _run('-q', *argv, stdin=stdin)
			assert (status, out) == (1, '') and 'could not be created' in err, (store, argv)
	assert not os.path.exists(str(tmp_path / 'nodir'))


def test_cli_unreadable_part_fails(tmp_path, monkeypatch):
	"""I2: read, get and verify exit 1 when a part cannot be read; the other parts' rows are still printed."""
	m = str(tmp_path / 'm.tsvz')
	_touch(m, b'a\t1\n')
	os.mkdir(m + '.1')  # a numbered part that cannot be opened, even by root
	for argv, rows in ((['read', m], 'a\t1\n'), (['get', m, 'a'], 'a\t1\n'), (['verify', m], '')):
		status, out, err = _run('-q', *argv)
		assert (status, out) == (1, rows) and 'could not read' in err, argv
	p = str(tmp_path / 'p.tsvz')
	_touch(p, b'a\t1\n')
	real = open

	def deny(path, *args, **kwargs):
		if path == p:
			raise PermissionError(13, 'Permission denied', path)
		return real(path, *args, **kwargs)
	monkeypatch.setattr(TSVZ, 'open', deny, raising=False)
	status, out, err = _run('read', p)
	assert (status, out) == (1, '') and 'Permission denied' in err and 'could not read' in err


@pytest.mark.skipif(os.name != 'posix', reason='POSIX locales')
def test_cli_non_ascii_store_name_under_the_c_locale(tmp_path):
	"""I3: a non-ASCII STORE path keeps working where the locale is ASCII (Python 3.6 under LANG=C)."""
	env = dict(os.environ, LANG='C', LC_ALL='C', PYTHONUTF8='0', PYTHONCOERCECLOCALE='0')
	env.pop('PYTHONIOENCODING', None)
	store = os.path.join(str(tmp_path), 'stör.tsvz')

	def run(*args):
		argv = [sys.executable, os.path.join(HERE, 'TSVZ.py')] + [a.encode('utf-8') for a in args]
		return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
	r = run('set', store, 'k', 'é')
	assert r.returncode == 0 and b'Traceback' not in r.stderr
	assert os.path.exists(store.encode('utf-8'))
	r = run('get', store, 'k')
	assert (r.returncode, r.stdout) == (0, 'k\té\n'.encode('utf-8'))
	r = run('parts', store)
	assert (r.returncode, r.stdout) == (0, b'0\t\t' + store.encode('utf-8') + b'\tactive\n')


def test_cli_parse_short_option_equals_and_long_prefixes():
	"""I4, I5: 3.39's argparse spellings -d=, and unique long-option prefixes still parse."""
	P = TSVZ._cliParseArgs
	assert P(['-d=,', 'read', 'x.csv']).delimiter == ','
	assert P(['read', 'x.tsv', '-c=id']).header == 'id'
	a = P(['--verb', '--delim', 'comma', '--head=id', '--form', 'records', 'read', 'x.csv'])
	assert a.verbose and a.delimiter == 'comma' and a.header == 'id' and a.format == 'records'
	assert P(['--x-s', 'read', 'x.tsv']).strict is True
	for argv in (['--he', 'read', 'x.tsv'], ['--ver', 'read', 'x.tsv'], ['--de', ',', 'read', 'x.tsv'],
				 ['--x-', 'read', 'x.tsv'], ['--bogus', 'read', 'x.tsv']):
		with pytest.raises(TSVZ._CliUsageError):
			P(argv)


def test_cli_delimiter_option_is_checked(tmp_path):
	"""I4: -d=, writes commas; a long -d is warned about; an undecodable -d is a usage error."""
	y = str(tmp_path / 'y.txt')
	assert _run('-d=,', y, 'append', 'a', 'b')[0] == 0
	assert open(y, 'rb').read().endswith(b'a,b\n')
	status, out, err = _run('read', y, '-d', '::')
	assert status == 0 and 'one character' in err
	status, out, err = _run('read', y, '-d', '\\')
	assert (status, out) == (2, '') and 'usage: tsvz' in err


# ==========================================================================
# Write handler (spec §21): library helpers
# ==========================================================================
def test_part_info_counts_committed_bytes(tmp_path):
	p = str(tmp_path / 'c.tsvz')
	_touch(p, b'a\t1\nb\t2\ntorn')
	info = TSVZ._PartInfo(p)
	reporter = TSVZ._Reporter(p)
	assert [raw for raw, _ in TSVZ._iterPartLines(info, reporter)] == [b'a\t1\n', b'b\t2\n']
	assert (info.committed, info.tail) == (8, b'torn')
	assert TSVZ._specLoad(p, '\t', reporter=TSVZ._Reporter(p)).infos[0].lines == 2


def test_spec_replay_line_matches_spec_replay(tmp_path):
	p = str(tmp_path / 'r.tsvz')
	good = '%08x' % (__import__('zlib').crc32(b'a\t1\n') & 0xffffffff)
	_touch(p, ('\ufeff#_defaults_#\t\tD\n#_checksum_crc32_#\na\t1\n#_checksum_crc32_#\t' + good + '\n'
			   '<#>h\tx<sep>y\nb\n#c\n\xe9\t2\n').encode('utf-8') + b'bad\xff\t3\n')
	whole = TSVZ._SpecState()
	expected = [(text, record) for _, _, text, record in
				TSVZ._specReplay([p], '\t', whole, TSVZ._Reporter(p), [])]
	single = TSVZ._SpecState()
	reporter = TSVZ._Reporter(p)
	got = []
	for lineNo, (raw, _) in enumerate(TSVZ._iterPartLines(TSVZ._PartInfo(p), reporter), 1):
		replayed = TSVZ._specReplayLine(raw, lineNo, '', p, single, '\t', reporter)
		if replayed is not None:
			got.append(replayed)
	assert got == expected
	assert single.defaults == whole.defaults == ['#_defaults_#', '', 'D']
	assert single.mismatches == whole.mismatches == []


def test_spec_pad_row():
	state = TSVZ._SpecState(['#_defaults_#', 'x', 'y'])
	row = ['k', '1']
	assert TSVZ._specPadRow(row, state, -1) == 3 and row == ['k', '1', 'y']
	row = ['k', '1', '2', '3']
	assert TSVZ._specPadRow(row, state, 3) == 3 and row == ['k', '1', '2', '3']


def test_legacy_read_loop_reports_width_and_committed_offset(tmp_path):
	p = str(tmp_path / 'l.tsv')
	_touch(p, b'k\tv\nj\tw\nlast\tx')
	data = OrderedDict()
	with open(p, 'rb') as f:
		width, committed, lines = TSVZ._legacyReadLoop(f, p, data, -1, 0, False, '\t', [], False, 'utf8', TSVZ._Reporter(p))
	assert (width, committed, lines) == (2, 8, 3)
	assert list(data) == ['k', 'j', 'last']  # 3.39 reads an unterminated last line as a record


def test_legacy_format_payload_is_what_append_writes(tmp_path):
	p = str(tmp_path / 'f.tsv')
	rows = [['k', 'a\tb'], ['j'], ['i', 'x', 'y']]
	payload, count = TSVZ._legacyFormatPayload(p, rows, None, [''], True, False, 'utf8', False, '\t')
	TSVZ.appendLinesTabularFile(p, rows, createIfNotExist=True)
	assert count == 3 and open(p, 'rb').read() == b'\n' + payload
	assert payload == b'k\ta<sep>b\t\nj\t\t\ni\tx\ty\n'


@pytest.mark.skipif(os.name != 'posix', reason='flock')
def test_try_lock_file_excludes_a_second_handle(tmp_path):
	p = str(tmp_path / 'x.serve')
	first = open(p, 'a+b')
	second = open(p, 'a+b')
	try:
		assert TSVZ._tryLockFile(first) is True
		assert TSVZ._tryLockFile(second) is False
		open(p, 'rb').close()  # closing another descriptor keeps a flock
		assert TSVZ._tryLockFile(second) is False
	finally:
		first.close()
	assert TSVZ._tryLockFile(second) is True
	second.close()


def test_parse_record_lines():
	reporter = TSVZ._Reporter('<test>')
	texts = ['# comment', '', 'k\tv<sep>w', '<#>_x_#\tdata', '#_defaults_#\t\tD<lt>', '\tno key', 'j']
	assert TSVZ._parseRecordLines(texts, '\t', True, False, reporter) == [
		(['k', 'v\tw'], False), (['#_x_#', 'data'], False), (['#_defaults_#', '', 'D<'], True), (['j'], False)]
	assert TSVZ._parseRecordLines(texts, '\t', True, True, reporter) == [
		(['k'], False), (['#_x_#'], False), (['#_defaults_#'], True), (['j'], False)]
	assert TSVZ._parseRecordLines(['k,a<sep>b', '#c', 'j,x'], ',', False, False, reporter) == [
		(['k', 'a,b'], False), (['j', 'x'], False)]
	assert reporter._events['empty-key'][0] == 2


# ==========================================================================
# Write handler (spec §21): the store engine
# ==========================================================================
def _view(path):
	"""Rows a direct reader returns for ``path``, in order (what ``tsvz read`` prints)."""
	return [list(row) for row in TSVZ.readTabularFile(path, verifyHeader=False, strict=False).values()]


@pytest.fixture
def engines():
	"""Open ``_ServeStore``s through this fixture so their writer threads stop."""
	opened = []

	def make(path, **kwargs):
		store = TSVZ._ServeStore(path, **kwargs)
		opened.append(store)
		return store
	yield make
	for store in opened:
		store.close()


@pytest.mark.parametrize('name', ['e.tsvz', 'e.psvz', 'e.tsv', 'e.csv'])
def test_engine_follows_direct_appends(tmp_path, engines, name):
	p = str(tmp_path / name)
	d = TSVZ._EXTENSION_DELIMITERS[name.rpartition('.')[2]].encode()
	_touch(p, b'a' + d + b'1\nb' + d + b'2\n')
	store = engines(p)
	assert store.read(None) == _view(p) == [['a', '1'], ['b', '2']]
	TSVZ.appendLinesTabularFile(p, [['c', '3'], ['a']])
	assert store.read(None) == _view(p) == [['b', '2'], ['c', '3']]
	TSVZ.appendLinesTabularFile(p, [['a', '9'], ['b', 'x' + d.decode() + 'y']])
	assert store.read(None) == _view(p)
	assert store.keys(None) == ['b', 'c', 'a'] and store.length(None) == 3
	assert store.has('a', None) and not store.has('z', None)


def test_engine_ignores_a_torn_tail_until_it_is_completed(tmp_path, engines, capsys):
	p = str(tmp_path / 't.tsvz')
	_touch(p, b'a\t1\n')
	store = engines(p)
	with open(p, 'ab') as f:
		f.write(b'b\t2')
	assert store.read(None) == [['a', '1']]
	assert store.read(None) == [['a', '1']]
	assert capsys.readouterr().err.count('ignored uncommitted bytes') == 1  # noted once, not per request
	with open(p, 'ab') as f:
		f.write(b'2\n')
	assert store.read(None) == _view(p) == [['a', '1'], ['b', '22']]


def test_engine_reloads_when_the_store_changes_other_than_by_appending(tmp_path, engines):
	p = str(tmp_path / 'r.tsvz')
	_touch(p, b'a\t1\nb\t2\na\n')
	store = engines(p)
	assert store.read(None) == [['b', '2']]
	TSVZ.scrubTabularFile(p, verifyHeader=False)  # shrinks the file in place
	TSVZ.appendTabularFile(p, ['c', '3'])
	assert store.read(None) == _view(p) == [['b', '2'], ['c', '3']]
	other = str(tmp_path / 'other')
	_touch(other, b'x\t1\ny\t2\n')
	os.replace(other, p)  # a new inode
	assert store.read(None) == [['x', '1'], ['y', '2']]
	with open(p, 'r+b') as f:  # rewritten in place, same size
		f.write(b'q\t7\nz\t8\n')
	st = os.stat(p)  # file times can be coarser than these steps; make the rewrite visible to stat()
	os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
	assert store.read(None) == [['q', '7'], ['z', '8']]
	_touch(p + '.1', b'n\t1\n')  # a new part
	assert store.read(None) == _view(p) == [['q', '7'], ['z', '8'], ['n', '1']]


def test_engine_follows_compressed_and_multi_part_stores(tmp_path, engines):
	g = str(tmp_path / 'g.tsvz.gz')
	_touch(g, gzip.compress(b'a\t1\n'))
	store = engines(g)
	TSVZ.appendTabularFile(g, ['b', '2'])
	assert store.read(None) == _view(g) == [['a', '1'], ['b', '2']]
	m = str(tmp_path / 'm.tsvz')
	_touch(m, b'a\t1\n')
	_touch(m + '.1', b'b\t2\n')
	store = engines(m)
	TSVZ.appendTabularFile(m, ['c', '3'])  # goes to the active part, m.tsvz.1
	assert store.read(None) == _view(m) == [['a', '1'], ['b', '2'], ['c', '3']]
	assert open(m + '.1', 'rb').read() == b'b\t2\nc\t3\n'


def test_engine_sees_a_new_part_within_one_directory_timestamp(tmp_path, engines, monkeypatch):
	"""A part created in the same timestamp tick as the last directory change is still found."""
	m = str(tmp_path / 'n.tsvz')
	_touch(m, b'a\t1\n')
	frozen = TSVZ._dirMtimeNs(m)
	monkeypatch.setattr(TSVZ, '_dirMtimeNs', lambda path: frozen)  # the new part leaves the mtime as it was
	store = engines(m)
	_touch(m + '.1', b'b\t2\n')
	assert store.read(None) == [['a', '1'], ['b', '2']]

def test_engine_get_resolves_keys_and_missing_rows(tmp_path, engines):
	p = str(tmp_path / 'g.tsvz')
	_touch(p, b'#_defaults_#\t\tNA\nalice\tAlice\t30\nbob\tBob\n')
	store = engines(p)
	assert store.get(['bob', 'alice  ', 'carol'], None) == ([['bob', 'Bob', 'NA'], ['alice', 'Alice', '30'],
															['carol', '', 'NA']], True)
	assert store.get(['alice'], None) == ([['alice', 'Alice', '30']], False)


def test_engine_writes_what_the_direct_writers_write(tmp_path, engines):
	for name in ('w.tsvz', 'w.tsv'):
		direct, served = str(tmp_path / ('d' + name)), str(tmp_path / ('s' + name))
		store = engines(served, create=True)
		_touch(direct, open(served, 'rb').read())
		rows = [(['k', 'a\tb', ''], False), (['#tag', 'x<y'], False), (['#_defaults_#', '', 'D'], True), (['gone'], False)]
		store.write(rows, False, None)
		TSVZ.appendLinesTabularFile(direct, [cells for cells, marker in rows])
		assert store.read(None) == _view(direct)  # read-your-writes: the read waits for the queued batch
		assert open(served, 'rb').read() == open(direct, 'rb').read()


def test_engine_acknowledges_disk_writes_after_fsync(tmp_path, engines, monkeypatch):
	synced = []
	real = os.fsync
	monkeypatch.setattr(os, 'fsync', lambda fd: (synced.append(fd), real(fd)))
	p = str(tmp_path / 'a.tsvz')
	_touch(p, b'a\t1\n')
	store = engines(p)
	store.write([(['b', '2'], False)], False, None)  # memory: no fsync needed to answer
	store.write([(['c', '3'], False)], True, None)  # --sync
	assert synced
	synced[:] = []
	store.write([(['#_write_ack_#', 'disk'], True)], False, None)
	store.read(None)
	store.write([(['d', '4'], False)], False, None)
	assert synced  # the store's own marker asks for disk


def test_engine_pop_popitem_and_setdefault(tmp_path, engines):
	p = str(tmp_path / 'p.tsvz')
	_touch(p, b'a\t1\nb\t2\nc\t3\n')
	store = engines(p)
	assert store.pop('b', False, None) == ['b', '2'] and store.pop('b', False, None) is None
	assert store.popitem(True, False, None) == ['c', '3'] and store.popitem(False, False, None) == ['a', '1']
	assert store.popitem(True, False, None) is None
	assert store.setdefault(['k', 'v'], False, None) == ['k', 'v']
	assert store.setdefault(['k', 'other'], False, None) == ['k', 'v']
	assert open(p, 'rb').read() == b'a\t1\nb\t2\nc\t3\nb\nc\na\nk\tv\n'


def test_engine_pop_is_atomic_under_concurrency(tmp_path, engines):
	p = str(tmp_path / 'race.tsvz')
	_touch(p, b''.join(b'k%d\t%d\n' % (i, i) for i in range(200)))
	store = engines(p)
	popped = []

	def worker():
		while True:
			row = store.popitem(True, False, None)
			if row is None:
				return
			popped.append(row[0])
	threads = [threading.Thread(target=worker) for _ in range(8)]
	for t in threads:
		t.start()
	for t in threads:
		t.join()
	assert sorted(popped) == sorted('k%d' % i for i in range(200))
	assert store.read(None) == [] == _view(p)


def test_engine_clear_scrub_verify_and_parts(tmp_path, engines):
	p = str(tmp_path / 'c.tsvz')
	_touch(p, b'#id\tval\na\t1\nb\t2\na\n')
	store = engines(p)
	assert store.scrub(None) is True and open(p, 'rb').read() == b'#id\tval\n#_version_#\t1\nb\t2\n'
	assert store.read(None) == [['b', '2']]
	assert store.clear(None) is True and store.read(None) == []
	m = str(tmp_path / 'm.tsvz')
	_touch(m, b'a\t1\n')
	_touch(m + '.1', b'#_checksum_crc32_#\nb\t2\n#_checksum_crc32_#\t00000000\n')
	store = engines(m)
	assert store.scrub(None) is False  # 4.1 refuses to compact a multi-part store
	mismatches, readable = store.verify(None)
	assert readable and [entry[:3] for entry in mismatches] == [(m + '.1', 3, 'crc32')]
	assert store.partRows(None) == [['0', '', m, ''], ['1', '1', m + '.1', 'active']]


def test_engine_reports_an_unreadable_part(tmp_path, engines):
	m = str(tmp_path / 'u.tsvz')
	_touch(m, b'a\t1\n')
	os.mkdir(m + '.1')
	store = engines(m)
	assert store.unreadable and store.read(None) == [['a', '1']]


def test_engine_close_writes_and_fsyncs_queued_writes(tmp_path, monkeypatch):
	synced = []
	real = os.fsync
	monkeypatch.setattr(os, 'fsync', lambda fd: (synced.append(fd), real(fd)))
	p = str(tmp_path / 'q.tsvz')
	store = TSVZ._ServeStore(p, create=True)
	for i in range(50):
		store.write([(['k%d' % i, 'v'], False)], False, None)
	store.close()
	assert synced and _view(p) == [['k%d' % i, 'v'] for i in range(50)]

def test_engine_survives_the_store_being_deleted(tmp_path, engines):
	"""Review focus 4: a store removed under the server is recreated by the next write."""
	p = str(tmp_path / 'x.tsvz')
	_touch(p, b'a\t1\n')
	store = engines(p)
	os.remove(p)
	assert store.read(None) == []
	store.write([(['b', '2'], False)], False, None)
	assert store.read(None) == _view(p) == [['b', '2']] and open(p, 'rb').read() == b'b\t2\n'


# ==========================================================================
# Write handler (spec §21): requests and responses
# ==========================================================================
def test_serve_responses(tmp_path, engines):
	p = str(tmp_path / 'r.tsvz')
	_touch(p, b'#_defaults_#\t\tNA\nalice\tAlice\t30\n<#>tag\ta<sep>b\tx\n')
	store = engines(p)
	R = lambda line, bulk=None: TSVZ._serveRespond(store, line, bulk)
	assert R('read') == ['alice\tAlice\t30', '<#>tag\ta<sep>b\tx', '#0']
	assert R('get\talice\tcarol') == ['alice\tAlice\t30', 'carol\t\tNA', '#3']
	assert R('get\t#tag') == ['<#>tag\ta<sep>b\tx', '#0'] == R('get\t<#>tag')
	assert R('has\talice') == ['#0'] and R('has\tnobody') == ['#3']
	assert R('len') == ['2', '#0'] and R('keys') == ['alice', '<#>tag', '#0']
	assert R('set\tbob\tB<lt>b\tline<LF>two') == ['#0']
	assert R('get\tbob') == ['bob\tB<lt>b\tline<LF>two', '#0']
	assert R('setdefault\tbob\tother') == ['bob\tB<lt>b\tline<LF>two', '#0']
	assert R('pop\tbob') == ['bob\tB<lt>b\tline<LF>two', '#0'] and R('pop\tbob') == ['#3']
	assert R('popitem\tfirst') == ['alice\tAlice\t30', '#0']
	assert R('set\t-', ['# comment', 'k\tv', '<#>_x_#\tdata', '#_defaults_#\t\tD']) == ['#0']
	# rows bind the defaults active where they are written: the marker comes after them
	assert R('get\t<#>_x_#\tk') == ['<#>_x_#\tdata\tNA', 'k\tv\tNA', '#0']
	assert R('delete\t-', ['k\tignored', '<#>_x_#']) == ['#0'] and R('has\tk') == ['#3']
	assert R('--sync\tset\tz\t1') == ['#0']
	assert open(p, 'rb').read().endswith(b'k\tv\n<#>_x_#\tdata\n#_defaults_#\t\tD\nk\n<#>_x_#\nz\t1\n')


def test_serve_usage_errors_keep_the_connection_usable(tmp_path, engines):
	store = engines(str(tmp_path / 'u.tsvz'), create=True)
	for line in ('', 'frob', 'x-export', '--bogus\tread', 'read\textra', 'get', 'has\ta\tb', 'set\t\tv',
				 'setdefault\tk', 'setdefault\t#_defaults_#\tx', 'popitem\tmiddle', 'stop'):
		response = TSVZ._serveRespond(store, line)
		assert response[-1].startswith('#2\t') or (line == 'stop' and response[-1].startswith('#1\t')), line
	assert TSVZ._serveRespond(store, 'read') == ['#0']


def test_serve_responses_carry_diagnostics_and_failures(tmp_path, engines, monkeypatch):
	p = str(tmp_path / 'd.tsvz')
	_touch(p, b'a\t1\n')
	store = engines(p)
	with open(p, 'ab') as f:
		f.write(b'torn')
	response = TSVZ._serveRespond(store, 'read')
	assert response[0].startswith('#!\tTSVZ warning: ') and 'torn' in response[0]
	assert response[1:] == ['a\t1', '#0']
	m = str(tmp_path / 'm.tsvz')
	_touch(m, b'a\t1\n')
	_touch(m + '.1', b'b\t2\n')
	store = engines(m)
	assert TSVZ._serveRespond(store, 'scrub')[-1] == '#1\tscrub refused; nothing was written'
	monkeypatch.chdir(str(tmp_path))
	_touch('#h.tsvz', b'#_checksum_crc32_#\na\t1\n#_checksum_crc32_#\t0\n')  # a path starting with '#'
	store = engines('#h.tsvz')
	lines, _, status, _ = TSVZ._serveParseResponse(TSVZ._serveRespond(store, 'verify'))
	assert status == 4 and lines[0].startswith('<#>h.tsvz\t3\tcrc32\t0\t')


def test_serve_parse_response():
	assert TSVZ._serveParseResponse(['#!\tw<LF>x', 'k\tv', '#1\tbad<sep>thing']) == (['k\tv'], ['w\nx'], 1, 'bad\tthing')
	with pytest.raises(TSVZ._ServeLost):
		TSVZ._serveParseResponse(['k\tv'])


def test_cli_has_len_keys_pop_popitem_setdefault(tmp_path):
	p = str(tmp_path / 'o.tsvz')
	_touch(p, b'a\t1\n<#>h\t2\nb\t3\n')
	assert _run('has', p, 'a') == (0, '', '') and _run('has', p, 'z') == (3, '', '')
	assert _run('len', p) == (0, '3\n', '') and _run('keys', p) == (0, 'a\n<#>h\nb\n', '')
	assert _run('pop', p, 'a') == (0, 'a\t1\n', '') and _run('pop', p, 'a') == (3, '', '')
	assert _run('popitem', p) == (0, 'b\t3\n', '') and _run('popitem', p, 'first') == (0, '<#>h\t2\n', '')
	assert _run('popitem', p) == (3, '', '')
	assert _run('setdefault', p, 'k', 'v', '-5') == (0, 'k\tv\t-5\n', '')
	assert _run('setdefault', p, 'k', 'other') == (0, 'k\tv\t-5\n', '')
	assert open(p, 'rb').read() == b'a\t1\n<#>h\t2\nb\t3\na\nb\n<#>h\nk\tv\t-5\n'
	fresh = str(tmp_path / 'fresh.tsvz')
	status, out, err = _run('setdefault', fresh, 'k', 'v')
	assert (status, out) == (0, 'k\tv\n') and open(fresh, 'rb').read() == b'k\tv\n'
	q = str(tmp_path / 'o.csv')
	_touch(q, b'a,x<sep>y\nb,2\n')
	assert _run('keys', q) == (0, 'a\nb\n', '') and _run('pop', q, 'a') == (0, 'a,x<sep>y\n', '')
	for argv in (['has', 'none.tsvz', 'k'], ['len', 'none.tsvz'], ['keys', 'none.tsv'], ['pop', 'none.tsvz', 'k'],
				 ['popitem', 'none.tsvz']):
		argv[1] = str(tmp_path / argv[1])
		status, out, err = _run(*argv)
		assert (status, out) == (1, '') and 'no such store' in err
	for argv in (['has', p], ['has', p, 'a', 'b'], ['len', p, 'x'], ['popitem', p, 'middle'], ['setdefault', p, 'k'],
				 ['pop', p, '']):
		with pytest.raises(TSVZ._CliUsageError):
			TSVZ._cliParseArgs(argv)


# ==========================================================================
# Write handler (spec §21): the server
# ==========================================================================
@pytest.fixture
def served():
	"""Start in-thread handlers: ``served(path, **handleOptions)`` returns the ``_ServeHandle``."""
	running = []

	def start(path, **options):
		handle = TSVZ._ServeHandle(path, **options)
		handle.lock()
		handle.start(TSVZ._ServeStore(path, create=True))
		thread = threading.Thread(target=handle.serve)
		thread.start()
		handle.thread = thread
		running.append((handle, thread))
		return handle
	yield start
	for handle, thread in running:
		handle.stop()
		thread.join(10)
		handle.close()


def _raw(address, data):
	"""Send raw bytes to a handler and return everything it answers until it closes or goes quiet."""
	import socket
	sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	sock.settimeout(2)
	sock.connect(address[len('unix:'):])
	try:
		sock.sendall(data)
		sock.shutdown(socket.SHUT_WR)
		chunks = []
		while True:
			chunk = sock.recv(65536)
			if not chunk:
				return b''.join(chunks)
			chunks.append(chunk)
	finally:
		sock.close()


@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix sockets')
def test_serve_over_a_unix_socket(tmp_path, served):
	p = str(tmp_path / 's.tsvz')
	handle = served(p)
	info = TSVZ._serveFind(p)
	assert info['address'] == handle.address and info['address'].startswith('unix:')
	assert info['pid'] == str(os.getpid()) and info['host'] == __import__('socket').gethostname()
	connection = TSVZ._ServeConnection(info)
	try:
		assert connection.request('set\talice\tAlice') == ([], [], 0, '')
		assert connection.request('set\t-', ['bob\tBob', 'carol']) == ([], [], 0, '')
		assert connection.request('read') == (['alice\tAlice', 'bob\tBob'], [], 0, '')
		assert connection.request('frob')[2:] == (2, 'unknown operation frob')
		assert connection.request('get\tbob') == (['bob\tBob'], [], 0, '')  # still usable after #2
	finally:
		connection.close()
	assert _raw(handle.address, b'len\nkeys\nbogus\n') == b'2\n#0\nalice\nbob\n#0\n#2\tunknown operation bogus\n'


@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix sockets')
def test_serve_closes_a_connection_with_an_overlong_line(tmp_path, served, monkeypatch):
	monkeypatch.setattr(TSVZ, '_SERVE_LINE_LIMIT', 100)
	handle = served(str(tmp_path / 'l.tsvz'))
	assert _raw(handle.address, b'get\t' + b'k' * 200 + b'\nlen\n') == b''
	assert _raw(handle.address, b'len\n') == b'0\n#0\n'

@pytest.mark.skipif(os.name != 'posix', reason='POSIX permissions')
def test_serve_socket_permissions(tmp_path, served):
	def modes(handle):
		path = handle.address[len('unix:'):]
		return os.stat(path).st_mode & 0o777, os.stat(os.path.dirname(path)).st_mode & 0o777
	assert modes(served(str(tmp_path / 'a.tsvz'))) == (0o600, 0o700)
	assert os.stat(str(tmp_path / 'a.tsvz.serve')).st_mode & 0o777 == 0o644
	assert modes(served(str(tmp_path / 'b.tsvz'), group=str(os.getgid()))) == (0o660, 0o710)
	assert modes(served(str(tmp_path / 'c.tsvz'), mode=0o666)) == (0o666, 0o711)


def test_serve_refuses_a_second_handler_for_the_same_store(tmp_path, served):
	p = str(tmp_path / 'b.tsvz')
	served(p)
	second = TSVZ._ServeHandle(p)
	with pytest.raises(TSVZ._ServeBusy) as busy:
		second.lock()
	assert busy.value.args[0]['pid'] == str(os.getpid())


def _serve_process(store, *options):
	"""Start ``tsvz serve STORE`` in the background; return the Popen once its pointer file is written."""
	proc = subprocess.Popen([sys.executable, os.path.join(HERE, 'TSVZ.py'), 'serve', store] + list(options),
							stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	deadline = time.time() + 10
	while TSVZ._serveFind(store) is None:
		if proc.poll() is not None or time.time() > deadline:
			proc.kill()
			raise AssertionError(proc.communicate()[1])
		time.sleep(0.02)
	return proc


def test_tsvz_serve_and_stop(tmp_path):
	p = str(tmp_path / 'p.tsvz')
	proc = _serve_process(p, '--x-header', 'id\\tval')
	try:
		address = TSVZ._serveFind(p)['address']
		r = _cli('serve', p)
		assert r.returncode == 1 and 'already served by pid {}'.format(proc.pid) in r.stderr
		connection = TSVZ._ServeConnection(TSVZ._serveFind(p))
		assert connection.request('set\tk\tv')[2] == 0
		connection.close()
		r = _cli('stop', p)
		assert (r.returncode, r.stdout) == (0, '')
		out, err = proc.communicate(timeout=10)
	finally:
		if proc.poll() is None:
			proc.kill()
	assert proc.returncode == 0 and out == b'' and b'serving' in err and b'stopped serving' in err
	assert open(p, 'rb').read() == b'#id\tval\nk\tv\n'  # the header of a store serve created; the write
	assert not os.path.exists(p + '.serve') and not os.path.exists(os.path.dirname(address[len('unix:'):]))
	r = _cli('stop', p)
	assert r.returncode == 1 and 'no handler is running' in r.stderr


@pytest.mark.skipif(os.name != 'posix', reason='signals')
def test_tsvz_serve_stops_on_sigterm_and_when_idle(tmp_path):
	import signal
	p = str(tmp_path / 'g.tsvz')
	proc = _serve_process(p)
	proc.send_signal(signal.SIGTERM)
	assert proc.wait(10) == 0 and not os.path.exists(p + '.serve')
	proc = _serve_process(p, '--x-idle-timeout', '0.3')
	assert proc.wait(10) == 0 and b'idle for 0.3 s' in proc.stderr.read()
	assert not os.path.exists(p + '.serve')


def test_tsvz_serve_usage_errors(tmp_path):
	p = str(tmp_path / 'u.tsvz')
	for options in (['--x-idle-timeout', 'soon'], ['--x-mode', '9x']):
		r = _cli('serve', p, *options)
		assert r.returncode == 2 and 'serve: bad' in r.stderr and 'usage: tsvz' in r.stderr
	assert _cli('serve', p, 'extra').returncode == 2

@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix sockets')
def test_serve_close_ends_open_connections(tmp_path, served):
	p = str(tmp_path / 'e.tsvz')
	handle = served(p)
	connection = TSVZ._ServeConnection(TSVZ._serveFind(p))
	try:
		assert connection.request('len')[2] == 0
		handle.stop()
		handle.thread.join(10)
		handle.close()
		with pytest.raises(TSVZ._ServeLost):
			connection.request('len')
	finally:
		connection.close()
	assert TSVZ._serveFind(p) is None

@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix sockets')
def test_serve_many_concurrent_clients(tmp_path, served):
	"""Review focus 1: interleaved requests from many connections all land, each answered on its own."""
	p = str(tmp_path / 'c.tsvz')
	served(p)
	errors = []

	def client(n):
		connection = TSVZ._ServeConnection(TSVZ._serveFind(p))
		try:
			for i in range(50):
				key = 'c{}k{}'.format(n, i)
				if connection.request('set\t{}\t{}'.format(key, i))[2] != 0 or \
						connection.request('get\t' + key)[0] != ['{}\t{}'.format(key, i)]:
					errors.append(key)
		finally:
			connection.close()
	threads = [threading.Thread(target=client, args=(n,)) for n in range(8)]
	for t in threads:
		t.start()
	for t in threads:
		t.join()
	assert errors == []
	assert len(_view(p)) == 400


@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix sockets')
def test_serve_ignores_bulk_input_cut_off_before_its_end(tmp_path, served):
	"""Review focus 2: a client that disconnects before the closing '#' writes nothing."""
	p = str(tmp_path / 'b.tsvz')
	handle = served(p)
	assert _raw(handle.address, b'set\t-\nk\tv\nj\tw\n') == b''
	assert _raw(handle.address, b'len\n') == b'0\n#0\n' and open(p, 'rb').read() == b''


def test_serve_takes_over_a_stale_pointer(tmp_path, served):
	"""Review focus 3: the pointer file of a crashed handler is replaced by the next one."""
	import socket
	p = str(tmp_path / 's.tsvz')
	_touch(p + '.serve', 'tsvz-handler\t1\naddress\tunix:/gone\nhost\t{}\npid\t1\n'.format(socket.gethostname()).encode())
	assert _run('get', p, 'k')[0] == 1  # no store yet; the stale pointer is reported and skipped
	handle = served(p)
	assert TSVZ._serveFind(p)['address'] == handle.address


# ==========================================================================
# CLI through a handler (spec §20.8)
# ==========================================================================
_ROUTED_STEPS = (
	(['read'], ''), (['get', 'alice', 'nobody'], ''), (['has', 'alice'], ''), (['len'], ''), (['keys'], ''),
	(['set', 'bob', 'B<b', 'two words'], ''), (['set', '#tag', 'v'], ''), (['set', '#_defaults_#', '', 'NA'], ''),
	(['get', 'carol', 'bob'], ''), (['delete', 'alice', 'nobody'], ''),
	(['set', '-'], 'k1\tv1\n# comment\nk2\tv<sep>2\n'), (['delete', '-'], 'k1\tignored\n'),
	(['pop', 'bob'], ''), (['pop', 'bob'], ''), (['popitem', 'first'], ''), (['setdefault', 'k', 'v', '-5'], ''),
	(['setdefault', 'k', 'other'], ''), (['read', '--format', 'table'], ''), (['verify'], ''),
	(['scrub'], ''), (['read'], ''), (['clear'], ''), (['read'], ''), (['popitem'], ''),
)


@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix sockets')
@pytest.mark.parametrize('name', ['d.tsvz', 'd.tsv'])
def test_cli_gives_the_same_results_through_a_handler(tmp_path, served, name):
	"""Spec §20.8: every operation prints the same and exits the same with or without a handler."""
	direct, routed = str(tmp_path / ('direct-' + name)), str(tmp_path / ('routed-' + name))
	start = b'alice\tAlice\t30\n<#>h\tx\ty\n' if name.endswith('z') else b'alice\tAlice\t30\nh\tx\ty\n'
	for path in (direct, routed):
		_touch(path, start)
	served(routed)
	for argv, stdin in _ROUTED_STEPS:
		first = _run(argv[0], direct, *argv[1:], stdin=stdin)
		second = _run(argv[0], routed, *argv[1:], stdin=stdin)
		assert first[:2] == second[:2], argv
	assert open(direct, 'rb').read() == open(routed, 'rb').read()


@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix sockets')
def test_cli_routes_only_what_the_handler_can_answer(tmp_path, served, monkeypatch):
	p = str(tmp_path / 'v.tsvz')
	_touch(p, b'a\t1\n')
	served(p)
	requests = []
	real = TSVZ._serveRespond

	def spy(store, line, bulk=None, logger=None):
		requests.append(line)
		return real(store, line, bulk, logger)
	monkeypatch.setattr(TSVZ, '_serveRespond', spy)
	assert _run('get', p, 'a') == (0, 'a\t1\n', '')
	assert _run('--x-direct', 'get', p, 'a') == (0, 'a\t1\n', '')
	assert _run('get', p, 'a', '--x-defaults', 'k\\tD') == (0, 'a\t1\n', '')  # an option the handler lacks
	q = str(tmp_path / 'v.txt')
	_touch(q, b'a|1\n')
	served(q)  # a loose file served with the delimiter its name implies (tab)
	assert _run('get', q, 'a', '-d', 'pipe')[:2] == (0, 'a|1\n')  # 3.39 warns about the name on stderr
	assert requests == ['get\ta']


def test_cli_falls_back_to_the_files_when_the_handler_is_gone(tmp_path):
	import socket
	p = str(tmp_path / 'f.tsvz')
	_touch(p, b'a\t1\n')
	_touch(p + '.serve', 'tsvz-handler\t1\naddress\tunix:{}\nhost\t{}\npid\t1\n'.format(
		str(tmp_path / 'gone.sock'), socket.gethostname()).encode())
	status, out, err = _run('get', p, 'a')
	assert (status, out) == (0, 'a\t1\n') and 'does not answer' in err
	_touch(p + '.serve', b'tsvz-handler\t1\naddress\tunix:/nowhere\nhost\tsome-other-host\npid\t1\n')
	assert _run('get', p, 'a') == (0, 'a\t1\n', '')  # another host's handler: skipped quietly
	assert _run('stop', p)[0] == 1


@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix sockets')
def test_cli_when_the_handler_drops_the_connection(tmp_path):
	"""A read falls back to the files; a write reports that its outcome is unknown."""
	import socket
	path = str(tmp_path / 'drop.sock')
	listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	listener.bind(path)
	listener.listen(8)

	def drop():
		while True:
			try:
				conn, _ = listener.accept()
			except OSError:
				return
			conn.recv(1024)
			conn.close()
	thread = threading.Thread(target=drop)
	thread.daemon = True
	thread.start()
	p = str(tmp_path / 'd.tsvz')
	_touch(p, b'a\t1\n')
	_touch(p + '.serve', 'tsvz-handler\t1\naddress\tunix:{}\nhost\t{}\npid\t1\n'.format(
		path, socket.gethostname()).encode())
	try:
		status, out, err = _run('read', p)
		assert (status, out) == (0, 'a\t1\n') and 'lost the handler' in err
		status, out, err = _run('set', p, 'b', '2')
		assert (status, out) == (1, '') and 'may or may not have been applied' in err
	finally:
		listener.close()


def test_tsvz_commands_through_tsvz_serve(tmp_path):
	p = str(tmp_path / 'e.tsvz')
	proc = _serve_process(p)
	try:
		assert _cli('set', p, 'k', 'v').returncode == 0
		r = _cli('get', p, 'k')
		assert (r.returncode, r.stdout) == (0, 'k\tv\n')
		r = subprocess.run([sys.executable, os.path.join(HERE, 'TSVZ.py'), 'set', p, '-'], input='j\tw\n',
						   stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
		assert r.returncode == 0 and _cli('read', p).stdout == 'k\tv\nj\tw\n'
		assert _cli('stop', p).returncode == 0
		assert proc.wait(10) == 0
	finally:
		if proc.poll() is None:
			proc.kill()
	assert open(p, 'rb').read() == b'k\tv\nj\tw\n'


# ==========================================================================
# TSVZClient: a served store from Python (spec §21)
# ==========================================================================
def _mapping_story(t):
	"""Run the same dict operations on a TSVZed or a TSVZClient; return what they returned."""
	seen = []
	t['alice'] = ['alice', 'Alice', '30']
	t['bob'] = 'bob\tBob\t7'
	t['carol'] = ['Carol', '5']  # the key is put first
	seen += [t['bob'], t.get('nobody', 'missing'), 'alice' in t, 'zed' in t, len(t), list(t)]
	del t['bob']
	seen += [t.pop('carol'), t.pop('carol', 'gone')]
	seen += [t.setdefault('dave', ['dave', 'D', '1']), t.setdefault('dave', ['dave', 'X', '2'])]
	t.update({'erin': ['erin', 'E', '2'], 'fay': 'fay\tF\t3'})
	seen += [sorted(t.keys()), t.popitem(), t.popitem(last=False)]
	del t['nobody']  # a missing key is not an error
	return seen


@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix sockets')
@pytest.mark.parametrize('ext', ['tsvz', 'tsv'])
def test_tsvz_client_behaves_like_tsvzed(tmp_path, served, ext):
	header = 'id\tname\tn' if ext == 'tsvz' else ''  # a .tsv header line is a row when served, as in `tsvz read`
	paths = [str(tmp_path / '{}.{}'.format(name, ext)) for name in ('tsvzed', 'served', 'local')]
	for path in paths[1:]:
		TSVZ.appendLinesTabularFile(path, [], header=header, createIfNotExist=True)
	served(paths[1])
	with TSVZ.TSVZed(paths[0], header=header) as t:
		expected = _mapping_story(t)
	results = []
	for path in paths[1:]:
		with TSVZ.TSVZClient(path) as c:
			results.append(_mapping_story(c))
			assert c.items() == [(row[0], row) for row in _view(path)]
	assert results == [expected, expected]
	assert expected[:6] == [['bob', 'Bob', '7'], 'missing', True, False, 3, ['alice', 'bob', 'carol']]
	views = [[row for row in _view(path) if row[0] != 'id'] for path in paths]
	assert views[0] == views[1] == views[2] == [['dave', 'D', '1'], ['erin', 'E', '2']]


def test_tsvz_client_missing_keys_follow_the_store(tmp_path):
	p = str(tmp_path / 'm.tsvz')
	_touch(p, b'#_defaults_#\t\tNA\na\t1\t2\n')
	with TSVZ.TSVZClient(p) as c:
		assert c['zed'] == ['zed', '', 'NA'] and c.get('zed') is None and 'zed' not in c
		c['#_return_defaults_when_missing_#'] = ['false']  # a marker write
		with pytest.raises(KeyError):
			c['zed']
		c.setDefaults(['', 'X'])
		assert c['a'] == ['a', '1', '2'] and open(p, 'rb').read().endswith(
			b'#_return_defaults_when_missing_#\tfalse\n#_defaults_#\t\tX\n')
		for method in ('move_to_end', 'rewrite', 'mapToFile', 'hardMapToFile'):
			with pytest.raises(NotImplementedError):
				getattr(c, method)('a')


def test_tsvz_client_warns_once_when_no_handler_runs(tmp_path, capsys):
	p = str(tmp_path / 'w.tsvz')
	with TSVZ.TSVZClient(p) as c:
		c['k'] = ['k', 'v']
		assert c['k'] == ['k', 'v'] and len(c) == 1
		assert repr(c) == 'TSVZClient({!r}, local)'.format(p)
	err = capsys.readouterr().err
	assert err.count('working on the files in this process') == 1
	assert open(p, 'rb').read() == b'k\tv\n'


@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix sockets')
def test_tsvz_client_sync_asks_for_disk_acknowledgement(tmp_path, served, monkeypatch):
	p = str(tmp_path / 's.tsvz')
	served(p)
	lines = []
	real = TSVZ._serveRespond
	monkeypatch.setattr(TSVZ, '_serveRespond', lambda store, line, bulk=None, logger=None: (
		lines.append(line), real(store, line, bulk, logger))[1])
	with TSVZ.TSVZClient(p, sync=True) as c:
		c['k'] = 'k\tv'
		assert c['k'] == ['k', 'v']
	assert lines == ['--sync\t--x-written\tset\tk\tv', 'get\tk']


@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix sockets')
def test_tsvz_client_reconnects_or_falls_back(tmp_path, served):
	p = str(tmp_path / 'r.tsvz')
	first = served(p)
	client = TSVZ.TSVZClient(p)
	try:
		client['k'] = ['k', 'v']
		first.stop()
		first.thread.join(10)
		first.close()
		second = served(p)
		assert client['k'] == ['k', 'v']  # the lost connection was replaced by one to the new handler
		assert repr(client).endswith('served)')
		second.stop()
		second.thread.join(10)
		second.close()
		with pytest.raises(ConnectionError):
			client.pop('k')  # not retried: it may have been applied
		assert repr(client).endswith('local)') and client['k'] == ['k', 'v']
	finally:
		client.close()


# ==========================================================================
# Write handler: final-review fixes
# ==========================================================================
_ROOT = hasattr(os, 'geteuid') and os.geteuid() == 0
_NOBODY = 65534
_UNIX = hasattr(__import__('socket'), 'AF_UNIX')


def _write_pointer(path, lines, mode=0o644):
	_touch(path, ''.join(line + '\n' for line in lines).encode('utf-8'))
	os.chmod(path, mode)


@pytest.mark.skipif(os.name != 'posix', reason='symlinks')
def test_serve_never_writes_through_a_symlinked_pointer(tmp_path):
	"""C1: a symlink planted at STORE.serve is refused, and its target is left alone."""
	victim = tmp_path / 'victim'
	victim.write_bytes(b'precious\n')
	os.chmod(str(victim), 0o600)
	p = str(tmp_path / 'd.tsvz')
	os.symlink(str(victim), p + '.serve')
	status, out, err = _run('serve', p)
	assert (status, out) == (1, '') and 'not a regular file' in err
	assert victim.read_bytes() == b'precious\n' and os.stat(str(victim)).st_mode & 0o777 == 0o600


@pytest.mark.skipif(not _ROOT, reason='needs root to plant a file owned by another user')
def test_serve_replaces_a_stale_pointer_instead_of_writing_into_it(tmp_path, served):
	"""C1/I1: a stale pointer (here another user's) is replaced by a new file, never written into."""
	p = str(tmp_path / 'o.tsvz')
	_touch(p + '.serve', b'old\n')
	os.chown(p + '.serve', _NOBODY, _NOBODY)
	os.chmod(p + '.serve', 0o666)
	old = open(p + '.serve', 'rb')  # a descriptor opened before the handler starts
	try:
		served(p)
		st = os.stat(p + '.serve')
		assert st.st_uid == os.geteuid() and st.st_mode & 0o777 == 0o644
		assert st.st_ino != os.fstat(old.fileno()).st_ino and old.read() == b'old\n'
	finally:
		old.close()


def test_tsvz_client_writes_survive_exit_without_close(tmp_path):
	"""C2: the in-process engine writes everything when the interpreter exits."""
	p = str(tmp_path / 'x.tsvz')
	code = ("import sys\nsys.path.insert(0, {!r})\nimport TSVZ\nc = TSVZ.TSVZClient({!r})\n"
			"for i in range(300):\n    c['k%d' % i] = ['k%d' % i, 'v']\n").format(HERE, p)
	r = subprocess.run([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	assert r.returncode == 0 and len(_view(p)) == 300


@pytest.mark.skipif(os.name != 'posix', reason='POSIX file types')
def test_clients_ignore_untrusted_pointer_files(tmp_path):
	"""C3: a pointer file that cannot be trusted is reported and the files are used."""
	import socket
	host = socket.gethostname()
	p = str(tmp_path / 't.tsvz')
	_touch(p, b'a\t1\n')
	sock = str(tmp_path / 'none.sock')
	cases = [
		(['tsvz-handler\t1', 'address\ttcp:10.255.255.1:9', 'host\t' + host, 'pid\t1'],
		 0o644, 'not a Unix-domain socket'),
		(['tsvz-handler\t1', 'address\tunix:' + sock, 'host\t' + host, 'pid\t1'], 0o664, 'writable by others'),
		(['tsvz-handler\t1', 'address\tunix:' + sock, 'host\t' + host, 'pid\t1<LF>stop'],
		 0o644, 'control characters'),
	]
	for lines, mode, reason in cases:
		_write_pointer(p + '.serve', lines, mode)
		status, out, err = _run('get', p, 'a')
		assert (status, out) == (0, 'a\t1\n') and reason in err, reason
	os.remove(p + '.serve')
	os.symlink('/dev/zero', p + '.serve')
	status, out, err = _run('get', p, 'a')
	assert (status, out) == (0, 'a\t1\n') and 'not a regular file' in err
	os.remove(p + '.serve')
	os.mkfifo(p + '.serve')
	r = subprocess.run([sys.executable, os.path.join(HERE, 'TSVZ.py'), 'get', p, 'a'], stdout=subprocess.PIPE,
					   stderr=subprocess.PIPE, universal_newlines=True, timeout=20)
	assert (r.returncode, r.stdout) == (0, 'a\t1\n') and 'not a regular file' in r.stderr


@pytest.mark.skipif(not (_ROOT and _UNIX), reason='needs root to make files owned by another user')
def test_clients_trust_only_pointers_and_sockets_of_the_store_owner(tmp_path, served):
	"""C3/I2: another user's pointer, or a socket another user re-created, is not used."""
	import socket
	p = str(tmp_path / 'w.tsvz')
	_touch(p, b'a\t1\n')
	host = socket.gethostname()
	planted = str(tmp_path / 'planted.sock')
	listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	listener.bind(planted)
	listener.listen(1)
	try:
		os.chown(planted, _NOBODY, _NOBODY)
		_write_pointer(p + '.serve', ['tsvz-handler\t1', 'address\tunix:' + planted, 'host\t' + host, 'pid\t1'])
		status, out, err = _run('get', p, 'a')
		assert (status, out) == (0, 'a\t1\n') and 'socket' in err and 'owned by' in err
		os.chown(p + '.serve', _NOBODY, _NOBODY)
		status, out, err = _run('get', p, 'a')
		assert (status, out) == (0, 'a\t1\n') and 'owned by' in err
	finally:
		listener.close()


def test_routed_writes_report_failures_and_pop_keeps_the_key(tmp_path, served, monkeypatch):
	"""I3: a write the handler cannot make exits 1, as on the files; later answers warn until writes work."""
	p = str(tmp_path / 'f.tsvz')
	_touch(p, b'a\t1\n')
	served(p)
	real = TSVZ._specAppendPayload

	def broken(*args, **kwargs):
		raise OSError(28, 'No space left on device')
	monkeypatch.setattr(TSVZ, '_specAppendPayload', broken)
	status, out, err = _run('set', p, 'b', '2')
	assert (status, out) == (1, '') and 'No space left' in err
	status, out, err = _run('pop', p, 'a')
	assert (status, out) == (1, '') and 'No space left' in err
	status, out, err = _run('get', p, 'a')
	assert (status, out) == (0, 'a\t1\n') and 'writes are failing' in err
	monkeypatch.setattr(TSVZ, '_specAppendPayload', real)
	assert _run('set', p, 'b', '2') == (0, '', '')
	assert _run('get', p, 'b') == (0, 'b\t2\n', '')


@pytest.mark.skipif(not _UNIX, reason='Unix sockets')
def test_routed_output_matches_the_files_in_corner_cases(tmp_path, served, monkeypatch):
	"""I4: parts/verify spelled differently, a deleted store, a legacy last line without a newline."""
	monkeypatch.chdir(str(tmp_path))
	p = str(tmp_path / 'p.tsvz')
	_touch(p, b'#_checksum_crc32_#\na\t1\n#_checksum_crc32_#\t0\n')
	served(p)  # the handler knows the absolute path; the client says p.tsvz
	for argv in (['parts', 'p.tsvz'], ['verify', 'p.tsvz']):
		assert _run(*argv)[:2] == _run('--x-direct', *argv)[:2], argv
	os.remove(p)
	for op in ('read', 'len', 'keys', 'get', 'has', 'pop', 'popitem'):
		argv = [op, p] + (['a'] if op in ('get', 'has', 'pop') else [])
		status, out, err = _run(*argv)
		assert (status, out) == (1, '') and 'no such store' in err, op
	q = str(tmp_path / 'q.tsv')
	_touch(q, b'a\t1\nb\t2\n')
	served(q)
	with open(q, 'ab') as f:
		f.write(b'c\t3\nd\t4')  # another writer's last line, without a newline
	for argv in (['get', q, 'd'], ['len', q], ['keys', q]):
		assert _run(*argv)[:2] == _run('--x-direct', *argv)[:2], argv


@pytest.mark.skipif(not _UNIX, reason='Unix sockets')
def test_tsvz_client_is_safe_to_share_between_threads(tmp_path, served):
	"""I5: one TSVZClient used by many threads answers each call with its own result."""
	p = str(tmp_path / 's.tsvz')
	served(p)
	client = TSVZ.TSVZClient(p)
	errors = []

	def work(n):
		for i in range(30):
			key = 't{}k{}'.format(n, i)
			client[key] = [key, str(i)]
			if client[key] != [key, str(i)]:
				errors.append(key)
	try:
		threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()
	finally:
		client.close()
	assert errors == [] and len(_view(p)) == 240


@pytest.mark.skipif(not _UNIX, reason='Unix sockets')
def test_serve_group_socket_is_not_put_in_a_private_runtime_dir(tmp_path, served, monkeypatch):
	"""I6: a socket meant for a group does not go under a 0700 XDG_RUNTIME_DIR."""
	xdg = tmp_path / 'xdg'
	xdg.mkdir()
	os.chmod(str(xdg), 0o700)
	monkeypatch.setenv('XDG_RUNTIME_DIR', str(xdg))
	handle = served(str(tmp_path / 'g.tsvz'), group=str(os.getgid()))
	assert not handle.address.startswith('unix:' + str(xdg))
	assert served(str(tmp_path / 'h.tsvz')).address.startswith('unix:' + str(xdg))


@pytest.mark.skipif(not _UNIX, reason='Unix sockets')
def test_serve_accepts_a_burst_of_connections(tmp_path, served):
	"""I7: many clients connecting at the same moment are all served, none falls back."""
	p = str(tmp_path / 'burst.tsvz')
	served(p)
	info = TSVZ._serveFind(p)
	failures = []
	start = threading.Event()

	def connect():
		start.wait()
		try:
			connection = TSVZ._ServeConnection(info)
		except TSVZ._ServeUnreachable as e:
			failures.append(str(e))
			return
		try:
			connection.request('len')
		finally:
			connection.close()
	threads = [threading.Thread(target=connect) for _ in range(64)]
	for t in threads:
		t.start()
	start.set()
	for t in threads:
		t.join()
	assert failures == []

def test_engine_reloads_on_a_same_size_edit(tmp_path, engines):
	"""M1: an in-place edit that keeps the size is seen, even when the bytes before the offset are unchanged."""
	p = str(tmp_path / 'm.tsvz')
	tail = b''.join(b'filler%02d\tx\n' % i for i in range(10))
	_touch(p, b'a\t100\n' + tail)
	store = engines(p)
	assert store.get(['a'], None)[0] == [['a', '100']]
	with open(p, 'r+b') as f:
		f.write(b'a\t999\n')
	st = os.stat(p)
	os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
	assert store.get(['a'], None)[0] == [['a', '999']]


@pytest.mark.skipif(os.name != 'posix', reason='signals')
def test_tsvz_serve_stops_cleanly_on_sighup(tmp_path):
	"""M2: closing the terminal (SIGHUP) stops the handler cleanly."""
	import signal
	p = str(tmp_path / 'h.tsvz')
	proc = _serve_process(p)
	proc.send_signal(signal.SIGHUP)
	assert proc.wait(10) == 0 and not os.path.exists(p + '.serve')


@pytest.mark.skipif(os.name != 'posix', reason='flock')
def test_serve_reports_a_file_system_without_locks(tmp_path, monkeypatch):
	"""M3: flock failing with ENOLCK is reported as such, not as another handler."""
	import errno

	def nolock(fd, operation):
		raise OSError(errno.ENOLCK, 'No locks available')
	monkeypatch.setattr(TSVZ.fcntl, 'flock', nolock)
	status, out, err = _run('serve', str(tmp_path / 'l.tsvz'))
	assert (status, out) == (1, '') and 'cannot lock' in err and 'already served' not in err


# ==========================================================================
# Write handler: Unix-domain sockets only
# ==========================================================================
def test_serve_has_no_tcp_option():
	with pytest.raises(TSVZ._CliUsageError):
		TSVZ._cliParseArgs(['serve', 'x.tsvz', '--x-tcp'])


def test_clients_ignore_a_pointer_with_a_tcp_address(tmp_path):
	"""Spec §21.3: a handler listens on a Unix-domain socket; other addresses are not used."""
	import socket
	p = str(tmp_path / 't.tsvz')
	_touch(p, b'a\t1\n')
	_write_pointer(p + '.serve', ['tsvz-handler\t1', 'address\ttcp:127.0.0.1:9', 'host\t' + socket.gethostname(),
								  'pid\t1'])
	status, out, err = _run('get', p, 'a')
	assert (status, out) == (0, 'a\t1\n') and 'not a Unix-domain socket' in err


@pytest.mark.skipif(not _UNIX, reason='Unix sockets')
def test_serve_has_no_auth_operation(tmp_path, served):
	handle = served(str(tmp_path / 'n.tsvz'))
	assert _raw(handle.address, b'auth\tx\nlen\n') == b'#2\tunknown operation auth\n0\n#0\n'


@pytest.mark.skipif(os.name != 'posix', reason='removes socket.AF_UNIX to imitate a platform without it')
def test_serve_needs_unix_domain_sockets(tmp_path):
	"""Without Unix-domain sockets, serve refuses before it touches the store or its pointer file."""
	p = str(tmp_path / 'w.tsvz')
	code = ("import socket, sys\ndel socket.AF_UNIX\nsys.path.insert(0, {!r})\nimport TSVZ\n"
			"sys.exit(TSVZ._cliMain(['serve', {!r}]))\n").format(HERE, p)
	r = subprocess.run([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
					   universal_newlines=True, timeout=20)
	assert r.returncode == 1 and 'Unix-domain sockets' in r.stderr
	assert not os.path.exists(p + '.serve') and not os.path.exists(p)


def test_legacy_upper_case_compressed_name_is_rewritten_as_compressed(tmp_path, capsys):
	# A codec suffix counts in any case: mapToFile rewrites DATA.TSV.GZ whole, as it does data.tsv.gz.
	p = str(tmp_path / 'DATA.TSV.GZ')
	d = TSVZ.TSVZed(p, header='key\tvalue')
	d['a'] = ['a', '1']
	d['b'] = ['b', '2']
	d.close()
	d = TSVZ.TSVZed(p, header='key\tvalue')
	del d['a']
	d.mapToFile()
	d.close()
	captured = capsys.readouterr()
	assert 'Failed to write' not in captured.out + captured.err
	assert 'does not end with' not in captured.out + captured.err
	with open(p, 'rb') as f:
		assert 'b\t2' in gzip.decompress(f.read()).decode().splitlines()



if __name__ == '__main__':
	sys.exit(pytest.main([__file__] + sys.argv[1:]))


def test_spec_a_read_starts_at_the_last_snapshot_part(tmp_path):
	# Spec §19.10.3: a reader may skip the parts before a part that begins with #_snapshot_#.
	# The parts here are crafted so that a skip shows: the snapshot part does not restate a,
	# which a producer may not do (§19.10.2a), but which tells a skip from a full replay.
	import gzip
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#h\tv\na\t1\n')
	_touch(p + '.1', b'#h\tv\n#_snapshot_#\nb\t2\n')
	_touch(p + '.2', b'c\t3\n')
	assert _run('read', p) == (0, 'b\t2\nc\t3\n', '')
	load = TSVZ._specLoad(p, '\t')
	assert load.headerLine == '#h\tv' and len(load.infos) == 3 and load.parts == [p, p + '.1', p + '.2']
	# The last snapshot part, compressed too.
	_touch(p + '.2', b'')
	os.remove(p + '.2')
	_touch(p + '.3.gz', gzip.compress(b'#h\tv\n#_SNAPSHOT_#\nd\t4\n'))
	assert _run('read', p) == (0, 'd\t4\n', '')
	os.remove(p + '.3.gz')
	# Not first (after a header comment), or with a value: not a snapshot part.
	for text in (b'b\t2\n#_snapshot_#\n', b'#_snapshot_#\tx\nb\t2\n', b'#h\tv\n#x\n#_snapshot_#\nb\t2\n'):
		_touch(p + '.1', text)
		assert _run('read', p) == (0, 'a\t1\nb\t2\n', ''), text


def test_spec_a_read_that_skips_history_writes_no_constructor_defaults(tmp_path):
	# The skipped parts may have declared defaults (here: and reset them); TSVZed cannot tell, so
	# it leaves the store's defaults as they are.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_defaults_#\tx\na\t1\n#_defaults_#\n')
	_touch(p + '.1', b'#_snapshot_#\n#_version_#\t1\na\t1\n')
	t = TSVZ.TSVZed(p, defaults=['k', 'd'])
	t.close()
	with open(p + '.1', 'rb') as f:
		assert f.read() == b'#_snapshot_#\n#_version_#\t1\na\t1\n'
	lite = TSVZ.TSVZedLite(p, defaults=['k', 'd'])
	lite['b'] = ['b', '2', '3']
	lite.close()
	with open(p + '.1', 'rb') as f:
		assert f.read() == b'#_snapshot_#\n#_version_#\t1\na\t1\nb\t2\t3\n'


def test_spec_writers_refuse_to_write_the_snapshot_marker(tmp_path):
	# Spec §19.10.2: a writer appending records never writes #_snapshot_#; as the first line of
	# a part it would make readers skip every part before it.
	p = str(tmp_path / 'm.tsvz')
	_touch(p, b'a\t1\nb\t2\n')
	_touch(p + '.1', b'')
	t = TSVZ.TSVZed(p)
	for key in ('#_snapshot_#', '#_SNAPSHOT_#'):
		with pytest.raises(ValueError, match='snapshot'):
			t[key] = ''
		with pytest.raises(ValueError, match='snapshot'):
			del t[key]
	t.close()
	with pytest.raises(ValueError, match='snapshot'):
		TSVZ.appendLinesTabularFile(p, [['#_snapshot_#']])
	for args in (('set', p, '#_snapshot_#'), ('delete', p, '#_snapshot_#')):
		status, out, err = _run(*args)
		assert status != 0 and 'snapshot' in err, (args, status, err)
	assert open(p + '.1', 'rb').read() == b''
	assert _run('read', p) == (0, 'a\t1\nb\t2\n', '')


def test_spec_verify_and_parts_see_the_parts_a_snapshot_part_lets_a_read_skip(tmp_path):
	# Spec §19.10.3-4: the skipped parts still belong to the store.
	p = str(tmp_path / 's.tsvz')
	_touch(p, b'#_checksum_crc32_#\na\t1\n#_checksum_crc32_#\t00000000\n')
	_touch(p + '.1', b'#_snapshot_#\nb\t2\n')
	assert _run('read', p) == (0, 'b\t2\n', '')
	status, out, err = _run('verify', p)
	assert status == 4, (out, err)
	status, out, err = _run('parts', p)
	assert status == 0 and len(out.splitlines()) == 2, out


# ==========================================================================
# Writing text that cannot be encoded as UTF-8 fails loudly (errors='strict')
# ==========================================================================
BAD_NAME = 'bad\udcff.txt'  # what os.fsdecode(b'bad\xff.txt') gives
BAD_SUFFIXES = ('.tsv', '.tsvz')


def _hash(suffix):
	return b'#' if suffix == '.tsvz' else b''  # a strict store's header is a comment


def _bytes(path):
	with open(path, 'rb') as f:
		return f.read()


def _expect_unencodable(excinfo, *shown):
	"""The error is a ValueError that names the text and the character, and points at both ways out."""
	message = str(excinfo.value)
	assert isinstance(excinfo.value, ValueError)
	for part in ('cannot write', 'as UTF-8', '\\udcff', '0xff', "errors='replace'") + shown:
		assert part in message, (part, message)
	assert 'bad\\udcff.txt' in message or 'bad\udcff' not in message  # never the raw surrogate


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
@pytest.mark.parametrize('call', ['appendTabularFile', 'appendLinesTabularFile', 'appendTSV'])
def test_append_refuses_an_unencodable_key_and_leaves_the_file(tmp_path, suffix, call):
	p = str(tmp_path / ('s' + suffix))
	_touch(p, b'id\tv\nk\t1\n')
	row = [BAD_NAME, 'x']
	with pytest.raises(ValueError) as e:
		getattr(TSVZ, call)(p, [row] if call == 'appendLinesTabularFile' else row, header='id\tv', strict=False)
	_expect_unencodable(e, 'the key', "'bad\\udcff.txt'")
	assert _bytes(p) == b'id\tv\nk\t1\n'


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_append_names_the_column_of_an_unencodable_value(tmp_path, suffix):
	p = str(tmp_path / ('s' + suffix))
	_touch(p, b'id\tname\nk\t1\n')
	with pytest.raises(ValueError) as e:
		TSVZ.appendLinesTabularFile(p, [['good', 'fine'], ['k2', BAD_NAME]], header='id\tname', strict=False)
	_expect_unencodable(e, "column 2 'name'", "key 'k2'")
	assert _bytes(p) == b'id\tname\nk\t1\n'


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_append_to_a_new_file_creates_nothing_when_the_row_is_refused(tmp_path, suffix):
	p = str(tmp_path / ('n' + suffix))
	with pytest.raises(ValueError):
		TSVZ.appendLinesTabularFile(p, [[BAD_NAME, 'x']], header='id\tv', createIfNotExist=True, strict=False)
	assert not os.path.exists(p)


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_creating_a_file_refuses_an_unencodable_header_and_creates_nothing(tmp_path, suffix):
	p = str(tmp_path / ('h' + suffix))
	with pytest.raises(ValueError) as e:
		TSVZ.appendLinesTabularFile(p, [['k', 'x']], header=['id', BAD_NAME], createIfNotExist=True, strict=False)
	_expect_unencodable(e, 'the header')
	assert not os.path.exists(p)
	with pytest.raises(ValueError):
		TSVZ.readTabularFile(p, header=['id', BAD_NAME], createIfNotExist=True, strict=False)
	assert not os.path.exists(p)


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_clear_refuses_an_unencodable_header_and_keeps_the_rows(tmp_path, suffix):
	p = str(tmp_path / ('c' + suffix))
	_touch(p, b'id\tv\nk\t1\n')
	with pytest.raises(ValueError) as e:
		TSVZ.clearTabularFile(p, header=['id', 'v' + BAD_NAME])
	_expect_unencodable(e, 'the header')
	assert _bytes(p) == b'id\tv\nk\t1\n'


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_scrub_refuses_an_unencodable_header_and_keeps_the_rows(tmp_path, suffix):
	p = str(tmp_path / ('r' + suffix))
	_touch(p, b'id\tv\nk\t1\n')
	with pytest.raises(ValueError):
		TSVZ.scrubTabularFile(p, header=['id', 'v' + BAD_NAME], verifyHeader=False)
	assert _bytes(p) == b'id\tv\nk\t1\n'


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_errors_replace_writes_a_question_mark_as_before(tmp_path, suffix):
	p = str(tmp_path / ('q' + suffix))
	TSVZ.appendLinesTabularFile(p, [[BAD_NAME, 'x']], header='id\tv', createIfNotExist=True, strict=False,
								errors='replace')
	assert _bytes(p) == _hash(suffix) + b'id\tv\nbad?.txt\tx\n'
	TSVZ.appendTabularFile(p, ['b2\udcff', 'y'], errors='replace')
	assert _bytes(p) == _hash(suffix) + b'id\tv\nbad?.txt\tx\nb2?\ty\n'
	q = str(tmp_path / ('q2' + suffix))  # clearing keeps a strict store's own header line, so clear a new file
	TSVZ.clearTabularFile(q, header=['id', 'v\udcff'], errors='replace')
	assert _bytes(q) == _hash(suffix) + b'id\tv?\n'


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_ordinary_non_ascii_text_still_round_trips(tmp_path, suffix):
	p = str(tmp_path / ('u' + suffix))
	rows = [['café', '中文'], ['\U0001f600', 'naïve']]
	TSVZ.appendLinesTabularFile(p, rows, header=['id', 'vé'], createIfNotExist=True, strict=False)
	assert _bytes(p) == (_hash(suffix).decode() + 'id\tvé\ncafé\t中文\n\U0001f600\tnaïve\n').encode('utf-8')
	back = TSVZ.readTabularFile(p, header=['id', 'vé'], strict=False)
	assert [back[k] for k in back] == rows
	t = TSVZ.TSVZed(p, header=['id', 'vé'])
	t['ü'] = ['ü', '☃']
	t.close()
	lite = TSVZ.TSVZedLite(p, header=['id', 'vé'])
	lite['ö'] = ['ö', '\U0001f4a9']
	assert lite['ö'] == ['ö', '\U0001f4a9']
	lite.close()
	back = TSVZ.readTabularFile(p, header=['id', 'vé'], strict=False)
	assert back['ü'] == ['ü', '☃'] and back['ö'] == ['ö', '\U0001f4a9']


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_reading_raw_invalid_bytes_still_replaces_and_warns(tmp_path, capsys, suffix):
	p = str(tmp_path / ('i' + suffix))
	_touch(p, _hash(suffix) + b'id\tv\nraw\xfe.txt\tx\n')
	data = TSVZ.readTabularFile(p, header='id\tv', strict=False)
	assert list(data) == ['raw�.txt']
	what = 'invalid UTF-8' if suffix == '.tsvz' else 'invalid utf8'
	assert _tsvz_warnings(capsys) == ['TSVZ warning: %s: %s replaced with U+FFFD (line 2)' % (p, what)]


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_tsvzed_set_refuses_an_unencodable_key_at_once(tmp_path, suffix):
	p = str(tmp_path / ('z' + suffix))
	t = TSVZ.TSVZed(p, header='id\tv', append_check_delay=0.01)
	t['ok'] = ['ok', '1']
	with pytest.raises(ValueError) as e:
		t[BAD_NAME] = [BAD_NAME, 'x']
	_expect_unencodable(e, 'the key')
	with pytest.raises(ValueError) as e:
		t['k2'] = ['k2', BAD_NAME]
	_expect_unencodable(e, "column 2 'v'")
	assert list(t) == ['ok'] and t.correctColumnNum == 2
	t['after'] = ['after', '2']  # the store is still usable
	t.close()
	assert t.appendThread.is_alive() is False
	assert _bytes(p) == _hash(suffix) + b'id\tv\nok\t1\nafter\t2\n'


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_tsvzed_refuses_an_unencodable_header_before_it_starts(tmp_path, suffix):
	p = str(tmp_path / ('zh' + suffix))
	before = threading.active_count()
	with pytest.raises(ValueError) as e:
		TSVZ.TSVZed(p, header=['id', BAD_NAME])
	_expect_unencodable(e, 'the header')
	assert not os.path.exists(p)
	assert threading.active_count() == before


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_tsvzed_errors_replace_writes_a_question_mark(tmp_path, suffix):
	p = str(tmp_path / ('zr' + suffix))
	t = TSVZ.TSVZed(p, header='id\tv', append_check_delay=0.01, errors='replace')
	t[BAD_NAME] = [BAD_NAME, 'x']
	t.close()
	assert _bytes(p) == _hash(suffix) + b'id\tv\nbad?.txt\tx\n'


def test_tsvzed_spec_marker_with_an_unencodable_value_is_refused(tmp_path):
	p = str(tmp_path / 'm.tsvz')
	t = TSVZ.TSVZed(p, append_check_delay=0.01)
	with pytest.raises(ValueError):
		t['#_defaults_#'] = ['#_defaults_#', BAD_NAME]
	assert t.defaults == ['#_defaults_#']
	t.close()
	assert _bytes(p) == b''


def test_tsvzed_legacy_rewrite_with_a_mutated_row_writes_nothing(tmp_path, capsys):
	p = str(tmp_path / 'w.tsv')
	t = TSVZ.TSVZed(p, header='id\tv', append_check_delay=0.01, rewrite_on_load=False)
	t['a'] = ['a', '1']
	t.commitAppendToFile()
	before = _bytes(p)
	t['a'][1] = BAD_NAME  # in place: no __setitem__ sees it
	t.dirty = True
	for method in (t.hardMapToFile, t.mapToFile):  # asked for by the program: it raises, as a refused set does
		with pytest.raises(TSVZ.TSVZEncodeError) as e:
			method()
		assert "cannot write 'bad\\udcff.txt' as UTF-8" in str(e.value)
	assert _bytes(p) == before
	assert not t.deSynced and t._refusedAt  # refused, not broken: no retry at every tick
	t.rewrite(force=True)  # one TSVZed makes itself is reported
	assert "cannot write 'bad\\udcff.txt' as UTF-8" in capsys.readouterr().out
	t.shutdownEvent.set()
	t.appendThread.join()


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_tsvzedlite_set_refuses_an_unencodable_key_and_changes_nothing(tmp_path, suffix):
	p = str(tmp_path / ('l' + suffix))
	lite = TSVZ.TSVZedLite(p, header='id\tv')
	lite['ok'] = ['ok', '1']
	before = _bytes(p)
	with pytest.raises(ValueError) as e:
		lite[BAD_NAME] = [BAD_NAME, 'x']
	_expect_unencodable(e, 'the key')
	with pytest.raises(ValueError) as e:
		lite['k2'] = ['k2', BAD_NAME]
	_expect_unencodable(e, "column 2 'v'")
	assert _bytes(p) == before and list(lite) == ['ok']
	lite.close()


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_tsvzedlite_refuses_an_unencodable_header_and_creates_nothing(tmp_path, suffix):
	p = str(tmp_path / ('lh' + suffix))
	with pytest.raises(ValueError) as e:
		TSVZ.TSVZedLite(p, header=['id', BAD_NAME])
	_expect_unencodable(e, 'the header')
	assert not os.path.exists(p)


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_tsvzedlite_errors_replace_writes_a_question_mark(tmp_path, suffix):
	p = str(tmp_path / ('lr' + suffix))
	lite = TSVZ.TSVZedLite(p, header='id\tv', errors='replace')
	lite[BAD_NAME] = [BAD_NAME, 'x']
	lite.close()
	assert _bytes(p) == _hash(suffix) + b'id\tv\nbad?.txt\tx\n'


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_cli_set_with_an_unencodable_argument_says_so_and_exits_non_zero(tmp_path, suffix):
	p = str(tmp_path / ('cli' + suffix))
	_touch(p, b'a\t1\n')
	for args in (('set', p, BAD_NAME, 'x'), ('set', p, 'k', BAD_NAME), ('append', p, 'k', BAD_NAME)):
		status, out, err = _run(*args)
		assert status == 1 and out == '', (args, status, out, err)
		assert 'cannot write' in err and '\\udcff' in err and 'Traceback' not in err, err
		assert _bytes(p) == b'a\t1\n'
	assert _run('set', p, 'café', '中')[0] == 0
	assert _run('get', p, 'café')[1] == 'café\t中\n'


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_cli_lookup_of_an_undecodable_argument_still_names_u_fffd(tmp_path, suffix):
	p = str(tmp_path / ('look' + suffix))
	_touch(p, b'\xff\t1\n')
	assert _run('get', p, '\udcff')[1] == '�\t1\n'  # the reader reads the byte as U+FFFD


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_tsvzclient_refuses_an_unencodable_write_and_sends_nothing(tmp_path, suffix):
	p = str(tmp_path / ('cl' + suffix))
	c = TSVZ.TSVZClient(p)
	c['ok'] = ['ok', '1']
	before = _bytes(p)
	with pytest.raises(ValueError) as e:
		c[BAD_NAME] = [BAD_NAME, 'x']
	_expect_unencodable(e)
	assert _bytes(p) == before
	c2 = TSVZ.TSVZClient(p, errors='replace')
	c2[BAD_NAME] = [BAD_NAME, 'x']
	assert b'bad?.txt\tx\n' in _bytes(p)


# --- review fixes -----------------------------------------------------------
def _stop(t):
	t.shutdownEvent.set()
	t.appendThread.join()


def test_tsvzed_legacy_commit_keeps_the_good_rows_around_a_refused_one(tmp_path, capsys):
	p = str(tmp_path / 'q.tsv')
	t = TSVZ.TSVZed(p, header='id\tv', append_check_delay=5, rewrite_on_load=False)
	t['x'] = ['x', '1']
	t['a'] = ['a', '1']
	t['y'] = ['y', '1']
	t['a'][1] = BAD_NAME  # in place: no __setitem__ sees it
	t.commitAppendToFile()
	assert _bytes(p) == b'id\tv\nx\t1\ny\t1\n'
	assert not t.appendQueue
	assert t.dirty  # a rewrite stores the row once it can be
	assert 'cannot write' in capsys.readouterr().out
	t['a'][1] = 'fixed'
	t.hardMapToFile()
	assert _bytes(p) == b'id\tv\nx\t1\na\tfixed\ny\t1\n'
	_stop(t)


class _FlipList(list):
	"""A row another thread changes between the check and the write: the first read is good, later ones are not."""
	reads = 0

	def __iter__(self):
		type(self).reads += 1
		return iter(['a', '1'] if type(self).reads == 1 else ['a', BAD_NAME])


@pytest.mark.parametrize('method', ['hardMapToFile', 'mapToFile'])
def test_tsvzed_legacy_rewrite_encodes_everything_before_it_touches_the_file(tmp_path, method):
	p = str(tmp_path / 'r.tsv')
	t = TSVZ.TSVZed(p, header='id\tv', append_check_delay=5, rewrite_on_load=False)
	t['a'] = ['a', '1']
	t['b'] = ['b', '2']
	t.commitAppendToFile()
	before = _bytes(p)
	_FlipList.reads = 0
	OrderedDict.__setitem__(t, 'a', _FlipList(['a', '1']))
	getattr(t, method)()
	assert _bytes(p) == before  # not emptied
	_stop(t)


def test_tsvzed_legacy_refused_rewrite_is_reported_once_and_retried_when_fixed(tmp_path, capsys):
	p = str(tmp_path / 'f.tsv')
	t = TSVZ.TSVZed(p, header='id\tv', append_check_delay=0.005, rewrite_on_load=False, rewrite_interval=0.05)
	t['a'] = ['a', '1']
	t['b'] = ['b', '2']
	assert _wait_for(lambda: b'b\t2' in _bytes(p))
	capsys.readouterr()
	t['a'][1] = BAD_NAME
	t.dirty = True
	time.sleep(0.6)
	out = capsys.readouterr().out
	assert out.count('cannot write') == 1, out
	assert 'Traceback' not in out
	t['a'][1] = 'fixed'
	t['c'] = ['c', '3']
	assert _wait_for(lambda: b'a\tfixed' in _bytes(p) and b'c\t3' in _bytes(p), 4)
	_stop(t)


# A superseded row ('ok' twice) makes the store dirty on load, so rewrite_on_load has something to rewrite.
LEGACY_KEY_ROW = b'id\tv\nr\xe9sum\xe9\t1\nok\t1\nok\t2\n'  # the first key is not ASCII: it reads as 'r\ufffdsum\ufffd'
LEGACY_CELL_ROW = b'id\tv\nk\tr\xe9sum\xe9\nok\t1\nok\t2\n'  # so is the value of k


def test_tsvzed_with_a_legacy_encoding_leaves_a_file_it_cannot_write_back_as_it_is(tmp_path, capsys):
	# U+FFFD is what reading puts for an undecodable byte; ASCII lacks it, and writing '?' would lose the
	# bytes and rename the key: the rewrite (rewrite_on_load is on by default) is refused, nothing is lost.
	p = str(tmp_path / 'e.tsv')
	_touch(p, LEGACY_KEY_ROW)
	t = TSVZ.TSVZed(p, header='id\tv', encoding='ascii', append_check_delay=0.01)
	out = capsys.readouterr().out
	assert out.count('Refused at') == 1 and 'U+FFFD' in out and 'ASCII' in out and 'Traceback' not in out, out
	assert _bytes(p) == LEGACY_KEY_ROW
	assert t['r�sum�'] == ['r�sum�', '1'] and t['ok'] == ['ok', '2']
	t.rewrite(force=True)
	for method in (t.hardMapToFile, t.mapToFile):
		with pytest.raises(TSVZ.TSVZEncodeError, match='U\\+FFFD'):
			method()
	assert capsys.readouterr().out.count('Refused at') == 0  # reported once, not at every try
	assert _bytes(p) == LEGACY_KEY_ROW
	t['new'] = ['new', '3']  # a row that can be written is appended as ever
	assert _wait_for(lambda: _bytes(p) == LEGACY_KEY_ROW + b'new\t3\n')
	t.close()
	assert _bytes(p) == LEGACY_KEY_ROW + b'new\t3\n'
	assert 'Refused' not in capsys.readouterr().out


@pytest.mark.parametrize('how', ['fix', 'remove'])
def test_tsvzed_with_a_legacy_encoding_writes_the_rest_once_the_bad_row_is_fixed_or_removed(tmp_path, capsys, how):
	p = str(tmp_path / (how + '.tsv'))
	_touch(p, LEGACY_CELL_ROW)
	t = TSVZ.TSVZed(p, header='id\tv', encoding='ascii', append_check_delay=0.005, rewrite_interval=0.05)
	assert 'Refused at' in capsys.readouterr().out and _bytes(p) == LEGACY_CELL_ROW
	time.sleep(0.3)
	assert _bytes(p) == LEGACY_CELL_ROW and 'Refused' not in capsys.readouterr().out
	if how == 'fix':
		t['k'] = ['k', 'fixed']
	else:
		del t['k']
	expected = b'id\tv\nk\tfixed\nok\t2\n' if how == 'fix' else b'id\tv\nok\t2\n'
	rows = lambda: b'\n'.join(line.rstrip() for line in _bytes(p).split(b'\n'))  # an in-place rewrite pads a shorter line
	assert _wait_for(lambda: rows() == expected, 4), _bytes(p)
	_stop(t)


@pytest.mark.parametrize('how', ['delete', 'update', 'pop', 'popitem', 'lone key'])
def test_tsvzed_with_a_legacy_encoding_refuses_to_name_a_key_it_read_as_u_fffd(tmp_path, how):
	# The tombstone or update of 'r�' would be written as 'r?', a key nobody can name again.
	p = str(tmp_path / 'n.tsv')
	original = b'id\tv\nok\t2\nr\xe9\t1\n'
	_touch(p, original)
	t = TSVZ.TSVZed(p, header='id\tv', encoding='ascii', append_check_delay=0.01, rewrite_on_load=False)
	key = 'r�'
	with pytest.raises(ValueError) as e:
		if how == 'delete':
			del t[key]
		elif how == 'update':
			t[key] = [key, '9']
		elif how == 'pop':
			t.pop(key)
		elif how == 'popitem':
			t.popitem()
		else:
			t[key] = key
	assert isinstance(e.value, TSVZ.TSVZEncodeError)
	assert 'U+FFFD' in str(e.value) and 'ASCII' in str(e.value), e.value
	assert t[key] == [key, '1'] and list(t) == ['ok', key]
	time.sleep(0.1)
	assert _bytes(p) == original
	t.close()
	assert _bytes(p) == original


def test_a_legacy_encoding_refuses_a_u_fffd_the_caller_gives(tmp_path):
	p = str(tmp_path / 'c.tsv')
	t = TSVZ.TSVZed(p, header='id\tv', encoding='cp1252', append_check_delay=0.01)
	with pytest.raises(ValueError) as e:
		t['k'] = ['k', 'a�b']
	assert 'U+FFFD' in str(e.value) and 'CP1252' in str(e.value)
	assert 'k' not in t
	with pytest.raises(ValueError):
		TSVZ.appendTabularFile(p, ['k', 'a�b'], encoding='cp1252')
	t.close()
	assert _bytes(p) == b'id\tv\n'
	u = str(tmp_path / 'u.tsv')
	t = TSVZ.TSVZed(u, header='id\tv', append_check_delay=0.01)  # UTF-8 can write it
	t['k'] = ['k', 'a�b']
	t.close()
	assert b'a\xef\xbf\xbdb' in _bytes(u)


@pytest.mark.parametrize('bad', ['Replace', None, 3, 'nonesuch'])
def test_errors_is_checked_where_it_is_given(tmp_path, bad):
	p = str(tmp_path / 'v.tsv')
	exc = (ValueError, TypeError)
	with pytest.raises(exc):
		TSVZ.TSVZed(p, errors=bad)
	with pytest.raises(exc):
		TSVZ.TSVZedLite(p, errors=bad)
	with pytest.raises(exc):
		TSVZ.appendTabularFile(p, ['a', '1'], createIfNotExist=True, errors=bad)
	with pytest.raises(exc):
		TSVZ.clearTabularFile(p, errors=bad)
	with pytest.raises(exc):
		TSVZ.scrubTabularFile(p, errors=bad)
	with pytest.raises(exc):
		TSVZ.TSVZClient(p, errors=bad)
	assert not os.path.exists(p)


def test_tsvzed_spec_strict_refuses_a_bad_record_and_writes_the_good_ones(tmp_path, capsys):
	p = str(tmp_path / 's.tsvz')
	t = TSVZ.TSVZed(p, append_check_delay=0.01)
	t['x'] = ['x', '1']
	t.memoryOnly = True  # the check at set time is skipped for a memory-only store
	t['bad'] = ['bad', BAD_NAME]
	t.memoryOnly = False
	t.appendQueue.append('bad\t' + BAD_NAME)  # a record queued before the toggle
	t['c'] = ['c', '1']
	assert _wait_for(lambda: b'c\t1\n' in _bytes(p))
	assert b'x\t1\n' in _bytes(p) and b'bad' not in _bytes(p)
	assert t.appendThread.is_alive()
	t.close()
	assert b'c\t1\n' in _bytes(p) and not t.appendQueue
	assert capsys.readouterr().out.count('cannot write') == 1


@pytest.mark.parametrize('mode', ['local', 'served'])
def test_client_reads_of_a_key_that_cannot_exist_agree_with_and_without_a_handler(tmp_path, served, mode):
	p = str(tmp_path / 'c.tsvz')
	if mode == 'served':
		if not hasattr(__import__('socket'), 'AF_UNIX'):
			pytest.skip('Unix sockets')
		served(p)
	c = TSVZ.TSVZClient(p)
	c['ok'] = ['ok', '1']
	assert (BAD_NAME in c) is False
	assert c.get(BAD_NAME, 'dflt') == 'dflt'
	t = TSVZ.TSVZed(p, append_check_delay=0.01)  # a missing key reads as the (here empty) defaults, for both
	assert c[BAD_NAME] == t[BAD_NAME] == [BAD_NAME, '']
	_stop(t)
	del c[BAD_NAME]  # no such key: not an error
	assert c.pop(BAD_NAME, 'dflt') == 'dflt'
	with pytest.raises(KeyError):
		c.pop(BAD_NAME)
	assert 'ok' in c
	c.close()


@pytest.mark.parametrize('mode', ['local', 'served'])
def test_client_takes_the_errors_handlers_it_accepts(tmp_path, served, mode):
	p = str(tmp_path / 'e.tsvz')
	if mode == 'served':
		if not hasattr(__import__('socket'), 'AF_UNIX'):
			pytest.skip('Unix sockets')
		served(p)
	for name, handler in (('r', 'replace'), ('s', 'surrogateescape'), ('b', 'backslashreplace')):
		c = TSVZ.TSVZClient(p, errors=handler)
		c[name] = [name, BAD_NAME]
		assert c[name][1].startswith('bad')
		c.close()


def test_tsvzed_compressed_legacy_file_keeps_monitoring_after_a_rewrite(tmp_path):
	p = str(tmp_path / 'z.tsv.gz')
	t = TSVZ.TSVZed(p, header='id\tv', append_check_delay=5)
	t['a'] = ['a', '1']
	assert t.monitor_external_changes
	t.mapToFile()
	assert t.monitor_external_changes
	_stop(t)


def _lite_with_a_stored_key(tmp_path, suffix, key):
	"""A TSVZedLite holding ``key``, which set would refuse: it is put in the index the way a load does."""
	p = str(tmp_path / ('lt' + suffix))
	lite = TSVZ.TSVZedLite(p, header='id\tv')
	lite['ok'] = ['ok', '1']
	lite['k'] = ['k', '2']
	lite.indexes[key] = lite.indexes['k']
	return lite, p


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
@pytest.mark.parametrize('how', ['del', 'pop', 'popitem_last', 'popitem_first'])
def test_tsvzedlite_refused_tombstone_leaves_memory_and_file_alone(tmp_path, suffix, how):
	lite, p = _lite_with_a_stored_key(tmp_path, suffix, BAD_NAME)
	if how == 'popitem_first':
		rest = dict(lite.indexes)
		lite.indexes.clear()
		lite.indexes[BAD_NAME] = rest.pop(BAD_NAME)
		lite.indexes.update(rest)
	before, keys = _bytes(p), list(lite.indexes)
	with pytest.raises(ValueError) as e:
		if how == 'del':
			del lite[BAD_NAME]
		elif how == 'pop':
			lite.pop(BAD_NAME)
		else:
			lite.popitem(last=(how == 'popitem_last'))
	_expect_unencodable(e)
	assert list(lite.indexes) == keys and _bytes(p) == before


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_tsvzedlite_refuses_unencodable_defaults_when_they_are_given(tmp_path, suffix):
	lite = TSVZ.TSVZedLite(str(tmp_path / ('d' + suffix)), header='id\tv')
	with pytest.raises(ValueError) as e:
		lite[TSVZ.DEFAULTS_INDICATOR_KEY] = [TSVZ.DEFAULTS_INDICATOR_KEY, BAD_NAME]
	_expect_unencodable(e, 'the defaults line')
	lite['a'] = ['a', '1']  # not blamed on a cell the caller never gave


def test_errors_survive_a_copy_a_pickle_and_fromkeys(tmp_path):
	import pickle
	p = str(tmp_path / 'p.tsv')
	lite = TSVZ.TSVZedLite(p, header='id\tv', errors='replace')
	assert pickle.loads(pickle.dumps(lite)).errors == 'replace'
	lite2 = TSVZ.TSVZedLite.fromkeys(['x'], ['x', 'y'], fileName=str(tmp_path / 'f.tsv'), header='id\tv', errors='replace')
	assert lite2.errors == 'replace'
	lite2[BAD_NAME] = [BAD_NAME, 'z']
	lite.close() if hasattr(lite, 'close') else None
	t = TSVZ.TSVZed.fromkeys(['x'], ['x', 'y'], fileName=str(tmp_path / 'g.tsv'), errors='replace')
	assert t.errors == 'replace'
	_stop(t)


def _count_encodes(monkeypatch):
	calls = []
	real = TSVZ._encodeRow

	def counting(cells, *args, **kwargs):
		calls.append(list(cells))
		return real(cells, *args, **kwargs)
	monkeypatch.setattr(TSVZ, '_encodeRow', counting)
	return calls


def test_legacy_writers_encode_each_row_once(tmp_path, monkeypatch):
	p = str(tmp_path / 'once.tsv')
	calls = _count_encodes(monkeypatch)
	TSVZ.appendLinesTabularFile(p, [['a', '1'], ['b', '2'], ['c', '3']], header='id\tv', createIfNotExist=True)
	rows = [c for c in calls if c[0] in 'abc']
	assert len(rows) == 3, calls
	del calls[:]
	TSVZ.scrubTabularFile(p, header='id\tv')
	assert len([c for c in calls if c[0] in 'abc']) == 3, calls
	del calls[:]
	t = TSVZ.TSVZed(p, header='id\tv', append_check_delay=5, rewrite_on_load=False)
	del calls[:]
	# A rewrite encodes every row once, under the lock, and writes the bytes it checked.
	t.hardMapToFile()
	assert len([c for c in calls if c[0] in 'abc']) == 3, calls
	del calls[:]
	t.mapToFile()
	assert len([c for c in calls if c[0] in 'abc']) == 3, calls
	_stop(t)


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
@pytest.mark.parametrize('extra', [(), ('--x-header', 'id\tv')])
def test_cli_refused_setdefault_creates_nothing(tmp_path, suffix, extra):
	p = str(tmp_path / ('sd' + suffix))
	status, out, err = _run('setdefault', p, 'k', BAD_NAME, *extra)
	assert status == 1 and out == '', (status, out, err)
	assert 'cannot write' in err and '\\udcff' in err and 'Traceback' not in err, err
	assert not os.path.exists(p) and os.listdir(str(tmp_path)) == []
	status, out, err = _run('setdefault', p, 'k', 'v', '--x-header', 'id\tv' + BAD_NAME)
	assert status == 1 and 'cannot write' in err and os.listdir(str(tmp_path)) == [], err
	_touch(p, b'a\t1\n')
	assert _run('setdefault', p, 'k', BAD_NAME)[0] == 1
	assert _bytes(p) == b'a\t1\n'


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_cli_setdefault_and_lone_set_find_the_key_a_reader_reads(tmp_path, suffix):
	p = str(tmp_path / ('live' + suffix))
	_touch(p, b'\xff\t1\ns\t2\n')
	assert _run('has', p, '\udcff')[0] == 0
	status, out, err = _run('setdefault', p, '\udcff', 'new')  # live: its row, nothing written
	assert (status, out) == (0, '\ufffd\t1\n') and err.count('U+FFFD') == 1, err
	assert _run('setdefault', p, 's', BAD_NAME)[0] == 0  # live: the value is not written
	assert _run('set', p, '\udcff')[0] == 0  # a lone key is a delete, as 'delete' does
	assert _run('has', p, '�')[0] != 0
	assert _run('get', p, 's')[1] == 's\t2\n'


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
@pytest.mark.parametrize('op', ['clear', 'delete'])
def test_cli_clear_and_delete_refuse_a_header_that_is_not_utf8(tmp_path, suffix, op):
	p = str(tmp_path / ('hd' + suffix))
	argv = (op, p) + (('k',) if op == 'delete' else ()) + ('--x-header', 'id\tv' + BAD_NAME)
	status, out, err = _run(*argv)
	assert status == 1 and 'cannot write' in err and 'Traceback' not in err, (status, err)
	assert os.listdir(str(tmp_path)) == []


@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix sockets')
def test_cli_setdefault_through_a_handler_looks_up_as_a_reader_reads(tmp_path, served):
	p = str(tmp_path / 'h.tsvz')
	_touch(p, b'\xff\t1\n')
	served(p)
	status, out, err = _run('setdefault', p, '\udcff', 'new')  # one get through the handler (see _cliSetdefaultLookup)
	assert (status, out, err) == (0, '�\t1\n', '')
	status, out, err = _run('setdefault', p, 'k', BAD_NAME)
	assert status == 1 and 'cannot write' in err, err
	assert _run('has', p, 'k')[0] != 0


# --- second review round: the rewrite takes its snapshot under the lock ----------
@pytest.mark.parametrize('method', ['hardMapToFile', 'mapToFile'])
def test_tsvzed_legacy_rewrite_does_not_cut_a_row_the_writer_appends_after_its_snapshot(tmp_path, method):
	p = str(tmp_path / 'race.tsv')
	t = TSVZ.TSVZed(p, header='key\tval', append_check_delay=0.001, rewrite_on_load=False)
	t['a'] = ['a', '1']
	assert _wait_for(lambda: b'a\t1' in _bytes(p))
	original = t.get_file_obj
	state = {'done': False}

	def hooked(modes='ab', *args, **kwargs):
		if modes in ('wb', 'r+b') and not state['done']:
			state['done'] = True
			t['late'] = ['late', '2']  # another thread sets a key just before the file is cut
			_wait_for(lambda: not t.appendQueue, 0.3)  # the writer must not get in before the rewrite is done
		return original(modes, *args, **kwargs)

	t.get_file_obj = hooked
	getattr(t, method)()
	t.get_file_obj = original
	assert state['done']
	assert _wait_for(lambda: b'late\t2' in _bytes(p)), _bytes(p)
	_stop(t)
	u = TSVZ.TSVZed(p, header='key\tval', append_check_delay=0.01, rewrite_on_load=False)
	assert dict(u) == {'a': ['a', '1'], 'late': ['late', '2']}
	_stop(u)


def test_tsvzed_legacy_concurrent_sets_survive_repeated_rewrites(tmp_path):
	# A rewrite in one thread and sets in another: no row set meanwhile is lost, whether the rewrite
	# reloads (an append of its own is not an external change), cuts the file, or fixes it in place.
	import threading
	p = str(tmp_path / 'many.tsv')
	t = TSVZ.TSVZed(p, header='key\tval', append_check_delay=0.0005, rewrite_on_load=False)
	stop = threading.Event()

	def setter():
		i = 0
		while not stop.is_set() and i < 1500:
			t['k%d' % i] = ['k%d' % i, str(i)]
			i += 1
			if i % 10 == 0:
				time.sleep(0.001)
		state['count'] = i

	state = {}
	thread = threading.Thread(target=setter)
	thread.start()
	rewrites = [lambda: t.rewrite(force=True), t.hardMapToFile, t.mapToFile]
	n = 0
	while thread.is_alive():
		rewrites[n % 3]()
		n += 1
	thread.join()
	t.close()
	u = TSVZ.TSVZed(p, header='key\tval', append_check_delay=0.01, rewrite_on_load=False)
	assert len(u) == state['count'], (len(u), state['count'])
	_stop(u)


@pytest.mark.parametrize('method', ['mapToFile', 'hardMapToFile'])
def test_tsvzed_legacy_refused_rewrite_does_not_delay_reloading_an_external_change(tmp_path, method):
	p = str(tmp_path / 'g.tsv')
	t = TSVZ.TSVZed(p, header='key\tval', rewrite_interval=30, append_check_delay=0.01, rewrite_on_load=False)
	t['a'] = ['a', '1']
	assert _wait_for(lambda: b'a\t1' in _bytes(p))
	t['a'][1] = BAD_NAME  # a row changed in place
	t.dirty = True
	with pytest.raises(TSVZ.TSVZEncodeError):
		getattr(t, method)()  # refused
	assert t._refusedAt is not None
	with open(p, 'ab') as f:  # another process appends
		f.write(b'ext\t9\n')
	assert _wait_for(lambda: 'ext' in t, 2.5), 'the external row was not reloaded while the refusal was pending'
	_stop(t)


def test_tsvzed_legacy_refusal_is_timed_on_the_monotonic_clock(tmp_path, monkeypatch):
	p = str(tmp_path / 'm.tsv')
	t = TSVZ.TSVZed(p, header='key\tval', rewrite_interval=0.05, append_check_delay=5, rewrite_on_load=False)
	t['a'] = ['a', '1']
	t.commitAppendToFile()
	t['a'][1] = BAD_NAME
	t.dirty = True
	with pytest.raises(TSVZ.TSVZEncodeError):
		t.mapToFile()  # refused
	calls = []
	original = t._mapToFile  # what rewrite() calls
	t._mapToFile = lambda *args, **kwargs: calls.append(1) or original(*args, **kwargs)
	real = time.time
	monkeypatch.setattr(TSVZ.time, 'time', lambda: real() + 3600)  # the wall clock jumps forward an hour
	time.sleep(0.1)
	t.rewrite()
	assert calls == []  # still inside the retry window: it is not the wall clock that counts
	_stop(t)


def test_tsvzed_fromkeys_passes_verbose_as_verbose_and_keeps_monitoring_on(tmp_path):
	# verbose used to land in the tenth positional parameter, monitor_external_changes
	for suffix in ('.tsv', '.tsvz'):
		t = TSVZ.TSVZed.fromkeys(['a', 'b'], ['x', 'y'], fileName=str(tmp_path / ('fk' + suffix)), header='id\tv\tw',
								 verbose=False, rewrite_on_load=False, rewrite_interval=0, append_check_delay=0.01)
		assert t.monitor_external_changes is True and t.verbose is False
		assert t.rewrite_interval == 0 and t.append_check_delay == 0.01 and t.rewrite_on_load is False
		_stop(t)
	t = TSVZ.TSVZed.fromkeys(['a'], ['x', 'y'], fileName=str(tmp_path / 'fv.tsv'), header='id\tv\tw', verbose=True)
	assert t.verbose is True and t.monitor_external_changes is True
	_stop(t)


# --- second review round: scrub looks at everything before it clears; a cut cell is not written ---------
SCRUB_ROWS = b'key\tv1\tv2\nk1\tx\t\nk2\ty\tz\n'


def test_legacy_scrub_refuses_defaults_it_cannot_write_before_it_empties_the_file(tmp_path):
	p = str(tmp_path / 'a.tsv')
	_touch(p, SCRUB_ROWS)
	with pytest.raises(TSVZ.TSVZEncodeError) as e:  # k1's empty v2 takes the default
		TSVZ.scrubTabularFile(p, header='key\tv1\tv2', defaults=['', '', BAD_NAME])
	assert "key 'k1'" in str(e.value) and "column 3 'v2'" in str(e.value), e.value
	assert _bytes(p) == SCRUB_ROWS  # the rows are still there
	TSVZ.scrubTabularFile(p, header='key\tv1\tv2', defaults=['', '', BAD_NAME], errors='replace')
	assert _bytes(p) == b'key\tv1\tv2\nk1\tx\tbad?.txt\nk2\ty\tz\n'


def test_legacy_scrub_with_a_legacy_encoding_refuses_a_row_it_read_and_keeps_the_file(tmp_path):
	p = str(tmp_path / 's.tsv')
	_touch(p, b'k\t\x81\n')  # 0x81 is undefined in cp1252: it reads as U+FFFD, which cp1252 lacks
	with pytest.raises(TSVZ.TSVZEncodeError) as e:
		TSVZ.scrubTabularFile(p, encoding='cp1252')
	assert 'U+FFFD' in str(e.value) and "key 'k'" in str(e.value)
	assert _bytes(p) == b'k\t\x81\n'
	TSVZ.scrubTabularFile(p, encoding='cp1252', errors='replace')
	assert b'k\t?\n' in _bytes(p)


def test_legacy_scrub_checks_a_header_it_keeps_and_a_header_it_writes(tmp_path):
	p = str(tmp_path / 'h.tsv')
	_touch(p, SCRUB_ROWS)
	# the file's own header is kept, so an argument that cannot be written is not written either
	TSVZ.scrubTabularFile(p, header='key\tv1\tv2é' + BAD_NAME, verifyHeader=True)
	assert _bytes(p) == SCRUB_ROWS


def test_cli_scrub_with_defaults_that_are_not_utf8_leaves_the_rows(tmp_path):
	p = str(tmp_path / 'c.tsv')
	_touch(p, SCRUB_ROWS)
	status, out, err = _run('--x-defaults', 'k\t\t' + BAD_NAME, 'scrub', p)
	assert status == 1 and 'cannot write' in err and 'Traceback' not in err, err
	assert _bytes(p) == SCRUB_ROWS


@pytest.mark.parametrize('create', [False, True])
def test_append_lines_checks_what_is_written_after_the_row_is_cut(tmp_path, create):
	# A third cell the header's width drops is never written, so it cannot be a reason to refuse the call.
	p = str(tmp_path / 'cut.tsv')
	if not create:
		_touch(p, b'key\tval\n')
	TSVZ.appendLinesTabularFile(p, [['k', 'v', 'extra' + BAD_NAME], ['j', 'w']], header='key\tval', createIfNotExist=create)
	assert _bytes(p) == b'key\tval\nk\tv\nj\tw\n'
	with pytest.raises(ValueError) as e:  # the cell that is written still is
		TSVZ.appendLinesTabularFile(p, [['z', BAD_NAME, 'extra']], header='key\tval')
	assert "column 2 'val'" in str(e.value)
	assert _bytes(p) == b'key\tval\nk\tv\nj\tw\n'


@pytest.mark.parametrize('cls', ['TSVZed', 'TSVZedLite'])
def test_setitem_checks_the_row_that_is_written_after_it_is_cut_to_the_column_count(tmp_path, cls):
	p = str(tmp_path / 'cut.tsv')
	kwargs = {'append_check_delay': 0.01, 'rewrite_on_load': False} if cls == 'TSVZed' else {}
	t = getattr(TSVZ, cls)(p, header='key\tval', correctColumnNum=2, strict=False, **kwargs)
	t['k'] = ['k', 'v', 'extra' + BAD_NAME]  # the third cell is dropped, never written
	assert t['k'] == ['k', 'v']
	with pytest.raises(TSVZ.TSVZEncodeError):
		t['j'] = ['j', BAD_NAME, 'extra']
	assert 'j' not in t
	if cls == 'TSVZed':
		_stop(t)
	else:
		t.fileObj.close()
	assert _bytes(p) == b'key\tval\nk\tv\n'


@pytest.mark.parametrize('cls', ['TSVZed', 'TSVZedLite'])
def test_a_refused_row_leaves_the_column_count_unset(tmp_path, cls):
	p = str(tmp_path / 'n.tsv')
	kwargs = {'append_check_delay': 0.01, 'rewrite_on_load': False} if cls == 'TSVZed' else {}
	t = getattr(TSVZ, cls)(p, **kwargs)
	assert t.correctColumnNum == -1
	with pytest.raises(TSVZ.TSVZEncodeError):
		t['k'] = ['k', BAD_NAME]
	assert t.correctColumnNum == -1 and len(t) == 0
	t['j'] = ['j', '1', '2']
	assert t.correctColumnNum == 3
	if cls == 'TSVZed':
		_stop(t)


# --- second review round: a .tsvz clear or scrub looks only at what it writes -------------------------
def test_spec_clear_and_scrub_do_not_refuse_a_header_argument_the_store_does_not_take(tmp_path):
	# The store has its own header line, which is kept: an argument that could not be written is not written.
	p = str(tmp_path / 'h.tsvz')
	for operation in ('clear', 'scrub'):
		_touch(p, b'#id\tv\nk\t1\n')
		getattr(TSVZ, operation + 'TabularFile')(p, header=['id', 'v' + BAD_NAME])
		assert _bytes(p) == (b'#id\tv\n' if operation == 'clear' else b'#id\tv\n#_version_#\t1\nk\t1\n')
	_touch(p, b'#id\tv\nk\t1\n')
	assert _run('--x-header', 'id\tv' + BAD_NAME, 'clear', p) == (0, '', '')
	assert _bytes(p) == b'#id\tv\n'
	_touch(p, b'#id\tv\nk\t1\n')
	assert _run('--x-header', 'id\tv' + BAD_NAME, 'scrub', p)[0] == 0
	# a store without one gets it written, and is refused when it cannot be
	_touch(p, b'k\t1\n')
	for operation in ('clear', 'scrub'):
		with pytest.raises(ValueError) as e:
			getattr(TSVZ, operation + 'TabularFile')(p, header=['id', 'v' + BAD_NAME])
		assert 'the header' in str(e.value)
		assert _bytes(p) == b'k\t1\n'


def test_spec_scrub_names_a_refused_cell_and_keeps_no_copy_of_a_part_it_did_not_touch(tmp_path):
	p = str(tmp_path / 'd.tsvz')
	raw = b'k\tv\n\0\0\0j\tw\n'  # damaged: a scrub keeps the original as d.tsvz.damaged-<time> before it rewrites
	_touch(p, raw)
	with pytest.raises(TSVZ.TSVZEncodeError) as e:
		TSVZ.scrubTabularFile(p, taskDic={'z': ['z', BAD_NAME]})
	assert "column 2 of key 'z'" in str(e.value) and 'k\\tv' not in str(e.value), e.value
	assert _bytes(p) == raw and os.listdir(str(tmp_path)) == ['d.tsvz']
	TSVZ.scrubTabularFile(p, taskDic={'z': ['z', 'fine']})
	assert any(n.startswith('d.tsvz.damaged-') for n in os.listdir(str(tmp_path)))


# --- second review round: TSVZClient answers for a key no store holds as TSVZed does ------------------
def _client(p, served, mode, **kwargs):
	if mode == 'served':
		if not hasattr(__import__('socket'), 'AF_UNIX'):
			pytest.skip('Unix sockets')
		served(p)
	return TSVZ.TSVZClient(p, **kwargs)


@pytest.mark.parametrize('mode', ['local', 'served'])
def test_client_setdefault_of_a_live_key_ignores_a_default_it_could_not_write(tmp_path, served, mode):
	p = str(tmp_path / 'q.tsvz')
	_touch(p, b'k\tv\n')
	c = _client(p, served, mode)
	assert c.setdefault('k', ['k', 'x' + BAD_NAME]) == ['k', 'v']  # as TSVZed, TSVZedLite and the CLI
	assert c.setdefault('k ', ['k ', BAD_NAME]) == ['k', 'v']
	with pytest.raises(TSVZ.TSVZEncodeError):  # not live: the write is refused, nothing is written
		c.setdefault('new', ['new', BAD_NAME])
	with pytest.raises(TSVZ.TSVZEncodeError):
		c.setdefault(BAD_NAME, [BAD_NAME, 'x'])
	assert 'new' not in c and len(c) == 1
	c.close()
	assert _bytes(p) == b'k\tv\n'


DEFAULTS_STORE = b'#_return_defaults_when_missing_#\ttrue\n#_defaults_#\tD\nk\tv\n'


@pytest.mark.parametrize('mode', ['local', 'served'])
def test_client_reads_a_key_that_cannot_exist_as_a_missing_key_of_the_store(tmp_path, served, mode):
	p = str(tmp_path / 'r.tsvz')
	_touch(p, DEFAULTS_STORE)
	t = TSVZ.TSVZed(p, append_check_delay=0.01)
	l = TSVZ.TSVZedLite(p)
	c = _client(p, served, mode)
	key = 'z' + BAD_NAME[3:]
	assert t[key] == l[key] == [key, 'D']
	assert c[key] == [key, 'D']  # the store returns its defaults for a missing key (§14.5)
	assert c['plain'] == ['plain', 'D']
	assert (key in c, c.get(key, 'dflt')) == (key in t, t.get(key, 'dflt')) == (False, 'dflt')
	c.close()
	_stop(t)


@pytest.mark.parametrize('mode', ['local', 'served'])
def test_client_reads_a_key_that_cannot_exist_as_a_missing_key_without_defaults(tmp_path, served, mode):
	p = str(tmp_path / 'n.tsvz')
	raw = b'#_return_defaults_when_missing_#\tfalse\nk\tv\n'
	_touch(p, raw)
	c = _client(p, served, mode)
	key = 'z' + BAD_NAME[3:]
	with pytest.raises(KeyError):
		c[key]
	with pytest.raises(KeyError):
		c.pop(key)
	assert c.pop(key, 'dflt') == 'dflt'
	del c[key]
	c.close()
	assert _bytes(p) == raw


@pytest.mark.parametrize('mode', ['local', 'served'])
def test_client_errors_replace_reads_deletes_and_pops_the_key_it_wrote(tmp_path, served, mode):
	p = str(tmp_path / 'e.tsvz')
	_touch(p, b'x\t0\n')
	c = _client(p, served, mode, errors='replace')
	c['k\udcff'] = ['k\udcff', 'v']  # stores 'k?'
	assert c['k\udcff'] == ['k?', 'v'] and 'k\udcff' in c and c.get('k\udcff') == ['k?', 'v']
	del c['k\udcff']  # TSVZed under errors='replace' writes the tombstone of 'k?' too
	assert 'k?' not in c and 'k\udcff' not in c
	c['j\udcff'] = ['j\udcff', 'w']
	assert c.pop('j\udcff') == ['j?', 'w']
	assert list(c.keys()) == ['x']
	c.close()
	assert _bytes(p) == b'x\t0\nk?\tv\nk?\nj?\tw\nj?\n'


@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix sockets')
def test_cli_routed_setdefault_is_one_atomic_request(tmp_path, served, monkeypatch):
	p = str(tmp_path / 'one.tsvz')
	_touch(p, b'\xff\t1\n')
	served(p)
	sent = []
	request = TSVZ._ServeConnection.request

	def spy(self, line, bulk=None):
		sent.append(line.split('\t')[0:3])
		return request(self, line, bulk)

	monkeypatch.setattr(TSVZ._ServeConnection, 'request', spy)
	assert _run('setdefault', p, 'new', 'v') == (0, 'new\tv\n', '')
	assert [line[1] if line[0].startswith('--') else line[0] for line in sent] == ['setdefault']  # no probe first
	del sent[:]
	# Arguments that hold a byte a request cannot carry: such a setdefault never writes, so it is one get of
	# KEY as a reader reads it (U+FFFD for the byte), answered by the handler; a missing KEY is then refused.
	assert _run('setdefault', p, '\udcff', 'ignored' + BAD_NAME)[:2] == (0, '�\t1\n')
	status, out, err = _run('setdefault', p, 'k', BAD_NAME)
	assert status == 1 and 'cannot write' in err, err
	assert sent == [['get', '�'], ['get', 'k']]
	assert _run('has', p, 'k')[0] != 0


@pytest.mark.parametrize('last', [True, False])
def test_tsvzedlite_popitem_refuses_without_changing_the_index_or_the_file(tmp_path, last):
	p = str(tmp_path / 'pi.tsv')
	_touch(p, b'a\t1\nb\t2\n')
	t = TSVZ.TSVZedLite(p, header='')
	t.indexes['z' + BAD_NAME] = ['z' + BAD_NAME, 'x']  # an in-memory row whose tombstone cannot be written
	if not last:
		t.indexes = dict(reversed(list(t.indexes.items())))
	order = list(t.indexes)
	with pytest.raises(TSVZ.TSVZEncodeError):
		t.popitem(last=last)
	assert list(t.indexes) == order
	assert _bytes(p) == b'a\t1\nb\t2\n'
	if last:
		t.indexes.pop('z' + BAD_NAME)
		assert t.popitem()[0] == 'b'


@pytest.mark.skipif(sys.version_info >= (3, 8), reason='a dict is reversible')
def test_tsvzedlite_popitem_does_not_list_the_keys_before_python_3_8(tmp_path, monkeypatch):
	p = str(tmp_path / 'o.tsv')
	_touch(p, ''.join('k%d\t%d\n' % (i, i) for i in range(50)).encode())
	t = TSVZ.TSVZedLite(p, header='')
	listed = []
	real = list

	class Meta(type):
		def __instancecheck__(cls, obj):
			return isinstance(obj, real)

	class CountingList(metaclass=Meta):  # the module's `list`: builds real lists and notes a pass over the keys
		def __new__(cls, *args):
			listed.append(bool(args) and args[0] is t.indexes)
			return real(*args)

	monkeypatch.setattr(TSVZ, 'list', CountingList, raising=False)
	while len(t):
		t.popitem()
	assert True not in listed  # list(self.indexes) at every pop made emptying a store quadratic


# --- third review round: the legacy writer's locks, stamps and queue ----------
def _stopped(p, **kwargs):
	"""A TSVZed whose background writer is stopped, so a test drives every write itself."""
	kwargs.setdefault('header', 'key\tval')
	kwargs.setdefault('append_check_delay', 0.001)
	kwargs.setdefault('rewrite_on_load', False)
	t = TSVZ.TSVZed(p, **kwargs)
	_stop(t)
	return t


def _dirty(t):
	"""Give ``t`` a row in the file and a newer one queued: a rewrite has something to change."""
	t['a'] = ['a', '1']
	t.commitAppendToFile()
	t['a'] = ['a', '2']
	t.commitAppendToFile()
	assert t.dirty


def _open_fds():
	return len(os.listdir('/proc/self/fd'))


class _InterruptingFile(object):
	"""A file whose writes raise KeyboardInterrupt (Ctrl-C in the middle of a rewrite)."""

	def __init__(self, f):
		self._f = f

	def write(self, data):
		raise KeyboardInterrupt

	def __getattr__(self, name):
		return getattr(self._f, name)


@pytest.mark.parametrize('method', ['mapToFile', 'hardMapToFile'])
def test_tsvzed_legacy_rewrite_lets_go_of_the_lock_and_the_file_when_interrupted(tmp_path, method):
	p = str(tmp_path / 'ki.tsv')
	t = _stopped(p)
	_dirty(t)
	original = t.get_file_obj
	opened = []

	def hooked(modes='ab', locked=False, **kwargs):
		f = original(modes, locked, **kwargs)
		if modes in ('wb', 'r+b'):
			opened.append(f)
			return _InterruptingFile(f)
		return f

	t.get_file_obj = hooked
	try:
		with pytest.raises(KeyboardInterrupt):
			getattr(t, method)()
		held = t.writeLock.locked()
	finally:
		t.get_file_obj = original
		if t.writeLock.locked():
			t.writeLock.release()  # so that the rest of the run does not hang where it fails
	assert not held, 'writeLock is still held: close(), atexit and __del__ would hang'
	assert opened and opened[0].closed
	t.mapToFile()  # and the next rewrite works
	assert _bytes(p) == b'key\tval\na\t2\n'


@pytest.mark.skipif(not os.path.isdir('/proc/self/fd'), reason='counts open descriptors in /proc')
@pytest.mark.parametrize('method', ['mapToFile', 'hardMapToFile'])
def test_tsvzed_legacy_rewrite_whose_file_lock_fails_never_writes_unlocked(tmp_path, method, monkeypatch, capsys):
	import errno
	p = str(tmp_path / 'nolock.tsv')
	t = _stopped(p)
	_dirty(t)
	before = _bytes(p)
	fds = _open_fds()

	def failing(f):
		raise OSError(errno.ENOLCK, 'No locks available')  # NFS without a lock daemon

	monkeypatch.setattr(TSVZ, '_lockFile', failing)
	getattr(t, method)()
	monkeypatch.undo()
	out = capsys.readouterr().out
	assert not t.writeLock.locked()
	assert _open_fds() == fds, 'the file was left open'
	assert out.count('No locks available') == 1 and 'NoneType' not in out and 'failback' not in out, out
	assert _bytes(p) == before  # it never wrote to a file it could not lock, nor emptied it
	assert not t._externalChange()
	assert t.dirty and dict(t) == {'a': ['a', '2']}
	getattr(t, method)()
	assert _bytes(p) == b'key\tval\na\t2\n'


@pytest.mark.parametrize('method', ['mapToFile', 'hardMapToFile'])
def test_tsvzed_legacy_rewrite_that_cannot_open_the_file_reports_why_once(tmp_path, method, monkeypatch, capsys):
	p = str(tmp_path / 'noopen.tsv')
	t = _stopped(p)
	_dirty(t)
	before = _bytes(p)
	real = TSVZ.openFileAsCompressed

	def failing(fileName, mode='rb', *args, **kwargs):
		if mode in ('wb', 'r+b'):
			raise PermissionError(13, 'Permission denied')
		return real(fileName, mode, *args, **kwargs)

	monkeypatch.setattr(TSVZ, 'openFileAsCompressed', failing)
	monkeypatch.setattr(TSVZ, '_openUntruncated', lambda fileName: failing(fileName, 'wb'), raising=False)
	getattr(t, method)()
	monkeypatch.undo()
	out = capsys.readouterr().out
	assert not t.writeLock.locked()
	assert out.count('Permission denied') == 1 and 'NoneType' not in out and 'failback' not in out, out
	assert _bytes(p) == before and t.dirty



def _touch_mtime(f):
	"""What a write that fails with ENOSPC does on Linux all the same: change the file's modification time."""
	now = max(time.time_ns() if hasattr(time, 'time_ns') else int(time.time() * 1e9), os.fstat(f.fileno()).st_mtime_ns + 1000)
	os.utime(f.fileno(), ns=(now, now))


class _FullDiskFile(object):
	"""A file on a full disk: a write stores its first line and fails, and so does every flush; each changes the time."""

	def __init__(self, f):
		self._f = f

	def write(self, data):
		import errno
		data = bytes(data)
		self._f.write(data[:data.find(b'\n') + 1])
		self._f.flush()
		_touch_mtime(self._f)
		raise OSError(errno.ENOSPC, 'No space left on device')

	def flush(self):
		import errno
		_touch_mtime(self._f)
		raise OSError(errno.ENOSPC, 'No space left on device')

	def __getattr__(self, name):
		return getattr(self._f, name)


@pytest.mark.parametrize('method', ['rewrite', 'hardMapToFile'])
def test_tsvzed_legacy_rewrite_that_fails_half_way_is_made_again_from_memory(tmp_path, monkeypatch, capsys, method):
	p = str(tmp_path / 'full.tsv')
	t = _stopped(p)
	for i in range(50):
		t['k%d' % i] = ['k%d' % i, str(i)]
	t.commitAppendToFile()
	t['k1'] = ['k1', 'upd']  # an update: dirty
	t.commitAppendToFile()
	rows = dict(t)
	realOpen = TSVZ.openFileAsCompressed

	def opening(fileName, mode='rb', *args, **kwargs):
		f = realOpen(fileName, mode, *args, **kwargs)
		return _FullDiskFile(f) if mode in ('r+b', 'wb') else f

	monkeypatch.setattr(TSVZ, 'openFileAsCompressed', opening)
	if hasattr(TSVZ, '_openUntruncated'):
		realUntruncated = TSVZ._openUntruncated
		monkeypatch.setattr(TSVZ, '_openUntruncated', lambda fileName: _FullDiskFile(realUntruncated(fileName)))
	if method == 'rewrite':
		t.rewrite(force=True)  # the background writer's: mapToFile, then hardMapToFile when that fails
	else:
		t.hardMapToFile()
	monkeypatch.undo()
	capsys.readouterr()
	assert not t.writeLock.locked()
	assert TSVZ.readTabularFile(p, header='key\tval', verbose=False) != rows and t.deSynced and t.dirty  # the disk filled up
	assert not t._externalChange(), "the rewrite's own damage was taken for another process's change"
	t.rewrite()  # the next tick, once there is space again
	assert dict(t) == rows
	assert TSVZ.readTabularFile(p, header='key\tval', verbose=False) == rows and not t.deSynced and not t.dirty


@pytest.mark.parametrize('suffix', ['tsv', 'tsv.gz'])
@pytest.mark.parametrize('how', ['lock fails', 'interrupted'])
@pytest.mark.parametrize('method', ['hardMapToFile', 'clear_file', 'clear'])
def test_tsvzed_legacy_rewrite_empties_the_file_only_once_it_is_locked(tmp_path, monkeypatch, capsys, suffix, how, method):
	import errno
	p = str(tmp_path / ('lockwait.' + suffix))
	t = _stopped(p)
	t['a'] = ['a', '1']
	t.commitAppendToFile()
	before = _bytes(p)
	real = TSVZ._lockFile

	def locking(f):
		mode = getattr(f, 'mode', '')
		if 'w' in str(mode) or mode == 'rb+':  # the rewrite's lock, which another writer holds
			if how == 'interrupted':
				raise KeyboardInterrupt  # Ctrl-C while it waits
			raise OSError(errno.ENOLCK, 'No locks available')
		real(f)

	monkeypatch.setattr(TSVZ, '_lockFile', locking)
	try:
		getattr(t, method)()
	except KeyboardInterrupt:
		pass
	monkeypatch.undo()
	capsys.readouterr()
	assert not t.writeLock.locked()
	assert _bytes(p) == before, 'the file was emptied before it was locked'
	assert not t._externalChange()
	t.mapToFile()
	assert TSVZ.readTabularFile(p, header='key\tval', verbose=False) == dict(t)


def test_tsvzed_legacy_append_that_cannot_open_the_file_keeps_the_rows_queued(tmp_path, monkeypatch, capsys):
	p = str(tmp_path / 'q.tsv')
	t = _stopped(p)
	t['b'] = ['b', '1']
	monkeypatch.setattr(TSVZ, 'openFileAsCompressed', lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, 'Permission denied')))
	t.commitAppendToFile()
	monkeypatch.undo()
	out = capsys.readouterr().out
	assert 'Permission denied' in out and 'NoneType' not in out, out
	assert not t.writeLock.locked() and list(t.appendQueue) == [['b', '1']]
	t.commitAppendToFile()
	assert _bytes(p) == b'key\tval\nb\t1\n'



class _ErrorLog(object):
	"""A teeLogger that keeps the error lines."""

	def __init__(self):
		self.errors = []

	def teelog(self, message, level='info'):
		if level == 'error':
			self.errors.append(message)


def test_tsvzed_legacy_append_that_keeps_failing_is_reported_once_and_retried_less_often(tmp_path, monkeypatch):
	p = str(tmp_path / 'deny.tsv')
	log = _ErrorLog()
	t = TSVZ.TSVZed(p, header='key\tval', rewrite_on_load=False, append_check_delay=0.001, teeLogger=log)
	t['a'] = ['a', '1']
	t.commitAppendToFile()
	real = TSVZ.openFileAsCompressed
	opens = []

	def denying(fileName, mode='rb', *args, **kwargs):
		if mode != 'rb':
			opens.append(mode)
			raise PermissionError(13, 'Permission denied')
		return real(fileName, mode, *args, **kwargs)

	monkeypatch.setattr(TSVZ, 'openFileAsCompressed', denying)
	t['b'] = ['b', '1']
	time.sleep(0.5)  # some 400 ticks of the background writer
	monkeypatch.undo()
	assert len(log.errors) == 1 and 'Permission denied' in log.errors[0], log.errors
	assert 1 <= len(opens) <= 6, len(opens)  # tried after 0.1 s, 0.2 s, 0.4 s, ... not at every tick
	assert list(t.appendQueue) == [['b', '1']]
	t.commitAppendToFile()  # an explicit call does not wait
	assert _bytes(p) == b'key\tval\na\t1\nb\t1\n'
	monkeypatch.setattr(TSVZ, 'openFileAsCompressed', denying)
	t['c'] = ['c', '1']
	t.commitAppendToFile()
	monkeypatch.undo()
	assert len(log.errors) == 2, 'a failure after one that went through is reported again'
	t.close()
	assert _bytes(p) == b'key\tval\na\t1\nb\t1\nc\t1\n'


def test_tsvzed_legacy_reload_that_keeps_failing_is_reported_once_and_retried_less_often(tmp_path, monkeypatch):
	p = str(tmp_path / 'strict.tsv')
	log = _ErrorLog()
	t = TSVZ.TSVZed(p, header='key\tval', rewrite_on_load=False, append_check_delay=0.001, strict=True, teeLogger=log)
	time.sleep(0.05)
	reads = []
	real = TSVZ.readTabularFile

	def reading(*args, **kwargs):
		reads.append(1)
		return real(*args, **kwargs)

	monkeypatch.setattr(TSVZ, 'readTabularFile', reading)
	with open(p, 'wb') as f:
		f.write(b'other\theader\n')  # another process: a strict store cannot be reloaded
	time.sleep(0.5)
	monkeypatch.undo()
	assert 1 <= len(reads) <= 6, len(reads)
	failures = [e for e in log.errors if e.startswith('Failed') and 'Header mismatch' in e]
	assert len(failures) == 1, log.errors
	with open(p, 'wb') as f:
		f.write(b'key\tval\nx\t1\n')  # put right
	deadline = time.time() + 5
	while 'x' not in t and time.time() < deadline:
		time.sleep(0.01)
	assert t['x'] == ['x', '1']
	t.close()


def test_tsvzed_legacy_reload_after_an_external_change_keeps_rows_set_since_the_last_append(tmp_path):
	p = str(tmp_path / 'keep.tsv')
	t = _stopped(p)
	_dirty(t)
	with open(p, 'ab') as g:
		g.write(b'ext\tB\n')  # so rewrite() reloads
	real = t.commitAppendToFile
	state = {'done': False}

	def commit():
		out = real()
		if not state['done']:
			state['done'] = True
			t['late'] = ['late', '1']  # another thread sets a key between rewrite()'s append and its reload
			del t['a']
		return out

	t.commitAppendToFile = commit
	t.rewrite(force=True)
	t.commitAppendToFile = real
	assert dict(t) == {'ext': ['ext', 'B'], 'late': ['late', '1']}
	t['ext'] = ['ext', 'C']  # dirty
	t.rewrite(force=True)  # a later rewrite keeps it
	t.commitAppendToFile()
	assert dict(TSVZ.readTabularFile(p, header='key\tval')) == {'ext': ['ext', 'C'], 'late': ['late', '1']}


def test_tsvzed_legacy_set_during_a_reload_is_written(tmp_path, monkeypatch):
	import threading
	p = str(tmp_path / 'during.tsv')
	t = _stopped(p)
	_dirty(t)
	real = TSVZ.readTabularFile

	def reading(*args, **kwargs):
		setter = threading.Thread(target=lambda: t.__setitem__('during', ['during', '1']))
		setter.start()  # the main thread sets a key while the background writer reads the file
		setter.join()
		return real(*args, **kwargs)

	monkeypatch.setattr(TSVZ, 'readTabularFile', reading)
	t.reload()
	monkeypatch.undo()
	assert not t.memoryOnly and t['during'] == ['during', '1']
	t.commitAppendToFile()
	assert b'during\t1' in _bytes(p), 'the set was kept in memory only'


_OTHER_WRITER = r'''
import fcntl, os, sys, time
path, stop = sys.argv[1], sys.argv[2]
n = 0
while not os.path.exists(stop):
	with open(path, 'ab') as g:
		fcntl.lockf(g, fcntl.LOCK_EX)
		g.write(b'ext%d\tB\n' % n)
		g.flush()
		fcntl.lockf(g, fcntl.LOCK_UN)
	n += 1
	time.sleep(0.003)
print(n)
'''


@pytest.mark.skipif(os.name != 'posix', reason='uses fcntl')
def test_tsvzed_legacy_rows_survive_rewrites_and_reloads_for_another_process_stress(tmp_path):
	# The other writer is a process: lockf locks belong to a process, so a thread of this one would lose its
	# lock whenever TSVZed closes a descriptor of the file.
	import subprocess
	p = str(tmp_path / 'two.tsv')
	stop = str(tmp_path / 'stop')
	t = TSVZ.TSVZed(p, header='key\tv', append_check_delay=0.0005, rewrite_interval=0.0001, rewrite_on_load=False)
	other = subprocess.Popen([sys.executable, '-c', _OTHER_WRITER, p, stop], stdout=subprocess.PIPE)
	try:
		time.sleep(0.1)
		for i in range(2000):
			t['a%d' % i] = ['a%d' % i, 'A']
			if i % 5 == 0:
				t['a0'] = ['a0', str(i)]  # an overwrite keeps it dirty, so rewrites happen
			if i % 10 == 0:
				time.sleep(0.0005)
	finally:
		_touch(stop)
		count = int(other.communicate(timeout=30)[0])
	time.sleep(0.1)
	t.close()
	back = TSVZ.readTabularFile(p, header='key\tv')
	assert count > 10
	assert [k for k in ('a%d' % i for i in range(2000)) if k not in back] == []
	assert [k for k in ('ext%d' % j for j in range(count)) if k not in back] == []


def test_tsvzed_legacy_change_right_after_a_rewrite_is_not_taken_for_seen(tmp_path):
	# mapToFile noted the file's time again after release_file_obj let go of the lock: a change another
	# process made in between was never reloaded, and the next rewrite erased it.
	p = str(tmp_path / 'mask.tsv')
	t = _stopped(p)
	_dirty(t)
	real = t._releaseRewrite

	def release(*args, **kwargs):
		real(*args, **kwargs)
		with open(p, 'ab') as g:  # another process appends right after the rewrite let go of the file lock
			g.write(b'ext\tB\n')

	t._releaseRewrite = release
	t.mapToFile()
	t._releaseRewrite = real
	assert t._externalChange()
	t['a'] = ['a', '3']
	t.rewrite(force=True)  # reloads the change first
	assert t['ext'] == ['ext', 'B'] and b'ext\tB' in _bytes(p)


def test_tsvzed_legacy_reload_notes_the_time_of_what_it_read(tmp_path, monkeypatch):
	p = str(tmp_path / 'r.tsv')
	t = _stopped(p)
	_dirty(t)
	real = TSVZ.readTabularFile

	def reading(*args, **kwargs):
		out = real(*args, **kwargs)
		with open(p, 'ab') as g:  # another process appends once the read is done
			g.write(b'ext\tB\n')
		return out

	monkeypatch.setattr(TSVZ, 'readTabularFile', reading)
	t.reload()
	monkeypatch.undo()
	assert 'ext' not in t and t._externalChange()  # not read, so not seen
	t.rewrite(force=True)
	assert t['ext'] == ['ext', 'B'] and b'ext\tB' in _bytes(p)


def test_tsvzed_legacy_append_does_not_mark_an_earlier_external_change_seen(tmp_path):
	p = str(tmp_path / 'ap.tsv')
	t = _stopped(p)
	t['a'] = ['a', '1']
	t.commitAppendToFile()
	assert not t._externalChange()
	with open(p, 'ab') as g:  # another process appends before this object's next append
		g.write(b'ext\tB\n')
	t['b'] = ['b', '1']
	t.commitAppendToFile()
	assert t._externalChange()  # still to be reloaded
	t['a'] = ['a', '2']
	t.rewrite(force=True)
	assert _bytes(p) == b'key\tval\na\t2\next\tB\nb\t1\n'


def test_tsvzed_legacy_mapToFile_leaves_monitoring_alone(tmp_path):
	import threading
	p = str(tmp_path / 'mon.tsv')
	t = _stopped(p)
	_dirty(t)
	seen = []
	original = t.get_file_obj

	def hooked(modes='ab', locked=False, **kwargs):
		seen.append(t.monitor_external_changes)  # what the background writer would see meanwhile
		return original(modes, locked, **kwargs)

	t.get_file_obj = hooked
	t.mapToFile()
	t.hardMapToFile()
	assert seen == [True, True]
	# Two threads in mapToFile at once: each saved and restored the attribute, so the one that saved it
	# while the other had turned it off turned it off for good.
	inside, release = threading.Event(), threading.Event()

	def blocking(modes='ab', locked=False, **kwargs):
		if threading.current_thread().name == 'first':
			inside.set()
			release.wait(5)
		return original(modes, locked, **kwargs)

	t.get_file_obj = blocking
	t['a'] = ['a', '3']
	first = threading.Thread(target=t.mapToFile, name='first')
	first.start()
	assert inside.wait(5)
	second = threading.Thread(target=t.mapToFile, name='second')
	second.start()
	second.join(0.2)  # it is waiting for the lock the first one holds
	release.set()
	first.join(5)
	second.join(5)
	t.get_file_obj = original
	assert t.monitor_external_changes is True


def test_tsvzed_legacy_rewrite_reloads_a_change_made_after_its_reload_instead_of_erasing_it(tmp_path):
	p = str(tmp_path / 'gap.tsv')
	t = _stopped(p)
	_dirty(t)
	with open(p, 'ab') as g:
		g.write(b'ext1\tB\n')  # the rewrite reloads this one first
	real = t._snapshotForRewrite
	state = {'done': False}

	def snapshot(*args, **kwargs):
		if not state['done']:
			state['done'] = True
			with open(p, 'ab') as g:  # another process appends between the reload and the rewrite
				g.write(b'ext2\tB\n')
		return real(*args, **kwargs)

	t._snapshotForRewrite = snapshot
	t.rewrite(force=True)
	assert b'ext2\tB' in _bytes(p), 'the rewrite erased a change it had not read'
	t.rewrite(force=True)  # the next try reloads it, then rewrites
	assert t['ext1'] == ['ext1', 'B'] and t['ext2'] == ['ext2', 'B']
	assert _bytes(p) == b'key\tval\na\t2\next1\tB\next2\tB\n'


def test_tsvzed_legacy_own_write_in_progress_is_not_an_external_change(tmp_path):
	import threading
	p = str(tmp_path / 'own.tsv')
	t = _stopped(p)
	_dirty(t)
	real = t._releaseRewrite

	def release(rw, *args, **kwargs):
		rw.raw.flush()  # the file has changed; its time is noted next, under the lock
		check = threading.Thread(target=t.checkExternalChanges)  # the background writer's look meanwhile
		check.start()
		check.join(0.2)
		real(rw, *args, **kwargs)
		check.join(5)
		seen.append(t.deSynced)  # as the background writer sees it once the rewrite lets go of writeLock

	seen = []
	t._releaseRewrite = release
	t.clear_file()
	t._releaseRewrite = real
	assert seen == [False], 'its own clear_file was taken for another process changing the file'


def test_tsvzed_legacy_reload_clears_a_change_it_has_read(tmp_path):
	p = str(tmp_path / 'rc.tsv')
	t = _stopped(p)
	_dirty(t)
	with open(p, 'ab') as g:
		g.write(b'ext\tB\n')
	t.checkExternalChanges()
	assert t.deSynced
	t.reload()  # memory is the file again
	calls = []
	original = t._mapToFile
	t._mapToFile = lambda *args, **kwargs: calls.append(1) or original(*args, **kwargs)
	t.rewrite()  # rewrite_interval=0: nothing to do
	assert calls == [] and not t.deSynced and t['ext'] == ['ext', 'B']


def test_tsvzed_legacy_own_writes_never_make_the_writer_rewrite_stress(tmp_path, capsys):
	import threading
	p = str(tmp_path / 'desync.tsv')
	t = TSVZ.TSVZed(p, header='key\tval', append_check_delay=0.0001, rewrite_interval=0, rewrite_on_load=False)
	calls = []
	original = t._mapToFile
	t._mapToFile = lambda *args, **kwargs: calls.append(threading.current_thread().name) or original(*args, **kwargs)
	for i in range(300):
		t['k'] = ['k', str(i)]  # an overwrite: dirty
		t.clear_file()  # a write of this thread's own while the background writer looks for changes
	time.sleep(0.05)
	t.close()
	assert calls == [] and 'External changes detected' not in capsys.readouterr().out


def _append_external(p, line=b'ext\tB\n'):
	with open(p, 'ab') as g:
		g.write(line)


def test_tsvzed_legacy_append_notes_the_time_before_it_unlocks_the_file(tmp_path, monkeypatch):
	p = str(tmp_path / 'unlock.tsv')
	t = _stopped(p)
	t['a'] = ['a', '1']
	real = TSVZ._unlockFile

	def unlocking(f):
		real(f)
		_append_external(p)  # another writer was waiting for the lock: it appends as soon as it is let go

	monkeypatch.setattr(TSVZ, '_unlockFile', unlocking)
	t.commitAppendToFile()
	monkeypatch.undo()
	assert t._externalChange(), "the other writer's append was taken for this one's"


def _has_zstd():
	try:
		TSVZ._codecModule('zstd')
		return True
	except ImportError:
		return False


# Every codec a legacy store can be written with; .tsv.zst where this Python has compression.zstd (3.14+).
ALL_SUFFIXES = ['tsv', 'tsv.gz', 'tsv.bz2', 'tsv.xz',
				pytest.param('tsv.zst', marks=pytest.mark.skipif(not _has_zstd(), reason='zstd needs Python 3.14'))]


def _compressedAs(suffix, data):
	"""``data`` as another process appends it to a file with ``suffix``: a member (a frame) of its codec."""
	return TSVZ._compressBytes(suffix.rpartition('.')[2], data) if suffix != 'tsv' else data


@pytest.mark.parametrize('suffix', ALL_SUFFIXES)
@pytest.mark.parametrize('how', ['append', 'rewrite'])
def test_tsvzed_legacy_write_looks_for_external_changes_once_the_file_is_locked(tmp_path, monkeypatch, how, suffix):
	# A compressed rewrite (hardMapToFile) looked before it took the lock: a change appended in between was erased.
	p = str(tmp_path / ('lock.' + suffix))
	t = _stopped(p)
	_dirty(t)
	t['b'] = ['b', '1']
	real = TSVZ._lockFile
	state = {'done': False}

	def locking(f):
		if not state['done']:
			state['done'] = True
			# another writer held the lock and appends just before it is granted here
			_append_external(p, _compressedAs(suffix, b'ext\tB\n'))
		real(f)

	monkeypatch.setattr(TSVZ, '_lockFile', locking)
	if how == 'append':
		t.commitAppendToFile()
		monkeypatch.undo()
		assert t._externalChange()
	else:
		t._mapToFile(defer=True)  # the rewrite rewrite() makes
		monkeypatch.undo()
		assert state['done'] and t.deSynced and t._externalChange()
		assert TSVZ.readTabularFile(p, header='key\tval', verbose=False).get('ext') == ['ext', 'B'], 'the change was erased'
	t.rewrite(force=True)
	assert t['ext'] == ['ext', 'B'] and TSVZ.readTabularFile(p, header='key\tval', verbose=False) == dict(t)


def test_tsvzed_legacy_deferred_rewrite_leaves_the_change_unseen(tmp_path):
	p = str(tmp_path / 'defer.tsv')
	t = _stopped(p)
	_dirty(t)
	_append_external(p)
	t._mapToFile(defer=True)
	assert t.deSynced and t._externalChange() and b'ext\tB' in _bytes(p)


@pytest.mark.parametrize('suffix', ALL_SUFFIXES)
def test_tsvzed_legacy_rewrite_deferred_for_a_change_after_its_reload_is_made_then(tmp_path, suffix):
	p = str(tmp_path / ('late.' + suffix))
	t = _stopped(p)
	_dirty(t)  # the file holds a twice: a rewrite has something to do
	real = t._mapToFile
	state = {'changes': 1}

	def mapping(*args, **kwargs):
		if state['changes']:
			state['changes'] -= 1
			_append_external(p, _compressedAs(suffix, b'ext\tB\n'))  # a member of the store's own codec
		return real(*args, **kwargs)

	t._mapToFile = mapping  # another process appends after rewrite() looked for changes, before the rewrite
	assert t.rewrite(force=True)  # what close() makes with rewrite_on_exit: there is no next tick
	assert dict(t) == {'a': ['a', '2'], 'ext': ['ext', 'B']}
	assert TSVZ.readTabularFile(p, header='key\tval', verbose=False) == dict(t)
	if suffix == 'tsv':
		assert _bytes(p) == b'key\tval\na\t2\next\tB\n'  # rewritten, not only appended to
	assert not t.deSynced and not t.dirty and not t._externalChange()
	state['changes'] = 10  # it keeps changing: given up after a few tries, left to the next tick
	t['a'] = ['a', '3']
	t.commitAppendToFile()
	assert not t.rewrite(force=True) and t.deSynced


def test_tsvzed_legacy_clear_drops_the_rows_set_before_it(tmp_path):
	p = str(tmp_path / 'clr.tsv')
	t = _stopped(p)
	t['#_defaults_#'] = ['#_defaults_#', 'd']
	for i in range(50):
		t['k%d' % i] = ['k%d' % i, 'x']  # queued, not yet appended
	t.clear()
	assert len(t) == 0
	t.commitAppendToFile()
	assert dict(TSVZ.readTabularFile(p, header='key\tval')) == {}, 'rows set before clear() came back'
	assert _bytes(p) == b'key\tval\n#_defaults_#\td\n'  # the defaults stay
	t['after'] = ['after', '1']
	t.commitAppendToFile()
	assert dict(TSVZ.readTabularFile(p, header='key\tval')) == {'after': ['after', '1']}


def test_tsvzed_legacy_clear_and_sets_in_another_thread_agree_stress(tmp_path):
	import threading
	p = str(tmp_path / 'clrs.tsv')
	t = TSVZ.TSVZed(p, header='key\tval', append_check_delay=0.0002, rewrite_on_load=False)
	stop = threading.Event()

	def setter():
		i = 0
		while not stop.is_set():
			t['k%d' % i] = ['k%d' % i, str(i)]
			i += 1
			if i % 20 == 0:
				time.sleep(0.0002)

	other = threading.Thread(target=setter)
	other.start()
	for _ in range(50):
		time.sleep(0.002)
		t.clear()
	stop.set()
	other.join()
	t.close()
	assert dict(TSVZ.readTabularFile(p, header='key\tval')) == dict(t)



# --- third review round: what a refused rewrite says, and to whom ----------
def _refusals(capsys):
	"""The refused rewrites reported (a row changed in place is also refused by the append, reported apart)."""
	return [line for line in capsys.readouterr().out.splitlines() if line.startswith(('Refused at mapToFile', 'Refused at hardMapToFile'))]


def test_tsvzed_legacy_refusal_is_reported_again_after_clear_reload_or_for_another_row(tmp_path, capsys):
	p = str(tmp_path / 'again.tsv')
	t = _stopped(p)
	t['a'] = ['a', '1']
	t['b'] = ['b', '1']
	t.commitAppendToFile()
	t['a'][1] = BAD_NAME  # a row changed in place: the rewrite is refused
	t.dirty = True
	t.rewrite(force=True)
	assert len(_refusals(capsys)) == 1
	t.rewrite(force=True)
	assert _refusals(capsys) == []  # the same refusal: reported once
	t['a'] = ['a', '2']
	t['b'][1] = 'x' + BAD_NAME  # another row now
	t.dirty = True
	t.rewrite(force=True)
	assert len(_refusals(capsys)) == 1, 'a different refusal was not reported'
	t.clear()
	t['c'] = ['c', '1']
	t['c'][1] = BAD_NAME
	t.dirty = True
	t.rewrite(force=True)
	refused = _refusals(capsys)
	assert len(refused) == 1 and "'c'" in refused[0], 'a refusal after clear() was not reported'


def test_tsvzed_legacy_encoding_refusal_is_reported_again_after_reload(tmp_path, capsys):
	p = str(tmp_path / 'r.tsv')
	_touch(p, LEGACY_KEY_ROW)
	t = TSVZ.TSVZed(p, header='id\tv', encoding='ascii', append_check_delay=0.01)
	assert len(_refusals(capsys)) == 1
	t.reload()
	t.rewrite(force=True)
	assert len(_refusals(capsys)) == 1
	_stop(t)
	assert _bytes(p) == LEGACY_KEY_ROW


@pytest.mark.parametrize('interval', [0, 0.05])
def test_tsvzed_legacy_refusal_says_what_the_user_can_do_and_when_it_is_tried_again(tmp_path, capsys, interval):
	p = str(tmp_path / 'say.tsv')
	_touch(p, LEGACY_KEY_ROW)
	t = TSVZ.TSVZed(p, header='id\tv', encoding='ascii', append_check_delay=0.01, rewrite_interval=interval)
	refused = _refusals(capsys)
	_stop(t)
	assert len(refused) == 1
	message = refused[0]
	assert 'fixed or removed' not in message  # a key holding U+FFFD cannot be removed through the API
	for part in ('edit the file', 'clear() the store', 'open it with the encoding it was written in'):
		assert part in message, (part, message)
	# errors='replace' would put '?' where the file holds bytes it could not decode: never advised as a way out
	assert "errors='replace' would write '?'" in message and "or pass errors='replace'" not in message
	if interval:
		assert 'tried again at most every 1 s' in message
	else:
		assert 'at most every' not in message and 'rewrite_interval=0 none is tried again on its own' in message


def test_unencodable_fffd_error_does_not_advise_replacing_it_with_a_question_mark():
	with pytest.raises(TSVZ.TSVZEncodeError) as e:
		TSVZ._encodeText('r�sum', 'cp1252')
	assert "or pass errors='replace'" not in str(e.value)
	assert "errors='replace' would write '?' for it, losing the bytes it stands for" in str(e.value)
	assert e.value.reason == "cannot write 'r\\ufffdsum' as CP1252: it holds \\ufffd (U+FFFD, what reading puts for a byte it could not decode), which CP1252 cannot encode"
	with pytest.raises(TSVZ.TSVZEncodeError) as e:
		TSVZ._encodeText('bad\udcff', 'utf-8')
	assert str(e.value).endswith("; escape it first or pass errors='replace'") and 'errors=' not in e.value.reason


@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix sockets')
def test_cli_refusal_names_the_cell_and_no_option_it_lacks_with_or_without_a_handler(tmp_path, served, monkeypatch):
	p = str(tmp_path / 'files.tsvz')
	q = str(tmp_path / 'served.tsvz')
	for path in (p, q):
		assert _run('set', path, 'k', 'v')[0] == 0
	served(q)
	before = _bytes(q)
	real = TSVZ._CLI_HANDLERS['set']

	def onTheFiles(args, *rest):
		assert args.store != q, 'not routed through the handler'
		return real(args, *rest)

	monkeypatch.setitem(TSVZ._CLI_HANDLERS, 'set', onTheFiles)
	errors = []
	for path in (p, q):
		status, out, err = _run('set', path, 'k2', 'v\udcff')
		assert status == 1 and out == '', (status, out, err)
		errors.append(err.replace(path, 'STORE'))
	assert errors[0] == errors[1], errors  # the same message through the handler as on the files
	assert "cannot write 'v\\udcff' as UTF-8 (column 2 of key 'k2')" in errors[0]
	assert 'errors=' not in errors[0] and '--x-written' not in errors[0], errors[0]
	assert _bytes(q) == before
	status, out, err = _run('setdefault', p, 'k3', 'v\udcff')
	assert status == 1 and 'cannot write' in err and 'errors=' not in err, err


@pytest.mark.parametrize('suffix', BAD_SUFFIXES)
def test_cli_setdefault_with_a_byte_it_cannot_write_never_writes_even_when_the_key_goes(tmp_path, monkeypatch, suffix):
	# It looked KEY up with `has`, then sent `setdefault` with the byte replaced (U+FFFD): a delete in between
	# made it write U+FFFD for the user's byte, exit 0.
	p = str(tmp_path / ('sd' + suffix))
	TSVZ.appendLinesTabularFile(p, [['k', 'old']], createIfNotExist=True)
	real = TSVZ._ServeLocal.request
	sent = []

	def request(self, line, bulk=None):
		sent.append(line.split('\t')[0])
		out = real(self, line, bulk)
		TSVZ.appendLinesTabularFile(p, [['k']])  # another writer deletes the key right after
		return out

	monkeypatch.setattr(TSVZ._ServeLocal, 'request', request)
	assert _run('setdefault', p, 'k', 'v\udcff') == (0, 'k\told\n', '')
	assert sent == ['get'] and b'\xef\xbf\xbd' not in _bytes(p)
	del sent[:]
	status, out, err = _run('setdefault', p, 'k', 'v\udcff')  # now missing: refused
	assert status == 1 and out == '' and "cannot write 'v\\udcff'" in err, err
	assert sent == ['get'] and b'\xef\xbf\xbd' not in _bytes(p)


# --- fourth review round: what stops a legacy rewrite once it has started ---------
def _many(t, n=200):
	"""Give ``t`` ``n`` rows in the file and an update queued: a rewrite has much to write."""
	for i in range(n):
		t['k%d' % i] = ['k%d' % i, 'v%d' % i]
	t.commitAppendToFile()
	t['k1'] = ['k1', 'upd']
	t.commitAppendToFile()
	assert t.dirty
	return dict(t)


def _back(p):
	try:
		return TSVZ.readTabularFile(p, header='key\tval', verbose=False)
	except Exception as e:
		return repr(e)


@pytest.mark.skipif(not os.path.isdir('/proc/self/fd'), reason='counts open descriptors in /proc')
@pytest.mark.parametrize('suffix', ['tsv', 'tsv.gz'])
@pytest.mark.parametrize('method', ['mapToFile', 'hardMapToFile', 'rewrite'])
def test_tsvzed_legacy_rewrite_interrupted_in_its_write_loop_is_made_again_by_the_next_rewrite(tmp_path, suffix, method):
	# A KeyboardInterrupt in the write loop set dirty but not deSynced: with rewrite_interval=0 rewrite() rewrites
	# only when deSynced, so the emptied or half-written file was never rewritten, not even at close().
	p = str(tmp_path / ('ki.' + suffix))
	t = _stopped(p)
	rows = _many(t)
	fds = _open_fds()
	real = t._snapshotForRewrite

	def snapshot():
		header, lines = real()

		def interrupted():
			for n, line in enumerate(lines):
				if n == 100:
					raise KeyboardInterrupt
				yield line
		return header, interrupted()

	t._snapshotForRewrite = snapshot
	with pytest.raises(KeyboardInterrupt):
		if method == 'rewrite':
			t.rewrite(force=True)
		else:
			getattr(t, method)()
	t._snapshotForRewrite = real
	assert not t.writeLock.locked() and _open_fds() == fds
	assert not t._externalChange(), "its own damage was taken for another process's change"
	assert dict(t) == rows and t.dirty and t.deSynced
	assert t.rewrite(), 'the next tick did not make it again'
	assert _back(p) == rows
	t['k2'] = ['k2', 'upd']  # and once more, left to close() alone
	t.commitAppendToFile()
	t._snapshotForRewrite = snapshot
	with pytest.raises(KeyboardInterrupt):
		t.mapToFile()
	t._snapshotForRewrite = real
	rows = dict(t)
	t.shutdownEvent.clear()  # the background writer is stopped: close() makes its exit rewrite() all the same
	t.close()
	assert _back(p) == rows


@pytest.mark.skipif(not os.path.isdir('/proc/self/fd'), reason='counts open descriptors in /proc')
@pytest.mark.parametrize('suffix', ['tsv', 'tsv.gz'])
@pytest.mark.parametrize('what', [KeyboardInterrupt, OSError])
@pytest.mark.parametrize('method', ['hardMapToFile', 'clear_file'])
def test_tsvzed_legacy_rewrite_stopped_right_after_the_file_is_emptied_is_made_again(tmp_path, monkeypatch, suffix, what, method):
	# Between truncate(0) and the first write: a KeyboardInterrupt left the emptied file open and locked and its
	# time not noted (the next tick reloaded it: memory to 0 rows); an error on a clean store left it empty for good.
	p = str(tmp_path / ('cut.' + suffix))
	t = _stopped(p)
	for i in range(20):
		t['k%d' % i] = ['k%d' % i, 'v%d' % i]
	t.commitAppendToFile()
	assert not t.dirty  # a clean store: nothing else makes it rewrite
	rows = dict(t)
	fds = _open_fds()

	def failing(*args, **kwargs):
		raise what('stopped right after the file was emptied')

	monkeypatch.setattr(TSVZ, '_codecWriter', failing)
	try:
		getattr(t, method)()
	except KeyboardInterrupt:
		pass
	monkeypatch.undo()
	assert not t.writeLock.locked() and _open_fds() == fds, 'the emptied file was left open and locked'
	assert not t._externalChange(), "its own emptied file was taken for another process's change"
	assert t.dirty and t.deSynced
	t.checkExternalChanges()  # the next tick
	t.rewrite()
	assert dict(t) == rows
	assert _back(p) == rows, 'the emptied file was never written again'  # clear_file() keeps memory: so does the file


class _BufferedFullDiskFile(object):
	"""A buffered file on a disk with room for ``room`` more bytes, as Linux has it: a write only fills the buffer,
	and flush() (close() too) writes what fits, changes the file's time and fails with ENOSPC when not all of it
	fits, keeping the rest for the next try. A rewrite under 8 KB, or any compressed one, fails there only."""

	def __init__(self, f, room=0):
		self._f = f
		self._room = room
		self._pending = b''

	def write(self, data):
		self._pending += bytes(data)
		return len(data)

	def flush(self):
		import errno
		if not self._pending:
			return
		fits, self._pending = self._pending[:self._room], self._pending[self._room:]
		self._room -= len(fits)
		self._f.write(fits)
		self._f.flush()
		_touch_mtime(self._f)
		if self._pending:
			raise OSError(errno.ENOSPC, 'No space left on device')

	def close(self):
		try:
			self.flush()
		finally:
			self._f.close()

	def __getattr__(self, name):
		return getattr(self._f, name)


@pytest.mark.parametrize('suffix', ALL_SUFFIXES)
@pytest.mark.parametrize('where', ['flush', 'fsync'])
@pytest.mark.parametrize('method', ['hardMapToFile', 'rewrite', 'clear_file'])
def test_tsvzed_legacy_rewrite_whose_flush_or_fsync_fails_is_made_again(tmp_path, monkeypatch, capsys, suffix, where, method):
	# release_file_obj swallowed a failed flush, fsync or close: the rewrite whose bytes never reached the disk (they
	# sat in the file's or the codec's buffer) was marked done, and after close() the file was empty or header-only.
	# Its time was also noted before close() wrote the buffer again, which changes it: the damage was then reloaded.
	import errno
	p = str(tmp_path / ('buf.' + suffix))
	t = _stopped(p)
	rows = _many(t, 20)
	if method == 'rewrite':
		t.deSynced = True  # what a tick rewrites with rewrite_interval=0
	real = TSVZ._openUntruncated
	monkeypatch.setattr(TSVZ, '_openUntruncated', lambda fileName: _BufferedFullDiskFile(real(fileName), 0 if where == 'flush' else 1 << 20))
	if where == 'fsync':
		def failing(fd):
			raise OSError(errno.EIO, 'Input/output error')
		monkeypatch.setattr(TSVZ.os, 'fsync', failing)
	if method == 'rewrite' and suffix == 'tsv':
		# the in-place rewrite mapToFile makes, failing, then the hardMapToFile it falls back on
		realOpen = TSVZ.openFileAsCompressed
		monkeypatch.setattr(TSVZ, 'openFileAsCompressed', lambda fileName, mode='rb', *a, **k: (
			_BufferedFullDiskFile(realOpen(fileName, mode, *a, **k), 0 if where == 'flush' else 1 << 20) if mode == 'r+b' else realOpen(fileName, mode, *a, **k)))
	if method == 'rewrite':
		t.rewrite()
	else:
		getattr(t, method)()
	monkeypatch.undo()
	out = capsys.readouterr().out
	assert ('No space left' if where == 'flush' else 'Input/output error') in out, out
	assert not t.writeLock.locked()
	assert t.dirty and t.deSynced, 'a rewrite that did not reach the file was marked done'
	assert not t._externalChange(), "its own damage was taken for another process's change"
	t.checkExternalChanges()  # the next tick, once there is room again
	t.rewrite()
	assert dict(t) == rows
	assert _back(p) == rows and not t.dirty and not t.deSynced


_FULL_TMPFS_SCRIPT = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
import TSVZ
suffix, method, d = sys.argv[2], sys.argv[3], sys.argv[4]
p = os.path.join(d, 'store.' + suffix)
filler = os.path.join(d, 'filler')
t = TSVZ.TSVZed(p, header='key\tval', rewrite_on_load=False, append_check_delay=0.001, verbose=False)
t.shutdownEvent.set()
t.appendThread.join()  # every write is driven from here
for i in range(20):
	t['k%d' % i] = ['k%d' % i, 'v%d' % i]
t.commitAppendToFile()
t['k1'] = ['k1', 'upd']
t.commitAppendToFile()
rows = dict(t)
real = TSVZ._codecWriter

def filling(*args, **kwargs):  # right after the rewrite emptied the file: another writer fills the disk
	with open(filler, 'wb') as f:
		try:
			while True:
				f.write(b'\0' * 4096)
				f.flush()
		except OSError:
			pass
	return real(*args, **kwargs)

TSVZ._codecWriter = filling
if method == 'rewrite':
	t.deSynced = True
	t.rewrite()
else:
	getattr(t, method)()
TSVZ._codecWriter = real
result = {'failed': os.path.exists(filler), 'dirty': t.dirty, 'deSynced': t.deSynced, 'external': t._externalChange()}
if result['failed']:
	os.remove(filler)
t.checkExternalChanges()
t.rewrite()
result['memory'] = dict(t) == rows
t.shutdownEvent.clear()
t.close()
try:
	result['file'] = TSVZ.readTabularFile(p, header='key\tval', verbose=False) == rows
except Exception as e:
	result['file'] = repr(e)
print(json.dumps(result))
"""


def _full_tmpfs(tmp_path, *args):
	"""Run _FULL_TMPFS_SCRIPT with ``args`` on a 1 MiB tmpfs of its own, in a private mount namespace (root on Linux)."""
	import json
	import shutil
	import subprocess
	if not sys.platform.startswith('linux') or os.geteuid() != 0 or not shutil.which('unshare'):
		pytest.skip('a real full disk needs root on Linux and unshare (a tmpfs in a private mount namespace)')
	mnt = tmp_path / 'mnt'
	mnt.mkdir()
	script = tmp_path / 'full.py'
	script.write_text(_FULL_TMPFS_SCRIPT)
	command = 'mount -t tmpfs -o size=1m tmpfs "$1" || exit 77; shift; exec "$@"'
	proc = subprocess.run(['unshare', '-m', 'sh', '-c', command, 'sh', str(mnt), sys.executable, str(script),
						   os.path.dirname(os.path.abspath(TSVZ.__file__))] + list(args) + [str(mnt)],
						  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
	if proc.returncode == 77 or (proc.returncode and 'unshare' in proc.stderr):
		pytest.skip('cannot mount a tmpfs in a private mount namespace here: ' + proc.stderr.strip()[-200:])
	assert proc.returncode == 0, proc.stderr
	return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize('suffix', ALL_SUFFIXES)
@pytest.mark.parametrize('method', ['hardMapToFile', 'rewrite'])
def test_tsvzed_legacy_rewrite_on_a_really_full_disk_is_made_again_from_memory(tmp_path, suffix, method):
	result = _full_tmpfs(tmp_path, suffix, method)
	if method == 'rewrite' and suffix == 'tsv':
		assert not result['failed']  # rewritten in place: it needs no room it did not have
	else:
		assert result['failed'] and result['dirty'] and result['deSynced'], result
	assert result == dict(result, external=False, memory=True, file=True), result


@pytest.mark.parametrize('suffix', ALL_SUFFIXES)
@pytest.mark.parametrize('method', ['hardMapToFile', 'clear_file'])
def test_tsvzed_legacy_rewrite_is_all_on_disk_before_it_unlocks_the_file(tmp_path, monkeypatch, suffix, method):
	# BZ2File.flush() and LZMAFile.flush() do not flush the compressor: release_file_obj unlocked a .bz2 or .xz
	# rewrite with nothing on disk (a .gz one without its trailer), and another writer could append in between.
	p = str(tmp_path / ('u.' + suffix))
	t = _stopped(p)
	_many(t, 500)
	real = TSVZ._unlockFile
	sizes = []

	def unlocking(f):
		sizes.append(os.path.getsize(p))
		real(f)

	monkeypatch.setattr(TSVZ, '_unlockFile', unlocking)
	getattr(t, method)()
	monkeypatch.undo()
	assert sizes == [os.path.getsize(p)], 'bytes on disk when the file was unlocked, and in the end'
	assert _back(p) == (dict(t) if method == 'hardMapToFile' else {})


@pytest.mark.parametrize('suffix', ALL_SUFFIXES)
@pytest.mark.parametrize('verbose', [False, True])
def test_tsvzed_legacy_failed_release_reports_the_real_error(tmp_path, monkeypatch, capsys, suffix, verbose):
	# release_file_obj logged file.name once it had closed the file: BZ2File and LZMAFile have no name before
	# Python 3.13 (AttributeError), and a closed one refuses it since (ValueError), which hid the real error.
	import errno
	p = str(tmp_path / ('r.' + suffix))
	t = _stopped(p, verbose=verbose)
	t['a'] = ['a', '1']

	def failing(fd):
		raise OSError(errno.EIO, 'Input/output error')

	monkeypatch.setattr(TSVZ.os, 'fsync', failing)
	t.commitAppendToFile()
	monkeypatch.undo()
	out = capsys.readouterr().out
	assert 'Input/output error' in out, out
	assert 'closed file' not in out and 'has no attribute' not in out, out
	assert not t.writeLock.locked()


@pytest.mark.parametrize('suffix', ['tsv', 'tsv.gz'])
@pytest.mark.parametrize('how', ['mapToFile', 'hardMapToFile', 'rewrite', 'rewrite_on_exit'])
def test_tsvzed_legacy_rewrite_of_a_removed_file_makes_it_again_from_memory(tmp_path, suffix, how):
	# mapToFile stopped when get_file_obj('r+b') could not open a file another process removed (main 1688d25 fell
	# back on hardMapToFile, which made it again); a compressed rewrite looking once the file is locked took the
	# file it had just created for another process's change.
	p = str(tmp_path / ('gone.' + suffix))
	t = _stopped(p, rewrite_on_exit=(how == 'rewrite_on_exit'))
	_dirty(t)
	t['b'] = ['b', '1']
	t.commitAppendToFile()
	rows = dict(t)
	os.remove(p)  # rm, a log rotation
	if how == 'rewrite_on_exit':
		t.shutdownEvent.clear()  # the background writer is stopped: close() makes its exit rewrite() all the same
		t.close()
	elif how == 'rewrite':
		assert t.rewrite(force=True)
	else:
		getattr(t, how)()
	assert dict(t) == rows
	assert os.path.exists(p) and _back(p) == rows
	assert not t._externalChange() and not t.dirty and not t.deSynced


def test_tsvzed_legacy_rewrite_deferred_in_one_thread_is_not_taken_for_done_by_another(tmp_path):
	# rewrite() learned that its rewrite was deferred from an attribute of the object: the background writer's
	# rewrite() starting meanwhile cleared it, and close()'s exit rewrite was skipped while True was returned.
	import threading
	p = str(tmp_path / 'race.tsv')
	t = _stopped(p)
	_dirty(t)
	real = t._mapToFile
	main = threading.current_thread()
	inside, release = threading.Event(), threading.Event()
	state = {'calls': 0}

	def mapping(*args, **kwargs):
		if threading.current_thread() is not main:
			inside.set()  # the other rewrite() has started its loop
			release.wait(5)
			return real(*args, **kwargs)
		state['calls'] += 1
		if state['calls'] == 1:
			_append_external(p)  # another process appends after this rewrite() looked for changes
			result = real(*args, **kwargs)  # deferred
			state['worker'] = threading.Thread(target=t.rewrite, name='worker')  # the background writer's tick meanwhile
			state['worker'].start()
			assert inside.wait(5)
			return result
		return real(*args, **kwargs)

	t._mapToFile = mapping
	try:
		made = t.rewrite(force=True)
		written = _bytes(p)
	finally:
		release.set()
		if 'worker' in state:
			state['worker'].join(5)
	t._mapToFile = real
	assert made and written == b'key\tval\na\t2\next\tB\n', (made, written)
	assert t['ext'] == ['ext', 'B']


def test_tsvzed_legacy_rewrite_that_keeps_failing_still_reloads_another_process_change_at_every_tick(tmp_path, monkeypatch):
	# The background writer skipped all of rewrite() while a failed rewrite waited to be tried again; rewrite() is
	# also its only reload of another process's change, which it then saw up to a minute late.
	p = str(tmp_path / 'ro.tsv')
	t = TSVZ.TSVZed(p, header='key\tval', rewrite_on_load=False, append_check_delay=0.001, teeLogger=_ErrorLog())
	_dirty(t)
	real = TSVZ.openFileAsCompressed

	def denying(fileName, mode='rb', *args, **kwargs):
		if mode in ('r+b', 'wb'):
			raise PermissionError(13, 'Permission denied')  # a file this process may append to but not rewrite
		return real(fileName, mode, *args, **kwargs)

	monkeypatch.setattr(TSVZ, 'openFileAsCompressed', denying)
	monkeypatch.setattr(TSVZ, '_openUntruncated', lambda fileName: denying(fileName, 'wb'))
	t.deSynced = True  # a tick tries the rewrite, and fails
	assert _wait_for(lambda: t._failing('rewrite'), 5)
	for _ in range(10):
		t._failed('rewrite', 'Permission denied', False)  # it kept failing: the next try is a minute away
	_append_external(p)
	started = time.time()
	assert _wait_for(lambda: 'ext' in t, 3), 'another process\'s change was not reloaded while the rewrite waits'
	assert time.time() - started < 3
	monkeypatch.undo()
	t._worked('rewrite')
	t.close()
	assert TSVZ.readTabularFile(p, header='key\tval', verbose=False) == {'a': ['a', '2'], 'ext': ['ext', 'B']}


def test_tsvzed_legacy_background_writer_makes_one_rewrite_a_tick_while_another_process_keeps_changing_the_file(tmp_path):
	# A tick tried up to three rewrites, each after a commit and a full reload, when another process kept changing
	# the file under them; main 1688d25 made one. The tries rewrite() makes are for an explicit call and close().
	p = str(tmp_path / 'busy.tsv')
	t = TSVZ.TSVZed(p, header='key\tval', rewrite_on_load=False, append_check_delay=0.01)
	_dirty(t)
	counts = {'ticks': 0, 'rewrites': 0, 'n': 0}
	realMap, realCheck = t._mapToFile, t.checkExternalChanges

	def checking(*args, **kwargs):
		counts['ticks'] += 1  # once at every tick
		return realCheck(*args, **kwargs)

	def mapping(*args, **kwargs):
		counts['rewrites'] += 1
		counts['n'] += 1
		_append_external(p, b'ext%d\tB\n' % counts['n'])  # it keeps changing: every rewrite is deferred
		return realMap(*args, **kwargs)

	t.checkExternalChanges, t._mapToFile = checking, mapping
	t.deSynced = True
	time.sleep(0.5)
	t.checkExternalChanges, t._mapToFile = realCheck, realMap
	assert counts['rewrites'] > 5, counts
	assert counts['rewrites'] <= counts['ticks'] + 1, counts
	assert _wait_for(lambda: not t.deSynced and not t.dirty, 3)  # once it stops changing, the next tick makes it
	t.close()
	assert TSVZ.readTabularFile(p, header='key\tval', verbose=False) == dict(t) and 'ext%d' % counts['n'] in t


def test_tsvzed_legacy_failed_rewrite_and_its_fallback_count_as_one_try(tmp_path, monkeypatch, capsys):
	# A failed mapToFile and the hardMapToFile it falls back on each counted a failure: the wait before the next
	# try grew four times at each failed rewrite, not twice as documented.
	p = str(tmp_path / 'twice.tsv')
	t = _stopped(p)
	_dirty(t)
	realOpen, realUntruncated = TSVZ.openFileAsCompressed, TSVZ._openUntruncated
	monkeypatch.setattr(TSVZ, 'openFileAsCompressed', lambda fileName, mode='rb', *a, **k: (
		_FullDiskFile(realOpen(fileName, mode, *a, **k)) if mode == 'r+b' else realOpen(fileName, mode, *a, **k)))
	monkeypatch.setattr(TSVZ, '_openUntruncated', lambda fileName: _FullDiskFile(realUntruncated(fileName)))
	waits = []
	for _ in range(3):
		t.rewrite(force=True)  # mapToFile fails, then the hardMapToFile it falls back on
		waits.append(t._failures['rewrite'][0])
	monkeypatch.undo()
	out = capsys.readouterr().out
	assert 'Failed to write at mapToFile()' in out and 'Failed to write at hardMapToFile()' in out, out
	assert waits == [TSVZ._FAILURE_RETRY_FIRST * f for f in (1, 2, 4)], waits
	t.rewrite()
	assert 'rewrite' not in t._failures and _bytes(p) == b'key\tval\na\t2\n'


# --- fifth review round: a rewrite lets go of the file once, whatever stops it where ----------
def _interrupt_at(n, call, where=None):
	"""Call ``call()``, raising KeyboardInterrupt at the ``n``-th line TSVZ.py runs in it; True when it got there.

	``_interrupt_at.fds`` is then how many descriptors were open while the KeyboardInterrupt was being handled (its
	traceback holding every frame): what a close() in the program's ``except KeyboardInterrupt`` meets.

	``where``: count only the lines of that TSVZ.py function and of what it calls (the first time it runs). Two kinds
	of line are skipped, where the tracer runs but CPython handles no signal: a ``try:`` line, before the try is set
	up (3.6 makes the opcode before SETUP_FINALLY uninterruptible; from 3.11 on it is a NOP), and a ``with`` line,
	which the tracer reports again as the block ends, before ``__exit__`` (no signal is handled between the last
	statement of the block and the call of a C ``__exit__``, a lock's).
	"""
	import gc
	import linecache
	source = TSVZ.__file__
	state = {'count': 0, 'hit': False, 'armed': 0 if where else 1}

	def local(frame, event, arg):
		if event == 'return' and where is not None and frame.f_code.co_name == where:
			state['armed'] = -1  # it ran once: nothing after it counts
		elif event == 'line' and state['armed'] > 0:
			line = linecache.getline(source, frame.f_lineno).strip()
			if line == 'try:' or line.startswith('with '):
				return local
			state['count'] += 1
			if state['count'] == n:
				state['hit'] = True
				raise KeyboardInterrupt
		return local

	def tracer(frame, event, arg):
		if frame.f_code.co_filename != source:
			return None
		if where is not None and frame.f_code.co_name == where and state['armed'] == 0:
			state['armed'] = 1
		return local if state['armed'] > 0 or where is not None else None

	sys.settrace(tracer)
	_interrupt_at.fds = None
	try:
		call()
	except KeyboardInterrupt:
		sys.settrace(None)
		_interrupt_at.fds = _open_fds()
		if not state['hit']:
			raise
	finally:
		sys.settrace(None)
	gc.collect()  # the traceback that held the frames is gone: what is still open was left open
	return state['hit']


def _lets_go(t, fds):
	"""Assert ``t`` holds neither writeLock nor the file, and that the next rewrite() does not wait for them."""
	import threading
	assert not t.writeLock.locked(), 'writeLock is still held: close(), atexit and __del__ would hang'
	assert _open_fds() == fds, 'the file was left open (and locked: the next rewrite waits for it)'
	assert _interrupt_at.fds in (None, fds), 'the file was left open for as long as the KeyboardInterrupt was handled'
	done = []
	th = threading.Thread(target=lambda: done.append(t.rewrite(force=True)), daemon=True)
	th.start()
	th.join(10)
	assert done, 'the next rewrite() waits for a lock left held'


@pytest.mark.filterwarnings('error::pytest.PytestUnraisableExceptionWarning')  # no codec writer is left to write when collected
@pytest.mark.skipif(not os.path.isdir('/proc/self/fd'), reason='counts open descriptors in /proc')
@pytest.mark.parametrize('suffix', ['tsv', 'tsv.gz'])
@pytest.mark.parametrize('method', ['mapToFile', 'hardMapToFile', 'clear_file', 'clear', 'defer'])
def test_tsvzed_legacy_rewrite_interrupted_at_any_line_lets_go_and_loses_nothing(tmp_path, capsys, suffix, method):
	# A KeyboardInterrupt in the release of a deferred rewrite (release_file_obj catches only Exception), on
	# entering the release of a written one or between the lock and the try left writeLock or the file held: the
	# next rewrite and close() hung. One before the look for another process's change noted its time as seen,
	# and the next rewrite erased the change. Here one lands at every line from the lock to the unlock in turn.
	# Then another process appends a row (``late``): whatever the interrupt left, the next rewrite keeps it, merged
	# or reloaded, without a warning. One that left _damage unknown (between it and the rewrite's start) or the
	# release before it noted the damage made that rewrite overwrite it, with a warning (50ea158).
	p = str(tmp_path / ('any.' + suffix))
	t = _stopped(p)
	_dirty(t)
	fds = _open_fds()
	n = 0
	while True:
		n += 1
		external = 'ext%d' % n
		if method == 'defer':  # another process appends after rewrite() looked: the rewrite is deferred
			_append_external(p, _compressedAs(suffix, external.encode() + b'\tB\n'))
			call = lambda: t._mapToFile(defer=True)
		else:
			call = getattr(t, method)
		before = dict(t)
		hit = _interrupt_at(n, call)
		late = {'late%d' % n: ['late%d' % n, 'L']}
		_append_external(p, _compressedAs(suffix, ('late%d\tL\n' % n).encode()))
		_lets_go(t, fds)  # its rewrite(force=True) takes in the late row
		assert not t._keepFileTime
		if method == 'defer':
			assert t.get(external) == ['ext' + external[3:], 'B'], (n, 'the change was erased')
			assert dict(t) == dict(before, **dict({external: [external, 'B']}, **late)), n
		elif method in ('clear', 'clear_file'):
			# Done: memory (clear()) or the file (clear_file(), and the reload of the late row) was emptied. Not done,
			# or clear() stopped after it emptied memory and before the file (docs/legacy-writer-failures.md 5):
			# the file is reloaded with the late row, or memory and the late row are written.
			assert dict(t) in (dict(before, **late), late), n
		else:
			assert dict(t) == dict(before, **late), (n, 'the late row was lost or memory was')
		assert _back(p) == dict(t), n
		assert not t._externalChange() and not t.deSynced, n
		assert 'Overwriting external changes' not in capsys.readouterr().out, n
		if not hit:
			break
		if method == 'defer':
			del t[external]  # as many rows at each try: the next one runs as many lines
		for key in late:
			t.pop(key, None)
		t['a'] = ['a', str(n)]  # dirty again, for the next try
		t.commitAppendToFile()
	assert n > 20, n  # it did get through all of it


@pytest.mark.filterwarnings('error::pytest.PytestUnraisableExceptionWarning')  # no codec writer is left to write when collected
@pytest.mark.skipif(not os.path.isdir('/proc/self/fd'), reason='counts open descriptors in /proc')
@pytest.mark.parametrize('suffix', ['tsv', 'tsv.gz', 'tsv.bz2'])
@pytest.mark.parametrize('first', ['none', 'fsync', 'write', 'defer'])
def test_tsvzed_legacy_rewrite_interrupted_again_in_its_release_lets_go_and_loses_nothing(tmp_path, monkeypatch, capsys, suffix, first):
	# In _releaseRewrite's finally writeLock was let go of after _unlockFile and after a close that caught only
	# Exception: a second Ctrl-C there (the first one stopped the write, or an fsync failed) left it held for good.
	# Here a KeyboardInterrupt lands at every line of the release in turn, after the first failure.
	import errno
	p = str(tmp_path / ('again.' + suffix))
	t = _stopped(p)
	_dirty(t)
	fds = _open_fds()
	real = t._snapshotForRewrite
	m = 0
	while True:
		m += 1
		external = 'ext%d' % m
		before = dict(t)
		if first == 'defer':
			_append_external(p, _compressedAs(suffix, external.encode() + b'\tB\n'))
		elif first == 'fsync':
			def failing(fd):
				raise OSError(errno.EIO, 'Input/output error')
			monkeypatch.setattr(TSVZ.os, 'fsync', failing)
		elif first == 'write':
			def snapshot():
				header, lines = real()

				def interrupted():
					yield lines[0]
					raise KeyboardInterrupt
				return header, interrupted()
			t._snapshotForRewrite = snapshot

		def call():
			try:
				t._hardMapToFile(defer=True)
			except KeyboardInterrupt:
				_interrupt_at.fds = _open_fds()  # the first one, or the second
		hit = _interrupt_at(m, call, where='_releaseRewrite')
		monkeypatch.undo()
		t._snapshotForRewrite = real
		late = {'late%d' % m: ['late%d' % m, 'L']}
		_append_external(p, _compressedAs(suffix, ('late%d\tL\n' % m).encode()))  # another process, after it
		_lets_go(t, fds)
		if first == 'defer':
			assert dict(t) == dict(before, **dict({external: [external, 'B']}, **late)), (m, 'the change was erased')
			del t[external]
		else:
			assert dict(t) == dict(before, **late), (m, 'the late row was lost or memory was')
		for key in late:
			del t[key]
		t.commitAppendToFile()
		assert _back(p) == dict(t), m
		assert not t._externalChange() and not t.deSynced, m
		assert 'Overwriting external changes' not in capsys.readouterr().out, m
		if not hit:
			break
		t['a'] = ['a', str(m)]  # dirty again, for the next try
		t.commitAppendToFile()
	assert m > 10, m


@pytest.mark.parametrize('suffix', ['tsv', 'tsv.gz'])
@pytest.mark.parametrize('method', ['rewrite', 'hardMapToFile', 'clear_file', 'append'])
@pytest.mark.parametrize('code', ['EINVAL', 'EROFS', 'ENOTSUP', 'EOPNOTSUPP', 'EIO', 'ENOSPC'])
def test_tsvzed_legacy_write_on_a_file_system_that_cannot_fsync(tmp_path, monkeypatch, capsys, suffix, method, code):
	# a2107b4 made a failed fsync a failed rewrite: where fsync always fails with EINVAL (a file system that
	# cannot sync) every rewrite and clear_file() failed though its data was written, and the background writer
	# rewrote the file twice at every try, for good (main 1688d25 took it for written). Only an fsync error that
	# may mean the data is not on disk fails it.
	import errno
	if not hasattr(errno, code):
		pytest.skip('no errno.' + code + ' here')
	p = str(tmp_path / ('nosync.' + suffix))
	t = _stopped(p)
	_dirty(t)
	calls = []

	def fsync(fd):
		calls.append(fd)
		raise OSError(getattr(errno, code), os.strerror(getattr(errno, code)))

	monkeypatch.setattr(TSVZ.os, 'fsync', fsync)
	if method == 'rewrite':
		t.rewrite(force=True)
	elif method == 'append':
		t['b'] = ['b', '1']
		t.commitAppendToFile()
	else:
		getattr(t, method)()
	monkeypatch.undo()
	out = capsys.readouterr().out
	assert calls
	if code in ('EIO', 'ENOSPC'):
		assert os.strerror(getattr(errno, code)) in out, out
		if method != 'append':
			assert t.deSynced, 'a rewrite whose fsync failed was taken for done'
		return
	assert 'Failed' not in out and 'rror' not in out, out
	assert not t.deSynced and len(calls) == 1, calls
	assert t.dirty == (method == 'append')  # the append leaves the file holding a twice, as it did
	assert _back(p) == ({} if method == 'clear_file' else dict(t))


def _failing_rewrite(monkeypatch, partial=b''):
	"""Make every rewrite fail with ENOSPC once it has emptied the file and written ``partial`` (not compressed)."""
	import errno

	def full(fileName=None, raw=None, *args, **kwargs):  # right after the truncate
		if raw is not None:
			raw.write(partial)
			raw.flush()
		raise OSError(errno.ENOSPC, 'No space left on device')

	def inplace(self, file, header, lines):
		file.write(partial)
		file.truncate()
		file.flush()
		full()

	monkeypatch.setattr(TSVZ, '_codecWriter', full)
	monkeypatch.setattr(TSVZ.TSVZed, '_writeInPlace', inplace)


@pytest.mark.parametrize('suffix', ALL_SUFFIXES)
@pytest.mark.parametrize('method', ['rewrite', 'hardMapToFile', 'rewrite_on_exit'])
def test_tsvzed_legacy_change_after_a_failed_rewrite_is_merged_into_memory_not_reloaded_over_it(tmp_path, monkeypatch, capsys, suffix, method):
	# A rewrite that failed once it had emptied the file (a full disk) leaves memory the only good copy. Another
	# process's change then made the next rewrite() reload the damaged file over memory: every row was lost, and
	# since 73c6b67 the background writer reloads at every tick while the failed rewrite waits. Main 1688d25 lost
	# them the same way. The rows the other process appended (a set and a delete) are now merged into memory.
	p = str(tmp_path / ('merge.' + suffix))
	t = _stopped(p, rewrite_on_exit=(method == 'rewrite_on_exit'))
	rows = _many(t, 5)
	_failing_rewrite(monkeypatch, partial=b'key\tval\nk0\tv0\n')
	if method == 'hardMapToFile':
		t.hardMapToFile()
	else:
		assert not t.rewrite(force=True)
	monkeypatch.undo()
	assert dict(t) == rows and t.dirty and t.deSynced
	assert _back(p) != rows  # damaged: emptied, or half written
	_append_external(p, _compressedAs(suffix, b'ext\tB\nk2\t\n'))  # space is back; another process sets ext, deletes k2
	t.checkExternalChanges()  # the next tick
	if method == 'rewrite_on_exit':
		t.shutdownEvent.clear()  # the background writer is stopped: close() makes its exit rewrite() all the same
		t.close()
	else:
		assert t.rewrite()
	expected = dict(rows, ext=['ext', 'B'])
	del expected['k2']
	assert dict(t) == expected
	assert _back(p) == expected and not t.dirty and not t.deSynced and not t._externalChange()
	assert 'Overwriting external changes' not in capsys.readouterr().out


@pytest.mark.parametrize('suffix', ['tsv', 'tsv.gz'])
def test_tsvzed_legacy_change_after_a_failed_rewrite_that_cannot_be_merged_keeps_memory(tmp_path, monkeypatch, capsys, suffix):
	# Another process rewrote the damaged file (not only appended to it): its change cannot be told apart from the
	# damage, so memory is kept and written, with a warning, rather than the damaged rows reloaded over it.
	p = str(tmp_path / ('nomerge.' + suffix))
	t = _stopped(p)
	rows = _many(t, 5)
	_failing_rewrite(monkeypatch, partial=b'key\tval\nk0\tv0\n')
	t.hardMapToFile()
	monkeypatch.undo()
	assert t.deSynced and _bytes(p) == b'key\tval\nk0\tv0\n'
	time.sleep(0.01)
	with open(p, 'wb') as g:
		g.write(_compressedAs(suffix, b'key\tval\nother\tX\n'))
	assert t.rewrite()
	assert 'Overwriting external changes' in capsys.readouterr().out
	assert dict(t) == rows and _back(p) == rows and not t.deSynced


def test_tsvzed_legacy_background_writer_never_reloads_a_file_its_failed_rewrite_damaged(tmp_path, monkeypatch):
	# 73c6b67: the background writer reloads another process's change at every tick while a failed rewrite waits to
	# be tried again. When that rewrite had emptied the file, the reload dropped every row; it now merges the change.
	p = str(tmp_path / 'tick.tsv')
	t = TSVZ.TSVZed(p, header='key\tval', rewrite_on_load=False, append_check_delay=0.001, teeLogger=_ErrorLog())
	for i in range(5):
		t['k%d' % i] = ['k%d' % i, 'v']
	assert _wait_for(lambda: not t.appendQueue)
	t['k0'] = ['k0', 'w']
	rows = dict(t)
	_failing_rewrite(monkeypatch)
	t.deSynced = True  # a tick tries the rewrite, and fails
	assert _wait_for(lambda: t._failing('rewrite'), 5)
	for _ in range(10):
		t._failed('rewrite', 'No space left on device', False)  # it keeps failing: the next try is a minute away
	assert b'k1' not in _bytes(p), 'the rewrite did not empty the file'  # the queued k0 may be appended to it
	_append_external(p)
	deadline = time.time() + 0.5
	while time.time() < deadline:  # many ticks
		assert dict(t) == rows, 'a tick reloaded the damaged file over memory'
		time.sleep(0.01)
	monkeypatch.undo()
	t._worked('rewrite')
	assert _wait_for(lambda: not t.deSynced, 5)
	t.close()
	assert dict(t) == dict(rows, ext=['ext', 'B'])
	assert TSVZ.readTabularFile(p, header='key\tval', verbose=False) == dict(t)


@pytest.mark.parametrize('suffix', ['tsv', 'tsv.gz'])
def test_tsvzed_legacy_rewrite_of_a_removed_file_merges_one_another_process_made_again_meanwhile(tmp_path, suffix):
	# _writeRewrite decided the file was missing before it opened and locked it, and then skipped the look for
	# another process's change: a file made again in that window was overwritten without a warning or a reload.
	p = str(tmp_path / ('again.' + suffix))
	t = _stopped(p)
	rows = _many(t, 5)
	os.remove(p)  # rm, a log rotation
	real = t.get_file_obj

	def opening(*args, **kwargs):
		if not os.path.exists(p):  # another process makes it again, with a row, before this one opens it
			with open(p, 'wb') as g:
				g.write(_compressedAs(suffix, b'key\tval\nnew\tN\n'))
		return real(*args, **kwargs)

	t.get_file_obj = opening
	assert t.rewrite(force=True)
	t.get_file_obj = real
	assert dict(t) == dict(rows, new=['new', 'N'])
	assert _back(p) == dict(t) and not t._externalChange()


@pytest.mark.parametrize('suffix', ['tsv', 'tsv.gz'])
@pytest.mark.parametrize('method', ['clear', 'clear_file', 'hardMapToFile'])
def test_tsvzed_legacy_rewrite_needs_no_64_mib_buffer(tmp_path, suffix, method):
	# A whole rewrite wrote through an io.BufferedWriter of 64 MiB, which clear() and clear_file() allocated to
	# write one header line; it was flushed, never closed, and its buffer dropped in the GC on the failure path.
	import tracemalloc
	p = str(tmp_path / ('small.' + suffix))
	t = _stopped(p)
	_many(t, 20)
	tracemalloc.start()
	try:
		getattr(t, method)()
		peak = tracemalloc.get_traced_memory()[1]
	finally:
		tracemalloc.stop()
	assert peak < 4 << 20, peak
	assert _back(p) == ({} if method != 'hardMapToFile' else dict(t))


# --- sixth review round: one release, a merge safe to interrupt, bounded rewrites ----------
class _FailingTruncate(object):
	"""A file whose truncate() fails (an I/O error, a file system that cannot)."""

	def __init__(self, f):
		self._f = f

	def truncate(self, *args):
		import errno
		raise OSError(errno.EIO, 'Input/output error')

	def __getattr__(self, name):
		return getattr(self._f, name)


def test_tsvzed_legacy_get_file_obj_whose_truncate_fails_does_not_take_the_file_for_its_own(tmp_path, monkeypatch):
	# 639fa34 noted the file as emptied before the truncate: when it failed, get_file_obj's error path noted the
	# file's time as this object's own, and another process's change made before was taken for seen.
	p = str(tmp_path / 'trunc.tsv')
	t = _stopped(p)
	_dirty(t)
	t.commitAppendToFile()
	_append_external(p)
	real = TSVZ._openUntruncated
	monkeypatch.setattr(TSVZ, '_openUntruncated', lambda fileName: _FailingTruncate(real(fileName)))
	assert t.get_file_obj('wb') is None
	monkeypatch.undo()
	assert not t.writeLock.locked()
	assert b'ext\tB' in _bytes(p) and t._externalChange(), "another process's change was taken for seen"


@pytest.mark.parametrize('suffix', ALL_SUFFIXES[1:])
def test_tsvzed_legacy_rewrite_given_up_leaves_no_codec_writer_to_write_when_collected(tmp_path, monkeypatch, capfd, suffix):
	# _abandonRewrite closed only the file under the codec's writer: when the writer was collected it wrote its
	# trailer into the closed file ('Exception ignored ... ValueError: write to closed file', 'lost gzip_file').
	import gc
	p = str(tmp_path / ('given.' + suffix))
	t = _stopped(p)
	_many(t, 50)
	seen = []
	if hasattr(sys, 'unraisablehook'):  # 3.8+; before it, what is ignored goes to stderr
		monkeypatch.setattr(sys, 'unraisablehook', seen.append)
	real = TSVZ.TSVZed._releaseRewrite
	state = {'armed': True}

	def entry(self, *args):  # a Ctrl-C as the release of a written rewrite is entered
		if state['armed']:
			state['armed'] = False
			raise KeyboardInterrupt
		return real(self, *args)

	monkeypatch.setattr(TSVZ.TSVZed, '_releaseRewrite', entry)
	with pytest.raises(KeyboardInterrupt):
		t.hardMapToFile()
	gc.collect()
	monkeypatch.undo()
	assert not seen and 'Exception ignored' not in capfd.readouterr().err, seen
	assert not t.writeLock.locked() and t.deSynced
	assert t.rewrite() and _back(p) == dict(t)
