# A file that ends right after its header, with no newline (a rewrite cut short, an editor): mapToFile writes
# the rows right after the header, on the same line.
import sys, os, time
sys.path.insert(0, sys.argv[1])
import TSVZ
p = 'headernl.tsv'
if os.path.exists(p): os.remove(p)
with open(p, 'wb') as f: f.write(b'key\tv')
t = TSVZ.TSVZed(p, header='key\tv', rewrite_on_load=False, append_check_delay=1000)
time.sleep(0.1)
t['a'] = ['a', '1']
t.appendQueue.clear()
t.mapToFile()
print('file after mapToFile:', open(p, 'rb').read())
os._exit(0)
