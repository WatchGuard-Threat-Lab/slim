# coding=utf-8
#
# Copyright © Splunk, Inc. All Rights Reserved.

""" app_installation module

"""

from __future__ import absolute_import, division, print_function, unicode_literals

from builtins import object
from collections.abc import Iterable, MutableMapping
from collections import OrderedDict  # pylint: disable=no-name-in-module
from json import JSONEncoder
from tempfile import mkstemp
from os import path
import os

import io
import shutil
import tarfile

from .. utils import SlimStatus, SlimLogger, slim_configuration, encode_filename, encode_string
from .. utils.internal import string

from . _deployment import AppDependencyGraph, AppDeploymentSpecification
from . _installation import AppInstallationGraph
from . _internal import ObjectView
from . _source import AppSource


# TODO: Remove this redundancy by creating an app._internal module with json decoding/encoding functions (?)
# Also see: app._installation._AppJsonEncoder
# Consider the alternative of putting this under the umbrella of ObjectView instead of creating a protected json module
# or vice versa

class _AppJsonEncoder(JSONEncoder):

    def __init__(self, indent=False):
        if indent:
            separators = None
            indent = 2
        else:
            separators = (',', ':')
            indent = None
        JSONEncoder.__init__(self, ensure_ascii=False, indent=indent, separators=separators)

    # Under Python 2.7 pylint incorrectly asserts AppJsonEncoder.default is hidden by an attribute defined in
    # json.encoder at or about line 162. Code inspection reveals this not to be the case, hence we
    # pylint: disable=method-hidden

    def default(self, o):
        if isinstance(o, Iterable):
            return list(o)
        return JSONEncoder.default(self, o)

_encoder = _AppJsonEncoder()
_encode = _encoder.encode
_iterencode = _encoder.iterencode


class AppServerClass(object):

    def __init__(self, name, object_view, repository, repository_path):

        self._name = string(name)
        self._repository = repository
        self._repository_path = repository_path
        self._workload = frozenset(object_view.workload)

        self.reload(object_view.apps)

    # region Special methods

    def __repr__(self):
        return 'AppServerClass(' + repr(self._name) + ')'

    def __str__(self):
        return _encode(self.to_dict())

    # endregion

    # region Properties

    @property
    def apps(self):
        return self._apps

    @property
    def name(self):
        return self._name

    @property
    def workload(self):
        return self._workload

    # endregion

    # region Methods

    def add_source(self, package_path):

        repository_path = self._repository_path

        try:
            is_tarfile = tarfile.is_tarfile(package_path)
        except OSError as error:
            SlimLogger.error(
                'Cannot add ', encode_filename(package_path), ' to repository directory ',
                encode_filename(repository_path), ': ', error.strerror)
            return None

        if not is_tarfile:
            SlimLogger.error(
                'Cannot add ', encode_filename(package_path), ' to repository directory ',
                encode_filename(repository_path), ' because it is not a source package')
            return None

        package = path.basename(package_path)

        if package in self._repository:
            return package

        try:
            shutil.copy(package_path, self._repository_path)
        except OSError as error:
            SlimLogger.error(
                'Cannot add ', encode_filename(package), ' to repository directory ', encode_filename(repository_path),
                ': ', error.strerror)
            return None

        self._repository[package] = AppSource(path.join(repository_path, package))
        return package

    def describe_app(self, app_id):

        apps = self.apps
        installation = apps.get(app_id)

        if installation is None:
            return None  # The app is not installed on this server class

        installations = apps.describe_installation(installation)
        return AppServerClassUpdate(self, None, installations)

    @classmethod
    def from_deployment_specification(cls, deployment_specification, server_classes):

        info = ObjectView((
            ('workload', deployment_specification.workload),
            ('apps', ObjectView(()))
        ))

        name = deployment_specification.name
        repository = server_classes.repository
        repository_path = server_classes.repository_path

        server_class = AppServerClass(name, info, repository, repository_path)
        server_classes[name] = server_class

        return server_class

    def get_source(self, package):

        try:
            source = self._repository[package]
        except KeyError:
            SlimLogger.error(
                'Package ', encode_filename(package), ' not found in repository directory ', encode_filename(
                    self._repository_path))
            return None

        if source is None:
            repository_path = self._repository_path
            package = path.join(repository_path, package)
            self._repository[package] = source = AppSource(package)

        return source

    def to_dict(self):
        return OrderedDict((
            ("workload", sorted(self.workload, reverse=True)),
            ("apps", self._apps.to_dict())
        ))

    def reload(self, apps):
        self._apps = AppInstallationGraph(self, apps)

    def remove_app(self, app_id):

        installation = self.apps.get(app_id)

        if installation is None:
            return None  # The app is not installed on this server class

        if len(installation.dependents) > 0:
            SlimLogger.error(
                app_id, ' cannot be uninstalled because it is still required by these apps:\n    ',
                '\n    '.join(installation.dependents))
            slim_configuration.payload.set_dependency_requirements(installation.dependents)
            slim_configuration.payload.status = SlimStatus.STATUS_ERROR_DEPENDENCY_REQUIRED
            return None

        self.apps.remove_installation(installation)
        return None

    def update_installation(self, app_installation_graph, disable_automatic_resolution=False):
        self.apps.update(app_installation_graph, disable_automatic_resolution)

    # endregion
    pass  # pylint: disable=unnecessary-pass


class AppServerClassCollection(MutableMapping):

    def __init__(self, repository, repository_path, server_classes=None):
        self._collection = OrderedDict() if server_classes is None else OrderedDict(server_classes)
        self._repository = repository
        self._repository_path = repository_path
        self._validate = True

        self._installed_packages = OrderedDict()

        # Compile a list of installed apps and the source package in the repository
        # This is used to account for dependencies defined without a packaged dependency
        for server_class in list(self._collection.values()):
            for installed_app in list(server_class.apps.values()):
                self._installed_packages[installed_app.id] = os.path.basename(installed_app.source.package)

    # region Special methods

    def __delitem__(self, name):
        self._collection.__delitem__(name)

    def __getitem__(self, name):
        return self._collection.__getitem__(name)

    def __contains__(self, name):
        return self._collection.__contains__(name)

    def __iter__(self):
        return self._collection.__iter__()

    def __len__(self):
        return self._collection.__len__()

    def __setitem__(self, name, value):
        self._collection.__setitem__(name, value)

    # endregion

    # region Properties

    @property
    def repository(self):
        return self._repository

    @property
    def repository_path(self):
        return self._repository_path

    @property
    def validate(self):
        return self._validate

    @validate.setter
    def validate(self, value):
        """ Provide the ability to enable or disable validation. This is useful when batch updates are required.
        """
        self._validate = value

    # endregion

    # region Methods

    @classmethod
    def load(cls, file, repository_path):  # pylint: disable=redefined-builtin

        # Load repository

        repository_path = path.abspath(repository_path)
        current_directory = os.getcwd()
        repository = OrderedDict()
        os.chdir(repository_path)

        try:
            directory_listing = os.listdir(repository_path)
        except OSError as error:
            SlimLogger.error('Cannot access repository directory ', encode_filename(repository_path), ': ', error)
        else:
            for name in directory_listing:
                if path.isfile(name) and tarfile.is_tarfile(name):
                    repository[name] = None
        finally:
            os.chdir(current_directory)

        # Read installation graph

        filename = file if isinstance(file, string) else file.name
        server_classes = OrderedDict()

        try:
            if file is filename:
                with io.open(filename, encoding='utf-8') as fptr:
                    text = fptr.read()
            else:
                with file:
                    text = file.read()
        except OSError as error:
            SlimLogger.error(
                'Cannot load installation graph from ', encode_filename(filename), ' file: ', error.strerror)
            return None

        object_view = ObjectView(text)

        # Create server class collection

        for name in object_view:
            info = object_view[name]
            if name in server_classes:
                SlimLogger.warning('Replacing definition of duplicate server class name in installation graph: ', name)
            server_classes[name] = AppServerClass(name, info, repository, repository_path)

        return AppServerClassCollection(repository, repository_path, server_classes)

    def reload(self):
        """ Reload the installation graph. If validation was previously disabled and we are no longer in a valid state,
            this operation will fail in the same way an initial load() operation may fail.
        """
        for server_class in list(self._collection.values()):
            object_view_apps = ObjectView(string(_encode(server_class.apps.to_dict())))
            server_class.reload(object_view_apps)

    # pylint: disable=too-many-locals
    # pylint: disable=too-many-arguments
    def add(self, app_source, deployment_specifications, target_os,
            is_external=False, disable_automatic_resolution=False):
        """ Adds `app_source` to the current installation graph

        All server classes referenced by `deployment_specifications` may be affected.

        :param app_source:
        :type app_source: AppSource

        :param deployment_specifications:
        :type deployment_specifications: AppDeploymentSpecification

        :param target_os: if not None, only use dependencies for the given target OS
        :type target_os: string

        :param is_external: is the app "external" (i.e., not installed by the system)
        :type is_external: bool

        :param disable_automatic_resolution:
        :type disable_automatic_resolution: bool

        :return: :const:`None`.

        """
        dependency_graph = AppDependencyGraph(app_source, self.repository_path, self._installed_packages, target_os)

        if SlimLogger.error_count():
            return

        target_workloads = app_source.manifest.targetWorkloads or ['*']

        error_count = 0

        for deployment_specification in deployment_specifications:

            if '*' not in target_workloads and deployment_specification.name not in target_workloads:
                SlimLogger.warning(
                    'Application includes non-targeted workload for: ', deployment_specification.name)
                continue

            server_class = self._collection.get(deployment_specification.name)

            if server_class is None:
                server_class = AppServerClass.from_deployment_specification(deployment_specification, self)
                self._collection[deployment_specification.name] = server_class

            installation_graph = AppInstallationGraph.from_dependency_graph(
                server_class, dependency_graph, target_os, self._validate, is_external
            )

            if SlimLogger.error_count() > error_count:
                error_count = SlimLogger.error_count()
                continue

            source_specifications = dependency_graph.get_deployment_specifications(deployment_specification)
            app_id = app_source.id

            removal_list = []

            for source in source_specifications:
                specification = source_specifications[source]
                source.validate_deployment_specification(specification)
                installation = installation_graph[source.id]
                installation.update_input_groups(app_id, specification.inputGroups)
                installation.create_deployment_package()
                if not is_external and installation.deployment_package.is_empty:
                    removal_list.append(installation)

            for installation in removal_list:
                if len(installation_graph) == 0:
                    break
                if installation.id not in installation_graph:
                    # This installation must have been removed on a previous iteration because it was a dependency of
                    # another installation in the removal_list
                    continue
                installation_graph.remove_installation(installation)

            server_class.update_installation(installation_graph, disable_automatic_resolution)
    # pylint: enable=too-many-arguments

    def update_app(self, app_source, target_os):
        """ Updates `app_source` on the current installation graph

        Any server class with this app installed may be affected; others are untouched

        :param app_source:
        :type app_source: AppSource

        :return: :const:`None`
        """
        dependency_graph = AppDependencyGraph(app_source, self.repository_path, self._installed_packages, target_os)

        if SlimLogger.error_count():
            return

        error_count = 0

        collection = self._collection
        target_workloads = app_source.manifest.targetWorkloads or ['*']

        for name in collection:
            server_class = collection[name]
            if server_class.apps.get(app_source.id) is None:
                if '*' in target_workloads or name in target_workloads:
                    SlimLogger.warning('App ', app_source.id, ' does not include targeted workload: ', name)
                    continue

            # Create the installation graph for this app source
            installation_graph = AppInstallationGraph.from_dependency_graph(
                server_class, dependency_graph, target_os, self._validate
            )

            if SlimLogger.error_count() > error_count:
                error_count = SlimLogger.error_count()
                continue

            # Update the server class with this new installation graph
            server_class.update_installation(installation_graph)

    def partition(self, app_source, output_dir, partition_all=True):
        """ Partitions an app into deployment packages

        """
        collection = self._collection
        deployment_packages = []
        target_workloads = app_source.manifest.targetWorkloads or ['*']

        for name in collection:
            server_class = collection[name]
            update = server_class.describe_app(app_source.id)
            if update is None:
                if '*' not in target_workloads and name in target_workloads:
                    SlimLogger.warning('Application does not include targeted workload: ', name)
            else:
                if '*' not in target_workloads and name not in target_workloads:
                    SlimLogger.warning('Application includes non-targeted workload for: ', name)
                else:
                    package = update.save(app_source, output_dir, partition_all)
                    if package is None:
                        SlimLogger.warning('Application does not include targeted workload: ', name)
                    else:
                        deployment_packages.append(package)

        if len(deployment_packages) > 0:
            installation_actions_file = path.join(output_dir, 'installation-actions.json')
            with io.open(installation_actions_file, encoding='utf-8', mode='w', newline='') as ostream:
                ostream.write(_encode(slim_configuration.payload.installation_actions))

        return deployment_packages

    def save(self, filename=None):
        graph_json = OrderedDict((
            ((name, server_class.to_dict()) for name, server_class in list(self._collection.items()))
        ))
        slim_configuration.payload.set_installation_graph(graph_json)

        if filename is not None:
            with io.open(filename, encoding='utf-8', mode='w', newline='') as ostream:
                ostream.write(string(_encode(graph_json)))
            output_dir = os.path.dirname(filename)

            if SlimLogger.is_debug_enabled():
                graph_updates_file = path.join(output_dir, 'graph-updates.json')
                with io.open(graph_updates_file, encoding='utf-8', mode='w', newline='') as ostream:
                    ostream.write(_encode(slim_configuration.payload.graph_updates))

    def remove_app(self, app_id, server_classes):
        app_found = False
        for name in server_classes:
            collection = self._collection[name]
            if app_id in collection.apps:
                app_found = True
                self._collection[name].remove_app(app_id)
        if not app_found:
            SlimLogger.warning('App ', app_id, ' has not been installed.')

    def update_installation(self, action, target_os, disable_automatic_resolution=False):

        if action.action == 'remove':
            SlimLogger.step('Performing remove action for ' + encode_string(action.args.app_id))
            self.remove_app(action.args.app_id, self._collection)

        elif action.action == 'add' or action.action == 'set':
            SlimLogger.step('Performing add action for ' + encode_string(action.args.app_package))

            package_path = os.path.join(slim_configuration.repository_path, action.args.app_package)
            app_source = AppSource(package_path, None)

            if SlimLogger.error_count():
                return

            deployment_specifications = AppDeploymentSpecification.get_deployment_specifications(
                action.args.deployment_packages,
                action.args.combine_search_head_indexer_workloads,
                action.args.workloads)

            if SlimLogger.error_count():
                return

            # If we are setting new mappings (instead of adding them), remove installations no longer referenced
            if action.action == 'set':

                # Compute the list of server classes this app will be removed from to match new specifications:
                # - Start with the current list of server classes this app has been added to
                # - Remove the list of server classes this app should remain on
                old_list = [name for name in self._collection if
                            self._collection[name].apps.get(app_source.id) is not None]
                new_list = [deployment_specification.name for deployment_specification in deployment_specifications]
                removal_list = set(old_list) - set(new_list)

                # First remove this app from the server classes no longer needed
                # If we cannot remove the app because of dependency conflicts, we cannot update the mappings
                self.remove_app(app_source.id, removal_list)
                if SlimLogger.error_count():
                    return

            # Add this app to the new deployment specifications
            self.add(app_source,
                     deployment_specifications,
                     target_os,
                     is_external=action.args.get("is_external", False),
                     disable_automatic_resolution=disable_automatic_resolution)

        elif action.action == 'update':
            SlimLogger.step('Performing update action for ' + encode_string(action.args.app_package))
            package_path = os.path.join(slim_configuration.repository_path, action.args.app_package)
            app_source = AppSource(package_path, None)

            if not SlimLogger.error_count():
                self.update_app(app_source, target_os)

        else:
            SlimLogger.error('Installation action ' + encode_string(action.name) + ' is unknown or not-yet-implemented')

    # endregion
    pass  # pylint: disable=unnecessary-pass


class AppServerClassUpdate(object):

    def __init__(self, server_class, removals, installations):

        self._server_class = server_class
        self._removals = removals
        self._additions = installations

    def save(self, app_source, output_dir, partition_all=True):

        remove = None if self._removals is None else [installation.id for installation in self._removals]

        if self._additions is None:
            add = None
        else:
            arcname = app_source.package_prefix + '-' + self._server_class.name
            add = None

            if partition_all:
                package_handle, package_name = mkstemp(dir=output_dir)
                sub_package_count = 0

                with io.open(package_handle, mode='w+b') as ostream:
                    with tarfile.open(package_name, fileobj=ostream, mode='w:gz') as package:
                        for installation in self._additions:
                            sub_package_name = installation.partition(output_dir)
                            if sub_package_name:
                                sub_package_archive_name = path.join(arcname, path.basename(sub_package_name))
                                package.add(sub_package_name, arcname=sub_package_archive_name)
                                os.remove(sub_package_name)
                                sub_package_count += 1

                if sub_package_count == 0:
                    os.remove(package_name)
                else:
                    add = path.abspath(path.join(output_dir, arcname + '.tar.gz'))
                    os.rename(package_name, add)
            else:
                for installation in self._additions:
                    if installation.id == app_source.id:
                        add = installation.partition(output_dir)
                        break

        slim_configuration.payload.add_installation_action(OrderedDict((
            ('serverClass', self._server_class.name),
            ('remove', remove),
            ('add', add)
        )))

        return add
