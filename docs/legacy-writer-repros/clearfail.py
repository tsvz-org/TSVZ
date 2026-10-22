import sys, os, time
sys.path.insert(0, sys.argv[1])
import TSVZ
p = 'clearfail.tsv'
if os.path.exists(p): os.remove(p)
with open(p, 'wb') as f: f.write(b'key\tv\nk0\t0\nk1\t1\n')
t = TSVZ.TSVZed(p, header='key\tv')
time.sleep(0.1)
real = TSVZ.openFileAsCompressed
def deny(fileName, mode='rb', *a, **k):
    if mode == 'wb':
        raise PermissionError(13, 'Permission denied')
    return real(fileName, mode, *a, **k)
TSVZ.openFileAsCompressed = deny
if hasattr(TSVZ, '_openUntruncated'):
    _ru = TSVZ._openUntruncated
    TSVZ._openUntruncated = lambda fn: deny(fn, 'wb')
r = t.clear()
TSVZ.openFileAsCompressed = real
if hasattr(TSVZ, '_openUntruncated'): TSVZ._openUntruncated = _ru
time.sleep(0.5)
print('clear returned normally; memory:', dict(t), '| deSynced', t.deSynced, 'dirty', t.dirty, '| file:', open(p,'rb').read())
t.close()
print('reopened:', dict(TSVZ.TSVZed(p, header='key\tv')))
os._exit(0)
