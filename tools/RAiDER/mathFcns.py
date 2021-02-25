"""Geodesy-related utility functions."""
import importlib
import multiprocessing as mp
import os
import re
from datetime import datetime, timedelta

import h5py
import numpy as np
import pandas as pd
import pyproj
from osgeo import gdal, osr

from RAiDER.constants import Zenith
from RAiDER import Geo2rdr
from RAiDER.logger import *

gdal.UseExceptions()


def sind(x):
    """Return the sine of x when x is in degrees."""
    return np.sin(np.radians(x))


def cosd(x):
    """Return the cosine of x when x is in degrees."""
    return np.cos(np.radians(x))