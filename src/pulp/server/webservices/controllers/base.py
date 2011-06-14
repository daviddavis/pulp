#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright © 2010 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public
# License as published by the Free Software Foundation; either version
# 2 of the License (GPLv2) or (at your option) any later version.
# There is NO WARRANTY for this software, express or implied,
# including the implied warranties of MERCHANTABILITY,
# NON-INFRINGEMENT, or FITNESS FOR A PARTICULAR PURPOSE. You should
# have received a copy of GPLv2 along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.

import logging
import sys
import traceback
from datetime import timedelta
from gettext import gettext as _

import web
from pymongo import json_util

from pulp.common import dateutils
from pulp.server import async
from pulp.server.auth.authentication import (
    check_username_password, check_user_cert, check_consumer_cert, check_oauth)
from pulp.server.auth.authorization import is_authorized, is_superuser
from pulp.server.auth.principal import clear_principal, set_principal
from pulp.server.compat import wraps, json
from pulp.server.tasking.scheduler import (
    ImmediateScheduler, AtScheduler, IntervalScheduler)
from pulp.server.tasking.task import task_complete_states
from pulp.server.webservices import http


_log = logging.getLogger(__name__)


class JSONController(object):
    """
    Base controller class with convenience methods for JSON serialization
    """
    @staticmethod
    def error_handler(method):
        """
        Static controller method wrapper that catches internal errors and
        reports them as JSON serialized trace back strings
        """
        @wraps(method)
        def report_error(self, *args, **kwargs):
            try:
                return method(self, *args, **kwargs)
            except Exception:
                exc_info = sys.exc_info()
                tb_msg = ''.join(traceback.format_exception(*exc_info))
                _log.error("%s" % (traceback.format_exc()))
                return self.internal_server_error(tb_msg)
        return report_error

    @staticmethod
    def auth_required(operation=None, super_user_only=False):
        """
        Static controller method wrapper that authenticates users based on
        various credentials and then checks their authorization before allowing
        the controller to be accessed.
        A None for the operation means not to check authorization, only check
        authentication.
        The super_user_only flag set to True means that only members of the
        built in SuperUsers role are authorized.
        @type operation: int or None
        @param operation: the operation a user needs permission for
        @type super_user_only: bool
        @param super_user_only: only authorize a user if they are a super user
        """
        def _auth_required(method):
            """
            Closure method for decorator.
            """
            user_pass_fail_msg = _('Invalid username or password')
            cert_fail_msg = _('Invalid SSL Certificate')
            oauth_fail_msg = _('Invalid OAuth Credentials')
            authen_fail_msg = _('Authentication Required')
            author_fail_msg = _('Permission Denied')

            @wraps(method)
            def _auth_decorator(self, *args, **kwargs):
                # XXX jesus h christ: is this some god awful shit
                # please, please refactor this into ... something ... anything!
                user = None
                # first, try username:password authentication
                username, password = http.username_password()
                if username is not None:
                    user = check_username_password(username, password)
                    if user is None:
                        return self.unauthorized(user_pass_fail_msg)
                # second, try certificate authentication
                if user is None:
                    cert_pem = http.ssl_client_cert()
                    if cert_pem is not None:
                        # first, check user certificate
                        user = check_user_cert(cert_pem)
                        if user is None:
                            # second, check consumer certificate
                            user = check_consumer_cert(cert_pem)
                    # third, check oauth credentials
                    if user is None:
                        auth = http.http_authorization()
                        username = http.request_info('HTTP_PULP_USER')
                        if None in (auth, username):
                            if cert_pem is not None:
                                return self.unauthorized(cert_fail_msg)
                        else:
                            meth = http.request_info('REQUEST_METHOD')
                            url = http.request_url()
                            query = http.request_info('QUERY_STRING')
                            user = check_oauth(username, meth, url, auth, query)
                            if user is None:
                                return self.unauthorized(oauth_fail_msg)
                # authentication has failed
                if user is None:
                    return self.unauthorized(authen_fail_msg)
                # forth, check authorization
                if super_user_only and not is_superuser(user):
                    return self.unauthorized(author_fail_msg)
                # if the operation is None, don't check authorization
                elif operation is not None and \
                     not is_authorized(http.resource_path(), user, operation):
                    return self.unauthorized(author_fail_msg)
                # everything ok, manage the principal and call the method
                set_principal(user)
                value = method(self, *args, **kwargs)
                clear_principal()
                return value

            return _auth_decorator
        return _auth_required

    # input methods -----------------------------------------------------------

    def params(self):
        """
        JSON decode the objects in the requests body and return them
        @return: dict of parameters passed in through the body
        """
        data = web.data()
        if not data:
            return {}
        return json.loads(data)

    def data(self):
        """
        Get binary POST/PUT payload.
        @return: raw data.
        """
        return web.data()

    def filters(self, valid):
        """
        Fetch any parameters passed on the url
        @type valid: list of str's
        @param valid: list of expected query parameters
        @return: dict of param: [value(s)] of uri query parameters
        """
        return http.query_parameters(valid)

    # result methods ----------------------------------------------------------

    def _task_to_dict(self, task):
        """
        Convert a task to a dictionary (non-destructive) while retaining the
        pertinent information for a status check but in a more convenient form
        for JSON serialization.
        @type task: Task instance
        @param task: task to convert
        @return dict representing task
        """
        fields = ('id', 'class_name', 'method_name', 'state', 'result',
                  'exception', 'traceback', 'progress')
        d = dict((f, getattr(task, f)) for f in fields)
        # convert the exception into a string as it cannot be json encoded
        if isinstance(task.exception, Exception):
            d['exception'] = str(task.exception)
        # time fields must be in iso8601 format
        fields = ('start_time', 'finish_time', 'scheduled_time')
        for f in fields:
            t = getattr(task, f, None)
            d[f] = t and dateutils.format_iso8601_datetime(t)
        # add scheduler information so we can differentiate between recurring
        # and non-recurring tasks
        d['scheduler'] = None
        if isinstance(task.scheduler, ImmediateScheduler):
            d['scheduler'] = 'immediate'
        elif isinstance(task.scheduler, AtScheduler):
            d['scheduler'] = 'at'
        elif isinstance(task.scheduler, IntervalScheduler):
            d['scheduler'] = 'interval'
        return d

    def _status_path(self, id):
        """
        Construct a URL path that can be used to poll a task's status
        A status path is constructed as follows:
        /<collection>/<object id>/<action>/<action id>/
        A GET request sent to this path will get a JSON encoded status object
        """
        parts = web.ctx.path.split('/')
        if parts[-2] == id:
            return http.uri_path()
        return http.extend_uri_path(id)

    def task_status(self, id):
        """
        Get the current status of an asynchronous task.
        @param id: task id
        @return: TaskModel instance
        """
        task = self.find_task(id)
        if task is None:
            return None
        status = self._task_to_dict(task)
        status.update({'status_path': self._status_path(id)})
        return status

    def filter_results(self, results, filters):
        """
        @deprecated: use mongo.filters_to_re_spec and pass the result into
                     pulp's api instead
        @type results: iterable of pulp model instances
        @param results: results from a db query
        @type filters: dict of str: list
        @param filters: result filters passed in, in the uri
        @return: list of model instances that meet the criteria in the filters
        """
        if not filters:
            return results
        new_results = []
        for result in results:
            is_good = True
            for filter, criteria in filters.items():
                if result[filter] not in criteria:
                    is_good = False
                    break
            if is_good:
                new_results.append(result)
        return new_results

    # http response methods ---------------------------------------------------

    def _output(self, data):
        """
        JSON encode the response and set the appropriate headers
        """
        http.header('Content-Type', 'application/json')
        return json.dumps(data, default=json_util.default)

    def _error_dict(self, msg, code=None):
        """
        Standardized error returns
        """
        if code is None:
            code = _('not set')
        d = {'error_message': msg,
             'error_code': code, # reserved for future use
             'http_status': web.ctx.status, }
        return d

    def ok(self, data):
        """
        Return an ok response.
        @type data: mapping type
        @param data: data to be returned in the body of the response
        @return: JSON encoded response
        """
        http.status_ok()

        return self._output(data)

    def created(self, location, data):
        """
        Return a created response.
        @type location: str
        @param location: URL of the created resource
        @type data: mapping type
        @param data: data to be returned in the body of the response
        @return: JSON encoded response
        """
        http.status_created()
        http.header('Location', location)
        return self._output(data)

    def accepted(self, status):
        """
        Return an accepted response with status information in the body.
        @return: JSON encoded response
        """
        http.status_accepted()
        status.update({'status_path': self._status_path(status['id'])})
        return self._output(status)

    def no_content(self):
        """
        Return a no content response
        @return: JSON encoded response
        """
        http.status_no_content()
        return self._output(None)

    def partial_content(self, data):
        '''
        Returns a partial content response. Typically, the returned data should
        include:
        - a list of successful content updates
        - a list of failed content updates
        - an overarching error message if applicable
        '''
        http.status_partial()
        return self._output(data)

    def bad_request(self, msg=None):
        """
        Return a not found error.
        @type msg: str
        @param msg: optional error message
        @return: JSON encoded response
        """
        http.status_bad_request()
        return self._output(msg)

    def unauthorized(self, msg=None):
        """
        Return an unauthorized error.
        @type msg: str
        @param msg: optional error message
        @return: JSON encoded response
        """
        http.status_unauthorized()
        return self._output(msg)

    def not_found(self, msg=None):
        """
        Return a not found error.
        @type msg: str
        @param msg: optional error message
        @return: JSON encoded response
        """
        http.status_not_found()
        return self._output(msg)

    def method_not_allowed(self, msg=None):
        """
        Return a method not allowed error.
        @type msg: str
        @param msg: optional error message
        @return: JSON encoded response
        """
        http.status_method_not_allowed()
        return None

    def not_acceptable(self, msg=None):
        """
        Return a not acceptable error.
        @type msg: str
        @param msg: optional error message
        @return: JSON encoded response
        """
        http.status_not_acceptable()
        return self._output(msg)

    def conflict(self, msg=None):
        """
        Return a conflict error.
        @type msg: str
        @param msg: optional error message
        @return: JSON encoded response
        """
        http.status_conflict()
        return self._output(msg)

    def internal_server_error(self, msg=None):
        """
        Return an internal server error.
        @type msg: str
        @param msg: optional error message
        @return: JSON encoded response
        """
        http.status_internal_server_error()
        return self._output(msg)
