# coding=utf-8
#
# Copyright © Splunk, Inc. All Rights Reserved.

from __future__ import absolute_import, division, print_function, unicode_literals
from collections.abc import Iterator  # pylint: disable=no-name-in-module

from ... utils.internal import string
from . file_position import FilePosition


class FileReader(Iterator):

    def __init__(self, istream, filename):
        self._filename = filename
        self._istream = istream
        self._line_number = 0
        self._line = None

    def __iter__(self):
        get = self.__next__
        try:
            while True:
                line = get()
                if len(line) == 0:
                    break
                yield line
        except StopIteration:
            pass

    def __str__(self):
        return string(self._filename) + ', line ' + string(self._line_number)

    @property
    def position(self):
        return FilePosition(self._filename, self._line_number)

    def __next__(self):
        line = self._line
        if line:
            self._line = None
            return line
        line = next(self._istream)
        self._line_number += 1
        return line

    # this is done because we have no control of the base class
    next = __next__

    def read_continuation(self, line):
        readline = self.__next__
        while line.endswith('\\\n'):
            try:
                continuation = readline()
            except StopIteration:
                break
            line = line[:-2] + '\n' + continuation
        return line

    @property
    def filename(self):
        return self._filename

    @property
    def line_number(self):
        return self._line_number

    def put_back(self, line):
        assert self._line is None
        self._line = line
