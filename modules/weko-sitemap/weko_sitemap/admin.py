# -*- coding: utf-8 -*-
#
# Copyright (C) 2019 National Institute of Informatics.
#
# weko-sitemap is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Module of weko-sitemap."""

from __future__ import absolute_import, print_function

from urllib.parse import urlparse

from celery.task.control import inspect
from celery.result import AsyncResult
from flask import abort, current_app, jsonify, render_template, request, \
    url_for
from flask_admin import BaseView, expose
from flask_babelex import gettext as _


class SitemapSettingView(BaseView):
    """Sitemap setting view."""

    @expose('/', methods=['GET'])
    def index(self):
        """Update sitemap page."""
        return self.render(current_app.config["WEKO_SITEMAP_ADMIN_TEMPLATE"])

    @expose('/update_sitemap', methods=['POST'])
    def update_sitemap(self):
        """Start the task to update the sitemap."""
        from .tasks import update_sitemap
        baseurl = urlparse(request.base_url).netloc  # Celery cannot access config
        task = update_sitemap.delay(baseurl)
        # Get all tasks:
        return jsonify({'task_id': task.id, 'loc': url_for('.get_task_status', task_id=task.id)})

    @expose('/task_status/<string:task_id>', methods=['GET'])
    def get_task_status(self, task_id):
        """Get the status of the sitemap update task."""
        if not task_id:
            return abort(500)

        task_result = AsyncResult(task_id)  # TODO: Change the responses and the logic
        if task_result.state == 'SUCCESS':
            response = {
                'start_time': task_result.info['start_time'],
                'end_time': task_result.info['end_time'],
                'total': task_result.info['total'],
                'state': task_result.state
            }
        else:  # PENDING ERROR or other state
            response = {
                'start_time': '',
                'end_time': '',
                'total': '',
                'state': task_result.state
            }
        return jsonify(response)


sitemap_adminview = {
    'view_class': SitemapSettingView,
    'kwargs': {
        'category': _('Setting'),
        'name': _('Sitemap'),
        'endpoint': 'sitemap'
    }
}
