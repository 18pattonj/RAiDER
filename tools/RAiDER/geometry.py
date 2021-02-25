"""Geodesy-related utility functions."""
import importlib
import os

import numpy as np
import pyproj

from RAiDER import Geo2rdr
from RAiDER.mathFcns import cosd, sind
from RAiDER.constants import Zenith