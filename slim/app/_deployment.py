# coding=utf-8
#
# Copyright © Splunk, Inc. All Rights Reserved.

# pylint: disable=too-many-lines


from __future__ import absolute_import, division, print_function, unicode_literals

from builtins import zip
from builtins import object

from collections.abc import Mapping
from collections import  deque  # pylint: disable=no-name-in-module
from fnmatch import fnmatch
from json import JSONEncoder

from os import makedirs, path

from tarfile import TarFile, TarInfo
import tarfile

import errno
import io
import re
import shutil

from . _internal.json_data import *
from .. rules import *
from .. utils import SlimStatus, SlimConstants, SlimIgnore, SlimLogger, encode_filename, encode_string, \
    slim_configuration
from .. utils.internal import hash_object, string
from .. utils.public import SlimTargetOSWildcard

from . _internal import ObjectView, OrderedSet


class _AppJsonEncoder(JSONEncoder):

    def __init__(self, indent=False):
        if indent:
            separators = None
            indent = 2
        else:
            separators = (',', ':')
            indent = None
        JSONEncoder.__init__(self, ensure_ascii=False, indent=indent, separators=separators)

    # pylint: disable=method-hidden
    def default(self, value):  # pylint: disable=arguments-differ
        # Confirmed by code inspection: under Python 2.7.11 pylint incorrectly asserts AppJsonEncoder.default is hidden
        # by an attribute defined in json.encoder line 162. Code inspection reveals this not to be the case, hence we
        # disable the method-hidden message
        try:
            return value.to_dict()
        except AttributeError:
            pass
        if isinstance(value, (Version, semantic_version.Spec)):
            return string(value)
        if isinstance(value, ObjectView):
            return value.__dict__
        if isinstance(value, Iterable):
            return list(value)
        return JSONEncoder.default(self, value)


_encoder = _AppJsonEncoder()
_encode = _encoder.encode
_iterencode = _encoder.iterencode


class AppDependencyGraph(Mapping):
    """ A directed acyclic graph representing the app and its dependencies

    """
    def __init__(self, app_source, repository, installed_packages=None, target_os=SlimTargetOSWildcard):

        self._root = app_source
        self._description = None
        self._graph = OrderedDict()
        self._dependents = OrderedDict()
        self._installed_packages = installed_packages
        self._dependency_sources = app_source.dependency_sources
        self._repository_sources = app_source.populate_dependency_sources(path.abspath(repository), installed_packages)
        self._target_os = target_os

        self._add_source(app_source)

        if self._is_cyclic():
            SlimLogger.error('Dependency graph for ', self._root.id, ' is cyclic.')
        elif self._check_dependencies():
            SlimLogger.error('Dependency graph for ', self._root.id, ' is conflicted.')

    # region Special Methods

    def __getitem__(self, app_source):
        return self._graph.__getitem__(app_source)

    def __contains__(self, app_source):
        return self._graph.__contains__(app_source)

    def __iter__(self):
        return self._graph.__iter__()

    def __len__(self):
        return self._graph.__len__()

    # endregion

    # region Properties

    @property
    def description(self):
        # type: () -> string
        description = self._description
        if description is None:
            description = self._description = self._describe(self.root, 1)
        return description

    @property
    def root(self):
        return self._root

    # endregion

    # region Methods

    def print_description(self, ostream):
        """ Describes the dependency graph in a pretty form (depth-first search).

        Indent each level by spaces starting with a |
        Each package will be prefixed with |-- to match npm style output

        Example:
        [dependency-graph]
        |-- com.splunk.app:fictional@1.0.0
        |   |-- com.splunk.addon:microsoft_windows@4.7.5 (accepting ~4.7.5)
        |      |-- com.splunk.addon:utilities@1.0.0 (accepting ~1.0.0)
        |   |-- com.splunk.addon:star_nix@5.2.1 (accepting ~5.2.1)

        """
        description = self.description

        # Output the dependency graph

        if ostream is not None:
            try:
                ostream.write('[dependency-graph]\n')
                ostream.write(description)
            except:
                ostream.write(b'[dependency-graph]\n')
                ostream.write(description.encode())

    def export_source_package(self, output_dir):

        source = self._root

        app_name = source.id
        app_package = source.package_prefix

        source_package_path = path.join(output_dir, app_package + '.tar.gz')

        with TarFile.gzopen(source_package_path, mode='w', compresslevel=9, encoding='utf-8') as source_package:

            source_package.add(source.directory, arcname=app_name, filter=SlimIgnore(app_name, source.directory).filter)

            if len(self._graph) > 1:
                info = TarInfo(SlimConstants.DEPENDENCIES_DIR)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755  # rwx r-x r-x
                source_package.addfile(info)

                for dependency in self._graph:
                    if dependency is source:
                        continue
                    package = dependency.package
                    source_package.add(package, arcname=path.join('.dependencies', path.basename(package)))

            slim_configuration.payload.set_source_package(source_package_path)
            return source_package.name

    def get_deployment_specifications(self, deployment_specification):
        """ Returns an ordered dictionary of AppDeploymentSpecification objects derived from deployment_specification

        The dictionary is keyed by AppSource and represents the set of apps to be deployed to the workload identified
        by deployment_specification.

        """
        input_groups = deployment_specification.inputGroups

        if input_groups is None or input_groups == AppDeploymentSpecification.all_input_groups:
            # Each app receives the same deployment specification in the absence of input groups
            result = OrderedDict(((source, deployment_specification) for source in self._graph))
        else:
            # Deployment specifications vary based on the input group requirements of the root app, transitively
            result = OrderedDict()
            self._get_deployment_specifications(self._root, deployment_specification, result)

        return result

    # TODO: SPL-123967: Reduce the number of locals or otherwise refactor this code to make it more understandable (?)
    # pylint: disable=too-many-branches, too-many-locals
    def report_unreferenced_input_groups(self, level):
        """ Issues an error message for unreferenced input groups by app

        Unreferenced input groups are listed by app, transitively.

        """
        reported_unreferenced_input_groups = False
        repository_sources = self._repository_sources
        union_of = AppDependencyGraph._union_of

        for app_source in self._graph:  # pylint: disable=too-many-nested-blocks

            app_dependencies = self._graph[app_source]
            input_groups = app_source.manifest.inputGroups

            if input_groups is None:
                continue

            referenced_dependencies = OrderedDict((app_dependency, frozenset()) for app_dependency in app_dependencies)
            dependency_declarations = app_source.manifest.dependencies

            if dependency_declarations is not None:
                for group_name in input_groups:
                    group = input_groups[group_name]
                    group_requires = group.requires
                    if group_requires is None:
                        continue
                    for dependency_name in group_requires:
                        dependency_package = dependency_declarations[dependency_name].package
                        dependency_source = repository_sources[dependency_package]
                        references = referenced_dependencies[dependency_source]
                        dependency_groups = group_requires[dependency_name]
                        referenced_dependencies[dependency_source] = union_of(references, dependency_groups)

            all_input_groups = AppDeploymentSpecification.all_input_groups
            no_input_groups = AppDeploymentSpecification.no_input_groups

            for dependency_source in referenced_dependencies:
                references = referenced_dependencies[dependency_source]
                if references != all_input_groups:
                    input_groups = dependency_source.manifest.inputGroups
                    input_groups = no_input_groups if input_groups is None else frozenset(input_groups)
                    unreferenced = input_groups.difference(references)
                    count = len(unreferenced)
                    if count > 0:
                        if count == 1:
                            for unreferenced in unreferenced:
                                pass
                            SlimLogger.message(
                                level, app_source.qualified_id, ': unreferenced input group: dependency ',
                                dependency_source.qualified_id, ': ', unreferenced)
                        else:
                            unreferenced = sorted(unreferenced)
                            SlimLogger.message(
                                level, app_source.qualified_id, ': unreferenced input groups: dependency ',
                                dependency_source.qualified_id, ': \n    ', '\n    '.join(unreferenced))
                        reported_unreferenced_input_groups = True

        return reported_unreferenced_input_groups

    def traverse(self, visit):
        """ Traverses the current dependency graph breadth first

        """
        app_dependents = self._dependents
        queue = deque((self._root,))
        graph = self._graph
        visited = set()

        while len(queue) > 0:
            app_source = queue.pop()
            if app_source not in visited:
                dependency_sources = graph[app_source]
                dependent_sources = app_dependents.get(app_source)
                dependency_sources = visit(app_source, dependency_sources, dependent_sources)
                queue.extendleft(dependency_sources)
                visited.add(app_source)

        return app_dependents

    # endregion

    # region Privates

    # pylint: disable=protected-access
    def _add_source(self, app_source):
        """ Iterative breadth first construction from the root: `app_source` """
        app_dependents = self._dependents
        queue = deque((app_source,))
        graph = self._graph
        while len(queue) > 0:
            app_source = queue.pop()
            if app_source not in graph:
                app_source._dependencies = app_dependencies = self._get_dependencies(app_source)
                for app_dependency, app_dependency_source in app_dependencies:
                    dependents = app_dependents.get(app_dependency_source)
                    if dependents is None:
                        dependents = deque()
                    dependents.append((app_dependency, app_source))
                    app_dependents[app_dependency_source] = dependents
                dependency_sources = [ds for _, ds in app_dependencies]
                graph[app_source] = OrderedSet(dependency_sources)
                queue.extendleft(dependency_sources)
        return app_dependents

    def _check_dependencies(self):
        for app_source, dependents in list(self._dependents.items()):
            version = app_source.manifest.info.id.version
            for dependency, dependent_source in dependents:
                version_range = dependency.version
                if not version_range.match(version):
                    SlimLogger.error(
                        app_source.id, ': Version ', version, ' was selected, but version ', version_range, ' is '
                        'required by ', dependent_source.qualified_id)

    def _describe(self, app_source, level):
        graph_output = ''
        if level == 1:
            graph_output += '|-- ' + app_source.id + '@' + string(app_source.version) + '\n'
        for app_dependency, app_dependency_source in app_source.dependencies:
            graph_output += '|' + '   ' * level + \
                            '|-- ' + app_dependency_source.id + \
                            '@' + string(app_dependency_source.version) + \
                            ' (accepting ' + str(app_dependency.version) + ')\n'
            graph_output += self._describe(app_dependency_source, level + 1)
        return graph_output

    def _get_dependencies(self, app_source):

        dependency_sources = self._dependency_sources
        repository_sources = self._repository_sources
        dependencies = deque()

        if app_source.manifest.dependencies is not None:
            for name, dependency in app_source.get_dependencies_for_target_os(self._target_os):
                # If the manifest does not define a static dependency, check the list of installed app packages
                # In the case of a true dynamic dependency, we cannot validate it so warn and skip this app
                # Continue to log a MISSING_DEPENDENCIES status to the payload so the caller knows it's missing
                if dependency.package:
                    package = dependency.package
                elif self._installed_packages and self._installed_packages.get(name):
                    package = self._installed_packages.get(name)
                elif dependency.optional:
                    SlimLogger.warning('Skipping validation for optional dependency ', encode_filename(name))
                    slim_configuration.payload.add_missing_optional_dependency(name)
                    continue
                else:
                    SlimLogger.warning('Skipping validation for dynamic dependency ', encode_filename(name))
                    slim_configuration.payload.add_missing_dependency(name)
                    slim_configuration.payload.status = SlimStatus.STATUS_ERROR_MISSING_DEPENDENCIES
                    continue

                try:
                    dependency_source = dependency_sources[package]  # from app source .dependencies
                except KeyError:
                    try:
                        dependency_source = repository_sources[package]  # from repository (backup)
                    except KeyError:
                        SlimLogger.error('Expected to find static dependency ', encode_filename(package))
                        slim_configuration.payload.add_missing_dependency(name)
                        slim_configuration.payload.status = SlimStatus.STATUS_ERROR_MISSING_DEPENDENCIES
                        continue
                repository_sources[package] = dependency_source

                # Make sure our dependency package has a manifest
                dependency_manifest = dependency_source.manifest
                if not dependency_manifest:
                    continue

                version = dependency_manifest.info.id.version
                version_range = dependency.version

                if version_range.match(version) is False:
                    SlimLogger.error(
                        app_source.id, ': Packaged version of dependency ', encode_string(name), ' is outside range ',
                        version_range, ': ', encode_filename(dependency_source.package))

                dependencies.append((dependency, dependency_source))

        return dependencies

    # TODO: SPL-123967: Reduce the number of locals or otherwise refactor this code to make it more understandable (?)
    # pylint: disable=too-many-locals
    def _get_deployment_specifications(self, app_source, deployment_specification, result):

        result[app_source] = deployment_specification
        union_of = AppDependencyGraph._union_of
        dependencies = app_source.manifest.dependencies

        # Build a dictionary of dependent app requirements

        input_groups = app_source.manifest.inputGroups
        requirements = OrderedDict()

        if input_groups is not None:

            for name in deployment_specification.inputGroups:
                group = input_groups.get(name)
                if group is None:
                    continue
                try:
                    aliases = group.requires
                except AttributeError:
                    continue
                for alias in aliases:
                    requirement = group.requires[alias]
                    dependency = dependencies[alias]
                    dependency_source = self._repository_sources[dependency.package]
                    assert dependency_source in self._graph[app_source], 'Dependency graph is corrupt'
                    requirements[dependency_source] = union_of(
                        requirements.get(dependency_source, AppDeploymentSpecification.no_input_groups), requirement
                    )

        # Add dependency sources to the `result`, taking requirements into account

        for dependency, dependency_source in app_source.dependencies:
            input_groups = requirements.get(dependency_source, AppDeploymentSpecification.no_input_groups)
            if dependency_source in result:
                dependency_deployment_specification = AppDeploymentSpecification((
                    ('name', deployment_specification.name), ('workload', deployment_specification.workload),
                    ('inputGroups', union_of(result[dependency_source].inputGroups, input_groups))))
            else:
                dependency_deployment_specification = AppDeploymentSpecification((
                    ('name', deployment_specification.name), ('workload', deployment_specification.workload),
                    ('inputGroups', input_groups)))
            self._get_deployment_specifications(dependency_source, dependency_deployment_specification, result)

    def _is_cyclic(self):
        """ Returns True if this dependency graph is cyclic.

        """
        graph = self._graph
        graph_path = set()
        visited = set()

        def visit(app_source):
            if app_source in visited:
                return False
            visited.add(app_source)
            graph_path.add(app_source)
            for neighbour in graph.get(app_source, ()):
                if neighbour in graph_path or visit(neighbour):
                    return True
            graph_path.remove(app_source)
            return False

        return any(visit(app_source) for app_source in graph)

    @staticmethod
    def _union_of(fg_1, fg_2):
        return AppDeploymentSpecification.all_input_groups if '*' in fg_1 or '*' in fg_2 else fg_1.union(fg_2)

    _forwarder_workload = frozenset(('forwarder',))

    # endregion
    pass  # pylint: disable=unnecessary-pass


class AppDeploymentPackage(object):

    # TODO: SPL-123967: Reduce the number of locals or otherwise refactor this code to make it more understandable (?)
    # pylint: disable=too-many-locals
    def __init__(self, app_source, deployment_specification):

        # Compute deployment package identifiers: self._name, self._stage_name, and self._archive_name

        app_root = app_source.directory
        app_manifest = app_source.manifest
        app_configuration = app_source.configuration

        app_id = app_manifest.info.id
        group, name, version = (app_id.group, app_root if app_id.name is None else app_id.name, app_id.version)

        self._name = '-'.join((part for part in (group, name, string(version)) if part is not None))
        self._stage_name = self._name + '-' + deployment_specification.name
        self._archive_name = self._stage_name + '.tar.gz'

        # TODO: Do we need rules to exclude files referenced by the info section of app.manifest (e.g., license files)?
        # TODO: Should we partition spec files?

        # Partition the app_source's configuration consistent with the current deployment specification

        relevant_configurations = OrderedDict()

        for configuration_file in app_configuration.files():
            # TODO: Lookup packaging rule for any stanza, falling back to the DefaultPackagingRule.instance()
            # Validation and packaging rules should be treated similarly (See AppConfigurationValidator)
            package = (InputsPackagingRule if configuration_file.name == 'inputs' else DefaultPackagingRule).instance()
            relevant_files = OrderedDict()
            for section in configuration_file.sections():
                relevant_stanzas = OrderedDict()
                for stanza in section.stanzas():
                    relevant_settings = OrderedDict()
                    for setting in stanza.settings():
                        if package.should_include_setting(stanza, setting, deployment_specification, app_manifest):
                            relevant_settings[setting.name] = setting
                    if package.should_include_stanza(stanza, relevant_settings, deployment_specification, app_manifest):
                        relevant_stanzas[stanza.name] = relevant_settings
                if len(relevant_stanzas) > 0:
                    file_name = section.name[len(path.commonprefix((app_root, section.name))) + 1:]
                    relevant_files[file_name] = relevant_stanzas
            if len(relevant_files) > 0:
                relevant_configurations[configuration_file.name] = relevant_files

        self._configuration = relevant_configurations
        self._app_root = app_root

        # Gather up those asset exclusion patterns that apply to the current deployment_specification

        workload = deployment_specification.workload
        exclusion_patterns = []

        for pattern, excluded_roles in AppDeploymentPackage._exclusion_rule:
            if len(workload.difference(excluded_roles)) == 0:
                exclusion_patterns.append(pattern)

        # Wrap up construction based on exclusion patterns

        self._set_asset_filenames(exclusion_patterns)
        self._is_empty = self._detect_is_empty(app_source, self._asset_filenames, relevant_configurations)

    # region Special methods

    def __repr__(self):
        result = (
            'AppDeploymentSpecification(' +
            'name=' + repr(self._name) +
            'stage_name=' + repr(self._stage_name) +
            'archive_name=' + repr(self._archive_name) +
            'configuration=' + repr(self._configuration) + ')'
        )
        return result

    def __str__(self):
        result = _encode(OrderedDict((
            ('name', self._name),
            ('archive_name', self._archive_name),
            ('configuration', self._configuration)
        )))
        return result

    # endregion

    # region Properties

    @property
    def archive_name(self):
        return self._archive_name

    @property
    def configuration(self):
        return self._configuration

    @property
    def is_empty(self):
        return self._is_empty

    @property
    def name(self):
        return self._name

    @property
    def stage_name(self):
        return self._stage_name

    # endregion

    # region Methods

    def export(self, output_dir):
        """ Exports the current targeted deployment package as a gzipped tarball

        """
        is_debug_enabled = SlimLogger.is_debug_enabled()

        if not is_debug_enabled:
            append_digest = None
            digest = None
        else:
            filename = path.join(output_dir, self._stage_name + '.configuration.json')

            with io.open(filename, encoding='utf-8', mode='w', newline='') as ostream:
                ostream.write(string(self))

            SlimLogger.debug(
                'Saved ', encode_filename(self._stage_name), ' configuration to ', encode_filename(filename)
            )

            def append_digest(tar_info):
                if not tar_info.isdir():
                    name, size = tar_info.name, tar_info.size
                    object_id = hash_object(path.join(source_container, name), size)
                    digest.append(OrderedDict((('name', name), ('objectId', object_id), ('size', size))))
                return tar_info

            source_container = path.dirname(self._app_root)
            digest = []

        self._export(output_dir, filter=append_digest)

        if is_debug_enabled:
            filename = path.join(output_dir, self._stage_name + '.file-digest.json')

            with io.open(filename, encoding='utf-8', mode='w', newline='') as ostream:
                ostream.write(_encode(sorted(digest, key=lambda item: item['name'])))

            SlimLogger.debug('Saved ', encode_filename(self._stage_name), ' file digest to ', encode_filename(filename))

        return path.join(output_dir, self.archive_name)

    # endregion

    # region Protected

    @staticmethod
    def _detect_is_empty(app_source, asset_filenames, configurations):
        """ Detect that an app is empty

        An app is empty, if it consists of no more than `default/app.conf`, `metadata/default.meta`, and documentation
        files that are referenced in the app manifest. The `bin` directory is ignored in the case where the app would
        otherwise be considered empty.

        :returns: const:`True`, if the app represented by `app_source`, `asset_filenames`, and `configurations` is
        empty. Otherwise, a value of const:`False` is returned.

        :rtype: bool

        """

        if len(configurations) == 0 or (len(configurations) == 1 and 'app' in configurations):
            if len(asset_filenames) == 0:
                return True
            app_info = app_source.manifest.info
            app_root = app_source.directory
            empty_set = {
                path.join(app_root, 'metadata'),
                path.join(app_root, 'metadata', 'default.meta')
            }
            for asset_file in asset_filenames:
                app_bin = path.join(app_root, 'bin')
                if asset_file.startswith(app_bin):
                    empty_set.add(asset_file)
            for item in app_info.license, app_info.privacyPolicy, app_info.releaseNotes:
                if item is None or item.text is None:
                    continue
                filename = path.normpath(item.text)
                while len(filename) > 0:
                    empty_set.add(path.normpath(path.join(app_root, filename)))
                    filename = path.dirname(filename)
            if asset_filenames.issubset(empty_set):
                return True

        return False

    def _exclude_conf_spec(self, filename):
        if filename.endswith('.conf.spec'):
            configuration_name = filename[:-len('.conf.spec')]
            return configuration_name not in self._configuration
        return False

    _exclusion_rule = (
        (('app.manifest',), frozenset(('forwarder', 'indexer', 'searchHead'))),
        (('appserver',), frozenset(('forwarder', 'indexer'))),
        (('lookups',), frozenset(('forwarder', 'indexer'))),
        (('static',), frozenset(('forwarder', 'indexer'))),
        (('default', 'data'), frozenset(('forwarder', 'indexer'))),
        (('default', '*.conf'), frozenset(('forwarder', 'indexer', 'searchHead'))),
        (('local', '*.conf'), frozenset(('forwarder', 'indexer', 'searchHead'))),
        (('metadata', 'local.meta'), frozenset(('forwarder', 'indexer', 'searchHead'))),
        (('README', _exclude_conf_spec), frozenset(('forwarder', 'indexer', 'searchHead')))
    )

    # pylint: disable=redefined-builtin
    # noinspection PyShadowingBuiltins
    def _export(self, output_dir, filter=None, keep_source=False):

        source = path.join(output_dir, self._stage_name)

        if path.isdir(source):
            shutil.rmtree(source)
        elif path.isfile(source) or path.islink(source):
            os.remove(source)

        shutil.copytree(self._app_root, source, ignore=self._ignore_assets)

        for configuration_name in self._configuration:
            configuration_info = self._configuration[configuration_name]
            for file_name in configuration_info:
                file_info = configuration_info[file_name]
                file_name = path.join(source, file_name)
                try:
                    ostream = io.open(file_name, encoding='utf-8', mode='w', newline='')
                except IOError as error:
                    if error.errno != errno.ENOENT:
                        raise
                    makedirs(path.dirname(file_name))
                    ostream = io.open(file_name, encoding='utf-8', mode='w', newline='')
                with ostream:
                    for stanza_name in file_info:
                        print('[', stanza_name.replace('\n', '\\\n'), ']', file=ostream, sep='')
                        stanza_info = file_info[stanza_name]
                        for setting_name in stanza_info:
                            print(string(stanza_info[setting_name]), file=ostream)

        archive = path.join(output_dir, self._archive_name)
        basename = path.basename(self._app_root)

        with tarfile.open(archive, 'w:gz') as package:
            package.add(source, arcname=basename, filter=filter)

        if keep_source:
            return

        shutil.rmtree(source)

    def _get_excluded_filenames(self, root, names, ignore_patterns):
        part_count = len(root) + 1
        candidates = []

        for ignore_pattern in ignore_patterns:
            if len(ignore_pattern) != part_count:
                continue
            match = True
            for filename, pattern in zip(reversed(root), ignore_pattern):
                if not fnmatch(filename, pattern):
                    match = False
                    break
            if match is True:
                candidates.append(self._get_match_function(ignore_pattern[-1]))

        ignored_names = []

        if len(candidates) == 0:
            return ignored_names

        for filename in names:
            for match_function in candidates:
                if match_function(filename):
                    ignored_names.append(filename)
                    break

        return ignored_names

    def _get_match_function(self, pattern):

        def _exclude_filename(filename):
            return fnmatch(filename, pattern)

        if isinstance(pattern, string):
            return _exclude_filename

        return pattern.__get__(self, self.__class__)  # expectation: pattern is a member function

    def _ignore_assets(self, source, names):
        return [name for name in names if path.join(source, name) not in self._asset_filenames]

    def _set_asset_filenames(self, ignore_patterns):

        get_excluded_filenames = self._get_excluded_filenames
        split_filename = self._split_filename
        file_counts = {self._app_root: 0}
        app_root = self._app_root
        asset_filenames = set()

        for root, directory_names, filenames in os.walk(app_root, topdown=True, followlinks=True):
            app_subdir = split_filename(root[len(path.commonprefix((app_root, root))) + 1:])
            if len(directory_names) > 0:
                for name in get_excluded_filenames(app_subdir, directory_names, ignore_patterns):
                    directory_names.remove(name)
                for name in directory_names:
                    name = path.join(root, name)
                    asset_filenames.add(name)
                    file_counts[name] = 0
            if len(filenames) > 0:
                for name in get_excluded_filenames(app_subdir, filenames, ignore_patterns):
                    filenames.remove(name)
                for name in filenames:
                    name = path.join(root, name)
                    asset_filenames.add(name)
            file_counts[root] += len(directory_names) + len(filenames)

        empty_directories = deque((name for name in file_counts if file_counts[name] == 0))

        while len(empty_directories) > 0:
            name = empty_directories.popleft()
            asset_filenames.remove(name)
            if len(asset_filenames) == 0:
                break
            name = path.dirname(name)
            file_counts[name] -= 1
            if file_counts[name] <= 0:
                empty_directories.append(name)

        self._asset_filenames = asset_filenames

    @staticmethod
    def _split_filename(filename):
        parts = []

        while len(filename) > 0:
            filename, part = path.split(filename)
            parts.append(part)

        return parts

    # endregion
    pass  # pylint: disable=unnecessary-pass


# pylint: disable=no-member
class AppDeploymentSpecification(ObjectView):

    # TODO: AppDeploymentSpecification should use AppConfigurationPlacement object, not frozenset to represent workloads
    # At the same time this change is made update:
    #   DefaultPackagingRule.includes,
    #   AppConfigurationPlacement.__init__, and
    #   AppConfigurationPlacement.is_overlapping

    # region Properties

    all_workloads = frozenset(('searchHead', 'indexer', 'forwarder'))
    all_input_groups = frozenset('*')
    no_input_groups = frozenset()

    # endregion

    # region Methods

    @staticmethod
    def from_forwarder_workloads(forwarder_workloads):

        deployment_specifications = OrderedDict()

        for input_group in forwarder_workloads:

            server_classes = forwarder_workloads[input_group]
            server_class = None

            # pylint: disable=cell-var-from-loop
            def update():
                deployment_specification = deployment_specifications.get(server_class)

                if deployment_specification is None:
                    deployment_specification = AppDeploymentSpecification((
                        ('name', server_class),
                        ('workload', frozenset(('forwarder',))),
                        ('inputGroups', frozenset((input_group,)))
                    ))
                else:
                    input_groups = deployment_specification.inputGroups
                    deployment_specification['inputGroups'] = input_groups.union((input_group,))

                if server_class in ('_search_heads', '_indexers'):
                    workload = deployment_specification.workload
                    deployment_specification['workload'] = workload.union((server_class,))

                deployment_specifications[server_class] = deployment_specification

            if isinstance(server_classes, string):
                server_class = server_classes
                update()
                continue

            if isinstance(server_classes, list):
                for server_class in server_classes:
                    if isinstance(server_class, string):
                        update()
                        continue
                    raise ValueError(
                        'Expected a server class name, not ', ObjectView.get_json_type_name(server_class), ': ',
                        server_class)
                continue

            raise ValueError(
                'Expected a server class name or a list of server class names, not ',
                ObjectView.get_json_type_name(server_classes), ': ',
                server_classes)

        return list(deployment_specifications.values())

    # TODO: SPL-123967: Refactor this code to make it more understandable (.)
    # pylint: disable=too-many-branches
    @staticmethod
    def get_deployment_specifications(
            deployment_specifications, combine_search_head_indexer_workloads, forwarder_deployment_specifications
    ):
        if combine_search_head_indexer_workloads:

            if len(deployment_specifications) == 0:
                # By default we get two deployment packages: one for a searchHead + indexer another for forwarders
                deployment_specifications.append(AppDeploymentSpecification((
                    ('name', '_search_head_indexers'),
                    ('workload', frozenset(('searchHead', 'indexer'))))))
                deployment_specifications.append(AppDeploymentSpecification((
                    ('name', '_forwarders'),
                    ('workload', frozenset(('forwarder',))))))
            else:
                # Deployment specifications targeting the 'searchHead' and/or 'indexer' workloads are combined

                input_groups = frozenset()
                remove_list = []

                for index, deployment_specification in enumerate(deployment_specifications):
                    if deployment_specification.workload.isdisjoint(('searchHead', 'indexer')):
                        continue
                    if deployment_specification.inputGroups is None:
                        continue
                    input_groups = input_groups.union(deployment_specification.inputGroups)
                    remove_list.append(index)

                for index in remove_list:
                    del deployment_specifications[index]

                if len(input_groups) == 0:
                    deployment_specification = AppDeploymentSpecification((
                        ('name', '_search_head_indexers'),
                        ('workload', frozenset(('searchHead', 'indexer')))))
                else:
                    deployment_specification = AppDeploymentSpecification((
                        ('name', '_search_head_indexers'),
                        ('workload', frozenset(('searchHead', 'indexer', 'forwarder'))),
                        ('inputGroups', input_groups)))

                deployment_specifications.append(deployment_specification)
        else:

            if len(deployment_specifications) == 0 and forwarder_deployment_specifications is None:
                # We get three deployment packages: one for each of search heads, indexers, and forwarders
                update_list = {'searchHead', 'indexer', 'forwarder'}
            else:
                # Forwarder groups assigned to 'searchHead' and 'indexer' workloads must be formulated, if not defined
                update_list = {'searchHead', 'indexer'}

                for deployment_specification in deployment_specifications:
                    workload = deployment_specification.workload.intersection(update_list)
                    count = len(workload)

                    if count == 0:
                        continue

                    if count == len(update_list):
                        update_list = None
                        break

                    for item in workload:
                        update_list.remove(item)

            if update_list is not None:

                if 'searchHead' in update_list:
                    deployment_specifications.append(AppDeploymentSpecification((
                        ('name', '_search_heads'),
                        ('workload', frozenset(('searchHead',))))))

                if 'indexer' in update_list:
                    deployment_specifications.append(AppDeploymentSpecification((
                        ('name', '_indexers'),
                        ('workload', frozenset(('indexer',))))))

                if 'forwarder' in update_list:
                    deployment_specifications.append(AppDeploymentSpecification((
                        ('name', '_forwarders'),
                        ('workload', frozenset(('forwarder',))))))

            if forwarder_deployment_specifications is not None:
                deployment_specifications += forwarder_deployment_specifications

        name_clashes = set()

        for deployment_specification in deployment_specifications:
            name = deployment_specification.name
            if name in name_clashes:
                SlimLogger.error('Duplicate deployment specification name: ', encode_string(name))
            name_clashes.add(name)

        return deployment_specifications

    @staticmethod
    def is_all_input_groups(input_groups):
        return input_groups is None or input_groups == AppDeploymentSpecification.all_input_groups

    @staticmethod
    def are_no_input_groups(input_groups):
        return input_groups == AppDeploymentSpecification.no_input_groups

    # endregion

    # region Protected

    class Converter(JsonDataTypeConverter):

        def convert_from(self, data_type, value):
            if value.get('inputGroups', None) is None:
                if 'forwarder' in value['workload']:
                    value['inputGroups'] = AppDeploymentSpecification.all_input_groups
            elif 'forwarder' not in value['workload']:
                raise ValueError(
                    'Deployment specification includes inputGroups, but does not include the forwarder workload: ' +
                    string(value))
            return value

        def convert_to(self, data_type, value):
            raise NotImplementedError()

    class InputGroupsConverter(JsonDataTypeConverter):

        def convert_from(self, data_type, value):
            if len(value) == 0:
                value = AppDeploymentSpecification.no_input_groups
            else:
                value = frozenset(value)
                if '*' in value:
                    value = AppDeploymentSpecification.all_input_groups
            return value

        def convert_to(self, data_type, value):
            raise NotImplementedError()

    class SafeFilenameConverter(JsonDataTypeConverter):

        def convert_from(self, data_type, value):
            match = self._safe_filename.match(value)
            if match is None:
                raise ValueError(
                    'Illegal characters in deployment specification.name ' + encode_string(value) + ' which must '
                    'match ' + self._safe_filename.pattern)
            return value

        def convert_to(self, data_type, value):
            raise NotImplementedError()

        _safe_filename = re.compile(r'[-._ a-zA-Z0-9]+$', re.UNICODE)

    class WorkloadConverter(JsonDataTypeConverter):

        def convert_from(self, data_type, value):
            if not self._workload_names.issuperset(value):
                raise ValueError('Deployment specification.workload is invalid')
            return frozenset(value)

        def convert_to(self, data_type, value):
            raise NotImplementedError()

        _workload_names = frozenset(('forwarder', 'indexer', 'searchHead'))

    schema = JsonSchema('deployment specification', JsonValue(required=True, data_type=JsonObject(
        JsonField(
            'name', JsonString(), converter=SafeFilenameConverter(), required=True),
        JsonField(
            'workload', JsonArray(JsonValue(JsonString())), converter=WorkloadConverter(), required=True),
        JsonField(
            'inputGroups', JsonArray(JsonValue(JsonString())), converter=InputGroupsConverter(), required=False)
    ), converter=Converter()))

    def _report_value_error(self, message):
        SlimLogger.error(message + ': ' + string(self))

    # endregion
    pass  # pylint: disable=unnecessary-pass
