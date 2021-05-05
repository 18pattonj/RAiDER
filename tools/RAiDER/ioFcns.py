""" General functions related to file I/O """
import h5py
import importlib
import os
import pyproj
import re
import requests

import numpy as np
import pandas as pd
import xarray as xr

from datetime import datetime
from osgeo import gdal, osr
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

gdal.UseExceptions()


def gdal_extents(fname):
    if os.path.exists(fname + '.vrt'):
        fname = fname + '.vrt'
    try:
        ds = gdal.Open(fname, gdal.GA_ReadOnly)
    except Exception:
        raise OSError('File {} could not be opened'.format(fname))

    # Check whether the file is georeferenced
    proj = ds.GetProjection()
    gt = ds.GetGeoTransform()
    if not proj or not gt:
        raise AttributeError('File {} does not contain geotransform information'.format(fname))

    xSize, ySize = ds.RasterXSize, ds.RasterYSize

    return [gt[0], gt[0] + (xSize - 1) * gt[1] + (ySize - 1) * gt[2], gt[3], gt[3] + (xSize - 1) * gt[4] + (ySize - 1) * gt[5]]


def gdal_open(fname, returnProj=False, userNDV=None):
    if os.path.exists(fname + '.vrt'):
        fname = fname + '.vrt'
    try:
        ds = gdal.Open(fname, gdal.GA_ReadOnly)
    except:
        raise OSError('File {} could not be opened'.format(fname))
    proj = ds.GetProjection()
    gt = ds.GetGeoTransform()

    val = []
    for band in range(ds.RasterCount):
        b = ds.GetRasterBand(band + 1)  # gdal counts from 1, not 0
        data = b.ReadAsArray()
        if userNDV is not None:
            logger.debug('Using user-supplied NoDataValue')
            data[data == userNDV] = np.nan
        else:
            try:
                ndv = b.GetNoDataValue()
                data[data == ndv] = np.nan
            except:
                logger.debug('NoDataValue attempt failed*******')
        val.append(data)
        b = None
    ds = None

    if len(val) > 1:
        data = np.stack(val)
    else:
        data = val[0]

    if not returnProj:
        return data
    else:
        return data, proj, gt
