import sys, os, time, threading, errno
sys.path.insert(0, sys.argv[1])
import TSVZ
p = 'steal.tsv'
if os.path.exists(p): os.remove(p)
class SlowLog:
    def teelog(self, msg, level='info'):
        if level == 'error':
            time.sleep(0.2)  # a logger that takes a moment (disk, network)
t = TSVZ.TSVZed(p, header='key\tv', append_check_delay=100, rewrite_on_load=False, teeLogger=SlowLog())
time.sleep(0.2)
t['a'] = ['a', '1']
realfsync = os.fsync
def fs(fd):
    if threading.current_thread().name == 'A':
        raise OSError(errno.EIO, 'Input/output error')
    return realfsync(fd)
os.fsync = fs
seen = {}
def b():
    time.sleep(0.05)
    with t.writeLock:  # e.g. a reload, a rewrite or checkExternalChanges in another thread
        time.sleep(0.6)
        seen['locked_while_B_holds_it'] = t.writeLock.locked()
A = threading.Thread(target=t.commitAppendToFile, name='A')
B = threading.Thread(target=b, name='B')
B.start(); A.start(); A.join(); B.join()
print(seen)
os._exit(0)
