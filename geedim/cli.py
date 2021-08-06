"""
    Copyright 2021 Dugal Harris - dugalh@gmail.com

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
"""

import click

from geedim import search

# map collection keys to classes
# cls_col_map = {'landsat7_c2_l2': lambda: search.LandsatImSearch(collection='landsat7_c2_l2'),
#                  'landsat8_c2_l2': lambda: search.LandsatImSearch(collection='landsat8_c2_l2'),
#                  'sentinel2_toa': lambda: search.Sentinel2CloudlessImSearch(collection='sentinel2_toa'),
#                  'sentinel2_sr': lambda: search.Sentinel2CloudlessImSearch(collection='sentinel2_sr'),
#                  'modis_nbar': lambda: search.LandsatImSearch(collection='modis_nbar')}

cls_col_map = {'landsat7_c2_l2': search.LandsatImSearch,
               'landsat8_c2_l2': search.LandsatImSearch,
               'sentinel2_toa': search.Sentinel2CloudlessImSearch,
               'sentinel2_sr': search.Sentinel2CloudlessImSearch,
               'modis_nbar': search.ModisNbarImSearch}

@click.group()
def cli():
    pass

@click.command()
@click.option(
    "-x",
    "--dataset",
    type=click.Choice(list(cls_col_map.keys()), case_sensitive=False),
    help="Collection.",
    default="landsat8_c2_l2",
)
@click.option(
    "-b",
    "--bbox",
    type=click.FLOAT,
    nargs=4,
    help="Bounding box in WGS84 (xmin, ymin, xmax, ymax).",
)
@click.option("-s", "--start_date", type=click.DateTime, help="Start date (YYYY-MM-DD).")
@click.option("-e", "--end_date", type=click.DateTime, help="End date (YYYY-MM-DD).")
def search(collection, bbox, start_date, end_date):
    imsearch = cls_col_map[collection](collection=collection)

    xmin, ymin, xmax, ymax = bbox
    coordinates = [[xmax, ymax], [xmax, ymin], [xmin, ymin], [xmin, ymax], [xmax, ymax]]
    bbox_dict = dict(type='Polygon', coordinates=[coordinates])

    res = imsearch.search(start_date, end_date, bbox_dict)
