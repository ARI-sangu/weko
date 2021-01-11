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

"""WEKO3 module docstring."""

import re
import uuid

from flask import abort, current_app, jsonify, request, url_for
from flask_admin import BaseView, expose
from flask_babelex import gettext as _
from invenio_accounts.models import Role, User
from weko_index_tree.models import Index
from weko_records.api import ItemTypes

from weko_workflow.models import Action as _Action
from weko_workflow.models import FlowAction

from .api import Action, Flow, WorkActivity, WorkFlow
from .config import WEKO_WORKFLOW_SHOW_HARVESTING_ITEMS


class FlowSettingView(BaseView):
    @expose('/', methods=['GET'])
    def index(self):
        """Get flow list info.

        :return:
        """
        workflow = Flow()
        flows = workflow.get_flow_list()
        return self.render(
            'weko_workflow/admin/flow_list.html',
            flows=flows
        )

    @expose('/<string:flow_id>', methods=['GET'])
    def flow_detail(self, flow_id='0'):
        """Get flow detail info.

        :param flow_id:
        :return:
        """
        users = User.query.filter_by(active=True).all()
        roles = Role.query.all()
        actions = self.get_actions()
        if '0' == flow_id:
            flow = None
            return self.render(
                'weko_workflow/admin/flow_detail.html',
                flow_id=flow_id,
                flow=flow,
                flows=None,
                users=users,
                roles=roles,
                actions=None,
                action_list=actions
            )
        UUID_PATTERN = re.compile(r'^[\da-f]{8}-([\da-f]{4}-){3}[\da-f]{12}$',
                                  re.IGNORECASE)
        if not UUID_PATTERN.match(flow_id):
            abort(404)
        workflow = Flow()
        flow = workflow.get_flow_detail(flow_id)
        return self.render(
            'weko_workflow/admin/flow_detail.html',
            flow_id=flow_id,
            flow=flow,
            flows=None,
            users=users,
            roles=roles,
            actions=flow.flow_actions,
            action_list=actions
        )

    @staticmethod
    def update_flow(flow_id):
        post_data = request.get_json()
        workflow = Flow()
        try:
            workflow.upt_flow(flow_id, post_data)
        except ValueError as ex:
            response = jsonify(msg=str(ex))
            response.status_code = 400
            return response

        return jsonify(code=0, msg=_('Updated flow successfully.'))

    @expose('/<string:flow_id>', methods=['POST'])
    def new_flow(self, flow_id='0'):
        if flow_id != '0':
            return self.update_flow(flow_id)

        post_data = request.get_json()
        workflow = Flow()
        try:
            flow = workflow.create_flow(post_data)
        except ValueError as ex:
            response = jsonify(msg=str(ex))
            response.status_code = 400
            return response

        redirect_url = url_for('flowsetting.flow_detail', flow_id=flow.flow_id)
        return jsonify(code=0, msg='', data={'redirect': redirect_url})

    @expose('/<string:flow_id>', methods=['DELETE'])
    def del_flow(self, flow_id='0'):
        """Delete Flow info."""
        if '0' == flow_id:
            return jsonify(code=500, msg='No data to delete.',
                           data={'redirect': url_for('flowsetting.index')})

        code = 0
        msg = ''

        flow = Flow()
        flow_detail = flow.get_flow_detail(flow_id)
        if flow_detail:
            workflow = WorkFlow()
            workflows = workflow.get_workflow_by_flow_id(flow_detail.id)
            if workflows and len(workflows) > 0:
                code = 500
                msg = 'Cannot be deleted because flow is used.'
            else:
                """Delete flow"""
                result = flow.del_flow(flow_id)
                code = result.get('code')
                msg = result.get('msg')

        return jsonify(code=code, msg=msg,
                       data={'redirect': url_for('flowsetting.index')})

    @staticmethod
    def get_actions():
        """Get Actions info."""
        actions = Action().get_action_list()
        action_list = list()
        for action in actions:
            if action.action_name in current_app.config[
                    'WEKO_WORKFLOW_ACTIONS']:
                action_list.append(action)
        return action_list

    @expose('/action/<string:flow_id>', methods=['POST'])
    def upt_flow_action(self, flow_id=0):
        """Update FlowAction Info."""
        actions = request.get_json()
        workflow = Flow()
        dict_code = workflow.upt_flow_action(flow_id, actions)
        list_code = dict_code.get('list_code')
        msg = ''
        dict_msg = []
        status = False
        for code in list_code:
            if code == 0:
                msg = _('Updated flow action successfully')
                status = True
            if code == 1:
                msg = _(
                    'Approval by Administrator action does not exist,'
                    ' or not in the right order.')
            elif code == 2:
                msg = _('Approval by Advisor action is not in the right order.')
            elif code == 3:
                msg = _(
                    'Approval by Guarantor action is not in the right order.')
            elif code == 4:
                msg = _('Start or End action is not in the right order.')
            dict_msg.append(dict(msg=msg))
        return jsonify(result=dict_msg, status=status)


class WorkFlowSettingView(BaseView):
    @expose('/', methods=['GET'])
    def index(self):
        """Get flow list info.

        :return:
        """
        workflow = WorkFlow()
        workflows = workflow.get_workflow_list()
        return self.render(
            'weko_workflow/admin/workflow_list.html',
            workflows=workflows
        )

    @expose('/<string:workflow_id>', methods=['GET'])
    def workflow_detail(self, workflow_id='0'):
        """Get workflow info.

        :return:
        """
        if WEKO_WORKFLOW_SHOW_HARVESTING_ITEMS:
            itemtype_list = ItemTypes.get_latest()
        else:
            itemtype_list = ItemTypes.get_latest_custorm_harvesting()
        flow_api = Flow()
        flow_list = flow_api.get_flow_list()
        specials_itemtypes_list = []
        index_list = None
        action = _Action.query.filter_by(
            action_endpoint='item_login_application').one_or_none()
        enable_auto_set_index = current_app.config.get(
            'WEKO_WORKFLOW_ENABLE_AUTO_SET_INDEX_FOR_ITEM_TYPE')
        if action and enable_auto_set_index:
            index_list = Index().get_all()
            for flow in flow_list:
                flow_action = FlowAction.query.filter_by(
                    flow_id=flow.flow_id, action_id=action.id).one_or_none()
                if flow_action:
                    specials_itemtypes_list.append(flow.flow_name)
        if len(specials_itemtypes_list) == 0:
            enable_auto_set_index = False
        if '0' == workflow_id:
            """Create new workflow"""
            return self.render(
                'weko_workflow/admin/workflow_detail.html',
                workflow=None,
                itemtype_list=itemtype_list,
                flow_list=flow_list,
                index_list=index_list,
                special_itemtype_list=specials_itemtypes_list,
                enable_showing_index_tree_selection=enable_auto_set_index
            )

        """Update the workflow info"""
        workflow = WorkFlow()
        workflows = workflow.get_workflow_detail(workflow_id)
        return self.render(
            'weko_workflow/admin/workflow_detail.html',
            workflow=workflows,
            itemtype_list=itemtype_list,
            flow_list=flow_list,
            index_list=index_list,
            special_itemtype_list=specials_itemtypes_list,
            enable_showing_index_tree_selection=enable_auto_set_index
        )

    @expose('/<string:workflow_id>', methods=['POST', 'PUT'])
    def update_workflow(self, workflow_id='0'):
        """Update workflow info.

        :return:
        """
        json_data = request.get_json()
        form_workflow = dict(
            flows_name=json_data.get('flows_name', None),
            itemtype_id=json_data.get('itemtype_id', 0),
            flow_id=json_data.get('flow_id', 0),
            index_tree_id=json_data.get('index_id')
        )
        workflow = WorkFlow()
        if '0' == workflow_id:
            """Create new workflow"""
            form_workflow.update(
                flows_id=uuid.uuid4()
            )
            workflow.create_workflow(form_workflow)
        else:
            """Update the workflow info"""
            form_workflow.update(
                id=json_data.get('id', None),
                flows_id=workflow_id
            )
            workflow.upt_workflow(form_workflow)
        return jsonify(code=0, msg='',
                       data={'redirect': url_for('workflowsetting.index')})

    @expose('/<string:workflow_id>', methods=['DELETE'])
    def delete_workflow(self, workflow_id='0'):
        """Update workflow info.

        :return:
        """
        workflow = WorkFlow()
        if '0' == workflow_id:
            return jsonify(code=500, msg='No data to delete.',
                           data={'redirect': url_for('workflowsetting.index')})

        code = 0
        msg = ''
        delete_flag = True

        workflow_detail = workflow.get_workflow_by_flows_id(workflow_id)
        if workflow_detail:
            activity = WorkActivity()
            activitys = activity.get_activity_by_workflow_id(workflow_detail.id)
            if activitys and len(activitys) > 0:
                for i in activitys:
                    if i.activity_status not in ['F', 'C']:
                        delete_flag = False
                        break

            if delete_flag:
                """Delete new workflow"""
                result = workflow.del_workflow(workflow_id)
                code = result.get('code')
                msg = result.get('msg')
            else:
                code = 500
                msg = 'Cannot be deleted because workflow is used.'

        return jsonify(code=code, msg=msg,
                       data={'redirect': url_for('workflowsetting.index')})


workflow_adminview = {
    'view_class': WorkFlowSettingView,
    'kwargs': {
        'category': _('WorkFlow'),
        'name': _('WorkFlow List'),
        'endpoint': 'workflowsetting'
    }
}

flow_adminview = {
    'view_class': FlowSettingView,
    'kwargs': {
        'category': _('WorkFlow'),
        'name': _('Flow List'),
        'endpoint': 'flowsetting'
    }
}

__all__ = (
    'flow_adminview',
    'workflow_adminview',
)
