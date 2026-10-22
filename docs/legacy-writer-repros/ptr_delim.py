import sys, os, threading, tempfile
sys.path.insert(0, sys.argv[1])
import TSVZ
d = tempfile.mkdtemp(dir=os.getcwd())
for name, delim in (('comma.csv', None), ('tab.tsv', None), ('nul.tsv', '\0')):
    p = os.path.join(d, name)
    h = TSVZ._ServeHandle(p)
    h.lock()
    st = TSVZ._ServeStore(p, create=True) if delim is None else TSVZ._ServeStore(p, create=True, delimiter=delim)
    h.start(st)
    th = threading.Thread(target=h.serve); th.start()
    print(name, 'delimiter', repr(st.delimiter), '-> _serveLookup:', TSVZ._serveLookup(p))
    h.stop(); th.join(10); h.close()
