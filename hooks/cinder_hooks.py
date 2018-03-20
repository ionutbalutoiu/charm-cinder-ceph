#!/usr/bin/python
#
# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import json
import uuid

from cinder_utils import (
    register_configs,
    restart_map,
    set_ceph_env_variables,
    PACKAGES,
    REQUIRED_INTERFACES,
    VERSION_PACKAGE,
)
from cinder_contexts import CephSubordinateContext
from charmhelpers.contrib.openstack.context import CephContext

from charmhelpers.core.hookenv import (
    Hooks,
    UnregisteredHookError,
    config,
    service_name,
    relation_set,
    relation_ids,
    status_set,
    log,
    leader_get,
    leader_set,
    is_leader,
)
from charmhelpers.fetch import apt_install, apt_update
from charmhelpers.core.host import (
    restart_on_change,
    service_restart,
)
from charmhelpers.contrib.storage.linux.ceph import (
    send_request_if_needed,
    is_request_complete,
    ensure_ceph_keyring,
    CephBrokerRq,
    delete_keyring,
)
from charmhelpers.payload.execd import execd_preinstall
from charmhelpers.contrib.openstack.utils import (
    set_os_workload_status,
    os_application_version_set,
)


hooks = Hooks()

CONFIGS = register_configs()


@hooks.hook('install.real')
def install():
    status_set('maintenance', 'Executing pre-install')
    execd_preinstall()
    status_set('maintenance', 'Installing apt packages')
    apt_update(fatal=True)
    apt_install(PACKAGES, fatal=True)


@hooks.hook('ceph-relation-joined')
def ceph_joined():
    if not os.path.isdir('/etc/ceph'):
        os.mkdir('/etc/ceph')


def get_ceph_request():
    service = service_name()
    rq = CephBrokerRq()
    replicas = config('ceph-osd-replication-count')
    weight = config('ceph-pool-weight')
    rq.add_op_create_pool(name=service, replica_count=replicas,
                          weight=weight,
                          group="volumes")
    if config('restrict-ceph-pools'):
        rq.add_op_request_access_to_group(
            name="volumes",
            object_prefix_permissions={'class-read': ['rbd_children']},
            permission='rwx')
        rq.add_op_request_access_to_group(
            name="images",
            object_prefix_permissions={'class-read': ['rbd_children']},
            permission='rwx')
        rq.add_op_request_access_to_group(
            name="vms",
            object_prefix_permissions={'class-read': ['rbd_children']},
            permission='rwx')
    return rq


@hooks.hook('ceph-relation-changed')
@restart_on_change(restart_map())
def ceph_changed():
    if 'ceph' not in CONFIGS.complete_contexts():
        log('ceph relation incomplete. Peer not ready?')
        return

    service = service_name()
    if not ensure_ceph_keyring(service=service,
                               user='cinder', group='cinder'):
        log('Could not create ceph keyring: peer not ready?')
        return

    if is_request_complete(get_ceph_request()):
        log('Request complete')
        CONFIGS.write_all()
        set_ceph_env_variables(service=service)
        for rid in relation_ids('storage-backend'):
            storage_backend(rid)
        for r_id in relation_ids('ceph-access'):
            ceph_access_joined(r_id)
        # Ensure that cinder-volume is restarted since only now can we
        # guarantee that ceph resources are ready.
        service_restart('cinder-volume')
    else:
        send_request_if_needed(get_ceph_request())


@hooks.hook('ceph-relation-broken')
def ceph_broken():
    service = service_name()
    delete_keyring(service=service)
    CONFIGS.write_all()


@hooks.hook('config-changed')
@restart_on_change(restart_map())
def write_and_restart():
    # NOTE(jamespage): seed uuid for use on compute nodes with libvirt
    if not leader_get('secret-uuid') and is_leader():
        leader_set({'secret-uuid': str(uuid.uuid4())})

    # NOTE(jamespage): trigger any configuration related changes
    #                  for cephx permissions restrictions
    ceph_changed()
    CONFIGS.write_all()


@hooks.hook('storage-backend-relation-joined')
def storage_backend(rel_id=None):
    if 'ceph' not in CONFIGS.complete_contexts():
        log('ceph relation incomplete. Peer not ready?')
    else:
        relation_set(
            relation_id=rel_id,
            backend_name=service_name(),
            subordinate_configuration=json.dumps(CephSubordinateContext()()),
            stateless=True,
        )


@hooks.hook('storage-backend-relation-changed')
def storage_backend_changed():
    # NOTE(jamespage) recall storage_backend as this only ever
    # changes post initial creation if the cinder charm is upgraded to a new
    # version of openstack.
    storage_backend()


@hooks.hook('upgrade-charm')
@restart_on_change(restart_map())
def upgrade_charm():
    if 'ceph' in CONFIGS.complete_contexts():
        CONFIGS.write_all()
        set_ceph_env_variables(service=service_name())
        for rid in relation_ids('storage-backend'):
            storage_backend(rid)


@hooks.hook('leader-settings-changed')
def leader_settings_changed():
    # NOTE(jamespage): lead unit will seed libvirt secret UUID
    #                  re-exec relations that use this data.
    for r_id in relation_ids('ceph-access'):
        ceph_access_joined(r_id)
    for r_id in relation_ids('storage-backend'):
        storage_backend(r_id)


@hooks.hook('ceph-access-relation-joined',
            'ceph-access-relation-changed')
def ceph_access_joined(relation_id=None):
    if 'ceph' not in CONFIGS.complete_contexts():
        log('Deferring key provision until ceph relation complete')
        return

    secret_uuid = leader_get('secret-uuid')
    if not secret_uuid:
        if is_leader():
            leader_set({'secret-uuid': str(uuid.uuid4())})
        else:
            log('Deferring key provision until leader seeds libvirt uuid')
            return

    # NOTE(jamespage): get key from ceph using a context
    ceph_keys = CephContext()()

    relation_set(
        relation_id=relation_id,
        relation_settings={'key': ceph_keys.get('key'),
                           'secret-uuid': leader_get('secret-uuid')}
    )


if __name__ == '__main__':
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e))
    set_os_workload_status(CONFIGS, REQUIRED_INTERFACES)
    os_application_version_set(VERSION_PACKAGE)
