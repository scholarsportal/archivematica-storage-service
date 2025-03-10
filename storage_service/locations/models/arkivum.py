from __future__ import absolute_import

# stdlib, alphabetical
import json
import logging
from lxml import etree
import os
import requests
import six.moves.urllib.request
import six.moves.urllib.parse
import six.moves.urllib.error

# Core Django, alphabetical
from django.conf import settings
import django.core.mail
from django.contrib.auth import get_user_model
from django.db import models
from django.utils.translation import ugettext_lazy as _

# Third party dependencies, alphabetical
import dateutil.parser
import scandir

# This project, alphabetical
from common import utils

# This module, alphabetical
from . import StorageException
from .location import Location
from .package import Package

LOGGER = logging.getLogger(__name__)

VERIFY = not settings.INSECURE_SKIP_VERIFY


class Arkivum(models.Model):
    space = models.OneToOneField("Space", to_field="uuid", on_delete=models.CASCADE)

    host = models.CharField(
        max_length=256,
        verbose_name=_("Host"),
        help_text=_(
            "Hostname of the Arkivum web instance. Eg. arkivum.example.com:8443"
        ),
    )
    # Optionally be able to rsync
    remote_user = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        verbose_name=_("Remote user"),
        help_text=_(
            "Optional: Username on the remote machine accessible via passwordless ssh."
        ),
    )
    remote_name = models.CharField(
        max_length=256,
        null=True,
        blank=True,
        verbose_name=_("Remote name"),
        help_text=_("Optional: Name or IP of the remote machine."),
    )

    class Meta:
        verbose_name = _("Arkivum")
        app_label = "locations"

    ALLOWED_LOCATION_PURPOSE = [Location.AIP_STORAGE]

    def browse(self, path):
        # Support browse so that the Location select works
        if self.remote_user and self.remote_name:
            path = os.path.join(path, "")
            ssh_path = "{}@{}:{}".format(
                self.remote_user, self.remote_name, utils.coerce_str(path)
            )
            return self.space.browse_rsync(ssh_path)
        else:
            return self.space.browse_local(path)

    def delete_path(self, delete_path):
        # Can this be done by just deleting the file on disk?
        # TODO folders
        url = "https://" + self.host + "/files/" + delete_path
        LOGGER.info("URL: %s", url)
        response = requests.delete(url, verify=VERIFY)
        LOGGER.info(
            "Response: %s, Response text: %s", response.status_code, response.text
        )
        if response.status_code != 204:
            raise StorageException("Unable to delete %s", delete_path)

    def move_to_storage_service(self, src_path, dest_path, dest_space):
        """ Moves src_path to dest_space.staging_path/dest_path. """
        # Get from watched dir
        if self.remote_user and self.remote_name:
            # Rsync from remote
            src_path = "{user}@{host}:{path}".format(
                user=self.remote_user, host=self.remote_name, path=src_path
            )
        self.space.create_local_directory(dest_path)
        self.space.move_rsync(src_path, dest_path)

    def move_from_storage_service(self, source_path, destination_path, package=None):
        """ Moves self.staging_path/src_path to dest_path. """
        try_mv_local = False
        # Rsync to Arkivum watched directory
        if self.remote_user and self.remote_name:
            self.space.create_rsync_directory(
                destination_path, self.remote_user, self.remote_name
            )
            rsync_dest = "{}@{}:{}".format(
                self.remote_user, self.remote_name, utils.coerce_str(destination_path)
            )
        else:
            rsync_dest = destination_path
            self.space.create_local_directory(destination_path)
            try_mv_local = True
        self.space.move_rsync(source_path, rsync_dest, try_mv_local=try_mv_local)

    def post_move_from_storage_service(self, staging_path, destination_path, package):
        """ POST to Arkivum with information about the newly stored Package. """
        if package is None:
            return

        relative_path = os.path.relpath(destination_path, self.space.path)
        if package.is_compressed:  # Single-file package
            url = "https://" + self.host + "/api/2/files/release/" + relative_path
            headers = {"Content-Type": "application/json"}

            # Get size, checksum, and checksum algorithm from pointer file;
            # infer compression algorithm from filename.
            root = etree.parse(package.full_pointer_file_path)
            fixity_elem = root.find(".//premis:fixity", namespaces=utils.NSMAP)
            algorithm = fixity_elem.findtext(
                "premis:messageDigestAlgorithm", namespaces=utils.NSMAP
            )
            checksum = fixity_elem.findtext(
                "premis:messageDigest", namespaces=utils.NSMAP
            )
            size = fixity_elem.findtext("premis:size", namespaces=utils.NSMAP)
            payload = {
                "size": size,
                "checksum": checksum,
                "checksumAlgorithm": algorithm,
                "compressionAlgorithm": os.path.splitext(package.current_path)[1],
            }
            payload = json.dumps(payload)
            files = None
        else:  # uncompressed bag
            url = "https://" + self.host + "/api/3/ingest-manifest/release"
            headers = None
            # FIXME Destination path has to exclude mount path, but what is part of the mounth path? Let's pretend it's the Space path
            payload = {"bagitPath": os.path.join("/", relative_path)}
            files = {"": ("", "")}

        LOGGER.debug(
            "POST URL: %s; Header: %s; Payload: %s; Files: %s",
            url,
            headers,
            payload,
            files,
        )
        try:
            response = requests.post(
                url, headers=headers, data=payload, files=files, verify=VERIFY
            )
        except requests.exceptions.ConnectionError:
            LOGGER.exception("Error in connection for POST to %s", url)
            raise StorageException(
                _("Error in connection for POST to %(url)s"), {"url": url}
            )

        LOGGER.debug(
            "Response: %s, Response text: %s", response.status_code, response.text
        )
        if response.status_code not in (requests.codes.ok, requests.codes.accepted):
            LOGGER.warning(
                "Arkivum responded with %s: %s", response.status_code, response.text
            )
            raise StorageException(
                _("Unable to notify Arkivum of %(package)s"), {"package": package}
            )
        # Response has request ID for polling status
        try:
            response_json = response.json()
        except json.JSONDecodeError:
            raise StorageException(
                _("Could not get request ID from Arkivum's response %(response)s"),
                {"response": response.text},
            )

        # Store request ID in misc_attributes
        request_id = response_json["id"]
        package.misc_attributes.update({"arkivum_identifier": request_id})
        package.save()

    def _get_package_info(self, package):
        """
        Return status and file info for a package in Arkivum.
        """
        # If no request ID, try POSTing to Arkivum again
        if "arkivum_identifier" not in package.misc_attributes:
            # Get local copy
            local_path = package.fetch_local_path()
            self.post_move_from_storage_service(local_path, package.full_path, package)
        # If still no request ID, cannot check status
        if "arkivum_identifier" not in package.misc_attributes:
            msg = _("Unable to contact Arkivum")
            LOGGER.warning(msg)
            return {"error": True, "error_message": msg}

        # Ask Arkivum for replication status
        if package.is_compressed:
            url = (
                "https://"
                + self.host
                + "/api/2/files/release/"
                + package.misc_attributes["arkivum_identifier"]
            )
        else:
            url = (
                "https://"
                + self.host
                + "/api/3/ingest-manifest/status/"
                + package.misc_attributes["arkivum_identifier"]
            )

        LOGGER.info("URL: %s", url)
        try:
            response = requests.get(url, verify=VERIFY)
        except Exception:
            msg = _("Error fetching package status")
            LOGGER.warning(msg, exc_info=True)
            return {"error": True, "error_message": msg}
        LOGGER.info(
            "Response: %s, Response text: %s", response.status_code, response.text
        )
        if response.status_code != 200:
            msg = _("Response from Arkivum server was %(response)s") % {
                "response": response
            }
            LOGGER.warning(msg)
            return {"error": True, "error_message": msg}

        try:
            response_json = response.json()
        except ValueError:
            msg = _("JSON could not be parsed from package info")
            LOGGER.warning(msg)
            return {"error": True, "error_message": msg}
        return response_json

    def _get_baginfo_txt_info(self, package):
        """
        Return information for the file bag-info.txt from a package in Arkivum.
        To be used as a fallback to obtain replication status of an
        uncompressed package when it is not returned by the
        ingest-manifest/status call in _get_package_info()
        """
        location = package.current_location
        url = (
            "https://"
            + self.host
            + "/api/2/files/fileInfo/"
            + location.relative_path.strip("/")
            + "/"
            + package.current_path.strip("/")
            + "/bag-info.txt"
        )
        LOGGER.info("URL: %s", url)

        try:
            response = requests.get(url, verify=VERIFY)
        except Exception:
            msg = _("Error fetching file info")
            LOGGER.warning(msg, exc_info=True)
            return {"error": True, "error_message": msg}
        LOGGER.info(
            "Response: %s, Response text: %s", response.status_code, response.text
        )
        if response.status_code != 200:
            msg = _("Response from Arkivum server was %(response)s") % {
                "response": response
            }
            LOGGER.warning(msg)
            return {"error": True, "error_message": msg}

        try:
            response_json = response.json()
        except ValueError:
            msg = _("JSON could not be parsed from file info")
            LOGGER.warning(msg)
            return {"error": True, "error_message": msg}
        return response_json

    def update_package_status(self, package):
        LOGGER.info("Package status: %s", package.status)
        response_json = self._get_package_info(package)
        if response_json.get("error"):
            return (None, response_json["error_message"])
        if package.is_compressed:
            # Look for ['fileInformation']['replicationState'] == 'green'
            replication = response_json.get("fileInformation", {}).get(
                "replicationState", ""
            )
        else:  # uncompressed bag
            # Look for ['replicationState'] == 'green'
            replication = response_json.get("replicationState")
            # If response to _get_package_info() didn't contain replicationState
            # try with _get_baginfo_txt_info()
            if replication is None:
                response_json = self._get_baginfo_txt_info(package)
                if response_json.get("error"):
                    return (None, response_json["error_message"])
                replication = response_json.get("replicationState", "")
        if replication.lower() == "green":
            # Set status to UPLOADED
            package.status = Package.UPLOADED
            package.save()
        LOGGER.info("Package status: %s", package.status)
        return (package.status, _("Replication status: ") + replication)

    def is_file_local(self, package, path=None, email_nonlocal=False):
        """
        Check if (a file in) this package is locally available.

        :param package: Package object that contains the file
        :param str path: Relative path to the file inside the package to check. If None, checks the whole package.n
        :param bool email_nonlocal: True if it should email superusers when the file is not cached by Arkivum.
        :return: True if file is locally available, False if not, None on error.
        """
        LOGGER.debug(
            "Checking if file %s in package %s is local (email if not cached: %s)",
            path,
            package,
            email_nonlocal,
        )
        if package.is_compressed:
            package_info = self._get_package_info(package)
            if package_info.get("error"):
                return None
            # Look for ['fileInformation']['local'] == True
            package_info = package_info["fileInformation"]
        else:  # uncompressed
            if path:
                url_path = (
                    package.current_location.relative_path
                    + "/"
                    + package.current_path
                    + "/"
                    + path
                )
                url_path = six.moves.urllib.parse.quote_plus(url_path, safe="/")
                url = "https://" + self.host + "/api/2/files/fileInfo/" + url_path
                LOGGER.info("URL: %s", url)
                try:
                    response = requests.get(url, verify=VERIFY)
                except Exception:
                    LOGGER.warning("Error fetching file information", exc_info=True)
                    return None
                LOGGER.info(
                    "Response: %s, Response text: %s",
                    response.status_code,
                    response.text,
                )
                if response.status_code != 200:
                    LOGGER.warning("Response from Arkivum server was %s", response)
                    return None
                try:
                    package_info = response.json()
                except ValueError:
                    LOGGER.warning("JSON could not be parsed from package info")
                    return None
            else:
                # TODO Implement checking all files in an uncompressed package
                # TODO This may be available from _get_package_info in future
                package_info = self._get_package_info(package)
                if package_info.get("error"):
                    return None
                if "local" not in package_info:
                    LOGGER.warning(
                        "Cannot determine if uncompressed package is locally available! Checking single file."
                    )
                    # Pick a random file and check the locality of it.
                    # WARNING assumes the package is local/not local as a unit.
                    for dirpath, dirs, files in scandir.walk(package.full_path):
                        if files:
                            file_path = os.path.join(dirpath, files[0])
                            break
                    file_path = os.path.relpath(file_path, package.full_path)
                    return self.is_file_local(package, file_path, email_nonlocal)
        LOGGER.debug("File info local: %s", package_info.get("local"))
        if package_info.get("local"):
            return True
        else:
            if email_nonlocal:
                if path:
                    item = _("File %(path)s in package %(package)s") % {
                        "path": path,
                        "package": package,
                    }
                else:
                    item = _("Package %(package)s") % {"package": package}
                message = _(
                    "%(item)s with Arkivum ID of %(package_id)s has been requested but is not available in the Arkivum cache."
                ) % {"item": item, "package_id": package_info.get("id")}

                django.core.mail.send_mail(
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=get_user_model()
                    .objects.filter(is_superuser=True, is_active=True)
                    .distinct()
                    .values_list("email", flat=True),
                    subject=_("Arkivum file not locally available"),
                    message=message,
                )
            return False

    def check_package_fixity(self, package):
        """
        Check fixity for package stored in this space. See Package.check_fixity for detailed description.

        Returns a tuple containing (success, [errors], message).
        Success will be True or False if the verification succeeds or fails, and None if the scan could not start (for instance, if this package fixity is Scheduled).
        [errors] will be a list of zero or more dicts with {'reason': 'string describing the problem', 'filepath': 'relative path to file'}
        message will be a human-readable string explaining the report; it will be an empty string for successful scans.
        timestamp will be the ISO-formated date the last fixity check was performed, or None on error.

        :return: Tuple of (success, [errors], message, timestamp)
        """
        if package.is_compressed:
            raise NotImplementedError(
                "Arkivum does not implement fixity for compressed packages"
            )
        package_info = self._get_package_info(package)
        if package_info.get("error"):
            return (False, [], package_info["error_message"], None)

        # Looking for ['status'] == "Failed", "Completed" or "Scheduled"
        # Looking for ['failures'] == [] or [{"reason": .., "filepath": ...}]
        errors = package_info.get("failures", [])
        replication = package_info.get("replicationState")
        if package_info["status"] == "Completed" and replication == "green":
            success = True
            message = ""
        elif package_info["status"] == "Scheduled" or replication == "amber":
            success = None
            message = _("Arkivum fixity check in progress")
        elif len(errors) > 1:  # Failed, multiple errors
            success = False
            message = _("invalid bag")
        else:  # Failed, only one error
            success = False
            message = errors[0]["reason"]
        timestamp = package_info.get("fixityLastChecked")
        if timestamp:
            timestamp = dateutil.parser.parse(timestamp).isoformat()

        return (success, errors, message, timestamp)
