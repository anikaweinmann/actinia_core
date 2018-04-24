# -*- coding: utf-8 -*-
"""
Asynchronous merging of several mapsets into a single one
"""
import pickle
import os
from flask import jsonify, make_response

from .async_persistent_processing import AsyncPersistentProcessing
from .async_resource_base import AsyncEphemeralResourceBase
from .common.redis_interface import enqueue_job
from .common.exceptions import AsyncProcessError, AsyncProcessTermination

__license__ = "GPLv3"
__author__     = "Sören Gebbert"
__copyright__  = "Copyright 2016, Sören Gebbert"
__maintainer__ = "Sören Gebbert"
__email__      = "soerengebbert@googlemail.com"


class AsyncPersistentMapsetMergingResource(AsyncEphemeralResourceBase):

    def __init__(self):
        AsyncEphemeralResourceBase.__init__(self)

    def post(self, location_name, mapset_name):
        """Merge several existing mapsets into a single one.
        All mapsets that should be merged and the target mapset will be locked
        for the processing.

        Args:
            location_name (str): The name of the location
            target_mapset_name (str): The name of the target mapset, into other mapsets should be merged

        Process arguments must be provided as JSON document in the POST request::

            ["mapset_A", "mapset_B", "mapset_C", "mapset_D", ...]

        Returns:
            flask.Response:
            The HTTP status and a JSON document that includes the
            status URL of the task that must be polled for updates.

        Example::

            {
              "HTTP code": 200,
              "Messages": "Resource accepted",
              "Resource id": "resource_id-985164c9-1db9-49cf-b2c4-3e8e48500e31",
              "Status": "accepted",
              "URLs": {
                "Resources": [],
                "Status": "http://104.155.60.87/resources/soeren/resource_id-985164c9-1db9-49cf-b2c4-3e8e48500e31"
              },
              "User id": "soeren"
            }


        """
        # Preprocess the post call
        rdc = self.preprocess(has_json=True, location_name=location_name, mapset_name=mapset_name)

        if rdc:
            enqueue_job(self.job_timeout, start_job, rdc)

        html_code, response_model = pickle.loads(self.response_data)
        return make_response(jsonify(response_model), html_code)


def start_job(*args):
    processing = AsyncPersistentMapsetMerging(*args)
    processing.run()


class AsyncPersistentMapsetMerging(AsyncPersistentProcessing):
    """Processing of grass modules in a temporary or original mapset.

    This class is designed to run GRASS modules that are specified in a process chain
    in a temporary mapset that later on is copied into the original location.

    If the processing should be run in an existing mapset, the original mapset will be
    used for processing.
    """
    def __init__(self, rdc):
        """Constructor

        Args:
            rdc (ResourceDataContainer): The data container that contains all required variables for processing

        """

        AsyncPersistentProcessing.__init__(self, rdc)
        self.lock_ids = {}                    # This dict holds the lock ids of all locked mapsets

    def _check_lock_mapset(self, mapset_name):
        """Check if the mapset exists and lock it

        If the mapset is a global mapset and Error will be raised.

        The duration of the lock is process_time_limit * process_num_limit
        and should be extended if needed.

        Only mapsets of the user database are locked.

        Unlock the mapset after the processing finished.
        """
        # check if the resource is accessible
        mapset_exists = self._check_mapset(mapset_name)

        if mapset_exists is False:
            raise AsyncProcessError("Mapset <%s> does not exist and can not be locked."%mapset_name)

        # Finally lock the mapset for the time that the user can allocate at maximum
        lock_id = "%s/%s/%s"%(self.user_group, self.location_name, mapset_name)
        ret = self.lock_interface.lock(resource_id=lock_id,
                                       expiration=self.process_time_limit*self.process_num_limit)

        if ret == 0:
            raise AsyncProcessError("Unable to lock mapset <%s>, resource is already locked"%mapset_name)
        self.message_logger.info("Mapset <%s> locked"%mapset_name)

        # if we manage to come here, the lock was correctly set, hence store the lock id for later unlocking
        self.lock_ids[lock_id] = mapset_name

    def _check_lock_source_mapsets(self, source_mapsets):
        """Check and lock the source mapsets from the merging list

        Args:
            source_mapsets: A list of source mapsets that should be checked
                            and locked

        Raises:
            This method will raise an AsyncProcessError

        """
        # Expect a list of mapset names
        if len(source_mapsets) == 0:
            raise AsyncProcessError("Empty source mapset list.")

        # Check and lock the mapsets
        for mapset in source_mapsets:
            self._check_lock_mapset(mapset)

    def _merge_mapsets(self):
        """Merge mapsets in a target mapset

            - Check the target mapset and lock it for the maximum time
              a user can consume -> process_num_limit*process_time_limit
            - Check and lock all source mapsets with the same scheme
            - Copy each source mapset into the target mapset
            - Extend the locks each copy run
            - Cleanup and unlock the mapsets

        """
        # Lock the target mapset
        self._check_lock_mapset(self.target_mapset_name)
        # Lock the source mapsets
        self._check_lock_source_mapsets(self.request_data)

        step = 1
        steps = len(self.request_data)

        mapsets_to_merge = []

        # Copy each mapset into the target
        for lock_id in self.lock_ids:
            # Check for termination requests
            if self.resource_logger.get_termination(self.user_id, self.resource_id) is True:
                raise AsyncProcessTermination("Mapset merging was terminated "
                                              "by user request at setp %i of %i"%(step, steps))

            mapset_name = self.lock_ids[lock_id]
            mapsets_to_merge.append(mapset_name)

            for lock_id in self.lock_ids:
                # Extent the lock for each process by max processing time * 2
                ret = self.lock_interface.extend(resource_id=lock_id,
                                                 expiration=self.process_time_limit * 2)
                if ret == 0:
                    raise AsyncProcessError("Unable to extend lock for mapset <%s>"%mapset_name)

            message = "Step %i of %i: Copy content from source " \
                      "mapset <%s> into target mapset <%s>"%(step, steps, mapset_name,
                                                             self.target_mapset_name)
            self._send_resource_update(message)
            self.message_logger.info(message)

            # Copy the source mapset into the target mapset
            if mapset_name != self.target_mapset_name:
                step += 1
                self._merge_mapset_into_target(mapset_name, self.target_mapset_name)

    def _execute(self):
        """The _execute() function that does all the magic.

        Overwrite this function in subclasses.

            - Check the target mapset and lock it for the maximum time
              a user can consume -> process_num_limit*process_time_limit
            - Check and lock all source mapsets with the same scheme
            - Copy each source mapset into the target mapset
            - Extend the locks each copy run
            - Cleanup and unlock the mapsets

        """
        # Setup the user credentials and logger
        self._setup()
        self._merge_mapsets()

    def _final_cleanup(self):
        """Final cleanup called in the run function at the very end of processing
        """
        # Clean up and remove the temporary gisdbase
        # Unlock mapsets
        AsyncPersistentProcessing._final_cleanup(self)
        # Unlock the mapsets
        for lock_id in self.lock_ids:
            self.lock_interface.unlock(lock_id)
