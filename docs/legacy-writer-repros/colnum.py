# correctColumnNum: written by __setitem__ (no lock) and by reload (main: no lock; branch: under _memLock).
import sys, inspect
sys.path.insert(0, sys.argv[1])
import TSVZ
for name in ('__setitem__', '_reload', 'reload'):
    f = getattr(TSVZ.TSVZed, name, None)
    if f is None: continue
    src = inspect.getsource(f)
    print(name, 'writes correctColumnNum:', 'self.correctColumnNum =' in src, '| takes _memLock:', '_memLock' in src)
