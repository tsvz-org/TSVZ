import sys, os, time
sys.path.insert(0, sys.argv[1])
import TSVZ
p = 'hashpop.tsv'
if os.path.exists(p): os.remove(p)
t = TSVZ.TSVZed(p, header='key\tv', rewrite_on_load=False, append_check_delay=1000)
time.sleep(0.1)
t['#note'] = ['#note', 'memory only']
t['#n2'] = ['#n2', 'memory only']
t.pop('#note')
t.popitem()  # '#n2'
print('queue after popping two in-memory-only # keys:', list(t.appendQueue))
t.commitAppendToFile()
print('file:', open(p, 'rb').read())
os._exit(0)
