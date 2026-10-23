import sys, os, gc
sys.path.insert(0, sys.argv[1])
import TSVZ
def rss():
    for l in open('/proc/self/status'):
        if l.startswith('VmRSS'): return int(l.split()[1])
def one(i):
    p = 'keep%d.tsv' % i
    if os.path.exists(p): os.remove(p)
    t = TSVZ.TSVZed(p, header='key\tv', rewrite_on_load=False, append_check_delay=0.01, verbose=False)
    for k in range(1000): t['k%d' % k] = ['k%d' % k, 'v' * 20]
    t.hardMapToFile()
    t.stopAppendThread()
gc.collect(); before = rss()
for i in range(50): one(i)
gc.collect()
alive = sum(1 for o in gc.get_objects() if isinstance(o, TSVZ.TSVZed))
print('stopped and unreferenced stores still alive:', alive, 'of 50; RSS grew by', rss() - before, 'KB')
os._exit(0)
