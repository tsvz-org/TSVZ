import sys, os, time, fcntl
sys.path.insert(0, sys.argv[1])
import TSVZ
p = 'exitbefore.tsv'
if os.path.exists(p): os.remove(p)
with open(p, 'wb') as f: f.write(b'key\tv\na\t1\nb\t2\n')
t = TSVZ.TSVZed(p, header='key\tv', rewrite_on_load=False, append_check_delay=0.2)
time.sleep(0.1)
t.move_to_end('a')
with open(p, 'ab') as g:
    fcntl.lockf(g, fcntl.LOCK_EX); g.write(b'other\tX\n'); fcntl.lockf(g, fcntl.LOCK_UN)
t.close()
print('file after close:', open(p, 'rb').read())
