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
import logging
import os
import pathlib
import re
import sys
from types import SimpleNamespace
from typing import List

import click
import rasterio.crs as rio_crs
from click.core import ParameterSource
from geedim import schema, Initialize, version
from geedim.collection import MaskedCollection
from geedim.download import BaseImage
from geedim.enums import CloudMaskMethod, CompositeMethod, ResamplingMethod
from geedim.mask import MaskedImage
from geedim.utils import get_bounds, Spinner
from rasterio.dtypes import dtype_ranges
from rasterio.errors import CRSError

logger = logging.getLogger(__name__)


class PlainInfoFormatter(logging.Formatter):
    """logging formatter to format INFO logs without the module name etc prefix"""

    def format(self, record):
        if record.levelno == logging.INFO:
            self._style._fmt = "%(message)s"
        else:
            self._style._fmt = "%(levelname)s:%(name)s: %(message)s"
        return super().format(record)


class ChainedCommand(click.Command):
    """
    click Command sub-class for managing parameters shared between chained commands.
    """

    def get_help(self, ctx):
        """ Strip some RST markup from the help text for CLI display.  Assumes no grid tables. """
        if not hasattr(self, 'click_wrap_text'):
            self.click_wrap_text = click.formatting.wrap_text
        # TODO: copy homonim sub_strings
        sub_strings = {
            '\b\n': '\n\b',  # convert from RST friendly to click literal (unwrapped) block marker
            ':option:': '',  # strip ':option:'
            '\| ': '',  # strip RST literal (unwrapped) marker in e.g. tables and bullet lists
            '\n\.\. _.*:\n': '',  # strip RST ref directive '\n.. <name>:\n'
            '`(.*)<(.*)>`_': '\g<1>',  # convert from RST cross-ref '`<name> <<link>>`_' to 'name'
            '::': ':'  # convert from RST '::' to ':'
        }

        def reformat_text(text, width, **kwargs):
            for sub_key, sub_value in sub_strings.items():
                text = re.sub(sub_key, sub_value, text, flags=re.DOTALL)
            return self.click_wrap_text(text, width, **kwargs)

        click.formatting.wrap_text = reformat_text
        return click.Command.get_help(self, ctx)

    def invoke(self, ctx):
        """Manage shared `image_list` and `region` parameters."""

        # initialise earth engine (do it here, rather than in cli() so that it does not delay --help)
        Initialize()

        # combine `region` and `bbox` into a single region in the context object
        region = ctx.params['region'] if 'region' in ctx.params else None
        bbox = ctx.params['bbox'] if 'bbox' in ctx.params else None
        region = region or bbox
        if region is not None:
            ctx.obj.region = region

        if 'image_id' in ctx.params:
            # append any image id's to the image_list
            ctx.obj.image_list += list(ctx.params['image_id'])

        return click.Command.invoke(self, ctx)


def _configure_logging(verbosity):
    """configure python logging level"""
    # adapted from rasterio https://github.com/rasterio/rasterio
    log_level = max(10, 20 - 10 * verbosity)

    # limit logging config to geedim by applying to package logger, rather than root logger
    # pkg_logger level etc are then 'inherited' by logger = getLogger(__name__) in the modules
    pkg_logger = logging.getLogger(__package__)
    formatter = PlainInfoFormatter()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    pkg_logger.addHandler(handler)
    pkg_logger.setLevel(log_level)
    logging.captureWarnings(True)


def _collection_cb(ctx, param, value):
    """click callback to validate collection name"""
    if value in schema.gd_to_ee:
        value = schema.gd_to_ee[value]
    return value


def _crs_cb(ctx, param, crs):
    """click callback to validate and parse the CRS."""
    if crs is not None:
        try:
            wkt_fn = pathlib.Path(crs)
            if wkt_fn.exists():  # read WKT from file, if it exists
                with open(wkt_fn, 'r') as f:
                    crs = f.read()

            crs = rio_crs.CRS.from_string(crs).to_wkt()
        except CRSError as ex:
            raise click.BadParameter(f'Invalid CRS value: {crs}.\n {str(ex)}', param=param)
    return crs


def _bbox_cb(ctx, param, value):
    """click callback to validate and parse --bbox"""
    if isinstance(value, tuple) and len(value) == 4:  # --bbox
        xmin, ymin, xmax, ymax = value
        coordinates = [[xmax, ymax], [xmax, ymin], [xmin, ymin], [xmin, ymax], [xmax, ymax]]
        value = dict(type='Polygon', coordinates=[coordinates])
    elif value is not None and len(value) != 0:
        raise click.BadParameter(f'Invalid bbox: {value}.', param=param)
    return value


def _region_cb(ctx, param, value):
    """click callback to validate and parse --region"""
    filename = value
    if isinstance(value, str):  # read region file/string
        if value == '-' or 'json' in value:
            with click.open_file(value, encoding='utf-8') as f:
                value = json.load(f)
        else:
            value = get_bounds(value, expand=10)
    elif value is not None and len(value) != 0:
        raise click.BadParameter(f'Invalid region: {filename}.', param=param)
    return value


def _mask_method_cb(ctx, param, value):
    """click callback to convert cloud mask method string to enum."""
    return CloudMaskMethod(value)


def _resampling_method_cb(ctx, param, value):
    """click callback to convert resampling string to enum."""
    return ResamplingMethod(value)


def _comp_method_cb(ctx, param, value):
    """click callback to convert composite method string to enum."""
    return CompositeMethod(value) if value else None


def _prepare_image_list(obj: SimpleNamespace, mask=False) -> List[MaskedImage, ]:
    """Validate and prepare the obj.image_list for export/download.  Returns a list of MaskedImage objects."""
    if len(obj.image_list) == 0:
        raise click.BadOptionUsage(
            'image_id', 'Either pass --id, or chain this command with a successful `search` or `composite`'
        )
    image_list = []
    for im_obj in obj.image_list:
        if isinstance(im_obj, str):
            im_obj = MaskedImage.from_id(im_obj, mask=mask, **obj.cloud_kwargs)
        elif isinstance(im_obj, MaskedImage):
            if mask:
                im_obj.mask_clouds()
        else:
            raise ValueError(f'Unsupported image object type: {type(im_obj)}')
        image_list.append(im_obj)

    if obj.region is None and any([not im.has_fixed_projection for im in image_list]):
        raise click.BadOptionUsage('region', 'One of --region or --bbox is required for a composite image.')
    return image_list


# Define click options that are common to more than one command
bbox_option = click.option(
    '-b', '--bbox', type=click.FLOAT, nargs=4, default=None, callback=_bbox_cb,
    help='Region defined by WGS84 bounding box co-ordinates (left, bottom, right, top).'
)
region_option = click.option(
    '-r', '--region', type=click.Path(exists=True, dir_okay=False, allow_dash=True), default=None, callback=_region_cb,
    help='Region defined by geojson polygon file, or raster file.  Use "-" to read geojson from stdin.'
)
crs_option = click.option(
    '-c', '--crs', type=click.STRING, default=None, callback=_crs_cb, show_default='source image CRS.',
    help='CRS to reproject image(s) to (EPSG string or path to WKT text file).'
)
scale_option = click.option(
    '-s', '--scale', type=click.FLOAT, default=None, show_default='minimum scale of the source image bands.',
    help='Pixel scale (size) to resample image(s) to (m).'
)
dtype_option = click.option(
    '-dt', '--dtype', type=click.Choice(list(dtype_ranges.keys()), case_sensitive=False), default=None,
    show_default='smallest data type able to represent the range of pixel values.',
    help='Data type to convert image(s) to.'
)
mask_option = click.option(
    '-m/-nm', '--mask/--no-mask', default=MaskedImage._default_mask, show_default=True,
    help='Whether to apply cloud/shadow mask(s); or fill mask(s), in the case of images without '
    'support for cloud/shadow masking.'
)
resampling_option = click.option(
    '-rs', '--resampling', type=click.Choice([rm.value for rm in ResamplingMethod], case_sensitive=True),
    default=BaseImage._default_resampling.value, show_default=True, callback=_resampling_method_cb,
    help='Resampling method.'
)
scale_offset_option = click.option(
    '-so/-nso', '--scale-offset/--no-scale-offset', default=False, show_default=True,
    help='Whether to apply any EE band scales and offsets to the image.'
)


# geedim CLI and chained command group
@click.group(chain=True)
@click.option('--verbose', '-v', count=True, help="Increase verbosity.")
@click.option('--quiet', '-q', count=True, help="Decrease verbosity.")
@click.version_option(version=version.__version__, message='%(version)s')
@click.pass_context
def cli(ctx, verbose, quiet):
    """ Search, composite and download Google Earth Engine imagery. """
    ctx.obj = SimpleNamespace(image_list=[], region=None, cloud_kwargs={})
    verbosity = verbose - quiet
    _configure_logging(verbosity)


# TODO: add clear docs on what is piped out of or into each command.
# TODO: use cloup and linear help layout.  move lists of option values from command docting to corresponding option help

# config command
@click.command(cls=ChainedCommand, context_settings=dict(auto_envvar_prefix='GEEDIM'))
@click.option(
    '-mc/-nmc', '--mask-cirrus/--no-mask-cirrus', default=True, show_default=True,
    help='Whether to mask cirrus clouds.  Valid for Landsat 8-9 images, and, for Sentinel-2 images with '
    'the `qa` :option:`--mask-method`.'
)
@click.option(
    '-ms/-nms', '--mask-shadows/--no-mask-shadows', default=True, show_default=True,
    help='Whether to mask cloud shadows.'
)
@click.option(
    '-mm', '--mask-method', type=click.Choice([cmm.value for cmm in CloudMaskMethod], case_sensitive=True),
    default=CloudMaskMethod.cloud_prob.value, show_default=True, callback=_mask_method_cb,
    help='Method used to mask clouds.  Valid for Sentinel-2 images. '
)
@click.option(
    '-p', '--prob', type=click.FloatRange(min=0, max=100), default=60, show_default=True,
    help='Cloud probability threshold (%). Valid for Sentinel-2 images with the `cloud-prob` :option:`--mask-method`'
)
@click.option(
    '-d', '--dark', type=click.FloatRange(min=0, max=1), default=.15, show_default=True,
    help='NIR reflectance threshold for shadow masking. NIR values below this threshold are '
    'potential cloud shadows.  Valid for Sentinel-2 images'
)
@click.option(
    '-sd', '--shadow-dist', type=click.INT, default=1000, show_default=True,
    help='Maximum distance (m) to look for cloud shadows from cloud edges.  Valid for Sentinel-2 images.'
)
@click.option(
    '-b', '--buffer', type=click.INT, default=50, show_default=True,
    help='Distance (m) to dilate cloud/shadow.  Valid for Sentinel-2 images.'
)
@click.option(
    '-cdi', '--cdi-thresh', type=click.FloatRange(min=-1, max=1), default=None,
    help='Cloud Displacement Index (CDI) threshold.  Values below this threshold are considered potential clouds.  '
    'Valid for Sentinel-2 images.  By default, the CDI is not used.'
)
@click.option(
    '-mcd', '--max-cloud-dist', type=click.INT, default=5000, show_default=True,
    help='Maximum distance (m) to look for clouds.  Used to form the cloud distance band for the `q-mosaic` '
    'compositing ``--method``.'
)
@click.pass_context
def config(ctx, mask_cirrus, mask_shadows, mask_method, prob, dark, shadow_dist, buffer, cdi_thresh, max_cloud_dist):
    # @formatter:off
    """
    Configure cloud/shadow masking.

    Chain this command with one or more other command(s) to configure cloud/shadow masking for those operation(s).
    A sensible default configuration is used if this command is not specified.

    Cloud/shadow masking is supported for the collections:
    \b

        ===========  ===========================
        geedim name  EE name
        ===========  ===========================
        l4-c2-l2     LANDSAT/LT04/C02/T1_L2
        l5-c2-l2     LANDSAT/LT05/C02/T1_L2
        l7-c2-l2     LANDSAT/LE07/C02/T1_L2
        l8-c2-l2     LANDSAT/LC08/C02/T1_L2
        l9-c2-l2     LANDSAT/LC09/C02/T1_L2
        s2-toa       COPERNICUS/S2
        s2-sr        COPERNICUS/S2_SR
        s2-toa-hm    COPERNICUS/S2_HARMONIZED
        s2-sr-hm     COPERNICUS/S2_SR_HARMONIZED
        ===========  ===========================
    \b

    For Sentinel-2 collections, ``--mask-method`` can be one of:
    \b

        * | `cloud-prob`: Use a threshold on the corresponding Sentinel-2 cloud
          | probability image.
        * | `qa`: Use the Sentinel-2 `QA60` quality band.
    \b

    Examples
    --------

    Search the Sentinel-2 SR collection for images with a cloudless portion of at least 60%, where cloud/shadow is
    identified with the `qa` ``mask-method``::

        geedim config --mask-method qa search -c s2-sr --cloudless-portion 60 -s 2022-01-01 -e 2022-01-14 --bbox 24 -34 24.5 -33.5

    Download and cloud/shadow mask a Landsat-8 image, where shadows are excluded from the mask::

        geedim config --no-mask-shadows download -i LANDSAT/LC08/C02/T1_L2/LC08_172083_20220104 --mask --bbox 24.25 -34 24.5 -33.75
    """
    # @formatter:on
    # store commandline configuration (only) in the context object for use by other commands
    for key, val in ctx.params.items():
        if ctx.get_parameter_source(key) == ParameterSource.COMMANDLINE:
            ctx.obj.cloud_kwargs[key] = val


cli.add_command(config)


# search command
@click.command(cls=ChainedCommand)
@click.option(
    '-c', '--collection', type=click.STRING, required=True, callback=_collection_cb,
    help=f'Earth Engine image collection to search. geedim or EE collection names can be used.'
)
@click.option('-s', '--start-date', type=click.DateTime(), required=True, help='Start date (UTC).')
@click.option('-e', '--end-date', type=click.DateTime(), required=True, help='End date (UTC).')
@bbox_option
@click.option(
    '-r', '--region', type=click.Path(exists=True, dir_okay=False, allow_dash=True), callback=_region_cb,
    help='Region defined by geojson polygon or raster file. Use "-" to read geojson from stdin.  One of '
    ':option:`--bbox` or :option:`--region` is required.'
)
@click.option(
    '-fp', '--fill-portion', type=click.FloatRange(min=0, max=100), default=0, show_default=True,
    help='Lower limit on the filled portion of the region (%).'
)
@click.option(
    '-cp', '--cloudless-portion', type=click.FloatRange(min=0, max=100), default=0, show_default=True,
    help='Lower limit on the cloud/shadow free portion of the region (%).  If cloud/shadow masking is not supported '
    'for the specified collection, :option:`--cloudless-portion` will operate like :option:`--fill-portion`.'
)
@click.option(
    '-op', '--output', type=click.Path(exists=False, dir_okay=False, writable=True), default=None,
    help='JSON file to write search results to.'
)
@click.pass_obj
def search(obj, collection, start_date, end_date, bbox, region, fill_portion, cloudless_portion, output):
    # @formatter:off
    """
    Search for images.

    Search a Google Earth Engine image collection for images, filtering by date, region and portion of
    filled pixels.  Cloud/shadow-free (cloudless) portion filtering is supported on the following
    collections:
    \b

        ===========  ===========================
        geedim name  EE name
        ===========  ===========================
        l4-c2-l2     LANDSAT/LT04/C02/T1_L2
        l5-c2-l2     LANDSAT/LT05/C02/T1_L2
        l7-c2-l2     LANDSAT/LE07/C02/T1_L2
        l8-c2-l2     LANDSAT/LC08/C02/T1_L2
        l9-c2-l2     LANDSAT/LC09/C02/T1_L2
        s2-toa       COPERNICUS/S2
        s2-sr        COPERNICUS/S2_SR
        s2-toa-hm    COPERNICUS/S2_HARMONIZED
        s2-sr-hm     COPERNICUS/S2_SR_HARMONIZED
        ===========  ===========================

    A search region must be specified with either the ``--bbox`` or ``--region`` option.

    Note that filled/cloudless portions are not taken from the granule metadata, but are calculated as portions of the
    specified search region for improved accuracy.
    \b

    Examples
    --------

    Search the GEDI canopy height collection for images with a filled portion of at least 0.5%::

        geedim search -c LARSE/GEDI/GEDI02_A_002_MONTHLY -s 2021-12-01 -e 2022-02-01 --bbox 23 -34 23.2 -33.8 --fill-portion 0.5

    Search the Landsat-9 collection for images with a cloud/shadow free portion of at least 50%::

        geedim search -c l9-c2-l2 -s 2022-01-01 -e 2022-03-01 --bbox 23 -34 23.2 -33.8 --cloudless-portion 50
    """
    # @formatter:on
    if not obj.region:
        raise click.BadOptionUsage('region', 'Either pass --region or --bbox')

    # create collection wrapper and search
    gd_collection = MaskedCollection.from_name(collection)
    logger.info('')
    label = (
        f'Searching for {collection} images between {start_date.strftime("%Y-%m-%d")} and '
        f'{end_date.strftime("%Y-%m-%d")}: '
    )
    with Spinner(label=label, leave=' '):
        gd_collection = gd_collection.search(
            start_date, end_date, obj.region, fill_portion=fill_portion, cloudless_portion=cloudless_portion,
            **obj.cloud_kwargs
        )
        num_images = len(gd_collection.properties)  # retrieve search result properties from EE

    if num_images == 0:
        logger.info('No images found\n')
    else:
        obj.image_list += list(gd_collection.properties.keys())  # store image ids for chained commands
        logger.info(f'{len(gd_collection.properties)} images found\n')
        logger.info(f'Image property descriptions:\n\n{gd_collection.schema_table}\n')
        logger.info(f'Search Results:\n\n{gd_collection.properties_table}')

    # write results to file
    if output is not None:
        output = pathlib.Path(output)
        with open(output, 'w', encoding='utf8', newline='') as f:
            json.dump(gd_collection.properties, f)


cli.add_command(search)


# download command
@click.command(cls=ChainedCommand)
@click.option('-i', '--id', 'image_id', type=click.STRING, multiple=True, help='Earth Engine image ID(s) to download.')
@bbox_option
@region_option
@click.option(
    '-dd', '--download-dir', type=click.Path(exists=True, file_okay=False, dir_okay=True, writable=True), default=None,
    show_default='current working directory.', help='Directory to download image file(s) into.'
)
@crs_option
@scale_option
@dtype_option
@mask_option
@resampling_option
@scale_offset_option
@click.option('-o', '--overwrite', is_flag=True, default=False, help='Overwrite the destination file if it exists.')
@click.pass_obj
def download(obj, image_id, bbox, region, download_dir, mask, overwrite, **kwargs):
    # @formatter:off
    """
    Download image(s).

    Download Earth Engine image(s) to GeoTIFF file(s), allowing optional region of interest, and other image
    formatting options to be specified.  Images larger than the `Earth Engine size limit
    <https://developers.google.com/earth-engine/apidocs/ee-image-getdownloadurl>`_ are split and downloaded as separate
    tiles, then re-assembled into a single GeoTIFF.  Downloaded image files are populated with metadata from the Earth
    Engine image and STAC.

    This command can be chained after the ``composite`` command, to download the composite image.  It can also be
    chained after the ``search`` command, in which case the search result images will be downloaded, without the need
    to specify image IDs with ``--id``, or region with ``--bbox`` / ``--region``.

    The following auxiliary bands are added to images from collections with support for cloud/shadow masking:
    \b

        ==============  =========================================
        Band name       Description
        ==============  =========================================
        FILL_MASK       Mask of filled (valid) pixels.
        SHADOW_MASK     Mask of cloud shadows.
        CLOUD_MASK      Mask of clouds.
        CLOUDLESS_MASK  Mask of filled & cloud/shadow-free pixels.
        CLOUD_DIST      Distance to nearest cloud (m).
        ==============  =========================================

    Images from other collections, will contain the FILL_MASK band only.

    If neither ``--bbox`` or ``--region`` are specified, the entire image granule
    will be downloaded.

    Image filenames are derived from their Earth Engine ID.
    \b

    Examples
    --------

    Download a region of a Landsat-9 image, applying the cloud/shadow mask and converting to uint16::

        geedim download -i LANDSAT/LC09/C02/T1_L2/LC09_173083_20220308 --mask --bbox 21.6 -33.5 21.7 -33.4 --dtype uint16

    Download the results of a MODIS NBAR search, specifying a CRS and scale to reproject to::

        geedim search -c MODIS/006/MCD43A4 -s 2022-01-01 -e 2022-01-03 --bbox 23 -34 24 -33 download --crs EPSG:3857 --scale 500
    """
    # @formatter:on
    logger.info('\nDownloading:\n')
    download_dir = download_dir or os.getcwd()
    image_list = _prepare_image_list(obj, mask=mask)
    for im in image_list:
        filename = pathlib.Path(download_dir).joinpath(im.name + '.tif')
        im.download(filename, overwrite=overwrite, region=obj.region, **kwargs)


cli.add_command(download)


# export command
@click.command(cls=ChainedCommand)
@click.option('-i', '--id', 'image_id', type=click.STRING, multiple=True, help='Earth Engine image ID(s) to export.')
@bbox_option
@region_option
@click.option(
    '-df', '--drive-folder', type=click.STRING, default=None, show_default='root folder.',
    help='Google Drive folder to export image(s) to.'
)
@crs_option
@scale_option
@dtype_option
@mask_option
@resampling_option
@scale_offset_option
@click.option(
    '-w/-nw', '--wait/--no-wait', default=True, show_default=True, help='Whether to wait for the export to complete.'
)
@click.pass_obj
def export(obj, image_id, bbox, region, drive_folder, mask, wait, **kwargs):
    # @formatter:off
    """
    Export image(s) to Google Drive.

    Export Earth Engine image(s) to GeoTIFF file(s) on Google Drive, allowing optional region of interest,
    and other image formatting options to be specified.

    This command can be chained after the ``composite`` command, to export the composite image.  It can also be
    chained after the ``search`` command, in which case the search result images will be exported, without the need
    to specify image IDs with ``--id``, or region with ``--bbox`` / ``--region``.

    The following auxiliary bands are added to images from collections with support for cloud/shadow masking:
    \b

        ==============  =========================================
        Band name       Description
        ==============  =========================================
        FILL_MASK       Mask of filled (valid) pixels.
        SHADOW_MASK     Mask of cloud shadows.
        CLOUD_MASK      Mask of clouds.
        CLOUDLESS_MASK  Mask of filled & cloud/shadow-free pixels.
        CLOUD_DIST      Distance to nearest cloud.
        ==============  =========================================

    Images from other collections, will contain the FILL_MASK band only.

    If neither ``--bbox`` or ``--region`` are specified, the entire image granule
    will be downloaded.

    Image filenames are derived from their Earth Engine ID.
    \b

    Examples
    --------

    Export a region of a Landsat-9 image, applying the cloud/shadow mask and converting to uint16::

        geedim export -i LANDSAT/LC09/C02/T1_L2/LC09_173083_20220308 --mask --bbox 21.6 -33.5 21.7 -33.4 --dtype uint16

    Export the results of a MODIS NBAR search to the 'geedim' folder, specifying a CRS and scale to reproject to::

        geedim search -c MODIS/006/MCD43A4 -s 2022-01-01 -e 2022-01-03 --bbox 23 -34 24 -33 export --crs EPSG:3857 --scale 500 -df geedim
    """
    # @formatter:on
    logger.info('\nExporting:\n')
    image_list = _prepare_image_list(obj, mask=mask)
    export_tasks = []
    for im in image_list:
        task = im.export(im.name, folder=drive_folder, wait=False, region=obj.region, **kwargs)
        export_tasks.append(task)
        logger.info(f'Started {im.name}') if not wait else None

    if wait:
        for task in export_tasks:
            BaseImage.monitor_export(task)


cli.add_command(export)


# composite command
@click.command(cls=ChainedCommand)
@click.option(
    '-i', '--id', 'image_id', type=click.STRING, multiple=True, help='Earth Engine image ID(s) to include in composite.'
)
@click.option(
    '-cm', '--method', 'method', type=click.Choice([cm.value for cm in CompositeMethod], case_sensitive=False),
    default=None, callback=_comp_method_cb,
    show_default='`q-mosaic` for cloud/shadow mask supported collections, `mosaic` otherwise.',
    help='Compositing method to use.'
)
@click.option(
    '-m/-nm', '--mask/--no-mask', default=True, show_default=True,
    help='Whether to apply cloud/shadow (or fill) masks to input images before compositing.  Fill masks are used for '
    'images without support for cloud/shadow masking.'
)
@click.option(
    '-rs', '--resampling', type=click.Choice([rm.value for rm in ResamplingMethod], case_sensitive=True),
    default=BaseImage._default_resampling.value, callback=_resampling_method_cb, show_default=True,
    help='Resample images with this method before compositing.'
)
@click.option(
    '-b', '--bbox', type=click.FLOAT, nargs=4, default=None, callback=_bbox_cb,
    help='Give preference to images with the highest cloudless (or filled) portion inside this bounding box (left, '
    'bottom, right, top).  Valid for `mosaic` and `q-mosaic` compositing :option:`--method`.'
)
@click.option(
    '-r', '--region', type=click.Path(exists=True, dir_okay=False, allow_dash=True), default=None, callback=_region_cb,
    help='Give preference to images with the highest cloudless (or filled) portion inside this geojson polygon, '
    'or raster file, region.  Valid for `mosaic` and `q-mosaic` compositing :option:`--method`.'
)
@click.option(
    '-d', '--date', type=click.DateTime(),
    help='Give preference to images closest to this date (UTC).  Valid for `mosaic` and `q-mosaic` compositing '
    ':option:`--method`.'
)
@click.pass_obj
def composite(obj, image_id, mask, method, resampling, bbox, region, date):
    # @formatter:off
    """
    Create a composite image.

    Create cloud/shadow-free and other composite image(s) from specified input images.

    ``download`` or ``export`` commands can be chained after the ``composite`` command to download/export the composite
    image. ``composite`` can also be chained after ``search``, ``download`` or ``composite``, in which case it will
    composite the output image(s) from the previous command.  Images specified with the ``--id`` option will be added
    to any existing chained images i.e. images output from previous chained commands.

    In general, input images should belong to the same collection.  In the specific case of Landsat, images from
    spectrally compatible collections can be combined i.e. Landsat-4 with Landsat-5, and Landsat-8 with Landsat-9.

    ``--method`` specifies the method for finding a composite pixel from the stack of corresponding input image
    pixels.  The following options are available:
    \b

        ==========  ========================================================
        Method      Description
        ==========  ========================================================
        `q-mosaic`  | Use the unmasked pixel with the highest cloud distance
                    | (i.e. distance to nearest cloud).  Where more than one
                    | pixel has the same cloud distance, the first one in the
                    | stack is selected.
        `mosaic`    Use the first unmasked pixel in the stack.
        `medoid`    | Use the medoid of the unmasked pixels i.e. the pixels
                    | of the image with minimum summed difference (across
                    | bands) to the median over the input images.
                    | Maintains relationship between bands.
        `median`    Use the median of the unmasked pixels.
        `mode`      Use the mode of the unmasked pixels.
        `mean`      Use the mean of the unmasked pixels.
        ==========  ========================================================

    For the `mosaic` and `q-mosaic` methods there are three ways of ordering (i.e. prioritising) images in the stack:
    \b

        * | If ``--date`` is specified, images are sorted by the absolute
          | difference of their capture time from this date.
        * | If either ``--region`` or ``--bbox`` are specified, images are sorted
          | by their cloudless/filled portion inside this region.
        * | If none of the above options are specified, images are sorted by their
          | capture time.

    By default, input images are masked before compositing.  This means that only cloud/shadow-free (or filled) pixels
    are used to make the composite.  You can turn off this behaviour with the ``--no-mask`` option.
    \b

    Examples
    --------
    Composite two Landsat-7 images using the default options and download the result::

        geedim composite -i LANDSAT/LE07/C02/T1_L2/LE07_173083_20100203 -i LANDSAT/LE07/C02/T1_L2/LE07_173083_20100219 download --bbox 22 -33.1 22.1 -33 --crs EPSG:3857 --scale 30

    Create and download a composite of a year of GEDI canopy height data, by chaining with ``search``::

        geedim search -c LARSE/GEDI/GEDI02_A_002_MONTHLY -s 2021-01-01 -e 2022-01-01 --bbox 23 -34 23.1 -33.9 --fill-portion 0.1 composite -cm mosaic download --crs EPSG:3857 --scale 25

    Create and download a cloud/shadow-free composite of Sentinel-2 SR images, by chaining with ``search``::

        geedim search -c s2-sr -s 2021-01-12 -e 2021-01-23 --bbox 23 -33.5 23.1 -33.4 composite -cm q-mosaic download --crs EPSG:3857 --scale 10
    """
    # @formatter:on

    # get image ids from command line or chained search command
    if len(obj.image_list) == 0:
        raise click.BadOptionUsage('image_id', 'Either pass --id, or chain this command with a successful ``search``')

    gd_collection = MaskedCollection.from_list(obj.image_list)
    obj.image_list = [
        gd_collection.composite(
            method=method, mask=mask, resampling=resampling, region=obj.region, date=date, **obj.cloud_kwargs
        )
    ]


cli.add_command(composite)

##
