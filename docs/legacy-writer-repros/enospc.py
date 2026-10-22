import sys, os, time, errno
sys.path.insert(0, sys.argv[1])
import TSVZ
p = 'enospc.tsv'
if os.path.exists(p): os.remove(p)
t = TSVZ.TSVZed(p, header='key\tv', append_check_delay=0.01, rewrite_on_load=False)
time.sleep(0.1)
real = TSVZ._endsWithoutNewline
n = [0]
def full(f):
    if n[0] == 0:
        n[0] += 1
        raise OSError(errno.ENOSPC, 'No space left on device')  # the disk was full for a moment
    return real(f)
TSVZ._endsWithoutNewline = full
t['new'] = ['new', '1']
time.sleep(0.5)
TSVZ._endsWithoutNewline = real
print('queue:', list(t.appendQueue), 'dirty:', t.dirty, 'deSynced:', t.deSynced, 'memory:', dict(t))
t.close()
print('file after close:', open(p,'rb').read())
