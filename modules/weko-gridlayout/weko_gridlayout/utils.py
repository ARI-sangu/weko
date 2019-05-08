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

"""Utilities for convert response json."""
import json

from flask import jsonify, make_response
from invenio_db import db

from .api import WidgetItems
from .models import WidgetDesignSetting, WidgetItem, WidgetType


def get_repository_list():
    """Get repository list from Community table.

    :return: Repository list.
    """
    result = {
        "repositories": [{"id": "Root Index", "title": ""}],
        "error": ""
    }
    try:
        from invenio_communities.models import Community
        with db.session.no_autoflush:
            communities = Community.query.all()
        if communities:
            for community in communities:
                community_result = dict()
                community_result['id'] = community.id
                community_result['title'] = community.title
                result['repositories'].append(community_result)
    except Exception as e:
        result['error'] = str(e)

    return result


def get_widget_list(repository_id):
    """Get Widget list.

    :param repository_id: Identifier of the repository.
    :return: Widget list.
    """
    result = {
        "widget-list": [],
        "error": ""
    }
    try:
        with db.session.no_autoflush:
            widget_item_list = WidgetItem.query.filter_by(
                repository_id=repository_id, is_enabled=True, is_deleted=False
            ).all()
        if widget_item_list:
            for widget_item in widget_item_list:
                data = dict()
                data["widgetId"] = widget_item.repository_id
                data["widgetType"] = widget_item.widget_type
                data["widgetLabel"] = widget_item.label
                data["widgetSetting"] = {
                    "label_color": widget_item.label_color,
                    "frame_border": widget_item.has_frame_border,
                    "frame_border_color": widget_item.frame_border_color,
                    "text_color": widget_item.text_color,
                    "background_color": widget_item.background_color,
                    "browsing_role": widget_item.browsing_role,
                    "edit_role": widget_item.edit_role
                }
                result["widget-list"].append(data)
    except Exception as e:
        result["error"] = str(e)

    return result


def get_widget_design_setting(repository_id):
    """Get Widget design setting by repository id.

    :param repository_id: Identifier of the repository
    :return: Widget design setting json.
    """
    result = {
        "widget-settings": [
        ],
        "error": ""
    }
    try:
        widget_setting = WidgetDesignSetting.select_by_repository_id(
            repository_id)
        if widget_setting:
            settings = widget_setting.get('settings')
            if settings:
                result["widget-settings"] = json.loads(settings)
    except Exception as e:
        result['error'] = str(e)

    return result


def update_widget_design_setting(data):
    """Update Widget layout setting.

    :param data: json data is submitted from client side.
    :return: result json.
    """
    result = {
        "result": False,
        "error": ''
    }
    repository_id = data.get('repository_id')
    setting_data = data.get('settings')
    try:
        if repository_id and setting_data:
            if WidgetDesignSetting.select_by_repository_id(repository_id):
                result["result"] = WidgetDesignSetting.update(repository_id,
                                                              setting_data)
            else:
                result["result"] = WidgetDesignSetting.create(repository_id,
                                                              setting_data)
        else:
            result['error'] = "Fail to save Widget design. Please check again."
    except Exception as e:
        result['error'] = str(e)
    return result


def get_widget_type_list():
    """Get all Widget types.

    :param: None
    :return: options json
    """
    widget_types = WidgetType.get_all_widget_types()
    options = []
    for widget_type in widget_types:
        option = dict()
        option["text"] = widget_type.type_name
        option["value"] = widget_type.type_id
        options.append(option)
    result = {"options": options}

    return result


def update_admin_widget_item_setting(data):
    """Create/update widget item.

    :param: widget item data
    :return: options json
    """
    status = 201
    success = True
    msg = ""

    flag = data.get('flag_edit')
    data_result = data.get('data')
    data_id = data.get('data_id')
    if not data_result:
        # raise WidgetItemInvalidDataRESTError()
        success = True
        msg = 'Invalid data.'
    if flag:
        if success:
            if WidgetItems.is_existed(data_id):
                if validate_admin_widget_item_setting(data_id):
                    if not WidgetItems.update(data_result, data_id):
                        success = False
                        msg = 'Update widget item fail.'
                    else:
                        handle_change_item_in_preview_widget_item(data_id,
                                                                  data_result)
                        msg = 'Widget item updated successfully.'
                else:
                    if not WidgetItems.update(data_result, data_id):
                        success = False
                        msg = 'Update widget item fail.'
                    else:
                        msg = 'Widget item updated successfully.'
            else:
                msg = 'Fail to update. Can not find Widget item to edit.'
    else:
        if WidgetItems.is_existed(data_result):
            success = False
            msg = 'Fail to create. Data input to create is exist!'
        else:
            if not WidgetItems.create(data_result):
                # raise WidgetItemAddedRESTError()
                success = False
                msg = 'Create widget item fail.'
            else:
                msg = 'Widget item created successfully.'

    return make_response(
        jsonify({'status': status,
                 'success': success,
                 'message': msg}), status)


def delete_item_in_preview_widget_item(data_id, json_data):
    """Delete item in preview widget design.

    Arguments:
        data_id {widget_item} -- [id of widget item]
        json_data {dict} -- [data to be updated]

    Returns:
        [data] -- [data after updated]

    """
    remove_list = []
    for item in json_data:
        if str(item.get('name')) == str(data_id.get('label')) and str(
                item.get('type')) == str(data_id.get('widget_type')):
            remove_list.append(item)
    for item in remove_list:
        json_data.remove(item)
    data = json.dumps(json_data)
    return data


def update_item_in_preview_widget_item(data_id, data_result, json_data):
    """Update item in preview widget design when it is edited in widget item.

    Arguments:
        data_id {widget_item} -- [id of widget item]
        data_result {widget_item} -- [sent]
        json_data {dict} -- [data to be updated]
    Returns:
        [data] -- [data after updated]

    """
    for item in json_data:
        if str(item.get('name')) == str(data_id.get('label')) and str(
                item.get('type')) == str(data_id.get('widget_type')):
            item['frame_border'] = data_result.get('frame_border')
            item['frame_border_color'] = data_result.get('frame_border_color')
            item['background_color'] = data_result.get('background_color')
            item['type'] = data_result.get('widget_type')
            item['label_color'] = data_result.get('label_color')
            item['text_color'] = data_result.get('text_color')
            item['name'] = data_result.get('label')
    data = json.dumps(json_data)
    return data


def handle_change_item_in_preview_widget_item(data_id, data_result):
    """Handle change when edit widget item effect to widget design.

    Arguments:
        data_id {widget_item} -- [id of widget item]
        data_result {widget_item} -- [data is sent by client]

    Returns:
        [False] -- [handle failed]
        [True] -- [handle success]

    """
    try:
        data = WidgetDesignSetting.select_by_repository_id(
            data_id.get('repository'))
        if data.get('settings'):
            json_data = json.loads(data.get('settings'))
            if str(data_id.get('repository')) != str(data_result.get(
                    'repository')) or data_result.get('enable') is False:
                data = delete_item_in_preview_widget_item(data_id, json_data)
            else:
                data = update_item_in_preview_widget_item(
                    data_id, data_result, json_data)
            return WidgetDesignSetting.update(data_id.get('repository'), data)

        return False
    except Exception as e:
        print(e)
        return False


def delete_admin_widget_item_setting(widget_id):
    """Delete widget item.

    :param: widget id
    :return: options json
    """
    status = 201
    success = True
    if validate_admin_widget_item_setting(widget_id):
        success = False
        msg = 'Delete widget item fail. The item is used in widget design.'
    elif not WidgetItems.delete(widget_id):
        success = False
        msg = 'Delete widget item fail.'
    else:
        msg = 'Widget item delete successfully.'

    return make_response(
        jsonify({'status': status,
                 'success': success,
                 'message': msg}), status)


def validate_admin_widget_item_setting(widget_id):
    """Validate widget item.

    :param: widget id
    :return: true if widget item is used in widget design else return false
    """
    try:
        if (type(widget_id)) is dict:
            repository_id = widget_id.get('repository')
            widget_type = widget_id.get('widget_type')
            label = widget_id.get('label')
        else:
            repository_id = widget_id.repository_id
            widget_type = widget_id.widget_type
            label = widget_id.label
        data = WidgetDesignSetting.select_by_repository_id(
            repository_id)
        if data.get('settings'):
            json_data = json.loads(data.get('settings'))
            for item in json_data:
                if str(item.get('name')) == str(label) and str(
                        item.get('type')) == str(widget_type):
                    return True
        return False
    except Exception as e:
        print(e)
        return True
