import sys, os, time, fcntl
sys.path.insert(0, sys.argv[1])
import TSVZ
p = 'loadrace.tsv'
if os.path.exists(p): os.remove(p)
with open(p, 'wb') as f: f.write(b'key\tv\na\t1\na\t2\n')  # an updated key: the load is dirty, rewrite_on_load rewrites
real = TSVZ.TSVZed.rewrite
def rewrite(self, *a, **k):
    if k.get('reloadInternalFromFile') is False and not getattr(self, '_injected', False):
        self._injected = True
        with open(p, 'ab') as g:  # another process appends right after this one read the file
            fcntl.lockf(g, fcntl.LOCK_EX); g.write(b'other\tX\n'); fcntl.lockf(g, fcntl.LOCK_UN)
    return real(self, *a, **k)
TSVZ.TSVZed.rewrite = rewrite
t = TSVZ.TSVZed(p, header='key\tv')
time.sleep(0.3)
print('memory:', dict(t))
print('file:', open(p,'rb').read())
t.close()
