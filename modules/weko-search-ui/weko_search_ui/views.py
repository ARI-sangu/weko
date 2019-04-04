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

"""Blueprint for weko-search-ui."""

import json
import os
import sys
from xml.etree import ElementTree as ET

from blinker import Namespace
from flask import Blueprint, abort, current_app, jsonify, make_response, \
    redirect, render_template, request, url_for
from invenio_i18n.ext import current_i18n
from invenio_indexer.api import RecordIndexer
from weko_index_tree.api import Indexes
from weko_index_tree.models import Index, IndexStyle
from weko_indextree_journal.api import Journals
from weko_deposit.api import WekoIndexer

from weko_search_ui.api import get_search_detail_keyword
from invenio_search import RecordsSearch
from invenio_records.api import Record
from invenio_db import db
from invenio_pidstore.models import PersistentIdentifier

from .api import SearchSetting
from .query import item_path_search_factory

_signals = Namespace()
searched = _signals.signal('searched')

blueprint = Blueprint(
    'weko_search_ui',
    __name__,
    template_folder='templates',
    static_folder='static',
)

blueprint_api = Blueprint(
    'weko_search_ui',
    __name__,
    # url_prefix='/',
    template_folder='templates',
    static_folder='static',
)


@blueprint.route("/search/index")
def search():
    """Index Search page ui."""
    search_type = request.args.get('search_type', '0')
    getArgs = request.args
    community_id = ""
    ctx = {'community': None}
    cur_index_id = search_type if search_type not in ('0', '1', ) else None
    if 'community' in getArgs:
        from weko_workflow.api import GetCommunity
        comm = GetCommunity.get_community_by_id(request.args.get('community'))
        ctx = {'community': comm}
        community_id = comm.id

    # Get index style
    style = IndexStyle.get(
        current_app.config['WEKO_INDEX_TREE_STYLE_OPTIONS']['id'])
    width = style.width if style else '3'

    # add at 1206 for search management
    sort_options, display_number = SearchSetting.get_results_setting()
    disply_setting = dict(size=display_number)

    detail_condition = get_search_detail_keyword('')

    height = style.height if style else None

    index_link_list = []
    for index in Index.query.all():
        if index.index_link_enabled and index.public_state:
            if hasattr(current_i18n, 'language'):
                if current_i18n.language == 'ja' and index.index_link_name:
                    index_link_list.append((index.id, index.index_link_name))
                else:
                    index_link_list.append(
                        (index.id, index.index_link_name_english))
            else:
                index_link_list.append(
                    (index.id, index.index_link_name_english))

    if 'item_management' in getArgs:
        management_type = request.args.get('item_management', 'sort')

        has_items = False
        has_child_trees = False
        if management_type == 'delete':
            # Does this tree has items or children?
            q = request.args.get('q')
            if q is not None and q.isdigit():
                    current_tree = Indexes.get_index(q)
                    recursive_tree = Indexes.get_recursive_tree(q)

                    if current_tree is not None:
                        tree_items = get_tree_items(current_tree.id)
                        has_items = len(tree_items) > 0
                        if recursive_tree is not None:
                            has_child_trees = len(recursive_tree) > 1

        return render_template(current_app.config['WEKO_ITEM_MANAGEMENT_TEMPLATE'],
                               index_id=cur_index_id, community_id=community_id,
                               width=width, height=height, management_type=management_type,
                               fields=current_app.config['WEKO_RECORDS_UI_BULK_UPDATE_FIELDS']['fields'],
                               licences=current_app.config['WEKO_RECORDS_UI_BULK_UPDATE_FIELDS']['licences'],
                               has_items=has_items,
                               has_child_trees=has_child_trees,
                               detail_condition=detail_condition, **ctx)

    elif 'item_link' in getArgs:
        activity_id = request.args.get('item_link')
        from weko_workflow.api import WorkActivity
        workFlowActivity = WorkActivity()
        activity_detail, item, steps, action_id, cur_step, temporary_comment, approval_record, \
            step_item_login_url, histories, res_check, pid, community_id, ctx \
            = workFlowActivity.get_activity_index_search(activity_id=activity_id)
        return render_template(
            'weko_workflow/activity_detail.html',
            activity=activity_detail,
            item=item,
            steps=steps,
            action_id=action_id,
            cur_step=cur_step,
            temporary_comment=temporary_comment,
            record=approval_record,
            step_item_login_url=step_item_login_url,
            histories=histories,
            res_check=res_check,
            pid=pid,
            index_id=cur_index_id,
            community_id=community_id,
            width=width,
            height=height,
            **ctx)
    else:
        journal_info = None
        if search_type in ('0', '1', '2'):
            searched.send(
                current_app._get_current_object(),
                search_args=getArgs)
            if search_type == '2':
                cur_index_id = request.args.get('q', '0')
                journal_info = get_journal_info(cur_index_id)
        return render_template(
            current_app.config['SEARCH_UI_SEARCH_TEMPLATE'],
            index_id=cur_index_id,
            community_id=community_id,
            sort_option=sort_options,
            disply_setting=disply_setting,
            detail_condition=detail_condition,
            width=width,
            height=height,
            index_link_enabled=style.index_link_enabled,
            index_link_list=index_link_list,
            journal_info=journal_info,
            **ctx)


@blueprint_api.route('/opensearch/description.xml', methods=['GET'])
def opensearch_description():
    """Returns WEKO3 opensearch description document.

    :return:
    """
    # create a response
    response = current_app.response_class()

    # set the returned data, which will just contain the title
    ns_opensearch = "http://a9.com/-/spec/opensearch/1.1/"
    # ns_jairo = "jairo.nii.ac.jp/opensearch/1.0/"

    ET.register_namespace('', ns_opensearch)
    # ET.register_namespace('jairo', ns_jairo)

    root = ET.Element('OpenSearchDescription')

    sname = ET.SubElement(root, '{' + ns_opensearch + '}ShortName')
    sname.text = current_app.config['WEKO_OPENSEARCH_SYSTEM_SHORTNAME']

    des = ET.SubElement(root, '{' + ns_opensearch + '}Description')
    des.text = current_app.config['WEKO_OPENSEARCH_SYSTEM_DESCRIPTION']

    img = ET.SubElement(root, '{' + ns_opensearch + '}Image')
    img.set('height', '16')
    img.set('width', '16')
    img.set('type', 'image/x-icon')
    img.text = request.host_url + \
        current_app.config['WEKO_OPENSEARCH_IMAGE_URL']

    url = ET.SubElement(root, '{' + ns_opensearch + '}Url')
    url.set('type', 'application/atom+xml')
    url.set('template', request.host_url +
            'api/opensearch/search?q={searchTerms}')

    url = ET.SubElement(root, '{' + ns_opensearch + '}Url')
    url.set('type', 'application/atom+xml')
    url.set('template', request.host_url +
            'api/opensearch/search?q={searchTerms}&amp;format=atom')

    response.data = ET.tostring(root)

    # update headers
    response.headers['Content-Type'] = 'application/xml'
    return response


def get_tree_items(index_tree_id):
    """Get tree items."""
    records_search = RecordsSearch()
    records_search = records_search.with_preference_param().params(version=False)
    records_search._index[0] = current_app.config['SEARCH_UI_SEARCH_INDEX']
    search_instance, qs_kwargs = item_path_search_factory(None,
                                                          records_search,
                                                          index_id=index_tree_id)
    search_result = search_instance.execute()
    rd = search_result.to_dict()

    return rd.get('hits').get('hits')


@blueprint.route("/item_management/bulk_delete", methods=['GET', 'PUT'])
def bulk_delete():
    """Bulk delete items and index trees."""

    def delete_records(index_tree_id):
        record_indexer = RecordIndexer()
        hits = get_tree_items(index_tree_id)
        for hit in hits:
            recid = hit.get('_id')
            record = Record.get_record(recid)
            if record is not None and record['path'] is not None:
                paths = record['path']
                if len(paths) > 0:
                    # Remove the element which matches the index_tree_id
                    for path in paths:
                        if path.endswith(str(index_tree_id)):
                            paths.remove(path)
                            break

                    # Do update the path on record
                    record.update({'path': paths})
                    record.commit()
                    db.session.commit()

                    # Indexing
                    indexer = WekoIndexer()
                    indexer.update_path(record, update_revision=False)

                    if len(paths) == 0:
                        record_indexer.delete_by_id(recid)
                        pids = PersistentIdentifier.query.filter_by(
                            object_uuid=recid).all()
                        for pid in pids:
                            db.session.delete(pid)  # Delete PersistentId
                        db.session.commit()  # terminate the transaction

    if request.method == 'PUT':
        # Do delete items inside the current index tree (maybe root tree)
        q = request.values.get('q')
        if q is not None and q.isdigit():
            current_tree = Indexes.get_index(q)
            recursive_tree = Indexes.get_recursive_tree(q)

            if current_tree is not None:

                # Delete items in current_tree
                delete_records(current_tree.id)

                # If recursively, then delete all child index trees and theirs items
                if request.values.get('recursively') == 'true' and recursive_tree is not None:
                    # Delete recursively
                    direct_child_trees = []
                    for index, obj in enumerate(recursive_tree):
                        if obj[1] != current_tree.id:
                            child_tree = Indexes.get_index(obj[1])

                            # Do delete items in child_tree
                            delete_records(child_tree.id)

                            # Add the level 1 child into the current_tree
                            if obj[0] == current_tree.id:
                                direct_child_trees.append(child_tree.id)
                    # Then do delete child_tree inside current_tree
                    for cid in direct_child_trees:
                        # Delete this tree and children
                        Indexes.delete(cid)

                return jsonify({'status': 1})
        else:
            return jsonify({'status': 0, 'msg': 'Invalid tree'})

    """Render view."""
    detail_condition = get_search_detail_keyword('')
    return render_template(current_app.config['WEKO_ITEM_MANAGEMENT_TEMPLATE'],
                           management_type='delete',
                           detail_condition=detail_condition)


@blueprint.route("/item_management/save", methods=['POST'])
def save_sort():
    """Save custom sort."""
    try:
        data = request.get_json()
        index_id = data.get("q_id")
        sort_data = data.get("sort")

        # save data to DB
        item_sort = {}
        for sort in sort_data:
            item_sort[sort.get('id')] = sort.get('custom_sort').get(index_id)

        Indexes.set_item_sort_custom(index_id, item_sort)

        # update es
        fp = Indexes.get_self_path(index_id)
        Indexes.update_item_sort_custom_es(fp.path, sort_data)

        jfy = {}
        jfy['status'] = 200
        jfy['message'] = 'Data is successfully updated.'
        return make_response(jsonify(jfy), jfy['status'])
    except Exception as ex:
        jfy['status'] = 405
        jfy['message'] = 'Error'
        return make_response(jsonify(jfy), jfy['status'])


def get_journal_info(index_id=0):
    """Get journal information.

    :return: The object.
    """
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

        result = {}
        for value in schema_data:
            title = value.get('title_i18n')
            if title is not None:
                data = journal.get(value['key'])
                dataMap = value.get('titleMap')
                if dataMap is not None:
                    res = [x['name'] for x in dataMap if x['value'] == data]
                    data = res[0]
                val = title.get(cur_lang) + '{0}{1}'.format(': ', data)
                result.update({value['key']: val})
        # real url: ?action=repository_opensearch&index_id=
        result.update({'openSearchUrl': request.url_root +
                       "search?search_type=2&q={}".format(index_id)})

    except BaseException:
        current_app.logger.error('Unexpected error: ', sys.exc_info()[0])
        abort(500)
    return result


@blueprint.route("/journal_info/<int:index_id>", methods=['GET'])
def journal_detail(index_id=0):
    """Render a check view."""
    result = get_journal_info(index_id)
    return jsonify(result)


@blueprint.route("/item_management/custom_sort", methods=['GET'])
def custom_sort():
    """Render view."""
    return render_template(current_app.config['WEKO_ITEM_MANAGEMENT_TEMPLATE'],
                           management_type='sort')
