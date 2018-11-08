"""
# Part 1: A raster file configured to be read asynchronously

 By default in *buzzard* when calling `get_data()` on a raster file opened normally, all the data is read from disk at once (using one `gdal.Band.ReadAsArray()` for example), and all the optional resampling is then performed in one step (using one `cv2.remap()` for example). When performing this operation on a large chunk of data, it would be much more efficient to read and resample __tile by tile to parallelize__ those tasks. To do so, use `async_=True` in `open_raster()` and `create_raster()`.

Another feature unlocked by using an _async raster_ to read a file is the `iter_data()` method. Compared to the `get_data()` method that takes a _Footprint_ and return an _ndarray_, this new method takes a _list of Footprint_ and return an _iterator of ndarray_. By using this method the next array to be yielded is prepared in priority and the next ones are also prepared at the same time if there are enough workers available. You can control how much arrays can be made available in advance by setting the optional `max_queue_size=5` parameter of the `iter_data()` method, this allows you to __prevent backpressure__ if you consume the _iterator of ndarray_ too slowly.

As seen before, the `async_` parameter can be a _boolean_, but instead of `True` you can also pass a _dict of options_ to parameterize how the raster is handled in the background. Some options control the amount of chunking to perform for the read and resampling steps, some other options allow you to choose the two _thread pools_ that will be used for reading and resampling. By default a single pool is shared by all _async rasters_ for _io_ operations (like reading a file), and another pool is shared for cpu intensive operations (like resampling).

This kind of __ressource sharing__ between rasters is not trivial and requires some synchronization. To do so, a thread (called the _scheduler_) is spawned in the `DataSource` to manage the queries to rasters. As you will see in the next parts, the _scheduler_ is able to manage other kind of rasters.
"""

import time
import os

import buzzard as buzz

import example_tools

def main():
    path = example_tools.create_random_elevation_gtiff() # TODO: tiled gtiff in construction
    ds = buzz.DataSource(allow_interpolation=True)

    print('Classic opening')
    # Features:
    # - Disk reads are not tiled
    # - Resampling operations are not tiled
    with ds.aopen_raster(path).close as r:
        test_raster(r)

    print('Opening within scheduler')
    # Features:
    # - Disk reads are automatically tiled and parallelized
    # - Resampling operations are automatically tiled and parallelized
    # - `iter_data()` method is available
    with ds.aopen_raster(path, async_=True).close as r:
        # `async_=True` is equivalent to
        # `async_={}`, and also equivalent to
        # `async_={io_pool='io', resample_pool='cpu', max_resampling_size=512, max_read_size=512}`
        test_raster(r)

    # `DataSource.close()` closes all rasters, the scheduler, and the pools.
    # If you let the garbage collector collect the `DataSource`, the rasters and
    # the scheduler will be correctly closed, but the pools will leak memory.
    ds.close()

    os.remove(path)

def test_raster(r):
    """Basic testing functions. It will be reused throughout those tests"""
    print('| Test 1 - Print raster informations')
    fp = r.fp
    if r.get_keys():
        print(f'|   key: {r.get_keys()[0]}')
    print(f'|   type: {type(r).__name__}')
    print(f'|   dtype: {r.dtype}, band-count: {len(r)}')
    print(f'|   Footprint: center:{fp.c}, scale:{fp.scale}')
    print(f'|              size(m):{fp.size}, raster-size(px):{fp.rsize}')
    fp_lowres = fp.intersection(fp, scale=fp.scale * 2)

    # *********************************************************************** **
    print('| Test 2 - Getting the full raster')
    with example_tools.Timer() as t:
        arr = r.get_data(band=-1)
    print(f'|   took {t}, {fp.rarea / float(t):_.0f} pixel/sec')

    # *********************************************************************** **
    print('| Test 3 - Getting and downsampling the full raster')
    with example_tools.Timer() as t:
        arr = r.get_data(fp=fp_lowres, band=-1)
    print(f'|   took {t}, {fp_lowres.rarea / float(t):_.0f} pixel/sec')

    # *********************************************************************** **
    print('| Test 4 - Getting the full raster in 9 tiles with a slow main'
          'thread')
    tiles = fp.tile_count(3, 3, boundary_effect='shrink').flatten()
    if hasattr(r, 'iter_data'):
        # Using `iter_data` of async rasters
        arr_iterator = r.iter_data(tiles, band=-1)
    else:
        # Making up an `iter_data` for classic rasters
        arr_iterator = (
            r.get_data(fp=tile, band=-1)
            for tile in tiles
        )
    with example_tools.Timer() as t:
        for tile, arr in zip(tiles, arr_iterator):
            time.sleep(1 / 9)
    print(f'|   took {t}, {r.fp.rarea / float(t):_.0f} pixel/sec\n')

if __name__ == '__main__':
    main()
