import sys, os, time, threading
sys.path.insert(0, sys.argv[1])
import TSVZ
which = sys.argv[2]
p = 'ki_commit.tsv'
if os.path.exists(p): os.remove(p)
t = TSVZ.TSVZed(p, header='key\tv', append_check_delay=100, rewrite_on_load=False)  # worker sleeps: main thread commits
time.sleep(0.2)
t['a'] = ['a', '1']
if which == 'commit':
    real = TSVZ._endsWithoutNewline
    def boom(f):
        raise KeyboardInterrupt
    TSVZ._endsWithoutNewline = boom
    try:
        t.commitAppendToFile()
    except KeyboardInterrupt:
        print('KI out of commitAppendToFile')
    TSVZ._endsWithoutNewline = real
else:
    realfsync = os.fsync
    def fs(fd):
        realfsync(fd); raise KeyboardInterrupt
    os.fsync = fs
    try:
        t.commitAppendToFile()
    except KeyboardInterrupt:
        print('KI out of release_file_obj')
    os.fsync = realfsync
print('writeLock held after KI:', t.writeLock.locked(), '| queue:', list(t.appendQueue))
print('file:', open(p,'rb').read())
ok = t.writeLock.acquire(timeout=1)
print('could take writeLock:', ok)
os._exit(0)
