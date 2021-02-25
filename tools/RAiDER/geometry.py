"""Geodesy-related utility functions."""
import importlib
import os

import numpy as np
import pyproj

from RAiDER import Geo2rdr
from RAiDER.mathFcns import sind, cosd
from RAiDER.constants import Zenith