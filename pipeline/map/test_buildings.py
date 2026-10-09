"""3D Massing conversion rules.  .venv/bin/python -m pytest pipeline/map/test_buildings.py -q"""
import numpy as np

from pipeline.map import buildings


def test_height_falls_back_to_avg_when_max_is_zero():
    assert buildings.height(0.0, 443.0) == 443.0
    assert buildings.height(247.9, 95.8) == 247.9


def test_web_mercator_back_to_lon_lat():
    x, y = buildings.to_merc(-79.3957, 43.6487)
    lon, lat = buildings.to_lonlat(np.array([[x, y]]))[0]
    assert abs(lon + 79.3957) < 1e-9 and abs(lat - 43.6487) < 1e-9


def test_hole_is_kept_and_the_outer_ring_turns_counter_clockwise():
    outer = np.array([(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)], float)  # clockwise, as shapefiles store outers
    hole = np.array([(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)], float)
    g = buildings.polygon([outer, hole])
    assert g.geom_type == "Polygon" and len(g.interiors) == 1 and g.exterior.is_ccw and g.area == 96


def test_rings_under_one_square_metre_are_dropped():
    assert buildings.polygon([np.array([(0, 0), (0, 0.5), (0.5, 0.5), (0.5, 0), (0, 0)], float)]) is None
