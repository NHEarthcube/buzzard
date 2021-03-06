import os
import time
import multiprocessing as mp
import multiprocessing.pool

import buzzard as buzz
import numpy as np
import scipy.ndimage

import example_tools
from part1 import test_raster

def main():
    return # None of the features shown here are implemented yet
    path = example_tools.create_random_elevation_gtiff()
    ds = buzz.Dataset()

    # Pool to parallelize:
    # - `ds.slopes` computations
    # - `ds.elevation` resamplings
    cpu_pool = mp.pool.ThreadPool(mp.cpu_count())

    # Pool to parallelize:
    # - `ds.elevation` disk reads
    io_pool = mp.pool.ThreadPool(4)

    ds.open_raster(
        'elevation',
        path=path,
        async_={'io_pool': io_pool, 'resample_pool': cpu_pool},
    )
    ds.create_raster_recipe(
        'slopes',
        computation_pool=cpu_pool,

        # The next 6 lines can be replaced by **buzz.algo.slopes(ds.elevation)
        fp=ds.elevation.fp,
        dtype='float32',
        channel_count=1,
        compute_array=slopes_of_elevation,
        queue_data_per_primitive={'dem': ds.elevation.queue_data},
        convert_footprint_per_primitive={'dem': lambda fp: fp.dilate(1)},
    )

    # Test 1 - Perform basic tests ****************************************** **
    # `test_raster` will request `slopes`'s' pixels. `elevation`'s' pixels will
    # be requested in cascade and then used to compute the `slopes`.
    test_raster(ds.slopes)

    # Test 2 - Multiple iterations at the same time ************************* **
    # Here the `elevation` raster is directly requested and also requested by
    # the `slopes`, the Dataset's scheduler is made to handle simultaneous
    # queries.
    tiles = ds.elevation.fp.tile_count(2, 2).flatten()
    dem_iterator = ds.elevation.iter_data(tiles)
    slopes_iterator = ds.slopes.iter_data(tiles)
    for tile, dem, slopes in zip(tiles, dem_iterator, slopes_iterator):
        print(f'Showing dem and slopes at:\n {tile}')
        example_tools.show_several_images(
            ('elevation (dem)', tile, dem),
            ('slopes', tile, slopes),
        )

    # Test 3 - Backpressure prevention ************************************** **
    tiles = ds.slopes.tile_count(3, 3).flatten()

    print('Creating a slopes iterator on 9 tiles')
    it = ds.slopes.iter_data(tiles, max_queue_size=1)
    print('  At most 5 dem arrays can be ready between `ds.elevation` and '
          '`ds.slopes`')
    print('  At most 1 slopes array can be ready out of the slopes iterator')

    print('Sleeping several seconds to let the scheduler create 6 of the 9 '
          'dem arrays, and 1 of the 9 slopes arrays.')
    time.sleep(4)

    with example_tools.Timer() as t:
        arr = next(it)
    print(f'Getting the first array took {t}, this was instant because it was '
          'ready')

    with example_tools.Timer() as t:
        for _ in range(5):
            next(it)
    print(f'Getting the next 5 arrays took {t}, it was quick because the dems '
          'were ready')

    with example_tools.Timer() as t:
        for _ in range(3):
            next(it)
    print(f'Getting the last 4 arrays took {t}, it was long because nothing was'
          ' ready')

    # Cleanup *************************************************************** **
    ds.close()
    os.remove(path)

def slopes_of_elevation(fp, primitive_fps, primitive_arrays, slopes):
    """A function to be fed to `compute_array` when constructing a recipe"""
    arr = primitive_arrays['dem']
    kernel = [
        [0, 1, 0],
        [1, 1, 1],
        [0, 1, 0],
    ]
    arr = (
        scipy.ndimage.maximum_filter(arr, None, kernel) -
        scipy.ndimage.minimum_filter(arr, None, kernel)
    )
    arr = arr[1:-1, 1:-1]
    arr = np.arctan(arr / fp.pxsizex)
    arr = arr / np.pi * 180.
    return arr

if __name__ == '__main__':
    main()
