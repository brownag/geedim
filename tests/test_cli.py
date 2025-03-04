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
import json
import pathlib
from datetime import datetime
from glob import glob
from typing import List, Dict

import numpy as np
import pytest
import rasterio as rio
from click.testing import CliRunner
from geedim.cli import cli
from geedim.utils import root_path
from rasterio.coords import BoundingBox
from rasterio.crs import CRS
from rasterio.features import bounds
from rasterio.warp import transform_geom


@pytest.fixture
def runner():
    """ click runner for command line execution. """
    return CliRunner()


@pytest.fixture
def region_25ha_file() -> pathlib.Path:
    """ Path to region_25ha geojson file. """
    return root_path.joinpath('tests/data/region_25ha.geojson')


@pytest.fixture
def region_100ha_file() -> pathlib.Path:
    """ Path to region_100ha geojson file. """
    return root_path.joinpath('tests/data/region_100ha.geojson')


@pytest.fixture
def region_10000ha_file() -> pathlib.Path:
    """ Path to region_10000ha geojson file. """
    return root_path.joinpath('tests/data/region_10000ha.geojson')


@pytest.fixture()
def l4_5_image_id_list(l4_image_id, l5_image_id) -> List[str]:
    """ A list of landsat 4 & 5 image ID's. """
    return [l4_image_id, l5_image_id]


@pytest.fixture()
def l8_9_image_id_list(l8_image_id, l9_image_id) -> List[str]:
    """ A list of landsat 8 & 9 image ID's. """
    return [l8_image_id, l9_image_id]


@pytest.fixture()
def s2_sr_image_id_list() -> List[str]:
    """ A list of Sentinel-2 SR image IDs. """
    return [
        'COPERNICUS/S2_SR/20211004T080801_20211004T083709_T34HEJ',
        'COPERNICUS/S2_SR/20211123T081241_20211123T083704_T34HEJ',
        'COPERNICUS/S2_SR/20220107T081229_20220107T083059_T34HEJ'
    ]


@pytest.fixture()
def gedi_image_id_list() -> List[str]:
    """ A list of GEDI canopy top height ID's. """
    return [
        'LARSE/GEDI/GEDI02_A_002_MONTHLY/202009_018E_036S', 'LARSE/GEDI/GEDI02_A_002_MONTHLY/202010_018E_036S',
        'LARSE/GEDI/GEDI02_A_002_MONTHLY/202112_018E_036S'
    ]


def _test_downloaded_file(
    filename: pathlib.Path, region: Dict = None, crs: str = None, scale: float = None, dtype: str = None,
    scale_offset: bool = None
):
    """ Helper function to test image file format against given parameters. """
    with rio.open(filename, 'r') as ds:
        ds: rio.DatasetReader = ds
        assert ds.nodata is not None
        array = ds.read(masked=True)
        am = array.mean()
        assert np.isfinite(am) and (am != 0)
        if region:
            exp_region = transform_geom('EPSG:4326', ds.crs, region)
            exp_bounds = BoundingBox(*bounds(exp_region))
            assert (
                (ds.bounds[0] <= exp_bounds[0]) and (ds.bounds[1] <= exp_bounds[1]) and
                (ds.bounds[2] >= exp_bounds[2]) and (ds.bounds[3] >= exp_bounds[3])
            )
        if crs:
            assert CRS(ds.crs) == CRS.from_string(crs)
        if scale:
            assert abs(ds.transform[0]) == scale
        if dtype:
            assert ds.dtypes[0] == dtype
        if scale_offset:
            refl_bands = [
                i for i in range(1, ds.count + 1)
                if ('center_wavelength' in ds.tags(i)) and (float(ds.tags(i)['center_wavelength']) < 1)
            ]
            array = ds.read(refl_bands, masked=True)
            assert all(array.min(axis=(1, 2)) >= -0.5)
            assert all(array.max(axis=(1, 2)) <= 1.5)


@pytest.mark.parametrize(
    'name, start_date, end_date, region, fill_portion, cloudless_portion, is_csmask', [
        ('LANDSAT/LC09/C02/T1_L2', '2022-01-01', '2022-02-01', 'region_100ha_file', 10, 50, True),
        ('LANDSAT/LE07/C02/T1_L2', '2022-01-01', '2022-02-01', 'region_100ha_file', 0, 0, True),
        ('LANDSAT/LT05/C02/T1_L2', '2005-01-01', '2006-02-01', 'region_100ha_file', 40, 50, True),
        ('COPERNICUS/S2_SR', '2022-01-01', '2022-01-15', 'region_100ha_file', 0, 50, True),
        ('COPERNICUS/S2', '2022-01-01', '2022-01-15', 'region_100ha_file', 50, 40, True),
        ('COPERNICUS/S2_SR_HARMONIZED', '2022-01-01', '2022-01-15', 'region_100ha_file', 0, 50, True),
        ('COPERNICUS/S2_HARMONIZED', '2022-01-01', '2022-01-15', 'region_100ha_file', 50, 40, True),
        ('LARSE/GEDI/GEDI02_A_002_MONTHLY', '2021-11-01', '2022-01-01', 'region_100ha_file', 1, 0, False)
    ]
)
def test_search(
    name, start_date: str, end_date: str, region: str, fill_portion: float, cloudless_portion: float, is_csmask: bool,
    tmp_path: pathlib.Path, runner: CliRunner, request: pytest.FixtureRequest
):
    """
    Test search command gives valid results for different cloud/shadow maskable, and generic collections.
    """
    region_file: Dict = request.getfixturevalue(region)
    results_file = tmp_path.joinpath('search_results.json')
    cli_str = (
        f'search -c {name} -s {start_date} -e {end_date} -r {region_file} -fp {fill_portion} '
        f'-cp {cloudless_portion} -op {results_file}'
    )
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (results_file.exists())
    with open(results_file, 'r') as f:
        properties = json.load(f)

    start_date = datetime.strptime(start_date, '%Y-%m-%d')
    end_date = datetime.strptime(end_date, '%Y-%m-%d')
    im_dates = np.array(
        [datetime.utcfromtimestamp(im_props['system:time_start'] / 1000) for im_props in properties.values()]
    )
    # test FILL_PORTION in expected range
    im_fill_portions = np.array([im_props['FILL_PORTION'] for im_props in properties.values()])
    assert np.all(im_fill_portions >= fill_portion) and np.all(im_fill_portions <= 100)
    if is_csmask:  # is a cloud/shadow masked collection
        # test CLOUDLESS_PORTION in expected range
        im_cl_portions = np.array([im_props['CLOUDLESS_PORTION'] for im_props in properties.values()])
        assert np.all(im_cl_portions >= cloudless_portion) and np.all(im_cl_portions <= 100)
        assert np.all(im_cl_portions <= im_fill_portions)
    # test search result image dates lie between `start_date` and `end_date`
    assert np.all(im_dates >= start_date) and np.all(im_dates < end_date)
    # test search result image dates are sorted
    assert np.all(sorted(im_dates) == im_dates)


def test_config_search_s2(region_10000ha_file: pathlib.Path, runner: CliRunner, tmp_path: pathlib.Path):
    """ Test `config` sub-command chained with `search` of Sentinel-2 affects CLOUDLESS_PORTION as expected. """
    results_file = tmp_path.joinpath('search_results.json')
    name = 'COPERNICUS/S2_SR'
    cl_portion_list = []
    for prob in [40, 80]:
        cli_str = (
            f'config --prob {prob} search -c {name} -s 2022-01-01 -e 2022-02-01 -r {region_10000ha_file} -op '
            f'{results_file}'
        )
        result = runner.invoke(cli, cli_str.split())
        assert (result.exit_code == 0)
        assert (results_file.exists())
        with open(results_file, 'r') as f:
            properties = json.load(f)
        cl_portion_list.append(np.array([prop_dict['CLOUDLESS_PORTION'] for prop_dict in properties.values()]))

    assert np.any(cl_portion_list[0] < cl_portion_list[1])
    assert not np.any(cl_portion_list[0] > cl_portion_list[1])


def test_config_search_l9(region_10000ha_file: pathlib.Path, runner: CliRunner, tmp_path: pathlib.Path):
    """ Test `config` sub-command chained with `search` of Landsat-9 affects CLOUDLESS_PORTION as expected. """
    results_file = tmp_path.joinpath('search_results.json')
    name = 'LANDSAT/LC09/C02/T1_L2'
    cl_portion_list = []
    for param in ['--mask-shadows', '--no-mask-shadows']:
        cli_str = (
            f'config {param} search -c {name} -s 2022-02-15 -e 2022-04-01 -r {region_10000ha_file} -op'
            f' {results_file}'
        )
        result = runner.invoke(cli, cli_str.split())
        assert (result.exit_code == 0)
        assert (results_file.exists())
        with open(results_file, 'r') as f:
            properties = json.load(f)
        cl_portion_list.append(np.array([prop_dict['CLOUDLESS_PORTION'] for prop_dict in properties.values()]))

    assert np.any(cl_portion_list[0] < cl_portion_list[1])
    assert not np.any(cl_portion_list[0] > cl_portion_list[1])


def test_region_bbox_search(region_100ha_file: pathlib.Path, runner: CliRunner, tmp_path: pathlib.Path):
    """ Test --bbox gives same search results as --region <geojson file>. """

    results_file = tmp_path.joinpath('search_results.json')
    with open(region_100ha_file, 'r') as f:
        region = json.load(f)
    bbox = bounds(region)
    bbox_str = ' '.join([str(b) for b in bbox])
    cli_strs = [
        f'search -c LANDSAT/LC09/C02/T1_L2 -s 2022-01-01 -e 2022-02-01 -r {region_100ha_file} -op {results_file}',
        f'search -c LANDSAT/LC09/C02/T1_L2 -s 2022-01-01 -e 2022-02-01 -b {bbox_str} -op {results_file}'
    ]

    props_list = []
    for cli_str in cli_strs:
        result = runner.invoke(cli, cli_str.split())
        assert (result.exit_code == 0)
        assert (results_file.exists())
        with open(results_file, 'r') as f:
            properties = json.load(f)
        props_list.append(properties)

    assert props_list[0] == props_list[1]


def test_raster_region_search(const_image_25ha_file, region_25ha_file, runner: CliRunner, tmp_path: pathlib.Path):
    """ Test --region works with a raster file. """

    results_file = tmp_path.joinpath('search_results.json')
    cli_strs = [
        f'search -c LANDSAT/LC09/C02/T1_L2 -s 2022-01-01 -e 2022-02-01 -r {region_25ha_file} -op {results_file}',
        f'search -c LANDSAT/LC09/C02/T1_L2 -s 2022-01-01 -e 2022-02-01 -r {const_image_25ha_file} -op {results_file}'
    ]

    props_list = []
    for cli_str in cli_strs:
        result = runner.invoke(cli, cli_str.split())
        assert (result.exit_code == 0)
        assert (results_file.exists())
        with open(results_file, 'r') as f:
            properties = json.load(f)
        props_list.append(properties)

    assert props_list[0].keys() == props_list[1].keys()


@pytest.mark.parametrize(
    'image_id, region_file', [
        ('l8_image_id', 'region_25ha_file'),
        ('s2_sr_hm_image_id', 'region_25ha_file'),
        ('gedi_cth_image_id', 'region_25ha_file'),
    ]
)  # yapf: disable
def test_download_defaults(
    image_id: str, region_file: pathlib.Path, tmp_path: pathlib.Path, runner: CliRunner, request
):
    """ Test image download with default crs, scale, dtype etc.  """
    image_id = request.getfixturevalue(image_id)
    region_file = request.getfixturevalue(region_file)
    out_file = tmp_path.joinpath(image_id.replace('/', '-') + '.tif')

    cli_str = f'download -i {image_id} -r {region_file} -dd {tmp_path}'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (out_file.exists())

    # test downloaded file readability and format
    with open(region_file) as f:
        region = json.load(f)
    _test_downloaded_file(out_file, region)


@pytest.mark.parametrize(
    'image_id, region_file, crs, scale, dtype, mask, resampling, scale_offset', [
        ('l5_image_id', 'region_25ha_file', 'EPSG:3857', 30, 'uint16', False, 'near', False),
        ('l9_image_id', 'region_25ha_file', 'EPSG:3857', 30, 'float32', False, 'near', True),
        ('s2_toa_image_id', 'region_25ha_file', 'EPSG:3857', 10, 'float64', True, 'bilinear', True),
        ('modis_nbar_image_id', 'region_100ha_file', 'EPSG:3857', 500, 'int32', False, 'bicubic', False),
        ('gedi_cth_image_id', 'region_25ha_file', 'EPSG:3857', 10, 'float32', True, 'bilinear', False),
        ('landsat_ndvi_image_id', 'region_25ha_file', 'EPSG:3857', 30, 'float64', True, 'near', False),
    ]
)
def test_download_params(
    image_id: str, region_file: str, crs: str, scale: float, dtype: str, mask: bool, resampling: str,
    scale_offset: bool, tmp_path: pathlib.Path, runner: CliRunner, request: pytest.FixtureRequest
):
    """ Test image download, specifying all possible cli params. """
    image_id = request.getfixturevalue(image_id)
    region_file = request.getfixturevalue(region_file)
    out_file = tmp_path.joinpath(image_id.replace('/', '-') + '.tif')

    cli_str = (
        f'download -i {image_id} -r {region_file} -dd {tmp_path} --crs {crs} --scale {scale} --dtype {dtype} '
        f'--resampling {resampling}'
    )
    cli_str += ' --mask' if mask else ' --no-mask'
    cli_str += ' --scale-offset' if scale_offset else ' --no-scale-offset'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (out_file.exists())

    with open(region_file) as f:
        region = json.load(f)
    # test downloaded file readability and format
    _test_downloaded_file(out_file, region=region, crs=crs, scale=scale, dtype=dtype, scale_offset=scale_offset)


def test_export_params(l8_image_id: str, region_25ha_file: pathlib.Path, runner: CliRunner):
    """ Test export starts ok, specifying all cli params"""
    cli_str = (
        f'export -i {l8_image_id} -r {region_25ha_file} -df geedim/test --crs EPSG:3857 --scale 30 '
        f'--dtype uint16 --mask --resampling bilinear --no-wait'
    )
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)


@pytest.mark.parametrize('image_list, scale', [('s2_sr_image_id_list', 10), ('l8_9_image_id_list', 30)])
def test_composite_defaults(
    image_list: str, scale: float, region_25ha_file: pathlib.Path, runner: CliRunner, tmp_path: pathlib.Path,
    request: pytest.FixtureRequest
):
    """ Test composite with default CLI parameters.  """
    image_list = request.getfixturevalue(image_list)
    image_ids_str = ' -i '.join(image_list)
    cli_str = f'composite -i {image_ids_str} download --crs EPSG:3857 --scale {scale} -r {region_25ha_file} -dd' \
              f' {tmp_path}'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)

    # test downloaded file exists
    out_files = glob(str(tmp_path.joinpath(f'*COMP*.tif')))
    assert len(out_files) == 1

    # test downloaded file readability and format
    with open(region_25ha_file) as f:
        region = json.load(f)
    _test_downloaded_file(out_files[0], region)


@pytest.mark.parametrize(
    'image_list, method, region_file, date, mask, resampling, download_scale', [
        ('s2_sr_image_id_list', 'mosaic', None, '2021-10-01', True, 'near', 10),
        ('l8_9_image_id_list', 'q-mosaic', 'region_25ha_file', None, True, 'bilinear', 30),
        ('gedi_image_id_list', 'medoid', None, None, True, 'bilinear', 10),
    ]
)
def test_composite_params(
    image_list: str, method: str, region_file: str, date: str, mask: bool, resampling: str, download_scale: float,
    region_25ha_file, runner: CliRunner, tmp_path: pathlib.Path, request: pytest.FixtureRequest
):
    """ Test composite with default CLI parameters. """
    image_list = request.getfixturevalue(image_list)
    region_file = request.getfixturevalue(region_file) if region_file else None
    image_ids_str = ' -i '.join(image_list)
    cli_comp_str = f'composite -i {image_ids_str} -cm {method} --resampling {resampling}'
    cli_comp_str += f' -r {region_file}' if region_file else ''
    cli_comp_str += f' -d {date}' if date else ''
    cli_comp_str += ' --mask' if mask else ' --no-mask'
    cli_download_str = f'download -r {region_25ha_file} --crs EPSG:3857 --scale {download_scale} -dd {tmp_path}'
    cli_str = cli_comp_str + ' ' + cli_download_str
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)

    # test downloaded file exists
    out_files = glob(str(tmp_path.joinpath(f'*COMP*.tif')))
    assert len(out_files) == 1

    # test downloaded file readability and format
    with open(region_25ha_file) as f:
        region = json.load(f)
    _test_downloaded_file(out_files[0], region=region, crs='EPSG:3857', scale=download_scale)


def test_search_composite_download(region_25ha_file, runner: CliRunner, tmp_path: pathlib.Path):
    """ Test chaining of `search`, `composite` and `download`. """

    cli_search_str = f'search -c COPERNICUS/S1_GRD -s 2022-01-01 -e 2022-02-01 -r {region_25ha_file}'
    cli_comp_str = f'composite --mask'
    cli_download_str = f'download --crs EPSG:3857 --scale 10 -dd {tmp_path}'
    cli_str = cli_search_str + ' ' + cli_comp_str + ' ' + cli_download_str
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)

    # test downloaded file exists
    out_files = glob(str(tmp_path.joinpath(f'*COMP*.tif')))
    assert len(out_files) == 1

    # test downloaded file readability and format
    with open(region_25ha_file) as f:
        region = json.load(f)
    _test_downloaded_file(out_files[0], region=region, crs='EPSG:3857', scale=10)


def test_search_composite_x2_download(region_25ha_file, runner: CliRunner, tmp_path: pathlib.Path):
    """
    Test chaining of `search`, `composite`, `composite` and `download` i.e. the first composite is included as a
    component image in the second composite.
    """

    cli_search_str = f'search -c l7-c2-l2 -s 2022-01-15 -e 2022-04-01 -r {region_25ha_file} -cp 20'
    cli_comp1_str = f'composite --mask'
    cli_comp2_str = f'composite -i LANDSAT/LE07/C02/T1_L2/LE07_173083_20220103 -cm mosaic --date 2022-04-01 --mask'
    cli_download_str = f'download --crs EPSG:3857 --scale 30 -dd {tmp_path}'
    cli_str = cli_search_str + ' ' + cli_comp1_str + ' ' + cli_comp2_str + ' ' + cli_download_str
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)

    # test downloaded file exists
    out_files = glob(str(tmp_path.joinpath(f'*COMP*.tif')))
    assert len(out_files) == 1

    # test downloaded file readability and format
    with open(region_25ha_file) as f:
        region = json.load(f)
    _test_downloaded_file(out_files[0], region=region, crs='EPSG:3857', scale=30)
