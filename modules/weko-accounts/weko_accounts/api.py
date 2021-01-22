# -*- coding: utf-8 -*-
#
# Copyright (C) 2020 National Institute of Informatics.
#
# WEKO3 is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see LICENSE file for more
# details.

"""Shibboleth User API."""

from datetime import datetime

from flask import current_app, session
from flask_babelex import gettext as _
from flask_login import current_user, user_logged_in, user_logged_out
from flask_security.utils import hash_password, verify_password
from invenio_accounts.models import Role, User
from invenio_db import db
from weko_user_profiles.models import UserProfile
from werkzeug.local import LocalProxy

from .models import ShibbolethUser, ShibUserRole

_datastore = LocalProxy(lambda: current_app.extensions['security'].datastore)


class ShibUser(object):
    """Shibuser."""

    def __init__(self, shib_attr=None):
        """
        Class ShibUser initialization.

        :param shib_attr: passed attribute for shibboleth user
        """
        self.shib_attr = shib_attr
        self.user = None
        """The :class:`invenio_accounts.models.User` instance."""
        self.shib_user = None
        """The :class:`.models.ShibbolethUser` instance."""

    def _set_weko_user_role(self, roles):
        """
        Assign role for weko3 user.

        :param role_name:
        :return:

        """
        ret = True
        shib_user = ShibbolethUser.query.filter_by(
                shib_eppn=self.shib_attr['shib_eppn']).one_or_none()
        roles = [x.strip() for x in roles.split(',')]
        role_ids = [x.id for x in Role.query.filter(
            Role.name.in_(roles)).all()]

        try:
            with db.session.begin_nested():
                db.session.query(ShibUserRole).filter_by(
                    shib_user_id=shib_user.id).delete(synchronize_session='fetch')
                shib_userroles = []
                for _id in role_ids:
                    shib_userroles.append(ShibUserRole(
                        shib_user_id=shib_user.id,
                        role_id=_id
                    ))
                db.session.add_all(shib_userroles)
                db.session.commit()
        except Exception as ex:
            current_app.logger.debug("An error occurred when trying to add "
                                     "Role: {} to this User!".format(ex))
            db.session.rollback()
            ret = False
        return ret

    def _get_site_license(self):
        """
        Assign role for weko3 user.

        :param shib_role_auth:
        :return:

        """
        return self.shib_attr.get('shib_ip_range_flag', False)

    def get_relation_info(self):
        """
        Get weko user info by Shibboleth user info.

        :return: ShibbolethUser if exists relation else None

        """
        shib_user = None
        _shib_username_allowed = current_app.config[
            'WEKO_ACCOUNTS_SHIB_ALLOW_USERNAME_INST_EPPN']

        if self.shib_attr['shib_eppn']:
            shib_user = ShibbolethUser.query.filter_by(
                shib_eppn=self.shib_attr['shib_eppn']).one_or_none()
        elif _shib_username_allowed and self.shib_attr.get('shib_user_name'):
            shib_user = ShibbolethUser.query.filter_by(
                shib_user_name=self.shib_attr['shib_user_name']).one_or_none()

        if shib_user:
            self.shib_user = shib_user
            if not self.user:
                self.user = shib_user.weko_user

        return shib_user

    def check_weko_user(self, account, pwd):
        """
        Check weko user info.

        :param account:
        :param pwd:
        :return: Boolean

        """
        weko_user = _datastore.find_user(email=account)
        if weko_user is None:
            return False
        if not verify_password(pwd, weko_user.password):
            return False
        return True

    def bind_relation_info(self, account):
        """
        Create new relation info with the user who belong with the email.

        :return: ShibbolenUser instance

        """
        self.user = User.query.filter_by(email=account).one_or_none()
        shib_user = ShibbolethUser.create(self.user, **self.shib_attr)
        self.shib_user = shib_user
        return shib_user

    def new_relation_info(self):
        """
        Create new relation info for shibboleth user when first login weko3.

        :return: ShibbolethUser instance

        """
        kwargs = dict(
            email=self.shib_attr.get('shib_mail'),
            password=hash_password(''),
            confirmed_at=datetime.utcnow(),
            active=True
        )

        self.user = _datastore.create_user(**kwargs)
        shib_user = ShibbolethUser.create(self.user, **self.shib_attr)
        self.shib_user = shib_user
        return shib_user

    def new_shib_profile(self):
        """
        Create new profile info for shibboleth user.

        :return: UserProfile instance

        """
        with db.session.begin_nested():
            # create profile.
            userprofile = UserProfile(user_id=self.user.id,
                                      timezone=current_app.config[
                                          'USERPROFILES_TIMEZONE_DEFAULT'],
                                      language=current_app.config[
                                          'USERPROFILES_LANGUAGE_DEFAULT'])
            userprofile.username = self.shib_user.shib_user_name
            db.session.add(userprofile)
        db.session.commit()
        return userprofile

    def shib_user_login(self):
        """
        Create login info for shibboleth user.

        :return:

        """
        session['user_id'] = self.user.id
        session['user_src'] = 'Shib'
        user_logged_in.send(current_app._get_current_object(), user=self.user)

    def assign_user_role(self):
        """
        Check and set relation role for Weko3 user by wekoSocietyAffiliation.

        :return:

        """
        error = ''

        if not self.user:
            error = _(r"Can't get relation Weko User.")
            return False, error

        shib_role_auth = self.shib_attr.get('shib_role_authority_name', '')
        if not shib_role_auth:
            current_app.logger.debug(_("Failed to get attribute."))
            return self._set_weko_user_role(
                current_app.config['WEKO_ACCOUNTS_GENERAL_ROLE']), error

        shib_role_config = current_app.config[
            'WEKO_ACCOUNTS_SHIB_ROLE_RELATION']
        if shib_role_auth in shib_role_config.keys():
            return self._set_weko_user_role(shib_role_config[
                shib_role_auth]), error
        else:
            error = _("Invalid attribute.")

        return False, error

    def valid_site_license(self):
        """
        Get license from shib attr.

        :return:

        """
        if self._get_site_license():
            return True, ''
        else:
            return False, _('Failed to login.')

    def check_in(self):
        """
        Get and check-in Shibboleth attr data before login to system.

        :return:

        """
        check_role, error = self.assign_user_role()
        if not check_role:
            return error

        # ! NEED RELATION SHIB_ATTR
        # check_license, error = self.valid_site_license()
        # if not check_license:
        #     return error

        return None

    @classmethod
    def shib_user_logout(cls):
        """
        Remove login info for shibboleth user.

        :return:

        """
        user_logged_out.send(current_app._get_current_object(),
                             user=current_user)


def get_user_info_by_role_name(role_name):
    """Get user info by role name."""
    role = Role.query.filter_by(name=role_name).first()
    return User.query.filter(User.roles.contains(role)).all()
