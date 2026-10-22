import sys, os, time
sys.path.insert(0, sys.argv[1])
import TSVZ
p = 'specsync.tsvz'
for f in os.listdir('.'):
    if f.startswith('specsync'): os.remove(f)
t = TSVZ.TSVZed(p, header='key\tv', append_check_delay=1000, rewrite_on_load=False)
time.sleep(0.1)
t['a'] = ['a', '1']; t.commitAppendToFile()
import subprocess
subprocess.check_call([sys.executable, '-c', "import sys; sys.path.insert(0, %r); import TSVZ, os; o = TSVZ.TSVZed(%r, header='key\\tv', rewrite_on_load=False); o['ext'] = ['ext', 'X']; o.close(); os._exit(0)" % (sys.argv[1], p)])
time.sleep(0.05)
t.checkExternalChanges()
real = t.commitAppendToFile
def commit():
    r = real()
    t.__dict__.pop('commitAppendToFile', None)
    t['late'] = ['late', '2']   # another thread sets a row between the commit and the reload
    return r
t.commitAppendToFile = commit
t._specSync()
print('late in memory after sync:', 'late' in t, '| queue:', list(t.appendQueue))
t.commitAppendToFile()
print('late in file:', 'late' in TSVZ.readTabularFile(p, header='key\tv'))
os._exit(0)
