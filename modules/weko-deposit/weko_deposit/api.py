# -*- coding: utf-8 -*-
#
# This file is part of WEKO3.
# Copyright (C) 2017 National Institute of Informatics.
#
# WEKO3 is free software; you can redistribute it
# and/or modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# WEKO3 is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with WEKO3; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place, Suite 330, Boston,
# MA 02111-1307, USA.

"""Weko Deposit API."""
import copy
import uuid
from datetime import datetime, timezone

import redis
from dictdiffer import patch
from dictdiffer.merge import Merger, UnresolvedConflictsException
from flask import abort, current_app, has_request_context, json, request, \
    session
from flask_security import current_user
from invenio_db import db
from invenio_deposit.api import Deposit, index, preserve
from invenio_deposit.errors import MergeConflict
from invenio_files_rest.models import Bucket, MultipartObject, ObjectVersion, \
    Part
from invenio_i18n.ext import current_i18n
from invenio_indexer.api import RecordIndexer
from invenio_pidrelations.contrib.records import RecordDraft
from invenio_pidrelations.contrib.versioning import PIDVersioning
from invenio_pidrelations.serializers.utils import serialize_relations
from invenio_pidstore.errors import PIDDoesNotExistError, PIDInvalidAction
from invenio_pidstore.models import PersistentIdentifier, PIDStatus
from invenio_records.models import RecordMetadata
from invenio_records_files.api import FileObject, Record
from invenio_records_files.models import RecordsBuckets
from invenio_records_rest.errors import PIDResolveRESTError
from simplekv.memory.redisstore import RedisStore
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.attributes import flag_modified
from weko_index_tree.api import Indexes
from weko_records.api import FeedbackMailList, ItemsMetadata, ItemTypes
from weko_records.models import ItemMetadata
from weko_records.utils import get_all_items, get_attribute_value_all_items, \
    get_options_and_order_list, json_loader, set_timestamp
from weko_user_profiles.models import UserProfile

from .config import WEKO_DEPOSIT_BIBLIOGRAPHIC_INFO, \
    WEKO_DEPOSIT_BIBLIOGRAPHIC_INFO_KEY, \
    WEKO_DEPOSIT_BIBLIOGRAPHIC_INFO_SYS_KEY, WEKO_DEPOSIT_SYS_CREATOR_KEY
from .pidstore import get_latest_version_id, weko_deposit_fetcher, \
    weko_deposit_minter
from .signals import item_created

PRESERVE_FIELDS = (
    '_deposit',
    '_buckets',
    '_files',
    '_internal',
    '_oai',
    'relations',
    'owners',
    'recid',
    'conceptrecid',
    'conceptdoi',
)


class WekoFileObject(FileObject):
    """Extend FileObject for detail page."""

    def __init__(self, obj, data):
        """Bind to current bucket."""
        self.obj = obj
        self.data = data
        self.info()

    def info(self):
        """Info."""
        super(WekoFileObject, self).dumps()
        self.data.update(self.obj.file.json)
        if hasattr(self, 'filename'):
            # If the record has not been set into an index, then the attr
            # 'filename' will not exist
            index = self['filename'].rfind('.')
            self['filename'] = self['filename'][:index]
        return self.data


class WekoIndexer(RecordIndexer):
    """Provide an interface for indexing records in Elasticsearch."""

    def get_es_index(self):
        """Elastic search settings."""
        self.es_index = current_app.config['SEARCH_UI_SEARCH_INDEX']
        self.es_doc_type = current_app.config['INDEXER_DEFAULT_DOCTYPE']
        self.file_doc_type = current_app.config['INDEXER_FILE_DOC_TYPE']

    def upload_metadata(self, jrc, item_id, revision_id):
        """Upload the item data to ElasticSearch.

        :param jrc:
        :param item_id: item id.
        """
        # delete the item when it is exist
        # if self.client.exists(id=str(item_id), index=self.es_index,
        #                       doc_type=self.es_doc_type):
        #     self.client.delete(id=str(item_id), index=self.es_index,
        #                        doc_type=self.es_doc_type)
        full_body = dict(id=str(item_id),
                         index=self.es_index,
                         doc_type=self.es_doc_type,
                         version=revision_id + 1,
                         version_type=self._version_type,
                         body=jrc)

        if 'content' in jrc:  # Only pass through pipeline if file exists
            full_body['pipeline'] = 'item-file-pipeline'

        self.client.index(**full_body)

    def delete_file_index(self, body, parent_id):
        """Delete file index in Elastic search.

        :param body:
        :param parent_id: Parent item id.
        """
        for lst in body:
            try:
                self.client.delete(id=str(lst),
                                   index=self.es_index,
                                   doc_type=self.file_doc_type,
                                   routing=parent_id)
            except BaseException:
                pass

    def update_publish_status(self, record):
        """Update publish status."""
        self.get_es_index()
        pst = 'publish_status'
        body = {'doc': {pst: record.get(pst)}}
        return self.client.update(
            index=self.es_index,
            doc_type=self.es_doc_type,
            id=str(record.id),
            body=body
        )

    def update_relation_version_is_last(self, version):
        """Update relation version is_last."""
        self.get_es_index()
        pst = 'relation_version_is_last'
        body = {'doc': {pst: version.get('is_last')}}
        return self.client.update(
            index=self.es_index,
            doc_type=self.es_doc_type,
            id=str(version.get('id')),
            body=body
        )

    def update_path(self, record, update_revision=True):
        """Update path."""
        self.get_es_index()
        path = 'path'
        body = {
            'doc': {
                path: record.get(path),
                '_updated': datetime.utcnow().replace(
                    tzinfo=timezone.utc).isoformat()
            }
        }
        if update_revision:
            return self.client.update(
                index=self.es_index,
                doc_type=self.es_doc_type,
                id=str(record.id),
                version=record.revision_id,
                body=body
            )
        else:
            return self.client.update(
                index=self.es_index,
                doc_type=self.es_doc_type,
                id=str(record.id),
                body=body
            )

    def index(self, record):
        """Index a record.

        :param record: Record instance.
        """
        self.get_es_index()

    def delete(self, record):
        """Delete a record.

        Not utilized.

        :param record: Record instance.
        """
        self.get_es_index()

        self.client.delete(id=str(record.id),
                           index=self.es_index,
                           doc_type=self.es_doc_type)

    def get_count_by_index_id(self, tree_path):
        """Get count by index id.

        :param tree_path: Tree_path instance.
        """
        search_query = {
            'query': {
                'term': {
                    'path.tree': tree_path
                }
            }
        }
        self.get_es_index()
        search_result = self.client.count(index=self.es_index,
                                          doc_type=self.es_doc_type,
                                          body=search_query)
        return search_result.get('count')

    def get_pid_by_es_scroll(self, path):
        """Get pid by es scroll.

        :param path:
        :return: _scroll_id
        """
        search_query = {
            "query": {
                "match": {
                    "path.tree": path
                }
            },
            "_source": "_id",
            "size": 3000
        }

        def get_result(result):
            if result:
                hit = result['hits']['hits']
                if hit:
                    return [h.get('_id') for h in hit]
                else:
                    return None
            else:
                return None

        ind, doc_type = self.record_to_index({})
        search_result = self.client.search(index=ind, doc_type=doc_type,
                                           body=search_query, scroll='1m')
        if search_result:
            res = get_result(search_result)
            scroll_id = search_result['_scroll_id']
            if res:
                yield res
                while res:
                    res = self.client.scroll(scroll_id=scroll_id, scroll='1m')
                    yield res

            self.client.clear_scroll(scroll_id=scroll_id)

    def update_feedback_mail_list(self, feedback_mail):
        """Update feedback mail info.

        :param feedback_mail: mail list in json format.
        :return: _feedback_mail_id.
        """
        self.get_es_index()
        pst = 'feedback_mail_list'
        body = {'doc': {pst: feedback_mail.get('mail_list')}}
        return self.client.update(
            index=self.es_index,
            doc_type=self.es_doc_type,
            id=str(feedback_mail.get('id')),
            body=body
        )

    def update_jpcoar_identifier(self, dc, item_id):
        """Update JPCOAR meta data item."""
        self.get_es_index()
        body = {'doc': {'_item_metadata': dc}}
        return self.client.update(
            index=self.es_index,
            doc_type=self.es_doc_type,
            id=str(item_id),
            body=body
        )


class WekoDeposit(Deposit):
    """Define API for changing deposit state."""

    indexer = WekoIndexer()

    deposit_fetcher = staticmethod(weko_deposit_fetcher)

    deposit_minter = staticmethod(weko_deposit_minter)

    data = None
    jrc = None
    is_edit = False

    @property
    def item_metadata(self):
        """Return the Item metadata."""
        return ItemsMetadata.get_record(self.id).dumps()

    def is_published(self):
        """Check if deposit is published."""
        return self['_deposit'].get('pid') is not None

    @preserve(fields=('_deposit', '$schema'))
    def merge_with_published(self):
        """Merge changes with latest published version."""
        pid, first = self.fetch_published()
        lca = first.revisions[self['_deposit']['pid']['revision_id']]
        # ignore _deposit and $schema field
        args = [lca.dumps(), first.dumps(), self.dumps()]
        for arg in args:
            if '$schema' in arg:
                del arg['$schema']
            if '_deposit' in arg:
                del arg['_deposit']
        args.append({})
        m = Merger(*args)
        try:
            m.run()
        except UnresolvedConflictsException:
            raise MergeConflict()
        return patch(m.unified_patches, lca)

    def _publish_new(self, id_=None):
        """Override the publish new to avoid creating multiple pids."""
        id_ = id_ or uuid.uuid4()
        record_pid = PersistentIdentifier.query.filter_by(
            pid_type='recid', object_uuid=self.id).first()

        self['_deposit']['pid'] = {
            'type': record_pid.pid_type,
            'value': record_pid.pid_value,
            'revision_id': 0,
        }

        data = dict(self.dumps())
        data['$schema'] = self.record_schema

        with self._process_files(id_, data):
            record = self.published_record_class.create(data, id_=id_)

        return record

    def publish(self, pid=None, id_=None):
        """Publish the deposit."""
        if not self.data:
            self.data = self.get('_deposit', {})
        if 'control_number' in self:
            self.pop('control_number')
        if '$schema' not in self:
            self['$schema'] = current_app.extensions['invenio-jsonschemas'].\
                path_to_url(current_app.config['DEPOSIT_DEFAULT_JSONSCHEMA'])
        self.is_edit = True
        try:
            deposit = super(WekoDeposit, self).publish(pid, id_)

            # update relation version current to ES
            pid = PersistentIdentifier.query.filter_by(
                pid_type='recid', object_uuid=self.id).first()
            relations = serialize_relations(pid)
            if relations and 'version' in relations:
                relations_ver = relations['version'][0]
                relations_ver['id'] = pid.object_uuid
                relations_ver['is_last'] = relations_ver.get('index') == 0
                self.indexer.update_relation_version_is_last(relations_ver)
            return deposit
        except SQLAlchemyError as ex:
            current_app.logger.debug(ex)
            db.session.rollback()
            return None

    @classmethod
    def create(cls, data, id_=None, recid=None):
        """Create a deposit.

        Adds bucket creation immediately on deposit creation.
        """
        if '$schema' in data:
            data.pop('$schema')

        bucket = Bucket.create(
            quota_size=current_app.config['WEKO_BUCKET_QUOTA_SIZE'],
            max_file_size=current_app.config['WEKO_MAX_FILE_SIZE'],
        )
        data['_buckets'] = {'deposit': str(bucket.id)}

        # save user_name & display name.
        if current_user and current_user.is_authenticated:
            user = UserProfile.get_by_userid(current_user.get_id())
            if '_deposit' in data:
                data['_deposit']['owners_ext'] = {
                    'username': user._username if user else '',
                    'displayname': user._displayname if user else '',
                    'email': current_user.email
                }

        if recid:
            deposit = super(WekoDeposit, cls).create(
                data,
                id_=id_,
                recid=recid
            )
        else:
            deposit = super(WekoDeposit, cls).create(data, id_=id_)

        if data.get('_deposit'):
            record_id = str(data['_deposit']['id'])
        parent_pid = PersistentIdentifier.create(
            'parent',
            'parent:{0}'.format(record_id),
            object_type='rec',
            object_uuid=id_,
            status=PIDStatus.REGISTERED
        )

        RecordsBuckets.create(record=deposit.model, bucket=bucket)

        recid = PersistentIdentifier.get('recid', record_id)
        depid = PersistentIdentifier.get('depid', record_id)
        PIDVersioning(parent=parent_pid).insert_draft_child(child=recid)
        RecordDraft.link(recid, depid)

        return deposit

    @preserve(result=False, fields=PRESERVE_FIELDS)
    def update(self, *args, **kwargs):
        """Update only drafts."""
        self['_deposit']['status'] = 'draft'
        if len(args) > 1:
            dc = self.convert_item_metadata(args[0], args[1])
        else:
            dc = self.convert_item_metadata(args[0])
        super(WekoDeposit, self).update(dc)
#        if 'pid' in self['_deposit']:
#            self['_deposit']['pid']['revision_id'] += 1
        try:
            if has_request_context():
                if current_user:
                    user_id = current_user.get_id()
                else:
                    user_id = -1
                item_created.send(
                    current_app._get_current_object(),
                    user_id=user_id,
                    item_id=self.pid,
                    item_title=self.data['title']
                )
        except BaseException:
            abort(500, 'MAPPING_ERROR')

    @preserve(result=False, fields=PRESERVE_FIELDS)
    def clear(self, *args, **kwargs):
        """Clear only drafts."""
        if self['_deposit']['status'] != 'draft':
            return
        super(WekoDeposit, self).clear(*args, **kwargs)

    @index(delete=True)
    def delete(self, force=True, pid=None):
        """Delete deposit.

        Status required: ``'draft'``.

        :param force: Force deposit delete.  (Default: ``True``)
        :param pid: Force pid object.  (Default: ``None``)
        :returns: A new Deposit object.
        """
        # Delete the recid
        recid = PersistentIdentifier.get(
            pid_type='recid', pid_value=self.pid.pid_value)

        if recid.status == PIDStatus.RESERVED:
            db.session.delete(recid)

        # if this item has been deleted
        self.delete_es_index_attempt(recid)

        # Completely remove bucket
        bucket = self.files.bucket
        with db.session.begin_nested():
            # Remove Record-Bucket link
            RecordsBuckets.query.filter_by(record_id=self.id).delete()
            mp_q = MultipartObject.query_by_bucket(bucket)
            # Remove multipart objects
            Part.query.filter(
                Part.upload_id.in_(mp_q.with_entities(
                    MultipartObject.upload_id).subquery())
            ).delete(synchronize_session='fetch')
            mp_q.delete(synchronize_session='fetch')
        bucket.locked = False
        bucket.remove()

        return super(Deposit, self).delete()

    def commit(self, *args, **kwargs):
        """Store changes on current instance in database and index it."""
        super(WekoDeposit, self).commit(*args, **kwargs)
        if self.data and len(self.data):
            # save item metadata
            self.save_or_update_item_metadata()

            if self.jrc and len(self.jrc):
                # upload item metadata to Elasticsearch
                set_timestamp(self.jrc, self.created, self.updated)

                # Get file contents
                self.get_content_files()

                # upload file content to Elasticsearch
                self.indexer.upload_metadata(self.jrc, self.pid.object_uuid,
                                             self.revision_id)

                # remove large base64 files for release memory
                if self.jrc.get('content'):
                    for content in self.jrc['content']:
                        if content.get('file'):
                            del content['file']

        # fix schema url
        record = RecordMetadata.query.get(self.pid.object_uuid)
        if record and record.json and '$schema' in record.json:
            record.json.pop('$schema')
            flag_modified(record, 'json')
            db.session.merge(record)

    def newversion(self, pid=None):
        """Create a new version deposit."""
        deposit = None
        try:
            if not self.is_published():
                raise PIDInvalidAction()

            # Check that there is not a newer draft version for this record
            # and this is the latest version
            pv = PIDVersioning(child=pid)
            if pv.exists and not pv.draft_child:  # and pid == pv.last_child:
                # the latest record: item without version ID
                last_pid = pid  # pv.last_child
                # Get copy of the latest record
                latest_record = WekoDeposit.get_record(last_pid.object_uuid)
                if latest_record:
                    data = latest_record.dumps()
                    owners = data['_deposit']['owners']
                    bucket = data['_buckets']
                    keys_to_remove = ('_deposit', 'doi', '_oai',
                                      '_files', '_buckets', '$schema')
                    for k in keys_to_remove:
                        data.pop(k, None)

                    # attaching version ID
                    recid = '{0}.{1}' . format(
                        last_pid.pid_value,
                        get_latest_version_id(last_pid.pid_value))
                    # NOTE: We call the superclass `create()` method, because
                    # we don't want a new empty bucket, but
                    # an unlocked snapshot of the old record's bucket.
                    deposit = super(WekoDeposit, self).create(data,
                                                              recid=recid)
                    # Injecting owners is required in case of creating new
                    # version this outside of request context
                    deposit['_deposit']['owners'] = owners
                    deposit['_buckets'] = {'deposit': bucket['deposit']}

                    recid = PersistentIdentifier.get(
                        'recid', str(data['_deposit']['id']))
                    depid = PersistentIdentifier.get(
                        'depid', str(data['_deposit']['id']))

                    PIDVersioning(
                        parent=pv.parent).insert_draft_child(
                        child=recid)
                    RecordDraft.link(recid, depid)

                    index = {'index': self.get('path', []),
                             'actions': self.get('publish_status')}
                    if 'activity_info' in session:
                        del session['activity_info']
                    item_metadata = ItemsMetadata.get_record(
                        last_pid.object_uuid).dumps()
                    item_metadata.pop('id', None)
                    args = [index, item_metadata]
                    deposit.update(*args)
                    deposit.commit()
            return deposit
        except SQLAlchemyError as ex:
            current_app.logger.debug(ex)
            db.session.rollback()
            return None

    def get_content_files(self):
        """Get content file metadata."""
        contents = []
        fmd = self.get_file_data()
        if fmd:
            for file in self.files:
                if isinstance(fmd, list):
                    for lst in fmd:
                        if file.obj.key == lst.get('filename'):
                            lst.update({'mimetype': file.obj.mimetype})

                            # update file_files's json
                            file.obj.file.update_json(lst)

                            # upload file metadata to Elasticsearch
                            try:
                                file_size_max = current_app.config[
                                    'WEKO_MAX_FILE_SIZE_FOR_ES']
                                mimetypes = current_app.config[
                                    'WEKO_MIMETYPE_WHITELIST_FOR_ES']
                                if file.obj.file.size <= file_size_max and \
                                        file.obj.mimetype in mimetypes:

                                    content = lst.copy()
                                    content.update(
                                        {"file": file.obj.file.read_file(lst)})
                                    contents.append(content)

                            except Exception as e:
                                abort(500, '{}'.format(str(e)))
                            break
            self.jrc.update({'content': contents})

    def get_file_data(self):
        """Get file data."""
        file_data = []
        for key in self.data:
            if isinstance(self.data.get(key), list):
                for item in self.data.get(key):
                    if (isinstance(item, dict) or isinstance(item, list)) \
                            and 'filename' in item:
                        file_data.extend(self.data.get(key))
                        break
        return file_data

    def save_or_update_item_metadata(self):
        """Save or update item metadata.

        Save when register a new item type, Update when edit an item
        type.
        """
        if current_user:
            current_user_id = current_user.get_id()
        else:
            current_user_id = '1'
        if current_user_id:
            dc_owner = self.data.get("owner", None)
            if not dc_owner:
                self.data.update(dict(owner=current_user_id))

        if ItemMetadata.query.filter_by(id=self.id).first():
            obj = ItemsMetadata.get_record(self.id)
            obj.update(self.data)
            obj.commit()
        else:
            ItemsMetadata.create(self.data, id_=self.pid.object_uuid,
                                 item_type_id=self.get('item_type_id'))

    def delete_old_file_index(self):
        """Delete old file index before file upload when edit an item."""
        if self.is_edit:
            lst = ObjectVersion.get_by_bucket(
                self.files.bucket, True).filter_by(is_head=False).all()
            klst = []
            for obj in lst:
                if obj.file_id:
                    klst.append(obj.file_id)
            if klst:
                self.indexer.delete_file_index(klst, self.pid.object_uuid)

    def convert_item_metadata(self, index_obj, data=None):
        """Convert Item Metadat.

        1. Convert Item Metadata
        2. Inject index tree id to dict
        3. Set Publish Status
        :param index_obj:
        :return: dc
        """
        # if this item has been deleted
        self.delete_es_index_attempt(self.pid)

        try:
            if not data:
                datastore = RedisStore(redis.StrictRedis.from_url(
                    current_app.config['CACHE_REDIS_URL']))
                cache_key = current_app.config[
                    'WEKO_DEPOSIT_ITEMS_CACHE_PREFIX'].format(
                    pid_value=self.pid.pid_value)

                data_str = datastore.get(cache_key)
                datastore.delete(cache_key)
                data = json.loads(data_str.decode('utf-8'))
        except BaseException:
            abort(500, 'Failed to register item')
        # Get index path
        index_lst = index_obj.get('index', [])
        # Prepare index id list if the current index_lst is a path list
        if index_lst:
            index_id_lst = []
            for _index in index_lst:
                indexes = str(_index).split('/')
                index_id_lst.append(indexes[len(indexes) - 1])
            index_lst = index_id_lst

        plst = Indexes.get_path_list(index_lst)

        if not plst or len(index_lst) != len(plst):
            raise PIDResolveRESTError(
                description='Any tree index has been deleted')

        index_lst.clear()
        for lst in plst:
            index_lst.append(lst.path)

        # convert item meta data
        try:
            dc, jrc, is_edit = json_loader(data, self.pid)
            dc['publish_date'] = data.get('pubdate')
            dc['title'] = [data.get('title')]
            dc['relation_version_is_last'] = True
            self.data = data
            self.jrc = jrc
            self.is_edit = is_edit
        except BaseException:
            abort(500, 'MAPPING_ERROR')

        # Save Index Path on ES
        jrc.update(dict(path=index_lst))
        # add at 20181121 start
        sub_sort = {}
        for pth in index_lst:
            # es setting
            sub_sort[pth[-13:]] = ""
#        jrc.update(dict(custom_sort=sub_sort))
#        dc.update(dict(custom_sort=sub_sort))
        dc.update(dict(path=index_lst))
        pubs = '1'
        actions = index_obj.get('actions')
        if actions == 'publish' or actions == '0':
            pubs = '0'
        elif 'id' in data:
            recid = PersistentIdentifier.query.filter_by(
                pid_type='recid', pid_value=data['id']).first()
            rec = RecordMetadata.query.filter_by(id=recid.object_uuid).first()
            pubs = rec.json['publish_status']

        ps = dict(publish_status=pubs)
        jrc.update(ps)
        dc.update(ps)
        return dc

    @classmethod
    def delete_by_index_tree_id(cls, path):
        """Delete by index tree id."""
        # first update target pid when index tree id was deleted
        # if cls.update_pid_by_index_tree_id(cls, path):
        #    from .tasks import delete_items_by_id
        #    delete_items_by_id.delay(path)
        obj_ids = next(cls.indexer.get_pid_by_es_scroll(path))
        try:
            for obj_uuid in obj_ids:
                r = RecordMetadata.query.filter_by(id=obj_uuid).first()
                try:
                    r.json['path'].remove(path)
                    flag_modified(r, 'json')
                except BaseException:
                    pass
                if not r.json['path']:
                    from weko_records_ui.utils import soft_delete
                    soft_delete(obj_uuid)
            db.session.commit()
        except Exception as ex:
            db.session.rollback()
            raise ex

    @classmethod
    def update_by_index_tree_id(cls, path, target):
        """Update by index tree id."""
        # update item path only
        from .tasks import update_items_by_id
        update_items_by_id.delay(path, target)

    def update_pid_by_index_tree_id(self, path):
        """Update pid by index tree id.

        :param path:
        :return: True: process success False: process failed
        """
        p = PersistentIdentifier
        try:
            dt = datetime.utcnow()
            with db.session.begin_nested():
                for result in self.indexer.get_pid_by_es_scroll(path):
                    db.session.query(p). \
                        filter(p.object_uuid.in_(result),
                               p.object_type == 'rec').\
                        update({p.status: 'D', p.updated: dt},
                               synchronize_session=False)
                    result.clear()
            db.session.commit()
            return True
        except Exception:
            db.session.rollback()
            return False

    def update_item_by_task(self, *args, **kwargs):
        """Update item by task."""
        return super(Deposit, self).commit(*args, **kwargs)

    def delete_es_index_attempt(self, pid):
        """Delete es index attempt."""
        # if this item has been deleted
        if pid.status == PIDStatus.DELETED:
            # attempt to delete index on es
            try:
                self.indexer.delete(self)
            except BaseException:
                pass
            raise PIDResolveRESTError(description='This item has been deleted')

    def update_feedback_mail(self):
        """Index feedback mail list."""
        item_id = self.id
        mail_list = FeedbackMailList.get_mail_list_by_item_id(item_id)
        if mail_list:
            feedback_mail = {
                "id": item_id,
                "mail_list": mail_list
            }
            self.indexer.update_feedback_mail_list(feedback_mail)

    def update_jpcoar_identifier(self):
        """
        Update JPCOAR meta data item.

        Update JPCOAR meta data item for grant DOI which added at the
        Identifier Grant screen.
        """
        obj = ItemsMetadata.get_record(self.id)
        attrs = ['attribute_value_mlt',
                 'item_1551265147138',
                 'item_1551265178780']
        dc = {
            attrs[1]: {attrs[0]: obj.get(attrs[1])},
            attrs[2]: {attrs[0]: [obj.get(attrs[2])]}
        }
        self.indexer.update_jpcoar_identifier(dc, self.id)
        record = RecordMetadata.query.get(self.id)
        if record and record.json:
            try:
                with db.session.begin_nested():
                    record.json[attrs[1]][attrs[0]] = obj.get(attrs[1])
                    record.json[attrs[2]][attrs[0]] = [obj.get(attrs[2])]
                    flag_modified(record, 'json')
                    db.session.merge(record)
                db.session.commit()
            except Exception as ex:
                current_app.logger.debug(ex)
                db.session.rollback()

    def merge_data_to_record_without_version(self, pid):
        """Update changes from record attached version to without version."""
        with db.session.begin_nested():
            # update item_metadata
            index = {'index': self.get('path', []),
                     'actions': self.get('publish_status')}
            item_metadata = ItemsMetadata.get_record(pid.object_uuid).dumps()
            item_metadata.pop('id', None)

            # Get draft bucket's data
            record_bucket = RecordsBuckets.query.filter_by(
                record_id=pid.object_uuid
            ).first()
            bucket = {
                "_buckets": {
                    "deposit": str(record_bucket.bucket_id)
                }
            }

            args = [index, item_metadata]
            self.update(*args)
            # Update '_buckets'
            super(WekoDeposit, self).update(bucket)
            self.commit()
            # update records_metadata
            flag_modified(self.model, 'json')
            db.session.merge(self.model)

        return self.__class__(self.model.json, model=self.model)


class WekoRecord(Record):
    """Extend Record obj for record ui."""

    file_cls = WekoFileObject
    record_fetcher = staticmethod(weko_deposit_fetcher)

    @property
    def pid(self):
        """Return an instance of record PID."""
        pid = self.record_fetcher(self.id, self)
        obj = PersistentIdentifier.get(pid.pid_type, pid.pid_value)
        return obj

    @property
    def pid_recid(self):
        """Return an instance of record PID."""
        pid = self.record_fetcher(self.id, self)
        obj = PersistentIdentifier.get('recid', pid.pid_value)
        return obj

    @property
    def navi(self):
        """Return the path name."""
        navs = Indexes.get_path_name(self.get('path', []))

        community = request.args.get('community', None)
        if not community:
            return navs

        from weko_workflow.api import GetCommunity
        comm = GetCommunity.get_community_by_id(community)
        comm_navs = [item for item in navs if str(
            comm.index.id) in item.path.split('/')]
        return comm_navs

    @property
    def item_type_info(self):
        """Return the information of item type."""
        item_type = ItemTypes.get_by_id(self.get('item_type_id'))
        return '{}({})'.format(item_type.item_type_name.name, item_type.tag)

    @property
    def items_show_list(self):
        """Return the item show list."""
        try:

            items = []
            solst, meta_options = get_options_and_order_list(
                self.get('item_type_id'))

            for lst in solst:
                key = lst[0]

                val = self.get(key)
                option = meta_options.get(key, {}).get('option')
                if not val or not option:
                    continue

                hidden = option.get("hidden")
                if hidden:
                    items.append({
                        'attribute_name_hidden': val.get('attribute_name')
                    })
                    continue

                mlt = val.get('attribute_value_mlt')
                if mlt is not None:
                    nval = dict()
                    nval['attribute_name'] = val.get('attribute_name')
                    nval['attribute_type'] = val.get('attribute_type')
                    if nval['attribute_name'] == 'Reference' \
                            or nval['attribute_type'] == 'file':
                        nval['attribute_value_mlt'] = \
                            get_all_items(copy.deepcopy(mlt),
                                          copy.deepcopy(solst), True)
                    else:
                        is_author = nval['attribute_type'] == 'creator'
                        from weko_gridlayout.utils import get_register_language
                        if is_author:
                            list_lang = []
                            for lang in get_register_language():
                                list_lang.append(lang['lang_code'])
                            creators = []
                            if mlt:
                                for author in mlt:
                                    after_format = {}
                                    self.format_creator(author, after_format,
                                                        current_i18n.language,
                                                        list_lang.copy())
                                    identifiers = WEKO_DEPOSIT_SYS_CREATOR_KEY[
                                        'identifiers']
                                    creator_mails = WEKO_DEPOSIT_SYS_CREATOR_KEY[
                                        'creator_mails']
                                    if identifiers in author:
                                        after_format[identifiers] = author[
                                            identifiers]
                                    if creator_mails in author:
                                        after_format[creator_mails] = author[
                                            creator_mails]
                                    creators.append(after_format)
                            nval['attribute_value_mlt'] = creators
                        elif self._is_bibliographic(mlt):
                            nval['attribute_value_mlt'] = \
                                self._get_bibliographic_list(copy.deepcopy(mlt))
                        else:
                            nval['attribute_value_mlt'] = \
                                get_attribute_value_all_items(
                                    copy.deepcopy(mlt),
                                    copy.deepcopy(solst),
                                    is_author)
                    items.append(nval)
                else:
                    items.append(val)

            return items
        except BaseException:
            abort(500)

    def _is_bibliographic(self, meta_data):
        """Check bibliographic information.

        :param meta_data:
        """
        def check_key(_meta_data):
            for key in WEKO_DEPOSIT_BIBLIOGRAPHIC_INFO_SYS_KEY:
                if key in _meta_data:
                    return True
            return False

        if isinstance(meta_data, dict):
            return check_key(meta_data)
        elif isinstance(meta_data, list) and len(meta_data) > 0 and isinstance(
                meta_data[0], dict):
            return check_key(meta_data[0])

        return False

    def _get_bibliographic_list(self, bibliographic_list_meta_data):
        """
        Get bibliographic information list.

        :rtype: object
        """

        def get_bibliographic(_bibliographic):
            """
            Get bibliographic information data.

            :param _bibliographic:
            :return: title_data, magazine, length
            """
            _title_data = []
            if _bibliographic.get('bibliographic_titles'):
                _title_data = get_source_title(
                    _bibliographic.get('bibliographic_titles'))
            _magazine, _length = get_magazine_information(_bibliographic)
            return _title_data, _magazine, _length

        def get_magazine_information(_bibliographic):
            """
            Get magazine information data.

            :param _bibliographic:
            :return:
            """
            magazine_name = WEKO_DEPOSIT_BIBLIOGRAPHIC_INFO
            _magazine = []
            for name in WEKO_DEPOSIT_BIBLIOGRAPHIC_INFO_KEY:
                if name == 'p.':
                    page = get_page_tart_and_page_end(
                        _bibliographic.get('bibliographicPageStart'),
                        _bibliographic.get('bibliographicPageEnd'))
                    if page != '':
                        _magazine.append({name: page})
                elif name == 'bibliographicIssueDates':
                    dates = get_issue_dates(
                        _bibliographic.get('bibliographicIssueDates'))
                    if dates:
                        _magazine.append(
                            {magazine_name.get(name): " ".join(
                                str(x) for x in dates)})
                elif _bibliographic.get(name):
                    _magazine.append(
                        {magazine_name.get(name): _bibliographic.get(name)})
            _length = len(_magazine) if len(_magazine) else 0
            return _magazine, _length

        def get_source_title(source_titles):
            """
            Get source title.

            :param source_titles:
            :return:
            """
            _title_data = []
            for source_title in source_titles:
                title = source_title['bibliographic_titleLang'] + ' : ' if \
                    source_title.get('bibliographic_titleLang') else ''
                title += source_title[
                    'bibliographic_title'] if source_title.get(
                    'bibliographic_title') else ''
                _title_data.append(title)
            return _title_data

        def get_page_tart_and_page_end(page_start, page_end):
            """
            Get page start and page end.

            :param page_start:
            :param page_end:
            :return:
            """
            page = ''
            page += page_start if page_start is not None else ''
            temp = page_end if page == '' else '-' + page_end
            page += temp if page_end else ''

            return page

        def get_issue_dates(issue_dates):
            """
            Get issue dates.

            :param issue_dates:
            :return:
            """
            date = []
            if isinstance(issue_dates, list):
                for issued_date in issue_dates:
                    if issued_date.get(
                        'bibliographicIssueDate') and issued_date.get(
                            'bibliographicIssueDateType') == 'Issued':
                        date.append(
                            issued_date.get('bibliographicIssueDate'))
                return date

        bibliographic_list = []
        for bibliographic in bibliographic_list_meta_data:
            title_data, magazine, length = get_bibliographic(bibliographic)
            bibliographic_list.append({
                'title_attribute_name': title_data,
                'magazine_attribute_name': magazine,
                'length': length
            })
        return bibliographic_list

    def format_creator(self, author, after_format, default_lang, list_lang):
        """Format Creator.

        :param author:
        :param after_format:
        :param default_lang:
        :param list_lang:
        """

        def format_creator_to_show_detail(_author, _default_lang, _parent_key,
                                          _name_lst):
            """Format creator to show detail.

            :param _author:
            :param _default_lang:
            :param _parent_key:
            :param _name_lst:
            """
            # Get creator name to show on item detail.
            name_key = ''
            lang_key = ''
            if _parent_key == WEKO_DEPOSIT_SYS_CREATOR_KEY['creator_names']:
                name_key = WEKO_DEPOSIT_SYS_CREATOR_KEY['creator_name']
                lang_key = WEKO_DEPOSIT_SYS_CREATOR_KEY['creator_lang']
            elif _parent_key == WEKO_DEPOSIT_SYS_CREATOR_KEY['family_names']:
                name_key = WEKO_DEPOSIT_SYS_CREATOR_KEY['family_name']
                lang_key = WEKO_DEPOSIT_SYS_CREATOR_KEY['family_lang']
            elif _parent_key == WEKO_DEPOSIT_SYS_CREATOR_KEY['given_names']:
                name_key = WEKO_DEPOSIT_SYS_CREATOR_KEY['given_name']
                lang_key = WEKO_DEPOSIT_SYS_CREATOR_KEY['given_lang']
            elif _parent_key == WEKO_DEPOSIT_SYS_CREATOR_KEY[
                    'alternative_names']:
                name_key = WEKO_DEPOSIT_SYS_CREATOR_KEY['alternative_name']
                lang_key = WEKO_DEPOSIT_SYS_CREATOR_KEY['alternative_lang']
            if _parent_key in _author:
                lst_value = _author[_parent_key]
                if len(lst_value) > 0:
                    for i in range(len(lst_value)):
                        if lst_value[i] and lst_value[i].get(
                                lang_key) == _default_lang:
                            _name_lst.append(
                                lst_value[i][name_key])
                            break

        def format_creator_to_show_popup(_author, _dict_temp,
                                         _default_lang=None):
            dicts = {}
            arr_item_lang = []
            arr_item = []
            for key, value in _author.items():
                if _default_lang is None:
                    if "creatorNames" == key:
                        dicts[0] = value

                    if key == "familyNames":
                        dicts[1] = value

                    if key == "givenNames":
                        dicts[2] = value

                    if key == "creatorAlternatives":
                        dicts[3] = value

                    if key == "affiliation":
                        dicts[4] = value

                    if key == "nameIdentifiers":
                        dicts[5] = value

                elif "creatorNames" in _author:
                    if "creatorNames" == key:
                        dicts[0] = value

                    if key == "creatorAlternatives":
                        dicts[1] = value

                    if key == "affiliation":
                        dicts[2] = value

                    if key == "nameIdentifiers":
                        dicts[3] = value

                else:
                    if key == "familyNames":
                        dicts[0] = value

                    if key == "givenNames":
                        dicts[1] = value

                    if key == "creatorAlternatives":
                        dicts[2] = value

                    if key == "affiliation":
                        dicts[3] = value

                    if key == "nameIdentifiers":
                        dicts[4] = value

            for key, value in dicts.items():
                for item in value:
                    count_curr_lang = 0
                    for k, v in item.items():
                        if v == _default_lang and 'Lang' in k:
                            count_curr_lang += 1

                    if count_curr_lang > 0:
                        arr_item_lang.append(item)
                    else:
                        count_other_lang = 0
                        for k, v in item.items():
                            if 'Lang' in k:
                                count_other_lang += 1
                        if count_other_lang == 0 and _default_lang is None:
                            arr_item.append(item)
            else:
                _dict_temp[_default_lang] = arr_item_lang
                _dict_temp['NoLanguage'] = arr_item

        name_lst = []
        creator_names = WEKO_DEPOSIT_SYS_CREATOR_KEY['creator_names']
        family_names = WEKO_DEPOSIT_SYS_CREATOR_KEY['family_names']
        given_names = WEKO_DEPOSIT_SYS_CREATOR_KEY['given_names']
        alternative_names = WEKO_DEPOSIT_SYS_CREATOR_KEY['alternative_names']
        list_parent_key = [creator_names, family_names, given_names,
                           alternative_names]
        for parent_key in list_parent_key:
            format_creator_to_show_detail(author, default_lang, parent_key,
                                          name_lst)
            if name_lst:
                break
        # if lst is None when chose default language
        if not name_lst:
            for lang in list_lang:
                for parent_key in list_parent_key:
                    format_creator_to_show_detail(author, lang, parent_key,
                                                  name_lst)
                    if name_lst:
                        break
                if name_lst:
                    break
        # if lst is None when chose priority language
        if not name_lst:
            for parent_key in list_parent_key:
                format_creator_to_show_detail(author, None, parent_key,
                                              name_lst)
                if name_lst:
                    break
        after_format['name'] = name_lst
        list_temp = []
        dict_temp = {}
        ja_kana_lang_code = "ja-Kana"
        # Format data by key is language default.
        format_creator_to_show_popup(author, dict_temp, default_lang)
        if dict_temp:
            list_temp.append(dict_temp)
            dict_temp = {}

        # Format data by key if language is ja-Kana.
        if default_lang == "ja":
            dict_temp = {}
            format_creator_to_show_popup(author, dict_temp, ja_kana_lang_code)
            if dict_temp:
                list_temp.append(dict_temp)
                dict_temp = {}

        # If list_temp not None when get value by default language
        if list_temp:
            list_lang.remove(default_lang)

        # Get value by priority language
        for lang in list_lang:
            format_creator_to_show_popup(author, dict_temp, lang)
            if dict_temp:
                list_temp.append(dict_temp)
                dict_temp = {}
            if lang == 'ja':
                format_creator_to_show_popup(author, dict_temp,
                                             ja_kana_lang_code)
                if dict_temp:
                    list_temp.append(dict_temp)
                    dict_temp = {}
        # Get value have no language
        format_creator_to_show_popup(author, dict_temp)
        if dict_temp:
            list_temp.append(dict_temp)
        after_format.update({'order_lang': list_temp})

    @property
    def pid_doi(self):
        """Return pid_value of doi identifier."""
        return self._get_pid('doi')

    @property
    def pid_cnri(self):
        """Return pid_value of doi identifier."""
        return self._get_pid('hdl')

    @property
    def pid_parent(self):
        """Return pid_value of doi identifier."""
        pid_ver = PIDVersioning(child=self.pid_recid)
        if pid_ver:
            return pid_ver.parents.one_or_none()
        else:
            return None

    @classmethod
    def get_record_by_pid(cls, pid):
        """Get record by pid."""
        pid = PersistentIdentifier.get('depid', pid)
        return cls.get_record(id_=pid.object_uuid)

    @classmethod
    def get_record_with_hps(cls, uuid):
        """Get record with hps."""
        record = cls.get_record(id_=uuid)
        path = []
        path.extend(record.get('path'))
        harvest_public_state = True
        if path:
            harvest_public_state = Indexes.get_harvest_public_state(path)
        return harvest_public_state, record

    @classmethod
    def get_record_cvs(cls, uuid):
        """Get record cvs."""
        record = cls.get_record(id_=uuid)
        path = []
        path.extend(record.get('path'))
        coverpage_state = False
        if path:
            coverpage_state = Indexes.get_coverpage_state(path)
        return coverpage_state

    def _get_pid(self, pid_type):
        """Return pid_value from persistent identifier."""
        try:
            return PersistentIdentifier.query.filter_by(
                pid_type=pid_type,
                object_uuid=self.pid_parent.object_uuid,
                status=PIDStatus.REGISTERED).one_or_none()
        except PIDDoesNotExistError as pid_not_exist:
            current_app.logger.error(pid_not_exist)
        return None
