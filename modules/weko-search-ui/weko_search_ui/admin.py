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

import copy
import json
from datetime import datetime
from urllib.parse import urlencode

from blinker import Namespace
from flask import Response, abort, current_app, jsonify, make_response, request
from flask_admin import BaseView, expose
from flask_babelex import gettext as _
from weko_index_tree.api import Indexes
from weko_index_tree.models import IndexStyle
from weko_records.api import ItemTypes
from weko_workflow.api import WorkFlow

from weko_search_ui.api import get_search_detail_keyword

from .config import WEKO_EXPORT_TEMPLATE_BASIC_ID, \
    WEKO_EXPORT_TEMPLATE_BASIC_NAME, WEKO_EXPORT_TEMPLATE_BASIC_OPTION, \
    WEKO_IMPORT_CHECK_LIST_NAME, WEKO_IMPORT_LIST_NAME, \
    WEKO_ITEM_ADMIN_IMPORT_TEMPLATE
from .tasks import import_item, remove_temp_dir_task
from .utils import check_import_items, check_sub_item_is_system, \
    create_flow_define, delete_records, get_change_identifier_mode_content, \
    get_content_workflow, get_lifetime, get_root_item_option, \
    get_sub_item_option, get_tree_items, handle_index_tree, handle_workflow, \
    make_stats_tsv, make_tsv_by_line


_signals = Namespace()
searched = _signals.signal('searched')


class ItemManagementBulkDelete(BaseView):
    """Item Management - Bulk Delete view."""

    @expose('/', methods=['GET', 'PUT'])
    def index(self):
        """Bulk delete items and index trees."""
        if request.method == 'PUT':
            # Do delete items inside the current index tree (maybe root tree)
            q = request.values.get('q')
            if q is not None and q.isdigit():
                current_tree = Indexes.get_index(q)
                recursive_tree = Indexes.get_recursive_tree(q)

                if current_tree is not None:

                    # Delete items in current_tree
                    delete_records(current_tree.id)

                    # If recursively, then delete items of child indices
                    if request.values.get('recursively') == 'true'\
                            and recursive_tree is not None:
                        # Delete recursively
                        direct_child_trees = []
                        for obj in recursive_tree:
                            if obj[1] != current_tree.id:
                                child_tree = Indexes.get_index(obj[1])

                                # Do delete items in child_tree
                                delete_records(child_tree.id)

                                # Add the level 1 child into the current_tree
                                if obj[0] == current_tree.id:
                                    direct_child_trees.append(child_tree.id)

                    return jsonify({'status': 1})
            else:
                return jsonify({'status': 0, 'msg': 'Invalid tree'})

        """Render view."""
        detail_condition = get_search_detail_keyword('')
        return self.render(
            current_app.config['WEKO_THEME_ADMIN_ITEM_MANAGEMENT_TEMPLATE'],
            management_type='delete',
            detail_condition=detail_condition
        )


class ItemManagementCustomSort(BaseView):
    """Item Management - Custom Sort view."""

    @expose('/', methods=['GET'])
    def index(self):
        """Custom sort index."""
        return self.render(
            current_app.config['WEKO_THEME_ADMIN_ITEM_MANAGEMENT_TEMPLATE'],
            management_type='sort',
        )

    @expose('/save', methods=['POST'])
    def save_sort(self):
        """Save custom sort."""
        try:
            data = request.get_json()
            index_id = data.get("q_id")
            sort_data = data.get("sort")

            # save data to DB
            item_sort = {}
            for sort in sort_data:
                sd = sort.get('custom_sort').get(index_id)
                if sd:
                    item_sort[sort.get('id')] = sd

            Indexes.set_item_sort_custom(index_id, item_sort)

            # update es
            # fp = Indexes.get_self_path(index_id)
            # Indexes.update_item_sort_custom_es(fp.path, sort_data)

            jfy = {'status': 200, 'message': 'Data is successfully updated.'}
        except Exception:
            jfy = {'status': 405, 'message': 'Error.'}
        return make_response(jsonify(jfy), jfy['status'])


class ItemManagementBulkSearch(BaseView):
    """Item Management - Search."""

    @expose('/', methods=['GET'])
    def index(self):
        """Index Search page ui."""
        search_type = request.args.get('search_type', '0')
        get_args = request.args
        community_id = ""
        ctx = {'community': None}
        cur_index_id = search_type if search_type not in ('0', '1', ) else None
        if 'community' in get_args:
            from weko_workflow.api import GetCommunity
            comm = GetCommunity.get_community_by_id(
                request.args.get('community'))
            ctx = {'community': comm}
            community_id = comm.id

        # Get index style
        style = IndexStyle.get(
            current_app.config['WEKO_INDEX_TREE_STYLE_OPTIONS']['id'])
        width = style.width if style else '3'

        detail_condition = get_search_detail_keyword('')

        height = style.height if style else None
        header = ''
        if 'item_management' in get_args:
            management_type = request.args.get('item_management', 'sort')
            has_items = False
            has_child_trees = False
            header = _('Custom Sort')
            if management_type == 'delete':
                header = _('Bulk Delete')
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
            elif management_type == 'update':
                header = _('Bulk Update')

            return self.render(
                current_app.config[
                    'WEKO_THEME_ADMIN_ITEM_MANAGEMENT_TEMPLATE'],
                index_id=cur_index_id,
                community_id=community_id,
                width=width,
                height=height,
                header=header,
                management_type=management_type,
                fields=current_app.config[
                    'WEKO_RECORDS_UI_BULK_UPDATE_FIELDS']['fields'],
                licences=current_app.config[
                    'WEKO_RECORDS_UI_LICENSE_DICT'],
                has_items=has_items,
                has_child_trees=has_child_trees,
                detail_condition=detail_condition,
                **ctx)
        else:
            return abort(500)

    @staticmethod
    def is_visible():
        """Should never be visible."""
        return False


class ItemImportView(BaseView):
    """BaseView for Admin Import."""

    @expose('/', methods=['GET'])
    def index(self):
        """Renders an item import view.

        :param
        :return: The rendered template.
        """
        workflow = WorkFlow()
        workflows = workflow.get_workflow_list()
        workflows_js = [get_content_workflow(item) for item in workflows]

        return self.render(
            WEKO_ITEM_ADMIN_IMPORT_TEMPLATE,
            workflows=json.dumps(workflows_js)
        )

    @expose('/check', methods=['POST'])
    def check(self) -> jsonify:
        """Validate item import."""
        data = request.get_json()
        list_record = []
        data_path = ''

        if data:
            result = check_import_items(
                data.get('file').split(",")[-1],
                data.get('is_change_identifier')
            )
            if isinstance(result, dict):
                if result.get('error'):
                    return jsonify(code=0, error=result.get('error'))
                else:
                    list_record = result.get('list_record', [])
                    data_path = result.get('data_path', '')
        remove_temp_dir_task.apply_async(
            (data_path,), countdown=get_lifetime())
        return jsonify(code=1, list_record=list_record, data_path=data_path)

    @expose('/download_check', methods=['POST'])
    def download_check(self):
        """Download report check result."""
        data = request.get_json()
        now = str(datetime.date(datetime.now()))

        file_name = "check_" + now + ".tsv"
        if data:
            tsv_file = make_stats_tsv(
                data.get('list_result'),
                WEKO_IMPORT_CHECK_LIST_NAME
            )
            return Response(
                tsv_file.getvalue(),
                mimetype="text/tsv",
                headers={
                    "Content-disposition": "attachment; filename=" + file_name
                }
            )
        else:
            return Response(
                [],
                mimetype="text/tsv",
                headers={
                    "Content-disposition": "attachment; filename=" + file_name
                }
            )

    @expose('/import', methods=['POST'])
    def import_items(self) -> jsonify:
        """Import item into System."""
        url_root = request.url_root
        data = request.get_json() or {}
        tasks = []
        list_record = [item for item in data.get(
            'list_record', []) if not item.get(
            'errors')]
        for item in list_record:
            handle_index_tree(item)
            item['root_path'] = data.get('root_path')
            create_flow_define()
            handle_workflow(item)
            task = import_item.delay(item, url_root)
            tasks.append({
                'task_id': task.task_id,
                'item_id': item.get('id'),
            })
        response_object = {
            "status": "success",
            "data": {
                "tasks": tasks
            }
        }
        return jsonify(response_object)

    @expose("/check_status", methods=["POST"])
    def get_status(self):
        """Get status of import process."""
        data = request.get_json()
        result = []
        if data and data.get('tasks'):
            status = 'done'
            for task_item in data.get('tasks'):
                task_id = task_item.get('task_id')
                task = import_item.AsyncResult(task_id)
                start_date = task.result.get(
                    "start_date"
                ) if task and isinstance(task.result, dict) else ""
                end_date = datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ) if task.successful() or task.failed() else ""
                result.append(dict(**{
                    "task_status": task.status,
                    "task_result": task.result,
                    "start_date": start_date,
                    "end_date": task_item.get("end_date") or end_date,
                    "task_id": task_id,
                    "item_id": task_item.get("item_id"),
                }))
                status = 'doing' if not (task.successful() or task.failed())\
                    else "done"
            response_object = {"status": status, "result": result}
        else:
            response_object = {"status": "error", "result": result}
        return jsonify(response_object)

    @expose('/export_import', methods=['POST'])
    def download_import(self):
        """Download import result."""
        data = request.get_json()
        now = str(datetime.date(datetime.now()))

        file_name = "List_Download " + now + ".tsv"
        if data:
            tsv_file = make_stats_tsv(
                data.get('list_result'),
                WEKO_IMPORT_LIST_NAME
            )
            return Response(
                tsv_file.getvalue(),
                mimetype="text/tsv",
                headers={
                    "Content-disposition": "attachment; filename=" + file_name
                }
            )
        else:
            return Response(
                [],
                mimetype="text/tsv",
                headers={
                    "Content-disposition": "attachment; filename=" + file_name
                }
            )

    @expose('/get_disclaimer_text', methods=['GET'])
    def get_disclaimer_text(self):
        """Get disclaimer text."""
        data = get_change_identifier_mode_content()
        return jsonify(code=1, data=data)

    @expose('/export_template', methods=['POST'])
    def export_template(self):
        """Download item type template."""
        def handle_sub_item(items, root_id=None, root_name=None):
            """Handle if is sub-item."""
            ids, names = [], []
            for key in sorted(items.keys()):
                item = items.get(key)
                if item.get('items'):
                    _ids, _names = handle_sub_item(
                        item.get('items').get('properties'))
                    ids += [key + '[0].' + _id for _id in _ids]
                    names += [item.get('title') + '#1.' + _name
                              for _name in _names]
                elif item.get('type') == 'object' and item.get('properties'):
                    _ids, _names = handle_sub_item(item.get('properties'))
                    ids += [key + '.' + _id for _id in _ids]
                    names += [item.get('title') + '.' + _name
                              for _name in _names]
                else:
                    ids.append(key)
                    names.append(item.get('title'))

            if root_id and root_name:
                ids = [root_id + '.' + _id for _id in ids]
                names = [root_name + '.' + _name
                         for _name in names]

            return ids, names

        result = Response(
            [],
            mimetype="text/tsv",
            headers={
                "Content-disposition": "attachment; filename="
            }
        )

        data = request.get_json()
        if data:
            item_type_id = int(data.get('item_type_id', 0))
            if item_type_id > 0:
                item_type = ItemTypes.get_by_id(
                    id_=item_type_id, with_deleted=True)
                if item_type:
                    file_name = '{}({}).tsv'.format(
                        item_type.item_type_name.name, item_type.id)
                    item_type_line = [
                        '#ItemType',
                        '{}({})'.format(
                            item_type.item_type_name.name, item_type.id),
                        '{}items/jsonschema/{}'.format(
                            request.url_root, item_type.id)
                    ]
                    ids_line = copy.deepcopy(WEKO_EXPORT_TEMPLATE_BASIC_ID)
                    names_line = copy.deepcopy(WEKO_EXPORT_TEMPLATE_BASIC_NAME)
                    systems_line = ['#'] + \
                        ['' for _ in range(len(ids_line) - 1)]
                    options_line = copy.deepcopy(
                        WEKO_EXPORT_TEMPLATE_BASIC_OPTION)

                    item_type = item_type.render
                    meta_fix = item_type.get('meta_fix', {})
                    meta_list = item_type.get('meta_list', {})
                    schema = item_type.get(
                        'schemaeditor', {}).get('schema', {})
                    form = item_type.get(
                        'table_row_map', {}).get('form', {})
                    for key, value in meta_fix.items():
                        _id, _name, _option = get_root_item_option(key, value)
                        ids_line.append(_id)
                        names_line.append(_name)
                        systems_line.append('')
                        options_line.append(', '.join(_option))

                    count_file = 1
                    for key in item_type.get('table_row', {}):
                        value = meta_list.get(key, {})
                        if key in schema:
                            item = schema.get(key)
                            root_id, root_name, root_option = \
                                get_root_item_option(key, value)
                            _ids, _names = handle_sub_item(
                                item.get('properties'), root_id, root_name)

                            _options = []
                            for _id in _ids:
                                if 'filename' in _id \
                                        or 'thumbnail_label' in _id:
                                    ids_line.append(
                                        '.file_path#{}'.format(count_file))
                                    names_line.append(
                                        '.ファイルパス#{}'.format(count_file))
                                    systems_line.append('')
                                    options_line.append('')
                                    count_file += 1

                                clean_key = _id.replace(
                                    '.metadata.', '').replace('[0]', '[]')
                                _options.append(
                                    get_sub_item_option(clean_key, form) or [])
                                systems_line.append(
                                    'System' if check_sub_item_is_system(
                                        clean_key, form) else ''
                                )

                            ids_line += _ids
                            names_line += _names
                            for _option in _options:
                                options_line.append(
                                    ', '.join(list(set(root_option + _option)))
                                )
                    tsv_file = make_tsv_by_line([
                        item_type_line,
                        ids_line,
                        names_line,
                        systems_line,
                        options_line
                    ])
                    result = Response(
                        tsv_file.getvalue(),
                        mimetype="text/tsv",
                        headers={
                            "Content-disposition": "attachment; "
                            + urlencode({'filename': file_name})
                        })
        return result


item_management_bulk_search_adminview = {
    'view_class': ItemManagementBulkSearch,
    'kwargs': {
        'endpoint': 'items/search',
        'category': _('Index Tree'),
        'name': '',
    }
}

item_management_bulk_delete_adminview = {
    'view_class': ItemManagementBulkDelete,
    'kwargs': {
        'category': _('Items'),
        'name': _('Bulk Delete'),
        'endpoint': 'items/bulk/delete'
    }
}

item_management_custom_sort_adminview = {
    'view_class': ItemManagementCustomSort,
    'kwargs': {
        'category': _('Index Tree'),
        'name': _('Custom Sort'),
        'endpoint': 'items/custom_sort'
    }
}

item_management_import_adminview = {
    'view_class': ItemImportView,
    'kwargs': {
        'category': _('Items'),
        'name': _('Import'),
        'endpoint': 'items/import'
    }
}

__all__ = (
    'item_management_bulk_delete_adminview',
    'item_management_bulk_search_adminview',
    'item_management_custom_sort_adminview',
    'item_management_import_adminview'
)
