# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2013 Nicira, Inc.
# All Rights Reserved
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless equired by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# @author: Salvatore Orlando, VMware

import netaddr
from oslo.config import cfg

from quantum.api.v2 import attributes
from quantum.common import constants
from quantum.common import exceptions as q_exc
from quantum.db import l3_db
from quantum.openstack.common import log as logging
from quantum.openstack.common.notifier import api as notifier_api
from quantum.plugins.nicira.nicira_nvp_plugin.common import (exceptions
                                                             as nvp_exc)
from quantum.plugins.nicira.nicira_nvp_plugin import NvpApiClient


LOG = logging.getLogger(__name__)

METADATA_DEFAULT_PREFIX = 30
METADATA_SUBNET_CIDR = '169.254.169.252/%d' % METADATA_DEFAULT_PREFIX
METADATA_GATEWAY_IP = '169.254.169.253'


class NvpMetadataAccess(object):

    def _find_metadata_port(self, context, ports):
        for port in ports:
            for fixed_ip in port['fixed_ips']:
                cidr = netaddr.IPNetwork(
                    self.get_subnet(context, fixed_ip['subnet_id'])['cidr'])
                if cidr in netaddr.IPNetwork(METADATA_SUBNET_CIDR):
                    return port

    def _create_metadata_access_network(self, context, router_id):
        # This will still ensure atomicity on Quantum DB
        # context.elevated() creates a deep-copy context
        ctx_elevated = context.elevated()
        with ctx_elevated.session.begin(subtransactions=True):
            # Add network
            # Network name is likely to be truncated on NVP

            net_data = {'name': ('meta-%s' % router_id)[:40],
                        'tenant_id': '',  # intentionally not set
                        'admin_state_up': True,
                        'port_security_enabled': False,
                        'shared': False,
                        'status': constants.NET_STATUS_ACTIVE}
            meta_net = self.create_network(ctx_elevated,
                                           {'network': net_data})
            # Add subnet
            subnet_data = {'network_id': meta_net['id'],
                           'tenant_id': '',  # intentionally not set
                           'name': 'meta-%s' % router_id,
                           'ip_version': 4,
                           'shared': False,
                           'cidr': METADATA_SUBNET_CIDR,
                           'enable_dhcp': True,
                           # Ensure default allocation pool is generated
                           'allocation_pools': attributes.ATTR_NOT_SPECIFIED,
                           'gateway_ip': METADATA_GATEWAY_IP,
                           'dns_nameservers': [],
                           'host_routes': []}
            meta_sub = self.create_subnet(ctx_elevated,
                                          {'subnet': subnet_data})
            self.add_router_interface(ctx_elevated, router_id,
                                      {'subnet_id': meta_sub['id']})
            # We need to send a notification to the dhcp agent in order
            # to start the metadata agent proxy
            # Note: the publisher id is the same used in the api module
            notifier_api.notify(context,
                                notifier_api.publisher_id('network'),
                                'network.create.end',
                                notifier_api.CONF.default_notification_level,
                                {'network': meta_net})

    def _destroy_metadata_access_network(self, context, router_id, ports):

        # context.elevated() creates a deep-copy context
        ctx_elevated = context.elevated()
        # This will still ensure atomicity on Quantum DB
        with ctx_elevated.session.begin(subtransactions=True):
            if ports:
                meta_port = self._find_metadata_port(ctx_elevated, ports)
                if not meta_port:
                    return
                meta_net_id = meta_port['network_id']
                self.remove_router_interface(
                    ctx_elevated, router_id, {'port_id': meta_port['id']})
                # Remove network (this will remove the subnet too)
                self.delete_network(ctx_elevated, meta_net_id)
                # We need to send a notification to the dhcp agent in order
                # to stop the metadata agent proxy
                # Note: the publisher id is the same used in the api module
                notifier_api.notify(
                    context,
                    notifier_api.publisher_id('network'),
                    'network.delete.end',
                    notifier_api.CONF.default_notification_level,
                    {'network_id': meta_net_id})

    def _handle_metadata_access_network(self, context, router_id):
        if not cfg.CONF.NVP.enable_metadata_access_network:
            LOG.debug(_("Metadata access network is disabled"))
            return
        # As we'll use a different device_owner for metadata interface
        # this query will return only 'real' router interfaces
        ctx_elevated = context.elevated()
        device_filter = {'device_id': [router_id],
                         'device_owner': [l3_db.DEVICE_OWNER_ROUTER_INTF]}
        with ctx_elevated.session.begin(subtransactions=True):
            ports = self.get_ports(ctx_elevated, filters=device_filter)
            try:
                if ports:
                    if not self._find_metadata_port(ctx_elevated, ports):
                        self._create_metadata_access_network(context,
                                                             router_id)
                    elif len(ports) == 1:
                        # The only port left if the metadata port
                        self._destroy_metadata_access_network(context,
                                                              router_id,
                                                              ports)
                else:
                    LOG.debug(_("No router interface found for router '%s'. "
                                "No metadata access network should be "
                                "created or destroyed"), router_id)
            # TODO(salvatore-orlando): A better exception handling in the
            # NVP plugin would allow us to improve error handling here
            except (q_exc.QuantumException, nvp_exc.NvpPluginException,
                    NvpApiClient.NvpApiException):
                # Any exception here should be regarded as non-fatal
                LOG.exception(_("An error occurred while operating on the "
                                "metadata access network for router:'%s'"),
                              router_id)
