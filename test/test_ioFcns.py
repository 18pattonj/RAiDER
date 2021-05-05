import datetime
import h5py
import os
import pytest

import numpy as np

from datetime import time
from test import TEST_DIR
from osgeo import gdal, osr

from RAiDER.ioFcns import (
    gdal_extents,
    gdal_open, 
)


def test_gdal_extent():
    # Create a simple georeferenced test file
    ds = gdal.GetDriverByName('GTiff').Create('test.tif', 11, 11, 1, gdal.GDT_Float64)
    ds.SetGeoTransform((17.0, 0.1, 0, 18.0, 0, -0.1))
    band = ds.GetRasterBand(1)
    band.WriteArray(np.random.randn(11, 11))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds = None
    band = None

    assert gdal_extents('test.tif') == [17.0, 18.0, 18.0, 17.0]

def test_gdal_extent2():
    with pytest.raises(AttributeError):
        gdal_extents(os.path.join(TEST_DIR, "test_geom", "lat.rdr"))

def test_gdal_open():
    out = gdal_open(os.path.join(TEST_DIR, "test_geom", "lat.rdr"), False)

    assert np.allclose(out.shape, (45, 226))