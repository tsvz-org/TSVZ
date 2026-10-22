import sys, os, time, fcntl
sys.path.insert(0, sys.argv[1])
import TSVZ
p = 'exitdefer.tsv'
if os.path.exists(p): os.remove(p)
with open(p, 'wb') as f: f.write(b'key\tv\na\t1\nb\t2\n')
t = TSVZ.TSVZed(p, header='key\tv', rewrite_on_load=False, append_check_delay=0.2)
time.sleep(0.1)
t.move_to_end('a')           # an in-place change: only a rewrite (rewrite_on_exit) stores the new order
name = '_mapToFile' if hasattr(TSVZ.TSVZed, '_mapToFile') else 'mapToFile'
real = getattr(TSVZ.TSVZed, name)
done = [False]
def mapper(self, *a, **k):
    if not done[0]:
        done[0] = True
        time.sleep(0.01)
        with open(p, 'ab') as g:  # another process appends after the exit rewrite looked for changes
            fcntl.lockf(g, fcntl.LOCK_EX); g.write(b'other\tX\n'); fcntl.lockf(g, fcntl.LOCK_UN)
    return real(self, *a, **k)
setattr(TSVZ.TSVZed, name, mapper)
t.close()
print('file after close:', open(p, 'rb').read())
