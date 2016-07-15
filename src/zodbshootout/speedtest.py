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

import os
import sys
import time
import transaction
import random
import cProfile
from pstats import Stats

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


class SpeedTest(object):

    MappingType = PersistentMapping
    debug = False

    def __init__(self, concurrency, objects_per_txn, object_size,
                 profile_dir=None,
                 mp_strategy='mp'):
        self.concurrency = concurrency
        self.objects_per_txn = objects_per_txn
        self.object_size = object_size
        self.profile_dir = profile_dir
        self.contender_name = None
        self.mp_strategy = mp_strategy
        self.rep = 0  # repetition number

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

    def write_test(self, db_factory, n, sync):
        db = db_factory()

        def do_add():
            start = time.time()
            conn = db.open()
            root = conn.root()
            m = root['speedtest'][n]
            m.update(self.data_to_store)
            transaction.commit()
            conn.close()
            end = time.time()
            return end - start

        db.open().close()
        sync()
        add_time = self._execute(do_add, 'add', n)

        def do_update():
            start = time.time()
            conn = db.open()
            root = conn.root()
            for obj in itervalues(root['speedtest'][n]):
                obj.attr = 1
            transaction.commit()
            conn.close()
            end = time.time()
            return end - start

        sync()
        update_time = self._execute(do_update, 'update', n)

        time.sleep(.1)
        db.close()
        return add_time, update_time

    def _clear_all_caches(self, db):
        # Clear all caches
        conn = db.open()
        conn.cacheMinimize()
        # Account for changes between ZODB 4 and 5,
        # where there may or may not be a MVCC adapter layer,
        # depending on storage type, so we check both.
        storage = conn._storage
        if hasattr(storage, '_cache'):
            storage._cache.clear()
        conn.close()

        if hasattr(db, 'storage') and hasattr(db.storage, '_cache'):
            db.storage._cache.clear()


    def read_test(self, db_factory, n, sync):
        db = db_factory()
        db.setCacheSize(len(self.data_to_store)+400)

        def do_read():
            start = time.time()
            conn = db.open()
            got = 0
            for obj in itervalues(conn.root()['speedtest'][n]):
                got += obj.attr
            del obj
            if got != self.objects_per_txn:
                raise AssertionError('data mismatch')
            conn.close()
            end = time.time()
            return end - start

        db.open().close()
        sync()
        warm = self._execute(do_read, 'warm', n)

        self._clear_all_caches(db)

        sync()
        cold = self._execute(do_read, 'cold', n)

        conn = db.open()
        conn.cacheMinimize()
        conn.close()

        sync()
        hot = self._execute(do_read, 'hot', n)
        sync()
        steamin = self._execute(do_read, 'steamin', n)

        db.close()
        return warm, cold, hot, steamin

    def _execute(self, func, phase_name, n):
        if not self.profile_dir:
            return func()

        basename = '%s-%s-%d-%02d-%d' % (
            self.contender_name, phase_name, self.objects_per_txn, n, self.rep)
        txt_fn = os.path.join(self.profile_dir, basename + ".txt")
        prof_fn = os.path.join(self.profile_dir, basename + ".prof")

        output = []
        d = {'_func': func, '_output': output}
        cProfile.runctx("_output.append(_func())", d, d, prof_fn)
        res = output[0]

        with open(txt_fn, 'w') as f:
            st = Stats(prof_fn, stream=f)
            st.strip_dirs()
            st.sort_stats('cumulative')
            st.print_stats()

        return res

    def run(self, db_factory, contender_name, rep):
        """Run a write and read test.

        Returns the mean time per transaction for 4 phases:
        write, cold read, hot read, and steamin' read.
        """
        self.contender_name = contender_name
        self.rep = rep

        run_in_child(self.populate, self.mp_strategy, db_factory)

        def write(n, sync):
            return self.write_test(db_factory, n, sync)
        def read(n, sync):
            return self.read_test(db_factory, n, sync)

        r = list(range(self.concurrency))
        write_times = distribute(write, r, strategy=self.mp_strategy)
        read_times = distribute(read, r, strategy=self.mp_strategy)

        add_times = [t[0] for t in write_times]
        update_times = [t[1] for t in write_times]
        warm_times = [t[0] for t in read_times]
        cold_times = [t[1] for t in read_times]
        hot_times = [t[2] for t in read_times]
        steamin_times = [t[3] for t in read_times]

        return (
            sum(add_times) / self.concurrency,
            sum(update_times) / self.concurrency,
            sum(warm_times) / self.concurrency,
            sum(cold_times) / self.concurrency,
            sum(hot_times) / self.concurrency,
            sum(steamin_times) / self.concurrency,
            )
