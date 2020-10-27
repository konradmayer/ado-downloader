#!/usr/bin/env python

"""
cds_downloader.py:
Climate Data Store Downloader

TODO:
 - Logging
 - Adapt requirements.txt
 - If config not complete, use criterias from cds_webapi
 - Check configuration
"""

__author__ = "Georg Seyerl"
__license__ = "MIT"
__maintainer__ = "Georg Seyerl"
__status__ = "Development"

import os
import json
import copy
import requests
import itertools
import shutil
import tempfile
import datetime

import operator
from functools import reduce

from multiprocessing import Process
from pathlib import Path

import cdsapi

import logging



class Downloader(object):
    """The :class:`Downloader` class provides common functionality for automated
    climate data download from cds.climate.copernicus.eu

    In order to use the downloader, one has to create a file with user
    credentials `api-how-to <https://cds.climate.copernicus.eu/api-how-to>`_.
    Alternatively, define two environment variables 'CDSAPI_URL' and
    'CDSAPI_KEY' with the user credentials from cds.

    """

    def __init__(self, cds_product, cds_filter, **kwargs):
        """
        Parameters
        ----------
        cds_product : string
            the cds product string
        cds_filter : dict
            the cds filter dictionary


        """
        self.cds_product = cds_product
        self.cds_filter = cds_filter
        self.cds_webapi = requests.get(
            url='https://cds.climate.copernicus.eu/api/v2.ui/resources/{}'.format(cds_product)).json()

        logging.basicConfig(filename="log/cds_downloader.log", encoding='utf-8', format='%(asctime)s %(message)s')
        logging.info('New downloader object initialized')


    @classmethod
    def from_cds(cls, cds_product, cds_filter, **kwargs):
        """
        Create Downloader from cds example

        Parameters
        ----------
        cds_product : string
            the cds product string
        cds_filter : dict
            the cds filter dictionary
        """
        try:
            dct_config = {"cds_product": cds_product,
                          "cds_filter": cds_filter}
            dct_config.update(**kwargs)
            cds_downloader = cls(**dct_config)

            return cds_downloader
        except Exception as e:
            print(e.args)
            raise


    @classmethod
    def from_dict(cls, dct_config):
        """
        Create Downloader from dictionary

        Parameters
        ----------
        dct_config : dict
            a dictionary with keys 'cds_product' and 'cds_filter'
        """
        try:
            cds_downloader = cls(**dct_config)

            return cds_downloader
        except Exception as e:
            print(e.args)
            raise


    @classmethod
    def from_json(cls, json_config_path):
        """
        Create Downloader from json file

        Parameters
        ----------
        json_config_path : string
            path to json config file
        """
        try:
            #Read JSON config file
            with open(json_config_path, 'r') as f:
                cds_downloader = cls(**json.load(f))

            return cds_downloader
        except Exception as e:
            print(e.args)
            raise


    def get_data(self, storage_path, split_keys=None):
        """This method downloads requested data from climate data store.

        Parameters
        ----------
        storage_path : string
            target storage path as string
        split_keys : list-like, optional
            The maximum single data request size depends on the copernicus
            climate data store and is automatically extracted from their
            metadata webapi. If split_keys=None, the method automatically
            chunks the cds request into multiple smaller requests and spawns a
            single process for each of them. Therefore, it extracts all
            list-like objects from the cds_filter (e.g. "year, "month, ...) and
            splits the data into single requests/files.

            By setting split_keys as a list of keys from the cds_filter, one can
            manually control the splitting (e.g. split_keys=["year", "month", "day"])

        Returns
        -------
        processes : list of multiprocessing.Process
            List of download process objects

        Examples
        --------
        Download small data collection with manual split_keys

        >>> from cds_downloader import Downloader
        >>> x = Downloader.from_cds(
        ...         "reanalysis-era5-single-levels",
        ...         {
        ...             "product_type": "reanalysis",
        ...             "format": "grib",
        ...             "variable": ["total_precipitation"],
        ...             "year": ["2020"],
        ...             "month": ["09"],
        ...             "day": ["01", "02", "03"],
        ...             "area": [50.7, 3.6, 42.9, 17.2]
        ...         },
        ...     )
        ...
        >>> x.get_data("/tmp", ["year","month","day"])

        """

        # User Credentials from environment variables
        # 'CDSAPI_URL' and 'CDSAPI_KEY'
        self.cdsapi_client = cdsapi.Client()

        # Create storage path
        Path(storage_path).mkdir(parents=True, exist_ok=True)

        # If necessary, find keys for download chunking
        if split_keys is None:
            self.split_keys = self._get_split_keys()
        else:
            self.split_keys = split_keys

        split_filter = self._expand_by_keys(self.cds_filter, self.split_keys)
        return self._retrieve_files(storage_path, split_filter)


    def update_data(self, storage_path, split_keys,
                    date_until=datetime.datetime.utcnow(), start_from_files=False):
        """This method provides update functionality for climate data collections
        retrieved with :meth:`cds_downloader.Downloader.get_data`

        It uses temporal information from cds metadata webapi and evaluates
        missing data. Redownload latest file in order to avoid missing data.

        Under development, only temporal split_keys allowed:
        split_keys in ["year", "month", "day", "time"]

        Parameters
        ----------
        storage_path : string
            storage path of data collection as string
        split_keys : list of strings
            list of keys in cds_filter
        date_until : datetime.datetime, optional
            update data collection until this date
        start_from_files : boolean, optional
             use first file of sorted file list as start reference date

        """

        # TODO:
        # - split_keys includes non temporal attributes (e.g. variable)

        # User Credentials from environment variables
        # 'CDSAPI_URL' and 'CDSAPI_KEY'
        try:
            self.cdsapi_client = cdsapi.Client()
        except Exception as e:
            logging.error("cdsapi client could not be initialized: \n" + e.args)
            raise("cdsapi client not initialized")

        self.split_keys = split_keys

        path_files = Path(storage_path)

        if path_files.is_dir():
            lst_existing_files = [f for f in path_files.glob("*." + self.cds_filter.get("format", "grib"))]
            lst_existing_files = sorted(lst_existing_files)
        else:
            logging.error("No valid path specified")
            raise("No valid path")

        temporal_filter = self._full_time_filter_from_webapi()

        file_split_keys = [tuple(f.name.rsplit("_")[:len(self.split_keys)]) for f in lst_existing_files]
        all_split_keys = [i for i in itertools.product(
            *[dict(self.cds_filter, **temporal_filter)[k] for k in self.split_keys])
        ]

        # Until present date
        index_present = all_split_keys.index(
            tuple(str(date_until.__getattribute__(k)).zfill(2) for k in self.split_keys if k in temporal_filter.keys())
        )

        # Keep last tuple
        missing_split_keys = [keys for keys in all_split_keys[:index_present+1] if keys not in file_split_keys[:-1]]

        # Exclude dates earlier than date of first file
        if start_from_files:
            index_first = all_split_keys.index(file_split_keys[0])
            missing_split_keys = [keys for keys in all_split_keys[index_first:index_present+1] if keys not in file_split_keys[:-1]]


        # Download new data in temporary folder
        with tempfile.TemporaryDirectory() as path_temp:
            dct_update = [{k:v for k,v in zip(self.split_keys, missing_split_key)} for missing_split_key in missing_split_keys]
            dct_update = [dict(temporal_filter, **upd) for upd in dct_update]
            split_filter = (dict(self.cds_filter, **upd) for upd in dct_update)

            all_processes = self._retrieve_files(path_temp, split_filter)

            lst_new_files = [f for f in Path(path_temp).iterdir()]

            # Move files from tmp folder to storage path
            for f in lst_new_files:
                # Move and overwrite file if necessary
                try:
                    shutil.move(f, path_files.joinpath(f.name))
                    logging.info("Move file from tmp to storage path: " + f)
                except Exception as e:
                    logging.error("Move file from tmp to storage path: " + f)
                    print(e.args)


    def _get_org_keys(self):
        exclude_keys = ["area", "grid"]
        lst_org = [k for k,v in self.cds_filter.items() if isinstance(v, list) and k not in exclude_keys]
        return lst_org


    def _get_request_size(self, lst_keys):
        request_size = reduce(
            operator.mul,
            [len(lst) for lst in [self.cds_filter.get(k, 1) for k in lst_keys]],
            1
        )
        return request_size


    def _get_split_keys(self):
        lst_org = self._get_org_keys()
        lst_ret = list()
        while self._get_request_size(lst_org) > self.cds_webapi["selection_limit"]:
            lst_ret.append(lst_org.pop(0))
        return lst_ret


    def _expand_by_keys(self, dct, lst_keys):
        tmp_dct = copy.deepcopy(dct)
        for value in itertools.product(*[dct.get(key) for key in lst_keys]):
            tmp_dct.update(dict(zip(lst_keys, value)))
            yield tmp_dct


    def _retrieve_file(self, cds_product, cds_filter, file_name):
        logging.info('Start download process ' + file_name)
        self.cdsapi_client.retrieve(
            cds_product,
            cds_filter,
            file_name
        )
        logging.info('Finish download process ' + file_name)


    def _retrieve_files(self, storage_path, split_filter):
        all_processes = []
        for cds_filter in split_filter:
            file_path = '_'.join([cds_filter.get(k) for k in self.split_keys] or ["all"]) + \
                        "_" + self.cds_product + \
                        "." + cds_filter.get("format", "grib")

            p = Process(
                target=self._retrieve_file,
                args=(self.cds_product,
                      cds_filter,
                      os.path.join(storage_path, file_path)
                )
            )
            p.start()
            all_processes.append(p)

        for p in all_processes:
            p.join()

        return all_processes


    def _full_time_filter_from_webapi(self, filter_names=["year", "month", "day", "time"]):
        return {
            form_ele.get("name"): form_ele.get("details", {}).get("values", None)
            for form_ele in self.cds_webapi.get("form")
            if form_ele.get("name") in filter_names
        }



