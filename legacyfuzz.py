#!/usr/bin/env python3
"""Differential fuzzer for TSVZ's legacy scrub (scrubTabularFile on a .tsv).

Modes (run inside Docker, see README at the bottom of this docstring):

  run    generate random small files x scrub-argument sets, check invariants 1-6, shrink the
         violations, print one JSON line per distinct minimal counterexample
         python3 legacyfuzz.py run --ver /f/55/TSVZ.py --seed 1 --n 400 --jobs 8
  check  take cases (JSON lines: {"text":..., "args":{...}}) and print which invariants each fails
         python3 legacyfuzz.py check --ver /f/55/TSVZ.py --cases /f/cases.jsonl
  pair   paired differential on the same cases: counts and shrinks inputs that fail in --ver-b but pass in --ver
         python3 legacyfuzz.py pair --ver /f/54/TSVZ.py --ver-b /f/55/TSVZ.py --seed 1 --n 60000 --jobs 8

--ver, --ver-b and --old take the path of a TSVZ.py file; its label (in module names and the output) is the name
of the folder holding it (`/f/54/TSVZ.py` is 54), or the file's own name when it is not called TSVZ.py. It starts
processes and writes temporary files: run it in a container with capped memory and pids, never on a host as root, e.g.
  docker run --rm --init --memory=2g --memory-swap=2g --pids-limit=512 --user 65534:65534 -e HOME=/tmp \
    -v "$PWD":/t:ro -v /path/to/old:/f:ro -w /tmp python:3.14 python3 /t/legacyfuzz.py pair --ver /f/54/TSVZ.py --ver-b /t/TSVZ.py ...

Invariants (every check runs on a fresh copy of the file; "the reader" is readTabularFile given the
scrub's own header, defaults and strict):
  1x/1a-1e/1o  same-arguments view: the reader's dict is the same before and after the scrub; the letter is the kind
      of change: x the reader raises after, a a live key is gone, b a key appeared, c a row got narrower,
      d a cell changed, e a row only got wider, o only the key order changed
  1t  same as 1 for TSVZed(..., rewrite_on_load=False, monitor_external_changes=False)
  2r  no cell of the file's own bytes lost (parsed on the plain rules, independent of any reader; 1-column files skipped)
  2   no cell lost: with the non-strict reader, every key and every non-empty cell it had is still there
  3   idempotent: a second scrub gives the same bytes as the first
  4   delete-all cycle: 5 x (append 3 keys, delete them, scrub) leaves the same size after every scrub
  4b  the size after those scrubs also equals the size of the plain scrub (reported apart)
  5   header kept: a file whose first line is H, scrubbed with header '' or H, is read by
      readTabularFile(header=H, strict=True) without raising (if it was before)
  6   the scrub raised (judge by exception type / 5.1 on the same input)
"""
import argparse, collections, contextlib, importlib.util, io, itertools, json, multiprocessing, os, random, shutil, sys, tempfile, threading, time



def label(path):
	"""The name a TSVZ.py path goes by: its folder's name (or the file's own name when it is not TSVZ.py)."""
	path = os.path.abspath(path)
	name = os.path.basename(path)
	if name == 'TSVZ.py' and os.path.basename(os.path.dirname(path)):
		return os.path.basename(os.path.dirname(path))
	return os.path.splitext(name)[0]
T = None  # the TSVZ module
TMP = None

# --------------------------------------------------------------------------- argument sets
HEADERS = ['', 'id\tv', 'id\tv\tw']
DEFAULTS = [None, ['#_defaults_#', 'P'], ['#_defaults_#', 'P', 'Q'], ['#_defaults_#', '', '']]  # None = `...`
STRICTS = [False, True]
COMBOS = [dict(header=h, defaults=d, strict=s) for h in HEADERS for d in DEFAULTS for s in STRICTS]


def load(ver):
	global T, TMP
	spec = importlib.util.spec_from_file_location('TSVZ_' + label(ver), ver)
	T = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = T
	spec.loader.exec_module(T)
	track(T)
	base = '/dev/shm' if os.path.isdir('/dev/shm') and os.access('/dev/shm', os.W_OK) else '/tmp'
	TMP = tempfile.mkdtemp(prefix='sf', dir=base)


def worker_init(ver):
	load(ver)


# --------------------------------------------------------------------------- generator
VALS = ['1', '2', 'X', '']
KEYS = ['a', 'b', 'c']
D = '#_defaults_#'


def gen_text(rng):
	lines = []
	r = rng.random()
	if r < 0.15:
		lines.append('')
	elif r < 0.50:
		lines.append(rng.choice(['id\tv', 'id\tv\tw']))
		if rng.random() < 0.04:
			lines.insert(0, '')
	for _ in range(rng.randint(0, 8)):
		k = rng.random()
		if k < 0.50:
			n = rng.randint(1, 5)
			lines.append('\t'.join([rng.choice(KEYS)] + [rng.choice(VALS) for _ in range(n - 1)]))
		elif k < 0.68:
			n = rng.randint(0, 4)
			lines.append('\t'.join([D] + [rng.choice(VALS) for _ in range(n)]))
		elif k < 0.76:
			lines.append('\t'.join([D] + [''] * rng.randint(0, 3)))  # bare / all-empty defaults (a reset)
		elif k < 0.90:
			lines.append('\t'.join([rng.choice(KEYS)] + [''] * rng.randint(0, 3)))  # deletion
		elif k < 0.96:
			lines.append('\t'.join(['#c'] + [rng.choice(VALS) for _ in range(rng.randint(0, 2))]))
		else:
			lines.append('')
	text = '\n'.join(lines) + ('\n' if lines else '')
	if lines and rng.random() < 0.05:
		text = text.rstrip('\n')
	return text


# --------------------------------------------------------------------------- helpers
def dflt(args):
	return ... if args['defaults'] is None else list(args['defaults'])


def write(path, text):
	with open(path, 'wb') as f:
		f.write(text.encode('utf8'))


def readb(path):
	with open(path, 'rb') as f:
		return f.read()


def quiet(fn, *a, **k):
	"""Run fn with stdout/stderr swallowed; returns ('ok', value) or ('exc', 'Type: msg')."""
	out, err = io.StringIO(), io.StringIO()
	try:
		with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			return 'ok', fn(*a, **k)
	except Exception as e:
		return 'exc', '%s: %s' % (type(e).__name__, str(e)[:160])


def view_of(d):
	return [[k, list(v)] for k, v in d.items()]


def reader(path, args, strict):
	k = dict(header=args['header'], defaults=dflt(args), strict=strict)
	st, v = quiet(T.readTabularFile, path, **k)
	return (st, view_of(v)) if st == 'ok' else (st, v)


def tsvzed_view(path, args):
	def go():
		s = T.TSVZed(path, header=args['header'], defaults=None if args['defaults'] is None else list(args['defaults']),
					 strict=args['strict'], createIfNotExist=False, rewrite_on_load=False, monitor_external_changes=False)
		try:
			return view_of(s)
		finally:
			s.close()
	return quiet(go)


def scrub(path, args):
	return quiet(T.scrubTabularFile, path, header=args['header'], defaults=dflt(args), strict=args['strict'])


def canon(v):
	"""status tuple -> comparable"""
	return v[0], json.dumps(v[1], sort_keys=False)


def raw_rows(text):
	"""{key: cells} of the live rows of the file's bytes, parsed on the plain rules (last line wins, a line with a key
	and nothing else deletes it, defaults and comment lines skipped), or None for a file whose first keyed line is one
	cell wide (a 1-column file: whether a bare key is a row or a deletion depends on the reader)."""
	rows = collections.OrderedDict()
	first = True
	for line in text.split('\n'):
		if not line or (line.startswith('#') and not line.startswith(D)):
			continue
		cells = line.split('\t')
		if cells[0] == D or not cells[0]:
			continue
		if first and len(cells) == 1:
			return None
		first = False
		if not any(cells[1:]):
			rows.pop(cells[0], None)
		else:
			rows[cells[0]] = cells
	return rows


SAME = {'1x', '1a', '1b', '1c', '1d', '1e', '1o'}
CSAME = {'cx', 'ca', 'cb', 'cc', 'cd', 'ce', 'co'}  # compat: the OLD module's reader, same args, before vs after
SEVEN = {'7x', '7t', '7l', '7r', '7c', '7i'}  # a TSVZed rewrite (hardMapToFile): raised, TSVZed view, loose cells, raw cells, compat, idempotent
OLD = None  # module whose readTabularFile is the compat reader


def group_of(inv):
	return SAME if inv in SAME else CSAME if inv in CSAME else {inv}


def with_module(mod, fn, *a):
	global T
	saved = T
	T = mod
	try:
		return fn(*a)
	finally:
		T = saved


def raw_lost(text, after_text):
	raw = raw_rows(text)
	if not raw:
		return None
	out = raw_rows(after_text)
	if out is None:
		return None
	lost = []
	for k, cells in raw.items():
		if k not in out:
			lost.append([k, 'KEY GONE'])
			continue
		bad = {i: c for i, c in enumerate(cells) if c and (i >= len(out[k]) or out[k][i] != c)}
		if bad:
			lost.append([k, bad])
	return lost or None


def kind_of(before, after):
	"""how a view changed: 1a a live key is gone, 1b a key appeared, 1c a row got narrower, 1d a cell changed,
	1e a row only got wider (after starts with before), 1o only the order changed"""
	b = {k: r for k, r in before}
	a = {k: r for k, r in after}
	if any(k not in a for k in b):
		return '1a'
	if any(k not in b for k in a):
		return '1b'
	if any(len(a[k]) < len(b[k]) for k in b):
		return '1c'
	if any(a[k][:len(b[k])] != b[k] for k in b):
		return '1d'
	if any(len(a[k]) > len(b[k]) for k in b):
		return '1e'
	return '1o'


def cells_missing(before, after):
	"""keys / non-empty cells of `before` (list of [k, row]) not found in `after`."""
	a = {k: row for k, row in after}
	miss = []
	for k, row in before:
		if k not in a:
			miss.append([k, 'KEY GONE'])
			continue
		need = collections.Counter(c for c in row[1:] if c)
		have = collections.Counter(a[k][1:])
		lost = need - have
		if lost:
			miss.append([k, dict(lost)])
	return miss


def first_cols(text):
	"""the width a reader takes for appended lines: the first keyed line's, else the first defaults line's, else 0"""
	dwidth = 0
	for line in text.split('\n'):
		if not line or (line.startswith('#') and not line.startswith(D)):
			continue
		if line.startswith(D):
			dwidth = dwidth or len(line.split('\t'))
			continue
		return len(line.split('\t'))
	return dwidth


# --------------------------------------------------------------------------- the checks
LIVE = []  # every TSVZed made while a case runs: stopped after it (reap)


def track(mod):
	"""Note every TSVZed ``mod`` makes, so reap() stops its thread. 3.39's close() leaves its append thread running when
	its final rewrite raises (and a constructor that raises is never closed): the leaked threads filled the container's
	pids limit and the 3.39 pair run stalled ("can't start new thread")."""
	cls = getattr(mod, 'TSVZed', None)
	if cls is None or getattr(cls, '_fuzzTracked', False):
		return
	init = cls.__init__

	def tracked(self, *args, **kwargs):
		LIVE.append(self)
		return init(self, *args, **kwargs)
	cls.__init__ = tracked
	cls._fuzzTracked = True


def reap():
	"""Stop the threads of the TSVZeds the case made (each was closed, or its constructor raised)."""
	while LIVE:
		o = LIVE.pop()
		event = getattr(o, 'shutdownEvent', None)
		if event is not None:
			event.set()
		thread = getattr(o, 'appendThread', None)
		if thread is not None and thread.is_alive() and thread is not threading.current_thread():
			thread.join(5)


def check_case(text, args, tsvzed=True, want=None):
	try:
		return _check_case(text, args, tsvzed, want)
	finally:
		reap()


def _check_case(text, args, tsvzed=True, want=None):
	"""-> dict invariant -> info (only the failing ones), plus '_scrubbed' bytes when it ran."""
	res = {}
	orig, work, extra = (os.path.join(TMP, n) for n in ('orig.tsv', 'work.tsv', 'extra.tsv'))
	write(orig, text)
	before_strict = reader(orig, args, True)
	before_loose = reader(orig, args, False)
	before_same = before_strict if args['strict'] else before_loose
	write(extra, text)
	tsvzed = tsvzed and (want is None or '1t' in want)
	before_ts = tsvzed_view(extra, args) if tsvzed else None

	want_c = OLD is not None and (want is None or bool(want & CSAME))
	old_before = with_module(OLD, reader, orig, args, args['strict']) if want_c else None
	want_cs = OLD is not None and (want is None or 'cs' in want)
	old_other = with_module(OLD, reader, orig, args, not args['strict']) if want_cs else None
	if want is None or want & SEVEN:
		check_rewrite(text, args, res)

	write(work, text)
	st, val = scrub(work, args)
	if st == 'exc':
		res['6'] = {'exc': val}
		return res
	s1 = readb(work)
	res['_scrubbed'] = s1.decode('utf8', 'replace')
	if want_c and old_before[0] == 'ok':
		old_after = with_module(OLD, reader, work, args, args['strict'])
		if old_after[0] != 'ok':
			res['cx'] = {'before': old_before[1], 'after': old_after[1], 'scrubbed': res['_scrubbed']}
		elif old_after[1] != old_before[1]:
			res['c' + kind_of(old_before[1], old_after[1])[1]] = {'before': old_before[1], 'after': old_after[1], 'scrubbed': res['_scrubbed']}
	if want_cs and old_other[0] == 'ok':
		# cs (informative): the OLD reader of the other strictness, same header and defaults
		oo = with_module(OLD, reader, work, args, not args['strict'])
		if oo != old_other:
			res['cs'] = {'before': old_other[1], 'after': oo[1], 'scrubbed': res['_scrubbed']}
	# oo / no: readers with OTHER arguments (old module / this one), before and after this scrub
	for inv, mod in (('oo', OLD), ('no', T)):
		if mod is None or not (want is None or inv in want):
			continue
		for oargs in OTHER_ARGS:
			if oargs == args:
				continue
			b = with_module(mod, reader, orig, oargs, oargs['strict'])
			if b[0] != 'ok':
				continue
			a = with_module(mod, reader, work, oargs, oargs['strict'])
			if a != b:
				res[inv] = {'reader': oargs, 'before': b[1], 'after': a[1], 'scrubbed': res['_scrubbed']}
				break
	# ol: an OLD TSVZed open on the file while this version scrubs it, then follows the change and appends
	if OLD is not None and (want is None or 'ol' in want):
		check_live_old(text, args, res)

	# 2r: no cell of the file's own bytes lost (independent of any reader: raw_rows)
	if want is None or '2r' in want:
		raw = raw_rows(text)
		if raw:
			out = raw_rows(res['_scrubbed'])
			if out is not None:
				lost = []
				for k, cells in raw.items():
					if k not in out:
						lost.append([k, 'KEY GONE'])
						continue
					bad = {i: c for i, c in enumerate(cells) if c and (i >= len(out[k]) or out[k][i] != c)}
					if bad:
						lost.append([k, bad])
				if lost:
					res['2r'] = {'lost': lost, 'raw': [list(c) for c in raw.values()], 'out': [list(c) for c in out.values()], 'scrubbed': res['_scrubbed']}
	# 1x / 1a-1e / 1o: same-arguments view (the letter is the kind of change, see kind_of)
	if want is None or want & SAME:
		after_same = reader(work, args, args['strict'])
		if before_same[0] == 'ok':
			if after_same[0] != 'ok':
				res['1x'] = {'before': before_same[1], 'after': after_same[1], 'scrubbed': res['_scrubbed']}
			elif before_same[1] != after_same[1]:
				res[kind_of(before_same[1], after_same[1])] = {'before': before_same[1], 'after': after_same[1], 'scrubbed': res['_scrubbed']}
	# 1s: the reader of the other strictness (same header and defaults) keeps its view too (informative)
	if want is None or '1s' in want:
		before_other = before_loose if args['strict'] else before_strict
		if before_other[0] == 'ok':
			after_other = reader(work, args, not args['strict'])
			if after_other != before_other:
				res['1s'] = {'before': before_other[1], 'after': after_other[1], 'scrubbed': res['_scrubbed']}
	# 1t: TSVZed
	if tsvzed and before_ts and before_ts[0] == 'ok':
		after_ts = tsvzed_view(work, args)
		if after_ts != before_ts:
			res['1t'] = {'before': before_ts[1], 'after': after_ts[1], 'scrubbed': res['_scrubbed']}
	# 2: no cell lost, non-strict reader
	after_loose = reader(work, args, False) if (want is None or '2' in want) else ('skip', None)
	if before_loose[0] == 'ok' and after_loose[0] != 'skip':
		if after_loose[0] != 'ok':
			res['2'] = {'before': before_loose[1], 'after': after_loose[1], 'scrubbed': res['_scrubbed']}
		else:
			miss = cells_missing(before_loose[1], after_loose[1])
			if miss:
				res['2'] = {'before': before_loose[1], 'after': after_loose[1], 'lost': miss, 'scrubbed': res['_scrubbed']}
	# 3: idempotent
	if want is not None and not (want & {'3', '4', '4b', '5'}):
		return res
	st2, _ = scrub(work, args)
	s2 = readb(work)
	if st2 == 'exc':
		res['6'] = {'exc': 'second scrub: ' + _}
	elif s2 != s1:
		res['3'] = {'first': s1.decode('utf8', 'replace'), 'second': s2.decode('utf8', 'replace')}
	# 4: delete-all cycle on the scrubbed file
	t = s2.decode('utf8', 'replace')
	# the cycle appends rows as wide as this reader takes them (a writer with the same arguments writes them so)
	H = args['header']
	if H and t.split('\n')[0] == H:
		w = len(H.split('\t'))
	elif args['defaults'] and len(args['defaults']) > 1:
		w = len(args['defaults'])
	else:
		w = first_cols(t.split('\n', 1)[1] if H and '\n' in t else t)
	if w >= 2 and '6' not in res and (want is None or want & {'4', '4b'}):
		base = len(s2)
		sizes = []
		cur = t
		for cyc in range(5):
			keys = ['k%d_%d' % (cyc, i) for i in range(3)]
			cur_b = readb(work).decode('utf8', 'replace')
			if cur_b and not cur_b.endswith('\n'):
				cur_b += '\n'
			add = ''.join('\t'.join([k] + ['1'] * (w - 1)) + '\n' for k in keys)
			add += ''.join('\t'.join([k] + [''] * (w - 1)) + '\n' for k in keys)
			write(work, cur_b + add)
			if cyc == 0:
				# the cycle applies only where its deletion lines delete for this reader (not in a 1-column file)
				seen = reader(work, args, False)
				if seen[0] != 'ok' or any(k in keys for k, _ in seen[1]):
					break
			st3, v3 = scrub(work, args)
			if st3 == 'exc':
				res['6'] = {'exc': 'cycle scrub: ' + v3}
				break
			sizes.append(len(readb(work)))
		else:
			if len(set(sizes)) != 1:
				res['4'] = {'sizes': sizes, 'plain': base, 'after': readb(work).decode('utf8', 'replace')}
			elif sizes[0] != base:
				res['4b'] = {'sizes': sizes, 'plain': base, 'after': readb(work).decode('utf8', 'replace')}
	# 5: header kept
	lines = text.split('\n')
	H = args['header']
	if text and lines[0] in HEADERS[1:] and H in ('', lines[0]) and (want is None or '5' in want):
		Hh = lines[0]
		pre = quiet(T.readTabularFile, orig, header=Hh, strict=True, defaults=...)
		if pre[0] == 'ok':
			post = quiet(T.readTabularFile, work if False else _rescrub_path(text, args), header=Hh, strict=True, defaults=...)
			if post[0] != 'ok':
				res['5'] = {'exc': post[1], 'scrubbed': res['_scrubbed']}
	return res


OTHER_ARGS = [dict(header='', defaults=None, strict=False), dict(header='', defaults=None, strict=True),
			  dict(header='id\tv', defaults=None, strict=False), dict(header='id\tv', defaults=['#_defaults_#', 'P'], strict=False)]


def check_live_old(text, args, res):
	"""ol: an OLD TSVZed holds the file open (watching it) while this version scrubs it; after it follows the change
	its view is what it was, and a row it then sets reads in both versions alongside what was there."""
	p = os.path.join(TMP, 'live.tsv')
	write(p, text)
	def go():
		o = OLD.TSVZed(p, header=args['header'], defaults=None if args['defaults'] is None else list(args['defaults']),
					   strict=args['strict'], createIfNotExist=False, rewrite_on_load=False, monitor_external_changes=True)
		try:
			before = view_of(o)
			quiet(T.scrubTabularFile, p, header=args['header'], defaults=dflt(args), strict=args['strict'])
			o.checkExternalChanges()
			if getattr(o, 'deSynced', False):
				if hasattr(o, '_syncFile'):
					o._call(o._syncFile)  # the incremental follow (5.x), as its writer thread runs it on a change
				else:
					o.reload()  # (3.39 follows by reading again)
			after = view_of(o)
			width = max([len(r) for _, r in before] or [2])
			if getattr(o, 'correctColumnNum', -1) > 1:  # (the store's own width: a strict store refuses another one)
				width = o.correctColumnNum
			o['zz'] = ['zz'] + ['9'] * (max(width, 2) - 1)
			o.commitAppendToFile() if hasattr(o, 'commitAppendToFile') else None
		finally:
			o.close()
		return before, after
	st, val = quiet(go)
	if st != 'ok':
		return
	before, after = val
	if after != before:
		res['ol'] = {'before': before, 'after': after, 'stage': 'follow'}
		return
	p0 = os.path.join(TMP, 'live0.tsv')
	write(p0, text)
	for name, mod in (('old', OLD), ('new', T)):
		b = with_module(mod, reader, p0, args, False)
		a = with_module(mod, reader, p, args, False)
		if b[0] != 'ok' or a[0] != 'ok':
			continue
		bd = dict((k, r) for k, r in b[1])
		ad = dict((k, r) for k, r in a[1])
		zz = ad.pop('zz', None)
		bd.pop('zz', None)
		# keys only: an old writer's own rewrite cuts cells past its width, as it always has
		if zz is None or set(ad) != set(bd):
			res['ol'] = {'reader': name, 'before': b[1], 'after': a[1], 'stage': 'append', 'file': readb(p).decode('utf8', 'replace')}
			return


def tsvzed_rewrite(path, args):
	def go():
		s = T.TSVZed(path, header=args['header'], defaults=None if args['defaults'] is None else list(args['defaults']),
					 strict=args['strict'], createIfNotExist=False, rewrite_on_load=False, monitor_external_changes=False)
		try:
			s.hardMapToFile()
		finally:
			s.close()
	return quiet(go)


def check_rewrite(text, args, res):
	"""7*: a TSVZed (same args) opened on the file and rewritten whole (hardMapToFile)."""
	p = os.path.join(TMP, 'rw.tsv')
	write(p, text)
	tv_before = tsvzed_view(p, args)
	loose_before = reader(p, args, False)
	old_before = with_module(OLD, reader, p, args, False) if OLD is not None else None
	st, val = tsvzed_rewrite(p, args)
	if st == 'exc':
		res['7x'] = {'exc': val}
		return
	b1 = readb(p).decode('utf8', 'replace')
	res['_rewritten'] = b1
	tv_after = tsvzed_view(p, args)
	if tv_before[0] == 'ok' and tv_after != tv_before:
		res['7t'] = {'before': tv_before[1], 'after': tv_after[1], 'rewritten': b1}
	if loose_before[0] == 'ok':
		la = reader(p, args, False)
		miss = cells_missing(loose_before[1], la[1]) if la[0] == 'ok' else 'raised'
		if miss:
			res['7l'] = {'before': loose_before[1], 'after': la[1], 'lost': miss, 'rewritten': b1}
	lost = raw_lost(text, b1)
	if lost:
		res['7r'] = {'lost': lost, 'rewritten': b1}
	if old_before is not None and old_before[0] == 'ok':
		oa = with_module(OLD, reader, p, args, False)
		if oa != old_before:
			res['7c'] = {'before': old_before[1], 'after': oa[1], 'rewritten': b1}
	st2, _ = tsvzed_rewrite(p, args)
	if st2 == 'ok' and readb(p).decode('utf8', 'replace') != b1:
		res['7i'] = {'first': b1, 'second': readb(p).decode('utf8', 'replace')}


def _rescrub_path(text, args):
	"""a fresh scrub of `text` (the work file was changed by the cycle check)"""
	p = os.path.join(TMP, 'h5.tsv')
	write(p, text)
	scrub(p, args)
	return p


# --------------------------------------------------------------------------- shrinking
def failing(text, args, inv, tsvzed, exc_type=None):
	want = {inv, '4b'} if inv == '4' else {inv, '4'} if inv == '4b' else set(group_of(inv)) if inv in SAME | CSAME else {inv}
	if inv == '6':
		want = None
	r = check_case(text, args, tsvzed=tsvzed, want=want)
	if inv not in r:
		return False
	if inv == '6' and exc_type:
		return exc_type in r['6']['exc']
	return True


def simpler_args(args):
	out = []
	if args['header']:
		out.append(dict(args, header=''))
		if args['header'] == 'id\tv\tw':
			out.append(dict(args, header='id\tv'))
	if args['defaults'] is not None:
		out.append(dict(args, defaults=None))
		if len(args['defaults']) > 2:
			out.append(dict(args, defaults=args['defaults'][:2]))
	if args['strict']:
		out.append(dict(args, strict=False))
	return out


def text_candidates(text):
	lines = text.split('\n')
	trailing = text.endswith('\n')
	if trailing:
		lines = lines[:-1]
	def mk(ls, nl=None):
		return '\n'.join(ls) + ('\n' if (trailing if nl is None else nl) and ls else '')
	# drop lines
	for i in range(len(lines)):
		yield mk(lines[:i] + lines[i + 1:])
	if text and not trailing:
		yield text + '\n'
	elif text:
		yield mk(lines, False)
	# drop a cell, blank a cell, normalise a cell
	for i, ln in enumerate(lines):
		cells = ln.split('\t')
		for j in range(len(cells)):
			if len(cells) > 1:
				c2 = cells[:j] + cells[j + 1:]
				yield mk(lines[:i] + ['\t'.join(c2)] + lines[i + 1:])
			if cells[j] and j > 0 and cells[j] != '':
				c2 = cells[:j] + [''] + cells[j + 1:]
				yield mk(lines[:i] + ['\t'.join(c2)] + lines[i + 1:])
			if cells[j] not in ('', '1', D, 'id', 'v', 'w') and j > 0:
				c2 = cells[:j] + ['1'] + cells[j + 1:]
				yield mk(lines[:i] + ['\t'.join(c2)] + lines[i + 1:])
		if cells[0] not in (D, 'a', 'id') and not cells[0].startswith('#'):
			c2 = ['a'] + cells[1:]
			yield mk(lines[:i] + ['\t'.join(c2)] + lines[i + 1:])
	# drop two lines at once
	for i in range(len(lines)):
		for j in range(i + 1, len(lines)):
			yield mk(lines[:i] + lines[i + 1:j] + lines[j + 1:])
	# rename keys by first appearance to a, b, c; every non-empty value to 1
	names = {}
	out = []
	for ln in lines:
		cells = ln.split('\t')
		if cells[0] and not cells[0].startswith('#') and cells[0] not in ('id',):
			cells[0] = names.setdefault(cells[0], 'abc'[len(names) % 3])
		out.append('\t'.join(cells))
	yield mk(out)
	out = []
	for ln in lines:
		cells = ln.split('\t')
		out.append('\t'.join(c if (i == 0 or not c or cells[0] == 'id') else '1' for i, c in enumerate(cells)))
	yield mk(out)


def shrink(text, args, inv, tsvzed, exc_type=None):
	budget = 4000
	changed = True
	while changed and budget > 0:
		changed = False
		for a2 in simpler_args(args):
			budget -= 1
			if failing(text, a2, inv, tsvzed, exc_type):
				args, changed = a2, True
				break
		if changed:
			continue
		for cand in text_candidates(text):
			if cand == text or len(cand) > len(text) and False:
				continue
			budget -= 1
			if failing(cand, args, inv, tsvzed, exc_type):
				text, changed = cand, True
				break
			if budget <= 0:
				break
	return text, args


# --------------------------------------------------------------------------- run
def case_rng(seed, i):
	return random.Random('%s:%d' % (seed, i))


def work_chunk(job):
	seed, lo, hi, tsvzed, keep = job
	stats = collections.Counter()
	small = collections.defaultdict(list)  # (inv, combo idx) -> [(len, i, text)] smallest `keep`
	t0 = time.time()
	for i in range(lo, hi):
		text = gen_text(case_rng(seed, i))
		stats['files'] += 1
		for ci, args in enumerate(COMBOS):
			stats['cases'] += 1
			try:
				r = check_case(text, args, tsvzed=tsvzed)
			except Exception as e:  # a harness problem, not a finding: count it
				stats['harness_error'] += 1
				stats['harness_error:%s' % type(e).__name__] += 1
				continue
			for inv, info in r.items():
				if inv.startswith('_'):
					continue
				stats['fail:' + inv] += 1
				key = (inv, ci)
				lst = small[key]
				lst.append((len(text), i, text, info.get('exc', '') if inv == '6' else ''))
				lst.sort(key=lambda x: (x[0], x[1]))
				del lst[keep:]
	stats['secs'] = time.time() - t0
	return stats, dict(small)


def shrink_job(job):
	inv, ci, text, exc, tsvzed = job
	args = COMBOS[ci]
	exc_type = exc.split(':')[0] if exc else None
	t, a = shrink(text, args, inv, tsvzed, exc_type)
	r = check_case(t, a, tsvzed=tsvzed)
	return inv, ci, text, t, a, r.get(inv), r.get('_scrubbed')


def run(a):
	ctx = multiprocessing.get_context('fork')
	chunk = max(1, a.n // (a.jobs * 8))
	jobs = [(a.seed, lo, min(a.n, lo + chunk), not a.no_tsvzed, a.keep) for lo in range(0, a.n, chunk)]
	t0 = time.time()
	stats = collections.Counter()
	small = collections.defaultdict(list)
	with ctx.Pool(a.jobs, initializer=worker_init, initargs=(a.ver,)) as pool:
		for st, sm in pool.imap_unordered(work_chunk, jobs):
			secs = st.pop('secs')
			stats.update(st)
			for k, v in sm.items():
				small[k] += v
				small[k].sort(key=lambda x: (x[0], x[1]))
				del small[k][a.keep:]
		print('# version %s seed %s files %d, case-args %d, harness errors %d, fuzz %.0fs' % (
			label(a.ver), a.seed, stats['files'], stats['cases'], stats['harness_error'], time.time() - t0), flush=True)
		print('# failing counts (raw, per file x arg set): ' + json.dumps({k[5:]: v for k, v in sorted(stats.items()) if k.startswith('fail:')}), flush=True)
		for k, v in sorted(stats.items()):
			if k.startswith('harness_error:'):
				print('# ' + k, v, flush=True)
		sj = [(inv, ci, text, exc, not a.no_tsvzed) for (inv, ci), lst in sorted(small.items()) for (_, _, text, exc) in lst]
		print('# shrinking %d candidates' % len(sj), flush=True)
		seen = {}
		for inv, ci, orig, t, args, info, scr in pool.imap_unordered(shrink_job, sj, chunksize=1):
			if info is None:
				continue
			key = (inv, t, json.dumps(args, sort_keys=True))
			if key in seen:
				seen[key]['hits'] += 1
				continue
			seen[key] = {'ver': label(a.ver), 'inv': inv, 'text': t, 'args': args, 'info': info, 'scrubbed': scr, 'hits': 1}
	for key in sorted(seen, key=lambda k: (k[0], len(k[1]), k[1])):
		print(json.dumps(seen[key]), flush=True)
	print('# done %.0fs total' % (time.time() - t0), flush=True)



# --------------------------------------------------------------------------- pair mode: paired regressions
MODS = {}


def load_pair(a_ver, b_ver):
	global T, TMP
	for k, v in (('a', a_ver), ('b', b_ver)):
		spec = importlib.util.spec_from_file_location('TSVZ_' + label(v), v)
		mod = importlib.util.module_from_spec(spec)
		sys.modules[spec.name] = mod
		spec.loader.exec_module(mod)
		track(mod)
		MODS[k] = mod
	base = '/dev/shm' if os.path.isdir('/dev/shm') and os.access('/dev/shm', os.W_OK) else '/tmp'
	TMP = tempfile.mkdtemp(prefix='sp', dir=base)


def pair_init(a_ver, b_ver, old_ver=None):
	global OLD
	load_pair(a_ver, b_ver)
	if old_ver:
		spec = importlib.util.spec_from_file_location('TSVZ_old_' + label(old_ver), old_ver)
		mod = importlib.util.module_from_spec(spec)
		sys.modules[spec.name] = mod
		spec.loader.exec_module(mod)
		track(mod)
		OLD = mod
	else:
		OLD = MODS['a']


def cc(which, text, args, tsvzed, want=None):
	global T
	T = MODS[which]
	return check_case(text, args, tsvzed=tsvzed, want=want)


def regressed(text, args, inv, tsvzed):
	"""inv fails in b and (for the 1x-1o kinds: no kind of it) not in a"""
	grp = group_of(inv)
	want = {inv, '4b'} if inv == '4' else {inv, '4'} if inv == '4b' else set(grp)
	if inv == '6':
		want = None
	rb = cc('b', text, args, tsvzed, want)
	if inv not in rb:
		return False
	ra = cc('a', text, args, tsvzed, want)
	return not any(k in ra for k in grp)


def pair_chunk(job):
	seed, lo, hi, tsvzed, keep = job
	stats = collections.Counter()
	small = collections.defaultdict(list)
	for i in range(lo, hi):
		text = gen_text(case_rng(seed, i))
		stats['files'] += 1
		for ci, args in enumerate(COMBOS):
			stats['cases'] += 1
			ra = cc('a', text, args, tsvzed)
			rb = cc('b', text, args, tsvzed)
			ga = {('1*' if k in SAME else 'c*' if k in CSAME else k) for k in ra if not k.startswith('_')}
			gb = {('1*' if k in SAME else 'c*' if k in CSAME else k) for k in rb if not k.startswith('_')}
			for g in gb - ga:
				stats['reg:' + g] += 1
			for g in ga - gb:
				stats['fixed:' + g] += 1
			for inv in rb:
				if inv.startswith('_'):
					continue
				grp = group_of(inv)
				if any(k in ra for k in grp):
					continue
				stats['regkind:' + inv] += 1
				lst = small[(inv, ci)]
				lst.append((len(text), i, text))
				lst.sort(key=lambda x: (x[0], x[1]))
				del lst[keep:]
	return stats, dict(small)


def pair_shrink(job):
	inv, ci, text, tsvzed = job
	args = COMBOS[ci]
	budget = 3000
	changed = True
	while changed and budget > 0:
		changed = False
		for a2 in simpler_args(args):
			budget -= 1
			if regressed(text, a2, inv, tsvzed):
				args, changed = a2, True
				break
		if changed:
			continue
		for cand in text_candidates(text):
			if cand == text:
				continue
			budget -= 1
			if regressed(cand, args, inv, tsvzed):
				text, changed = cand, True
				break
			if budget <= 0:
				break
	rb = cc('b', text, args, tsvzed)
	ra = cc('a', text, args, tsvzed)
	return inv, text, args, rb.get(inv), rb.get('_scrubbed') if not inv.startswith('7') else rb.get('_rewritten'), ra.get('_scrubbed') if not inv.startswith('7') else ra.get('_rewritten')


def run_pair(a):
	ctx = multiprocessing.get_context('fork')
	chunk = max(1, a.n // (a.jobs * 8))
	jobs = [(a.seed, lo, min(a.n, lo + chunk), not a.no_tsvzed, a.keep) for lo in range(0, a.n, chunk)]
	t0 = time.time()
	stats = collections.Counter()
	small = collections.defaultdict(list)
	with ctx.Pool(a.jobs, initializer=pair_init, initargs=(a.ver, a.ver_b, a.old)) as pool:
		for st, sm in pool.imap_unordered(pair_chunk, jobs):
			stats.update(st)
			for k, v in sm.items():
				small[k] += v
				small[k].sort(key=lambda x: (x[0], x[1]))
				del small[k][a.keep:]
		print('# pair a=%s b=%s seed %s files %d, case-args %d, %.0fs' % (label(a.ver), label(a.ver_b), a.seed, stats['files'], stats['cases'], time.time() - t0), flush=True)
		print('# regressions (b fails, a passes, per file x arg set): ' + json.dumps({k[4:]: v for k, v in sorted(stats.items()) if k.startswith('reg:')}), flush=True)
		print('# fixed (a fails, b passes): ' + json.dumps({k[6:]: v for k, v in sorted(stats.items()) if k.startswith('fixed:')}), flush=True)
		print('# regressions by kind: ' + json.dumps({k[8:]: v for k, v in sorted(stats.items()) if k.startswith('regkind:')}), flush=True)
		sj = [(inv, ci, text, not a.no_tsvzed) for (inv, ci), lst in sorted(small.items()) for (_, _, text) in lst]
		print('# shrinking %d candidates' % len(sj), flush=True)
		seen = {}
		for inv, text, args, info, sb, sa in pool.imap_unordered(pair_shrink, sj, chunksize=1):
			if info is None:
				continue
			key = (inv, text, json.dumps(args, sort_keys=True))
			if key in seen:
				seen[key]['hits'] += 1
				continue
			seen[key] = {'a': label(a.ver), 'b': label(a.ver_b), 'inv': inv, 'text': text, 'args': args, 'info': info, 'scrubbed_b': sb, 'scrubbed_a': sa, 'hits': 1}
	for key in sorted(seen, key=lambda k: (k[0], len(k[1]), k[1])):
		print(json.dumps(seen[key]), flush=True)
	print('# done %.0fs total' % (time.time() - t0), flush=True)



def views_chunk(job):
	seed, lo, hi = job
	stats = collections.Counter()
	ex = {}
	for i in range(lo, hi):
		text = gen_text(case_rng(seed, i))
		p = os.path.join(TMP, 'v.tsv')
		write(p, text)
		for ci, args in enumerate(COMBOS):
			for name, fn in (('read', lambda m: with_module(m, reader, p, args, args['strict'])),
							 ('tsvzed', lambda m: with_module(m, tsvzed_view, p, args))):
				va, vb = fn(MODS['a']), fn(MODS['b'])
				if va == vb:
					continue
				kind = 'x' if 'exc' in (va[0], vb[0]) else kind_of(va[1], vb[1])
				k = '%s:%s:%s' % (name, 'S' if args['strict'] else 'n', kind)
				stats[k] += 1
				if k not in ex or len(text) < len(ex[k]['text']):
					ex[k] = {'text': text, 'args': args, 'a': va[1], 'b': vb[1]}
	return stats, ex


def run_views(a):
	ctx = multiprocessing.get_context('fork')
	chunk = max(1, a.n // (a.jobs * 8))
	jobs = [(a.seed, lo, min(a.n, lo + chunk)) for lo in range(0, a.n, chunk)]
	stats = collections.Counter()
	ex = {}
	with ctx.Pool(a.jobs, initializer=pair_init, initargs=(a.ver, a.ver_b)) as pool:
		for st, e in pool.imap_unordered(views_chunk, jobs):
			stats.update(st)
			for k, v in e.items():
				if k not in ex or len(v['text']) < len(ex[k]['text']):
					ex[k] = v
	print('# views a=%s b=%s seed %s files %d: reader differences (reader:strict:kind, per file x arg set)' % (label(a.ver), label(a.ver_b), a.seed, a.n))
	print('# ' + json.dumps(dict(sorted(stats.items()))))
	for k in sorted(ex):
		print(json.dumps(dict(ex[k], kind=k)))


def check(a):
	load(a.ver)
	with open(a.cases) as f:
		for line in f:
			if not line.strip() or line.startswith('#'):
				continue
			c = json.loads(line)
			args = c['args']
			r = check_case(c['text'], args, tsvzed=not a.no_tsvzed)
			print(json.dumps({'id': c.get('id'), 'ver': label(a.ver), 'text': c['text'], 'args': args,
							  'fails': {k: v for k, v in r.items() if not k.startswith('_')}, 'scrubbed': r.get('_scrubbed')}), flush=True)


if __name__ == '__main__':
	ap = argparse.ArgumentParser()
	sub = ap.add_subparsers(dest='mode', required=True)
	for name in ('run', 'check', 'pair', 'views'):
		p = sub.add_parser(name)
		p.add_argument('--ver', required=True)
		p.add_argument('--no-tsvzed', action='store_true')
		p.add_argument('--old', default=None)
		if name in ('pair', 'views'):
			p.add_argument('--ver-b', required=True)
		if name in ('run', 'pair', 'views'):
			p.add_argument('--seed', default='1')
			p.add_argument('--n', type=int, default=200)
			p.add_argument('--jobs', type=int, default=8)
			p.add_argument('--keep', type=int, default=5)
		else:
			p.add_argument('--cases', required=True)
	a = ap.parse_args()
	if a.mode == 'pair':
		run_pair(a)
	elif a.mode == 'views':
		run_views(a)
	elif a.mode == 'run':
		load(a.ver)  # (workers load it again; this checks it imports)
		run(a)
	else:
		check(a)
