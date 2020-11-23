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

"""Weko Search-UI admin."""

import base64
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import uuid
from collections import defaultdict
from datetime import datetime
from functools import reduce
from io import StringIO
from operator import getitem

from flask import abort, current_app, request
from flask_babelex import gettext as _
from invenio_db import db
from invenio_files_rest.models import ObjectVersion
from invenio_i18n.ext import current_i18n
from invenio_oaiharvester.harvester import RESOURCE_TYPE_URI
from invenio_pidrelations.contrib.versioning import PIDVersioning
from invenio_pidstore.errors import PIDDoesNotExistError
from invenio_pidstore.models import PersistentIdentifier
from invenio_records.api import Record
from invenio_records.models import RecordMetadata
from invenio_search import RecordsSearch
from jsonschema import Draft4Validator
from weko_admin.models import SessionLifetime
from weko_authors.utils import check_email_existed
from weko_deposit.api import WekoDeposit, WekoIndexer, WekoRecord
from weko_deposit.pidstore import get_latest_version_id
from weko_handle.api import Handle
from weko_index_tree.api import Indexes
from weko_indextree_journal.api import Journals
from weko_records.api import FeedbackMailList, ItemTypes, Mapping
from weko_records.serializers.utils import get_mapping
from weko_workflow.api import Flow, WorkActivity
from weko_workflow.config import IDENTIFIER_GRANT_LIST, \
    IDENTIFIER_GRANT_SUFFIX_METHOD
from weko_workflow.models import FlowDefine, WorkFlow
from weko_workflow.utils import IdentifierHandle, check_required_data, \
    get_identifier_setting, get_sub_item_value, register_hdl_by_handle, \
    register_hdl_by_item_id, saving_doi_pidstore

from .config import ACCESS_RIGHT_TYPE_URI, DATE_ISO_TEMPLATE_URL, \
    VERSION_TYPE_URI, \
    WEKO_ADMIN_IMPORT_CHANGE_IDENTIFIER_MODE_FILE_EXTENSION, \
    WEKO_ADMIN_IMPORT_CHANGE_IDENTIFIER_MODE_FILE_LANGUAGES, \
    WEKO_ADMIN_IMPORT_CHANGE_IDENTIFIER_MODE_FILE_LOCATION, \
    WEKO_ADMIN_IMPORT_CHANGE_IDENTIFIER_MODE_FIRST_FILE_NAME, \
    WEKO_ADMIN_LIFETIME_DEFAULT, WEKO_FLOW_DEFINE, \
    WEKO_FLOW_DEFINE_LIST_ACTION, WEKO_IMPORT_DOI_TYPE, \
    WEKO_IMPORT_EMAIL_PATTERN, WEKO_IMPORT_PUBLISH_STATUS, \
    WEKO_IMPORT_SUFFIX_PATTERN, WEKO_IMPORT_SYSTEM_ITEMS, WEKO_REPO_USER, \
    WEKO_SYS_USER
from .query import feedback_email_search_factory, item_path_search_factory

err_msg_suffix = 'Suffix of {} can only be used with half-width' \
    + ' alphanumeric characters and half-width symbols "_-.; () /".'


def get_tree_items(index_tree_id):
    """Get tree items."""
    records_search = RecordsSearch()
    records_search = records_search.with_preference_param().params(
        version=False)
    records_search._index[0] = current_app.config['SEARCH_UI_SEARCH_INDEX']
    search_instance, _ = item_path_search_factory(
        None, records_search, index_id=index_tree_id)
    search_result = search_instance.execute()
    rd = search_result.to_dict()
    return rd.get('hits').get('hits')

def delete_records(index_tree_id):
    """Bulk delete records."""
    hits = get_tree_items(index_tree_id)
    for hit in hits:
        recid = hit.get('_id')
        record = Record.get_record(recid)
        if record is not None and record['path'] is not None:
            paths = record['path']
            if len(paths) > 0:
                # Remove the element which matches the index_tree_id
                removed_path = None
                for path in paths:
                    if path.endswith(str(index_tree_id)):
                        removed_path = path
                        paths.remove(path)
                        break

                # Do update the path on record
                record.update({'path': paths})
                record.commit()
                db.session.commit()

                # Indexing
                indexer = WekoIndexer()
                indexer.update_path(record, update_revision=False)

                if len(paths) == 0 and removed_path is not None:
                    from weko_deposit.api import WekoDeposit
                    WekoDeposit.delete_by_index_tree_id(removed_path)
                    Record.get_record(recid).delete()  # flag as deleted
                    db.session.commit()  # terminate the transaction


def get_journal_info(index_id=0):
    """Get journal information.

    :argument
        index_id -- {int} index id
    :return: The object.

    """
    result = {}
    try:
        if index_id == 0:
            return None
        schema_file = os.path.join(
            os.path.abspath(__file__ + "/../../../"),
            'weko-indextree-journal/weko_indextree_journal',
            current_app.config['WEKO_INDEXTREE_JOURNAL_FORM_JSON_FILE'])
        schema_data = json.load(open(schema_file))

        cur_lang = current_i18n.language
        journal = Journals.get_journal_by_index_id(index_id)
        if len(journal) <= 0 or journal.get('is_output') is False:
            return None

        for value in schema_data:
            title = value.get('title_i18n')
            if title is not None:
                data = journal.get(value['key'])
                if data is not None and len(str(data)) > 0:
                    data_map = value.get('titleMap')
                    if data_map is not None:
                        res = [x['name']
                               for x in data_map if x['value'] == data]
                        data = res[0]
                    val = title.get(cur_lang) + '{0}{1}'.format(': ', data)
                    result.update({value['key']: val})
        open_search_uri = request.host_url + journal.get('title_url')
        result.update({'openSearchUrl': open_search_uri})

    except BaseException:
        current_app.logger.error('Unexpected error: ', sys.exc_info()[0])
        abort(500)
    return result


def get_feedback_mail_list():
    """Get tree items."""
    records_search = RecordsSearch()
    records_search = records_search.with_preference_param().params(
        version=False)
    records_search._index[0] = current_app.config['SEARCH_UI_SEARCH_INDEX']
    search_instance = feedback_email_search_factory(None, records_search)
    search_result = search_instance.execute()
    rd = search_result.to_dict()
    return rd.get('aggregations').get('feedback_mail_list')\
        .get('email_list').get('buckets')


def parse_feedback_mail_data(data):
    """Parse data."""
    result = {}
    if data is not None and isinstance(data, list):
        for author in data:
            if author.get('doc_count'):
                email = author.get('key')
                hits = author.get('top_tag_hits').get('hits').get('hits')
                result[email] = {
                    'author_id': '',
                    'item': []
                }
                for index in hits:
                    if not result[email]['author_id']:
                        result[email]['author_id'] = index.get(
                            '_source').get('author_id')
                    result[email]['item'].append(index.get('_id'))
    return result


def check_permission():
    """Check user login is repo_user or sys_user."""
    from flask_security import current_user
    is_permission_user = False
    for role in list(current_user.roles or []):
        if role == WEKO_SYS_USER or role == WEKO_REPO_USER:
            is_permission_user = True

    return is_permission_user


def get_content_workflow(item):
    """Get content workflow.

    :argument
        item    -- {Object PostgreSql} list work flow

    :return
        result  -- {dictionary} content of work flow

    """
    result = dict()
    result['flows_name'] = item.flows_name
    result['id'] = item.id
    result['itemtype_id'] = item.itemtype_id
    result['flow_id'] = item.flow_id
    result['flow_name'] = item.flow_define.flow_name
    result['item_type_name'] = item.itemtype.item_type_name.name

    return result


def set_nested_item(data_dict, map_list, val):
    """Set item in nested dictionary."""
    reduce(getitem, map_list[:-1], data_dict)[map_list[-1]] = val

    return data_dict


def convert_nested_item_to_list(data_dict, map_list):
    """Set item in nested dictionary."""
    a = reduce(getitem, map_list[:-1], data_dict)[map_list[-1]]
    a = list(a.values())
    reduce(getitem, map_list[:-1], data_dict)[map_list[-1]] = a

    return data_dict


def define_default_dict():
    """Define nested dict.

    :return
       return       -- {dict}.
    """
    return defaultdict(define_default_dict)


def defaultify(d: dict) -> dict:
    """Create default dict.

    :argument
        d            -- {dict} current dict.
    :return
        return       -- {dict} default dict.

    """
    if not isinstance(d, dict):
        return d
    return defaultdict(
        define_default_dict,
        {k: defaultify(v) for k, v in d.items()}
    )


def handle_generate_key_path(key) -> list:
    """Handle generate key path.

    :argument
        key     -- {string} string key.
    :return
        return       -- {list} list key path after convert.

    """
    key = key.replace(
        '#.',
        '.'
    ).replace(
        '[',
        '.'
    ).replace(
        ']',
        ''
    ).replace(
        '#',
        '.'
    )
    key_path = key.split(".")
    if len(key_path) > 0 and not key_path[0]:
        del key_path[0]

    return key_path


def parse_to_json_form(data: list) -> dict:
    """Parse set argument to json object.

    :argument
        data    -- {list zip} argument if json object.
    :return
        return  -- {dict} dict after convert argument.

    """
    result = defaultify({})

    def convert_data(pro, path=None):
        """Convert data."""
        if path is None:
            path = []

        term_path = path
        if isinstance(pro, dict):
            list_pro = list(pro.keys())
            for pro_name in list_pro:
                term = list(term_path)
                term.append(pro_name)
                convert_data(pro[pro_name], term)
            if list_pro[0].isnumeric():
                convert_nested_item_to_list(result, term_path)
        else:
            return

    for key, name, value in data:
        key_path = handle_generate_key_path(key)
        name_path = handle_generate_key_path(name)
        if value:
            a = handle_check_identifier(name_path)
            if not a:
                set_nested_item(result, key_path, value)
            else:
                set_nested_item(result, key_path, value)
                a += ' key'
                set_nested_item(result, [a], key_path[1])

    convert_data(result)
    result = json.loads(json.dumps(result))
    return result


def check_import_items(file_content: str, is_change_identifier: bool):
    """Validation importing zip file.

    :argument
        file_content -- content file's name.
        is_change_identifier -- Change Identifier Mode.
    :return
        return       -- PID object if exist.

    """
    file_content_decoded = base64.b64decode(file_content)
    temp_path = tempfile.TemporaryDirectory()
    save_path = "/tmp"

    try:
        # Create temp dir for import data
        import_path = temp_path.name + '/' + \
            datetime.utcnow().strftime(r'%Y%m%d%H%M%S')
        data_path = save_path + '/' + \
            datetime.utcnow().strftime(r'%Y%m%d%H%M%S')
        os.mkdir(data_path)

        with open(import_path + '.zip', 'wb+') as f:
            f.write(file_content_decoded)
        shutil.unpack_archive(import_path + '.zip', extract_dir=data_path)

        data_path += '/data'
        list_record = []
        for tsv_entry in os.listdir(data_path):
            if tsv_entry.endswith('.tsv'):
                list_record.extend(
                    unpackage_import_file(data_path, tsv_entry))
        list_record = handle_check_exist_record(list_record)
        handle_item_title(list_record)
        handle_check_and_prepare_publish_status(list_record)
        handle_check_and_prepare_index_tree(list_record)
        handle_check_and_prepare_feedback_mail(list_record)
        handle_set_change_identifier_flag(
            list_record, is_change_identifier)
        handle_check_cnri(list_record)
        handle_check_doi_ra(list_record)
        handle_check_doi(list_record)
        handle_check_date(list_record)
        return {
            'list_record': list_record,
            'data_path': data_path
        }
    except Exception:
        current_app.logger.error('-' * 60)
        traceback.print_exc(file=sys.stdout)
        current_app.logger.error('-' * 60)
    finally:
        temp_path.cleanup()


def unpackage_import_file(data_path: str, tsv_file_name: str) -> list:
    """Getting record data from TSV file.

    :argument
        file_content -- Content files.
    :return
        return       -- PID object if exist.

    """
    tsv_file_path = '{}/{}'.format(data_path, tsv_file_name)
    data = read_stats_tsv(tsv_file_path)
    list_record = data.get('tsv_data')
    handle_fill_system_item(list_record)
    list_record = handle_validate_item_import(list_record, data.get(
        'item_type_schema', {}
    ))
    return list_record


def read_stats_tsv(tsv_file_path: str) -> dict:
    """Read importing TSV file.

    :argument
        tsv_file_path -- tsv file's url.
    :return
        return       -- PID object if exist.

    """
    result = {
        'error': False,
        'error_code': 0,
        'tsv_data': [],
        'item_type_schema': {}
    }
    tsv_data = []
    item_path = []
    item_path_name = []
    check_item_type = {}
    schema = ''
    with open(tsv_file_path, 'r') as tsvfile:
        for num, row in enumerate(tsvfile, start=1):
            data_row = row.rstrip('\n').split('\t')
            if num == 1:
                if data_row[2] and data_row[2].split('/')[-1]:
                    item_type_id = data_row[2].split('/')[-1]
                    check_item_type = get_item_type(int(item_type_id))
                    schema = data_row[2]
                    if not check_item_type:
                        result['item_type_schema'] = {}
                    else:
                        result['item_type_schema'] = check_item_type['schema']

            elif num == 2:
                item_path = data_row
            elif num == 3:
                item_path_name = data_row
            elif (num == 4 or num == 5) and row.startswith('#'):
                continue
            else:
                data_parse_metadata = parse_to_json_form(
                    zip(item_path, item_path_name, data_row)
                )

                json_data_parse = parse_to_json_form(
                    zip(item_path_name, item_path, data_row)
                )
                if isinstance(check_item_type, dict):
                    item_type_name = check_item_type.get('name')
                    item_type_id = check_item_type.get('item_type_id')
                    tsv_item = dict(
                        **json_data_parse,
                        **data_parse_metadata,
                        **{
                            'item_type_name': item_type_name or '',
                            'item_type_id': item_type_id or '',
                            '$schema': schema if schema else ''
                        }
                    )
                else:
                    tsv_item = dict(**json_data_parse, **data_parse_metadata)
                tsv_data.append(tsv_item)

    result['tsv_data'] = tsv_data
    return result


def handle_validate_item_import(list_record, schema) -> list:
    """Validate item import.

    :argument
        list_record     -- {list} list record import.
        schema     -- {dict} item_type schema.
    :return
        return       -- list_item_error.

    """
    result = []
    v2 = Draft4Validator(schema) if schema else None
    for record in list_record:
        errors = []
        record_id = record.get("id")
        if record_id and (not represents_int(record_id)):
            errors.append(_('Please specify item ID by half-width number.'))
        if record.get('metadata'):
            if v2:
                a = v2.iter_errors(record.get('metadata'))
                errors = errors + [error.message for error in a]
            else:
                errors = errors = errors + \
                    [_('Specified item type does not exist.')]

        item_error = dict(**record, **{
            'errors': errors if len(errors) else None
        })
        result.append(item_error)

    return result


def represents_int(s):
    """Handle check string is int.

    :argument
        s     -- {str} string number.
    :return
        return       -- true if is Int.

    """
    try:
        int(s)
        return True
    except ValueError:
        return False


def get_item_type(item_type_id=0) -> dict:
    """Get item type.

    :param item_type_id: Item type ID. (Default: 0).
    :return: The json object.
    """
    result = None
    if item_type_id > 0:
        itemtype = ItemTypes.get_by_id(item_type_id)
        if itemtype and itemtype.schema \
                and itemtype.item_type_name.name and item_type_id:
            result = {
                'schema': itemtype.schema,
                'name': itemtype.item_type_name.name,
                'item_type_id': item_type_id
            }

    if result is None:
        return {}

    return result


def handle_check_exist_record(list_record) -> list:
    """Check record is exist in system.

    :argument
        list_record -- {list} list record import.
    :return
        return      -- list record has property status.

    """
    result = []
    for item in list_record:
        item = dict(**item, **{
            'status': 'new'
        })
        errors = item.get('errors') or []
        try:
            item_id = item.get('id')
            if item_id:
                item_exist = WekoRecord.get_record_by_pid(item_id)
                if item_exist:
                    if item_exist.pid.is_deleted():
                        item['status'] = None
                        errors.append(_('Item already DELETED'
                                        ' in the system'))
                        item['errors'] = errors
                        result.append(item)
                        continue
                    else:
                        exist_url = request.url_root + \
                            'records/' + item_exist.get('recid')
                        if item.get('uri') == exist_url:
                            _edit_mode = item.get('edit_mode')
                            if not _edit_mode or _edit_mode.lower() \
                                    not in ['keep', 'upgrade']:
                                errors.append(
                                    _('Please specify either \"Keep\"'
                                      ' or "Upgrade".'))
                                item['status'] = None
                            else:
                                item['status'] = _edit_mode.lower()
                        else:
                            errors.append(_('Specified URI and system'
                                            ' URI do not match.'))
                            item['status'] = None
            else:
                item['id'] = None
                if item.get('uri'):
                    errors.append(_('Item ID does not match the'
                                    + ' specified URI information.'))
                    item['status'] = None
        except PIDDoesNotExistError:
            pass
        except BaseException:
            current_app.logger.error(
                'Unexpected error: ',
                sys.exc_info()[0]
            )
        if item.get('status') == 'new':
            handle_remove_identifier(item)
        if errors:
            item['errors'] = errors
        result.append(item)
    return result


def handle_check_identifier(name) -> str:
    """Check data is Identifier of Identifier Registration.

    :argument
        name_path     -- {list} list name path.
    :return
        return       -- Name of key if is Identifier.

    """
    result = ''
    if 'Identifier' in name or 'Identifier Registration' in name:
        result = name[0]
    return result


def handle_remove_identifier(item) -> dict:
    """Remove Identifier of Identifier Registration.

    :argument
        item         -- Item.
    :return
        return       -- Item had been removed property.

    """
    if item and item.get('Identifier key'):
        del item['metadata'][item.get('Identifier key')]
        del item['Identifier key']
        del item['Identifier']
    if item and item.get('Identifier Registration key'):
        del item['metadata'][item.get('Identifier Registration key')]
        del item['Identifier Registration key']
        del item['Identifier Registration']
    return item


def compare_identifier(item, item_exist):
    """Compare data is Identifier.

    :argument
        item           -- {dict} item import.
        item_exist     -- {dict} item in system.
    :return
        return       -- Name of key if is Identifier.

    """
    if item.get('Identifier key'):
        item_iden = item.get('metadata', '').get(item.get('Identifier key'))
        item_exist_iden = item_exist.get(item.get(
            'Identifier key')).get('attribute_value_mlt')
        if len(item_iden) == len(item_exist_iden):
            list_dif = difference(item_iden, item_exist_iden)
            if list_dif:
                item['errors'] = ['Errors in Identifier']
                item['status'] = ''
        elif len(item_iden) > len(item_exist_iden):
            list_dif = difference(item_iden, item_exist_iden)
            for i in list_dif + item_iden:
                if i not in item_exist_iden:
                    try:
                        pids = [
                            k for k in i.values() if k != 'DOI' or k != 'HDL']
                        for pid in pids:
                            item_check = \
                                WekoRecord.get_record_by_pid(pid)
                            if item_check and item_check.id != item.id:
                                item['errors'] = ['Errors in Identifier']
                                item['status'] = ''
                    except BaseException:
                        current_app.logger.error('Unexpected error: ',
                                                 sys.exc_info()[0])
            if item['errors']:
                item['metadata'][item.get('Identifier key')] = list(set([
                    it for it in list_dif + item_iden
                ]))
        elif len(item_iden) < len(item_exist_iden):
            item['metadata'][item.get('Identifier key')] = item_exist_iden
    if item.get('uri'):
        pass
    return item


def make_tsv_by_line(lines):
    """Make TSV file."""
    tsv_output = StringIO()

    writer = csv.writer(tsv_output, delimiter='\t')
    writer.writerows(lines)

    return tsv_output


def make_stats_tsv(raw_stats, list_name):
    """Make TSV report file for stats."""
    tsv_output = StringIO()

    writer = csv.writer(tsv_output, delimiter='\t',
                        lineterminator="\n")

    writer.writerow(list_name)
    for item in raw_stats:
        term = []
        for name in list_name:
            term.append(item.get(name))
        writer.writerow(term)

    return tsv_output


def difference(list1, list2):
    """Make TSV report file for stats."""
    list_dif = [i for i in list1 + list2 if i not in list1 or i not in list2]
    return list_dif


def check_identifier_new(item):
    """Check data Identifier.

    :argument
        item           -- {dict} item import.
        item_exist     -- {dict} item in system.
    :return
        return       -- Name of key if is Identifier.

    """
    if item.get('Identifier key'):
        item_iden = item.get('metadata', '').get(item.get('Identifier key'))
        for it in item_iden:
            try:
                pids = [
                    k for k in it.values() if k != 'DOI' or k != 'HDL']
                for pid in pids:
                    item_check = \
                        WekoRecord.get_record_by_pid(pid)
                    if item_check and item_check.id != item.id:
                        item['errors'] = ['Errors in Identifier']
                        item['status'] = ''
            except BaseException:
                current_app.logger.error('Unexpected error: ',
                                         sys.exc_info()[0])
    return item


def create_deposit(item_id):
    """Create deposit.

    :argument
        item           -- {dict} item import.
        item_exist     -- {dict} item in system.

    """
    try:
        if item_id is not None:
            dep = WekoDeposit.create({}, recid=int(item_id))
            db.session.commit()
        else:
            dep = WekoDeposit.create({})
            db.session.commit()
        return dep['recid']
    except Exception:
        db.session.rollback()


def up_load_file_content(record, root_path):
    """Upload file content.

    :argument
        record         -- {dict} item import.
        root_path      -- {str} root_path.

    """
    try:
        file_path = record.get('file_path')
        if file_path:
            pid = PersistentIdentifier.query.filter_by(
                pid_type='recid',
                pid_value=record.get('id')).first()
            rec = RecordMetadata.query.filter_by(
                id=pid.object_uuid).first()
            bucket = rec.json['_buckets']['deposit']
            for file_name in file_path:
                with open(root_path + '/' + file_name, 'rb') as file:
                    obj = ObjectVersion.create(
                        bucket,
                        get_file_name(file_name)
                    )
                    obj.set_contents(file)
                    db.session.commit()
    except Exception:
        db.session.rollback()


def get_file_name(file_path):
    """Get file name.

    :argument
        file_path    -- {str} file_path.
    :returns         -- {str} file name

    """
    return file_path.split('/')[-1] if file_path.split('/')[-1] else ''


def register_item_metadata(item):
    """Upload file content.

    :argument
        list_record    -- {list} list item import.
        file_path      -- {str} file path.
    """
    def clean_file_metadata(item_type_id, data):
        # clear metadata of file information
        item_map = get_mapping(Mapping.get_record(
            item_type_id), 'jpcoar_mapping')
        _, key = get_data_by_property(
            item, item_map, "file.URI.@value")
        if key:
            key = key.split('.')[0]
            if not data.get(key):
                deleted_items = data.get('deleted_items') or []
                deleted_items.append(key)
                data['deleted_items'] = deleted_items
        return data

    def clean_file_bucket(deposit):
        # clean bucket
        file_names = [file['filename'] for file in deposit.get_file_data()]
        lastest_files_version = []
        # remove lastest version
        for file in deposit.files:
            if file.obj.key not in file_names:
                file.obj.remove()
            else:
                lastest_files_version.append(file.obj.version_id)
        # remove old version of file
        all_file_version = ObjectVersion.get_by_bucket(
            deposit.files.bucket, True, True).all()
        for file in all_file_version:
            if file.key not in file_names:
                file.remove()
            elif file.version_id not in lastest_files_version:
                file.remove()

    def clean_all_file_in_bucket(deposit):
        all_file_version = ObjectVersion.get_by_bucket(
            deposit.files.bucket, True, True).all()
        for file in all_file_version:
            file.remove()

    item_id = str(item.get('id'))
    try:
        pid = PersistentIdentifier.query.filter_by(
            pid_type='recid',
            pid_value=item_id
        ).first()

        record = WekoDeposit.get_record(pid.object_uuid)

        _deposit_data = record.dumps().get("_deposit")
        deposit = WekoDeposit(record, record.model)
        new_data = dict(
            **item.get('metadata'),
            **_deposit_data,
            **{
                '$schema': item.get('$schema'),
                'title': item.get('item_title'),
            }
        )
        item_status = {
            'index': new_data['path'],
            'actions': 'publish',
        }
        if not new_data.get('pid'):
            new_data = dict(**new_data, **{
                'pid': {
                    'revision_id': 0,
                    'type': 'recid',
                    'value': item_id
                }
            })
        new_data = clean_file_metadata(item['item_type_id'], new_data)
        deposit.update(item_status, new_data)
        if item.get('file_path'):
            # update
            clean_file_bucket(deposit)
        else:
            # delete
            clean_all_file_in_bucket(deposit)
        deposit.commit()
        deposit.publish()

        feedback_mail_list = item['metadata'].get('feedback_mail_list')
        if feedback_mail_list:
            FeedbackMailList.update(
                item_id=deposit.id,
                feedback_maillist=feedback_mail_list
            )
            deposit.update_feedback_mail()
        else:
            FeedbackMailList.delete(deposit.id)
            deposit.remove_feedback_mail()

        with current_app.test_request_context():
            if item['status'] in ['upgrade', 'new']:
                _deposit = deposit.newversion(pid)
                _deposit.publish()
            else:
                _pid = PIDVersioning(child=pid).last_child
                _record = WekoDeposit.get_record(_pid.object_uuid)
                _deposit = WekoDeposit(_record, _record.model)
                _deposit.update(item_status, new_data)
                _deposit.commit()
                _deposit.merge_data_to_record_without_version(pid)
                _deposit.publish()

            if feedback_mail_list:
                FeedbackMailList.update(
                    item_id=_deposit.id,
                    feedback_maillist=feedback_mail_list
                )
                _deposit.update_feedback_mail()

        db.session.commit()

    except Exception as ex:
        db.session.rollback()
        current_app.logger.error('item id: %s update error.' % item_id)
        current_app.logger.error(ex)
        return {
            'success': False,
            'error': str(ex)
        }
    return {
        'success': True
    }


def update_publish_status(item_id, status):
    """Handle get title.

    :argument
        item_id     -- {str} Item Id.
        status      -- {str} Publish status (0: public, 1: private)
    :return

    """
    record = WekoRecord.get_record_by_pid(item_id)
    record['publish_status'] = status
    record.commit()
    db.session.commit()
    indexer = WekoIndexer()
    indexer.update_publish_status(record)


def handle_workflow(item: dict):
    """Handle workflow.

    :argument
        title           -- {dict or list} title.
    :return
        return       -- title string.

    """
    pid = PersistentIdentifier.query.filter_by(
        pid_type='recid', pid_value=item.get('id')).first()
    if pid:
        activity = WorkActivity()
        wf_activity = activity.get_workflow_activity_by_item_id(
            pid.object_uuid)
        if wf_activity:
            return
    else:
        workflow = WorkFlow.query.filter_by(
            itemtype_id=item.get('item_type_id')).first()
        if workflow:
            return
        else:
            create_work_flow(item.get('item_type_id'))


def create_work_flow(item_type_id):
    """Handle create work flow.

    :argument
        item_type_id        -- {str} item_type_id.
    :return

    """
    flow_define = FlowDefine.query.filter_by(
        flow_name='Registration Flow').first()
    it = ItemTypes.get_by_id(item_type_id)

    if flow_define and it:
        try:
            data = WorkFlow()
            data.flows_id = uuid.uuid4()
            data.flows_name = it.item_type_name.name
            data.itemtype_id = it.id
            data.flow_id = flow_define.id
            db.session.add(data)
            db.session.commit()
        except Exception as ex:
            db.session.rollback()
            current_app.logger.error("create work flow error")
            current_app.logger.error(ex)


def create_flow_define():
    """Handle create flow_define."""
    flow_define = FlowDefine.query.filter_by(
        flow_name='Registration Flow').first()

    if not flow_define:
        the_flow = Flow()
        flow = the_flow.create_flow(WEKO_FLOW_DEFINE)

        if flow and flow.flow_id:
            the_flow.upt_flow_action(flow.flow_id,
                                     WEKO_FLOW_DEFINE_LIST_ACTION)


def import_items_to_system(item: dict, url_root: str):
    """Validation importing zip file.

    :argument
        item        -- Items Metadata.
        url_root    -- url_root.
    :return
        return      -- PID object if exist.

    """
    response = None
    if not item:
        return None
    else:
        root_path = item.get('root_path', '')
        if item.get('status') == 'new':
            item_id = create_deposit(item.get('id'))
            item['id'] = item_id
        up_load_file_content(item, root_path)
        response = register_item_metadata(item)
        if response.get('success') and \
                current_app.config.get('WEKO_HANDLE_ALLOW_REGISTER_CRNI'):
            response = register_item_handle(item, url_root)
        if response.get('success'):
            response = register_item_doi(item)
        if response.get('success'):
            status_number = WEKO_IMPORT_PUBLISH_STATUS.index(
                item.get('publish_status')
            )
            response = register_item_update_publish_status(
                item,
                str(status_number))

        return response


def remove_temp_dir(path):
    """Validation importing zip file.

    :argument
        path     -- {string} path temp_dir.
    :return

    """
    shutil.rmtree(str(path.replace("/data", "")))


def handle_item_title(list_record):
    """Prepare item title.

    :argument
        list_record -- {list} list record import.
    :return

    """
    for item in list_record:
        error = None
        item_type_mapping = Mapping.get_record(item['item_type_id'])
        item_map = get_mapping(item_type_mapping, 'jpcoar_mapping')
        title_data, _title_key = get_data_by_property(
            item, item_map, "title.@value")
        if not title_data:
            error = _('Title is required item.')
        else:
            item['item_title'] = title_data[0]

        if error:
            item['errors'] = item['errors'] + [error] \
                if item.get('errors') else [error]


def handle_check_and_prepare_publish_status(list_record):
    """Check and prepare publish status.

    :argument
        list_record -- {list} list record import.
    :return

    """
    for item in list_record:
        error = None
        publish_status = item.get('publish_status')
        if not publish_status:
            error = _('{} is required item.').format('PUBLISH_STATUS')
        elif publish_status not in WEKO_IMPORT_PUBLISH_STATUS:
            error = _('Please set "public" or "private" for {}.') \
                .format('PUBLISH_STATUS')

        if error:
            item['errors'] = item['errors'] + [error] \
                if item.get('errors') else [error]


def handle_check_and_prepare_index_tree(list_record):
    """Check index existed and prepare index tree data.

    :argument
        list_record -- {list} list record import.
    :return

    """
    errors = []
    warnings = []

    def check(index_ids, index_names, parent_id=0, is_root=False):
        index_id = index_ids[0]
        index_name = index_names[0]
        index = None
        try:
            index = Indexes.get_index(index_id)
        except Exception:
            current_app.logger.warning("Specified IndexID is invalid!")

        if index and (
            (is_root and not index.parent)
            or (not is_root and parent_id and index.parent == parent_id)
        ):
            if index.index_name_english != index_name:
                warnings.append(
                    _('Specified {} does not match with existing index.')
                    .format('POS_INDEX'))
        elif index_name:
            index = Indexes.get_index_by_name_english(
                index_name, parent_id)
            msg_not_exist = _('The specified {} does not exist in system.')
            if not index:
                if index_id:
                    errors.append(msg_not_exist.format('IndexID, POS_INDEX'))
                    return None
                else:
                    errors.append(msg_not_exist.format('POS_INDEX'))
                    return None
            else:
                if index_id:
                    errors.append(msg_not_exist.format('IndexID'))
                    return None

        data = {
            'index_id': index.id if index else index_id,
            'index_name': index.index_name_english if index else index_name,
            'parent_id': parent_id,
            'existed': index is not None
        }

        if len(index_ids) > 1:
            child = check(index_ids[1:], index_names[1:],
                          data['index_id'], False)
            if child:
                data['child'] = child
            else:
                return None

        return data

    for item in list_record:
        indexes = []
        index_ids = item.get('IndexID')
        pos_index = item.get('pos_index')

        if not index_ids and not pos_index:
            errors = [_('Both of IndexID and POS_INDEX are not being set.')]
        else:
            if not index_ids:
                index_ids = ['' for i in range(len(pos_index))]
            for x, index_id in enumerate(index_ids):
                tree_ids = [i.strip() for i in index_id.split('/')]
                tree_names = []
                if pos_index and x <= len(pos_index) - 1:
                    tree_names = [i.strip() for i in pos_index[x].split('/')]
                else:
                    tree_names = ['' for i in range(len(tree_ids))]

                root = check(tree_ids, tree_names, 0, True)
                if root:
                    indexes.append(root)

        if indexes:
            item['indexes'] = indexes

        if errors:
            errors = list(set(errors))
            item['errors'] = item['errors'] + errors \
                if item.get('errors') else errors
            errors = []

        if warnings:
            warnings = list(set(warnings))
            item['warnings'] = item['warnings'] + warnings \
                if item.get('warnings') else warnings
            warnings = []


def handle_index_tree(item):
    """Handle get index_id of item need import to.

    :argument
        item     -- {object} record item.
    :return

    """
    def check_and_create_index(index):
        if not index['existed']:
            exist_index = Indexes.get_index_by_name_english(
                index['index_name'], index['parent_id'])
            if exist_index:
                index['index_id'] = exist_index.id
            else:
                now = datetime.now()
                index_id = index['index_id'] if index['index_id'] \
                    else int(datetime.timestamp(now) * 10 ** 3)
                create_index = Indexes.create(
                    pid=index['parent_id'],
                    indexes={'id': index_id,
                             'value': index['index_name']})
                if create_index:
                    index['index_id'] = index_id
                    if index.get('child'):
                        index['child']['parent_id'] = index_id

        if index.get('child'):
            return check_and_create_index(index['child'])
        else:
            return index['index_id']  # Return last child index_id

    indexes = item['indexes']
    if indexes:
        path = []
        for index in indexes:
            path.append(check_and_create_index(index))
        item['metadata']['path'] = path


def handle_check_and_prepare_feedback_mail(list_record):
    """Check feedback email is existed in database and prepare data.

    :argument
        list_record -- {list} list record import.
    :return

    """
    for item in list_record:
        errors = []
        feedback_mail = []
        if item.get('feedback_mail'):
            for mail in item.get('feedback_mail'):
                if not re.search(WEKO_IMPORT_EMAIL_PATTERN, mail):
                    errors.append(_('Specified {} is invalid.').format(mail))
                else:
                    email_checked = check_email_existed(mail)
                    feedback_mail.append(email_checked)

            if feedback_mail:
                item['metadata']['feedback_mail_list'] = feedback_mail
            if errors:
                errors = list(set(errors))
                item['errors'] = item['errors'] + errors \
                    if item.get('errors') else errors


def handle_set_change_identifier_flag(list_record, is_change_identifier):
    """Set Change Identifier Mode flag.

    :argument
        list_record -- {list} list record import.
        is_change_identifier -- {bool} Change Identifier Mode.
    :return

    """
    for item in list_record:
        item['is_change_identifier'] = is_change_identifier


def handle_check_cnri(list_record):
    """Check CNRI.

    :argument
        list_record -- {list} list record import.
    :return

    """
    for item in list_record:
        error = None
        item_id = str(item.get('id'))
        cnri = item.get('cnri')
        cnri_set = current_app.config.get('WEKO_HANDLE_ALLOW_REGISTER_CRNI')

        if item.get('is_change_identifier') and cnri_set:
            if not cnri:
                error = _('Please specify {}.').format('CNRI')
            else:
                if len(cnri) > 290:
                    error = _('The specified {} exceeds the maximum length.') \
                        .format('CNRI')
                else:
                    split_cnri = cnri.split('/')
                    if len(split_cnri) > 1:
                        prefix = '/'.join(split_cnri[0:-1])
                        suffix = split_cnri[-1]
                    else:
                        prefix = cnri
                        suffix = "{:010d}".format(int(item_id))
                        item['cnri'] = prefix + '/' + suffix

                    if prefix != Handle().get_prefix():
                        error = _('Specified Prefix of {} is incorrect.') \
                            .format('CNRI')
                    if not re.search(WEKO_IMPORT_SUFFIX_PATTERN, suffix):
                        error = _(err_msg_suffix).format('CNRI')
        else:
            if item.get('status') == 'new' \
                    or item.get('is_change_identifier') or not cnri_set:
                if cnri:
                    error = _('{} cannot be set.').format('CNRI')
            else:
                pid_cnri = None
                try:
                    pid_cnri = WekoRecord.get_record_by_pid(item_id).pid_cnri
                    if pid_cnri:
                        if not cnri:
                            error = _('Please specify {}.').format('CNRI')
                        elif not pid_cnri.pid_value.endswith(str(cnri)):
                            error = _('Specified {} is different from existing'
                                      + ' {}.').format('CNRI', 'CNRI')
                    elif cnri:
                        error = _('Specified {} is different '
                                  + 'from existing {}.').format('CNRI', 'CNRI')
                except Exception as ex:
                    current_app.logger.error(
                        'item id: %s not found.' % item_id)
                    current_app.logger.error(ex)

        if error:
            item['errors'] = item['errors'] + [error] \
                if item.get('errors') else [error]
            item['errors'] = list(set(item['errors']))


def handle_check_doi_ra(list_record):
    """Check DOI_RA.

    :argument
        list_record -- {list} list record import.
    :return

    """
    def check_existed(item_id, doi_ra):
        error = None
        try:
            pid = WekoRecord.get_record_by_pid(item_id).pid_recid
            identifier = IdentifierHandle(pid.object_uuid)
            _value, doi_type = identifier.get_idt_registration_data()

            if doi_type and doi_type[0] != doi_ra:
                error = _('Specified {} is different from '
                          + 'existing {}.').format('DOI_RA', 'DOI_RA')
        except Exception as ex:
            current_app.logger.error('item id: %s not found.' % item_id)
            current_app.logger.error(ex)
        return error

    for item in list_record:
        error = None
        item_id = str(item.get('id'))
        doi_ra = item.get('doi_ra')

        if item.get('doi') and not doi_ra:
            error = _('{} is required item.').format('DOI_RA')
        elif doi_ra:
            if doi_ra not in WEKO_IMPORT_DOI_TYPE:
                error = _('DOI_RA should be set by one of JaLC'
                          + ', Crossref, DataCite, NDL JaLC.')
            elif item.get('is_change_identifier'):
                if not handle_doi_required_check(item):
                    error = _('PID does not meet the conditions.')
            else:
                if item.get('status') == 'new':
                    if item.get('doi'):
                        error = _('{} cannot be set.').format('DOI')
                    elif not handle_doi_required_check(item):
                        error = _('PID does not meet the conditions.')
                else:
                    error = check_existed(item_id, doi_ra)

        if error:
            item['errors'] = item['errors'] + [error] \
                if item.get('errors') else [error]
            item['errors'] = list(set(item['errors']))


def handle_check_doi(list_record):
    """Check DOI.

    :argument
        list_record -- {list} list record import.
    :return

    """
    for item in list_record:
        error = None
        item_id = str(item.get('id'))
        doi = item.get('doi')
        doi_ra = item.get('doi_ra')

        if item.get('is_change_identifier') \
                and doi_ra and not doi:
            error = _('{} is required item.').format('DOI')
        elif doi_ra:
            if item.get('is_change_identifier'):
                if not doi:
                    error = _('Please specify {}.').format('DOI')
                else:
                    if len(doi) > 290:
                        error = _('The specified {} exceeds'
                                  + ' the maximum length.').format('DOI')
                    else:
                        split_doi = doi.split('/')
                        if len(split_doi) > 1:
                            prefix = '/'.join(split_doi[0:-1])
                            suffix = split_doi[-1]
                        else:
                            prefix = doi
                            suffix = "{:010d}".format(int(item_id))
                            item['doi'] = prefix + '/' + suffix

                        if prefix != get_doi_prefix(doi_ra):
                            error = _('Specified Prefix of {} is incorrect.') \
                                .format('DOI')
                        if not re.search(WEKO_IMPORT_SUFFIX_PATTERN, suffix):
                            error = _(err_msg_suffix).format('DOI')
            else:
                if item.get('status') == 'new':
                    if doi:
                        error = _('{} cannot be set.').format('DOI')
                else:
                    pid_doi = None
                    try:
                        pid_doi = WekoRecord.get_record_by_pid(item_id).pid_doi
                    except Exception as ex:
                        current_app.logger.error(
                            'item id: %s not found.' % item_id)
                        current_app.logger.error(ex)
                    if pid_doi:
                        if not doi:
                            error = _('Please specify {}.').format('DOI')
                        elif not pid_doi.pid_value.endswith(doi):
                            error = _('Specified {} is different from'
                                      + ' existing {}.').format('DOI', 'DOI')

        if error:
            item['errors'] = item['errors'] + [error] \
                if item.get('errors') else [error]
            item['errors'] = list(set(item['errors']))


def register_item_handle(item, url_root):
    """Register item handle (CNRI).

    :argument
        item    -- {object} Record item.
        url_root -- {str} url_root.
    :return
        response -- {object} Process status.

    """
    item_id = str(item.get('id'))
    try:
        record = WekoRecord.get_record_by_pid(item_id)
        pid = record.pid_recid
        pid_hdl = record.pid_cnri
        cnri = item.get('cnri')

        if item.get('is_change_identifier'):
            if item.get('status') == 'new':
                register_hdl_by_handle(cnri, pid.object_uuid)
            else:
                if pid_hdl and not pid_hdl.pid_value.endswith(cnri):
                    pid_hdl.delete()
                    register_hdl_by_handle(cnri, pid.object_uuid)
                elif not pid_hdl:
                    register_hdl_by_handle(cnri, pid.object_uuid)
        else:
            if item.get('status') == 'new':
                register_hdl_by_item_id(item_id, pid.object_uuid, url_root)

        db.session.commit()
    except Exception as ex:
        db.session.rollback()
        current_app.logger.error('item id: %s update error.' % item_id)
        current_app.logger.error(ex)
        return {
            'success': False,
            'error': str(ex)
        }
    return {
        'success': True
    }


def prepare_doi_setting():
    """Prepare doi link with empty."""
    identifier_setting = get_identifier_setting('Root Index')
    if identifier_setting:
        text_empty = '<Empty>'
        if not identifier_setting.jalc_doi:
            identifier_setting.jalc_doi = text_empty
        if not identifier_setting.jalc_crossref_doi:
            identifier_setting.jalc_crossref_doi = text_empty
        if not identifier_setting.jalc_datacite_doi:
            identifier_setting.jalc_datacite_doi = text_empty
        if not identifier_setting.ndl_jalc_doi:
            identifier_setting.ndl_jalc_doi = text_empty
        # Semi-automatic suffix
        if identifier_setting.suffix and IDENTIFIER_GRANT_SUFFIX_METHOD == 1:
            identifier_setting.suffix = '/' + identifier_setting.suffix
        else:
            identifier_setting.suffix = ''
        return identifier_setting


def get_doi_prefix(doi_ra):
    """Get DOI prefix."""
    identifier_setting = prepare_doi_setting()
    if identifier_setting:
        suffix = identifier_setting.suffix or ''
        if doi_ra == WEKO_IMPORT_DOI_TYPE[0]:
            return identifier_setting.jalc_doi + suffix
        elif doi_ra == WEKO_IMPORT_DOI_TYPE[1]:
            return identifier_setting.jalc_crossref_doi + suffix
        elif doi_ra == WEKO_IMPORT_DOI_TYPE[2]:
            return identifier_setting.jalc_datacite_doi + suffix
        elif doi_ra == WEKO_IMPORT_DOI_TYPE[3]:
            return identifier_setting.ndl_jalc_doi + suffix


def prepare_doi_link(item_id):
    """Get DOI link."""
    item_id = '%010d' % int(item_id)
    identifier_setting = prepare_doi_setting()
    suffix = identifier_setting.suffix or ''

    return {
        'identifier_grant_jalc_doi_link':
            IDENTIFIER_GRANT_LIST[1][2] + '/'
            + identifier_setting.jalc_doi
            + suffix + '/' + item_id,
        'identifier_grant_jalc_cr_doi_link':
            IDENTIFIER_GRANT_LIST[2][2] + '/'
            + identifier_setting.jalc_crossref_doi
            + suffix + '/' + item_id,
        'identifier_grant_jalc_dc_doi_link':
            IDENTIFIER_GRANT_LIST[3][2] + '/'
            + identifier_setting.jalc_datacite_doi
            + suffix + '/' + item_id,
        'identifier_grant_ndl_jalc_doi_link':
            IDENTIFIER_GRANT_LIST[4][2] + '/'
            + identifier_setting.ndl_jalc_doi
            + suffix + '/' + item_id
    }


def register_item_doi(item):
    """Register item DOI.

    :argument
        item    -- {object} Record item.
    :return
        response -- {object} Process status.

    """
    item_id = str(item.get('id'))
    is_change_identifier = item.get('is_change_identifier')
    doi_ra = item.get('doi_ra')
    doi = item.get('doi')
    try:
        record_without_version = WekoRecord.get_record_by_pid(item_id)
        pid = record_without_version.pid_recid
        pid_doi = record_without_version.pid_doi

        lastest_version_id = item_id + '.' + \
            str(get_latest_version_id(item_id) - 1)
        pid_lastest = WekoRecord.get_record_by_pid(
            lastest_version_id).pid_recid

        if is_change_identifier:
            if doi_ra and doi:
                data = {
                    'identifier_grant_jalc_doi_link':
                        IDENTIFIER_GRANT_LIST[1][2] + '/' + doi,
                    'identifier_grant_jalc_cr_doi_link':
                        IDENTIFIER_GRANT_LIST[2][2] + '/' + doi,
                    'identifier_grant_jalc_dc_doi_link':
                        IDENTIFIER_GRANT_LIST[3][2] + '/' + doi,
                    'identifier_grant_ndl_jalc_doi_link':
                        IDENTIFIER_GRANT_LIST[4][2] + '/' + doi
                }
                if pid_doi:
                    pid_doi.delete()
                saving_doi_pidstore(
                    pid_lastest.object_uuid,
                    pid.object_uuid,
                    data,
                    WEKO_IMPORT_DOI_TYPE.index(doi_ra) + 1,
                    is_feature_import=True
                )
        else:
            if doi_ra and not doi:
                data = prepare_doi_link(item_id)
                saving_doi_pidstore(
                    pid_lastest.object_uuid,
                    pid.object_uuid,
                    data,
                    WEKO_IMPORT_DOI_TYPE.index(doi_ra) + 1,
                    is_feature_import=True
                )

        deposit = WekoDeposit.get_record(pid.object_uuid)
        deposit.commit()
        deposit.publish()
        deposit = WekoDeposit.get_record(pid_lastest.object_uuid)
        deposit.commit()
        deposit.publish()

        db.session.commit()
    except Exception as ex:
        db.session.rollback()
        current_app.logger.error('item id: %s update error.' % item_id)
        current_app.logger.error(ex)
        return {
            'success': False,
            'error': str(ex)
        }
    return {
        'success': True
    }


def register_item_update_publish_status(item, status):
    """Update Publish Status.

    :argument
        item    -- {object} Record item.
        status  -- {str} Publish Status.
    :return
        response -- {object} Process status.

    """
    try:
        item_id = str(item.get('id'))
        lastest_version_id = item_id + '.' + \
            str(get_latest_version_id(item_id) - 1)

        update_publish_status(item_id, status)
        if lastest_version_id:
            update_publish_status(lastest_version_id, status)
    except Exception as ex:
        db.session.rollback()
        current_app.logger.error('item id: %s update error.' % item_id)
        current_app.logger.error(ex)
        return {
            'success': False,
            'error': str(ex)
        }
    return {
        'success': True
    }


def handle_doi_required_check(record):
    """DOI Validation check (Resource Type, Required, either required).

    :argument
        record    -- {object} Record item.
    :return
        true/false -- {object} Validation result.

    """
    ddi_item_type_name = 'DDI'
    journalarticle_type = ['other（プレプリント）', 'conference paper',
                           'data paper', 'departmental bulletin paper',
                           'editorial', 'journal article', 'periodical',
                           'review article', 'article']
    thesis_types = ['thesis', 'bachelor thesis', 'master thesis',
                    'doctoral thesis']
    report_types = ['technical report', 'research report', 'report',
                    'book', 'book part']
    elearning_type = ['learning material']
    dataset_type = ['software', 'dataset']
    datageneral_types = ['internal report', 'policy report', 'report part',
                         'working paper', 'interactive resource',
                         'musical notation', 'research proposal',
                         'technical documentation', 'workflow',
                         'その他（その他）', 'sound', 'patent',
                         'cartographic material', 'map', 'lecture', 'image',
                         'still image', 'moving image', 'video',
                         'conference object', 'conference proceedings',
                         'conference poster']

    item_type = None

    if 'doi_ra' in record and record['doi_ra'] in ['JaLC',
                                                   'Crossref',
                                                   'DataCite',
                                                   'NDL JaLC']:
        doi_type = record['doi_ra']
        item_type_mapping = Mapping.get_record(record['item_type_id'])
        if item_type_mapping:
            item_type = ItemTypes.get_by_id(id_=record['item_type_id'])
            item_map = get_mapping(item_type_mapping, 'jpcoar_mapping')
        else:
            return False

        properties = {}
        # 必須
        required_properties = []
        # いずれか必須
        either_properties = []

        resource_type, resource_type_key = get_data_by_property(record,
                                                                item_map,
                                                                'type.@value')
        if not resource_type or not item_type \
                or check_required_data(resource_type, resource_type_key):
            return False

        resource_type = resource_type.pop()
        if doi_type == 'JaLC':
            if resource_type in journalarticle_type \
                or resource_type in report_types \
                or (resource_type in elearning_type) \
                    or resource_type in datageneral_types:
                required_properties = ['title']
                if item_type.item_type_name.name != ddi_item_type_name:
                    required_properties.append('fileURI')
            elif resource_type in thesis_types:
                required_properties = ['title',
                                       'creator']
                if item_type.item_type_name.name != ddi_item_type_name:
                    required_properties.append('fileURI')
            elif resource_type in dataset_type:
                required_properties = ['title',
                                       'givenName']
                if item_type.item_type_name.name != ddi_item_type_name:
                    required_properties.append('fileURI')
                either_properties = ['geoLocationPoint',
                                     'geoLocationBox',
                                     'geoLocationPlace']
        elif doi_type == 'Crossref':
            if resource_type in journalarticle_type:
                required_properties = ['title',
                                       'publisher',
                                       'sourceIdentifier',
                                       'sourceTitle']
                if item_type.item_type_name.name != ddi_item_type_name:
                    required_properties.append('fileURI')
            elif resource_type in report_types:
                required_properties = ['title']
                if item_type.item_type_name.name != ddi_item_type_name:
                    required_properties.append('fileURI')
            elif resource_type in thesis_types:
                required_properties = ['title',
                                       'creator']
                if item_type.item_type_name.name != ddi_item_type_name:
                    required_properties.append('fileURI')
        # DataCite DOI identifier registration
        elif doi_type == 'DataCite' \
                and item_type.item_type_name.name != ddi_item_type_name:
            required_properties = ['fileURI']
        # NDL JaLC DOI identifier registration
        elif doi_type == 'NDL JaLC' \
                and item_type.item_type_name.name != ddi_item_type_name:
            required_properties = ['fileURI']

        if required_properties:
            properties['required'] = required_properties
        if either_properties:
            properties['either'] = either_properties
        if properties:
            return validation_item_property(record, item_map, properties)
        else:
            return False

    return False


def get_data_by_property(record, item_map, item_property):
    """
    Get data by property text.

    :param item_property: property value in item_map
    :return: error_list or None
    """
    key = item_map.get(item_property)
    data = []
    if not key:
        current_app.logger.error(str(item_property) + ' jpcoar:mapping '
                                                      'is not correct')
        return None, None
    attribute = record['metadata'].get(key.split('.')[0])
    if not attribute:
        return None, key
    else:
        data_result = get_sub_item_value(
            attribute, key.split('.')[-1])
        if data_result:
            for value in data_result:
                data.append(value)
    return data, key


def validation_item_property(record, item_map, properties):
    """
    Validate item property.

    :param record: Record object.
    :param item_map: Mapping Data.
    :param properties: Property's keywords.
    :return: True or False
    """
    if properties.get('required'):
        if not validattion_item_property_required(
                record, item_map, properties['required']):
            return False
    if properties.get('either'):
        if not validattion_item_property_either_required(
                record, item_map, properties['either']):
            return False
    return True


def validattion_item_property_required(
        record, item_map, properties):
    """
    Validate item property is required.

    :param mapping_data: Mapping Data contain record and item_map
    :param properties: Property's keywords
    :return: error_list or None
    """
    # check jpcoar:URI
    if 'fileURI' in properties:
        _, key = get_data_by_property(
            record, item_map, "file.URI.@value")
        data = []
        if key:
            key = key.split('.')[0]
            item_file = record['metadata'].get(key)
            if item_file:
                file_name_data = get_sub_item_value(
                    item_file, 'filename')
                if file_name_data:
                    for value in file_name_data:
                        data.append(value)
                data.append(file_name_data)

            if check_required_data(data, key + '.filename', True):
                return False
    # check タイトル dc:title
    if 'title' in properties:
        title_data, title_key = get_data_by_property(
            record, item_map, "title.@value")
        lang_data, lang_key = get_data_by_property(
            record, item_map, "title.@attributes.xml:lang")

        requirements = check_required_data(title_data, title_key, True)
        lang_requirements = check_required_data(lang_data,
                                                lang_key,
                                                True)
        if requirements or lang_requirements:
            return False
    # check 識別子 jpcoar:givenName
    if 'givenName' in properties:
        _, key = get_data_by_property(
            record, item_map, "creator.givenName.@value")
        data = []
        if key:
            creators = record['metadata'].get(key.split('.')[0])
            if creators:
                given_name_data = get_sub_item_value(
                    creators, key.split('.')[-1])
                if given_name_data:
                    for value in given_name_data:
                        data.append(value)
                data.append(given_name_data)

        if check_required_data(data, key, True):
            return False
    # check 識別子 jpcoar:givenName and jpcoar:nameIdentifier
    if 'creator' in properties:
        _, key = get_data_by_property(
            record, item_map, "creator.givenName.@value")
        _, idt_key = get_data_by_property(
            record, item_map, "creator.nameIdentifier.@value")

        data = []
        idt_data = []
        creators = record['metadata'].get(key.split('.')[0])
        if key:
            creator_data = get_sub_item_value(
                creators,
                key.split('.')[-1])
            if creator_data:
                for value in creator_data:
                    data.append(value)
        if idt_key:
            creator_name_identifier = get_sub_item_value(
                creators, idt_key.split('.')[-1])
            if creator_name_identifier:
                for value in creator_name_identifier:
                    idt_data.append(value)

        requirements = check_required_data(data, key, True)
        idt_requirements = check_required_data(idt_data, idt_key, True)
        if requirements and idt_requirements:
            return False
    # check 収録物識別子 jpcoar:sourceIdentifier
    if 'sourceIdentifier' in properties:
        data, key = get_data_by_property(
            record, item_map, "sourceIdentifier.@value")
        type_data, type_key = get_data_by_property(
            record, item_map, "sourceIdentifier.@attributes.identifierType")

        requirements = check_required_data(data, key)
        type_requirements = check_required_data(type_data, type_key)
        if requirements or type_requirements:
            return False
    # check 収録物名 jpcoar:sourceTitle
    if 'sourceTitle' in properties:
        data, key = get_data_by_property("sourceTitle.@value")
        lang_data, lang_key = get_data_by_property(
            record, item_map, "sourceTitle.@attributes.xml:lang")

        requirements = check_required_data(data, key)
        lang_requirements = check_required_data(lang_data, lang_key)
        if requirements or lang_requirements:
            return False
        elif 'en' not in lang_data:
            return False
    # check 収録物名 dc:publisher
    if 'publisher' in properties:
        data, key = get_data_by_property("publisher.@value")
        lang_data, lang_key = get_data_by_property(
            record, item_map, "publisher.@attributes.xml:lang")

        requirements = check_required_data(data, key, True)
        lang_requirements = check_required_data(lang_data,
                                                lang_key,
                                                True)
        if requirements or lang_requirements:
            return False
        elif 'en' not in lang_data:
            return False

    return True


def validattion_item_property_either_required(
        record, item_map, properties):
    """
    Validate item property is either required.

    :param mapping_data: Mapping Data contain record and item_map
    :param properties: Property's keywords
    :return: error_list or None
    """
    if 'geoLocationPoint' in properties:
        latitude_data, latitude_key = get_data_by_property(
            record,
            item_map,
            "geoLocation.geoLocationPoint.pointLatitude.@value")
        longitude_data, longitude_key = get_data_by_property(
            record,
            item_map,
            "geoLocation.geoLocationPoint.pointLongitude.@value")

        latitude_requirement = check_required_data(
            latitude_data, latitude_key, True)
        longitude_requirement = check_required_data(
            longitude_data, longitude_key, True)

        if latitude_requirement and longitude_requirement:
            return False
    # check 位置情報（空間） datacite:geoLocationBox
    if 'geoLocationBox' in properties:
        east_data, east_key = get_data_by_property(
            "geoLocation.geoLocationBox.eastBoundLongitude.@value")
        north_data, north_key = get_data_by_property(
            "geoLocation.geoLocationBox.northBoundLatitude.@value")
        south_data, south_key = get_data_by_property(
            "geoLocation.geoLocationBox.southBoundLatitude.@value")
        west_data, west_key = get_data_by_property(
            "geoLocation.geoLocationBox.westBoundLongitude.@value")

        east_requirement = check_required_data(
            east_data, east_key, True)

        north_requirement = check_required_data(
            north_data, north_key, True)

        south_requirement = check_required_data(
            south_data, south_key, True)

        west_requirement = check_required_data(
            west_data, west_key, True)

        if east_requirement and north_requirement and south_requirement and \
                west_requirement:
            return False
    # check 位置情報（自由記述） datacite:geoLocationPlace
    if 'geoLocationPlace' in properties:
        data, key = get_data_by_property(
            "geoLocation.geoLocationPlace.@value")

        requirements = check_required_data(data, key, True)
        if requirements:
            return False

    return True


def handle_check_date(list_record):
    """Support validate three pattern: yyyy-MM-dd, yyyy-MM, yyyy.

    :argument
        list_record -- {list} list record import.
    :return

    """
    for record in list_record:
        error = None
        date_iso_keys = []
        item_type = ItemTypes.get_by_id(id_=record.get(
            'item_type_id', 0), with_deleted=True)
        if item_type:
            item_type = item_type.render
            form = item_type.get('table_row_map', {}).get('form', {})
            date_iso_keys = get_list_key_of_iso_date(form)
        for key in date_iso_keys:
            _keys = key.split('.')
            attribute = record.get('metadata').get(_keys[0])
            if attribute:
                data_result = get_sub_item_value(attribute, _keys[-1])
                for value in data_result:
                    if not validattion_date_property(value):
                        error = _('Please specify the date with any format of'
                                  + ' YYYY-MM-DD, YYYY-MM, YYYY.')
                        record['errors'] = record['errors'] + [error] \
                            if record.get('errors') else [error]
                        record['errors'] = list(set(record['errors']))
                        break
                if error:
                    break


def validattion_date_property(date_str):
    """
    Validate item property is either required.

    :param properties: Property's keywords
    :return: error_list or None
    """
    for fmt in ('%Y-%m-%d', '%Y-%m', '%Y'):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            pass
    return False


def get_list_key_of_iso_date(schemaform):
    """Get list key of iso date."""
    keys = []
    for item in schemaform:
        if not item.get('items'):
            if item.get('templateUrl', '') == DATE_ISO_TEMPLATE_URL:
                keys.append(item.get('key').replace('[]', ''))
        else:
            keys.extend(get_list_key_of_iso_date(item.get('items')))
    return keys


def get_current_language():
    """Get current language.

    :return:
    """
    current_lang = current_i18n.language
    # In case current_lang is not English
    # neither Japanese set default to English
    languages = \
        WEKO_ADMIN_IMPORT_CHANGE_IDENTIFIER_MODE_FILE_LANGUAGES
    if current_lang not in languages:
        current_lang = 'en'
    return current_lang


def get_change_identifier_mode_content():
    """Read data of change identifier mode base on language.

    :return:
    """
    file_extension = \
        WEKO_ADMIN_IMPORT_CHANGE_IDENTIFIER_MODE_FILE_EXTENSION
    first_file_name = \
        WEKO_ADMIN_IMPORT_CHANGE_IDENTIFIER_MODE_FIRST_FILE_NAME
    folder_path = \
        WEKO_ADMIN_IMPORT_CHANGE_IDENTIFIER_MODE_FILE_LOCATION
    current_lang = get_current_language()
    file_name = first_file_name + "_" + current_lang + file_extension
    data = []
    try:
        with open(folder_path + file_name) as file:
            data = file.read().splitlines()
    except FileNotFoundError as ex:
        current_app.logger.error(str(ex))
    return data


def get_root_item_option(item_id, item):
    """Handle if is root item."""
    _id = '.metadata.{}'.format(item_id)
    _name = item.get('title')

    _option = []
    if item.get('option').get('required'):
        _option.append('Required')
    if item.get('option').get('hidden'):
        _option.append('Hide')
    if item.get('option').get('multiple'):
        _option.append('Allow Multiple')
        _id += '[0]'
        _name += '#1'

    return _id, _name, _option


def get_sub_item_option(key, schemaform):
    """Get sub-item option."""
    _option = None
    for item in schemaform:
        if not item.get('items'):
            if item.get('key') == key:
                _option = []
                if item.get('required'):
                    _option.append('Required')
                if item.get('isHide'):
                    _option.append('Hide')
                break
        else:
            _option = get_sub_item_option(
                key, item.get('items'))
            if _option is not None:
                break
    return _option


def check_sub_item_is_system(key, schemaform):
    """Check the sub-item is system."""
    is_system = None
    for item in schemaform:
        if not item.get('items'):
            if item.get('key') == key:
                is_system = False
                if item.get('readonly'):
                    is_system = True
                break
        else:
            is_system = check_sub_item_is_system(
                key, item.get('items'))
            if is_system is not None:
                break
    return is_system


def get_lifetime():
    """Get db life time."""
    try:
        db_lifetime = SessionLifetime.get_validtime()
        if db_lifetime is None:
            return WEKO_ADMIN_LIFETIME_DEFAULT
        else:
            return db_lifetime.lifetime * 60
    except BaseException:
        return 0


def get_system_data_uri(key_type, key):
    """Get uri from key of System item."""
    if key_type == WEKO_IMPORT_SYSTEM_ITEMS[0]:
        return RESOURCE_TYPE_URI.get(key, None)
    elif key_type == WEKO_IMPORT_SYSTEM_ITEMS[1]:
        return VERSION_TYPE_URI.get(key, None)
    elif key_type == WEKO_IMPORT_SYSTEM_ITEMS[2]:
        return ACCESS_RIGHT_TYPE_URI.get(key, None)


def handle_fill_system_item(list_record):
    """Auto fill data into system item.

    :argument
        list_record -- {list} list record import.
    :return

    """
    def recursive_sub(keys, node, uri_key, current_type):
        if isinstance(node, list):
            for sub_node in node:
                recursive_sub(keys[1:], sub_node, uri_key, current_type)
        elif isinstance(node, dict):
            if len(keys) > 1:
                recursive_sub(keys[1:], node.get(keys[0]),
                              uri_key, current_type)
            else:
                type_data = node.get(keys[0])
                uri = get_system_data_uri(current_type, type_data)
                if uri is not None:
                    node[uri_key] = uri

    item_type_id = None
    item_map = None
    for item in list_record:
        if item_type_id != item['item_type_id']:
            item_type_id = item['item_type_id']
            item_map = get_mapping(Mapping.get_record(
                item_type_id), 'jpcoar_mapping')

        # Resource Type
        _, resourcetype_key = get_data_by_property(
            item, item_map, "type.@value")
        _, resourceuri_key = get_data_by_property(
            item, item_map, "type.@attributes.rdf:resource")
        if resourcetype_key and resourceuri_key:
            recursive_sub(resourcetype_key.split('.'),
                          item['metadata'],
                          resourceuri_key.split('.')[-1],
                          WEKO_IMPORT_SYSTEM_ITEMS[0])

        # Version Type
        _, versiontype_key = get_data_by_property(
            item, item_map, "versiontype.@value")
        _, versionuri_key = get_data_by_property(
            item, item_map, "versiontype.@attributes.rdf:resource")
        if versiontype_key and versionuri_key:
            recursive_sub(versiontype_key.split('.'),
                          item['metadata'],
                          versionuri_key.split('.')[-1],
                          WEKO_IMPORT_SYSTEM_ITEMS[1])

        # Access Right
        _, accessRights_key = get_data_by_property(
            item, item_map, "accessRights.@value")
        _, accessRightsuri_key = get_data_by_property(
            item, item_map, "accessRights.@attributes.rdf:resource")
        if accessRights_key and accessRightsuri_key:
            recursive_sub(accessRights_key.split('.'),
                          item['metadata'],
                          accessRightsuri_key.split('.')[-1],
                          WEKO_IMPORT_SYSTEM_ITEMS[2])
