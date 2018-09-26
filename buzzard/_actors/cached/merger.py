import functools
import collections

import multiprocessing as mp
import multiprocessing.pool
import numpy as np

from buzzard._actors.message import Msg
from buzzard._actors.pool_job import CacheJobWaiting, PoolJobWorking

class ActorMerger(object):
    """Actor that takes care of merging several array into one fp
    TODO: in this state it is used only for cached
          aren't they merge operations even in not cached rasters
          yes there are, but it will be developped later, these classes are still only for cached rasters
    """

    def __init__(self, raster):
        self._raster = raster
        self._alive = True
        merge_pool = raster.merge_pool
        if merge_pool is not None:
            self._waiting_room_address = '/Pool{}/WaitingRoom'.format(id(merge_pool))
            self._working_room_address = '/Pool{}/WorkingRoom'.format(id(merge_pool))
            if isinstance(merge_pool, mp.ThreadPool):
                self._same_address_space = True
            elif isinstance(merge_pool, mp.Pool):
                self._same_address_space = False
            else:
                assert False, 'Type should be checked in facade'
        self._waiting_jobs = set()
        self._working_jobs = set()

        self.dst_array = None

    @property
    def address(self):
        return '/Raster{}/Merger'.format(self._raster.uid)

    @property
    def alive(self):
        return self._alive

    # ******************************************************************************************* **
    def receive_merge_those_arrays(self, cache_fp, array_per_fp):
        msgs = []
        assert len(array_per_fp) > 0

        if len(array_per_fp) == 1:
            (fp, arr), = array_per_fp.items()
            assert fp == cache_fp
            msgs += [
                Msg('Writer', 'write_this_array', cache_fp, arr)
            ]
        elif self._raster.computation_pool is None:
            work = self._create_work_job(cache_fp, array_per_fp)
            res = work.func()
            res = self._normalize_user_result(res)
            msgs += self._commit_work_result(work, res)
        else:
            wait = Wait(self, cache_fp, array_per_fp)
            self._waiting_jobs.add(wait)
            msgs += [Msg(self._waiting_room_address, 'schedule_job', wait)]

        return msgs

    def receive_token_to_working_room(self, job, token):
        self._waiting_jobs.remove(job)
        work = self._create_work_job(job.cache_fp, job.array_per_fp)
        self._working_jobs.add(work)
        return [
            Msg(self._working_room_address, 'launch_job_with_token', work, token)
        ]

    def receive_job_done(self, job, result):
        self._working_jobs.remove(work)
        return self._commit_work_result(job, result)

    def receive_die(self):
        """Receive message: The raster was killed"""
        assert self._alive
        self._alive = False

        msgs = []
        for job in self._waiting_jobs:
            msgs += [Msg(self._waiting_room_address, 'unschedule_job', job)]
        for job in self._working_jobs:
            msgs += [Msg(self._working_room_address, 'cancel_job', job)]
        self._waiting_jobs.clear()
        self._working_jobs.clear()

        return []

    # ******************************************************************************************* **
    def _create_work_job(self, cache_fp, array_per_fp):
        return Work(
            self, cache_fp, array_per_fp
        )

    def _commit_work_result(self, cache_fp, arr):
        return [
            Msg('Writer', 'write_this_array', cache_fp, arr)
        ]

    def _normalize_user_result(self, cache_fp, res):
        try:
            res = np.atleast_3d(res)
        except:
            raise ValueError("Result of recipe's `merge_arrays` has type {}, it can't be converted to ndarray".format(
                type(res)
            ))
        y, x, c = res.shape
        if (y, x) != cache_fp.shape:
            raise ValueError("Result of recipe's `merge_arrays` has shape `{}`, should start with {}".format(
                res.shape,
                cache_fp.shape,
            ))
        if c != len(self._raster):
            raise ValueError("Result of recipe's `merge_arrays` has shape `{}`, should have {} bands".format(
                res.shape,
                len(self._raster),
            ))
        res = res.astype(self._raster.dtype, copy=False)
        return res

    # ******************************************************************************************* **

class Wait(CacheJobWaiting):
    def __init__(self, actor, cache_fp, array_per_fp):
        self.cache_fp = cache_fp
        self.array_per_fp = array_per_fp
        super().__init__(actor.address, actor._raster.uid, self.cache_fp, 3, self.cache_fp)

class Work(PoolJobWorking):
    def __init__(self, actor, cache_fp, array_per_fp):
        self.cache_fp = cache_fp

        if actor._raster.resample_pool is None or actor._same_address_space:
            func = functools.partial(
                # TODO: Refine `merge_arrays` function prototype
                self._raster.merge_arrays,
                cache_fp,
                array_per_fp,
                actor._raster.facade_proxy,
            )
        else:
            func = functools.partial(
                self._raster.merge_arrays,
                cache_fp,
                array_per_fp,
                None
            )

        super().__init__(actor.address, func)
