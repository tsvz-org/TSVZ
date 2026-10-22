import sys, os, time, threading
sys.path.insert(0, sys.argv[1])
import TSVZ
p = 'swapread.tsv'
if os.path.exists(p): os.remove(p)
with open(p, 'wb') as f:
    f.write(b'key\tv\n' + b''.join(b'k%d\t%d\n' % (i, i) for i in range(200000)))
t = TSVZ.TSVZed(p, header='key\tv', rewrite_on_load=False)
stop = threading.Event(); misses = [0, 0]
def reader():
    while not stop.is_set():
        misses[1] += 1
        if 'k199999' not in t:  # a key that is live the whole time
            misses[0] += 1
th = threading.Thread(target=reader); th.start()
for i in range(5):
    t.reload()
stop.set(); th.join()
print('lookups of a live key that found it missing: %d of %d' % tuple(misses))
os._exit(0)
