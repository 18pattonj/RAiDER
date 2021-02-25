""" General function related to file I/O """
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

from RAiDER.utilFcns import checkLOS

gdal.UseExceptions()