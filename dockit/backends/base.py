
class BaseDocumentStorage(object):
    _indexers = dict()
    
    @classmethod
    def register_indexer(cls, name, index_cls):
        cls._indexers[name] = index_cls
    
    @classmethod
    def get_indexer(cls, name):
        return cls._indexers[name]

    def register_document(self, document):
        pass
        #for key, field in document._meta.fields.iteritems():
        #    if getattr(field, 'db_index', False):
        #        document.objects.enable_index("equals", key, {'field_name':key})
    
    def save(self, collection, data):
        raise NotImplementedError
    
    def get(self, collection, doc_id):
        raise NotImplementedError
    
    def delete(self, collection, doc_id):
        raise NotImplementedError
    
    def all(self, doc_class, collection):
        raise NotImplementedError
    
    def get_id(self, data):
        return data.get(self.get_id_field_name())
    
    def get_id_field_name(self):
        raise NotImplementedError

