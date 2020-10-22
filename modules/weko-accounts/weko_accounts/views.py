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

"""Views for weko-accounts.

Set the templates and static folders as well as the test case by flask
Blueprint.
"""

import json
import sys

import redis
from flask import Blueprint, abort, current_app, flash, redirect, \
    render_template, request, session, url_for
from flask_babelex import gettext as _
from flask_login import current_user
from flask_security import url_for_security
from invenio_admin.proxies import current_admin
from simplekv.memory.redisstore import RedisStore
from werkzeug.local import LocalProxy

from .api import ShibUser
from .utils import generate_random_str, parse_attributes

_app = LocalProxy(lambda: current_app.extensions['weko-admin'].app)

blueprint = Blueprint(
    'weko_accounts',
    __name__,
    template_folder='templates',
    static_folder='static',
    url_prefix='/weko',
)


def _has_admin_access():
    """Use to check if a user has any admin access."""
    return current_user.is_authenticated and current_admin \
        .permission_factory(current_admin.admin.index_view).can()


@blueprint.route('/')
def index():
    """Render a basic view."""
    return render_template(
        'weko_accounts/index.html',
        module_name=_('WEKO-Accounts'))


@blueprint.route('/auto/login', methods=['GET'])
def shib_auto_login():
    """Create new account and auto login when shibboleth user first login.

    :return: next url
    """
    _shib_login_url = current_app.config['WEKO_ACCOUNTS_SHIB_IDP_LOGIN_URL']
    try:
        is_auto_bind = False
        shib_session_id = request.args.get('SHIB_ATTR_SESSION_ID', None)
        if not shib_session_id:
            shib_session_id = session['shib_session_id']
            is_auto_bind = True

        if shib_session_id is None or len(shib_session_id) == 0:
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))

        datastore = RedisStore(redis.StrictRedis.from_url(
            current_app.config['CACHE_REDIS_URL']))
        cache_key = current_app.config[
            'WEKO_ACCOUNTS_SHIB_CACHE_PREFIX'] + shib_session_id
        if not datastore.redis.exists(cache_key):
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))

        cache_val = datastore.get(cache_key)
        if not cache_val:
            datastore.delete(cache_key)
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))

        cache_val = json.loads(str(cache_val, encoding='utf-8'))
        shib_user = ShibUser(cache_val)
        if not is_auto_bind:
            shib_user.get_relation_info()
        else:
            shib_user.new_relation_info()

        error = shib_user.check_in()

        if error:
            ShibUser.shib_user_logout()
            datastore.delete(cache_key)
            current_app.logger.error(error)
            flash(error, category='error')
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))

        if shib_user.shib_user:
            shib_user.shib_user_login()

        datastore.delete(cache_key)
        return redirect(session['next'] if 'next' in session else '/')
    except BaseException:
        current_app.logger.error('Unexpected error: ', sys.exc_info()[0])
    return abort(400)


@blueprint.route('/confim/user', methods=['POST'])
def confirm_user():
    """Check weko user info.

    :return:
    """
    _shib_login_url = current_app.config['WEKO_ACCOUNTS_SHIB_IDP_LOGIN_URL']
    try:
        csrf_random = request.form.get('csrf_random', '')
        if csrf_random != session['csrf_random']:
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))
        shib_session_id = session['shib_session_id']
        if shib_session_id is None or len(shib_session_id) == 0:
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))
        datastore = RedisStore(redis.StrictRedis.from_url(
            current_app.config['CACHE_REDIS_URL']))
        cache_key = current_app.config[
            'WEKO_ACCOUNTS_SHIB_CACHE_PREFIX'] + shib_session_id
        if not datastore.redis.exists(cache_key):
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))
        cache_val = datastore.get(cache_key)
        if cache_val is None:
            datastore.delete(cache_key)
            return redirect(_shib_login_url.format(request.url_root))
        cache_val = json.loads(str(cache_val, encoding='utf-8'))
        shib_user = ShibUser(cache_val)
        account = request.form.get('WEKO_ATTR_ACCOUNT', None)
        password = request.form.get('WEKO_ATTR_PWD', None)
        if not shib_user.check_weko_user(account, password):
            datastore.delete(cache_key)
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))
        shib_user.bind_relation_info(account)

        error = shib_user.check_in()

        if error:
            datastore.delete(cache_key)
            flash(error, category='error')
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))

        if shib_user.shib_user:
            shib_user.shib_user_login()
        datastore.delete(cache_key)
        return redirect(session['next'] if 'next' in session else '/')
    except BaseException:
        current_app.logger.error('Unexpected error: ', sys.exc_info()[0])
    return abort(400)


@blueprint.route('/shib/login', methods=['GET'])
def shib_login():
    """Get shibboleth user login page.

    :return: confirm user page when relation is empty
    """
    _shib_login_url = current_app.config['WEKO_ACCOUNTS_SHIB_IDP_LOGIN_URL']
    try:
        shib_session_id = request.args.get('SHIB_ATTR_SESSION_ID', None)

        if shib_session_id is None or len(shib_session_id) == 0:
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))

        datastore = RedisStore(redis.StrictRedis.from_url(
            current_app.config['CACHE_REDIS_URL']))
        cache_key = current_app.config[
            'WEKO_ACCOUNTS_SHIB_CACHE_PREFIX'] + shib_session_id

        if not datastore.redis.exists(cache_key):
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))
        cache_val = datastore.get(cache_key)

        if not cache_val:
            datastore.delete(cache_key)
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))

        cache_val = json.loads(str(cache_val, encoding='utf-8'))
        session['shib_session_id'] = shib_session_id
        csrf_random = generate_random_str(length=64)
        session['csrf_random'] = csrf_random

        shib_role_auth = cache_val.get('shib_role_authority_name', '')
        if not shib_role_auth:
            current_app.logger.debug(_("Failed to get attribute."))

        shib_role_config = current_app.config[
            'WEKO_ACCOUNTS_SHIB_ROLE_RELATION']

        if shib_role_auth and shib_role_auth not in shib_role_config.keys():
            current_app.logger.error(_("Invalid attribute."))
            flash(_("Invalid attribute."), category='error')
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))

        return render_template(
            current_app.config['WEKO_ACCOUNTS_CONFIRM_USER_TEMPLATE'],
            csrf_random=csrf_random,
            email=cache_val['shib_mail'] if len(
                cache_val['shib_mail']) > 0 else '')
    except BaseException:
        current_app.logger.error('Unexpected error: ', sys.exc_info()[0])

    return abort(400)


@blueprint.route('/shib/login', methods=['POST'])
def shib_sp_login():
    """The request from shibboleth sp.

    :return: confirm page when relation is empty
    """
    _shib_login_url = current_app.config['WEKO_ACCOUNTS_SHIB_IDP_LOGIN_URL']
    try:
        shib_session_id = request.form.get('SHIB_ATTR_SESSION_ID', None)
        if not shib_session_id and not current_app.config[
                'WEKO_ACCOUNTS_SHIB_LOGIN_ENABLED']:
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))

        shib_attr, error = parse_attributes()
        if error:
            if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                    and current_app.config.get(
                        'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
                return redirect(_shib_login_url.format(request.url_root))
            else:
                return redirect(url_for_security('login'))

        datastore = RedisStore(redis.StrictRedis.from_url(
            current_app.config['CACHE_REDIS_URL']))
        ttl_sec = int(current_app.config[
            'WEKO_ACCOUNTS_SHIB_LOGIN_CACHE_TTL'])
        datastore.put(
            current_app.config[
                'WEKO_ACCOUNTS_SHIB_CACHE_PREFIX'] + shib_session_id,
            bytes(json.dumps(shib_attr), encoding='utf-8'),
            ttl_secs=ttl_sec)

        shib_user = ShibUser(shib_attr)
        # Check the relation of shibboleth user with weko account.
        rst = shib_user.get_relation_info()

        next_url = 'weko_accounts.shib_auto_login'
        if rst is None:
            # Relation is not existed, cache shibboleth info to redis.
            next_url = 'weko_accounts.shib_login'

        query_string = {
            'SHIB_ATTR_SESSION_ID': shib_session_id,
            '_method': 'GET'
        }
        return url_for(next_url, **query_string)
    except BaseException:
        current_app.logger.error('Unexpected error: ', sys.exc_info()[0])
        if current_app.config.get('WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED') \
                and current_app.config.get(
                    'WEKO_ACCOUNTS_SHIB_INST_LOGIN_DIRECTLY_ENABLED'):
            return redirect(_shib_login_url.format(request.url_root))
        else:
            return redirect(url_for_security('login'))


@blueprint.route('/shib/sp/login', methods=['GET'])
def shib_stub_login():
    """Shibboleth SP login redirect.

    :return:
    """
    _shib_login_url = current_app.config['WEKO_ACCOUNTS_SHIB_IDP_LOGIN_URL']
    if not current_app.config['WEKO_ACCOUNTS_SHIB_LOGIN_ENABLED']:
        return abort(403)

    session['next'] = request.args.get('next', '/')

    # LOGIN USING JAIROCLOUD PAGE
    if current_app.config['WEKO_ACCOUNTS_SHIB_IDP_LOGIN_ENABLED']:
        return redirect(_shib_login_url.format(request.url_root))
    else:
        return render_template(
            current_app.config[
                'WEKO_ACCOUNTS_SECURITY_LOGIN_SHIB_USER_TEMPLATE'],
            module_name=_('WEKO-Accounts'))


@blueprint.route('/shib/logout')
def shib_logout():
    """Shibboleth user logout.

    :return:
    """
    ShibUser.shib_user_logout()
    return 'logout success'
