from django.views.generic import View

from pulp.server.auth import authorization
from pulp.server.exceptions import MissingResource
from pulp.server.managers import factory
from pulp.server.webservices.controllers.decorators import auth_required
from pulp.server.webservices.views.util import (generate_json_response,
                                                generate_json_response_with_pulp_encoder)


class DistributorResourceView(View):
    pass


class DistributorsView(View):
    pass


class ImporterResourceView(View):
    """
    Views for an individual importer.
    """

    @auth_required(authorization.READ)
    def get(self, request, importer_id):
        """
        Return a response containing serialized data for the specified importer.

        :param request : WSGI request object
        :type  request : django.core.handlers.wsgi.WSGIRequest
        :param importer_id : name of importer to return information for
        :type  importer_id : string

        :return : Response containing serialized data for specified importer
        :rtype  : django.http.HttpResponse

        :raises : MissingResource if importer_id cannot be found
        """
        manager = factory.plugin_manager()
        all_importers = manager.importers()

        for importer in all_importers:
            if importer['id'] == importer_id:
                importer['_href'] = request.get_full_path()
                return generate_json_response(importer)

        raise MissingResource(importer_type_id=importer_id)


class ImportersView(View):
    pass


class TypeResourceView(View):
    """
    View for dealing with a specific plugin type.
    """

    @auth_required(authorization.READ)
    def get(self, request, type_id):
        """
        Return a single type definition.

        :param request: WSGI request object
        :type  request: django.core.handlers.wsgi.WSGIRequst
        :return       : Serialized response containing a type definition
        :rtype        : HttpResponse

        :raises       : MissingResource if type_id is not found
        """
        manager = factory.plugin_manager()
        all_types = manager.types()

        for plugin_type in all_types:
            if plugin_type['id'] == type_id:
                plugin_type['_href'] = request.get_full_path()
                return generate_json_response_with_pulp_encoder(plugin_type)

        raise MissingResource(type=type_id)


class TypesView(View):
    """
    View for dealing with all plugin types.
    """

    @auth_required(authorization.READ)
    def get(self, request):
        """
        Get all type definitions

        :param request: WSGI Request obect
        :type  request: django.core.handlers.wsgi.WSGIRequest
        :return       : Response containing serialized list data for all available content types
        :rtype        : django.http.HttpResponse
        """
        manager = factory.plugin_manager()
        type_defs = manager.types()

        for type_definition in type_defs:
            href = {'_href': '/'.join([request.get_full_path().rstrip('/'),
                                       type_definition['id'], ''])}
            type_definition.update(href)

        return generate_json_response_with_pulp_encoder(type_defs)
