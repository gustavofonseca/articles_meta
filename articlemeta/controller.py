# coding: utf-8
import unicodedata
from datetime import datetime, timedelta
import urlparse
import warnings
import uuid

import pymongo
from xylose.scielodocument import Article, Journal
from decorators import LogHistoryChange

LIMIT = 1000


def get_dbconn(db_dsn):
    """Connects to the MongoDB server and returns a database handler."""

    def _ensure_indexes(db):
        """
        Ensures that an index exists on specified collections.

        Definitions:
        index_by_collection = {
            'collection_name': [
                ('field_name_1', pymongo.DESCENDING),
                ('field_name_2', pymongo.ASCENDING),
                ...
            ],
        }

        Obs:
        Care must be taken when the database is being accessed through multiple clients at once.
        If an index is created using this client and deleted using another,
        any call to ensure_index() within the cache window will fail to re-create the missing index.

        Docs:
        http://api.mongodb.org/python/current/api/pymongo/collection.html#pymongo.collection.Collection.ensure_index
        """
        index_by_collection = {
            'historychanges_article': [
                ('date', pymongo.ASCENDING),
                ('collection', pymongo.ASCENDING),
                ('code', pymongo.ASCENDING),
            ],
            'historychanges_journal': [
                ('date', pymongo.ASCENDING),
                ('collection', pymongo.ASCENDING),
                ('code', pymongo.ASCENDING),
            ],
        }

        for collection, indexes in index_by_collection.iteritems():
            db[collection].ensure_index(indexes)

    db_url = urlparse.urlparse(db_dsn)
    conn = pymongo.Connection(host=db_url.hostname, port=db_url.port)
    db = conn[db_url.path[1:]]
    if db_url.username and db_url.password:
        db.authenticate(db_url.username, db_url.password)
    _ensure_indexes(db)
    return db

class DataBroker(object):
    _dbconn_cache = {}

    def __init__(self, databroker):
        self.db = databroker

    @classmethod
    def from_dsn(cls, db_dsn, reuse_dbconn=False):
        """Returns a DataBroker instance for a given DSN.

        :param db_dsn: Domain Service Name, i.e. mongodb://192.168.1.162:27017/scielo_network
        :reuse_dbconn: (optional) If connections to MongoDB must be reused
        """
        if reuse_dbconn:
            cached_db = cls._dbconn_cache.get(db_dsn)
            if cached_db is None:
                db = get_dbconn(db_dsn)
                cls._dbconn_cache[db_dsn] = db
            else:
                db = cached_db
        else:
            db = get_dbconn(db_dsn)

        return cls(db)

    def _check_article_meta(self, metadata):
        """
            This method will check the given metadata and retrieve
            a new dictionary with some new fields.
        """

        article = Article(metadata)

        issns = set([article.journal.any_issn(priority=u'electronic'),
                    article.journal.any_issn(priority=u'print')])

        metadata['code'] = article.publisher_id
        metadata['code_issue'] = article.publisher_id[1:18]
        metadata['code_title'] = list(issns)
        metadata['collection'] = article.collection_acronym
        metadata['document_type'] = article.document_type
        metadata['publication_year'] = article.publication_date[0:4]
        metadata['validated_scielo'] = 'False'
        metadata['validated_wos'] = 'False'
        metadata['sent_wos'] = 'False'
        metadata['sent_doaj'] = 'False'
        metadata['applicable'] = 'False'
        metadata['_shard_id'] = uuid.uuid4().hex

        if article.doi:
            metadata['doi'] = article.doi

        try:
            metadata['processing_date'] = article.processing_date
        except:
            if article.publication_date > datetime.now().date().isoformat():
                metadata['processing_date'] = datetime.now().date().isoformat()

        return metadata

    def _check_journal_meta(self, metadata):
        """
            This method will check the given metadata and retrieve
            a new dictionary with some new fields.
        """
        journal = Journal(metadata)

        issns = set([journal.any_issn(priority=u'electronic'),
                     journal.any_issn(priority=u'print')])

        metadata['code'] = list(issns)
        metadata['collection'] = journal.collection_acronym

        return metadata

    def _log_changes(self, document_type, code, event, collection=None, date=None):

        if document_type in ['article', 'journal']:
            log_data = {
                'code': code,
                'collection': collection,
                'event': event,
                'date': date or datetime.now().isoformat(),
            }
            log_id = self.db['historychanges_%s' % document_type].insert(log_data)
            return log_id

    def historychanges(self, document_type, collection=None, event=None,
                       code=None, from_date='1500-01-01T00:00:00',
                       until_date=None, limit=LIMIT, offset=0):

        if offset < 0:
            offset = 0

        if limit < 0:
            limit = LIMIT

        fltr = {}
        fltr['date'] = {'$gt': from_date, '$lte': until_date or datetime.now().isoformat()}

        if collection:
            fltr['collection'] = collection

        if event:
            fltr['event'] = event

        if code:
            fltr['code'] = code

        total = self.db['historychanges_%s' % document_type].find(fltr).count()
        data = self.db['historychanges_%s' % document_type].find(fltr).skip(offset).limit(limit).sort("date")

        meta = {
            'limit': limit,
            'offset': offset,
            'filter': fltr,
            'total': total
        }

        objects = [{'date': i['date'], 'code': i['code'], 'collection': i['collection'], 'event': i['event']} for i in data]
        result = {
            'meta': meta,
            'objects': objects
        }
        return result

    def get_journal(self, collection=None, issn=None):

        fltr = {}

        if collection:
            fltr['collection'] = collection

        if issn:
            fltr['code'] = issn

        data = self.db['journals'].find(fltr, {'_id': 0})

        if not data:
            return None

        return [i for i in data]

    @LogHistoryChange(document_type="journal", event_type="delete")
    def delete_journal(self, issn, collection=None):

        fltr = {
            'code': issn,
            'collection': collection
        }

        self.db['journals'].remove(fltr)

        return fltr

    @LogHistoryChange(document_type="journal", event_type="add")
    def add_journal(self, metadata):

        journal = self._check_journal_meta(metadata)

        if not journal:
            return None

        self.db['journals'].update(
            {'code': journal['code'], 'collection': journal['collection']},
            {'$set': journal},
            safe=False,
            upsert=True
        )

        return journal

    def identifiers_collection(self):

        data = self.db['collections'].find({}, {'_id': 0})

        if not data:
            return None

        return [i for i in data]


    def get_collection(self, collection):

        fltr = {'code': collection}

        return self.db['collections'].find_one(fltr, {'_id': 0})

    def collection(self, collection=None):
        """
        DEPRECATED
        """
        warnings.warn("deprecated: replaced by identifiers_collection and get_collection", DeprecationWarning)

        self.get_collection(collection=collection)

    def identifiers_journal(self, collection=None, limit=LIMIT, offset=0):

        if offset < 0:
            offset = 0

        if limit < 0:
            limit = LIMIT

        fltr = {}
        if collection:
            fltr['collection'] = collection

        total = self.db['journals'].find(fltr).count()
        data = self.db['journals'].find(fltr, {'code': 1, 'collection': 1}).skip(offset).limit(limit)

        meta = {'limit': limit,
                'offset': offset,
                'filter': fltr,
                'total': total}

        result = {'meta': meta, 'objects': [{'code': i['code'], 'collection': i['collection']} for i in data]}

        return result

    def identifiers_article(self,
                            collection=None,
                            issn=None,
                            from_date='1500-01-01',
                            until_date=None,
                            limit=LIMIT,
                            offset=0):

        if offset < 0:
            offset = 0

        if limit < 0:
            limit = LIMIT

        fltr = {}
        fltr['processing_date'] = {'$gte': from_date, '$lte': until_date or datetime.now().date().isoformat()}

        if collection:
            fltr['collection'] = collection

        if issn:
            fltr['code_title'] = issn

        total = self.db['articles'].find(fltr).count()
        data = self.db['articles'].find(fltr, {
            'code': 1,
            'collection': 1,
            'processing_date': 1}
        ).skip(offset).limit(limit)

        meta = {'limit': limit,
                'offset': offset,
                'filter': fltr,
                'total': total}

        result = {'meta': meta, 'objects': [{'code': i['code'], 'collection': i['collection'], 'processing_date': i['processing_date']} for i in data]}

        return result

    def identifiers_press_release(self,
                                  collection=None,
                                  issn=None,
                                  from_date='1500-01-01',
                                  until_date=None,
                                  limit=LIMIT,
                                  offset=0):

        if offset < 0:
            offset = 0

        if limit < 0:
            limit = LIMIT

        fltr = {}
        fltr['processing_date'] = {'$gte': from_date, '$lte': until_date or datetime.now().date().isoformat()}

        fltr['document_type'] = u'press-release'

        if collection:
            fltr['collection'] = collection

        if issn:
            fltr['code_title'] = issn

        total = self.db['articles'].find(fltr).count()
        data = self.db['articles'].find(fltr, {
            'code': 1,
            'collection': 1,
            'processing_date': 1}
        ).skip(offset).limit(limit)

        meta = {'limit': limit,
                'offset': offset,
                'filter': fltr,
                'total': total}

        result = {'meta': meta, 'objects': [{'code': i['code'], 'collection': i['collection'], 'processing_date': i['processing_date']} for i in data]}

        return result

    def get_article(self, code, collection=None, replace_journal_metadata=False):
        """
            replace_journal_metadata: replace the content of the title attribute
            that cames with the article record. The content is replaced by the
            oficial and updated journal record. This may be used in cases that
            the developer intent to retrive the must recent journal data instead
            of the journal data recorded when the article was inserted in the
            collection.
        """

        fltr = {'code': code}
        if collection:
            fltr['collection'] = collection

        data = self.db['articles'].find_one(fltr)

        if not data:
            return None

        if replace_journal_metadata:
            journal = self.get_journal(collection=collection, issn=data['title']['v400'][0]['_'])

            if journal and len(journal) != 0:
                data['title'] = journal[0]

        del(data['_id'])

        return data

    def get_articles(self, code, collection=None, replace_journal_metadata=False):

        fltr = {'code': code}
        if collection:
            fltr['collection'] = collection

        data = self.db['articles'].find(fltr, {'_id': 0})

        for article in data:
            if replace_journal_metadata:
                journal = self.get_journal(collection=collection, issn=article['title']['v400'][0]['_'])

                if journal and len(journal) == 1:
                    article['title'] = journal[0]

            yield article

    def exists_article(self, code, collection=None):

        fltr = {'code': code}

        if collection:
            fltr['collection'] = collection

        if self.db['articles'].find(fltr).count() >= 1:
            return True

        return False

    @LogHistoryChange(document_type="article", event_type="delete")
    def delete_article(self, code, collection=None):

        fltr = {
            'code': code,
            'collection': collection
        }

        self.db['articles'].remove(fltr)

        return fltr

    @LogHistoryChange(document_type="article", event_type="add")
    def add_article(self, metadata):

        article = self._check_article_meta(metadata)

        if not article:
            return None

        article['created_at'] = article['processing_date']

        self.db['articles'].update(
            {'code': article['code'], 'collection': article['collection']},
            {'$set': article},
            safe=False,
            upsert=True
        )

        return article

    @LogHistoryChange(document_type="article", event_type="update")
    def update_article(self, metadata):

        article = self._check_article_meta(metadata)

        if not article:
            return None

        self.db['articles'].update(
            {'code': article['code'], 'collection': article['collection']},
            {'$set': article},
            safe=False,
            upsert=True
        )

        return article

    def set_doaj_status(self, code, status):

        self.db['articles'].update(
            {'code': code},
            {'$set': {'sent_doaj': str(status)}},
            safe=False
        )
