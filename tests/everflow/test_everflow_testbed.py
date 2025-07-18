"""Test cases to support the Everflow Mirroring feature in SONiC."""
import logging
import os
import random
import time
import pytest
import threading
import ptf.testutils as testutils
from ptf.mask import Mask
import ptf.packet as packet
from . import everflow_test_utilities as everflow_utils
import ptf.packet as scapy
from tests.ptf_runner import ptf_runner
from .everflow_test_utilities import TARGET_SERVER_IP, BaseEverflowTest, DOWN_STREAM, UP_STREAM, DEFAULT_SERVER_IP
# Module-level fixtures
from tests.common.fixtures.ptfhost_utils import copy_ptftests_directory                                   # noqa: F401
from tests.common.fixtures.ptfhost_utils import copy_acstests_directory                                   # noqa: F401
from .everflow_test_utilities import setup_info, setup_arp_responder, erspan_ip_ver, EVERFLOW_DSCP_RULES  # noqa: F401
from .everflow_test_utilities import skip_ipv6_everflow_tests                                             # noqa: F401
from tests.common.fixtures.ptfhost_utils import copy_arp_responder_py                                     # noqa: F401
from tests.common.dualtor.mux_simulator_control import toggle_all_simulator_ports_to_rand_selected_tor    # noqa: F401

pytestmark = [
    pytest.mark.topology("t0", "t1", "t2", "lt2", "ft2", "m0", "m1")
]

logger = logging.getLogger(__name__)

MEGABYTE = 1024 * 1024
DEFAULT_PTF_SOCKET_RCV_SIZE = 1 * MEGABYTE
DEFAULT_PTF_QLEN = 15000


@pytest.fixture
def partial_ptf_runner(request, ptfhost, erspan_ip_ver):  # noqa F811
    """
    Fixture to run each Everflow PTF test case via ptf_runner.

    Takes all the necessary arguments to run the test case and returns a handle to the caller
    to execute the ptf_runner.
    """
    def _partial_ptf_runner(setup_info, direction, session_info, acl_stage, mirror_type,        # noqa F811
                            expect_receive=True, test_name=None, **kwargs):
        # Some of the arguments are fixed for each Everflow test case and defined here.
        # Arguments specific to each Everflow test case are passed in by each test via _partial_ptf_runner.
        # Arguments are passed in dictionary format via kwargs within each test case.
        src_ip = session_info["session_src_ip"] if erspan_ip_ver == 4 else session_info["session_src_ipv6"]
        dst_ip = session_info["session_dst_ip"] if erspan_ip_ver == 4 else session_info["session_dst_ipv6"]
        params = {
                  'hwsku':  setup_info[direction]['everflow_dut'].facts['hwsku'],
                  'asic_type':  setup_info[direction]['everflow_dut'].facts['asic_type'],
                  'router_mac': setup_info[direction]['ingress_router_mac'],
                  'session_src_ip': src_ip,
                  'session_dst_ip': dst_ip,
                  'session_ttl': session_info['session_ttl'],
                  'session_dscp': session_info['session_dscp'],
                  'acl_stage': acl_stage,
                  'mirror_stage': mirror_type,
                  'expect_received': expect_receive,
                  'check_ttl': ('False' if setup_info[direction]['everflow_dut'].is_multi_asic or
                                "t2" in setup_info["topo"] else 'True')}
        params.update(kwargs)
        # On dualtor testbed, the dst_mac for upstream traffic is vlan MAC,
        # while the src_mac for mirrored traffic is router MAC
        if setup_info.get('dualtor', False) and (direction == UP_STREAM):
            params.update({
                'dualtor_upstream': True,
                'router_mac': setup_info[direction]['egress_router_mac'],
                'vlan_mac': setup_info[direction]['ingress_router_mac']
            })
        ptf_runner(host=ptfhost,
                   testdir="acstests",
                   platform_dir="ptftests",
                   testname="everflow_tb_test.EverflowTest" if not test_name else test_name,
                   params=params,
                   socket_recv_size=DEFAULT_PTF_SOCKET_RCV_SIZE,
                   qlen=DEFAULT_PTF_QLEN,
                   log_file="/tmp/{}.{}.log".format(request.cls.__name__, request.function.__name__))

    return _partial_ptf_runner


class EverflowIPv4Tests(BaseEverflowTest):
    """Base class for testing the Everflow feature w/ IPv4."""

    DEFAULT_SRC_IP = "20.0.0.1"
    DEFAULT_DST_IP = "30.0.0.1"
    MIRROR_POLICER_UNSUPPORTED_ASIC_LIST = ["th3", "j2c+", "jr2"]

    @pytest.fixture(params=[DOWN_STREAM, UP_STREAM])
    def dest_port_type(self, setup_info, setup_mirror_session, tbinfo, request, erspan_ip_ver):        # noqa F811
        """
        This fixture parametrize  dest_port_type and can perform action based
        on that. As of now cleanup is being done here.
        """
        remote_dut = setup_info[request.param]['remote_dut']

        ip = "ipv4" if erspan_ip_ver == 4 else "ipv6"
        remote_dut.shell(remote_dut.get_vtysh_cmd_for_namespace(
            f"vtysh -c \"config\" -c \"router bgp\" -c \"address-family {ip}\" -c \"redistribute static\"",
            setup_info[request.param]["remote_namespace"]))
        # For FT2, we only cover UP_STREAM direction as traffic is always coming from LT2
        # and also going to LT2
        if tbinfo['topo']['type'] == "ft2" and request.param == DOWN_STREAM:
            pytest.skip("Skipping DOWN_STREAM test on FT2 topology.")

        yield request.param

        session_prefixes = setup_mirror_session["session_prefixes"] if erspan_ip_ver == 4 \
            else setup_mirror_session["session_prefixes_ipv6"]

        for index in range(0, min(3, len(setup_info[request.param]["dest_port"]))):
            tx_port = setup_info[request.param]["dest_port"][index]
            peer_ip = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo, ip_version=erspan_ip_ver)
            everflow_utils.remove_route(remote_dut, session_prefixes[0],
                                        peer_ip, setup_info[request.param]["remote_namespace"])
            everflow_utils.remove_route(remote_dut, session_prefixes[1],
                                        peer_ip, setup_info[request.param]["remote_namespace"])

        remote_dut.shell(remote_dut.get_vtysh_cmd_for_namespace(
            f"vtysh -c \"config\" -c \"router bgp\" -c \"address-family {ip}\" -c \"no redistribute static\"",
            setup_info[request.param]["remote_namespace"]))
        time.sleep(15)

    @pytest.fixture(autouse=True)
    def add_dest_routes(self, setup_info, tbinfo, dest_port_type):      # noqa F811
        if self.acl_stage() != 'egress':
            yield
            return

        default_traffic_port_type = DOWN_STREAM if dest_port_type == UP_STREAM else UP_STREAM

        duthost = setup_info[default_traffic_port_type]['remote_dut']
        rx_port = setup_info[default_traffic_port_type]["dest_port"][0]
        nexthop_ip = everflow_utils.get_neighbor_info(duthost, rx_port, tbinfo)

        ns = setup_info[default_traffic_port_type]["remote_namespace"]
        dst_mask = "30.0.0.0/28"

        everflow_utils.add_route(duthost, dst_mask, nexthop_ip, ns)

        yield

        everflow_utils.remove_route(duthost, dst_mask, nexthop_ip, ns)

    def test_everflow_basic_forwarding(self, setup_info, setup_mirror_session,              # noqa F811
                                       dest_port_type, ptfadapter, tbinfo,
                                       toggle_all_simulator_ports_to_rand_selected_tor,     # noqa F811
                                       setup_standby_ports_on_rand_unselected_tor_unconditionally,  # noqa F811
                                       erspan_ip_ver):                                              # noqa F811
        """
        Verify basic forwarding scenarios for the Everflow feature.

        Scenarios covered include:
            - Resolved route
            - Unresolved route
            - LPM (longest prefix match)
            - Route creation and removal
        """
        everflow_dut = setup_info[dest_port_type]['everflow_dut']
        remote_dut = setup_info[dest_port_type]['remote_dut']
        remote_dut.shell(remote_dut.get_vtysh_cmd_for_namespace(
            "vtysh -c \"configure terminal\" -c \"no ip nht resolve-via-default\"",
            setup_info[dest_port_type]["remote_namespace"]))

        # Add a route to the mirror session destination IP
        tx_port = setup_info[dest_port_type]["dest_port"][0]
        peer_ip = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo, ip_version=erspan_ip_ver)
        session_prefixes = setup_mirror_session["session_prefixes"] if erspan_ip_ver == 4 \
            else setup_mirror_session["session_prefixes_ipv6"]
        everflow_utils.add_route(remote_dut, session_prefixes[0], peer_ip,
                                 setup_info[dest_port_type]["remote_namespace"])

        time.sleep(15)

        # Verify that mirrored traffic is sent along the route we installed
        rx_port_ptf_id = setup_info[dest_port_type]["src_port_ptf_id"]
        tx_port_ptf_id = setup_info[dest_port_type]["dest_port_ptf_id"][0]
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            [tx_port_ptf_id],
            dest_port_type,
            erspan_ip_ver=erspan_ip_ver
        )
        # Add a (better) unresolved route to the mirror session destination IP
        peer_ip = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo, resolved=False,
                                                   ip_version=erspan_ip_ver)
        everflow_utils.add_route(remote_dut, session_prefixes[1], peer_ip,
                                 setup_info[dest_port_type]["remote_namespace"])
        time.sleep(15)

        # Verify that mirrored traffic is still sent along the original route
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            [tx_port_ptf_id],
            dest_port_type,
            erspan_ip_ver=erspan_ip_ver
        )

        # Remove the unresolved route
        everflow_utils.remove_route(remote_dut, session_prefixes[1],
                                    peer_ip, setup_info[dest_port_type]["remote_namespace"])

        # Add a better route to the mirror session destination IP
        tx_port = setup_info[dest_port_type]["dest_port"][1]
        peer_ip = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo, ip_version=erspan_ip_ver)
        everflow_utils.add_route(remote_dut, session_prefixes[1], peer_ip,
                                 setup_info[dest_port_type]["remote_namespace"])
        time.sleep(15)

        # Verify that mirrored traffic uses the new route
        tx_port_ptf_id = setup_info[dest_port_type]["dest_port_ptf_id"][1]
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            [tx_port_ptf_id],
            dest_port_type,
            erspan_ip_ver=erspan_ip_ver
        )

        # Remove the better route.
        everflow_utils.remove_route(remote_dut, session_prefixes[1], peer_ip,
                                    setup_info[dest_port_type]["remote_namespace"])
        time.sleep(15)

        # Verify that mirrored traffic switches back to the original route
        tx_port_ptf_id = setup_info[dest_port_type]["dest_port_ptf_id"][0]
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            [tx_port_ptf_id],
            dest_port_type,
            erspan_ip_ver=erspan_ip_ver
        )

        remote_dut.shell(remote_dut.get_vtysh_cmd_for_namespace(
            "vtysh -c \"configure terminal\" -c \"ip nht resolve-via-default\"",
            setup_info[dest_port_type]["remote_namespace"]))

    def test_everflow_neighbor_mac_change(self, setup_info, setup_mirror_session,               # noqa F811
                                          dest_port_type, ptfadapter, tbinfo,
                                          toggle_all_simulator_ports_to_rand_selected_tor,      # noqa F811
                                          setup_standby_ports_on_rand_unselected_tor_unconditionally,  # noqa F811
                                          erspan_ip_ver):                                              # noqa F811
        """Verify that session destination MAC address is changed after neighbor MAC address update."""

        everflow_dut = setup_info[dest_port_type]['everflow_dut']
        remote_dut = setup_info[dest_port_type]['remote_dut']

        # Add a route to the mirror session destination IP
        tx_port = setup_info[dest_port_type]["dest_port"][0]
        peer_ip = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo, ip_version=erspan_ip_ver)
        session_prefixes = setup_mirror_session["session_prefixes"] if erspan_ip_ver == 4 \
            else setup_mirror_session["session_prefixes_ipv6"]
        everflow_utils.add_route(remote_dut, session_prefixes[0], peer_ip,
                                 setup_info[dest_port_type]["remote_namespace"])
        time.sleep(15)

        # Verify that mirrored traffic is sent along the route we installed
        rx_port_ptf_id = setup_info[dest_port_type]["src_port_ptf_id"]
        tx_port_ptf_id = setup_info[dest_port_type]["dest_port_ptf_id"][0]
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            [tx_port_ptf_id],
            dest_port_type,
            erspan_ip_ver=erspan_ip_ver
        )

        # Update the MAC on the neighbor interface for the route we installed
        if setup_info[dest_port_type]["dest_port_lag_name"][0] != "Not Applicable":
            tx_port = setup_info[dest_port_type]["dest_port_lag_name"][0]

        remote_dut.shell(remote_dut.get_linux_ip_cmd_for_namespace(
            "ip neigh replace {} lladdr 00:11:22:33:44:55 nud permanent dev {}".
            format(peer_ip, tx_port), setup_info[dest_port_type]["remote_namespace"]))
        time.sleep(15)
        try:
            # Verify that everything still works
            self._run_everflow_test_scenarios(
                ptfadapter,
                setup_info,
                setup_mirror_session,
                everflow_dut,
                rx_port_ptf_id,
                [tx_port_ptf_id],
                dest_port_type,
                erspan_ip_ver=erspan_ip_ver
            )

        finally:

            # Clean up the test
            remote_dut.shell(
                remote_dut.get_linux_ip_cmd_for_namespace("ip neigh del {} dev {}".format(peer_ip, tx_port),
                                                          setup_info[dest_port_type]["remote_namespace"]))
            remote_dut.get_asic_or_sonic_host_from_namespace(setup_info[dest_port_type]["remote_namespace"]).command(
                "ping {} -c3".format(peer_ip))

        # Verify that everything still works
        time.sleep(10)  # for redis to get update to other lc
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            [tx_port_ptf_id],
            dest_port_type,
            erspan_ip_ver=erspan_ip_ver
        )

    def test_everflow_remove_unused_ecmp_next_hop(self, setup_info, setup_mirror_session,               # noqa F811
                                                  dest_port_type, ptfadapter, tbinfo,
                                                  toggle_all_simulator_ports_to_rand_selected_tor,      # noqa F811
                                                  setup_standby_ports_on_rand_unselected_tor_unconditionally,  # noqa F811
                                                  erspan_ip_ver):                                              # noqa F811
        """Verify that session is still active after removal of next hop from ECMP route that was not in use."""

        everflow_dut = setup_info[dest_port_type]['everflow_dut']
        remote_dut = setup_info[dest_port_type]['remote_dut']

        # Create two ECMP next hops
        tx_port = setup_info[dest_port_type]["dest_port"][0]
        peer_ip_0 = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo, ip_version=erspan_ip_ver)
        session_prefixes = setup_mirror_session["session_prefixes"] if erspan_ip_ver == 4 \
            else setup_mirror_session["session_prefixes_ipv6"]
        everflow_utils.add_route(remote_dut, session_prefixes[0], peer_ip_0,
                                 setup_info[dest_port_type]["remote_namespace"])
        time.sleep(15)

        tx_port = setup_info[dest_port_type]["dest_port"][1]
        peer_ip_1 = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo, ip_version=erspan_ip_ver)
        everflow_utils.add_route(remote_dut, session_prefixes[0], peer_ip_1,
                                 setup_info[dest_port_type]["remote_namespace"])
        time.sleep(15)

        # Verify that mirrored traffic is sent to one of the next hops
        rx_port_ptf_id = setup_info[dest_port_type]["src_port_ptf_id"]
        tx_port_ptf_ids = [
            setup_info[dest_port_type]["dest_port_ptf_id"][0],
            setup_info[dest_port_type]["dest_port_ptf_id"][1]
        ]
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            tx_port_ptf_ids,
            dest_port_type,
            erspan_ip_ver=erspan_ip_ver
        )

        # Remaining Scenario not applicable for this topology
        if len(setup_info[dest_port_type]["dest_port"]) <= 2:
            return

        # Add another ECMP next hop
        tx_port = setup_info[dest_port_type]["dest_port"][2]
        peer_ip = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo, ip_version=erspan_ip_ver)
        everflow_utils.add_route(remote_dut, session_prefixes[0], peer_ip,
                                 setup_info[dest_port_type]["remote_namespace"])
        time.sleep(15)

        # Verify that mirrored traffic is not sent to this new next hop
        tx_port_ptf_id = setup_info[dest_port_type]["dest_port_ptf_id"][2]
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            [tx_port_ptf_id],
            dest_port_type,
            expect_recv=False,
            valid_across_namespace=False,
            erspan_ip_ver=erspan_ip_ver
        )

        # Remove the extra hop
        everflow_utils.remove_route(remote_dut, session_prefixes[0], peer_ip,
                                    setup_info[dest_port_type]["remote_namespace"])
        time.sleep(15)

        # Verify that mirrored traffic is not sent to the deleted next hop
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            [tx_port_ptf_id],
            dest_port_type,
            expect_recv=False,
            valid_across_namespace=False,
            erspan_ip_ver=erspan_ip_ver
        )

        # Verify that mirrored traffic is still sent to one of the original next hops
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            tx_port_ptf_ids,
            dest_port_type,
            erspan_ip_ver=erspan_ip_ver
        )

    def test_everflow_remove_used_ecmp_next_hop(self, setup_info, setup_mirror_session,                 # noqa F811
                                                dest_port_type, ptfadapter, tbinfo,
                                                toggle_all_simulator_ports_to_rand_selected_tor,        # noqa F811
                                                setup_standby_ports_on_rand_unselected_tor_unconditionally,  # noqa F811
                                                erspan_ip_ver):                                              # noqa F811
        """Verify that session is still active after removal of next hop from ECMP route that was in use."""

        everflow_dut = setup_info[dest_port_type]['everflow_dut']
        remote_dut = setup_info[dest_port_type]['remote_dut']

        if tbinfo['topo']['type'] == "t2":
            if everflow_dut.facts['switch_type'] == "voq":
                pytest.skip("Skip test as is not supported on a VoQ chassis.")

        # Remaining Scenario not applicable for this topology
        if len(setup_info[dest_port_type]["dest_port"]) <= 2:
            pytest.skip("Skip test as not enough neighbors/ports.")

        # Add a route to the mirror session destination IP
        tx_port = setup_info[dest_port_type]["dest_port"][0]
        peer_ip_0 = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo, ip_version=erspan_ip_ver)
        session_prefixes = setup_mirror_session["session_prefixes"] if erspan_ip_ver == 4 \
            else setup_mirror_session["session_prefixes_ipv6"]
        everflow_utils.add_route(remote_dut, session_prefixes[0], peer_ip_0,
                                 setup_info[dest_port_type]["remote_namespace"])
        time.sleep(15)

        # Verify that mirrored traffic is sent along the route we installed
        rx_port_ptf_id = setup_info[dest_port_type]["src_port_ptf_id"]
        tx_port_ptf_id = setup_info[dest_port_type]["dest_port_ptf_id"][0]
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            [tx_port_ptf_id],
            dest_port_type,
            erspan_ip_ver=erspan_ip_ver
        )

        # Add two new ECMP next hops
        tx_port = setup_info[dest_port_type]["dest_port"][1]
        peer_ip_1 = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo, ip_version=erspan_ip_ver)
        everflow_utils.add_route(remote_dut, session_prefixes[0], peer_ip_1,
                                 setup_info[dest_port_type]["remote_namespace"])

        tx_port = setup_info[dest_port_type]["dest_port"][2]
        peer_ip_2 = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo, ip_version=erspan_ip_ver)
        everflow_utils.add_route(remote_dut, session_prefixes[0], peer_ip_2,
                                 setup_info[dest_port_type]["remote_namespace"])
        time.sleep(15)

        # Verify that traffic is still sent along the original next hop
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            [tx_port_ptf_id],
            dest_port_type,
            valid_across_namespace=False,
            erspan_ip_ver=erspan_ip_ver
        )

        # Verify that traffic is not sent along either of the new next hops
        tx_port_ptf_ids = [
            setup_info[dest_port_type]["dest_port_ptf_id"][1],
            setup_info[dest_port_type]["dest_port_ptf_id"][2]
        ]
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            tx_port_ptf_ids,
            dest_port_type,
            expect_recv=False,
            valid_across_namespace=False,
            erspan_ip_ver=erspan_ip_ver
        )

        # Remove the original next hop
        everflow_utils.remove_route(remote_dut, session_prefixes[0], peer_ip_0,
                                    setup_info[dest_port_type]["remote_namespace"])
        time.sleep(15)

        # Verify that mirrored traffic is no longer sent along the original next hop
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            [tx_port_ptf_id],
            dest_port_type,
            expect_recv=False,
            erspan_ip_ver=erspan_ip_ver
        )

        # Verify that mirrored traffis is now sent along either of the new next hops
        self._run_everflow_test_scenarios(
            ptfadapter,
            setup_info,
            setup_mirror_session,
            everflow_dut,
            rx_port_ptf_id,
            tx_port_ptf_ids,
            dest_port_type,
            erspan_ip_ver=erspan_ip_ver
        )

    def test_everflow_dscp_with_policer(
            self,
            setup_info,                                         # noqa F811
            policer_mirror_session,
            dest_port_type,
            partial_ptf_runner,
            config_method,
            tbinfo,
            toggle_all_simulator_ports_to_rand_selected_tor,    # noqa F811
            setup_standby_ports_on_rand_unselected_tor_unconditionally,  # noqa F811
            erspan_ip_ver                                                # noqa F811
    ):
        """Verify that we can rate-limit mirrored traffic from the MIRROR_DSCP table.
        This tests single rate three color policer mode and specifically checks CIR value
        and switch behaviour under condition when CBS is assumed to be fully absorbed while
        sending traffic over a period of time. Received packets are accumulated and actual
        receive rate is calculated and compared with CIR value with tollerance range 10%.
        """
        if erspan_ip_ver == 6:
            pytest.skip("EverflowPolicerTest does not support IPv6.")

        # Add explicit for regular packet so that it's dest port is different then mirror port
        # NOTE: This is important to add since for the Policer test case regular packets
        # and mirror packets can go to same interface, which causes tail drop of
        # police packets and impacts test case cir/cbs calculation.

        everflow_dut = setup_info[dest_port_type]['everflow_dut']
        remote_dut = setup_info[dest_port_type]['remote_dut']

        vendor = everflow_dut.facts["asic_type"]
        hostvars = everflow_dut.host.options['variable_manager']._hostvars[everflow_dut.hostname]

        everflow_tolerance = 10
        if vendor == 'marvell-teralynx':
            everflow_tolerance = 11

        rate_limit = 100
        if vendor in ["marvell-prestera", "marvell"]:
            rate_limit = rate_limit * 1.25

        send_time = "10"
        if vendor == "mellanox":
            send_time = "75"

        for asic in self.MIRROR_POLICER_UNSUPPORTED_ASIC_LIST:
            vendorAsic = "{0}_{1}_hwskus".format(vendor, asic)
            if vendorAsic in list(hostvars.keys()) and everflow_dut.facts['hwsku'] in hostvars[vendorAsic]:
                pytest.skip("Skipping test since mirror policing is not supported on {0} {1} platforms"
                            .format(vendor, asic))

        if setup_info['topo'] in ['t0', 'm0_vlan']:
            default_tarffic_port_type = dest_port_type
            # Use the second portchannel as missor session nexthop
            tx_port = setup_info[dest_port_type]["dest_port"][1]
        else:
            default_tarffic_port_type = DOWN_STREAM if dest_port_type == UP_STREAM else UP_STREAM
            tx_port = setup_info[dest_port_type]["dest_port"][0]
        default_traffic_tx_port = setup_info[default_tarffic_port_type]["dest_port"][0]
        default_traffic_peer_ip = everflow_utils.get_neighbor_info(everflow_dut, default_traffic_tx_port, tbinfo)
        everflow_utils.add_route(everflow_dut, self.DEFAULT_DST_IP + "/32", default_traffic_peer_ip,
                                 setup_info[default_tarffic_port_type]["remote_namespace"])
        time.sleep(15)

        # Add explicit route for the mirror session
        peer_ip = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo)
        session_prefixes = policer_mirror_session["session_prefixes"]
        everflow_utils.add_route(remote_dut, session_prefixes[0], peer_ip,
                                 setup_info[dest_port_type]["remote_namespace"])
        time.sleep(15)

        try:
            # Add MIRROR_DSCP table for test
            table_name = "EVERFLOW_DSCP"
            table_type = "MIRROR_DSCP"
            bind_interface_namespace = setup_info[dest_port_type]["everflow_namespace"]
            rx_port_ptf_id = setup_info[dest_port_type]["src_port_ptf_id"]
            tx_port_ptf_id = setup_info[dest_port_type]["dest_port_ptf_id"][0]
            if setup_info['topo'] in ['t0', 'm0_vlan'] and self.acl_stage() == "egress":
                # For T0 upstream, the EVERFLOW_DSCP table is binded to one of portchannels
                bind_interface = setup_info[dest_port_type]["dest_port_lag_name"][0]
                mirror_port_id = setup_info[dest_port_type]["dest_port_ptf_id"][1]
            else:
                bind_interface = setup_info[dest_port_type]["src_port"]
                mirror_port_id = tx_port_ptf_id
                if setup_info[dest_port_type]["src_port_lag_name"] != "Not Applicable":
                    bind_interface = setup_info[dest_port_type]["src_port_lag_name"]

            # Temp change for multi-asic to create acl table in host and namespace
            # Will be removed once CLI is command is enahnced to work across all namespaces.
            self.apply_acl_table_config(everflow_dut, table_name, table_type, config_method, [bind_interface])
            if bind_interface_namespace:
                self.apply_acl_table_config(everflow_dut, table_name, table_type, config_method,
                                            [bind_interface], bind_interface_namespace)
            # Add rule to match on DSCP
            self.apply_acl_rule_config(everflow_dut,
                                       table_name,
                                       policer_mirror_session["session_name"],
                                       config_method,
                                       rules=EVERFLOW_DSCP_RULES)

            # Run test with expected CIR/CBS in packets/sec and tolerance %
            partial_ptf_runner(setup_info,
                               dest_port_type,
                               policer_mirror_session,
                               self.acl_stage(),
                               self.mirror_type(),
                               expect_receive=True,
                               test_name="everflow_policer_test.EverflowPolicerTest",
                               src_port=rx_port_ptf_id,
                               dst_mirror_ports=mirror_port_id,
                               dst_ports=tx_port_ptf_id,
                               meter_type="packets",
                               cir=rate_limit,
                               cbs=rate_limit,
                               send_time=send_time,
                               tolerance=everflow_tolerance)
        finally:
            # Clean up ACL rules and routes
            BaseEverflowTest.remove_acl_rule_config(everflow_dut, table_name, config_method)
            self.remove_acl_table_config(everflow_dut, table_name, config_method)
            if bind_interface_namespace:
                self.remove_acl_table_config(everflow_dut, table_name, config_method, bind_interface_namespace)
            everflow_utils.remove_route(remote_dut, session_prefixes[0], peer_ip,
                                        setup_info[dest_port_type]["remote_namespace"])
            everflow_utils.remove_route(everflow_dut, self.DEFAULT_DST_IP + "/32", default_traffic_peer_ip,
                                        setup_info[default_tarffic_port_type]["remote_namespace"])

    def test_everflow_frwd_with_bkg_trf(self,
                                        setup_info,  # noqa F811
                                        setup_mirror_session,
                                        dest_port_type, ptfadapter, tbinfo,
                                        erspan_ip_ver, toggle_all_simulator_ports_to_rand_selected_tor,  # noqa F811
                                        setup_standby_ports_on_rand_unselected_tor_unconditionally):  # noqa F811
        """
        Verify basic forwarding scenarios for the Everflow feature with background traffic.
        Background Traffic PKT1 IP in IP with same ports & macs but with dummy ips
        Background Traffic PKT2 Packer with same ports & macs but with dummy ips
        """
        everflow_dut = setup_info[dest_port_type]['everflow_dut']
        remote_dut = setup_info[dest_port_type]['remote_dut']
        remote_dut.shell(remote_dut.get_vtysh_cmd_for_namespace(
            "vtysh -c \"configure terminal\" -c \"no ip nht resolve-via-default\"",
            setup_info[dest_port_type]["remote_namespace"]))

        # Add a route to the mirror session destination IP
        tx_port = setup_info[dest_port_type]["dest_port"][0]
        peer_ip = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo, ip_version=erspan_ip_ver)
        session_prefixes = setup_mirror_session["session_prefixes"] if erspan_ip_ver == 4 \
            else setup_mirror_session["session_prefixes_ipv6"]
        everflow_utils.add_route(remote_dut, session_prefixes[0], peer_ip,
                                 setup_info[dest_port_type]["remote_namespace"])

        time.sleep(15)

        # Verify that mirrored traffic is sent along the route we installed
        rx_port_ptf_id = setup_info[dest_port_type]["src_port_ptf_id"]
        tx_port_ptf_id = setup_info[dest_port_type]["dest_port_ptf_id"][0]

        # Events to control the thread
        stop_thread = threading.Event()
        cmd = 'show ip bgp summary'
        parse_result = everflow_dut.show_and_parse(cmd)
        neigh_ipv4 = {entry['neighborname']: entry['neighbhor'] for entry in parse_result}
        if len(neigh_ipv4) < 2:
            pytest.skip("Skipping as Less than 2 Neigbhour")

        def background_traffic(run_count=None):
            selected_addrs1 = random.sample(list(neigh_ipv4.values()), 2)
            selected_addrs2 = random.sample(list(neigh_ipv4.values()), 2)
            selected_addrs3 = random.sample(list(neigh_ipv4.values()), 2)
            router_mac = setup_info[dest_port_type]["ingress_router_mac"]
            inner_pkt2 = self._base_tcp_packet(ptfadapter, setup_info, router_mac, src_ip=selected_addrs1[1],
                                               dst_ip=selected_addrs1[0])
            packets = [
                testutils.simple_ipv4ip_packet(
                    ip_src=selected_addrs2[0],
                    eth_dst=router_mac,
                    ip_dst=selected_addrs2[1],
                    eth_src=ptfadapter.dataplane.get_mac(*list(ptfadapter.dataplane.ports.keys())[0]),
                    inner_frame=inner_pkt2[scapy.IP]
                ),
                self._base_tcp_packet(ptfadapter, setup_info, router_mac, src_ip=selected_addrs3[0],
                                      dst_ip=selected_addrs3[1])
            ]

            count = 0
            if run_count is None:
                logger.debug("Background Traffic Started")
            # Run the loop either for a specified count or until stop_thread is set
            while (run_count is None and not stop_thread.is_set()) or (run_count is not None and count < run_count):
                for bkg_trf in packets:
                    # Send packet
                    time.sleep(.1)
                    testutils.send(ptfadapter, rx_port_ptf_id, bkg_trf)

                    # Verify packet if run_count is specified
                    if run_count is not None:
                        exp_pkt = Mask(bkg_trf)
                        exp_pkt.set_do_not_care_scapy(scapy.IP, "id")
                        exp_pkt.set_do_not_care_scapy(packet.Ether, 'dst')
                        exp_pkt.set_do_not_care_scapy(packet.Ether, 'src')
                        exp_pkt.set_do_not_care_scapy(packet.IP, "chksum")
                        exp_pkt.set_do_not_care_scapy(packet.IP, 'ttl')
                        testutils.verify_packet_any_port(
                            ptfadapter,
                            exp_pkt,
                            ports=ptfadapter.ptf_port_set
                        )

                count += 1

        background_traffic(run_count=1)
        background_thread = threading.Thread(target=background_traffic)
        background_thread.daemon = True
        background_thread.start()

        try:
            self._run_everflow_test_scenarios(
                ptfadapter,
                setup_info,
                setup_mirror_session,
                everflow_dut,
                rx_port_ptf_id,
                [tx_port_ptf_id],
                dest_port_type,
                erspan_ip_ver=erspan_ip_ver
            )

            # Add a (better) unresolved route to the mirror session destination IP
            peer_ip = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo, resolved=False,
                                                       ip_version=erspan_ip_ver)
            everflow_utils.add_route(remote_dut, session_prefixes[1], peer_ip,
                                     setup_info[dest_port_type]["remote_namespace"])
            time.sleep(15)
            background_traffic(run_count=1)

            # Verify that mirrored traffic is still sent along the original route
            self._run_everflow_test_scenarios(
                ptfadapter,
                setup_info,
                setup_mirror_session,
                everflow_dut,
                rx_port_ptf_id,
                [tx_port_ptf_id],
                dest_port_type,
                erspan_ip_ver=erspan_ip_ver
            )

            # Remove the unresolved route
            everflow_utils.remove_route(remote_dut, session_prefixes[1],
                                        peer_ip, setup_info[dest_port_type]["remote_namespace"])

            # Add a better route to the mirror session destination IP
            tx_port = setup_info[dest_port_type]["dest_port"][1]
            peer_ip = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo, ip_version=erspan_ip_ver)
            everflow_utils.add_route(remote_dut, session_prefixes[1], peer_ip,
                                     setup_info[dest_port_type]["remote_namespace"])
            time.sleep(15)
            background_traffic(run_count=1)
            # Verify that mirrored traffic uses the new route
            tx_port_ptf_id = setup_info[dest_port_type]["dest_port_ptf_id"][1]
            self._run_everflow_test_scenarios(
                ptfadapter,
                setup_info,
                setup_mirror_session,
                everflow_dut,
                rx_port_ptf_id,
                [tx_port_ptf_id],
                dest_port_type,
                erspan_ip_ver=erspan_ip_ver
            )

            # Remove the better route.
            everflow_utils.remove_route(remote_dut, session_prefixes[1], peer_ip,
                                        setup_info[dest_port_type]["remote_namespace"])
            time.sleep(15)
            background_traffic(run_count=1)
            # Verify that mirrored traffic switches back to the original route
            tx_port_ptf_id = setup_info[dest_port_type]["dest_port_ptf_id"][0]
            self._run_everflow_test_scenarios(
                ptfadapter,
                setup_info,
                setup_mirror_session,
                everflow_dut,
                rx_port_ptf_id,
                [tx_port_ptf_id],
                dest_port_type,
                erspan_ip_ver=erspan_ip_ver
            )

            remote_dut.shell(remote_dut.get_vtysh_cmd_for_namespace(
                "vtysh -c \"configure terminal\" -c \"ip nht resolve-via-default\"",
                setup_info[dest_port_type]["remote_namespace"]))
        finally:
            # Signal the background thread to stop
            logger.debug("Thread_stop")
            stop_thread.set()
            # Wait for the background thread to finish
            background_thread.join()
            background_traffic(run_count=1)

    def test_everflow_fwd_recircle_port_queue_check(self, setup_info, setup_mirror_session, # noqa F811
                                                    dest_port_type, ptfadapter, tbinfo,
                                       toggle_all_simulator_ports_to_rand_selected_tor,     # noqa F811
                                       setup_standby_ports_on_rand_unselected_tor_unconditionally):    # noqa F811
        """
        Verify basic forwarding scenario with mirror session config having specific queue for the Everflow feature.
        Make sure the mirrored packets are sent via the specific queue on recircle port
        """
        duthost_set = BaseEverflowTest.get_duthost_set(setup_info)
        everflow_dut = setup_info[dest_port_type]['everflow_dut']
        remote_dut = setup_info[dest_port_type]['remote_dut']
        remote_dut.shell(remote_dut.get_vtysh_cmd_for_namespace(
            "vtysh -c \"configure terminal\" -c \"no ip nht resolve-via-default\"",
            setup_info[dest_port_type]["remote_namespace"]))

        def update_acl_rule_config(table_name, session_name, config_method, rules=everflow_utils.EVERFLOW_V4_RULES):
            rules_config = everflow_utils.load_acl_rules_config(table_name,
                                                                os.path.join(everflow_utils.FILE_DIR, rules))
            rules_config['rules'] = [
                rule for rule in rules_config['rules']
                if 'transport' not in rule.get('qualifiers', {}) or
                   not any('tcp-flags' in x for x in rule.get('qualifiers', {}).get('transport', {}).keys())
            ]
            duthost.host.options["variable_manager"].extra_vars.update(rules_config)

            if config_method == everflow_utils.CONFIG_MODE_CLI:
                duthost.template(src=os.path.join(everflow_utils.TEMPLATE_DIR,
                                                  everflow_utils.EVERFLOW_RULE_CREATE_TEMPLATE),
                                 dest=os.path.join(everflow_utils.DUT_RUN_DIR,
                                                   everflow_utils.EVERFLOW_RULE_CREATE_FILE))

                command = "acl-loader update full {} --table_name {} --session_name {}" \
                    .format(os.path.join(everflow_utils.DUT_RUN_DIR, everflow_utils.EVERFLOW_RULE_CREATE_FILE),
                            table_name,
                            session_name)

                # NOTE: Until the repo branches, we're only applying the flag
                # on egress mirroring to preserve backwards compatibility.
                if self.mirror_type() == "egress":
                    command += " --mirror_stage {}".format(self.mirror_type())

            elif config_method == everflow_utils.CONFIG_MODE_CONFIGLET:
                pass

            everflow_utils.check_rule_creation_on_dut(duthost, command)

        table_name = "EVERFLOW" if self.acl_stage() == "ingress" else "EVERFLOW_EGRESS"

        for duthost in duthost_set:
            BaseEverflowTest.remove_acl_rule_config(duthost, table_name, everflow_utils.CONFIG_MODE_CLI)

        for duthost in duthost_set:
            update_acl_rule_config(table_name, setup_mirror_session["session_name"], everflow_utils.CONFIG_MODE_CLI)

        def configure_mirror_session_with_queue(mirror_session, queue_num):
            if mirror_session["session_name"]:
                remove_command = "config mirror_session remove {}".format(mirror_session["session_name"])
                for duthost in duthost_set:
                    duthost.command(remove_command)
                add_command = "config mirror_session add {} {} {} {} {} {} {}" \
                    .format(mirror_session["session_name"],
                            mirror_session["session_src_ip"],
                            mirror_session["session_dst_ip"],
                            mirror_session["session_dscp"],
                            mirror_session["session_ttl"],
                            mirror_session["session_gre"],
                            queue_num)
                for duthost in duthost_set:
                    duthost.command(add_command)
            else:
                pytest.skip("Mirror session info is empty, can't proceed further!")

        queue = str(random.randint(1, 7))
        # Apply mirror session config with a different queue value other than default '0'
        configure_mirror_session_with_queue(setup_mirror_session, queue)

        # Add a route to the mirror session destination IP
        tx_port = setup_info[dest_port_type]["dest_port"][0]
        peer_ip = everflow_utils.get_neighbor_info(remote_dut, tx_port, tbinfo)
        everflow_utils.add_route(remote_dut, setup_mirror_session["session_prefixes"][0], peer_ip,
                                 setup_info[dest_port_type]["remote_namespace"])

        time.sleep(15)

        # Verify that mirrored traffic is sent along the route we installed
        rx_port_ptf_id = setup_info[dest_port_type]["src_port_ptf_id"]
        tx_port_ptf_id = setup_info[dest_port_type]["dest_port_ptf_id"][0]
        src_port = setup_info[dest_port_type]["src_port"]
        # Clear queue counters for the port asic instance
        asic_ns = everflow_dut.get_port_asic_instance(src_port).get_asic_namespace()
        if asic_ns is not None:
            asic_id = everflow_dut.get_asic_id_from_namespace(asic_ns)
            recircle_port = "Ethernet-Rec{}".format(asic_id)
        else:
            # For single-asic SKUs, recircle port is Ethernet-Rec0
            recircle_port = "Ethernet-Rec0"

        try:
            everflow_utils.verify_mirror_packets_on_recircle_port(
                self,
                ptfadapter,
                setup_info,
                setup_mirror_session,
                everflow_dut,
                rx_port_ptf_id,
                [tx_port_ptf_id],
                dest_port_type,
                queue,
                asic_ns,
                recircle_port
            )
        finally:
            remote_dut.shell(remote_dut.get_vtysh_cmd_for_namespace(
                "vtysh -c \"configure terminal\" -c \"ip nht resolve-via-default\"",
                setup_info[dest_port_type]["remote_namespace"]))

    def _run_everflow_test_scenarios(self, ptfadapter, setup, mirror_session, duthost, rx_port,
                                     tx_ports, direction, expect_recv=True, valid_across_namespace=True,
                                     erspan_ip_ver=4):  # noqa F811
        # FIXME: In the ptf_runner version of these tests, LAGs were passed down to the tests
        # as comma-separated strings of LAG member port IDs (e.g. portchannel0001 -> "2,3").
        # Because the DSCP test is still using ptf_runner we will preserve this for now,
        # but we should try to make the format a little more friendly once the DSCP test also gets converted.
        tx_port_ids = self._get_tx_port_id_list(tx_ports)
        target_ip = "30.0.0.10"
        default_ip = self.DEFAULT_DST_IP
        if setup['topo'] in ['t0', 'm0_vlan'] and direction == DOWN_STREAM:
            target_ip = TARGET_SERVER_IP
            default_ip = DEFAULT_SERVER_IP

        router_mac = setup[direction]["ingress_router_mac"]

        pkt_dict = {
            "(src ip)": self._base_tcp_packet(ptfadapter, setup, router_mac, src_ip="20.0.0.10", dst_ip=default_ip),
            "(dst ip)": self._base_tcp_packet(ptfadapter, setup, router_mac, dst_ip=target_ip),
            "(l4 src port)": self._base_tcp_packet(ptfadapter, setup, router_mac, sport=0x1235, dst_ip=default_ip),
            "(l4 dst port)": self._base_tcp_packet(ptfadapter, setup, router_mac, dport=0x1235, dst_ip=default_ip),
            "(ip protocol)": self._base_tcp_packet(ptfadapter, setup, router_mac, ip_protocol=0x7E, dst_ip=default_ip),
            "(tcp flags)": self._base_tcp_packet(ptfadapter, setup, router_mac, flags=0x12, dst_ip=default_ip),
            "(l4 src range)": self._base_tcp_packet(ptfadapter, setup, router_mac, sport=4675, dst_ip=default_ip),
            "(l4 dst range)": self._base_tcp_packet(ptfadapter, setup, router_mac, dport=4675, dst_ip=default_ip),
            "(dscp)": self._base_tcp_packet(ptfadapter, setup, router_mac, dscp=51, dst_ip=default_ip)
        }

        for description, pkt in list(pkt_dict.items()):
            logging.info("Sending packet with qualifier set %s to DUT" % description)
            self.send_and_check_mirror_packets(
                setup,
                mirror_session,
                ptfadapter,
                duthost,
                pkt,
                direction,
                src_port=rx_port,
                dest_ports=tx_port_ids,
                expect_recv=expect_recv,
                valid_across_namespace=valid_across_namespace,
                erspan_ip_ver=erspan_ip_ver
            )

    def _base_tcp_packet(
        self,
        ptfadapter,
        setup,
        router_mac,
        src_ip=DEFAULT_SRC_IP,
        dst_ip=DEFAULT_DST_IP,
        ip_protocol=None,
        dscp=None,
        sport=0x1234,
        dport=0x50,
        flags=0x10
    ):
        pkt = testutils.simple_tcp_packet(
            eth_src=ptfadapter.dataplane.get_mac(*list(ptfadapter.dataplane.ports.keys())[0]),
            eth_dst=router_mac,
            ip_src=src_ip,
            ip_dst=dst_ip,
            ip_ttl=64,
            ip_dscp=dscp,
            tcp_sport=sport,
            tcp_dport=dport,
            tcp_flags=flags
        )

        if ip_protocol:
            pkt["IP"].proto = ip_protocol

        return pkt


class TestEverflowV4IngressAclIngressMirror(EverflowIPv4Tests):
    def acl_stage(self):
        return "ingress"

    def mirror_type(self):
        return "ingress"


class TestEverflowV4IngressAclEgressMirror(EverflowIPv4Tests):
    def acl_stage(self):
        return "ingress"

    def mirror_type(self):
        return "egress"


class TestEverflowV4EgressAclIngressMirror(EverflowIPv4Tests):
    def acl_stage(self):
        return "egress"

    def mirror_type(self):
        return "ingress"


class TestEverflowV4EgressAclEgressMirror(EverflowIPv4Tests):
    def acl_stage(self):
        return "egress"

    def mirror_type(self):
        return "egress"
