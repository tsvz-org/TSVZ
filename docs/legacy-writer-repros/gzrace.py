import sys, os, time, gzip, fcntl
sys.path.insert(0, sys.argv[1])
import TSVZ
p = 'gzr.tsv.gz'
if os.path.exists(p): os.remove(p)
t = TSVZ.TSVZed(p, header='key\tv', rewrite_on_load=False, append_check_delay=1000)
time.sleep(0.1)
t['a'] = ['a', '1']
real = TSVZ._unlockFile
done = [False]
def unlock(f):
    real(f)
    if not done[0]:
        done[0] = True
        with open(p, 'ab') as g:  # another writer takes the lock the moment it is free and appends a member
            fcntl.lockf(g, fcntl.LOCK_EX)
            g.write(gzip.compress(b'other\tX\n'))
            fcntl.lockf(g, fcntl.LOCK_UN)
TSVZ._unlockFile = unlock
t.commitAppendToFile()
TSVZ._unlockFile = real
try:
    print('decompressed:', gzip.decompress(open(p,'rb').read()))
except Exception as e:
    print('gzip read fails:', type(e).__name__, e)
print('readTabularFile:', dict(TSVZ.readTabularFile(p, header='key\tv')))
os._exit(0)
