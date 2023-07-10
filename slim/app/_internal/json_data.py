# coding=utf-8
#
# Copyright © Splunk, Inc. All Rights Reserved.

from __future__ import absolute_import, division, print_function, unicode_literals

from builtins import object
from abc import ABCMeta, abstractmethod, abstractproperty
from collections import OrderedDict  # pylint: disable=no-name-in-module
from collections.abc import Iterable, MutableMapping, MutableSequence
from itertools import chain
from numbers import Real
import os
from future.utils import with_metaclass

import semantic_version
from semantic_version import Version

from ... utils import encode_string
from ... utils.internal import string

try:
    from lxml.html import builder as e  # pylint: disable=unused-import
except ImportError:
    e = None


class JsonSchema(object):
    """

    :param name:
    :type name: string

    :param definition:
    :type definition: JsonValue

    """

    __slots__ = ('name', 'definition')

    # noinspection PyPropertyAccess
    def __init__(self, name, definition):
        assert isinstance(definition, JsonValue)
        self.name = name
        self.definition = definition

    def convert_from(self, value, onerror=None):
        if onerror is None:
            onerror = self._onerror
        name = self.name
        self.definition.validate(name, value, onerror)
        return self.definition.convert_from(name, value, onerror)

    def to_html(self):
        return e.TABLE(
            e.CAPTION('JSON Schema: ', self.name),
            e.TR(
                e.TH('member'), e.TH('type'), e.TH('default'), e.TH('required'), e.TH('description')
            ),
            *tuple(chain.from_iterable(self.definition.data_type.to_html(None)))
        )

    @staticmethod
    def _onerror(*args):
        raise ValueError((string(arg) for arg in args))


class JsonValue(object):
    """

    :param data_type:
    :type data_type: JsonDataType

    :param converter:
    :type converter: JsonDataTypeConverter

    :param default:
    :type default: object

    :param required:
    :type required: bool

    """

    __slots__ = ('data_type', 'converter', 'default', 'required', 'version')

    # noinspection PyPropertyAccess
    # pylint: disable=too-many-arguments
    def __init__(self, data_type, converter=None, default=None, required=False, version='1.0.0'):
        assert converter is None or isinstance(converter, JsonDataTypeConverter)
        assert isinstance(data_type, JsonDataType)
        # pylint: disable=unidiomatic-typecheck
        assert isinstance(required, bool)
        self.data_type = data_type
        self.converter = converter
        self.default = default
        self.required = required
        self.version = version
    # pylint: enable=too-many-arguments

    def convert_from(self, name, value, onerror):
        if value is None:
            if self.required is True:
                onerror('A value of type ', self.data_type.name, ' is required for ', name)
            return self.default
        return self.data_type.convert_from(name, value, self.converter, self.default, onerror)

    def to_html(self, name):
        return tuple(chain(
            (
                e.TR(
                    # TODO: JSON encode self.default and self.required
                    e.TD(name),
                    e.TD(self.data_type.name),
                    e.TD(string(self.default)),
                    e.TD(string(self.required)),
                    e.TD(string(self.version)),
                    e.TD('')
                ),
            ),
            *self.data_type.to_html(name)
        ))

    def validate(self, name, value, onerror):
        if value is None:
            if self.required is True:
                onerror('A value of type ', self.data_type.name, ' is required for ', name)
                return False
            return True
        return self.data_type.validate(name, value, onerror)


class JsonField(JsonValue):

    __slots__ = ('name',)

    # pylint: disable=too-many-arguments
    def __init__(self, name, data_type, converter=None, default=None, required=False, version='1.0.0'):
        assert isinstance(name, string)  # pylint: disable=unidiomatic-typecheck
        super(JsonField, self).__init__(data_type, converter, default, required, version)
        self.name = name
    # pylint: enable=too-many-arguments

    def to_html(self, name):
        name = self.name if name is None else name + '.' + self.name
        return tuple(chain(
            (
                e.TR(
                    # TODO: JSON encode self.default and self.required
                    e.TD(name),
                    e.TD(self.data_type.name),
                    e.TD(string(self.default)),
                    e.TD(string(self.required)),
                    e.TD(string(self.version)),
                    e.TD('')
                ),
            ),
            *self.data_type.to_html(name)
        ))


class JsonDataType(with_metaclass(ABCMeta, object)):

    @abstractproperty
    def name(self):
        pass

    # pylint: disable=too-many-arguments
    def convert_from(self, name, value, converter, default, onerror):
        if not self.validate(name, value, onerror):
            return default
        if converter is None:
            return value
        try:
            value = converter.convert_from(self, value)
        except (TypeError, ValueError) as error:
            onerror(name, ': ', error)
            value = default
        return value

    @abstractmethod
    def is_instance(self, value):
        pass

    # pylint: disable=unused-argument
    def to_html(self, name):
        return ()

    def validate(self, name, value, onerror):
        if self.is_instance(value):
            return True
        onerror('Expected ', self.name, ' value for ', name, ', not ' + string(value))
        return False


class JsonArray(JsonDataType):

    def __init__(self, definition):
        assert isinstance(definition, JsonValue)
        self._definition = definition

    @property
    def name(self):
        return 'Array of ' + self._definition.data_type.name

    # pylint: disable=too-many-arguments
    def convert_from(self, name, value, converter, default, onerror):

        if not self.validate(name, value, onerror):
            return default

        definition = self._definition

        if definition.data_type.is_instance(value):
            value = [definition.convert_from(name, value, onerror)]
        else:
            name += '[{0}]'
            if isinstance(value, MutableSequence):
                for i, element in enumerate(value):
                    value[i] = definition.convert_from(name.format(i), element, onerror)
            else:
                value = type(value)(
                    definition.convert_from(name.format(i), element, onerror) for i, element in enumerate(value)
                )

        return value if converter is None else converter.convert_from(self, value)

    def is_instance(self, value):
        """ Returns :const:`True` if value is an :class:`Iterable` or an instance of the array type.

        Otherwise, a value of :const:`False` is returned.

        """
        return (
            isinstance(value, Iterable) or  # pylint: disable=unidiomatic-typecheck
            self._definition.data_type.is_instance(value)
        )

    def to_html(self, name):
        value = self._definition.to_html('[i]' if name is None else name + '[i]'),  # nopep8, pylint: disable=trailing-comma-tuple
        return value

    def validate(self, name, value, onerror):

        if not super(JsonArray, self).validate(name, value, onerror):
            return False

        definition = self._definition

        if self.is_instance(value):
            error_count = 0
            for i, element in enumerate(value):
                error_count += definition.validate(name + '[' + string(i) + ']', element, onerror) is False
            return error_count == 0

        return definition.validate(name, value, onerror)


class JsonBoolean(JsonDataType):

    def __new__(cls):
        instance = getattr(cls, '_instance', None)
        if instance is None:
            instance = cls._instance = super(JsonBoolean, cls).__new__(cls)
        return instance

    @property
    def name(self):
        return 'Boolean'

    def is_instance(self, value):
        return isinstance(value, bool)  # pylint: disable=unidiomatic-typecheck


class JsonNumber(JsonDataType):

    def __new__(cls):
        instance = getattr(cls, '_instance', None)
        if instance is None:
            instance = cls._instance = super(JsonNumber, cls).__new__(cls)
        return instance

    @property
    def name(self):
        return 'Number'

    def is_instance(self, value):
        value_type = type(value)
        return issubclass(value_type, Real) and value_type is not bool


class JsonObject(JsonDataType):

    def __init__(self, *args, **kwargs):
        if len(args) > 0:
            assert all((isinstance(arg, JsonField) for arg in args))
            self._fields = OrderedDict(((field.name, field) for field in args))
        else:
            self._fields = None
        try:
            extra = kwargs['any']
            assert isinstance(extra, JsonValue)
        except KeyError:
            extra = None
        self._any = extra

    @property
    def name(self):
        return 'Object'

    # pylint: disable=too-many-arguments
    def convert_from(self, name, value, converter, default, onerror):

        if not super(JsonObject, self).validate(name, value, onerror):
            # We've got something other than an object so we return `default`; a value that presumably makes sense to
            # downstream processors
            return default

        field_pack, fields = self._any, self._fields

        if not (field_pack is None and fields is None):

            def convert_from(definition):
                return definition.convert_from(name + '.' + field_name, value.get(field_name), onerror)

            if fields is None:
                for field_name in value:
                    value[field_name] = convert_from(field_pack)
            else:
                for field_name in fields:
                    value[field_name] = convert_from(fields[field_name])
                if field_pack is not None:
                    for field_name in value:
                        if field_name in fields:
                            continue
                        value[field_name] = convert_from(field_pack)

        if converter is not None:
            try:
                value = converter.convert_from(self, value)
            except (ValueError, TypeError) as error:
                onerror(name, ': ', error)
                value = None

        return value

    def is_instance(self, value):
        return isinstance(value, MutableMapping)

    def to_html(self, object_name=None):  # pylint: disable=arguments-differ
        fields = self._fields
        if fields is None:
            value = self._any.to_html('<name>' if object_name is None else object_name + '.<name>'),  # nopep8, pylint: disable=trailing-comma-tuple
        else:
            value = tuple(chain(fields[name].to_html(object_name) for name in self._fields))
        return value

    def validate(self, name, value, onerror):
        """ Validates the named object `value`.

        :param name: dotted-path name of the object.
        :type name: string

        :param value: value of the object.
        :type value: MutableMapping

        :param onerror: a callable that accepts a variable-length list of objects.
        :type onerror: callable

        :return: const:`True`, if the object `value` validates; otherwise const::`False`.

        """
        if not super(JsonObject, self).validate(name, value, onerror):
            return False

        extra, fields = self._any, self._fields

        if extra is None and fields is None:
            return True  # no constraints on field names and no validation of field values

        def validate(field_definition):
            return field_definition.validate(name + '.' + field_name, value.get(field_name), onerror) is False

        error_count = 0

        if fields is None:

            # Validate extra (dynamic) field values in the absence of fixed (static) field values

            for field_name in value:
                error_count += validate(extra)
        else:

            # Validate fixed field names

            for field_name in fields:
                error_count += validate(fields[field_name])

            if extra is None:

                # Complain about undefined (illegal) field names because dynamic field values are not allowed

                for field_name in value:
                    if field_name not in fields:
                        onerror('Illegal field name: ', name, '.', field_name)
                        error_count += 1
            else:

                # Validate extra (dynamic) field values in the presence of fixed (static) field values

                for field_name in value:
                    if field_name not in fields:
                        error_count += validate(extra)

        return error_count == 0


class JsonString(JsonDataType):

    def __new__(cls):
        instance = getattr(cls, '_instance', None)
        if instance is None:
            instance = cls._instance = super(JsonString, cls).__new__(cls)
        return instance

    @property
    def name(self):
        return 'String'

    # pylint: disable=too-many-arguments
    def convert_from(self, name, value, converter, default, onerror):
        if not self.validate(name, value, onerror):
            return default
        if converter is None:
            return string(value)
        try:
            value = converter.convert_from(self, value)
        except (TypeError, ValueError) as error:
            onerror(name, ': ', error)
            value = default
        return value

    def is_instance(self, value):
        return isinstance(value, (bytes, string))


class JsonDataTypeConverter(with_metaclass(ABCMeta, object)):

    @abstractmethod
    def convert_from(self, data_type, value):
        pass

    @abstractmethod
    def convert_to(self, data_type, value):
        pass


class JsonFilenameConverter(JsonDataTypeConverter):

    def __init__(self, verify=None):
        self._verify = lambda stat, value: value if verify is None else verify

    def convert_from(self, data_type, value):
        """ Verifies that the given value is the name of an existing file.

        :return: `value`.
        :rtype: `string`

        """
        assert isinstance(data_type, JsonString) and isinstance(value, string)  # pylint: disable=unidiomatic-typecheck
        try:
            stat = os.stat(value)
        except OSError as error:
            # noinspection PyTypeChecker
            raise ValueError(error.strerror + ': ' + encode_string(value))
        return self._verify(stat, value)

    def convert_to(self, data_type, value):
        """ Converts the given semantic :type:`Version` `value` to the given `data_type`.

        :return: a value of the given `data_type`.

        """
        assert isinstance(data_type, JsonString) and isinstance(value, Version)
        return string(value)


class JsonVersionConverter(JsonDataTypeConverter):

    def __init__(self, version_spec=None):
        self._version_spec = semantic_version.Spec(version_spec) if isinstance(version_spec, string) else version_spec

    def convert_from(self, data_type, value):
        """ Converts the given `value` to a semantic :type:`Version` number using the specified `data_type`.

        :return: semantic version number
        :rtype: `Version`

        """
        assert isinstance(data_type, JsonString) and isinstance(value, string)  # pylint: disable=unidiomatic-typecheck
        version_spec = self._version_spec
        value = Version.coerce(value)
        if version_spec is None or value in version_spec:
            return value
        raise ValueError('Illegal version number: ' + string(value))

    def convert_to(self, data_type, value):
        """ Converts the given semantic :type:`Version` `value` to the given `data_type`.

        :return: a value of the given `data_type`.

        """
        assert isinstance(data_type, JsonString) and isinstance(value, Version)
        return string(value)


class JsonVersionSpecConverter(JsonDataTypeConverter):

    def convert_from(self, data_type, value):
        """ Converts the given `value` to a semantic :type:`Version` number using the specified `data_type`.

        :return: semantic version number
        :rtype: `Version`

        """
        assert isinstance(data_type, JsonString) and isinstance(value, string)  # pylint: disable=unidiomatic-typecheck
        try:
            value = semantic_version.Spec(value)
        except ValueError:
            raise ValueError('Illegal version specification: ' + string(value))
        return value

    def convert_to(self, data_type, value):
        """ Converts the given semantic :type:`Version` `value` to the given `data_type`.

        :return: a value of the given `data_type`.

        """
        assert isinstance(data_type, JsonString) and isinstance(value, Version)
        return string(value)

    any_version = semantic_version.Spec('*')
