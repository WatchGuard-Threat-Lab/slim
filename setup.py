#!/usr/bin/env python
# coding=utf-8
#
# Copyright © Splunk, Inc. All Rights Reserved.

from __future__ import absolute_import, division, print_function

from os import path
import os
import sys

from platform import system
from setuptools import Command, find_packages, setup
from setuptools.command.install import install


class CreateSymlink(Command):
    def __init__(self, dist, **kwargs):
        Command.__init__(self, dist, **kwargs)
        self.install_purelib = None
        self.install_scripts = None
        self.exec_prefix = None
        self.outputs = []

    def finalize_options(self):
        pass

    def get_outputs(self):
        return self.outputs

    def initialize_options(self):
        pass

    def run(self):

        def create_symlink():
            if path.islink(link):
                os.remove(link)
            os.symlink(target, link)

        if self.exec_prefix is None:
            # We're installing to the user's directory (see --user option) and we've got no good place to create links
            return

        if hasattr(sys, 'real_prefix'):

            # We're installing to a virtual environment
            prefix = self.exec_prefix

        else:

            # We're installing to the system environment

            if system() == 'Darwin':
                prefix = '/usr/local'
                link = path.join(prefix, 'bin', 'slim')
                target = path.join(self.install_scripts, 'slim')
                self.execute(create_symlink, (), 'linking ' + link + ' -> ' + target)
                self.outputs.append(link)
            else:
                prefix = '/usr'

        target_directory = path.join(self.install_purelib, 'slim/man/man1')
        link_directory = path.join(prefix, 'share', 'man', 'man1')

        if not path.isdir(link_directory):
            os.makedirs(link_directory)

        for target in os.listdir(target_directory):
            link = path.join(link_directory, target)
            target = path.join(target_directory, target)
            self.execute(create_symlink, (), 'linking ' + link + ' -> ' + target)
            self.outputs.append(link)


class Install(install):

    def __init__(self, dist):
        self._should_create_symlink = None
        install.__init__(self, dist)

    def run(self):
        if self.should_create_symlink():
            self.distribution.command_options['create_symlink'] = {
                'install_purelib': ('install command', self.install_purelib),
                'install_scripts': ('install command', self.install_scripts),
                'exec_prefix': ('install command', self.exec_prefix)
            }
        install.run(self)

    # We should create a symlink to the Python executable if we're running on Darwin or Linux and we're
    #
    # * executing in the system Python runtime on a Darwin or Linux system and
    #   In a virtual environment `sys.prefix` points to the virtualenv directory while `sys.real_prefix` points to the
    #   prefix of the system Python. In a non-virtual environment `sys.real_prefix` is undefined.
    #
    # * installing to the system site-packages directory
    #   We know that we are installing to the system site-packages directory if `self.install_base` is equal to
    #   `self.install_platbase`.

    def should_create_symlink(self):
        value = self._should_create_symlink
        if value is None:
            value = self._should_create_symlink = (
                # not hasattr(sys, 'real_prefix') and
                system() in ('Darwin', 'Linux') and
                self.install_base == self.install_platbase
            )
        return value

    new_commands = [('create_symlink', should_create_symlink)]

    # Lambda required in Python 3 to workaround "Names in class scope are not accessible"
    # https://stackoverflow.com/questions/13905741/accessing-class-variables-from-a-list-comprehension-in-the-class-definition
    sub_commands = (
        lambda new_commands = new_commands:
        [cmd for cmd in install.sub_commands if cmd[0] not in new_commands] + new_commands
    )()


if sys.version_info < (2, 7):
    raise NotImplementedError('The ' + description + ' requires Python 2.7')

setup(
    name='splunk-packaging-toolkit',
    version='1.0.1',
    description='Splunk Packaging Toolkit',
    url='https://dev.splunk.com',
    author='Splunk, Inc.',
    author_email='devinfo@splunk.com',
    license='https://www.splunk.com/en_us/legal/splunk-software-license-agreement.html',
    classifiers=[
        'Programming Language :: Python',
        'Development Status :: 5 - Production/Stable',
        'Environment :: Other Environment',
        'Intended Audience :: Developers',
        'License :: Other/Proprietary License',
        'Operating System :: OS Independent',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'Topic :: Software Development :: Libraries :: Application Frameworks',
    ],

    entry_points={'console_scripts': ['slim = slim.__main__:main']},
    packages=find_packages(),
    package_data={
        'slim': [
            'config/conf-specs/*.spec',
            'config/common-information-models.json',
            'config/splunk-releases.json',
            'config/settings',
            'config/ignore',
            'man/man1/*.1'
        ],
    },
    data_files=[('', ['LICENSE.txt'])],
    install_requires=['semantic_version>=2.5.0', 'future>=0.18.2'],

    cmdclass={'install': Install, 'create_symlink': CreateSymlink}
)
