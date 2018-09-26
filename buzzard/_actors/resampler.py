import functools
import collections

import multiprocessing as mp
import multiprocessing.pool
import numpy as np

from buzzard._actors.message import Msg
from buzzard._actors.pool_job import ProductionJobWaiting, PoolJobWorking
from buzzard._a_proxy_raster_remap import ABackProxyRasterRemapMixin

class ActorResampler(object):
    """Actor that takes care of resamplig sample tiles, and wait for all
    resamplings to be performed for a production array.
    """

    def __init__(self, raster):
        self._raster = raster
        self._alive = True
        resample_pool = raster.resample_pool
        if resample_pool is not None:
            self._waiting_room_address = '/Pool{}/WaitingRoom'.format(id(resample_pool))
            self._working_room_address = '/Pool{}/WorkingRoom'.format(id(resample_pool))
            if isinstance(resample_pool, mp.ThreadPool):
                self._same_address_space = True
            elif isinstance(resample_pool, mp.Pool):
                self._same_address_space = False
            else:
                assert False, 'Type should be checked in facade'
        self._waiting_jobs = set()
        self._working_jobs = set()

        self._prod_array_of_prod_tile = (
            collections.defaultdict(dict)
        ) # type: Mapping[CachedQueryInfos, Mapping[int, np.ndarray]]
        self._missing_resample_fps_per_prod_tile = (
            collections.defaultdict(dict)
        ) # type: Mapping[CachedQueryInfos, Mapping[int, Set[Footprint]]]

    @property
    def address(self):
        return '/Raster{}/Resampler'.format(self._raster.uid)

    @property
    def alive(self):
        return self._alive

    # ******************************************************************************************* **
    def receive_resample_and_accumulate(self, qi, prod_idx, sample_fp, resample_fp, subsample_array):
        """Receive message: A resampling operation is ready to be performed

        Parameters
        ----------
        qi: _actors.cached.query_infos.QueryInfos
        prod_idx: int
        sample_fp: None or Footprint of shape (Y, X)
        resample_fp: Footprint of shape (Y', X')
        subsample_array: None or ndarray of shape (Y, X)
        """
        msgs = []

        pi = qi.prod[prod_idx]
        interpolation_needed = pi.share_area and not pi.same_grid
        if self._raster.resample_pool is not None and interpolation_needed:
            # Need to externalize resampling on a pool
            wait = Wait(self, qi, prod_idx, sample_fp, resample_fp, subsample_array)
            self._waiting_jobs.add(wait)
            msgs += [
                Msg(self._waiting_room_address, 'schedule_job', wait)
            ]
        else:
            # Perform remapping on the scheduler
            if interpolation_needed:
                job = self._create_work_job(
                    self, qi, prod_idx, sample_fp, resample_fp, subsample_array,
                )
                job.func()
                msgs += self._commit_work_result(job, None)

            else:
                # There is no need to accumulate
                if not pi.share_area:
                    # production footprint is fully outside raster
                    assert sample_fp is None
                    assert subsample_array is None
                    arr = np.full(
                        np.r_[resample_fp.shape, len(qi.band_ids)],
                        qi.dst_nodata, raster.dtype,
                    )
                elif sample_fp == pi.fp:
                    # production footprint is fully inside raster
                    assert subsample_array.shape[:2] == tuple(resample_fp.shape)
                    arr = subsample_array
                    if self._raster.nodata is not None and self._raster.nodata != qi.dst_nodata:
                        arr[arr == self._raster.nodata] = qi.dst_nodata
                    if qi.band_ids != qi.unique_band_ids:
                        indices = [
                            qi.unique_band_ids.find(bi)
                            for bi in qi.band_ids
                        ]
                        arr = arr[..., indices]
                else:
                    # production footprint is both inside and outside raster
                    arr = np.full(
                        np.r_[resample_fp.shape, len(qi.unique_band_ids)],
                        qi.dst_nodata, raster.dtype,
                    )
                    slices = sample_fp.slice_in(pr.fp)
                    arr[slices] = subsample_array
                    if self._raster.nodata is not None and self._raster.nodata != qi.dst_nodata:
                        arr[slices][arr == self._raster.nodata] = qi.dst_nodata
                    if qi.band_ids != qi.unique_band_ids:
                        indices = [
                            qi.unique_band_ids.find(bi)
                            for bi in qi.band_ids
                        ]
                        arr = arr[..., indices]

                msgs += [Msg(
                    'Producer', 'made_this_array', qi, prod_idx, arr
                )]

        return msgs

    def receive_token_to_working_room(self, job, token):
        """Receive message: Waiting job can proceede to working room"""
        msgs = []
        self._waiting_jobs.remove(job)

        work = self._create_work_job(
            self, job.qi, job.prod_idx, job.sample_fp, job.resample_fp, job.subsample_array,
        )
        self._working_jobs.add(work)

        return [
            Msg(self._working_room_address, 'launch_job_with_token', work, token)
        ]

    def receive_job_done(self, job, result):
        self._working_jobs.remove(job)
        return self._commit_work_result(job, result)

    def receive_cancel_this_query(self, qi):
        """Receive message: One query was dropped

        Parameters
        ----------
        qi: _actors.cached.query_infos.QueryInfos
        """
        msgs = []

        # Cancel waiting jobs
        jobs_to_kill = [
            job
            for job in self._waiting_jobs
            if job.qi == qi
        ]
        for job in jobs_to_kill:
            msgs += [Msg(self._waiting_room_address, 'unschedule_job', job)]
            self._waiting_jobs.remove(job)

        # Cancel working jobs
        jobs_to_kill = [
            job
            for job in self._working_jobs
            if job.qi == qi
        ]
        for job in self._working_jobs:
            msgs += [Msg(self._working_room_address, 'cancel_job', job)]
            self._working_jobs.remove(job)

        return []

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
    def _create_work_job(self, qi, prod_idx, sample_fp, resample_fp, subsample_array):
        pi = qi.prod[prod_idx]

        if (qi not in self._prod_array_of_prod_tile or
            prod_idx not in self._prod_array_of_prod_tile[qi]):
            self._prod_array_of_prod_tile[qi][prod_idx] = np.full(
                np.r_[pi.fp.shape, len(qi.band_ids)],
                qi.dst_nodata, raster.dtype,
            )
            self._missing_resample_fps_per_prod_tile[qi][prod_idx] = set(pi.resample_fps)
        arr = self._prod_array_of_prod_tile[qi][prod_idx]

        return Work(
            self, qi, prod_idx,
            sample_fp, resample_fp,
            subsample_array, arr
        )

    def _commit_work_result(self, work_job, res):
        msgs = []

        qi = work_job.qi
        prod_idx = work_job.prod_idx
        resample_fp = work_job.resample_fp

        self._missing_resample_fps_per_prod_tile[qi][prod_idx].remove(resample_fp)

        if not self._same_address_space:
            work_job.dst_array_slice[:] = res
        else:
            assert res is None

        if len(self._missing_resample_fps_per_prod_tile[qi][prod_idx]) == 0:
            arr = self._prod_array_of_prod_tile[qi][prod_idx]

            # Produce array
            if qi.band_ids != qi.unique_band_ids:
                indices = [
                    qi.unique_band_ids.find(bi)
                    for bi in qi.band_ids
                ]
                arr = arr[..., indices]
            msgs += [
                Msg('Producer', 'made_this_array', qi, prod_idx, arr)
            ]

            # Garbage collect
            del self._missing_resample_fps_per_prod_tile[qi][prod_idx]
            del self._prod_array_of_prod_tile[qi][prod_idx]
            if len(self._missing_resample_fps_per_prod_tile[qi]) == 0:
                del self._missing_resample_fps_per_prod_tile[qi]
                del self._prod_array_of_prod_tile[qi]

        return msgs

    # ******************************************************************************************* **

class Wait(ProductionJobWaiting):

    def __init__(self, actor, qi, prod_idx, sample_fp, resample_fp, subsample_array):
        self.qi = qi
        self.prod_idx = prod_idx
        self.sample_fp = sample_fp
        self.resample_fp = resample_fp
        self.subsample_array = subsample_array
        super().__init__(actor.address, qi, prod_idx, 0, self.resample_fp)

class Work(PoolJobWorking):
    def __init__(self, actor, qi, prod_idx, sample_fp, resample_fp, subsample_array, dst_array):
        self.qi = qi
        self.prod_idx = prod_idx
        self.resample_fp = resample_fp
        produce_fp = qi.prod[prod_idx].fp

        dst_array_slice = dst_array[resample_fp.slice_in(produce_fp)]

        if actor._raster.resample_pool is None or actor._same_address_space:
            func = functools.partial(
                _resample_subsample_array,
                sample_fp, resample_fp, subsample_array,
                actor._raster.nodata, qi.dst_nodata,
                qi.interpolation, dst_array_slice,
            )
        else:
            self.dst_array_slice = dst_array_slice
            func = functools.partial(
                _resample_subsample_array,
                sample_fp, resample_fp, subsample_array,
                actor._raster.nodata, qi.dst_nodata,
                qi.interpolation, None,
            )

        super().__init__(actor.address, func)

def _resample_subsample_array(sample_fp, resample_fp, subsample_array, src_nodata, dst_nodata, interpolation, dst_opt):
    """
    Parameters
    ----------
    sample_fp: Footprint of shape (Y, X)
        source footprint (before resampling)
    resample_fp: Footprint of shape (Y', X')
        destination footprint
    subsample_array: np.ndarray of shape (Y, X)
        source array (sould match sample_fp)
    src_nodata: None or nbr
    dst_nodata: nbr
    interpolation: str
    dst_opt: None or np.ndarray
        optional destination for resample

    Returns
    -------
    None or np.ndarray of shape (Y', X')
    """

    # TODO: Inplace remap
    res = ABackProxyRasterRemapMixin.remap(
        src_fp=sample_fp, dst_fp=resample_fp,
        array=subsample_array, mask=None,
        src_nodata=src_nodata, dst_nodata=dst_nodata,
        mask_mode='dilate', interpolation=interpolation,
    )
    if dst_opt is not None:
        dst_opt[:] = res
        return None
    else:
        return res
