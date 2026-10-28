# Limitations

Approved known limitations and deferrals. Reviewers: read this first, and don't raise these again.

### A read in another thread can drop a lockf write lock
- Kind: deferred
- What the operator sees: where TSVZ cannot take an open file description lock (macOS and other systems that
  are not Linux, or a Linux file system that refuses those locks), it locks a file with `lockf`, and closing
  any descriptor of that file in the process drops the lock. A read of the store in another thread of the same
  process (a handler's, another `TSVZed`'s, a `readTabularFile`) can then drop a write's lock while the write
  holds it, and another process's write can land in the middle of it. On Linux with open file description
  locks (the usual case) this does not happen.
- Why: 4.9 tried a close gate (a close waits while another thread holds the lock) and took it out again
  (cbe1829): five review rounds kept finding hangs in it. A fix needs its own design, for example `flock` on
  macOS, checked on a Mac.
- Revisit when: someone runs TSVZ writers on macOS with threads or a handler, or a Mac is at hand to check a
  `flock` design.
- Approved: 2026-10-24, the user chose "Record as deferred (Recommended)".

### A second Ctrl-C at exit abandons a write in progress
- Kind: known limitation
- What the operator sees: a program that exits while a `TSVZed` rewrites a `.tsv`-family file waits for the write;
  a Ctrl-C then prints "TSVZ: the write to X is being finished; Ctrl-C again to abandon it". A second Ctrl-C ends
  the program at once and cuts the rewrite where it is: a plain file is left with the rows not yet rewritten
  padded or duplicated, a compressed file can be left unreadable, and memory, the good copy, goes with the
  process. A `kill -9` or a power cut during any in-place rewrite does the same (spec §19.8: an in-place rewrite
  is not crash-atomic).
- Why: an operator who presses Ctrl-C twice wants out now, and an exit that refuses it blocks a clear intent.
  Writing a temporary file and renaming it over the store would survive it, but was rejected for the reasons in
  f329717 (mode, owner, links and other processes' writes lost).
- Revisit when: someone loses a store this way.
- Approved: 2026-10-24, the user chose "One writer thread (Recommended)", whose description said "At exit the
  program finishes a write in progress, and a second Ctrl-C abandons it."
