import sys, os, time, fcntl
sys.path.insert(0, sys.argv[1])
import TSVZ
p = 'res.tsv'
if os.path.exists(p): os.remove(p)
t = TSVZ.TSVZed(p, header='key\tv', rewrite_on_load=False, append_check_delay=1000)
time.sleep(0.1)
t['k'] = ['k', 'v1']          # queued, not yet appended
t.mapToFile()                 # a rewrite (here explicit; the worker's own when dirty) writes memory, k included
print('after rewrite:', open(p,'rb').read(), 'queue:', list(t.appendQueue))
other = TSVZ.TSVZed(p, header='key\tv', rewrite_on_load=False, append_check_delay=1000)  # another process
del other['k']; other.commitAppendToFile()
print('other deleted k:', open(p,'rb').read())
t.commitAppendToFile()        # the stale queued row is appended after the other's tombstone
print('after append  :', open(p,'rb').read())
print('k live on read:', 'k' in TSVZ.readTabularFile(p, header='key\tv'))
os._exit(0)
