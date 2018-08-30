from buzzard._actors.message import Msg

class ActorProducer(object):
    """Actor that takes care of waiting for cache tiles reads and launching resamplings"""

    def __init__(self, raster):
        self._raster = raster
        self._alive = True

        self._produce_per_query = collections.defaultdict(dict)

    @property
    def address(self):
        return '/Raster{}/Producer'.format(self._raster.uid)

    @property
    def alive(self):
        return self._alive

    # ******************************************************************************************* **
    def receive_make_this_array(self, qi, prod_idx):
        """Receive message: Start making this array"""
        msgs = []

        pi = qi.prod[prod_idx]
        pr = _ProdArray(pi)

        if len(qi.prod[prod_idx].cache_fps) != 0:
            msgs += [
                'CacheExtractor', 'sample_those_cache_files_to_an_array', qi, prod_idx,
            ]

        for resample_fp, cache_fps in self.resample_needs.items():
            if len(cache_fps) == 0:
                sample_fp = pi.resample_sample_dep_fp[resample_fp]
                assert sample_fp is None
                sample_array = None
                msgs += [Msg(
                    'Resampler', 'resample_and_accumulate',
                    qi, prod_idx, resample_fp, sample_array,
                )]

        self._produce_per_query[qi][prod_idx] = pr
        return msgs

    def receive_sampled_a_cache_file_to_the_array(self, qi, prod_idx, cache_fp, array):
        """Receive message: A cache file was read for that output array"""
        msgs = []
        pr = self._produce_per_query[qi][prod_idx]
        pi = pr.pi
        if pr.sample_array is None:
            pr.sample_array = array
        else:
            assert array is pr.sample_array

        for resample_fp, cache_fps in self.resample_needs.items():
            if cache_fp in cache_fps:
                cache_fps.remove(cache_fp)
            if len(cache_fps) == 0:
                sample_fp = pi.resample_sample_dep_fp[resample_fp]
                if sample_fp is None:
                    sample_array = None
                else:
                    sl = sample_fp.slice_in(pi.sample_fp)
                    sample_array = pr.sample_array[sl]
                    assert sample_array is not None
                msgs += [Msg(
                    'Resampler', 'resample_and_accumulate',
                    qi, prod_idx, resample_fp, sample_array,
                )]

        return msgs

    def receive_made_this_array(self, qi, prod_idx, array):
        """Receive message: Done creating an output array"""
        del self._produce_per_query[qi][prod_idx]
        if len(self._produce_per_query[qi]) == 0:
            del self._produce_per_query[qi]
        return [Msg(
            'QueriesHandler', 'made_this_array', qi, prod_idx, array
        )]

    def receive_cancel_this_query(self, qi):
        """Receive message: One query was dropped

        Parameters
        ----------
        qi: _actors.cached.query_infos.QueryInfos
        """
        del self._produce_per_query[qi]
        return []

    def receive_die(self):
        """Receive message: The raster was killed"""
        assert self._alive
        self._alive = False

        self._produce_per_query.clear()
        return []

    # ******************************************************************************************* **

class _ProdArray(self):

    def __init__(self, pi):
        self.pi = pi
        self.resample_needs = {
            resample_fp: set(cache_fps)
            for resample_fp, cache_fps in pi.resample_cache_deps_fps
        }
        self.sample_array = None
