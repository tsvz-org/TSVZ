import sys, os, time
sys.path.insert(0, sys.argv[1])
import TSVZ
p = 'wd.tsv'
if os.path.exists(p): os.remove(p)
t = TSVZ.TSVZed(p, header='key\tv', rewrite_on_load=False, rewrite_interval=5)
t['a'] = ['a', '1']; time.sleep(0.1); t['a'] = ['a', '2']  # dirty
time.sleep(0.1)
os.remove(p)  # log rotation / cleanup removes the file
time.sleep(0.3)
print('worker alive:', t.appendThread.is_alive())
t['b'] = ['b', '1']
time.sleep(0.3)
print('queue:', list(t.appendQueue), 'file exists:', os.path.exists(p))
os._exit(0)
