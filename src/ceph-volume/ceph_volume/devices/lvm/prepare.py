from __future__ import print_function
import json
import os
from textwrap import dedent
from ceph_volume.util import prepare as prepare_utils
from ceph_volume.util import system
from ceph_volume import conf, decorators
from . import api
from .common import prepare_parser


def prepare_filestore(device, journal, secrets, id_=None, fsid=None):
    """
    :param device: The name of the volume group or lvm to work with
    :param journal: similar to device but can also be a regular/plain disk
    :param secrets: A dict with the secrets needed to create the osd (e.g. cephx)
    :param id_: The OSD id
    :param fsid: The OSD fsid, also known as the OSD UUID
    """
    cephx_secret = secrets.get('cephx_secret', prepare_utils.create_key())
    json_secrets = json.dumps(secrets)

    # allow re-using an existing fsid, in case prepare failed
    fsid = fsid or system.generate_uuid()
    # allow re-using an id, in case a prepare failed
    osd_id = id_ or prepare_utils.create_id(fsid, json_secrets)
    # create the directory
    prepare_utils.create_path(osd_id)
    # format the device
    prepare_utils.format_device(device)
    # mount the data device
    prepare_utils.mount_osd(device, osd_id)
    # symlink the journal
    prepare_utils.link_journal(journal, osd_id)
    # get the latest monmap
    prepare_utils.get_monmap(osd_id)
    # prepare the osd filesystem
    prepare_utils.osd_mkfs(osd_id, fsid)
    # write the OSD keyring if it doesn't exist already
    prepare_utils.write_keyring(osd_id, cephx_secret)


def prepare_bluestore():
    raise NotImplemented()


class Prepare(object):

    help = 'Format an LVM device and associate it with an OSD'

    def __init__(self, argv):
        self.argv = argv

    def get_journal_pv(self, argument):
        # it is safe to get the pv by its name
        device = api.get_pv(pv_name=argument)
        if device:
            # means this has an existing uuid, so we can use it without
            # recreating it
            if device.pv_uuid:
                return device
            # otherwise, we need to create it as a 'pv', ask back again for it
            # as a pv, and return that
            api.create_pv(argument)
            return api.get_pv(pv_name=argument)
        # if we get to this point, this should be a red flag, `prepare` probably is
        # out of options to use anything so raise an error
        raise RuntimeError(
            '--journal specified an invalid or non-existent device: %s' % argument
        )

    def get_journal_lv(self, argument):
        """
        Perform some parsing of the value of ``--journal`` so that the process
        can determine correctly if it got a device path or an lv
        :param argument: The value of ``--journal``, that will need to be split
        to retrieve the actual lv
        """
        try:
            vg_name, lv_name = argument.split('/')
        except (ValueError, AttributeError):
            return None
        return api.get_lv(lv_name=lv_name, vg_name=vg_name)

    @decorators.needs_root
    def prepare(self, args):
        # FIXME we don't allow re-using a keyring, we always generate one for the
        # OSD, this needs to be fixed. This could either be a file (!) or a string
        # (!!) or some flags that we would need to compound into a dict so that we
        # can convert to JSON (!!!)
        secrets = {'cephx_secret': prepare_utils.create_key()}

        cluster_fsid = conf.ceph.get('global', 'fsid')
        fsid = args.osd_fsid or system.generate_uuid()
        # allow re-using an id, in case a prepare failed
        osd_id = args.osd_id or prepare_utils.create_id(fsid, json.dumps(secrets))
        vg_name, lv_name = args.data.split('/')
        if args.filestore:
            data_lv = api.get_lv(lv_name=lv_name, vg_name=vg_name)

            # we must have either an existing data_lv or a newly created, so lets make
            # sure that the tags are correct
            if not data_lv:
                raise RuntimeError('no data logical volume found with: %s' % args.data)

            if not args.journal:
                raise RuntimeError('--journal is required when using --filestore')

            journal_lv = self.get_journal_lv(args.journal)
            if journal_lv:
                # set this so it doesn't matter if it is an lv or a disk, we
                # can apply the tags regardless
                journal_lvm = journal_lv
                journal_device = journal_lv.lv_path
                journal_uuid = journal_lv.lv_uuid
            # otherwise assume this is a regular disk, that will need to be
            # created as a 'pv'
            else:
                journal_pv = self.get_journal_pv(args.journal)
                # set this so it doesn't matter if it is an lv or a disk, we
                # can apply the tags regardless
                journal_lvm = journal_pv
                journal_device = journal_pv.pv_name
                journal_uuid = journal_pv.pv_uuid

            journal_lvm.set_tags({
                'ceph.type': 'journal',
                'ceph.osd_fsid': fsid,
                'ceph.osd_id': osd_id,
                'ceph.cluster_fsid': cluster_fsid,
                'ceph.journal_device': journal_device,
                'ceph.journal_uuid': journal_uuid,
                'ceph.data_device': data_lv.lv_path,
                'ceph.data_uuid': data_lv.lv_uuid,
            })

            data_lv.set_tags({
                'ceph.type': 'data',
                'ceph.osd_fsid': fsid,
                'ceph.osd_id': osd_id,
                'ceph.cluster_fsid': cluster_fsid,
                'ceph.journal_device': journal_device,
                'ceph.journal_uuid': journal_uuid,
                'ceph.data_device': data_lv.lv_path,
                'ceph.data_uuid': data_lv.lv_uuid,
            })

            prepare_filestore(
                data_lv.lv_path,
                journal_device,
                secrets,
                id_=osd_id,
                fsid=fsid,
            )
        elif args.bluestore:
            prepare_bluestore(args)

    def main(self):
        sub_command_help = dedent("""
        Prepare an OSD by assigning an ID and FSID, registering them with the
        cluster with an ID and FSID, formatting and mounting the volume, and
        finally by adding all the metadata to the logical volumes using LVM
        tags, so that it can later be discovered.

        Once the OSD is ready, an ad-hoc systemd unit will be enabled so that
        it can later get activated and the OSD daemon can get started.

        Example calls for supported scenarios:

        Filestore
        ---------

          Existing logical volume (lv) or device:

              ceph-volume lvm prepare --filestore --data {vg name/lv name} --journal /path/to/device

          Or:

              ceph-volume lvm prepare --filestore --data {vg name/lv name} --journal {vg name/lv name}

        """)
        parser = prepare_parser(
            prog='ceph-volume lvm prepare',
            description=sub_command_help,
        )
        if len(self.argv) == 0:
            print(sub_command_help)
            return
        args = parser.parse_args(self.argv)
        self.prepare(args)
