from dockit.backends import get_document_backend

import re
import sys

from django.conf import settings
from django.db.models.options import get_verbose_name
from django.utils.translation import activate, deactivate_all, get_language, string_concat
from django.utils.encoding import smart_str, force_unicode
from django.utils.datastructures import SortedDict
from django.db.models import FieldDoesNotExist

from manager import Manager
from common import register_schema
from signals import pre_save, post_save, pre_delete, post_delete, class_prepared, pre_init, post_init

class Options(object):
    """ class based on django.db.models.options. We only keep
    useful bits."""
    
    abstract = False
    ordering = ['_id']
    
    DEFAULT_NAMES = ('verbose_name', 'db_table', 'ordering', 'schema_key',
                     'app_label', 'collection', 'virtual', 'proxy')
    
    def __init__(self, meta, app_label=None):
        self.module_name, self.verbose_name = None, None
        self.verbose_name_plural = None
        self.object_name, self.app_label = None, app_label
        self.meta = meta
        self.fields = SortedDict()
        self.collection = None
        self.schema_key = None
        self.virtual = False
        self.proxy = False
    
    def contribute_to_class(self, cls, name):
        cls._meta = self
        self.installed = re.sub('\.models$', '', cls.__module__) in settings.INSTALLED_APPS
        # First, construct the default values for these options.
        self.object_name = cls.__name__
        self.module_name = self.object_name.lower()
        self.verbose_name = get_verbose_name(self.object_name)
        self.collection = self.default_schema_key()
        self.schema_key = self.default_schema_key()

        # Next, apply any overridden values from 'class Meta'.
        if getattr(self, 'meta', None):
            meta_attrs = self.meta.__dict__.copy()
            for name in self.meta.__dict__:
                # Ignore any private attributes that Django doesn't care about.
                # NOTE: We can't modify a dictionary's contents while looping
                # over it, so we loop over the *original* dictionary instead.
                if name.startswith('_'):
                    del meta_attrs[name]
            for attr_name in self.DEFAULT_NAMES:
                if attr_name in meta_attrs:
                    setattr(self, attr_name, meta_attrs.pop(attr_name))
                elif hasattr(self.meta, attr_name):
                    setattr(self, attr_name, getattr(self.meta, attr_name))

            # verbose_name_plural is a special case because it uses a 's'
            # by default.
            setattr(self, 'verbose_name_plural', meta_attrs.pop('verbose_name_plural', string_concat(self.verbose_name, 's')))

            # Any leftover attributes must be invalid.
            if meta_attrs != {}:
                raise TypeError("'class Meta' got invalid attribute(s): %s" % ','.join(meta_attrs.keys()))
            del self.meta
        else:
            self.verbose_name_plural = string_concat(self.verbose_name, 's')
    
    def default_schema_key(self):
        return "%s.%s" % (smart_str(self.app_label), smart_str(self.module_name))
    
    def __str__(self):
        return self.collection

    def verbose_name_raw(self):
        """
        There are a few places where the untranslated verbose name is needed
        (so that we get the same value regardless of currently active
        locale).
        """
        lang = get_language()
        deactivate_all()
        raw = force_unicode(self.verbose_name)
        activate(lang)
        return raw
    verbose_name_raw = property(verbose_name_raw)
    
    def get_field(self, name):
        if name not in self.fields:
            raise FieldDoesNotExist
        return self.fields[name]
    
    def get_field_by_name(self, name):
        """
        Returns the (field_object, model, direct, m2m), where field_object is
        the Field instance for the given name, model is the model containing
        this field (None for local fields), direct is True if the field exists
        on this model, and m2m is True for many-to-many relations. When
        'direct' is False, 'field_object' is the corresponding RelatedObject
        for this field (since the field doesn't have an instance associated
        with it).

        Uses a cache internally, so after the first access, this is very fast.
        """
        if name not in self.fields:
            raise FieldDoesNotExist
        return (self.fields[name], None, True, False)
    
    def get_ordered_objects(self):
        return []
    
    @property
    def pk(self):
        class DummyField(object):
            def __init__(self, **kwargs):
                for key, value in kwargs.iteritems():
                    setattr(self, key, value)
        return DummyField(attname='pk')
    
    def get_backend(self):
        return get_document_backend()

class SchemaBase(type):
    """
    Metaclass for all schemas.
    """
    def __new__(cls, name, bases, attrs):
        super_new = super(SchemaBase, cls).__new__
        
        module = attrs.pop('__module__')
        new_class = super_new(cls, name, bases, {'__module__': module})
        
        attr_meta = attrs.pop('Meta', None)
        if not attr_meta:
            meta = getattr(new_class, 'Meta', None)
        else:
            meta = attr_meta
            if getattr(meta, 'proxy', False):
                if not hasattr(new_class, '_meta'):
                    raise ValueError('Proxy schemas must inherit from another schema')
                parent_meta = getattr(new_class, '_meta')
                for key in Options.DEFAULT_NAMES:
                    if not hasattr(meta, key) and hasattr(parent_meta, key):
                        setattr(meta, key, getattr(parent_meta, key))
        
        if getattr(meta, 'app_label', None) is None:
            document_module = sys.modules[new_class.__module__]
            app_label = document_module.__name__.split('.')[-2]
        else:
            app_label = getattr(meta, 'app_label')
        
        for base in bases:
            if hasattr(base, '_meta') and hasattr(base._meta, 'fields'):
                attrs.update(base._meta.fields)
        
        new_class.add_to_class('_meta', Options(meta, app_label=app_label))
        
        fields = [(field_name, attrs.pop(field_name)) for field_name, obj in attrs.items() if hasattr(obj, 'creation_counter')]
        fields.sort(key=lambda x: x[1].creation_counter)
        
        for field_name, obj in fields:
            new_class.add_to_class(field_name, obj)
        
        for obj_name, obj in attrs.items():
            new_class.add_to_class(obj_name, obj)
        
        if not new_class._meta.virtual:
            register_schema(new_class._meta.schema_key, new_class)
        
        class_prepared.send(**{'sender':cls, 'class':new_class})
        return new_class
    
    def add_to_class(cls, name, value):
        if hasattr(value, 'contribute_to_class'):
            value.contribute_to_class(cls, name)
        else:
            setattr(cls, name, value)

class Schema(object):
    __metaclass__ = SchemaBase
    
    def __init__(self, **kwargs):
        pre_init.send(sender=self.__class__, kwargs=kwargs)
        #super(Schema, self).__init-_()
        self._primitive_data = dict()
        self._python_data = dict()
        self._parent = None #TODO make parent a configurable field
        for key, value in kwargs.iteritems():
            #TODO check that key is a field or _data
            setattr(self, key, value)
        assert self._primitive_data is not None
        assert self._python_data is not None
        post_init.send(sender=self.__class__, instance=self)
    
    @classmethod
    def to_primitive(cls, val):
        #CONSIDER shouldn't val be a schema?
        if hasattr(val, '_primitive_data') and hasattr(val, '_python_data') and hasattr(val, '_meta'):
            #we've cached python values on access, we need to pump these back to the primitive dictionary
            for name, entry in val._python_data.iteritems():
                if name in val._meta.fields:
                    try:
                        val._primitive_data[name] = val._meta.fields[name].to_primitive(entry)
                    except:
                        print name, val._meta.fields[name], entry
                        raise
                else:
                    #TODO run entry through generic primitive processor
                    val._primitive_data[name] = entry
            return val._primitive_data
        assert isinstance(val, (dict, list, type(None))), str(type(val))
        return val
    
    @classmethod
    def to_python(cls, val, parent=None):
        if val is None:
            val = dict()
        return cls(_primitive_data=val, _parent=parent)
    
    def __getattribute__(self, name):
        fields = object.__getattribute__(self, '_meta').fields
        if name in fields:
            python_data = object.__getattribute__(self, '_python_data')
            if name not in python_data:
                primitive_data = object.__getattribute__(self, '_primitive_data')
                python_data[name] = fields[name].to_python(primitive_data.get(name), parent=self)
            return python_data[name]
        return object.__getattribute__(self, name)
    
    def __setattr__(self, name, val):
        if name in self._meta.fields:
            field = self._meta.fields[name]
            if not field.is_instance(val):
                val = field.to_python(val)
            self._python_data[name] = val
            #field = self._fields[name]
            #store_val = field.to_primitive(val)
            #self._primtive_data[name] = store_val
        else:
            super(Schema, self).__setattr__(name, val)
    
    def __getitem__(self, key):
        if key in self._meta.fields:
            return getattr(self, key)
        if key in self._primitive_data and key not in self._python_data:
            from serializer import PRIMITIVE_PROCESSOR
            r_val = self._primitive_data[key]
            p_val = PRIMITIVE_PROCESSOR.to_python(r_val)
            self._python_data[key] = p_val
        return self._python_data[key]
    
    def __setitem__(self, key, value):
        if key in self._meta.fields:
            setattr(self, key, value)
            return
        self._python_data[key] = value
    
    def __delitem__(self, key):
        if key in self._meta.fields:
            setattr(self, key, None)
            return
        self._python_data.pop(key, None)
        self._primitive_data.pop(key, None)
    
    def __hasitem__(self, key):
        if key in self._meta.fields:
            return True
        return key in self._python_data
    
    def keys(self):
        #TODO more dictionary like functionality
        return set(self._primitive_data.keys() + self._meta.fields.keys())
    
    def dot_notation(self, notation):
        return self.dot_notation_to_value(notation, self)
    
    def dot_notation_set_value(self, notation, value, parent=None):
        from fields import SchemaField
        field = SchemaField(schema=type(self))
        return field.dot_notation_set_value(notation, value, self)
    
    def dot_notation_to_value(self, notation, parent):
        from fields import SchemaField
        field = SchemaField(schema=type(self))
        return field.dot_notation_to_value(notation, parent)
    
    @classmethod
    def dot_notation_to_field(cls, notation):
        from fields import SchemaField
        field = SchemaField(schema=cls)
        return field.dot_notation_to_field(notation)

class DocumentBase(SchemaBase):
    def __new__(cls, name, bases, attrs):
        new_class = SchemaBase.__new__(cls, name, bases, attrs)
        if 'objects' not in attrs:
            objects = Manager()
            objects.contribute_to_class(new_class, 'objects')
        
        if not new_class._meta.virtual and not new_class._meta.proxy:
            backend = get_document_backend()
            backend.register_document(new_class)
        return new_class

class Document(Schema):
    __metaclass__ = DocumentBase
    
    def get_id(self):
        backend = self._meta.get_backend()
        return backend.get_id(self._primitive_data)
    
    pk = property(get_id)
    
    def save(self):
        created = not self.pk
        pre_save.send(sender=type(self), instance=self)
        backend = self._meta.get_backend()
        data = type(self).to_primitive(self)
        backend.save(self._meta.collection, data)
        for value in self.objects.get_indexes().itervalues():
            value.on_document_save(self)
        post_save.send(sender=type(self), instance=self, created=created)
        
    def delete(self):
        pre_delete.send(sender=type(self), instance=self)
        backend = self._meta.get_backend()
        backend.delete(self._meta.collection, self.get_id())
        for value in self.objects.get_indexes().itervalues():
            value.on_document_delete(self)
        post_delete.send(sender=type(self), instance=self)
    
    def serializable_value(self, field_name):
        try:
            field = self._meta.get_field_by_name(field_name)[0]
        except FieldDoesNotExist:
            return getattr(self, field_name)
        return getattr(self, field.attname)
    
    def __str__(self):
        if hasattr(self, '__unicode__'):
            return force_unicode(self).encode('utf-8')
        return '%s object' % self.__class__.__name__

class UserMeta(object):
    def __init__(self, **kwargs):
        for key, value in kwargs.iteritems():
            setattr(self, key, value)

def create_schema(name, fields, module='dockit.models'):
    attrs = SortedDict(fields)
    attrs['__module__'] = module
    return SchemaBase.__new__(SchemaBase, name, (Schema,), attrs)

def create_document(name, fields, module='dockit.models', collection=None):
    attrs = SortedDict(fields)
    attrs['__module__'] = module
    if collection:
       attrs['Meta'] = UserMeta(collection=collection)
    return DocumentBase.__new__(DocumentBase, name, (Document,), attrs)

