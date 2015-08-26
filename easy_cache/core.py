# -*- coding: utf-8 -*-
from __future__ import unicode_literals
import threading
import logging
import inspect
from time import time
import collections

import os
import six
from .utils import get_function_path


logger = logging.getLogger(__name__)


def force_text(obj):
    if isinstance(obj, six.text_type):
        return obj
    try:
        return six.text_type(obj)
    except UnicodeDecodeError:
        return obj.decode('utf-8')


def force_binary(obj):
    if isinstance(obj, six.binary_type):
        return obj

    try:
        return six.binary_type(obj)
    except UnicodeEncodeError:
        return obj.encode('utf-8')


class Value(object):

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


NOT_FOUND = Value('NOT_FOUND')
NOT_SET = Value('NOT_SET')
DEFAULT_TIMEOUT = Value('DEFAULT_TIMEOUT')
CACHE_KEY_DELIMITER = force_text(':')
TAG_KEY_PREFIX = force_text('tag')
RETURN_VALUE_PARAM_NAME = 'returned_value'

LAZY_MODE = os.environ.get('CACHE_TOOLS_LAZY_MODE_ENABLE', '') == 'yes'
DEFAULT_CACHE_ALIAS = 'default-cache-tools'


class CacheHandler(object):
    """ Inspired by Django """

    def __init__(self):
        self._caches = threading.local()

    def __getitem__(self, alias):
        try:
            return self._caches.caches[alias]
        except AttributeError:
            self._caches.caches = {}
        except KeyError:
            pass

        if alias == DEFAULT_CACHE_ALIAS:
            from django.core.cache import cache
        else:
            try:
                # noinspection PyUnresolvedReferences
                from django.core.cache import caches
                cache = caches[alias]
            except ImportError:
                from django.core.cache import get_cache
                cache = get_cache(alias)

        self._caches.caches[alias] = cache
        return cache

    def __setitem__(self, key, value):
        try:
            self._caches.caches
        except AttributeError:
            self._caches.caches = {}

        self._caches.caches[key] = value

    def get_default(self):
        return self[DEFAULT_CACHE_ALIAS]

    def set_default(self, cache_instance):
        self[DEFAULT_CACHE_ALIAS] = cache_instance


caches = CacheHandler()


# setters
def set_cache_key_delimiter(delimiter):
    if not isinstance(delimiter, six.string_types):
        raise TypeError('Invalid delimiter type, string required')

    global CACHE_KEY_DELIMITER
    CACHE_KEY_DELIMITER = force_text(delimiter)


def set_tag_key_prefix(prefix):
    if not isinstance(prefix, six.string_types):
        raise TypeError('Invalid tag prefix type, string required')

    global TAG_KEY_PREFIX
    TAG_KEY_PREFIX = force_text(prefix)


def set_global_cache_instance(cache_instance):
    caches.set_default(cache_instance)


def get_default_cache_instance():
    return caches.get_default()


def invalidate_cache_key(cache_key, cache_instance=None, cache_alias=None):
    _cache = cache_instance or caches[cache_alias or DEFAULT_CACHE_ALIAS]
    return _cache.delete(cache_key)


def invalidate_cache_prefix(prefix, cache_instance=None, cache_alias=None):
    return invalidate_cache_tags(prefix, cache_instance, cache_alias)


def invalidate_cache_tags(tags, cache_instance=None, cache_alias=None):
    if isinstance(tags, six.string_types):
        tags = [tags]

    _cache = TaggedCacheProxy(cache_instance or caches[cache_alias or DEFAULT_CACHE_ALIAS])
    return _cache.invalidate(tags)


def create_cache_key(*parts):
    """ Generate cache key using global delimiter char """
    if len(parts) == 1:
        parts = parts[0]
        if isinstance(parts, six.string_types):
            parts = [parts]

    return CACHE_KEY_DELIMITER.join(force_text(p) for p in parts)


def create_tag_cache_key(*parts):
    return create_cache_key(TAG_KEY_PREFIX, *parts)


def get_timestamp():
    return int(time() * 1000000)


def hash_dict(dictionary):
    """
        http://stackoverflow.com/questions/5884066/hashing-a-python-dictionary
        works only for hashable items in dict
    """
    return hash(frozenset(dictionary.items()))


class MetaCallable(collections.Mapping):
    """ Object contains meta information about method or function decorated with ecached,
        passed arguments, returned results, signature description and so on.
    """
    def __init__(self, args=(), kwargs=None, returned_value=NOT_SET, call_args=None):
        self.args = args
        self.kwargs = kwargs or {}
        self.returned_value = returned_value
        self.call_args = call_args or {}

    def __iter__(self):
        return iter(self.call_args)

    def __len__(self):
        return len(self.call_args)

    def __getitem__(self, item):
        return self.call_args[item]

    @property
    def has_returned_value(self):
        return self.returned_value is not NOT_SET


class TaggedCacheProxy(object):
    """ Each cache key/value pair can have additional tags to check
     if cached values is still valid.
    """
    def __init__(self, cache_instance):
        """
            :param cache_instance: should support `set_many` and
            `get_many` operations
        """
        self._cache = cache_instance

    def make_value(self, key, value, tags):
        data = {}
        tags = [create_tag_cache_key(_) for _ in tags]

        # get tags and their cached values (if exists)
        tags_dict = self._cache.get_many(tags)

        # set new timestamps for missed tags
        for tag_key in tags:
            if tag_key not in tags_dict:
                # this should be sent to cache as separate key-value
                data[tag_key] = get_timestamp()

        tags_dict.update(data)

        data[key] = {
            'value': value,
            'tags': tags_dict,
        }

        return data

    def __getattr__(self, item):
        return getattr(self._cache, item)

    def set(self, key, value, *args, **kwargs):
        value_dict = self.make_value(key, value, kwargs.pop('tags'))
        return self._cache.set_many(value_dict, *args, **kwargs)

    def get(self, key, default=None, **kwargs):
        value = self._cache.get(key, default=NOT_FOUND, **kwargs)

        # not found in cache
        if value is NOT_FOUND:
            return default

        tags_dict = value.get('tags')
        if not tags_dict:
            return value

        # check if it has valid tags
        cached_tags_dict = self._cache.get_many(tags_dict.keys())

        # compare dicts
        if hash_dict(cached_tags_dict) != hash_dict(tags_dict):
            # cache is invalid - return default value
            return default

        return value.get('value', default)

    def invalidate(self, tags):
        """ Invalidates cache by tags """
        ts = get_timestamp()
        return self._cache.set_many({create_tag_cache_key(tag): ts for tag in tags})


class Cached(object):

    def __init__(self,
                 function,
                 cache_key=None,
                 timeout=DEFAULT_TIMEOUT,
                 cache_instance=None,
                 cache_alias=None,
                 as_property=False):

        # processing different types of cache_key parameter
        if cache_key is None:
            self.cache_key = self.create_cache_key
        elif isinstance(cache_key, (list, tuple)):
            self.cache_key = create_cache_key(
                force_text(key).join(('{', '}')) for key in cache_key
            )
        else:
            self.cache_key = cache_key

        self.function = function
        self.timeout = timeout
        self.as_property = as_property
        self.instance = None
        self.klass = None

        self._scope = None
        self._cache_instance = cache_instance
        self._cache_alias = cache_alias or DEFAULT_CACHE_ALIAS

    @property
    def scope(self):
        return self.instance or self.klass or self._scope

    @scope.setter
    def scope(self, value):
        self._scope = value

    if LAZY_MODE:
        def _get_cache_instance(self):
            if self._cache_instance is None:
                return caches[self._cache_alias]
            return self._cache_instance
    else:
        def _get_cache_instance(self):
            if self._cache_instance is None:
                self._cache_instance = caches[self._cache_alias]
            return self._cache_instance

    cache_instance = property(_get_cache_instance)

    def __call__(self, *args, **kwargs):
        callable_meta = self.collect_meta(args, kwargs)
        cache_key = self.generate_cache_key(callable_meta)
        cached_value = self.get_cached_value(cache_key)

        if cached_value is NOT_FOUND:
            value = self.function(*callable_meta.args, **callable_meta.kwargs)
            callable_meta.returned_value = value
            self.set_cached_value(cache_key, callable_meta)
            return value

        return cached_value

    def create_cache_key(self, *args, **kwargs):
        """ if cache_key parameter is not specified we use default algorithm """
        scope = self.scope
        prefix = get_function_path(self.function, scope)

        args = list(args)
        if scope:
            try:
                args.remove(scope)
            except ValueError:
                pass

        for k in sorted(kwargs):
            args.append(kwargs[k])
        return create_cache_key(prefix, *args)

    def update_arguments(self, args, kwargs):
        # if we got instance method or class method - modify positional arguments
        if self.instance:
            # first argument in args is "self"
            args = (self.instance, ) + args
        elif self.klass:
            # firs argument in args is "cls"
            args = (self.klass, ) + args

        return args, kwargs

    def __get__(self, instance, klass):
        if instance is not None:
            # bound method
            self.instance = instance
        elif klass:
            # class method
            self.klass = klass

        if self.as_property and instance is not None:
            return self.__call__()

        return self

    def get_cached_value(self, cache_key):
        logger.debug('Get cache_key="%s"', cache_key)
        return self.cache_instance.get(cache_key, NOT_FOUND)

    def set_cached_value(self, cache_key, callable_meta, **extra):
        if self.timeout is not DEFAULT_TIMEOUT:
            extra['timeout'] = self.timeout

        logger.debug('Set cache_key="%s"', cache_key)
        self.cache_instance.set(cache_key, callable_meta.returned_value, **extra)

    @staticmethod
    def _check_if_meta_required(callable_template):
        arg_spec = inspect.getargspec(callable_template)
        if arg_spec.varargs is None and arg_spec.keywords is None and len(arg_spec.args) == 1:
            return True
        elif arg_spec.varargs and arg_spec.keywords:
            return False
        raise TypeError('Invalid signature for "%s", must be one of '
                        '"func(meta)" or "func(*args, **kwargs)"' % callable_template)

    def _format(self, template, meta):
        if isinstance(template, (staticmethod, classmethod)):
            template = template.__func__

        if isinstance(template, collections.Callable):
            if self._check_if_meta_required(template):
                return template(meta)
            else:
                return template(*meta.args, **meta.kwargs)

        if not self.function:
            return template

        if isinstance(template, six.string_types):
            return force_text(template).format(**meta.call_args)
        elif isinstance(template, (list, tuple, set)):
            return [force_text(t).format(**meta.call_args) for t in template]

        raise TypeError(
            'Unsupported type for key template: {!r}'.format(type(template))
        )

    def collect_meta(self, args, kwargs, returned_value=NOT_SET):
        """ :returns: MetaCallable """
        args, kwargs = self.update_arguments(args, kwargs)

        meta = MetaCallable(args=args, kwargs=kwargs, returned_value=returned_value)

        if not self.function:
            return meta

        # default arguments are also passed to template function
        arg_spec = inspect.getargspec(self.function)
        diff_count = len(arg_spec.args) - len(args)

        # do not provide default arguments which were already passed
        if diff_count > 0:
            default_kwargs = dict(zip(arg_spec.args[-diff_count:],
                                      arg_spec.defaults[-diff_count:]))
        else:
            default_kwargs = {}

        default_kwargs.update(kwargs)
        meta.kwargs = default_kwargs
        meta.call_args = inspect.getcallargs(self.function, *args, **kwargs)
        return meta

    def generate_cache_key(self, callable_meta):
        return self._format(self.cache_key, callable_meta)

    def invalidate_cache_by_args(self, *args, **kwargs):
        callable_meta = self.collect_meta(args, kwargs)
        cache_key = self.generate_cache_key(callable_meta)
        return self.cache_instance.delete(cache_key)

    def __unicode__(self):
        return (
            '<Cached: callable="{}", cache_key="{}", timeout={}>'.format(
                get_function_path(self.function, self.scope),
                get_function_path(self.cache_key),
                self.timeout)
        )

    def __str__(self):
        if six.PY2:
            return force_binary(self.__unicode__())
        return self.__unicode__()

    def __repr__(self):
        try:
            return self.__str__()
        except (UnicodeEncodeError, UnicodeDecodeError):
            return '[Bad Unicode data]'


class TaggedCached(Cached):
    """ Cache with tags and prefix support """

    def __init__(self,
                 function,
                 cache_key=None,
                 timeout=None,
                 cache_instance=None,
                 cache_alias=None,
                 as_property=False,
                 tags=(),
                 prefix=None):

        super(TaggedCached, self).__init__(
            function=function,
            cache_key=cache_key,
            cache_instance=cache_instance,
            cache_alias=cache_alias,
            timeout=timeout,
            as_property=as_property,
        )
        assert tags or prefix
        self.tags = tags
        self.prefix = prefix

        if self._cache_instance:
            self._cache_instance = TaggedCacheProxy(self.cache_instance)

    if LAZY_MODE:
        @property
        def cache_instance(self):
            if self._cache_instance is None:
                return TaggedCacheProxy(caches[self._cache_alias])
            return self._cache_instance
    else:
        @property
        def cache_instance(self):
            if self._cache_instance is None:
                self._cache_instance = TaggedCacheProxy(caches[self._cache_alias])
            return self._cache_instance

    def invalidate_cache_by_tags(self, tags=(), *args, **kwargs):
        """ Invalidate cache for this method or property by one of provided tags
            :type tags: str | list | tuple | callable
        """
        if not self.tags:
            raise ValueError('Tags were not specified, nothing to invalidate')

        def to_set(obj):
            return set([obj] if isinstance(obj, six.string_types) else obj)

        callable_meta = self.collect_meta(args, kwargs)
        all_tags = to_set(self._format(self.tags, callable_meta))

        if not tags:
            tags = all_tags
        else:
            tags = to_set(self._format(tags, callable_meta))
            if all_tags:
                tags = tags & all_tags

        return self.cache_instance.invalidate(tags)

    def invalidate_cache_by_prefix(self, *args, **kwargs):
        if not self.prefix:
            raise ValueError('Prefix was not specified, nothing to invalidate')

        callable_meta = self.collect_meta(args, kwargs)
        prefix = self._format(self.prefix, callable_meta)
        return self.cache_instance.invalidate([prefix])

    def generate_cache_key(self, callable_meta):
        cache_key = super(TaggedCached, self).generate_cache_key(callable_meta)
        if self.prefix:
            prefix = self._format(self.prefix, callable_meta)
            cache_key = create_cache_key(prefix, cache_key)
        return cache_key

    def set_cached_value(self, cache_key, callable_meta, **extra):
        # generate tags and prefix only after successful execution
        tags = self._format(self.tags, callable_meta)

        if self.prefix:
            prefix = self._format(self.prefix, callable_meta)
            tags = set(tags) | {prefix}

        return super(TaggedCached, self).set_cached_value(cache_key, callable_meta, tags=tags)

    def __unicode__(self):
        return six.text_type(
            '<TaggedCached: callable="{}", cache_key="{}", tags="{}", prefix="{}", '
            'timeout={}>'.format(
                get_function_path(self.function, self.scope),
                get_function_path(self.cache_key),
                get_function_path(self.tags),
                get_function_path(self.prefix),
                self.timeout)
        )