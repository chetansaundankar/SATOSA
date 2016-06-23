"""
A saml2 backend module for the satosa proxy
"""
import copy
import json
import logging
from base64 import urlsafe_b64encode, urlsafe_b64decode
from urllib.parse import urlparse

from saml2 import BINDING_HTTP_POST
from saml2 import BINDING_HTTP_REDIRECT
from saml2.client_base import Base
from saml2.config import SPConfig
from saml2.extension.ui import NAMESPACE as UI_NAMESPACE
from saml2.samlp import NameIDPolicy

from .base import BackendModule
from ..exception import SATOSAAuthenticationError
from ..internal_data import (UserIdHashType, InternalRequest, InternalResponse,
                             AuthenticationInformation)
from ..logging_util import satosa_logging
from ..metadata_creation.description import (MetadataDescription, OrganizationDesc,
                                             ContactPersonDesc, UIInfoDesc)
from ..response import SeeOther, Response, MetadataResponse
from ..util import rndstr, get_saml_name_id_format

logger = logging.getLogger(__name__)


class SamlBackend(BackendModule):
    """
    A saml2 backend module
    """

    def __init__(self, outgoing, internal_attributes, config, base_url, name):
        """
        :type outgoing:
        (satosa.context.Context, satosa.internal_data.InternalResponse) -> satosa.response.Response
        :type internal_attributes: dict[str, dict[str, list[str] | str]]
        :type config: dict[str, Any]
        :type base_url: str
        :type name: str

        :param outgoing: Callback should be called by the module after
                                   the authorization in the backend is done.
        :param internal_attributes: Internal attribute map
        :param config: The module config
        :param base_url: base url of the service
        :param name: name of the plugin
        """
        super().__init__(outgoing, internal_attributes, base_url, name)
        sp_config = SPConfig().load(copy.deepcopy(config["config"]), False)

        self.sp = Base(sp_config)
        self.config = config
        self.attribute_profile = config.get("attribute_profile", "saml")
        self.bindings = [BINDING_HTTP_REDIRECT, BINDING_HTTP_POST]
        self.discosrv = config.get("disco_srv")

    def _create_name_id_policy(self, usr_id_hash_type):
        """
        Creates a name id policy

        :type usr_id_hash_type: satosa.internal_data.UserIdHashType
        :rtype: saml2.samlp.NameIDPolicy

        :param usr_id_hash_type: The internal id hash type
        :return: A name id policy
        """
        nameid_format = get_saml_name_id_format(usr_id_hash_type)
        name_id_policy = NameIDPolicy(format=nameid_format)
        return name_id_policy

    def start_auth(self, context, internal_req):
        """
        See super class method satosa.backends.base.BackendModule#start_auth
        :type context: satosa.context.Context
        :type internal_req: satosa.internal_data.InternalRequest
        :rtype: satosa.response.Response
        """

        # if there is only one IdP in the metadata, bypass the discovery service
        idps = self.sp.metadata.identity_providers()
        if len(idps) == 1:
            return self.authn_request(context, idps[0], internal_req)

        try:
            entity_id = context.internal_data["mirror.target_entity_id"]
            entity_id = urlsafe_b64decode(entity_id).decode("utf-8")
            return self.authn_request(context, entity_id, internal_req)
        except KeyError:
            return self.disco_query(context, internal_req)

    def disco_query(self, context, internal_req):
        """
        Makes a request to the discovery server

        :type context: satosa.context.Context
        :type internal_req: satosa.internal_data.InternalRequest
        :rtype: satosa.response.SeeOther

        :param context: The current context
        :param internal_req: The request
        :return: Response
        """
        eid = self.sp.config.entityid
        # returns list of 2-tuples
        disco_resp = self.sp.config.getattr("endpoints", "sp")["discovery_response"]
        # The first value of the first tuple is the one I want
        ret = disco_resp[0][0]
        loc = self.sp.create_discovery_service_request(self.discosrv, eid, **{"return": ret})
        return SeeOther(loc)

    def authn_request(self, context, entity_id, internal_req):
        """
        Do an authorization request on idp with given entity id.
        This is the start of the authorization.

        :type context: satosa.context.Context
        :type entity_id: str
        :type internal_req: satosa.internal_data.InternalRequest
        :rtype: satosa.response.Response

        :param context: The curretn context
        :param entity_id: Target IDP entity id
        :param internal_req: The request
        :return: Response
        """
        hash_type = self.config.get("hash_type", UserIdHashType.persistent.name)
        req_args = {"name_id_policy": self._create_name_id_policy(hash_type)}
        state = context.state

        try:
            # Picks a binding to use for sending the Request to the IDP
            _binding, destination = self.sp.pick_binding(
                "single_sign_on_service", self.bindings, "idpsso",
                entity_id=entity_id)
            satosa_logging(logger, logging.DEBUG,
                           "binding: %s, destination: %s" % (_binding, destination), state)
            # Binding here is the response binding that is which binding the
            # IDP should use to return the response.
            acs = self.sp.config.getattr("endpoints", "sp")["assertion_consumer_service"]
            # just pick one
            endp, return_binding = acs[0]
            req_id, req = self.sp.create_authn_request(destination,
                                                    binding=return_binding,
                                                    **req_args)
            relay_state = rndstr()
            ht_args = self.sp.apply_binding(_binding, "%s" % req, destination, relay_state=relay_state)
            satosa_logging(logger, logging.DEBUG, "ht_args: %s" % ht_args, state)
        except Exception as exc:
            satosa_logging(logger, logging.DEBUG,
                           "Failed to construct the AuthnRequest for state", state, exc_info=True)
            raise SATOSAAuthenticationError(state, "Failed to construct the AuthnRequest") from exc

        state.add(self.name, relay_state)

        if _binding == BINDING_HTTP_REDIRECT:
            for param, value in ht_args["headers"]:
                if param == "Location":
                    resp = SeeOther(str(value))
                    break
            else:
                satosa_logging(logger, logging.DEBUG, "Parameter error for state", state)
                raise SATOSAAuthenticationError(state, "Parameter error")
        else:
            resp = Response(ht_args["data"], headers=ht_args["headers"])

        return resp

    def authn_response(self, context, binding):
        """
        Endpoint for the idp response
        :type context: satosa.context,Context
        :type binding: str
        :rtype: satosa.response.Response

        :param context: The current context
        :param binding: The saml binding type
        :return: response
        """
        _authn_response = context.request
        state = context.state

        if not _authn_response["SAMLResponse"]:
            satosa_logging(logger, logging.DEBUG, "Missing Response for state", state)
            raise SATOSAAuthenticationError(state, "Missing Response")

        try:
            _response = self.sp.parse_authn_request_response(
                _authn_response["SAMLResponse"], binding)
        except Exception as err:
            satosa_logging(logger, logging.DEBUG,
                           "Failed to parse authn request for state", state,
                           exc_info=True)
            raise SATOSAAuthenticationError(state, "Failed to parse authn request") from err

        # check if the relay_state matches the cookie state
        if state.get(self.name) != _authn_response['RelayState']:
            satosa_logging(logger, logging.DEBUG,
                           "State did not match relay state for state", state)
            raise SATOSAAuthenticationError(state, "State did not match relay state")

        context.state.remove(self.name)
        return self.auth_callback_func(context, self._translate_response(_response, context.state))

    def disco_response(self, context):
        """
        Endpoint for the discovery server response

        :type context: satosa.context.Context
        :rtype: satosa.response.Response

        :param context: The current context
        :return: response
        """
        info = context.request
        state = context.state

        try:
            entity_id = info["entityID"]
        except KeyError as err:
            satosa_logging(logger, logging.DEBUG, "No IDP chosen for state", state, exc_info=True)
            raise SATOSAAuthenticationError(state, "No IDP chosen") from err
        else:
            request_info = InternalRequest(None, None)
            return self.authn_request(context, entity_id, request_info)

    def _translate_response(self, response, state):
        """
        Translates a saml authorization response to an internal response

        :type response: saml2.response.AuthnResponse
        :rtype: satosa.internal_data.InternalResponse
        :param response: The saml authorization response
        :return: A translated internal response
        """
        _authn_info = response.authn_info()[0]
        timestamp = response.assertion.authn_statement[0].authn_instant
        issuer = response.response.issuer.text
        auth_class_ref = _authn_info[0]

        auth_info = AuthenticationInformation(auth_class_ref, timestamp, issuer)
        internal_resp = InternalResponse(auth_info=auth_info)

        internal_resp.set_user_id(response.get_subject().text)
        internal_resp.add_attributes(self.converter.to_internal(self.attribute_profile, response.ava))

        satosa_logging(logger, logging.DEBUG,
                       "received attributes:\n%s" % json.dumps(response.ava, indent=4), state)

        return internal_resp

    def _metadata(self, context):
        """
        Endpoint for retrieving the backend metadata
        :type context: satosa.context.Context
        :rtype: satosa.backends.saml2.MetadataResponse

        :param context: The current context
        :return: response with metadata
        """
        satosa_logging(logger, logging.DEBUG, "Sending metadata response", context.state)
        return MetadataResponse(self.sp.config)

    def register_endpoints(self):
        """
        See super class method satosa.backends.base.BackendModule#register_endpoints
        :rtype list[(str, ((satosa.context.Context, Any) -> Any, Any))]
        """
        url_map = []
        sp_endpoints = self.sp.config.getattr("endpoints", "sp")
        for endp, binding in sp_endpoints["assertion_consumer_service"]:
            parsed_endp = urlparse(endp)
            url_map.append(("^%s$" % parsed_endp.path[1:], (self.authn_response, binding)))

        if "publish_metadata" in self.config:
            metadata_path = urlparse(self.config["publish_metadata"])
            url_map.append(("^%s$" % metadata_path.path[1:], self._metadata))

        if self.discosrv:
            for endp, binding in sp_endpoints["discovery_response"]:
                parsed_endp = urlparse(endp)
                url_map.append(
                    ("^%s$" % parsed_endp.path[1:], self.disco_response))

        return url_map

    def get_metadata_desc(self):
        """
        See super class satosa.backends.backend_base.BackendModule#get_metadata_desc
        :rtype: satosa.metadata_creation.description.MetadataDescription
        """
        # TODO Only get IDPs
        metadata_desc = []
        for metadata_file in self.sp.metadata.metadata:
            metadata_file = self.sp.metadata.metadata[metadata_file]
            entity_ids = []

            if metadata_file.entity_descr is None:
                for entity_descr in metadata_file.entities_descr.entity_descriptor:
                    entity_ids.append(entity_descr.entity_id)
            else:
                entity_ids.append(metadata_file.entity_descr.entity_id)

            entity = metadata_file.entity
            for entity_id in entity_ids:

                description = MetadataDescription(
                    urlsafe_b64encode(entity_id.encode("utf-8")).decode("utf-8"))

                # Add organization info
                try:
                    organization = OrganizationDesc()
                    organization_info = entity[entity_id]['organization']

                    for name_info in organization_info.get("organization_name", []):
                        organization.add_name(name_info["text"], name_info["lang"])
                    for display_name_info in organization_info.get("organization_display_name", []):
                        organization.add_display_name(display_name_info["text"],
                                                      display_name_info["lang"])
                    for url_info in organization_info.get("organization_url", []):
                        organization.add_url(url_info["text"], url_info["lang"])

                    description.set_organization(organization)
                except:
                    pass

                # Add contact person info
                try:
                    contact_persons = entity[entity_id]['contact_person']
                    for cont_pers in contact_persons:
                        person = ContactPersonDesc()

                        if 'contact_type' in cont_pers:
                            person.contact_type = cont_pers['contact_type']
                        for address in cont_pers.get('email_address', []):
                            person.add_email_address(address["text"])
                        if 'given_name' in cont_pers:
                            person.given_name = cont_pers['given_name']['text']
                        if 'sur_name' in cont_pers:
                            person.sur_name = cont_pers['sur_name']['text']

                        description.add_contact_person(person)
                except KeyError:
                    pass

                # Add ui info
                try:
                    for idpsso_desc in entity[entity_id]["idpsso_descriptor"]:
                        # TODO Can have more than one ui info?
                        ui_elements = idpsso_desc["extensions"]["extension_elements"]
                        ui_info = UIInfoDesc()

                        for element in ui_elements:
                            if not element["__class__"] == "%s&UIInfo" % UI_NAMESPACE:
                                continue
                            for desc in element.get("description", []):
                                ui_info.add_description(desc["text"], desc["lang"])
                            for name in element.get("display_name", []):
                                ui_info.add_display_name(name["text"], name["lang"])
                            for logo in element.get("logo", []):
                                ui_info.add_logo(logo["text"], logo["width"], logo["height"],
                                                 logo["lang"])

                        description.set_ui_info(ui_info)
                except KeyError:
                    pass

                metadata_desc.append(description)
        return metadata_desc
