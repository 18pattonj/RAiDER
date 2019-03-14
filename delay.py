"""Compute the delay from a point to the transmitter.

Dry and hydrostatic delays are calculated in separate functions.
Currently we take samples every _STEP meters, which causes either
inaccuracies or inefficiencies, and we no longer can integrate to
infinity.
"""


from osgeo import gdal
gdal.UseExceptions()

# standard imports
import datetime
import itertools
import numpy as np
import os
import pyproj
import tempfile
#import queue
import threading
import sys

# local imports
import constants as const
import demdownload
import losreader
import util


# Step in meters to use when integrating
_STEP = const._STEP
# Top of the troposphere
_ZREF = const._ZMAX


class Zenith:
    """Special value indicating a look vector of "zenith"."""
    pass


def _too_high(positions, zref):
    """Find index of first position higher than zref.

    This is useful when we're trying to cut off integration at the top
    of the troposphere. I calculate the list of all points, then use
    this function to compute the first index above the troposphere, then
    I can cut the list down to just the important points.
    """
    positions_ecef = np.moveaxis(positions, -1, 0)
    positions_lla = np.stack(util.ecef2lla(*positions_ecef))
    high_indices = np.where(positions_lla[2] > zref)[0]
    first_high_index = high_indices[0] if high_indices.size else len(positions)
    return first_high_index


def _get_lengths(look_vecs):
    '''
    Returns the lengths of a vector or set of vectors
    '''
    if look_vecs is Zenith:
        return _ZREF

    lengths = np.linalg.norm(look_vecs, axis=-1)
    lengths[~np.isfinite(lengths)] = 0
    return lengths


def _getZenithLookVecs(lats, lons, heights, zref = _ZREF):

    '''
    Returns look vectors when Zenith is used
    '''
    return (np.array((util.cosd(lats)*util.cosd(lons),
                              util.cosd(lats)*util.sind(lons),
                              util.sind(lats))).T
                    * (zref - heights)[..., np.newaxis])


def _compute_ray(L, S, V, stepSize):
    '''
    Compute and return points along a ray, given a total length, 
    start position (in x,y,z) and a unit look vector.
    '''
    # Have to handle the case where there are invalid data
    try:
        thisspace = np.arange(0, L, stepSize)
    except ValueError:
        thisspace = np.array([])
    ray = S + thisspace[..., np.newaxis]*V
    return ray


def _helper(tup):
    return _compute_ray(tup[0], tup[1], tup[2], tup[3])
    #return _compute_ray(L, S, V, stepSize)

def _get_rays_p(lengths, stepSize, start_positions, scaled_look_vecs, Nproc = 4):
    import multiprocessing as mp

    # setup for multiprocessing
    data = zip(lengths, start_positions, scaled_look_vecs, [stepSize]*len(lengths))

    pool = mp.Pool(Nproc)
    positions_l = pool.map(helper, data)
    return positions_l


def _get_rays_d(lengths, stepSize, start_positions, scaled_look_vecs, Nproc = 2):
   import dask.bag as db
   L = db.from_sequence(lengths)
   S = db.from_sequence(start_positions)
   Sv = db.from_sequence(scaled_look_vecs)
   Ss = db.from_sequence([stepSize]*len(lengths))

   # setup for multiprocessing
   data = db.zip(L, S, Sv, Ss)

   positions_l = db.map(helper, data)
   return positions_l.compute()


def _get_rays(lengths, stepSize, start_positions, scaled_look_vecs):
    '''
    Create the integration points for each ray path. 
    ''' 
    positions_l= []
    rayData = zip(lengths, start_positions, scaled_look_vecs)
    for L, S, V in rayData:
        positions_l.append(_compute_ray(L, S, V, stepSize))

    return positions_l


def _transform(ray, oldProj, newProj):
    '''
    Transform a ray from one coordinate system to another
    '''
    newRay = np.stack(
                pyproj.transform(
                      oldProj, newProj, ray[:,0], ray[:,1], ray[:,2])
                      ,axis = -1)
    return newRay


def _re_project(tup): 
    newPnt = _transform(tup[0],tup[1], tup[2])
    return newPnt


def getIntFcn(weatherObj, itype = 'wet', interpType = 'rgi'):
    '''
    Function to create and return an Interpolator object
    '''
    import interpolator as intprn

    ifFun = intprn.Interpolator()
    ifFun.setPoints(*weatherObj.getPoints())
    ifFun.setProjection(weatherObj.getProjection())

    if itype == 'wet':
        ifFun.getInterpFcns(weatherObj.getWetRefractivity(), interpType = interpType)
    elif itype == 'hydro':
        ifFun.getInterpFcns(weatherObj.getHydroRefractivity(), interpType = interpType)
    return ifFun
 

def _common_delay(weatherObj, lats, lons, heights, 
                  look_vecs, 
                  stepSize = _STEP, interpType = 'rgi',
                  verbose = False, nproc = 8):
    """
    This function calculates the line-of-sight vectors, estimates the point-wise refractivity
    index for each one, and then integrates to get the total delay in meters. The point-wise
    delay is calculated by interpolating the weatherObj, which contains a weather model with
    wet and hydrostatic refractivity at each weather model grid node, to the points along 
    the ray. The refractivity is integrated along the ray to get the final delay. 

    Inputs: 
     weatherObj - a weather model object
     lats       - Grid of latitudes for each ground point
     lons       - Grid of longitudes for each ground point
     heights    - Grid of heights for each ground point
     look_vecs  - Grid of look vectors (should be full-length) for each ground point
     raytrace   - If True, will use the raytracing method, if False, will use the Zenith 
                  + projection method
     stepSize   - Integration step size in meters 
     intpType   - Can be one of 'scipy': LinearNDInterpolator, or 'sane': _sane_interpolate. 
                  Any other string will use the RegularGridInterpolate method

    Outputs: 
     delays     - A list containing the wet and hydrostatic delays for each ground point in 
                  meters. 
    """
    import dask.bag as db
    import multiprocessing as mp
    if look_vecs is Zenith:
#        _,_,zs = weatherObj.getPoints()
        look_vecs = _getZenithLookVecs(lats, lons, heights, zref = _ZREF)
#        wet_pw  = weatherObj.getWetRefractivity()
#        hydro_pw= weatherObj.getHydroRefractivity()
#        wet_delays = _integrateZenith(zs, wet_pw)
#        hydro_delays = _integrateZenith(zs, hydro_pw)
#        return wet_delays,hydro_delays

    if verbose:
        import time
        print('_common_delay: Starting look vector calculation')
        print('_common_delay: The integration stepsize is {} m'.format(stepSize))
        st = time.time()

    # TODO: check adding accuracy of last fraction of the point
    mask = np.isnan(heights)
    # Get the integration points along the look vectors
    # First get the length of each look vector, get integration steps along 
    # each, then get the unit vector pointing in the same direction
    lengths = _get_lengths(look_vecs)
    lengths[mask] = np.nan
    start_positions = np.array(util.lla2ecef(lats, lons, heights)).T
    scaled_look_vecs = look_vecs / lengths[..., np.newaxis]
    positions_l= _get_rays(lengths, stepSize, start_positions, scaled_look_vecs)

    if verbose:
        print('_common_delay: Finished _get_rays')
        ft = time.time()
        print('Ray initialization took {:4.2f} secs'.format(ft-st))
        print('_common_delay: Starting _re_project')
        st = time.time()

    ecef = pyproj.Proj(proj='geocent')
    newProj = weatherObj.getProjection()
    def f(x):
        return _transform(x, ecef, newProj)
    newPts = list(map(f, positions_l))

    if verbose:
        print('_common_delay: Finished re-projecting')
        print('_common_delay: The size of look_vecs is {}'.format(np.shape(look_vecs)))
        ft = time.time()
        print('Re-projecting took {:4.2f} secs'.format(ft-st))
        print('_common_delay: Starting Interpolation')
        st = time.time()

    # Define the interpolator
    import pdb
    pdb.set_trace()
    ifWet = getIntFcn(weatherObj,interpType =interpType)
    ifHydro = getIntFcn(weatherObj,itype = 'hydro', interpType = interpType)

    # call the interpolator on each ray
    def interpRayWet(ray):
        return ifWet(ray)[0]
    def interpRayHydro(ray):
        return ifHydro(ray)[0]

    # Use dask to parallelize the interpolation. Unfortunately does not
    # give very good  results, in that I'm getting only a factor of 3
    # speed-up for a lot of cores, but that's 1000 seconds faster for 
    # my smal region, so worth doing. 

    useDask = True
    if useDask:
        if verbose:
            print('Beginning interpolation using Dask')
        Npart = min(len(newPts)//100 + 1, 400)
        PntBag = db.from_sequence(newPts, npartitions=Npart)
        wet_pw = PntBag.map(interpRayWet).compute()
        hydro_pw = PntBag.map(interpRayHydro).compute()
    else:
        if verbose:
            print('Beginning interpolation without Dask')
        wet_pw, hydro_pw = [], []
        for pnt in newPts:
            wet_pw.append(interpRayWet(pnt))
            hydro_pw.append(interpRayHydro(pnt))
        

    if verbose:
        print('_common_delay: Finished interpolation')
        ft = time.time()
        print('Interpolation took {:4.2f} secs'.format(ft-st))
        print('Average of {:1.6f} secs/ray'.format(.5*(ft-st)/len(newPts)))
        print('_common_delay: finished point-wise delay calculations')

    delays = _integrateLOS(stepSize, wet_pw, hydro_pw)

    return delays


def _integrateLOS(stepSize, wet_pw, hydro_pw):
    delays = [] 
    for d in (wet_pw, hydro_pw):
        delays.append(_integrate_delays(stepSize, d))
    return delays


def _integrateZenith(zs, pw):
    return 1e-6*np.trapz(pw, zs, axis = 2)


# integrate the delays to get overall delay
def int_fcn(y, dx):
    return 1e-6*dx*np.nansum(y)


def _integrate_delays(stepSize, refr):
    '''
    This function gets the actual delays by integrating the refractivity in 
    each node. Refractivity is given in the 'refr' variable. 
    '''
    delays = []
    for ray in refr:
        delays.append(int_fcn(ray, stepSize))
    return delays


def wet_delay(weather, lats, lons, heights, look_vecs, raytrace=True, verbose = False):
    """Compute wet delay along the look vector."""

    if verbose:
        print('wet_delay: Running _common_delay for weather.wet_delay')

    return _common_delay(weather.wet_delay, lats, lons, heights, look_vecs,
                         verbose)


def hydrostatic_delay(weather, lats, lons, heights, look_vecs, raytrace=True, verbose = False):
    """Compute hydrostatic delay along the look vector."""

    if verbose:
        print('hydrostatic_delay: Running _common_delay for weather.hydrostatic_delay')

    return _common_delay(weather.hydrostatic_delay, lats, lons, heights,
                         look_vecs, verbose)


def delay_over_area(weather, 
                    lat_min, lat_max, lat_res, 
                    lon_min, lon_max, lon_res, 
                    ht_min, ht_max, ht_res, 
                    los=Zenith, 
                    parallel = True, verbose = False):
    """Calculate (in parallel) the delays over an area."""
    lats = np.arange(lat_min, lat_max, lat_res)
    lons = np.arange(lon_min, lon_max, lon_res)
    hts = np.arange(ht_min, ht_max, ht_res)

    if verbose:
        print('delay_over_area: Size of lats: {}'.format(np.shape(lats)))
        print('delay_over_area: Size of lons: {}'.format(np.shape(lons)))
        print('delay_over_area: Size of hts: {}'.format(np.shape(hts)))

    # It's the cartesian product (thanks StackOverflow)
    llas = np.array(np.meshgrid(lats, lons, hts)).T.reshape(-1, 3)
    if verbose:
        print('delay_over_area: Size of llas: {}'.format(np.shape(llas)))

    if verbose:
        print('delay_over_area: running delay_from_grid')

    return delay_from_grid(weather, llas, los, parallel=parallel, verbose = verbose)


def _parmap(f, i):
    """Execute f on elements of i in parallel."""
    # Queue of jobs
    q = queue.Queue()
    # Space for answers
    answers = list()
    for idx, x in enumerate(i):
        q.put((idx, x))
        answers.append(None)

    def go():
        while True:
            try:
                i, elem = q.get_nowait()
            except queue.Empty:
                break
            answers[i] = f(elem)

    threads = [threading.Thread(target=go) for _ in range(os.cpu_count())]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    return answers


def delay_from_grid(weather, llas, los, parallel=False, raytrace=True, verbose = False):
    """Calculate delay on every point in a list.

    weather is the weather object, llas is a list of lat, lon, ht points
    at which to calculate delay, and los an array of line-of-sight
    vectors at each point. Pass parallel=True if you want to have real
    speed. If raytrace=True, we'll do raytracing, if raytrace=False,
    we'll do projection.
    """

    # Save the shape so we can restore later, but flatten to make it
    # easier to think about
    real_shape = llas.shape[:-1]
    llas = llas.reshape(-1, 3)
    # los can either be a bunch of vectors or a bunch of scalars. If
    # raytrace, then it's vectors, otherwise scalars. (Or it's Zenith)
    if verbose: 
        if los is Zenith:
           print("LOS is Zenith")
        else:
           print('LOS is not Zenith')

    if los is not Zenith:
        if raytrace:
            los = los.reshape(-1, 3)
        else:
            los = los.flatten()

    lats, lons, hts = np.moveaxis(llas, -1, 0)

    # Call _common_delay to compute both the hydrostatic and wet delays
    wet, hydro = _common_delay(weather, lats, lons, hts, los, verbose = verbose)
#    hydro = hydrostatic_delay(weather, lats, lons, hts, los,
#    wet = wet_delay(weather, lats, lons, hts, los, raytrace=raytrace, verbose = verbose)

    # Restore shape
    if los is not Zenith:
        hydro, wet = np.stack((hydro, wet)).reshape((2,) + real_shape)

    return hydro, wet


def delay_from_files(weather, lat, lon, ht, parallel=False, los=Zenith,
                     raytrace=True, verbose = False):
    """Read location information from files and calculate delay."""
    lats = util.gdal_open(lat)
    lons = util.gdal_open(lon)
    hts = util.gdal_open(ht)

    if los is not Zenith:
        incidence, heading = util.gdal_open(los)
        if raytrace:
            los = losreader.los_to_lv(
                incidence, heading, lats, lons, hts, _ZREF).reshape(-1, 3)
        else:
            los = incidence.flatten()

    # We need the three to be the same shape so that we know what to
    # reshape hydro and wet to. Plus, them being different sizes
    # indicates a definite user error.
    if not lats.shape == lons.shape == hts.shape:
        raise ValueError('lat, lon, and ht should have the same shape, but ' + 
                         'instead lat had shape {}, lon had shape '.format(lats.shape) + 
                         '{}, and ht had shape {}'.format(lons.shape,hts.shape))

    llas = np.stack((lats.flatten(), lons.flatten(), hts.flatten()), axis=1)
    hydro, wet = delay_from_grid(weather, llas, los,
                                 parallel=parallel, raytrace=raytrace, verbose = verbose)
    hydro, wet = np.stack((hydro, wet)).reshape((2,) + lats.shape)
    return hydro, wet


def _tropo_delay_with_values(los, lats, lons, hts, 
                             weather, zref, 
                             time, 
                             raytrace = True,
                             parallel = True, verbose = False):
    """Calculate troposphere delay from processed command-line arguments."""
    # LOS
    if los is None:
        los = Zenith
    else:
        los = losreader.infer_los(los, lats, lons, hts, zref)

    # We want to test if any shapes are different
    test1 = hts.shape == lats.shape == lons.shape
    try:
        test = los.shape[:-1] != hts.shape
    except:
        test2 = los is not Zenith

    if not test1 or test2:
        raise ValueError(
         'I need lats, lons, heights, and los to all be the same shape. ' +
         'lats had shape {}, lons had shape {}, '.format(lats.shape, lons.shape)+
         'heights had shape {}, and los was not Zenith'.format(hts.shape))

    if verbose: 
        print('_tropo_delay_with_values: called delay_from_grid')

    # Do the calculation
    llas = np.stack((lats, lons, hts), axis=-1)
    hydro, wet = delay_from_grid(weather, llas, los, parallel=parallel,
                                 raytrace=raytrace, verbose = verbose)
    return hydro, wet


def get_weather_and_nodes(model, filename, zmin=None):
    """Look up weather information from a model and file.

    We use the module.load method to load the weather model file, but
    we'll also create a weather model object for it.
    """
    xs, ys, proj, t, q, z, lnsp = model.load(filename)
    return (reader.read_model_level(module, xs, ys, proj, t, q, z, lnsp, zmin),
            xs, ys, proj)


def tropo_delay(los = None, lat = None, lon = None, 
                heights = None, 
                weather = None, 
                zref = 15000, 
                out = None, 
                wmLoc = None, 
                wmName = None,
                time = None,
                outformat='ENVI', 
                parallel=True,
                writeLL = True,
                verbose = False, 
                download_only = False):
    """Calculate troposphere delay from command-line arguments.

    We do a little bit of preprocessing, then call
    _tropo_delay_with_values. Then we'll write the output to the output
    file.
    """
    import pyproj

    if verbose:
        print('Weather Model Name: {}'.format(wmName))
        print('type of time: {}'.format(type(time)))
        print('Location to put/get weather model file: {}'.format(wmLoc))
        print('Download-only is {}'.format(download_only))
        print('Will format weather model file to: {} format'.format(outformat))

    if out is None:
        out = os.getcwd()
    if wmLoc is None:
        wmLoc = out

    # Make weather
    weather_type = weather['type']
    weather_files = weather['files']
    weather_fmt = weather['name']

    allowedWMTypes = ['ERA-I', 'ERA-5', 'MERRA-2', 'WRF', 'HRRR', 'pickle']
    if weather_fmt not in allowedWMTypes:
        raise RuntimeError('Weather model {} not allowed/implemented'.format(weather_fmt))

    # weather model file for storing the weather model
    if wmName is None:
        wmName = os.path.join(wmLoc, '{}_{}.nc'
                   .format(weather_fmt, datetime.datetime.strftime(time, '%Y_%m_%d_T%H_%M_%S')))

    # For later
    str1 = time.isoformat() + "_" if time is not None else ""
    str2 = "z" if los is None else "s" 
    str3 = 'td.{}'.format(outformat)
    hydroname, wetname = (
        '{}_{}_'.format(weather_fmt, dtyp) + str1 + str2 + str3
        for dtyp in ('hydro', 'wet'))

    hydro_file_name = os.path.join(out, hydroname)
    wet_file_name = os.path.join(out, wetname)

    # set_geo_info should be a list of functions to call on the dataset,
    # and each will do some bit of work
    set_geo_info = list()

    # Lat, lon
    if lat is None:
        # They'll get set later with weather
        lats = lons = None
        latproj = lonproj = None
    else:
        try:
            lats, latproj = util.gdal_open(lat, returnProj = True)
            lons, lonproj = util.gdal_open(lon, returnProj = True)
        except:
            lats = lat
            lons = lon
            latproj = lonproj = None
            lon = lat = None

    # set_geo_info should be a list of functions to call on the dataset,
    # and each will do some bit of work
    set_geo_info = list()
    if lat is not None:
        def geo_info(ds):
            ds.SetMetadata({'X_DATASET': os.path.abspath(lat), 'X_BAND': '1',
                            'Y_DATASET': os.path.abspath(lon), 'Y_BAND': '1'})
        set_geo_info.append(geo_info)
    # Is it ever possible that lats and lons will actually have embedded
    # projections?
    if latproj:
        def geo_info(ds):
            ds.SetProjection(latproj)
        set_geo_info.append(geo_info)
    elif lonproj:
        def geo_info(ds):
            ds.SetProjection(lonproj)
        set_geo_info.append(geo_info)

    height_type, height_info = heights
    if verbose:
        print('Type of height: {}'.format(height_type))
        if weather_files is not None:
            print('{} weather files'.format(len(weather_files)))


    if weather_type == 'wrf':
        import wrf
        weather = wrf.WRF()
        weather.load(*weather_files)

        # Let lats and lons to weather model nodes if necessary
        #TODO: Need to fix the case where lats are None, because
        # the weather model need not be in latlong projection
        if lats is None:
            lats, lons = wrf.wm_nodes(*weather_files)
    elif weather_type == 'pickle':
        weather = util.pickle_load(weather_files)
    else:
        weather_model = weather_type
        if weather_files is None:
            if lats is None:
                raise ValueError(
                    'Unable to infer lats and lons if you also want me to '
                    'download the weather model')

            f = os.path.join(out, wmName)
            if not os.path.exists(f):
                try:
                   weather_model.fetch(lats, lons, time, f)
                except Exception as e:
                   print('ERROR: Unable to download weather data')
                   print('Exception encountered: {}'.format(e))
                   sys.exit(0)
            else:
                print('WARNING: Weather model already exists, skipping download')
            if download_only:
                print('WARNING: download_only flag selected. I will only '\
                      'download the weather'\
                      ' model, without doing any further processing.')
                return None, None

            weather_model.load(f)
            weather = weather_model
        else:
            weather, xs, ys, proj = weather_model.weather_and_nodes(
                weather_files)
            if lats is None:
                def geo_info(ds):
                    ds.SetProjection(str(proj))
                    ds.SetGeoTransform((xs[0], xs[1] - xs[0], 0, ys[0], 0,
                                        ys[1] - ys[0]))
                set_geo_info.append(geo_info)
                lla = pyproj.Proj(proj='latlong')
                xgrid, ygrid = np.meshgrid(xs, ys, indexing='ij')
                lons, lats = pyproj.transform(proj, lla, xgrid, ygrid)

    if weather_type != 'pickle':
        try:
            import pickle
            with open('pickledHRRR.pik', 'wb') as f:
                pickle.dump(weather, f)
        except:
            print('Tried to pickle the weather model, could not')


    # must be done even if it already exists
    lats,lons = weather.getLL() 

    writeLL = False
    if writeLL: 
       import util
       lonFileName = '{}_Lon_{}.dat'.format(weather_fmt, 
                         datetime.datetime.strftime(time, '%Y_%m_%d_T%H_%M_%S'))
       latFileName = '{}_Lat_{}.dat'.format(weather_fmt, 
                         datetime.datetime.strftime(time, '%Y_%m_%d_T%H_%M_%S'))
       util.writeArrayToRaster(lons, lonFileName)
       util.writeArrayToRaster(lats, latFileName)

    lla = weather.getProjection()
    if verbose:
        print(type(weather))
        print(weather._xs.shape)
        print(weather)
        #p = weather.plot(p)

    # Height
    if height_type == 'dem':
        try:
            import util
            hts = util.gdal_open(height_info)
        except RuntimeError:
            print('WARNING: File {} could not be opened, proceeding with DEM download'.format(height_info))
            hts = demdownload.download_dem(lats, lons)
    elif height_type == 'lvs':
        hts = height_info

    if height_type == 'download':
        hts = demdownload.download_dem(lats, lons)

    # Pretty different calculation depending on whether they specified a
    # list of heights or just a DEM
    if height_type == 'lvs':
        shape = (len(hts),) + lats.shape
        total_hydro = np.zeros(shape)
        total_wet = np.zeros(shape)
        for i, ht in enumerate(hts):
            hydro, wet = _tropo_delay_with_values(
                los, lats, lons, np.broadcast_to(ht, lats.shape), weather,
                zref, time, parallel=parallel, verbose = verbose)
            total_hydro[i] = hydro
            total_wet[i] = wet

        if outformat == 'hdf5':
            raise NotImplemented
        else:
            drv = gdal.GetDriverByName(outformat)
            hydro_ds = drv.Create(
                hydro_file_name, total_hydro.shape[2],
                total_hydro.shape[1], len(hts), gdal.GDT_Float64)
            for lvl, (hydro, ht) in enumerate(zip(total_hydro, hts), start=1):
                band = hydro_ds.GetRasterBand(lvl)
                band.SetDescription(str(ht))
                band.WriteArray(hydro)
            for f in set_geo_info:
                f(hydro_ds)
            hydro_ds = None
        
            wet_ds = drv.Create(
                wet_file_name, total_wet.shape[2],
                total_wet.shape[1], len(hts), gdal.GDT_Float64)
            for lvl, (wet, ht) in enumerate(zip(total_wet, hts), start=1):
                band = wet_ds.GetRasterBand(lvl)
                band.SetDescription(str(ht))
                band.WriteArray(wet)
            for f in set_geo_info:
                f(wet_ds)
            wet_ds = None

    else:
        hydro, wet = _tropo_delay_with_values(
            los, lats, lons, hts, weather, zref, time, parallel = parallel, verbose = verbose)
    
        # Write the output file
        # TODO: maybe support other files than ENVI
        if outformat == 'hdf5':
            raise NotImplemented
        else:
            drv = gdal.GetDriverByName(outformat)
            hydro_ds = drv.Create(
                hydro_file_name, hydro.shape[1], hydro.shape[0],
                1, gdal.GDT_Float64)
            hydro_ds.GetRasterBand(1).WriteArray(hydro)
            for f in set_geo_info:
                f(hydro_ds)
            hydro_ds = None
            wet_ds = drv.Create(
                wet_file_name, wet.shape[1], wet.shape[0], 1,
                gdal.GDT_Float64)
            wet_ds.GetRasterBand(1).WriteArray(wet)
            for f in set_geo_info:
                f(wet_ds)
            wet_ds = None

    return hydro_file_name, wet_file_name
