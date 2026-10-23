# Legacy writer failures left for a later design pass

The review of the strict-UTF-8-writes branch (d51fc13..c071d24) found failure-mode bugs in
the legacy (`.tsv`/`.csv`/`.nsv`, compressed or not) TSVZed writer. That branch fixed the ones
it introduced or made worse than main 1688d25. The ones below behave the same on main. They
are left for one design pass over how the legacy writer handles failures (interrupts, full
disks, lost locks, other processes), instead of more piecemeal fixes.

Each one has a script in `docs/legacy-writer-repros/`. Run it from an empty scratch directory
(it writes its files there), giving it the directory that holds the `TSVZ.py` to test:

    mkdir -p /tmp/repro && cd /tmp/repro
    python3 -u /path/to/TSVZ/docs/legacy-writer-repros/ki_commit.py /path/to/TSVZ commit

"main" is 1688d25 and "branch" is strict-utf8-writes-2 with its fixes. Both were run on
Linux with Python 3.13. The review's finding numbers for items 1 to 15 are 1, 7, 6, 4, 10, 14,
none (found while testing), 5, 8, what is left of 13, 11, a second opinion's, a second
opinion's, 15 and 12.

## Interrupts and failed writes

1. **A KeyboardInterrupt during an append leaves writeLock held for good and loses the rows.**
   `commitAppendToFile` has no `try/finally` for `BaseException`, and `release_file_obj` catches
   only `Exception`. A Ctrl-C between `get_file_obj` and the final release leaves `writeLock`
   held, and the rows already popped from the queue are lost. The next `close()` hangs.
   Script: `ki_commit.py DIR commit` (interrupt before the write) and `ki_commit.py DIR release`
   (interrupt in the fsync). Main: `writeLock held after KI: True | queue: []`,
   `could take writeLock: False`. Branch: the rows are no longer lost (they leave the queue only
   once on disk, see item 2: `queue: [['a', '1']]`), but writeLock is still held for good.

2. **A failed append loses the rows it popped.** `commitAppendToFile` pops rows before it writes
   and never puts them back when the write fails. Only a failed open keeps them. The .tsvz path
   (`_specCommitLocked`) puts them back with `extendleft(reversed(items))`.
   Script: `enospc.py DIR` (one ENOSPC). Main: `queue: []`, memory still has `new`,
   and the file after close is only the header.
   **Fixed on the branch** (the review of ee041a6 found that an append cut short on a file a
   failed rewrite damaged was then merged back over memory): the rows leave the queue only once
   written and synced, and a write that fails is cut off again. Branch: the background writer's
   next try writes the row (`queue: []`), and the file after close is `b'key\tv\nnew\t1\n'`.

3. **release_file_obj can release a writeLock that another thread acquired.** Its except path
   releases `writeLock`, then `if self.writeLock.locked(): release()` releases it again if
   another thread took it in between, and notes the file time for that thread.
   Script: `steal.py DIR`. Main and branch: thread B's `with t.writeLock` fails with
   `RuntimeError: release unlocked lock`, and `{'locked_while_B_holds_it': False}`.

4. **A compressed append is unlocked before it is all written.** For a compressed file,
   `release_file_obj` calls the codec writer's `flush()`, unlocks, and only then `close()`s it.
   `GzipFile.flush()` writes all but the trailer, but `BZ2File.flush()` and `LZMAFile.flush()`
   write nothing: a .bz2 or .xz append is written whole after the unlock (500 rows: 0 of 1047
   bytes on disk at the unlock for .bz2, 0 of 712 for .xz, 1698 of 1708 for .gz). Another
   writer that appends in that window corrupts the stream.
   Script: `gzrace.py DIR`. Main: `gzip read fails ... invalid block type`, and
   `readTabularFile: {}`.
   **Fixed on the branch** for appends too: every legacy append writes its rows as whole members
   (or frames) with os.write, under the file lock, before it unlocks (one helper, `_appendOpen`).
   Branch: `decompressed: b'key\tv\na\t1\nother\tX\n'`.
   Rewrites had this too, and wider: every .bz2 or .xz rewrite (hardMapToFile, clear) was written
   after the unlock (2000 rows: 0 of 4700 bytes on disk at the unlock for .bz2, 0 of 1808 for
   .xz, 8052 of 8062 for .gz; the same on main). The branch now closes a rewrite's codec writer,
   and flushes and syncs the file, before it unlocks it, so that a failure there is seen as the
   rewrite's own; only appends are left.

5. **A failed clear() empties memory and never tries again.** `_clearFile(dropRows=True)` empties
   memory and the queue before it knows it can open and lock the file. When it cannot, only
   `deSynced` is set and nothing tries again. Suggested fix: open and lock first, then drop
   memory and the queue, then write the header.
   Script: `clearfail.py DIR`. Main and branch: `memory: {}`, but the file still holds both rows
   and a reopen reads them.

6. **A missing file kills the background writer.** `rewrite()` calls `os.path.getmtime` outside
   its `try`. If the file is gone while the store is dirty and `rewrite_interval > 0`, the worker
   thread dies.
   Script: `workerdies.py DIR`. Main and branch: `FileNotFoundError` in `_appendWorker`,
   `worker alive: False`, and the row stays queued.

7. **A rewrite writes rows right after a header that has no newline.** For a file that ends right
   after its header, with no newline (a rewrite cut short, an editor), `mapToFile` writes the
   rows on the header's line.
   Script: `headernl.py DIR`. Main and branch: `b'key\tva\t1\n'`.

## Other processes

8. **A rewrite appends the queued rows again.** The rewrite snapshot already contains the queued
   rows, but the queue is not trimmed, so they are appended again after the rewrite. That can
   undo a later delete or update by another process.
   Script: `resurrect.py DIR`. Main: the other process deletes `k`, the stale queued
   row is appended after the tombstone, and `k live on read: True`.
   **Fixed on the branch** (the review of b28d917 found that the merge after a failed rewrite
   skipped such a key as still queued, and dropped another process's change of it): a rewrite
   that reached the disk drops from the queue the rows its snapshot held. Branch: `queue: []`
   after the rewrite, and `k live on read: False`.

9. **rewrite_on_load can overwrite a change made just after the load.** `load()` calls
   `rewrite(force=True, reloadInternalFromFile=False)`, so the rewrite_on_load rewrite overwrites
   a change another process makes after the load's read.
   Script: `loadrace.py DIR`. Main and branch: "Overwriting external changes", and the other
   process's row `other` is gone from the file.

10. **A reload drops what only memory holds.** Reloading another process's change replaces
    memory with the file plus the queued rows. An order set by `move_to_end`, or a value changed
    in place, is lost, so the rewrite_on_exit that `move_to_end` turns on no longer writes it. On
    the branch, a rewrite that another process's change stops reloads that change and is made
    again; the order is lost the same way.
    Script: `exitdefer.py DIR` (the change comes after rewrite()'s check). Main: the rewrite
    overwrites the change, keeps the order `b, a` and erases `other`. Branch: keeps `other` and
    loses the order. Script: `exitbefore.py DIR` (the change comes before the check). Main and
    branch: keep `other` and lose the order.

## Memory and threads

11. **Other threads see live keys missing during a reload.** The reload swap runs
    `OrderedDict.clear(self)` and then re-inserts every row. Readers do not take `_memLock`, so
    other threads see live keys missing during every reload. Suggested fix: delete the keys
    that are gone and set the ones that changed, so a kept key is never absent.
    Script: `swapread.py DIR`. Main: 135 M of 136 M lookups of a live key missed (main clears
    memory before the read). Branch: 1.7 M of 219 M missed (only during the swap).

12. **correctColumnNum is written outside _memLock.** `__setitem__` writes it outside
    `_memLock`, and `_reload` reads and writes it inside. That leaves a narrow window on an
    empty store.
    Script: `colnum.py DIR` (code inspection only). Main writes it in both places without any
    lock, so the race is at least as wide there.

13. **pop and popitem queue a tombstone for an in-memory-only '#' key.** `pop()` and `popitem()`
    queue a tombstone for a `#` key, which is memory only, so the tombstone is written to the
    file. `del` does not do this.
    Script: `hashpop.py DIR`. Main and branch: the file gets `#note\t` and `#n2\t` lines.

14. **A .tsvz `_specSync` drops rows set between the commit and the reload.** It drops them from
    memory only; the queue keeps them, so the file gets them. This was known.
    Script: `specsync.py DIR` sets a row between the commit and the reload. It does not show the
    drop on main or branch: the window is inside the reload's read. `_specSync` is the same on
    both, and `_specReload` differs only by the `errors=` argument.

## Handlers

15. **A handler for a tab- or NUL-delimited loose store writes a pointer file that clients
    reject.** The handler writes `x-delimiter` with the delimiter escaped. `_serveReadPointer`
    decodes it back to a TAB or NUL, and `_serveLookup` rejects any value that holds a control
    character. Clients never find a handler for such a store, which includes every plain `.tsv`.
    Script: `ptr_delim.py DIR`. Main and branch: a `.csv` handler is found, but for `.tsv` and NUL
    the pointer "has control characters in its x-delimiter".
