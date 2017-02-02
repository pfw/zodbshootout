##############################################################################
#
# Copyright (c) 2009 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
"""
The core speed test loop.
"""
from __future__ import print_function, absolute_import

from collections import namedtuple
from functools import partial
from itertools import chain
from pstats import Stats
from threading import Event

import cProfile

import os
import random
import statistics
import sys
import time
import transaction

from persistent.mapping import PersistentMapping

from .fork import distribute
from .fork import run_in_child
from ._pobject import pobject_base_size
from ._pobject import PObject

def itervalues(d):
    try:
        iv = d.itervalues
    except AttributeError:
        iv = d.values
    return iv()



with open(__file__, 'rb') as _f:
    # Just use the this module as the source of our data
    _random_file_data = _f.read().replace(b'\n', b'').split()
del _f

random.seed(__file__) # reproducible random functions

def _random_data(size):
    """
    Create a random data of at least the given size.

    Use pseudo-random data in case compression is in play so we get a more realistic
    size and time value than a single 'x'*size would get.
    """

    def fdata():
        words = _random_file_data
        chunksize = min(size, 1024)
        while True:
            sample = random.sample(words, len(words) // 10)
            yield b' '.join(sample[0:chunksize])
    datagen = fdata()

    data = b''
    while len(data) < size:
        data += next(datagen)
    return data

_random_data(100) # call once for the sake of leak checks

WriteTimes = namedtuple('WriteTimes', ['add_time', 'update_time'])
ReadTimes = namedtuple('ReadTimes', ['warm_time', 'cold_time', 'hot_time', 'steamin_time'])
SpeedTestTimes = namedtuple('SpeedTestTimes', WriteTimes._fields + ReadTimes._fields)

class SpeedTest(object):

    MappingType = PersistentMapping
    debug = False

    individual_test_reps = 20

    def __init__(self, concurrency, objects_per_txn, object_size,
                 profile_dir=None,
                 mp_strategy='mp',
                 test_reps=None):
        self.concurrency = concurrency
        self.objects_per_txn = objects_per_txn
        self.object_size = object_size
        self.profile_dir = profile_dir
        self.contender_name = None
        if mp_strategy in ('shared', 'unique'):
            self.mp_strategy = 'threads'
        else:
            self.mp_strategy = mp_strategy

        if test_reps:
            self.individual_test_reps = test_reps
        self.rep = 0  # repetition number

        if mp_strategy == 'shared':
            self._wait_for_master_to_do = self._threaded_wait_for_master_to_do

    def _wait_for_master_to_do(self, _thread_number, _sync, func, *args):
        # We are the only thing running, this object is not shared,
        # func must always be called.
        func(*args)

    def _threaded_wait_for_master_to_do(self, thread_number, sync, func, *args):
        """
        Block all threads until *func* is called by the first thread (0).
        """
        sync()
        if thread_number == 0:
            func(*args)
        sync()

    @property
    def data_to_store(self):
        # Must be fresh when accessed because could already
        # be stored in another database if we're using threads
        data_size = max(0, self.object_size - pobject_base_size)
        return dict((n, PObject(_random_data(data_size))) for n in range(self.objects_per_txn))

    def populate(self, db_factory):
        db = db_factory()
        conn = db.open()
        root = conn.root()

        # clear the database
        root['speedtest'] = None
        transaction.commit()
        db.pack()

        # put a tree in the database
        root['speedtest'] = t = self.MappingType()
        for i in range(self.concurrency):
            t[i] = self.MappingType()
        transaction.commit()
        conn.close()
        db.close()
        if self.debug:
            print('Populated storage.', file=sys.stderr)


    def _clear_all_caches(self, db):
        # Clear all caches
        # No connection should be open when this is called.
        db.pool.map(lambda c: c.cacheMinimize())
        conn = db.open()
        # Account for changes between ZODB 4 and 5,
        # where there may or may not be a MVCC adapter layer,
        # depending on storage type, so we check both.
        storage = conn._storage
        if hasattr(storage, '_cache'):
            storage._cache.clear()
        conn.close()

        if hasattr(db, 'storage') and hasattr(db.storage, '_cache'):
            db.storage._cache.clear()

    def _times_of_runs(self, func, times, args=()):
        run_times = [func(*args) for _ in range(times)]
        return run_times

    def _close_conn(self, conn):
        if self.debug:
            loads, stores = conn.getTransferCounts(True)
            db_name = conn.db().database_name
            print("DB", db_name, "conn", conn, "loads", loads, "stores", stores, file=sys.stderr)
        conn.close()

    # We should always include conn.open() inside our times,
    # because it talks to the storage to poll invalidations.
    # conn.close() does not talk to the storage, but it does
    # do some cache maintenance and should be excluded if possible.

    def write_test(self, db_factory, n, sync):
        db = db_factory()

        def do_add():
            start = time.time()

            conn = db.open()
            root = conn.root()
            m = root['speedtest'][n]
            m.update(self.data_to_store)
            transaction.commit()

            end = time.time()

            self._close_conn(conn)
            return end - start

        db.open().close()
        sync()
        add_time = self._execute(self._times_of_runs, 'add', n,
                                 do_add, self.individual_test_reps)

        def do_update():
            start = time.time()

            conn = db.open()
            root = conn.root()
            for obj in itervalues(root['speedtest'][n]):
                obj.attr = 1
            transaction.commit()

            end = time.time()

            self._close_conn(conn)
            return end - start

        sync()
        update_time = self._execute(self._times_of_runs, 'update', n,
                                    do_update, self.individual_test_reps)

        time.sleep(.1)
        # In shared thread mode, db.close() doesn't actually do anything.
        db.close()
        return [WriteTimes(a, u) for a, u in zip(add_time, update_time)]

    def read_test(self, db_factory, thread_number, sync):
        db = db_factory()
        # Explicitly set the number of cached objects so we're
        # using the storage in an understandable way.
        # Set to double the number of objects we should have created
        # to account for btree nodes.
        db.setCacheSize(self.objects_per_txn * 2)

        def do_read(clear_all=False, clear_conn=False):
            if clear_all:
                self._wait_for_master_to_do(thread_number, sync, self._clear_all_caches, db)

            start = time.time()
            conn = db.open()

            if clear_conn and not clear_all:
                # clear_all did this already.
                # sadly we have to include this time in our
                # timings because opening the connection must
                # be included. hopefully this is too fast to have an impact.
                conn.cacheMinimize()

            got = 0

            for obj in itervalues(conn.root()['speedtest'][thread_number]):
                got += obj.attr
            obj = None
            if got != self.objects_per_txn:
                raise AssertionError('data mismatch')

            end = time.time()
            self._close_conn(conn)
            return end - start

        db.open().close()
        sync()
        # In shared thread mode, the 'warm' test, immediately following the update test,
        # is similar to the steamin test, because we're likely to get the same
        # Connection object again (because the DB wasn't really closed.)
        # Of course, this really only applies when self.concurrency is 1; for other
        # values, we can't be sure.
        warm = self._execute(do_read, 'warm', thread_number)

        sync()
        cold = self._execute(self._times_of_runs, 'cold', thread_number,
                             do_read, self.individual_test_reps, (True, True))

        sync()
        hot = self._execute(self._times_of_runs, 'hot', thread_number,
                            do_read, self.individual_test_reps, (False, True))

        sync()
        steamin = self._execute(self._times_of_runs, 'steamin', thread_number,
                                do_read, self.individual_test_reps)

        db.close()
        return [ReadTimes(w, c, h, s) for w, c, h, s
                in zip([warm] * self.individual_test_reps,
                       cold, hot, steamin)]

    def _execute(self, func, phase_name, n, *args):
        if not self.profile_dir:
            return func(*args)

        basename = '%s-%s-%d-%02d-%d' % (
            self.contender_name, phase_name, self.objects_per_txn, n, self.rep)
        txt_fn = os.path.join(self.profile_dir, basename + ".txt")
        prof_fn = os.path.join(self.profile_dir, basename + ".prof")

        profiler = cProfile.Profile()
        profiler.enable()
        try:
            res = func(*args)
        finally:
            profiler.disable()

        profiler.dump_stats(prof_fn)

        with open(txt_fn, 'w') as f:
            st = Stats(profiler, stream=f)
            st.strip_dirs()
            st.sort_stats('cumulative')
            st.print_stats()

        return res

    def run(self, db_factory, contender_name, rep):
        """Run a write and read test.

        Returns a list of SpeedTestTimes items containing the results
        for every test, as well as a WriteTime and ReadTime summary
        """
        self.contender_name = contender_name
        self.rep = rep

        run_in_child(self.populate, self.mp_strategy, db_factory)


        thread_numbers = list(range(self.concurrency))
        write_times = distribute(partial(self.write_test, db_factory),
                                 thread_numbers, strategy=self.mp_strategy)
        read_times = distribute(partial(self.read_test, db_factory),
                                thread_numbers, strategy=self.mp_strategy)

        write_times = list(chain(*write_times))
        read_times = list(chain(*read_times))

        # Return the raw data here so as to not throw away any (more) data
        times = [SpeedTestTimes(*(w + r)) for w, r in zip(write_times, read_times)]

        # These are just for summary purpose
        add_times = [t.add_time for t in write_times]
        update_times = [t.update_time for t in write_times]
        warm_times = [t.warm_time for t in read_times]
        cold_times = [t.cold_time for t in read_times]
        hot_times = [t.hot_time for t in read_times]
        steamin_times = [t.steamin_time for t in read_times]

        write_times = WriteTimes(*[statistics.mean(x) for x in (add_times, update_times)])
        read_times = ReadTimes(*[statistics.mean(x) for x in (warm_times, cold_times, hot_times, steamin_times)])

        return times, write_times, read_times
